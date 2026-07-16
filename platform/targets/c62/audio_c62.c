/*
 * SPDX-FileCopyrightText: Copyright 2020-2026 OpenRTX Contributors
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <zephyr/logging/log.h>
#include <zephyr/kernel.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/device.h>
#include "AudioSystem.h"
#include "AudioTrack.h"
#include "AudioRecord.h"
#include <assert.h>
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <interfaces/audio.h>
#include <interfaces/radio.h>

#define BRIDGE_SAMPLE_RATE (48000U) // Patched DSP's native ADC/DAC rate

#define C62_M17_INPUT_RATE (24000U)
#define C62_ADC_NATIVE_RATE (48000U)
#define C62_RATE_REPORT_MS (2000U)

// Live audio streaming configuration
#define AUDIO_CHUNK_MS (20) // 20ms chunks for low latency
#define AUDIO_CHUNK_SAMPLES (BRIDGE_SAMPLE_RATE * AUDIO_CHUNK_MS / 1000)
#define AUDIO_CHUNK_SIZE (AUDIO_CHUNK_SAMPLES * sizeof(int16_t))
#define AUDIO_BUFFER_CHUNKS (8) // Circular buffer with multiple chunks
#define AUDIO_THREAD_STACK_SIZE (8 * 1024)
#define AUDIO_THREAD_PRIORITY (5)

#define PATH(x, y) ((x << 4) | y)

// Circular buffer for audio data exchange between threads
typedef struct {
    uint8_t buffer[AUDIO_CHUNK_SIZE];
    bool filled;
} AudioChunk;

typedef struct {
    AudioChunk chunks[AUDIO_BUFFER_CHUNKS];
    int write_idx;
    int read_idx;
    struct k_mutex mutex;
    struct k_sem data_ready;
    struct k_sem space_available;
} AudioRingBuffer;

#define C62_AUDIO_CHANNEL_MIC CHANNEL_IN_LEFT
#define C62_AUDIO_CHANNEL_RX CHANNEL_IN_RIGHT
#define C62_AUDIO_CHANNEL_SPK CHANNEL_OUT_FRONT_LEFT
#define C62_AUDIO_CHANNEL_TX CHANNEL_OUT_FRONT_RIGHT

#define AUDIO_INPUT_CHANNEL_COUNT 2

static const struct gpio_dt_spec speaker_enable =
    GPIO_DT_SPEC_GET(DT_PATH(gpio_controls, speaker_enable), gpios);

// Live audio streaming state
typedef struct {
    enum AudioSource source;
    enum AudioSink sink;
    bool active;
} AudioPath;

static AudioPath s_active_paths[3]; // Support up to 3 simultaneous paths
static int s_active_path_count = 0;
static struct k_mutex s_audio_mutex;

// Audio devices for each source/sink
static AudioRecord s_record;
static AudioTrack s_track_spk;
static AudioTrack s_track_rtx_output;

static bool s_audio_initialized = false;
static bool s_audio_streaming = false;
static bool s_bridge_initialized = false;

// Track which devices are currently active
static bool s_mic_active = false;
static bool s_rtx_input_active = false;
static bool s_spk_active = false;
static bool s_rtx_output_active = false;

static const struct audioDriver c62_input_audio_driver;
static const struct audioDriver c62_output_audio_driver;

const struct audioDevice outputDevices[] = {
    { NULL, 0, 0, SINK_MCU },
    { &c62_output_audio_driver, (const void *)(uintptr_t)C62_AUDIO_CHANNEL_TX,
      0, SINK_RTX },
    { &c62_output_audio_driver, (const void *)(uintptr_t)C62_AUDIO_CHANNEL_SPK,
      1, SINK_SPK },
};

const struct audioDevice inputDevices[] = {
    { NULL, 0, 0, SOURCE_MCU },
    { &c62_input_audio_driver, (const void *)(uintptr_t)C62_AUDIO_CHANNEL_RX, 0,
      SOURCE_RTX },
    { &c62_input_audio_driver, (const void *)(uintptr_t)C62_AUDIO_CHANNEL_MIC,
      1, SOURCE_MIC },
};

// Ring buffer for audio data exchange (in PSRAM)
__attribute__((section(".psram_section"))) static AudioRingBuffer s_ring_buffer;

// Separate threads for input and output
static struct k_thread s_audio_input_thread;
static struct k_thread s_audio_output_thread;
K_THREAD_STACK_DEFINE(s_audio_input_stack, AUDIO_THREAD_STACK_SIZE);
K_THREAD_STACK_DEFINE(s_audio_output_stack, AUDIO_THREAD_STACK_SIZE);

/*
 * The C62 DSP can capture the ADC natively at 48 kHz, but OpenRTX currently
 * configures its M17 demodulator for 24 kHz.  Capture at 48 kHz and apply a
 * real low-pass decimator to satisfy that existing interface.  M17 can also
 * operate directly at 48 kHz; once the complete firmware path uses that rate,
 * this conversion is no longer needed.
 *
 * Opening a 48 kHz record stream requires the C62 DSP patch.  The stock DSP
 * firmware rejects every input rate except 16 kHz.
 *
 * 31-tap, unity-gain Blackman-windowed low-pass, Fc = 8 kHz at Fs = 48 kHz.
 * The coefficients are Q15.  Response is approximately -0.1 dB at 4.8 kHz
 * and -63 dB at the new 12 kHz Nyquist frequency.
 */
#define C62_DECIM_TAPS 31
static const int16_t c62_decim_coeffs[C62_DECIM_TAPS] = {
    0,     3,   12,   0,    -63,   -117, 0,    327, 509,   0,     -1138,
    -1685, 0,   4203, 8874, 10918, 8874, 4203, 0,   -1685, -1138, 0,
    509,   327, 0,    -117, -63,   0,    12,   3,   0,
};

typedef struct {
    AudioRecord record;
    struct streamCtx *ctx;
    stream_sample_t *interleaved_buffer;
    stream_sample_t *raw_buffer;
    stream_sample_t *ready_buffer;
    size_t block_samples;
    size_t raw_samples;
    uint32_t physical_rate;
    uint32_t record_channel_mask;
    uint8_t record_channels;
    uint8_t channel_index;
    uint8_t next_half;
    uint8_t instance;
    bool opened;
    bool decimate_by_two;
    int16_t fir_delay[C62_DECIM_TAPS];
    size_t fir_pos;
    uint8_t fir_phase;
    uint64_t sample_count;
    int64_t rate_start_ms;
    int64_t rate_report_ms;
} C62InputStream;

typedef struct {
    AudioTrack track;
    struct streamCtx *ctx;
    size_t block_samples;
    uint8_t next_half;
    uint8_t instance;
    bool opened;
    bool stop_requested;
    uint64_t sample_count;
    int64_t rate_start_ms;
    int64_t rate_report_ms;
} C62OutputStream;

static C62InputStream s_input_streams[2];
static C62OutputStream s_output_streams[2];

static int16_t q15_saturate(int64_t value)
{
    value += 1LL << 14;
    value >>= 15;

    if (value > INT16_MAX)
        return INT16_MAX;
    if (value < INT16_MIN)
        return INT16_MIN;
    return (int16_t)value;
}

static void decimate_48k_to_24k(C62InputStream *stream,
                                const stream_sample_t *input,
                                size_t input_samples, stream_sample_t *output)
{
    size_t out_pos = 0;

    for (size_t i = 0; i < input_samples; i++) {
        stream->fir_delay[stream->fir_pos] = input[i];
        stream->fir_pos = (stream->fir_pos + 1) % C62_DECIM_TAPS;
        stream->fir_phase ^= 1;

        if (stream->fir_phase != 0)
            continue;

        int64_t accumulator = 0;
        size_t pos = stream->fir_pos;
        for (size_t tap = 0; tap < C62_DECIM_TAPS; tap++) {
            pos = (pos == 0) ? C62_DECIM_TAPS - 1 : pos - 1;
            accumulator += (int32_t)stream->fir_delay[pos]
                         * c62_decim_coeffs[tap];
        }
        output[out_pos++] = q15_saturate(accumulator);
    }

    __ASSERT(out_pos == input_samples / 2, "invalid C62 decimator phase");
}

static void report_input_rate(C62InputStream *stream)
{
    int64_t now = k_uptime_get();
    if ((now - stream->rate_report_ms) < C62_RATE_REPORT_MS)
        return;

    int64_t elapsed = now - stream->rate_start_ms;
    uint32_t measured = 0;
    if (elapsed > 0)
        measured = (uint32_t)((stream->sample_count * 1000U) / elapsed);

    printk("C62RATE input=%u logical=%u physical=%u samples=%u ms=%u "
           "measured=%u\n",
           stream->instance, stream->ctx->sampleRate, stream->physical_rate,
           (uint32_t)stream->sample_count, (uint32_t)elapsed, measured);
    stream->rate_report_ms = now;
}

static void report_output_rate(C62OutputStream *stream)
{
    int64_t now = k_uptime_get();
    if ((now - stream->rate_report_ms) < C62_RATE_REPORT_MS)
        return;

    int64_t elapsed = now - stream->rate_start_ms;
    uint32_t measured = 0;
    if (elapsed > 0)
        measured = (uint32_t)((stream->sample_count * 1000U) / elapsed);

    printk("C62RATE output=%u rate=%u samples=%u ms=%u submitted=%u\n",
           stream->instance, stream->ctx->sampleRate,
           (uint32_t)stream->sample_count, (uint32_t)elapsed, measured);
    stream->rate_report_ms = now;
}

static void c62_input_close(C62InputStream *stream)
{
    struct streamCtx *ctx = stream->ctx;

    if (stream->opened)
        AudioRecord_dtor(&stream->record);
    if (stream->interleaved_buffer != NULL)
        k_free(stream->interleaved_buffer);
    if (stream->raw_buffer != NULL)
        k_free(stream->raw_buffer);

    memset(stream, 0, sizeof(*stream));
    if (ctx != NULL) {
        ctx->priv = NULL;
        ctx->running = 0;
    }
}

static int c62_input_start(const uint8_t instance, const void *config,
                           struct streamCtx *ctx)
{
    if ((ctx == NULL) || (instance >= ARRAY_SIZE(s_input_streams)))
        return -EINVAL;
    if ((ctx->running != 0) || (ctx->bufSize == 0))
        return -EBUSY;
    if ((ctx->bufMode == BUF_CIRC_DOUBLE) && ((ctx->bufSize & 1U) != 0))
        return -EINVAL;

    C62InputStream *stream = &s_input_streams[instance];
    if (stream->ctx != NULL)
        return -EBUSY;

    memset(stream, 0, sizeof(*stream));
    stream->ctx = ctx;
    stream->instance = instance;
    stream->block_samples = (ctx->bufMode == BUF_CIRC_DOUBLE) ?
                                ctx->bufSize / 2 :
                                ctx->bufSize;
    stream->decimate_by_two = (ctx->sampleRate == C62_M17_INPUT_RATE);
    stream->physical_rate = stream->decimate_by_two ? C62_ADC_NATIVE_RATE :
                                                      ctx->sampleRate;
    stream->raw_samples = stream->decimate_by_two ? stream->block_samples * 2 :
                                                    stream->block_samples;

    const uint32_t requested_channel = (uint32_t)(uintptr_t)config;
    stream->record_channel_mask = requested_channel;
    stream->record_channels = 1;

    /*
     * The patched DSP keeps its six-channel input producer. AudioRecord's
     * stereo mask selects producer indices 0 (MIC) and 1 (RX), returning only
     * those two channels interleaved. Expose the requested one as mono to the
     * OpenRTX stream interface.
     */
    if ((stream->physical_rate == C62_ADC_NATIVE_RATE)
        && ((requested_channel == C62_AUDIO_CHANNEL_MIC)
            || (requested_channel == C62_AUDIO_CHANNEL_RX))) {
        stream->record_channel_mask = C62_AUDIO_CHANNEL_MIC
                                    | C62_AUDIO_CHANNEL_RX;
        stream->record_channels = 2;
        stream->channel_index = (requested_channel == C62_AUDIO_CHANNEL_RX) ?
                                    1 :
                                    0;

        stream->interleaved_buffer = k_malloc(stream->raw_samples
                                              * stream->record_channels
                                              * sizeof(stream_sample_t));
        if (stream->interleaved_buffer == NULL) {
            c62_input_close(stream);
            return -ENOMEM;
        }
    }

    if (stream->decimate_by_two) {
        stream->raw_buffer =
            k_malloc(stream->raw_samples * sizeof(stream_sample_t));
        if (stream->raw_buffer == NULL) {
            c62_input_close(stream);
            return -ENOMEM;
        }
    }

    int ret = AudioRecord_ctor(&stream->record, 0, stream->physical_rate,
                               PCM_16_BIT, stream->record_channel_mask, 0,
                               NULL);
    if (ret != 0) {
        printk("C62AUDIO input open failed: endpoint=%u logical=%u physical=%u "
               "error=%d\n",
               instance, ctx->sampleRate, stream->physical_rate, ret);
        c62_input_close(stream);
        return ret;
    }
    stream->opened = true;

    size_t af_frame_samples = stream->record.mCblk->frameCount;
    if ((af_frame_samples == 0)
        || ((stream->raw_samples % af_frame_samples) != 0)) {
        printk("C62AUDIO incompatible input block: requested=%u DSP=%u\n",
               (uint32_t)stream->raw_samples, (uint32_t)af_frame_samples);
        c62_input_close(stream);
        return -EINVAL;
    }

    AudioRecord_start(&stream->record);
    ctx->priv = stream;
    ctx->running = 1;
    stream->rate_start_ms = k_uptime_get();
    stream->rate_report_ms = stream->rate_start_ms;

    printk("C62AUDIO input start: endpoint=%u channel=0x%x record=0x%x "
           "channels=%u logical=%u physical=%u block=%u DSPframe=%u "
           "conversion=%s\n",
           instance, requested_channel, stream->record_channel_mask,
           stream->record_channels, ctx->sampleRate, stream->physical_rate,
           (uint32_t)stream->block_samples, (uint32_t)af_frame_samples,
           stream->decimate_by_two ? "FIR/2" : "none");
    return 0;
}

static int c62_input_data(struct streamCtx *ctx, stream_sample_t **buffer)
{
    if ((ctx == NULL) || (buffer == NULL) || (ctx->priv == NULL))
        return -EINVAL;

    C62InputStream *stream = ctx->priv;
    if (stream->ready_buffer == NULL)
        return -EAGAIN;

    *buffer = stream->ready_buffer;
    return (int)stream->block_samples;
}

static int c62_input_sync(struct streamCtx *ctx, uint8_t dirty)
{
    (void)dirty;
    if ((ctx == NULL) || (ctx->priv == NULL) || (ctx->running == 0))
        return -EPIPE;

    C62InputStream *stream = ctx->priv;
    stream_sample_t *destination = ctx->buffer;
    if (ctx->bufMode == BUF_CIRC_DOUBLE)
        destination += stream->next_half * stream->block_samples;

    stream_sample_t *mono_buffer = stream->decimate_by_two ?
                                       stream->raw_buffer :
                                       destination;
    void *read_buffer = (stream->interleaved_buffer != NULL) ?
                            (void *)stream->interleaved_buffer :
                            (void *)mono_buffer;
    size_t read_bytes = stream->raw_samples * stream->record_channels
                      * sizeof(stream_sample_t);
    ssize_t ret = AudioRecord_read(&stream->record, read_buffer, read_bytes);
    if (ret < 0)
        return (int)ret;
    if ((size_t)ret != read_bytes) {
        printk("C62AUDIO short input read: expected=%u actual=%d\n",
               (uint32_t)read_bytes, (int)ret);
        return -EIO;
    }

    if (stream->interleaved_buffer != NULL) {
        for (size_t i = 0; i < stream->raw_samples; i++) {
            mono_buffer[i] =
                stream->interleaved_buffer[i * stream->record_channels
                                           + stream->channel_index];
        }
    }

    if (stream->decimate_by_two)
        decimate_48k_to_24k(stream, stream->raw_buffer, stream->raw_samples,
                            destination);

    stream->ready_buffer = destination;
    if (ctx->bufMode == BUF_CIRC_DOUBLE)
        stream->next_half ^= 1;
    stream->sample_count += stream->block_samples;
    report_input_rate(stream);
    return 0;
}

static void c62_input_stop(struct streamCtx *ctx)
{
    if ((ctx == NULL) || (ctx->priv == NULL))
        return;
    c62_input_close(ctx->priv);
}

static void c62_input_terminate(struct streamCtx *ctx)
{
    c62_input_stop(ctx);
}

static void c62_output_close(C62OutputStream *stream)
{
    struct streamCtx *ctx = stream->ctx;

    if (stream->opened)
        AudioTrack_dtor(&stream->track);

    memset(stream, 0, sizeof(*stream));
    if (ctx != NULL) {
        ctx->priv = NULL;
        ctx->running = 0;
    }
}

static int c62_output_start(const uint8_t instance, const void *config,
                            struct streamCtx *ctx)
{
    if ((ctx == NULL) || (instance >= ARRAY_SIZE(s_output_streams)))
        return -EINVAL;
    if ((ctx->running != 0) || (ctx->bufSize == 0))
        return -EBUSY;
    if ((ctx->bufMode == BUF_CIRC_DOUBLE) && ((ctx->bufSize & 1U) != 0))
        return -EINVAL;

    C62OutputStream *stream = &s_output_streams[instance];
    if (stream->ctx != NULL)
        return -EBUSY;

    memset(stream, 0, sizeof(*stream));
    stream->ctx = ctx;
    stream->instance = instance;
    stream->block_samples = (ctx->bufMode == BUF_CIRC_DOUBLE) ?
                                ctx->bufSize / 2 :
                                ctx->bufSize;

    uint32_t channel = (uint32_t)(uintptr_t)config;
    int ret = AudioTrack_ctor(&stream->track, ctx->sampleRate, PCM_16_BIT,
                              channel, 0, NULL);
    if (ret != 0) {
        printk("C62AUDIO output open failed: endpoint=%u rate=%u error=%d\n",
               instance, ctx->sampleRate, ret);
        c62_output_close(stream);
        return ret;
    }
    stream->opened = true;

    AudioTrack_start(&stream->track);
    ctx->priv = stream;
    ctx->running = 1;
    stream->rate_start_ms = k_uptime_get();
    stream->rate_report_ms = stream->rate_start_ms;

    printk("C62AUDIO output start: endpoint=%u rate=%u block=%u\n", instance,
           ctx->sampleRate, (uint32_t)stream->block_samples);
    return 0;
}

static int c62_output_data(struct streamCtx *ctx, stream_sample_t **buffer)
{
    if ((ctx == NULL) || (buffer == NULL) || (ctx->priv == NULL))
        return -EINVAL;

    C62OutputStream *stream = ctx->priv;
    *buffer = ctx->buffer;
    if (ctx->bufMode == BUF_CIRC_DOUBLE)
        *buffer += stream->next_half * stream->block_samples;
    return (int)stream->block_samples;
}

static int c62_output_sync(struct streamCtx *ctx, uint8_t dirty)
{
    (void)dirty;
    if ((ctx == NULL) || (ctx->priv == NULL) || (ctx->running == 0))
        return -EPIPE;

    C62OutputStream *stream = ctx->priv;
    if (stream->stop_requested) {
        uint32_t drain_ms =
            (uint32_t)((stream->block_samples * 1000U) / ctx->sampleRate);
        k_sleep(K_MSEC(drain_ms + 1));
        c62_output_close(stream);
        return 0;
    }

    stream_sample_t *source = ctx->buffer;
    if (ctx->bufMode == BUF_CIRC_DOUBLE)
        source += stream->next_half * stream->block_samples;

    size_t write_bytes = stream->block_samples * sizeof(stream_sample_t);
    ssize_t ret = AudioTrack_write(&stream->track, source, write_bytes);
    if (ret < 0)
        return (int)ret;
    if ((size_t)ret != write_bytes) {
        printk("C62AUDIO short output write: expected=%u actual=%d\n",
               (uint32_t)write_bytes, (int)ret);
        return -EIO;
    }

    if (ctx->bufMode == BUF_CIRC_DOUBLE)
        stream->next_half ^= 1;
    stream->sample_count += stream->block_samples;
    report_output_rate(stream);
    return 0;
}

static void c62_output_stop(struct streamCtx *ctx)
{
    if ((ctx == NULL) || (ctx->priv == NULL))
        return;
    C62OutputStream *stream = ctx->priv;
    stream->stop_requested = true;
}

static void c62_output_terminate(struct streamCtx *ctx)
{
    if ((ctx == NULL) || (ctx->priv == NULL))
        return;
    c62_output_close(ctx->priv);
}

static const struct audioDriver c62_input_audio_driver = {
    .start = c62_input_start,
    .data = c62_input_data,
    .sync = c62_input_sync,
    .stop = c62_input_stop,
    .terminate = c62_input_terminate,
};

static const struct audioDriver c62_output_audio_driver = {
    .start = c62_output_start,
    .data = c62_output_data,
    .sync = c62_output_sync,
    .stop = c62_output_stop,
    .terminate = c62_output_terminate,
};

// Ring buffer helper functions
static void ring_buffer_init(AudioRingBuffer *rb)
{
    k_mutex_init(&rb->mutex);
    k_sem_init(&rb->data_ready, 0, AUDIO_BUFFER_CHUNKS);
    k_sem_init(&rb->space_available, AUDIO_BUFFER_CHUNKS, AUDIO_BUFFER_CHUNKS);

    rb->write_idx = 0;
    rb->read_idx = 0;

    for (int i = 0; i < AUDIO_BUFFER_CHUNKS; i++) {
        rb->chunks[i].filled = false;
    }
}

static bool ring_buffer_write(AudioRingBuffer *rb, const uint8_t *data,
                              size_t size)
{
    if (size > AUDIO_CHUNK_SIZE) {
        return false;
    }

    // Wait for space to be available (with timeout to prevent deadlock)
    if (k_sem_take(&rb->space_available, K_MSEC(100)) != 0) {
        printk("Ring buffer full, dropping audio chunk\n");
        return false;
    }

    k_mutex_lock(&rb->mutex, K_FOREVER);

    // Copy data to the write buffer
    memcpy(rb->chunks[rb->write_idx].buffer, data, size);
    rb->chunks[rb->write_idx].filled = true;

    // Advance write index
    rb->write_idx = (rb->write_idx + 1) % AUDIO_BUFFER_CHUNKS;

    k_mutex_unlock(&rb->mutex);

    // Signal that data is ready
    k_sem_give(&rb->data_ready);

    return true;
}

static bool ring_buffer_read(AudioRingBuffer *rb, uint8_t *data, size_t *size)
{
    // Wait for data to be available
    if (k_sem_take(&rb->data_ready, K_MSEC(100)) != 0) {
        return false;
    }

    k_mutex_lock(&rb->mutex, K_FOREVER);

    // Copy data from the read buffer
    memcpy(data, rb->chunks[rb->read_idx].buffer, AUDIO_CHUNK_SIZE);
    *size = AUDIO_CHUNK_SIZE;
    rb->chunks[rb->read_idx].filled = false;

    // Advance read index
    rb->read_idx = (rb->read_idx + 1) % AUDIO_BUFFER_CHUNKS;

    k_mutex_unlock(&rb->mutex);

    // Signal that space is available
    k_sem_give(&rb->space_available);

    return true;
}

// Audio input thread - records audio and pushes to ring buffer
static void audio_input_thread(void *arg1, void *arg2, void *arg3)
{
    (void)arg1;
    (void)arg2;
    (void)arg3;

    printk("Audio input thread started\n");

    static uint8_t
        chunk_buffer[AUDIO_CHUNK_SIZE
                     * AUDIO_INPUT_CHANNEL_COUNT /* channel input MIC + RTX */];

    while (s_audio_streaming) {
        k_mutex_lock(&s_audio_mutex, K_FOREVER);

        // Check if we have any active recording sources
        bool has_mic_source = false;
        bool has_rtx_source = false;

        for (int i = 0; i < s_active_path_count; i++) {
            if (!s_active_paths[i].active)
                continue;

            if (s_active_paths[i].source == SOURCE_MIC) {
                has_mic_source = true;
            } else if (s_active_paths[i].source == SOURCE_RTX) {
                has_rtx_source = true;
            }
        }

        k_mutex_unlock(&s_audio_mutex);

        ssize_t read_size =
            AudioRecord_read(&s_record, chunk_buffer,
                             AUDIO_CHUNK_SIZE * AUDIO_INPUT_CHANNEL_COUNT);

        // Record audio from active source
        if (has_mic_source) {
            if (read_size > 0) {
                // Deinterleave MIC and RTX data
                for (size_t i = 0; i < AUDIO_CHUNK_SAMPLES; i++) {
                    chunk_buffer[i * 2] = chunk_buffer
                        [i * 4]; // MIC data (assuming MIC is on the first channel)
                    chunk_buffer[i * 2 + 1] = chunk_buffer[i * 4 + 1];
                }
                read_size = AUDIO_CHUNK_SAMPLES
                          * sizeof(int16_t); // Update read size
                // Push to ring buffer for output thread
                if (!ring_buffer_write(&s_ring_buffer, chunk_buffer,
                                       read_size)) {
                    // Buffer overflow handled in ring_buffer_write
                }
            }
        } else if (has_rtx_source) {
            if (read_size > 0) {
                // Deinterleave MIC and RTX data
                for (size_t i = 0; i < AUDIO_CHUNK_SAMPLES; i++) {
                    chunk_buffer[i * 2] = chunk_buffer
                        [i * 4
                         + 2]; // MIC data (assuming MIC is on the first channel)
                    chunk_buffer[i * 2 + 1] = chunk_buffer[i * 4 + 1 + 2];
                }
                read_size = AUDIO_CHUNK_SAMPLES
                          * sizeof(int16_t); // Update read size
                // Push to ring buffer for output thread
                if (!ring_buffer_write(&s_ring_buffer, chunk_buffer,
                                       read_size)) {
                    // Buffer overflow handled in ring_buffer_write
                }
            }
        } else {
            // Sleep if no active recording
            k_sleep(K_MSEC(10));
        }
    }

    printk("Audio input thread stopped\n");
}

/* Audio output thread - pulls from ring buffer and plays audio */
static void audio_output_thread(void *arg1, void *arg2, void *arg3)
{
    (void)arg1;
    (void)arg2;
    (void)arg3;

    printk("Audio output thread started\n");

    static uint8_t chunk_buffer[AUDIO_CHUNK_SIZE];
    size_t chunk_size;

    while (s_audio_streaming) {
        k_mutex_lock(&s_audio_mutex, K_FOREVER);

        // Check if we have any active playback sinks
        bool has_spk_sink = false;
        bool has_rtx_sink = false;

        for (int i = 0; i < s_active_path_count; i++) {
            if (!s_active_paths[i].active)
                continue;

            if (s_active_paths[i].sink == SINK_SPK) {
                has_spk_sink = true;
            } else if (s_active_paths[i].sink == SINK_RTX) {
                has_rtx_sink = true;
            }
        }

        k_mutex_unlock(&s_audio_mutex);

        // Play audio to SPK if needed
        if (has_spk_sink) {
            // Pull from ring buffer
            if (ring_buffer_read(&s_ring_buffer, chunk_buffer, &chunk_size)) {
                // Write to speaker track
                ssize_t written = AudioTrack_write(&s_track_spk, chunk_buffer,
                                                   chunk_size);

                if (written < 0) {
                    printk("AudioTrack_write failed: %d\n", (int)written);
                }
            }
        } else if (has_rtx_sink) {
            // Pull from ring buffer
            if (ring_buffer_read(&s_ring_buffer, chunk_buffer, &chunk_size)) {
                // Write to RTX output track
                ssize_t written = AudioTrack_write(&s_track_rtx_output,
                                                   chunk_buffer, chunk_size);

                if (written < 0) {
                    printk("AudioTrack_write failed: %d\n", (int)written);
                }
            }
        } else {
            // Sleep if no active playback
            k_sleep(K_MSEC(10));
        }
    }

    printk("Audio output thread stopped\n");
}

static int bridge_init(void)
{
    if (s_bridge_initialized)
        return 0;

    int ret = AudioRecord_ctor(&s_record, 0, BRIDGE_SAMPLE_RATE, PCM_16_BIT,
                               C62_AUDIO_CHANNEL_MIC | C62_AUDIO_CHANNEL_RX, 0,
                               NULL);
    if (ret != 0) {
        printk("Failed to initialize bridge AudioRecord: %d\n", ret);
        return ret;
    }

    ret = AudioTrack_ctor(&s_track_spk, BRIDGE_SAMPLE_RATE, PCM_16_BIT,
                          C62_AUDIO_CHANNEL_SPK, 0, NULL);
    if (ret != 0) {
        printk("Failed to initialize bridge speaker AudioTrack: %d\n", ret);
        AudioRecord_dtor(&s_record);
        return ret;
    }

    ret = AudioTrack_ctor(&s_track_rtx_output, BRIDGE_SAMPLE_RATE, PCM_16_BIT,
                          C62_AUDIO_CHANNEL_TX, 0, NULL);
    if (ret != 0) {
        printk("Failed to initialize bridge RTX AudioTrack: %d\n", ret);
        AudioRecord_dtor(&s_record);
        AudioTrack_dtor(&s_track_spk);
        return ret;
    }

    s_bridge_initialized = true;
    return 0;
}

static void bridge_terminate(void)
{
    if (!s_bridge_initialized)
        return;

    AudioRecord_dtor(&s_record);
    AudioTrack_dtor(&s_track_spk);
    AudioTrack_dtor(&s_track_rtx_output);
    s_bridge_initialized = false;
}

void audio_init()
{
    int ret;

    if (s_audio_initialized) {
        printk("Audio already initialized\n");
        return;
    }

    printk("Initializing audio subsystem\n");

    // Initialize mutex
    k_mutex_init(&s_audio_mutex);

    // Initialize ring buffer
    ring_buffer_init(&s_ring_buffer);

    // Initialize active paths array
    for (int i = 0; i < 3; i++) {
        s_active_paths[i].active = false;
    }
    s_active_path_count = 0;

    // Set audio system parameters
    String8 param;
    String8_ctor_char(&param, "ADC_PDM_GAIN_A_LEFT=6;"
                              "ADC_PDM_GAIN_D_LEFT=20;"
                              "ADC_PDM_GAIN_A_RIGHT=6;"
                              "ADC_PDM_GAIN_D_RIGHT=20");
    ret = AudioSystem_setParameters(0, &param);
    String8_dtor(&param);
    if (ret != 0) {
        printk("Failed to set audio parameters: %d\n", ret);
        return;
    }

    s_audio_initialized = true;
    printk("Audio subsystem initialized successfully\n");
}

void audio_terminate()
{
    if (!s_audio_initialized) {
        return;
    }

    printk("Terminating audio subsystem\n");

    // Stop streaming if active
    if (s_audio_streaming) {
        s_audio_streaming = false;

        // Wait for both threads to finish
        k_thread_join(&s_audio_input_thread, K_SECONDS(1));
        k_thread_join(&s_audio_output_thread, K_SECONDS(1));

        // Stop all active devices
        if (s_mic_active || s_rtx_input_active) {
            AudioRecord_stop(&s_record);
            s_mic_active = false;
            s_rtx_input_active = false;
        }
        if (s_spk_active) {
            AudioTrack_stop(&s_track_spk);
            s_spk_active = false;
        }
        if (s_rtx_output_active) {
            AudioTrack_stop(&s_track_rtx_output);
            s_rtx_output_active = false;
        }
    }

    bridge_terminate();

    for (size_t i = 0; i < ARRAY_SIZE(s_input_streams); i++) {
        if (s_input_streams[i].ctx != NULL)
            c62_input_close(&s_input_streams[i]);
    }
    for (size_t i = 0; i < ARRAY_SIZE(s_output_streams); i++) {
        if (s_output_streams[i].ctx != NULL)
            c62_output_close(&s_output_streams[i]);
    }

    // Clear all paths
    k_mutex_lock(&s_audio_mutex, K_FOREVER);
    for (int i = 0; i < 3; i++) {
        s_active_paths[i].active = false;
    }
    s_active_path_count = 0;
    k_mutex_unlock(&s_audio_mutex);

    s_audio_initialized = false;
    printk("Audio subsystem terminated\n");
}

void audio_connect(const enum AudioSource source, const enum AudioSink sink)
{
    if (!s_audio_initialized) {
        printk("Audio not initialized, initializing now\n");
        audio_init();
    }

    if (source == SOURCE_RTX)
        radio_enableAfOutput();

    if (sink == SINK_SPK)
        gpio_pin_set_dt(&speaker_enable, 1);

    /*
     * Paths touching the MCU are moved by the audioDriver callbacks above.
     * Only direct hardware-to-hardware paths need the legacy 16 kHz bridge.
     */
    if ((source == SOURCE_MCU) || (sink == SINK_MCU)) {
        printk("Audio stream path connected: %d->%d\n", source, sink);
        return;
    }

    if (bridge_init() != 0) {
        printk("Unable to open direct audio bridge: %d->%d\n", source, sink);
        return;
    }

    k_mutex_lock(&s_audio_mutex, K_FOREVER);

    // Check if path already exists
    for (int i = 0; i < s_active_path_count; i++) {
        if (s_active_paths[i].source == source && s_active_paths[i].sink == sink
            && s_active_paths[i].active) {
            printk("Audio path already connected: %d->%d\n", source, sink);
            k_mutex_unlock(&s_audio_mutex);
            return;
        }
    }

    // Add new path
    if (s_active_path_count < 3) {
        s_active_paths[s_active_path_count].source = source;
        s_active_paths[s_active_path_count].sink = sink;
        s_active_paths[s_active_path_count].active = true;
        s_active_path_count++;

        printk("Audio path will connect: %d->%d (total: %d)\n", source, sink,
               s_active_path_count);

        AudioRecord_start(&s_record);
        // Start the appropriate source device based on the path
        if (source == SOURCE_MIC && !s_mic_active) {
            s_mic_active = true;
            printk("Started input\n");
        } else if (source == SOURCE_RTX && !s_rtx_input_active) {
            s_rtx_input_active = true;
            printk("Started RTX input\n");
        }

        // Start the appropriate sink device based on the path
        if (sink == SINK_SPK && !s_spk_active) {
            AudioTrack_start(&s_track_spk);
            s_spk_active = true;
            printk("Started SPK output\n");
        } else if (sink == SINK_RTX && !s_rtx_output_active) {
            AudioTrack_start(&s_track_rtx_output);
            s_rtx_output_active = true;
            printk("Started RTX output\n");
        }

        // Start streaming thread if not already running
        if (!s_audio_streaming) {
            s_audio_streaming = true;

            // Create input thread (recording)
            k_thread_create(&s_audio_input_thread, s_audio_input_stack,
                            AUDIO_THREAD_STACK_SIZE, audio_input_thread, NULL,
                            NULL, NULL, AUDIO_THREAD_PRIORITY, 0, K_NO_WAIT);
            k_thread_name_set(&s_audio_input_thread, "audio_input");

            // Create output thread (playback)
            k_thread_create(&s_audio_output_thread, s_audio_output_stack,
                            AUDIO_THREAD_STACK_SIZE, audio_output_thread, NULL,
                            NULL, NULL, AUDIO_THREAD_PRIORITY, 0, K_NO_WAIT);
            k_thread_name_set(&s_audio_output_thread, "audio_output");

            printk("Audio streaming threads started\n");
        }
    } else {
        printk("Maximum audio paths reached\n");
    }

    k_mutex_unlock(&s_audio_mutex);
}

void audio_disconnect(const enum AudioSource source, const enum AudioSink sink)
{
    if (!s_audio_initialized) {
        return;
    }

    if (sink == SINK_SPK)
        gpio_pin_set_dt(&speaker_enable, 0);

    if (source == SOURCE_RTX)
        radio_disableAfOutput();

    if ((source == SOURCE_MCU) || (sink == SINK_MCU)) {
        printk("Audio stream path disconnected: %d->%d\n", source, sink);
        return;
    }

    k_mutex_lock(&s_audio_mutex, K_FOREVER);

    // Find and remove the path
    bool found = false;
    for (int i = 0; i < s_active_path_count; i++) {
        if (s_active_paths[i].source == source && s_active_paths[i].sink == sink
            && s_active_paths[i].active) {
            s_active_paths[i].active = false;
            found = true;

            // Compact array by moving last element to this position
            if (i < s_active_path_count - 1) {
                s_active_paths[i] = s_active_paths[s_active_path_count - 1];
            }
            s_active_path_count--;

            printk("Audio path disconnected: %d->%d (remaining: %d)\n", source,
                   sink, s_active_path_count);
            break;
        }
    }

    if (!found) {
        printk("Audio path not found: %d->%d\n", source, sink);
    }

    // Stop streaming if no active paths
    if (s_active_path_count == 0 && s_audio_streaming) {
        s_audio_streaming = false;
        k_mutex_unlock(&s_audio_mutex);

        // Wait for both threads to finish
        k_thread_join(&s_audio_input_thread, K_SECONDS(1));
        k_thread_join(&s_audio_output_thread, K_SECONDS(1));

        // Stop all active devices
        if (s_mic_active || s_rtx_input_active) {
            AudioRecord_stop(&s_record);
            s_mic_active = false;
            s_rtx_input_active = false;
            printk("Stopped input\n");
        }
        if (s_spk_active) {
            AudioTrack_stop(&s_track_spk);
            s_spk_active = false;
            printk("Stopped SPK output\n");
        }
        if (s_rtx_output_active) {
            AudioTrack_stop(&s_track_rtx_output);
            s_rtx_output_active = false;
            printk("Stopped RTX output\n");
        }

        printk("Audio streaming stopped\n");
    } else {
        // Check if we need to stop any devices that are no longer used
        bool mic_needed = false;
        bool rtx_input_needed = false;
        bool spk_needed = false;
        bool rtx_output_needed = false;

        // Check if any remaining paths use each device
        for (int i = 0; i < s_active_path_count; i++) {
            if (s_active_paths[i].active) {
                if (s_active_paths[i].source == SOURCE_MIC)
                    mic_needed = true;
                if (s_active_paths[i].source == SOURCE_RTX)
                    rtx_input_needed = true;
                if (s_active_paths[i].sink == SINK_SPK)
                    spk_needed = true;
                if (s_active_paths[i].sink == SINK_RTX)
                    rtx_output_needed = true;
            }
        }

        // Stop devices that are no longer needed
        if (!mic_needed && s_mic_active && !rtx_input_needed
            && s_rtx_input_active) {
            AudioRecord_stop(&s_record);
            s_mic_active = false;
            s_rtx_input_active = false;
            printk("Stopped input\n");
        }
        if (!spk_needed && s_spk_active) {
            AudioTrack_stop(&s_track_spk);
            s_spk_active = false;
            printk("Stopped SPK output\n");
        }
        if (!rtx_output_needed && s_rtx_output_active) {
            AudioTrack_stop(&s_track_rtx_output);
            s_rtx_output_active = false;
            printk("Stopped RTX output\n");
        }

        k_mutex_unlock(&s_audio_mutex);
    }
}

bool audio_checkPathCompatibility(const enum AudioSource p1Source,
                                  const enum AudioSink p1Sink,
                                  const enum AudioSource p2Source,
                                  const enum AudioSink p2Sink)

{
    // If both paths use the same source, they're incompatible
    if (p1Source == p2Source && p1Source != SOURCE_MCU) {
        return false;
    }

    // If both paths use the same sink, they're incompatible
    if (p1Sink == p2Sink && p1Sink != SINK_MCU) {
        return false;
    }

    // MCU source/sink can be used multiple times (software buffers)
    // Hardware sources/sinks (MIC, SPK, RTX) can only be used once

    return true;
}

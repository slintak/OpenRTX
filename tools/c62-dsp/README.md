<!--
SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Retevis C62 native 48 kHz DSP audio

The C62's CSK6011B uses a Cadence HiFi4 DSP for audio I/O. ListenAI's closed
DSP firmware accepts only 16 kHz input even though its PDM driver contains a
48 kHz configuration. This directory reproducibly creates a patched copy for
the C62 port in [OpenRTX PR #415][pr415] that allows native 48 kHz input.

Neither the original nor the patched binary is distributed by OpenRTX. The
helper downloads the original from [Tunas1337's C62 LSF SDK][lsf-sdk] and
refuses to touch any input whose SHA-256 does not match the version analyzed
and tested on hardware.

## Build the image

Requirements are Python 3, `make`, and `curl`:

```sh
make -C tools/c62-dsp
```

To use an already downloaded blob instead:

```sh
make -C tools/c62-dsp \
    STOCK=/path/to/dsp_firmware.bin \
    OUTPUT=/path/to/dsp_firmware_48khz.bin
```

Known hashes:

| Image | SHA-256 |
| --- | --- |
| Stock | `7dd697c9c9b11d2d9164400d067efa81a3b7ffb5af176133550621c2e8fcbdb8` |
| Patched | `f2f9f798af3c3db89e971a28e53bd1c1afc2135ded61e15a301856ae48ce6c8f` |

The script checks the hash and original bytes at every patch location before
writing its output. An upstream firmware update therefore fails safely instead
of producing an unverified image.

## What is changed

The layout-preserving patch changes 17 bytes in ten HiFi4 instruction
bundles. It:

1. removes the `AudioFlinger_openRecord` 16 kHz-only policy check;
2. propagates the requested input rate through record metadata;
3. selects the existing 48 kHz hardware getter and 48 kHz/OSR-250 PDM mode;
4. changes all three linked cross-core input frame sizes from 160 to 480
   samples; and
5. retains all six DSP input channels and fits one complete 480-sample frame
   in a 5760-byte FIFO (`1 * 480 * 6 channels * 2 bytes`).

The PDM selector and transport changes were isolated with tagged firmware
probes and exercised on a physical C62. The patched image opens 48 kHz record
streams and returns complete 480-sample frames. An electrical loopback test
also verified that source channel 0 carries audio from the external microphone
input. The FIFO has only one frame of buffering, so clients must consume input
promptly. This work does **not** claim working M17 reception or decoding; those
experiments are intentionally outside this patch set.

The six-channel producer geometry is intentional. Reducing it to the two
channels currently consumed by OpenRTX produced correctly timed, but
zero-valued, capture frames in hardware tests. On the MCU side, request the
stereo `CHANNEL_IN_LEFT | CHANNEL_IN_RIGHT` mask: `AudioRecord` then selects
the MIC and RTX/RX source indices and returns only those two interleaved
channels. The accompanying C62 driver exposes the selected channel as mono to
the rest of OpenRTX.

## Flashing and recovery

Flashing the wrong partition can make the radio unbootable. Back up the full
4 MB flash first and keep the stock DSP image available for recovery.

The DSP partition starts at `0x100000`. With `cskburn` and the programming
cable on `/dev/ttyUSB0`, a verified write is:

```sh
cskburn -s /dev/ttyUSB0 -C 6 -b 115200 -v --verify-all \
    0x100000 tools/c62-dsp/dsp_firmware_48khz.bin
```

To enter burn mode, turn the radio off, hold the lower side key (secondary
PTT), turn it on, then release the key. The display remains dark. Do not press
the main PTT because it shares the boot-ROM UART RX line.

Restore the original by running the same command with the verified stock
`dsp_firmware.bin`. The OpenRTX application lives in the separate partition at
`0x000000`.

## Reverse-engineering notes

The blob is mapped at `0x60000000` and uses the `venus_hifi4` Xtensa ISA,
including FLIX instruction bundles. Base-Xtensa disassemblers cannot reliably
decode it. The published patch intentionally contains only byte replacements,
their expected context, and the findings needed to review them; it does not
require proprietary Cadence tools or redistribute ListenAI firmware.

[pr415]: https://github.com/OpenRTX/OpenRTX/pull/415
[lsf-sdk]: https://github.com/Tunas1337/lsf-zephyr-sdk-c62

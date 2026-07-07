# OpenRTX Serial Control and Data Protocol (draft 2)

Working title: RTXLink v2. First discussion draft, follows the direction agreed
at the #remote-rig Discord channel:

1. text-based CAT command set following the Kenwood/Yaesu de facto standard,
2. a dedicated CAT channel and a dedicated data channel, exposed as two USB
   CDC-ACM interfaces where USB is available,
3. length-indicated framing (WebSocket style) for the data channel, replacing
   SLIP.

This draft adds one requirement: **radios
whose only interface is a plain UART (e.g. the Kenwood-style speaker/mic
connector on handhelds) MUST still provide both channels.** File transfer and
codeplug access cannot be a USB-only feature. Section 4 defines modal
switching for that case, following the observation that a data transfer
always begins with an explicit switch into file transfer mode anyway, so the
line protocol can switch together with the command set. Kenwood
compatibility is not affected.

> EDITOR: notes like this one mark open questions and will not be part of the
> final document.

The key words MUST, SHOULD, MAY etc. are to be interpreted as described in
RFC 2119. The **radio** is the device running OpenRTX; the **controller** is
the computer talking to it. All multi-byte integers are little-endian unless
stated otherwise; the frame CRC is the big-endian exception (section 5.2).

## 1. Architecture

Two independent logical channels:

| Channel | Content                            | Direction  |
|:--------|:-----------------------------------|:-----------|
| CAT     | ASCII commands, Kenwood dialect    | both       |
| DATA    | binary frames (file transfer, ...) | both       |

Transport mappings:

| Transport            | CAT                  | DATA                     |
|:---------------------|:---------------------|:-------------------------|
| USB (composite CDC)  | CDC interface 0      | CDC interface 1          |
| single UART          | default mode         | modal switch to frame mode (section 4) |

On USB, the CAT interface carries pure Kenwood-style text: any existing
program can open the port and talk to the radio directly. The DATA interface
carries only frames (section 5).

## 2. CAT channel

### 2.1. Dialect rules

- Commands are ASCII, terminated by `;` (0x3B). No checksum, no length field.
- Command names are two letters; parameters are fixed-width ASCII fields as
  defined per command.
- The radio answers a read command with the same command name followed by the
  value, terminated by `;`. Set commands are not answered unless AI mode
  requires it (section 2.4).
- Error handling, Kenwood-compatible: `?;` unknown or unsupported command or
  malformed syntax, `E;` communication error, `O;` processing not completed.
- In the CAT stream the radio MUST ignore CR (0x0D), LF (0x0A) and NUL bytes
  between commands. Bytes 0x00 to 0x1F MUST NOT appear inside commands
  (matches the Yaesu rule; keeps the CAT plane free of binary bytes).
- The dialect is deliberately human-typable: a user with a plain serial
  terminal can drive the radio by hand, for debugging or on-the-fly
  operation. Tolerating CR/LF exists for this reason.

### 2.2. Standard command subset

Initial subset, chosen to cover what existing programs (Hamlib, loggers,
WSJT-X) actually use. Field widths follow the Kenwood TS-590 reference.

| Cmd  | Function                  | Notes |
|:-----|:--------------------------|:------|
| `ID` | radio identification      | see editor note below |
| `FA` | VFO A frequency, 11 digit Hz | maps to the OpenRTX VFO |
| `IF` | composite status          | 38-byte TS-480 layout; unsupported fields carry the reference's neutral value (zeros, or spaces where the TS-480 sends spaces) |
| `MD` | operating mode            | OpenRTX value table below |
| `TX` / `RX` | PTT on / off       | `TX;`, `TX0;` and `TX1;` all key the radio |
| `PC` | output power, watts       | integer watts, see editor note |
| `SM` | signal meter              | `SM0xxxx`, scaled from RSSI |
| `SQ` | squelch level             | `SQ0xxx`, scaled to the 0-15 OpenRTX range |
| `AG` | AF gain (volume)          | `AG0xxx` |
| `MC` | memory channel select     | 3 digits, switches to memory mode |
| `FR` / `FT` | RX/TX tuner mode   | `FR0` VFO, `FR2` memory, see below |
| `TO` | TX subaudible tone on/off | `TO0;` / `TO1;` |
| `TN` | TX tone frequency         | `TNnn;`, tone number, section 2.5 |
| `CT` | tone squelch (RX CTCSS) on/off | `CT0;` / `CT1;` |
| `CN` | RX CTCSS frequency        | `CNnn;`, tone number, section 2.5 |
| `OS` | repeater offset direction | `OSn;`, 0 simplex, 1 plus, 2 minus |
| `OF` | repeater offset frequency | `OFnnnnnnnnn;`, 9 digits, Hz |
| `PS` | power status / power off  | `PS0;` performs a clean shutdown |
| `AI` | auto information mode     | section 2.4 |

Everything else from the Kenwood set is answered with `?;` (the standard
"unsupported" reply, which existing software already handles).

`FR`/`FT` keep the Kenwood digit assignment (0 VFO A, 1 VFO B, 2 memory).
OpenRTX has no second VFO, so the radio reports 0 or 2 and answers `FR1;`
with `?;`; reusing digit 1 for the memory mode would make every existing
parser misread the radio. There is no split operation, `FT` mirrors `FR`.

The tone commands follow the TS-590 syntax (`TO`/`TN`/`CT`/`CN`) and the
repeater shift follows the TS-2000 (`OS`/`OF`), since HF transceivers have
no repeater shift. `TO`/`TN` control the transmitted subaudible tone,
`CT`/`CN` the receive tone squelch; they map to the OpenRTX txTone/rxTone
channel fields. `OS`/`OF` express the TX frequency as the RX frequency
plus or minus the offset. `CT2;` (cross tone) and `OS3;` (the E-type "="
shift) are not supported and answered with `?;`.

`MD` uses an OpenRTX-specific value table:

| Value | Mode |
|:------|:-----|
| `MD4` | FM (matches the Kenwood HF value)      |
| `MD6` | NFM (matches the Kenwood TH-D74 value) |
| `MD8` | DMR |
| `MD9` | M17 |

Other digits are answered with `?;`. Defining `MD` digits per model is
established practice: Kenwood's own TH-D74 handheld uses a completely
different table (0 FM, 1 DV, 6 NFM) than their HF transceivers, and host
software maps the digits per model anyway. Since OpenRTX identifies itself
with its own `ID` code, there is no pretense of being an existing model, so
there is no reason to avoid digits for the digital modes.

> EDITOR, `ID`: we either return our own unused code (proposal: `ID990;`) and
> contribute an OpenRTX backend to Hamlib, or emulate a real Kenwood model
> (TS-480: `ID020;`) so that unmodified software auto-detects us but then
> expects features we do not have. Recommendation: own ID plus Hamlib backend;
> optionally a build flag for TS-480 emulation.

> EDITOR, `PC`: Kenwood expresses power in integer watts, which cannot express
> 0.5 W handheld settings. Proposal: `PC` rounds to nearest watt, exact value
> readable and settable through the `ZP` extension (milliwatts).

### 2.3. OpenRTX extension commands

Extensions use the same syntax and live in the `Z*` command space, which is
unused by the Kenwood and modern Yaesu sets. Kept deliberately minimal:

| Cmd  | Function                          | Format |
|:-----|:----------------------------------|:-------|
| `ZC` | M17 source callsign               | `ZCxxxxxxxxx;` 9 chars, space-padded |
| `ZD` | M17 destination                   | `ZDxxxxxxxxx;` 9 chars, space-padded |
| `ZN` | M17 channel access number         | `ZNnn;` 00 to 15 |
| `ZP` | TX power in milliwatts            | `ZPnnnnnn;` 6 digits |
| `ZR` | battery charge                    | `ZRnnn;` percent, read-only |
| `ZV` | firmware version string           | read-only |
| `ZT` | data mode switch                  | `ZTn;` 0 normal, 1 file transfer, 2 KISS TNC |

`ZC`/`ZD` accept the M17 callsign alphabet (A-Z, 0-9, `-`, `/`, `.`);
lowercase input is accepted and stored uppercase, anything else is
answered `?;`. An empty `ZD` (all spaces) selects the M17 broadcast
destination. `ZV` answers printable ASCII and never contains `;`.

`ZT` gates the data services: the FMP bulk and filesystem-mutating
commands are only allowed in mode 1 (section 3.1), the TNC only operates
in mode 2. On a single-UART radio it also switches the line protocol
(section 4). The radio confirms the switch by sending the read-format
answer (e.g. `ZT1;`) before changing mode — the one set command that is
answered, since the confirmation delimits the mode switch. Leaving
file transfer mode requires a reboot (FMP Reboot command or power cycle);
leaving TNC mode uses the standard KISS return command (0xFF).

### 2.4. Auto information (unsolicited notifications)

`AI0;` disables, `AI2;` enables auto information mode; `AI;` reads the
setting. Default is off, the setting is per-session and resets on power
cycle.

With AI enabled, the radio sends the read-format answer of a parameter
whenever that parameter changes, regardless of the cause of the change:
`FA...;` on frequency change (VFO knob included), `MD...;` on mode change,
`TX;`/`RX;` on PTT state change, `MC...;` on channel change, `SM...;`
rate-limited. This covers every readable parameter of sections 2.2 and
2.3, the `Z*` extensions included (e.g. `ZR...;` on battery change); the
list above gives examples, not the full set.

Notifications are best effort: the radio MUST rate-limit them (RECOMMENDED
at most 10 per second per parameter) and MAY drop them under load; every
notified value remains readable by polling. Controllers MUST tolerate
notifications at any position in the stream, including between a command and
its answer.

> EDITOR: implementation is planned as a later phase, after the firmware
> gains an internal event exchange system. This section reserves the protocol
> surface now so controllers can rely on it; until implemented, the radio
> answers `AI` like any other unsupported command (`?;`), which existing
> software handles gracefully.

### 2.5. Tone numbers

`TN` and `CN` take the Kenwood two-digit tone number, TS-590 table:

| No | Hz   | No | Hz    | No | Hz    | No | Hz    |
|:---|:-----|:---|:------|:---|:------|:---|:------|
| 00 | 67.0 | 11 | 97.4  | 22 | 141.3 | 33 | 206.5 |
| 01 | 69.3 | 12 | 100.0 | 23 | 146.2 | 34 | 210.7 |
| 02 | 71.9 | 13 | 103.5 | 24 | 151.4 | 35 | 218.1 |
| 03 | 74.4 | 14 | 107.2 | 25 | 156.7 | 36 | 225.7 |
| 04 | 77.0 | 15 | 110.9 | 26 | 162.2 | 37 | 229.1 |
| 05 | 79.7 | 16 | 114.8 | 27 | 167.9 | 38 | 233.6 |
| 06 | 82.5 | 17 | 118.8 | 28 | 173.8 | 39 | 241.8 |
| 07 | 85.4 | 18 | 123.0 | 29 | 179.9 | 40 | 250.3 |
| 08 | 88.5 | 19 | 127.3 | 30 | 186.2 | 41 | 254.1 |
| 09 | 91.5 | 20 | 131.8 | 31 | 192.8 | —  | —     |
| 10 | 94.8 | 21 | 136.5 | 32 | 203.5 | —  | —     |

The TS-590 additionally defines number 42 as the 1750 Hz repeater tone
burst; OpenRTX does not implement the burst, so `TN42;` is answered `?;`.

OpenRTX supports the full 50-tone CTCSS list (`ctcss_tone[]` in
`openrtx/src/core/cps.c`); the eight tones absent from the Kenwood table
(159.8, 165.5, 171.3, 177.3, 183.5, 189.9, 196.6 and 199.5 Hz) cannot be
selected through `TN`/`CN` (open question 7). A tone set from the radio UI
to one of those eight is reported as the nearest table entry.

## 3. DATA channel

The DATA channel carries binary frames. Each frame belongs to a port,
identifying the service:

| Port | Service                        |
|:-----|:-------------------------------|
| 0x00 | stdio redirection (section 3.3) |
| 0x01 | reserved — the CAT protocol ID of the historical rtxlink; CAT never travels in frames |
| 0x02 | FMP, file management protocol  |
| 0x03 | DAT, bulk data blocks          |
| 0x04 | KISS TNC (reserved, M17 packet mode) |
| 0x05 to 0x7F | reserved for future standard services |
| 0x80 to 0xFF | experimental use |

A frame addressed to an unknown port, or to a service that is not active
in the current mode, is discarded silently. FMP, having a request-response
structure, instead answers a request it cannot serve with an error status
(section 3.1).

### 3.1. FMP, file management protocol (port 0x02)

FMP reads and writes files on the radio filesystem and dumps or restores the
content of its nonvolatile memories. Bulk data never travels inside FMP
frames: FMP negotiates the transfer, the data itself moves over DAT
(section 3.2).

Request payload:

`[CMD][NParams][len 1]...[len N][param 1]...[param N]`

One length byte per parameter, then the parameters. A command without
parameters is the two bytes `[CMD][0x00]`.

Response payload:

`[CMD][status][NArgs][len 1]...[len N][arg 1]...[arg N]`

The command byte echoes the request. Status 0x00 means success; non-zero
status is a POSIX errno value, or 0xFF meaning an unspecified error.

| Command  | OpCode | Parameters           | Response args    | Description |
|:---------|:-------|:---------------------|:-----------------|:------------|
| Meminfo  | 0x01   | none                 | array of meminfo | Describe the nonvolatile memories |
| Dump     | 0x02   | MemIndex             | Size (u32)       | Read a whole memory, data over DAT |
| Flash    | 0x03   | MemIndex, Size (u32) | none             | Write a whole memory, data over DAT |
| Read     | 0x04   | Path                 | Size (u32)       | Read a file, data over DAT |
| Write    | 0x05   | Path, Size (u32)     | none             | Write a file, data over DAT |
| List     | 0x06   | Path, Index (u16, optional) | Total (u16), array of strings | List directory entries |
| Move     | 0x07   | SrcPath, DstPath     | none             | Rename or move a file |
| Copy     | 0x08   | SrcPath, DstPath     | none             | Copy a file |
| MkDir    | 0x09   | Path                 | none             | Create a directory |
| Remove   | 0x0A   | Path                 | none             | Delete a file or directory |
| Reboot   | 0x11   | none                 | none             | Reboot the radio, leaving file transfer mode |
| Reset    | 0xFF   | none                 | none             | Abort the pending operation, reset FMP |

Sizes are u32, little-endian. Paths are ASCII, at most 128 bytes. On a radio
without a filesystem only Meminfo, Dump, Flash, Reboot and Reset are
available; the other commands return ENOTSUP.

A directory listing can exceed the maximum frame payload, so List is
paginated: Index is the position of the first entry to return (default 0),
the first response argument is the total number of entries as u16, the
remaining arguments are consecutive entries starting at Index, as many as
fit one frame. Entries are returned sorted by name; the controller repeats
the command with a growing Index until it has collected Total entries.

Meminfo, List, Reboot and Reset are available whenever the DATA channel is
reachable. Every other command requires file transfer mode, entered with
the CAT command `ZT1;` (section 2.3), and is answered with status EPERM
outside of it: bulk transfers and filesystem modifications only happen
while the radio has stopped normal RTX operation, which guarantees data
consistency. The only way back to normal operation is a reboot (FMP Reboot
or a physical power cycle), which ensures all changes are reloaded
consistently — this holds on USB too, where the CAT interface stays
reachable during file transfer mode. Reboot is an FMP command rather than
a CAT one because on a single-UART radio the CAT plane is not reachable
while in file transfer mode (section 4).

Meminfo returns one 32-byte descriptor per memory; MemIndex is the position
of the descriptor in the response, starting from 0:

```c
struct meminfo
{
    uint32_t size;     // Memory size in bytes, little-endian
    uint8_t  flags;    // Memory type and access flags
    char     name[27]; // Name, NUL-padded
} __attribute__((packed));
```

> EDITOR: the flags bit assignments must be extracted from the firmware nvmem
> headers and written down here.

### 3.2. DAT, bulk data transfer (port 0x03)

DAT moves the bulk data negotiated by an FMP Dump/Flash/Read/Write command.
The direction and the total byte count are fixed by that FMP exchange; DAT
itself carries no metadata. Only one transfer can be active at a time.

Block payload:

|  0  |  1   | 2 ... N+1 |
|:---:|:----:|:---------:|
| seq | ~seq | data      |

`seq` is a block sequence number starting at 0 and wrapping modulo 256;
`~seq` is its bitwise complement. The data field carries at most 1024 bytes;
all blocks of a transfer except the last SHOULD carry the same amount of
data, and SHOULD use power-of-two sizes so that flash page boundaries are
preserved (1024 recommended).

Acknowledge frames are single-byte DAT payloads: ACK 0x06, NAK 0x15.

A frame corrupted on the wire is discarded by the framing layer
(section 5.1) — the DAT layer never sees it. Loss therefore shows up only
as silence, and the side that is waiting breaks the silence after a
timeout (RECOMMENDED 500 ms) as described below; every such retransmission
counts towards the retry limit.

**Radio to controller** (Dump, Read): the controller starts the transfer
by sending ACK. The radio answers ACK with the next block and anything
else (NAK) by resending the current block; a controller that does not
receive a block within the timeout sends NAK. The radio keeps the transfer
armed until it receives the ACK following the final block, and only then
returns to idle. If that final ACK is lost the radio times out instead,
which is harmless: the controller already holds all the data.

**Controller to radio** (Flash, Write): the controller sends blocks; the
radio verifies the sequence bytes, writes the data and answers each block
with ACK, or with NAK when the payload is malformed or the write fails. A
block whose seq equals the *previous*, already acknowledged sequence
number is a retransmission whose ACK was lost: the radio MUST answer it
with ACK again and discard the data. Any other sequence mismatch is
answered with NAK. On NAK, and on timeout waiting for an ACK, the
controller resends the current block. The block that completes the byte
count commits the data; the radio MUST keep re-acknowledging
retransmissions of that final block until the next FMP command or until
the transfer times out, so that a lost final ACK cannot make the
controller believe a completed transfer failed.

Integrity is provided by the frame CRC; DAT adds no separate checksum.
Each side considers the transfer failed after 10 retransmissions of the
same block or after 2 seconds without any valid frame; a failed transfer
is aborted with FMP Reset and the radio remains in file transfer mode.

### 3.3. stdio redirection (port 0x00)

Port 0x00 carries the radio's standard I/O as raw text: frames from the
radio carry console and log output, frames from the controller carry
console input. The service is optional and build-dependent; it gives no
delivery guarantee beyond the frame itself, and the radio MAY drop output
frames under load. A radio without a console discards incoming stdio
frames.

## 4. Single-UART operation, modal switching

On a single UART the line is in exactly one mode at a time. A data transfer
always begins with an explicit switch into file transfer mode, so the line
protocol switches together with the command set; the same holds for the TNC.
This is also how classic packet TNCs behave (command mode vs KISS mode).

| Mode           | Line carries       | Entered by | Left by |
|:---------------|:-------------------|:-----------|:--------|
| CAT (default)  | Kenwood-style text | power-on   | `ZT1;` or `ZT2;` |
| File transfer  | frames (section 5) | `ZT1;`     | reboot (FMP Reboot, power cycle) |
| KISS TNC       | frames (section 5) | `ZT2;`     | KISS return command (0xFF) |

- In CAT mode the line carries only printable ASCII (section 2.1). A
  controller speaking plain Kenwood CAT works unmodified and never sees a
  binary byte.
- The radio confirms a mode switch by sending the read-format answer
  (`ZT1;`/`ZT2;`); every byte after the answer's `;` belongs to the new
  mode.
- In a data mode the line carries only frames (section 5); the CAT plane is
  unavailable until the mode is left.
- A mode switch takes effect between two bytes of one stream: an
  implementation MUST hand the unconsumed remainder of its receive buffer
  to the parser of the new mode and clear the old mode's buffers,
  otherwise bytes sent back-to-back with the switch command are lost. A
  controller MAY send `ZT1;` and its first frame in a single write.
- Resynchronization in frame mode: after a CRC mismatch the receiver MUST
  NOT trust the failed frame's LEN field — it resumes the SOH hunt at the
  byte following that frame's SOH. A receiver that does not complete a
  frame within 500 ms of its SOH byte discards the buffered bytes and
  hunts for the next SOH.

On USB the DATA interface is permanently in frame mode and the CAT interface
always carries only text, so no line-protocol switch happens; `ZT` keeps its role
as the state gate (the radio still stops RTX operation for file transfer).

> EDITOR: because CAT text is printable-only and frames are SOH-delimited,
> the two could technically coexist byte-interleaved on one line, keeping
> CAT reachable during long TNC sessions. Left out for simplicity; can be
> revisited if concurrent CAT over UART turns out to be needed.

## 5. Frame format

### 5.1. Layout

|  0   |  1   | 2..3     | 4 .. 4+LEN-1 | last 2 |
|:----:|:----:|:---------|:-------------|:-------|
| SOH  | PORT | LEN (LE) | payload      | CRC-16 (BE) |

- SOH is 0x01.
- PORT is the service identifier (section 3).
- LEN is the payload length in bytes, little-endian, 0 to 1026. Receivers
  MUST discard frames announcing more than 1026 bytes. The limit fits a DAT
  block carrying 1024 data bytes plus its two sequence bytes.
- The CRC-16 is computed over PORT, LEN and the payload (everything between
  SOH and the CRC) and transmitted big-endian, so a receiver may validate by
  computing the CRC over PORT..CRC and comparing with zero.

### 5.2. CRC

CRC-16/AUG-CCITT: polynomial 0x1021, init 0x1D0F, no reflection, no final
XOR, check value of "123456789" is 0xE5CC.

This is the polynomial and bit ordering of the firmware's `crc_ccitt()`
(`openrtx/src/core/crc.c`), which however initializes the register to
0x0000 — that variant is CRC-16/XMODEM and is unsuitable for framing:
leading zero bytes leave a zero register unchanged, so a header of
`port 0x00, LEN 0` would carry CRC 0x0000 and a run of zeros behind a
stray SOH byte would validate as an empty stdio frame. The 0x1D0F init
exists precisely to make leading zeros significant; the firmware function
gains an init parameter.

### 5.3. Test vectors

| Frame                          | Bytes on the wire |
|:-------------------------------|:------------------|
| FMP Meminfo request            | `01 02 02 00 01 00 6B 14` |
| DAT ACK                        | `01 03 01 00 06 C2 3A` |
| stdio carrying "OpenRTX"       | `01 00 07 00 4F 70 65 6E 52 54 58 CF E0` |

## 6. Worked example, single-UART session

Controller sets 145.5 MHz, reads the S-meter, then enters file transfer mode
and requests the memory descriptors:

```
C -> R:  FA00145500000;                      set frequency
C -> R:  SM0;                                read S-meter
R -> C:  SM00012;
C -> R:  ZT1;                                enter file transfer mode
R -> C:  ZT1;                                confirmed; line is now in frame mode
C -> R:  01 02 02 00 01 00 6B 14             frame: FMP Meminfo request
R -> C:  01 02 ...                           frame: FMP Meminfo response
C -> R:  01 02 02 00 11 00 68 67             frame: FMP Reboot
```

The radio reboots and the line is back in CAT mode.

## 7. Open questions

1. `ID;` code: own identifier plus Hamlib backend, or TS-480 emulation flag
   (section 2.2).
2. The `MD` digit assignments for DMR (8) and M17 (9) are arbitrary, better
   proposals welcome. If the TS-480 emulation build flag is adopted, digital
   modes would report `MD4` there to keep legacy parsers safe.
3. Exact `IF;` field layout to commit to (loggers parse it strictly).
4. Which AI notifications Kenwood-compatible software expects (`IF;` vs
   per-command answers) needs a check against Hamlib and common loggers.
5. TNC mode framing: KISS encapsulated in frames (port 0x04, uniform parser)
   or raw KISS on the line after `ZT2;` (direct compatibility with existing
   KISS software such as kissattach or Dire Wolf, at the cost of a second
   framing dialect). Input from the trx-control and M17Netd side would help
   here.
6. Whether the 500 ms frame and retransmission timeout is the right number.
7. The Kenwood tone table reaches 42 of the 50 OpenRTX CTCSS tones
   (section 2.5). Add a `Z*` command taking the frequency in decihertz to
   reach the remaining eight, or live with the subset?
8. DCS: OpenRTX has no DCS implementation today, so the tone commands
   cover CTCSS only. When the firmware gains DCS, adopt the TS-2000 `QC`
   command (three digits, the 103 standard DCS codes numbered sequentially
   from 000)?

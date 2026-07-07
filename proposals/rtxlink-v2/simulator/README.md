# rtxlink-sim

A radio simulator implementing the OpenRTX serial control and data protocol,
draft 2 (`../rtxlink-v2.md`). It models a single-UART radio, so it
exercises the most interesting part of the spec: Kenwood-style CAT and the
modal switch into frame mode on one line.

The simulated radio remembers its settings (frequency, mode, power, M17
identity, ...) across power cycles in `radio-fs/state.json`, stores uploaded
files in `radio-fs/` and backs its two simulated nonvolatile memories with
flash-like binary files in `radio-fs/nvm/`.

## Run

```bash
uv run rtxlink-sim --link /tmp/radio.pty
```

The simulator logs to stdout and prints the PTY path. Talk to it like to a
real radio:

```bash
picocom /tmp/radio.pty        # then type: ID; FA; MD9; ZV; AI2; ...
```

Try `AI2;` and watch unsolicited S-meter notifications arrive as the
simulated signal wanders. `ZT1;` switches the line to frame mode (your
terminal will show binary from that point; that is expected).

## End-to-end demo

With the simulator running:

```bash
uv run python demo_client.py /tmp/radio.pty
```

The client walks through the whole spec: CAT subset, tone and repeater
shift commands, Z* extensions, AI notifications, the `ZT1;` modal switch
(sent together with the first frame in a single write, exercising the
mode-boundary rule of spec section 4), FMP Meminfo/Write/Read/List, DAT
upload and download including a block retransmission, and FMP Reboot back
to CAT mode. It reuses the simulator's `frames` module, demonstrating that
the frame codec works for both ends.

## Layout

Module names mirror the spec sections: `cat.py` (section 2), `fmp.py` (3.1),
`dat.py` (3.2), `frames.py` (4 and 5), `crc.py` (5.2), `radio.py` (the line
mode state machine), `state.py` (persisted radio state).

# RTXLink v2 proposal

A discussion proposal for the OpenRTX serial control and data protocol,
following the direction agreed in the #remote-rig channel:

- **`rtxlink-v2.md`**, the draft specification: Kenwood-style text CAT
  (including CTCSS and repeater shift) with a small `Z*` extension set, a
  framed data channel (length-indicated, CRC-16), two USB CDC interfaces
  where USB is available, and modal switching on single-UART radios.
  Editor notes mark the open questions.
- **`simulator/`**, a Python reference implementation of the draft: a
  simulated radio on a PTY. It persists its settings and files, logs
  everything it does, and comes with an end-to-end demo client that
  exercises the whole spec (CAT subset, tones, AI notifications, mode
  switch, FMP, DAT transfers with retransmission, reboot).

## Try it

```bash
cd simulator
uv run rtxlink-sim --link /tmp/radio.pty      # terminal 1: the radio
picocom /tmp/radio.pty                        # terminal 2: type ID; FA; AI2; ...
uv run python demo_client.py /tmp/radio.pty   # or run the full demo session
```

Nothing here is final: the point of the simulator is to make the spec
concrete enough to poke at. Comments, objections and better ideas welcome.

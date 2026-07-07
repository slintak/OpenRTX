# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""End-to-end demo client for the rtxlink-sim radio simulator.

Exercises the whole draft spec against a running simulator: CAT commands,
tone and repeater-shift commands, Z* extensions, AI notifications, the
modal switch to file transfer mode (including the switch command and the
first frame sent in a single write), FMP, DAT transfers with a block
retransmission, and FMP Reboot back to CAT mode.

Usage:  uv run python demo_client.py <pty-path>
"""

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from rtxlink_sim import frames  # noqa: E402  (the frame codec works for both ends)

fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY)
os.set_blocking(fd, False)

import termios  # noqa: E402
attrs = termios.tcgetattr(fd)
attrs[0] = attrs[1] = attrs[3] = 0          # raw: no echo, no canonical mode
termios.tcsetattr(fd, termios.TCSANOW, attrs)

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def read_bytes(deadline: float) -> bytes:
    out = bytearray()
    while time.monotonic() < deadline:
        try:
            out += os.read(fd, 4096)
        except BlockingIOError:
            time.sleep(0.01)
    return bytes(out)


def cat(cmd: str, timeout: float = 1.0) -> str:
    """Send a CAT command, return the first ';'-terminated string that
    comes back — with AI off that is the command's answer."""
    os.write(fd, cmd.encode())
    buf = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            buf += os.read(fd, 4096).decode("ascii", "replace")
        except BlockingIOError:
            time.sleep(0.01)
        if ";" in buf:
            return buf.split(";")[0] + ";"
    return buf


decoder = frames.FrameDecoder()


def read_frame(timeout: float = 2.0) -> tuple[int, bytes] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            got = decoder.feed(os.read(fd, 4096))
            if got:
                return got[0]
        except BlockingIOError:
            time.sleep(0.01)
    return None


def fmp_frame(cmd: int, params: list[bytes] = ()) -> bytes:
    payload = bytes([cmd, len(params)]) + bytes(len(p) for p in params) \
        + b"".join(params)
    return frames.encode(frames.PORT_FMP, payload)


def parse_fmp(resp: bytes) -> tuple[int, list[bytes]]:
    status, nargs = resp[1], resp[2]
    lengths, pos, args = resp[3:3 + nargs], 3 + nargs, []
    for ln in lengths:
        args.append(resp[pos:pos + ln])
        pos += ln
    return status, args


def fmp(cmd: int, params: list[bytes] = ()) -> tuple[int, list[bytes]]:
    os.write(fd, fmp_frame(cmd, params))
    port, resp = read_frame()
    assert port == frames.PORT_FMP, f"unexpected port {port}"
    return parse_fmp(resp)


print("== CAT plane ==")
check("ID", cat("ID;") == "ID990;")
cat("FA00145737500;", 0.2)
check("FA set/read", cat("FA;") == "FA00145737500;")
cat("MD9;", 0.2)
check("MD set/read M17", cat("MD;") == "MD9;")
cat("ZCok5vas;", 0.2)                       # lowercase input, stored uppercase
check("ZC set/read (upcased)", cat("ZC;") == "ZCOK5VAS   ;")
cat("ZP002700;", 0.2)
check("ZP set/read", cat("ZP;") == "ZP002700;")
check("PC rounds to watts", cat("PC;") == "PC003;")
resp = cat("IF;")
check("IF is 38 chars", len(resp) == 38, repr(resp))
check("unknown command", cat("XX;") == "?;")
check("negative PC rejected", cat("PC-05;") == "?;")

print("== tones and repeater shift ==")
cat("TO1;", 0.2)
cat("TN12;", 0.2)
check("TO/TN set/read", cat("TO;") == "TO1;" and cat("TN;") == "TN12;")
check("TN42 (1750 Hz burst) rejected", cat("TN42;") == "?;")
check("CT default off", cat("CT;") == "CT0;")
cat("OS1;", 0.2)
cat("OF000600000;", 0.2)
check("OS/OF set/read", cat("OS;") == "OS1;" and cat("OF;") == "OF000600000;")

print("== VFO/memory, Kenwood FR digits ==")
cat("MC005;", 0.2)
check("MC switches to memory, FR reads 2", cat("FR;") == "FR2;")
check("FR1 (VFO B) unsupported", cat("FR1;") == "?;")
cat("FR0;", 0.2)
check("back to VFO", cat("FR;") == "FR0;")

print("== AI notifications ==")
cat("AI2;", 0.2)
noise = read_bytes(time.monotonic() + 3.5).decode("ascii", "replace")
check("unsolicited SM arrives", "SM0" in noise, noise.strip())
cat("AI0;", 0.2)
time.sleep(0.3)
read_bytes(time.monotonic() + 0.2)          # drain

print("== modal switch and file transfer ==")
# ZT1; and the first frame in a single write: every byte after the
# answer's ';' already belongs to frame mode (spec section 4).
os.write(fd, b"ZT1;" + fmp_frame(0x01))     # Meminfo
prefix, answer = bytearray(), None
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline and answer is None:
    try:
        data = os.read(fd, 4096)
    except BlockingIOError:
        time.sleep(0.01)
        continue
    if len(prefix) < 4:
        take = min(4 - len(prefix), len(data))
        prefix += data[:take]
        data = data[take:]
    for got in decoder.feed(data):
        answer = got
        break
check("ZT1 confirmed", bytes(prefix) == b"ZT1;")
check("frame in the same write is answered",
      answer is not None and answer[0] == frames.PORT_FMP)

status, args = parse_fmp(answer[1]) if answer else (None, [])
names = [struct.unpack("<IB27s", a)[2].rstrip(b"\0").decode() for a in args]
check("Meminfo", status == 0 and names == ["settings", "eflash"], str(names))

payload_data = b"Ahoj z demo klienta! " * 40          # 840 bytes
status, _ = fmp(0x05, [b"demo.txt", struct.pack("<I", len(payload_data))])
check("FMP Write accepted", status == 0)
block = frames.encode(frames.PORT_DAT, bytes([0, 0xFF]) + payload_data)
os.write(fd, block)
port, resp = read_frame()
check("DAT upload ACKed", port == frames.PORT_DAT and resp == bytes([0x06]))
os.write(fd, block)                         # retransmission, as if ACK was lost
port, resp = read_frame()
check("retransmitted block re-ACKed, not NAKed",
      port == frames.PORT_DAT and resp == bytes([0x06]))

status, args = fmp(0x04, [b"demo.txt"])    # Read back
size = struct.unpack("<I", args[0])[0]
check("FMP Read size matches", status == 0 and size == len(payload_data))
received = b""
os.write(fd, frames.encode(frames.PORT_DAT, bytes([0x06])))   # start with ACK
while len(received) < size:
    port, blk = read_frame()
    assert port == frames.PORT_DAT
    received += blk[2:]
    os.write(fd, frames.encode(frames.PORT_DAT, bytes([0x06])))
check("DAT download matches upload", received == payload_data)

status, args = fmp(0x06, [b"/"])           # List: Total (u16), then entries
total = struct.unpack("<H", args[0])[0]
check("FMP List shows demo.txt",
      status == 0 and b"demo.txt" in args[1:] and total == len(args) - 1,
      str(args[1:]))

print("== reboot back to CAT ==")
status, _ = fmp(0x11)                       # Reboot
check("Reboot acknowledged", status == 0)
time.sleep(0.3)
check("line is back in CAT mode", cat("ID;") == "ID990;")
check("frequency survived the reboot", cat("FA;") == "FA00145737500;")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

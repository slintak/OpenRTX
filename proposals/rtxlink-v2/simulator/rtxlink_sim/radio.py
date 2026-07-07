# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The simulated radio: line mode state machine, spec section 4.

The line is in exactly one mode at a time:

    CAT (default)  -> Kenwood-style text, handled by cat.py
    FILE transfer  -> frames only, FMP + DAT
    KISS TNC       -> frames only, KISS on port 0x04

`ZT1;`/`ZT2;` switch modes (confirmed before switching), FMP Reboot or a
power cycle leaves file transfer mode, the KISS return command (0xFF)
leaves TNC mode.
"""

import enum
import logging
import time
from pathlib import Path

from . import dat, frames
from .cat import CatHandler
from .fmp import FmpHandler
from .state import RadioState

log = logging.getLogger("radio")


class LineMode(enum.Enum):
    CAT = "CAT"
    FILE = "file transfer"
    TNC = "KISS TNC"


class Radio:
    def __init__(self, fs_root: Path, on_power_off):
        self.fs_root = fs_root
        (fs_root / "nvm").mkdir(parents=True, exist_ok=True)
        self.state_file = fs_root / "nvm" / "state.json"
        self.state = RadioState.load(self.state_file)
        self.mode = LineMode.CAT
        self.decoder = frames.FrameDecoder()
        self.fmp = FmpHandler(fs_root)
        self.cat = CatHandler(self.state,
                              on_data_mode=self._enter_data_mode,
                              on_power_off=on_power_off,
                              on_change=self._notify)
        self._pending_mode = None
        self._notify_last: dict[str, float] = {}
        self._out = bytearray()
        log.info("radio powered on, line mode: %s", self.mode.value)

    # ---- byte pump -----------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        """Process incoming bytes, return the bytes the radio sends back."""
        self._out.clear()
        chunk = data
        # The loop hands the unconsumed remainder of the chunk to the new
        # mode's parser whenever a mode boundary falls inside it (spec 4).
        while chunk:
            if self.mode is LineMode.CAT:
                out, chunk = self.cat.feed(chunk)
                self._out += out
                self.state.save(self.state_file)
                if self._pending_mode is not None:
                    self.mode = self._pending_mode
                    self._pending_mode = None
                    log.info("line mode: %s", self.mode.value)
            else:
                self.decoder.push(chunk)
                chunk = b""
                while self.mode is not LineMode.CAT and \
                        (frame := self.decoder.next_frame()) is not None:
                    self._frame(*frame)
                if self.mode is LineMode.CAT:   # left frame mode mid-chunk
                    chunk = self.decoder.take_remainder()
        return bytes(self._out)

    def tick(self) -> None:
        """Periodic housekeeping, called by the main loop (~10 Hz)."""
        self.decoder.check_timeout()
        if dat.expired(self.fmp.transfer):
            if self.fmp.transfer.done:
                log.info("completed DAT transfer closed")
            else:
                log.warning("DAT transfer timed out, aborting")
            self.fmp.transfer = None

    # ---- frame mode ----------------------------------------------------------

    def _frame(self, port: int, payload: bytes) -> None:
        if port == frames.PORT_FMP:
            in_file_mode = self.mode is LineMode.FILE
            self._send_frame(port, self.fmp.handle(payload, in_file_mode))
            if self.fmp.reboot_requested:
                self._reboot()
        elif port == frames.PORT_DAT:
            self._dat(payload)
        elif port == frames.PORT_KISS:
            self._kiss(payload)
        elif port == frames.PORT_STDIO:
            log.info("stdin from controller: %r", payload)
        else:
            log.warning("frame for unimplemented port 0x%02X, discarded", port)

    def _dat(self, payload: bytes) -> None:
        transfer = self.fmp.transfer
        if transfer is None:
            log.warning("DAT frame with no active transfer, discarded")
            return
        reply = transfer.handle(payload)
        if reply is not None:
            self._send_frame(frames.PORT_DAT, reply)
        # A finished sender received its final ACK and is gone; a finished
        # receiver lingers to re-ACK retransmissions of the final block
        # (spec 3.2) until the next FMP command or the timeout closes it.
        if transfer.done and isinstance(transfer, dat.DatSender):
            self.fmp.transfer = None

    def _kiss(self, payload: bytes) -> None:
        if self.mode is not LineMode.TNC:
            log.warning("KISS frame outside TNC mode, discarded")
            return
        if payload and payload[0] == 0xFF:      # KISS "return" command
            log.info("KISS return, leaving TNC mode")
            self.mode = LineMode.CAT
            log.info("line mode: %s", self.mode.value)
        elif payload and payload[0] & 0x0F == 0:
            log.info("KISS data frame, %d bytes 'transmitted over RF'; "
                     "echoing back as simulated reception", len(payload) - 1)
            self._send_frame(frames.PORT_KISS, payload)
        else:
            log.debug("KISS control frame 0x%02X ignored", payload[0])

    def _send_frame(self, port: int, payload: bytes) -> None:
        self._out += frames.encode(port, payload)

    # ---- mode switching and AI -----------------------------------------------

    def _enter_data_mode(self, target: int) -> None:
        self._pending_mode = LineMode.FILE if target == 1 else LineMode.TNC

    def _reboot(self) -> None:
        log.info("rebooting (FMP Reboot): reloading state, line mode back to CAT")
        self.state = RadioState.load(self.state_file)
        self.mode = LineMode.CAT
        self.fmp = FmpHandler(self.fs_root)
        # A reboot drops everything in flight: recreating the decoder also
        # discards bytes that followed the Reboot frame in the same chunk.
        self.decoder = frames.FrameDecoder()
        self.cat.reset()
        self.cat.state = self.state

    def _notify(self, name: str) -> None:
        """AI mode, spec section 2.4: emit the read-format answer on change."""
        if self.state.ai != 2 or self.mode is not LineMode.CAT or name == "AI":
            return
        now = time.monotonic()
        if now - self._notify_last.get(name, 0.0) < 0.1:    # 10 per second cap
            log.debug("AI notification for %s rate-limited", name)
            return
        self._notify_last[name] = now
        answer = self.cat.answer(name)
        log.info("AI notify: %r", answer)
        self._out += answer.encode("ascii")

    def environment_tick(self) -> bytes:
        """Simulated world: RSSI wanders, battery drains. Returns AI output."""
        import random
        self._out.clear()
        before = self.cat.answer("SM")
        self.state.rssi_dbm = max(-141.0, min(-53.0,
            self.state.rssi_dbm + random.uniform(-6, 6)))
        if self.cat.answer("SM") != before:     # notify on change only
            self._notify("SM")
        if random.random() < 0.02:
            self.state.battery = max(0, self.state.battery - 1)
            self._notify("ZR")
        return bytes(self._out)

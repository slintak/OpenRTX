# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""CAT channel, spec section 2.

Kenwood dialect: ASCII commands terminated by ';', two-letter names,
fixed-width parameters. Unknown or malformed input answers '?;'.
"""

import logging

from .state import RadioState, VALID_MODES, MAX_POWER_MW

log = logging.getLogger("cat")

RADIO_ID = "990"          # spec section 2.2, editor note: own ID code
FIRMWARE = "rtxlink-sim 0.1.0"
TONE_MAX = 41             # spec section 2.5; 42 (1750 Hz burst) unsupported
M17_CHARSET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/.")


class CatHandler:
    """Parses the CAT byte stream and executes commands against the state.

    The owner (radio.py) provides callbacks for the few commands with side
    effects beyond the state object: mode switch (ZT), power off (PS0) and
    state-change reporting used for AI notifications.
    """

    def __init__(self, state: RadioState, on_data_mode, on_power_off, on_change):
        self.state = state
        self.on_data_mode = on_data_mode    # called with 1 (file) or 2 (TNC)
        self.on_power_off = on_power_off
        self.on_change = on_change          # called with the changed command name
        self._buf = bytearray()
        self._switched = False

    def reset(self) -> None:
        """Drop buffered input, e.g. on reboot or when re-entering CAT mode."""
        self._buf.clear()

    # ---- byte stream handling, spec section 2.1 ----------------------------

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        """Consume CAT-mode bytes, return (answer bytes, unconsumed rest).

        Stops right after a mode-switching command: every byte after the
        answer's ';' belongs to the new mode (spec section 4) and is
        returned to the caller unconsumed.
        """
        out = bytearray()
        self._switched = False
        for pos, byte in enumerate(data):
            if byte in (0x0A, 0x0D, 0x00):      # CR/LF/NUL ignored
                continue
            if byte == 0x3B:                    # ';'
                cmd = self._buf.decode("ascii", "replace")
                self._buf.clear()
                out += self._execute(cmd).encode("ascii")
                if self._switched:
                    return bytes(out), data[pos + 1:]
            elif 0x20 <= byte <= 0x7E:
                self._buf += bytes([byte])
                if len(self._buf) > 64:         # runaway input, resync
                    log.warning("command longer than 64 chars, discarding")
                    self._buf.clear()
            else:
                log.warning("binary byte 0x%02X in CAT mode, ignoring", byte)
        return bytes(out), b""

    # ---- command execution --------------------------------------------------

    def _execute(self, cmd: str) -> str:
        if len(cmd) < 2:
            log.warning("malformed command %r", cmd)
            return "?;"
        name, param = cmd[:2].upper(), cmd[2:]
        handler = getattr(self, f"_cmd_{name}", None)
        if handler is None:
            log.info("unsupported command %r, answering ?;", cmd)
            return "?;"
        try:
            reply = handler(param)
        except (ValueError, IndexError):
            log.warning("malformed parameter in %r", cmd)
            return "?;"
        log.info("CAT %-14r -> %r", cmd + ";", reply)
        return reply

    def answer(self, name: str) -> str:
        """Read-format answer for a command name, also used by AI notify."""
        st = self.state
        match name:
            case "FA": return f"FA{st.freq_hz:011d};"
            case "MD": return f"MD{st.mode};"
            case "PC": return f"PC{max(1, round(st.power_mw / 1000)):03d};"
            case "SM": return f"SM0{self._smeter():04d};"
            case "SQ": return f"SQ0{st.squelch * 17:03d};"
            case "AG": return f"AG0{st.volume:03d};"
            case "MC": return f"MC{st.mem_channel:03d};"
            case "FR": return f"FR{2 if st.tuner_mem else 0};"
            case "FT": return f"FT{2 if st.tuner_mem else 0};"
            case "TO": return f"TO{int(st.tone_tx_en)};"
            case "TN": return f"TN{st.tone_tx:02d};"
            case "CT": return f"CT{int(st.tone_rx_en)};"
            case "CN": return f"CN{st.tone_rx:02d};"
            case "OS": return f"OS{st.offset_dir};"
            case "OF": return f"OF{st.offset_hz:09d};"
            case "AI": return f"AI{st.ai};"
            case "ZC": return f"ZC{st.m17_src:<9};"
            case "ZD": return f"ZD{st.m17_dst:<9};"
            case "ZN": return f"ZN{st.m17_can:02d};"
            case "ZP": return f"ZP{st.power_mw:06d};"
            case "ZR": return f"ZR{st.battery:03d};"
            case "TXRX": return "TX;" if st.ptt else "RX;"
        raise ValueError(name)

    def _smeter(self) -> int:
        # Map RSSI -141..-53 dBm to S-meter reading 0..30 (TS-590 range)
        return max(0, min(30, int((self.state.rssi_dbm + 141) / 3)))

    def _set(self, field: str, value, notify: str) -> str:
        setattr(self.state, field, value)
        log.info("state: %s = %r", field, value)
        self.on_change(notify)
        return ""                               # set commands are not answered

    @staticmethod
    def _int(p: str, width: int) -> int:
        """Parse a fixed-width unsigned decimal field (spec section 2.1)."""
        if len(p) != width or not p.isdigit():
            raise ValueError(p)
        return int(p)

    # ---- standard subset, spec section 2.2 ----------------------------------

    def _cmd_ID(self, p):
        return f"ID{RADIO_ID};" if not p else "?;"

    def _cmd_FA(self, p):
        if not p:
            return self.answer("FA")
        if len(p) != 11 or not p.isdigit():
            return "?;"
        return self._set("freq_hz", int(p), "FA")

    def _cmd_MD(self, p):
        if not p:
            return self.answer("MD")
        mode = self._int(p, 1)
        if mode not in VALID_MODES:
            return "?;"
        return self._set("mode", mode, "MD")

    def _cmd_TX(self, p):
        if p not in ("", "0", "1"):
            return "?;"
        return self._set("ptt", True, "TXRX")

    def _cmd_RX(self, p):
        return self._set("ptt", False, "TXRX") if not p else "?;"

    def _cmd_PC(self, p):
        if not p:
            return self.answer("PC")
        watts = self._int(p, 3)
        return self._set("power_mw", min(watts * 1000, MAX_POWER_MW), "PC")

    def _cmd_SM(self, p):
        return self.answer("SM") if p == "0" else "?;"

    def _cmd_SQ(self, p):
        if p == "0":
            return self.answer("SQ")
        if len(p) == 4 and p[0] == "0":
            return self._set("squelch", min(15, self._int(p[1:], 3) // 17), "SQ")
        return "?;"

    def _cmd_AG(self, p):
        if p == "0":
            return self.answer("AG")
        if len(p) == 4 and p[0] == "0":
            return self._set("volume", min(255, self._int(p[1:], 3)), "AG")
        return "?;"

    def _cmd_MC(self, p):
        if not p:
            return self.answer("MC")
        channel = self._int(p, 3)
        if not self.state.tuner_mem:
            self._set("tuner_mem", True, "FR")  # MC switches to memory mode
        return self._set("mem_channel", channel, "MC")

    def _cmd_FR(self, p):
        # Kenwood digits: 0 VFO A, 1 VFO B, 2 memory. No second VFO, so
        # 1 is unsupported (spec section 2.2).
        if not p:
            return self.answer("FR")
        if p not in ("0", "2"):
            return "?;"
        return self._set("tuner_mem", p == "2", "FR")

    _cmd_FT = _cmd_FR                           # no split operation, FT mirrors FR

    def _cmd_TO(self, p):
        if not p:
            return self.answer("TO")
        if p not in ("0", "1"):
            return "?;"
        return self._set("tone_tx_en", p == "1", "TO")

    def _cmd_TN(self, p):
        if not p:
            return self.answer("TN")
        tone = self._int(p, 2)
        if tone > TONE_MAX:
            return "?;"
        return self._set("tone_tx", tone, "TN")

    def _cmd_CT(self, p):
        if not p:
            return self.answer("CT")
        if p not in ("0", "1"):                 # 2 (cross tone) unsupported
            return "?;"
        return self._set("tone_rx_en", p == "1", "CT")

    def _cmd_CN(self, p):
        if not p:
            return self.answer("CN")
        tone = self._int(p, 2)
        if tone > TONE_MAX:
            return "?;"
        return self._set("tone_rx", tone, "CN")

    def _cmd_OS(self, p):
        if not p:
            return self.answer("OS")
        if p not in ("0", "1", "2"):            # 3 (E-type "=") unsupported
            return "?;"
        return self._set("offset_dir", int(p), "OS")

    def _cmd_OF(self, p):
        if not p:
            return self.answer("OF")
        return self._set("offset_hz", self._int(p, 9), "OF")

    def _cmd_PS(self, p):
        if not p:
            return "PS1;"
        if p == "0":
            self.on_power_off()
            return ""
        return "" if p == "1" else "?;"         # already on

    def _cmd_AI(self, p):
        if not p:
            return self.answer("AI")
        if p not in ("0", "2"):
            return "?;"
        return self._set("ai", int(p), "AI")

    def _cmd_IF(self, p):
        """Composite status, TS-480 field layout, 38 bytes.

        IF  f(11)  spc(5)  RIT(5)  RIT-on XIT bank ch(2) TX mode FR scan
            split tone tone-nr(2) shift
        """
        if p:
            return "?;"
        st = self.state
        return ("IF"
                + f"{st.freq_hz:011d}"          # P1  frequency
                + "     "                       # P2  step size (unused, 5 chars)
                + "+0000"                       # P3  RIT offset
                + "0" "0"                       # P4/P5 RIT/XIT off
                + f"{st.mem_channel // 100 % 10}"       # P6  memory bank
                + f"{st.mem_channel % 100:02d}"         # P7  memory channel
                + ("1" if st.ptt else "0")      # P8  TX/RX
                + f"{st.mode}"                  # P9  mode (OpenRTX table)
                + ("2" if st.tuner_mem else "0")  # P10 FR source (Kenwood digit)
                + "0"                           # P11 scan
                + "0"                           # P12 split
                + ("1" if st.tone_tx_en else "2" if st.tone_rx_en else "0")  # P13
                + f"{st.tone_tx if st.tone_tx_en else st.tone_rx:02d}"       # P14
                + f"{st.offset_dir}"            # P15 shift direction
                + ";")

    # ---- OpenRTX extensions, spec section 2.3 --------------------------------

    def _callsign(self, p, field, name):
        """ZC/ZD: M17 callsign alphabet, case-insensitive (spec section 2.3)."""
        if not p:
            return self.answer(name)
        if len(p) > 9:
            return "?;"
        call = p.strip().upper()
        if any(c not in M17_CHARSET for c in call):
            return "?;"
        return self._set(field, call, name)

    def _cmd_ZC(self, p):
        return self._callsign(p, "m17_src", "ZC")

    def _cmd_ZD(self, p):
        return self._callsign(p, "m17_dst", "ZD")   # empty = broadcast

    def _cmd_ZN(self, p):
        if not p:
            return self.answer("ZN")
        can = self._int(p, 2)
        if can > 15:
            return "?;"
        return self._set("m17_can", can, "ZN")

    def _cmd_ZP(self, p):
        if not p:
            return self.answer("ZP")
        if len(p) != 6 or not p.isdigit():
            return "?;"
        return self._set("power_mw", min(int(p), MAX_POWER_MW), "ZP")

    def _cmd_ZR(self, p):
        return self.answer("ZR") if not p else "?;"

    def _cmd_ZV(self, p):
        return f"ZV{FIRMWARE};" if not p else "?;"

    def _cmd_ZT(self, p):
        if not p:
            return "ZT0;"                       # in CAT mode we are always in 0
        if p == "0":
            return ""                           # setting normal mode is a no-op
        if p in ("1", "2"):
            # Confirm first, then switch: every byte after the answer's ';'
            # belongs to the new mode (spec section 4), so feed() stops here
            # and hands the rest of its input back to the radio.
            self.on_data_mode(int(p))
            self._switched = True
            return f"ZT{p};"
        return "?;"

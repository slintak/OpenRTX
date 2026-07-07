# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Simulated radio state, spec-visible parameters only.

Persisted to <fs-dir>/state.json so the radio "remembers" its settings
across power cycles, like the real firmware does with its NVM.
"""

import json
import logging
from dataclasses import dataclass, asdict, fields
from pathlib import Path

log = logging.getLogger("state")

# MD digits, spec section 2.2
MODE_FM = 4
MODE_NFM = 6
MODE_DMR = 8
MODE_M17 = 9
VALID_MODES = {MODE_FM, MODE_NFM, MODE_DMR, MODE_M17}

MAX_POWER_MW = 5000  # simulated handheld


@dataclass
class RadioState:
    freq_hz: int = 145_500_000   # FA
    mode: int = MODE_FM          # MD
    power_mw: int = 1000         # PC / ZP
    squelch: int = 4             # SQ, 0..15
    volume: int = 128            # AG, 0..255
    mem_channel: int = 1         # MC
    tuner_mem: bool = False      # FR/FT, False = VFO (0), True = memory (2)
    tone_tx_en: bool = False     # TO
    tone_tx: int = 8             # TN, Kenwood tone number (08 = 88.5 Hz)
    tone_rx_en: bool = False     # CT
    tone_rx: int = 8             # CN
    offset_dir: int = 0          # OS, 0 simplex, 1 plus, 2 minus
    offset_hz: int = 0           # OF, 9 digits
    m17_src: str = "N0CALL"      # ZC, up to 9 chars
    m17_dst: str = ""            # ZD
    m17_can: int = 0             # ZN, 0..15

    # Volatile, not persisted:
    ptt: bool = False            # TX/RX
    ai: int = 0                  # AI, 0 or 2, resets on power cycle
    battery: int = 87            # ZR, percent
    rssi_dbm: float = -121.0     # SM

    _VOLATILE = ("ptt", "ai", "battery", "rssi_dbm")

    def save(self, path: Path) -> None:
        data = {k: v for k, v in asdict(self).items() if k not in self._VOLATILE}
        path.write_text(json.dumps(data, indent=2))
        log.debug("state saved to %s", path)

    @classmethod
    def load(cls, path: Path) -> "RadioState":
        state = cls()
        if path.exists():
            known = {f.name for f in fields(cls)}
            for key, value in json.loads(path.read_text()).items():
                if key in known and key not in cls._VOLATILE:
                    setattr(state, key, value)
            log.info("state restored from %s", path)
        else:
            log.info("no saved state, using defaults")
        return state

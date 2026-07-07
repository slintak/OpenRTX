# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Radio simulator for the OpenRTX serial control and data protocol, draft 2.

Module layout mirrors the specification:

    cat.py     -- section 2, CAT channel (Kenwood dialect + Z* extensions)
    frames.py  -- sections 4 and 5, modal line handling and frame format
    fmp.py     -- section 3.1, file management protocol
    dat.py     -- section 3.2, bulk data transfer
    radio.py   -- ties it all together, holds the line mode state machine
    state.py   -- the simulated radio state, persisted as JSON
    crc.py     -- section 5.2, CRC-16/AUG-CCITT
"""

__version__ = "0.1.0"

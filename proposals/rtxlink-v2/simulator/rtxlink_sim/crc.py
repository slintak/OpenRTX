# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""CRC-16/AUG-CCITT, spec section 5.2.

Polynomial 0x1021, init 0x1D0F, no reflection, no final XOR.
Check value: crc(b"123456789") == 0xE5CC.

Same polynomial and bit ordering as the firmware's crc_ccitt(), which
however starts from 0x0000 (CRC-16/XMODEM); the framing uses the 0x1D0F
init so that leading zero bytes affect the checksum (spec section 5.2).
"""


def crc_ccitt(data: bytes) -> int:
    crc = 0x1D0F
    for byte in data:
        x = ((crc >> 8) ^ byte) & 0xFF
        x ^= x >> 4
        crc = ((crc << 8) ^ (x << 12) ^ (x << 5) ^ x) & 0xFFFF
    return crc


assert crc_ccitt(b"123456789") == 0xE5CC

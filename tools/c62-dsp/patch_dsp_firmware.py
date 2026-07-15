#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Create the C62 DSP image used for native 48 kHz audio capture.

All offsets are relative to the raw blob, which is loaded at 0x60000000. The
replacement bytes were assembled and disassembled with the exact venus_hifi4
Xtensa configuration. Every replacement preserves its FLIX bundle size.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path


STOCK_SHA256 = "7dd697c9c9b11d2d9164400d067efa81a3b7ffb5af176133550621c2e8fcbdb8"
PATCHED_SHA256 = "f2f9f798af3c3db89e971a28e53bd1c1afc2135ded61e15a301856ae48ce6c8f"


@dataclass(frozen=True)
class Patch:
    offset: int
    original: bytes
    replacement: bytes
    description: str


PATCHES = (
    Patch(
        0x13F4D,
        bytes.fromhex("cf 85 01 08 ca 02 10 02 08 8b 00"),
        bytes.fromhex("cf 55 01 08 ca 02 10 02 08 8b 00"),
        "allow non-16-kHz AudioRecord requests",
    ),
    Patch(
        0x13F9A,
        bytes.fromhex("8e a0 dd 09 5b e8"),
        bytes.fromhex("8e a0 05 0d 1b fd"),
        "publish the requested AudioRecord rate",
    ),
    Patch(
        0x58EDF,
        bytes.fromhex("22 a0 7d 90 22 11"),
        bytes.fromhex("22 a1 77 90 22 11"),
        "select the 48-kHz AudioHardware input rate",
    ),
    Patch(
        0x1B059,
        bytes.fromhex("cf aa a2 0b 1a 00 90 00 12 8c 00"),
        bytes.fromhex("cf aa a4 0b 1a 00 90 00 12 8c 00"),
        "select the supported 48-kHz/OSR-250 PDM configuration",
    ),
    Patch(
        0x17001,
        bytes.fromhex("fe a0 01 08 1b e0"),
        bytes.fromhex("fe e0 01 18 1b e0"),
        "set the RecordThread frame size to 480 samples",
    ),
    Patch(
        0x1700F,
        bytes.fromhex("4e 0a a0 2f 19 e0"),
        bytes.fromhex("4e 0a e0 2f 59 e0"),
        "set the client IC-stream frame size to 480 samples",
    ),
    Patch(
        0x174E6,
        bytes.fromhex("ee 04 01 0d 1b e0"),
        bytes.fromhex("ee 01 01 0d 1b e0"),
        "fit one complete six-channel frame in the input FIFO",
    ),
    Patch(
        0x174EC,
        bytes.fromhex("be 06 a0 0c 1b e0"),
        bytes.fromhex("be 06 e0 0c 5b e0"),
        "publish complete 480-sample frames for all six input channels",
    ),
    Patch(
        0x174F2,
        bytes.fromhex("ee a1 0f 0e 19 e0"),
        bytes.fromhex("ee a1 2d 0e 19 e0"),
        "set the input FIFO length multiplier for 5760 bytes",
    ),
    Patch(
        0x17504,
        bytes.fromhex("fe b1 ee 07 59 e8"),
        bytes.fromhex("fe b1 ee 09 59 e8"),
        "set the input FIFO length shift for 5760 bytes",
    ),
)


def sha256(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def apply_patch(blob: bytearray, patch: Patch) -> None:
    end = patch.offset + len(patch.original)
    actual = bytes(blob[patch.offset:end])
    if actual != patch.original:
        raise SystemExit(
            f"refusing unexpected bytes at 0x{patch.offset:x}: "
            f"got {actual.hex(' ')}, expected {patch.original.hex(' ')}"
        )
    if len(patch.original) != len(patch.replacement):
        raise RuntimeError("DSP patches must preserve the binary layout")
    blob[patch.offset:end] = patch.replacement


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Patch the known C62 DSP firmware for native 48-kHz audio"
    )
    parser.add_argument("input", type=Path, help="stock dsp_firmware.bin")
    parser.add_argument("output", type=Path, help="patched output image")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.input.resolve() == args.output.resolve():
        raise SystemExit("input and output must be different files")

    stock = args.input.read_bytes()
    stock_digest = sha256(stock)
    if stock_digest != STOCK_SHA256:
        raise SystemExit(
            "refusing unknown DSP firmware:\n"
            f"  input:    {args.input}\n"
            f"  SHA-256: {stock_digest}\n"
            f"  expected: {STOCK_SHA256}"
        )

    patched = bytearray(stock)
    for patch in PATCHES:
        apply_patch(patched, patch)
        print(f"0x{patch.offset:06x}: {patch.description}")

    patched_digest = sha256(patched)
    if patched_digest != PATCHED_SHA256:
        raise SystemExit(
            f"internal verification failed: output SHA-256 {patched_digest}"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(patched)
    print(f"\nwrote {args.output} ({len(patched)} bytes)")
    print(f"SHA-256: {patched_digest}")


if __name__ == "__main__":
    main()

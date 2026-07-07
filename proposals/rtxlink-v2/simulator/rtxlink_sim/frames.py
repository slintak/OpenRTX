# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Frame format and frame-mode reception, spec sections 4 and 5.

Frame layout: SOH | PORT | LEN (u16 LE) | payload | CRC-16 (BE).
The CRC covers PORT..payload; appending it big-endian lets a receiver
validate by computing the CRC over PORT..CRC and comparing with zero.
"""

import logging
import time

from .crc import crc_ccitt

log = logging.getLogger("frame")

SOH = 0x01
MAX_PAYLOAD = 1026          # DAT block: 1024 data + 2 sequence bytes
FRAME_TIMEOUT = 0.5         # seconds, spec section 4

PORT_STDIO = 0x00
PORT_FMP = 0x02
PORT_DAT = 0x03
PORT_KISS = 0x04


def encode(port: int, payload: bytes) -> bytes:
    header = bytes([port]) + len(payload).to_bytes(2, "little") + payload
    return bytes([SOH]) + header + crc_ccitt(header).to_bytes(2, "big")


class FrameDecoder:
    """Incremental decoder for the frame-mode byte stream."""

    def __init__(self):
        self._buf = bytearray()
        self._started = 0.0

    def push(self, data: bytes) -> None:
        self._buf += data

    def next_frame(self) -> tuple[int, bytes] | None:
        """Extract one frame from the buffer, or None if none is complete."""
        while self._buf:
            # Hunt for SOH, discard anything before it
            if self._buf[0] != SOH:
                skip = self._buf.find(bytes([SOH]))
                dropped = skip if skip >= 0 else len(self._buf)
                log.warning("discarding %d bytes while hunting for SOH", dropped)
                del self._buf[:dropped]
                continue
            if len(self._buf) < 4:
                self._started = self._started or time.monotonic()
                return None
            length = int.from_bytes(self._buf[2:4], "little")
            if length > MAX_PAYLOAD:
                log.warning("frame announces %d bytes (max %d), discarding SOH",
                            length, MAX_PAYLOAD)
                del self._buf[0]
                continue
            total = 1 + 3 + length + 2
            if len(self._buf) < total:
                self._started = self._started or time.monotonic()
                return None
            if crc_ccitt(self._buf[1:total]) != 0:  # residue check, PORT..CRC
                # Spec section 4: the LEN field of a failed frame cannot be
                # trusted; resume the SOH hunt right after this SOH.
                log.warning("frame CRC mismatch, resyncing")
                del self._buf[0]
                continue
            frame = bytes(self._buf[:total])
            del self._buf[:total]
            self._started = 0.0
            return frame[1], frame[4:4 + length]
        self._started = 0.0
        return None

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Convenience wrapper: push bytes, return all completed frames."""
        self.push(data)
        frames = []
        while (frame := self.next_frame()) is not None:
            frames.append(frame)
        return frames

    def take_remainder(self) -> bytes:
        """Hand back buffered bytes, e.g. when the line leaves frame mode."""
        rest = bytes(self._buf)
        self._buf.clear()
        self._started = 0.0
        return rest

    def check_timeout(self) -> None:
        """Spec section 4: abort an unfinished frame after 500 ms."""
        if self._started and time.monotonic() - self._started > FRAME_TIMEOUT:
            log.warning("frame timeout, discarding %d buffered bytes", len(self._buf))
            self._buf.clear()
            self._started = 0.0

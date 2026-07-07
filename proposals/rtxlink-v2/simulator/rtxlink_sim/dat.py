# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""DAT, bulk data transfer, spec section 3.2.

Block payload: seq | ~seq | data (<= 1024 bytes). ACK 0x06, NAK 0x15.
Direction and total byte count come from the preceding FMP command.
"""

import logging
import time

log = logging.getLogger("dat")

ACK = 0x06
NAK = 0x15
BLOCK_SIZE = 1024
TIMEOUT = 2.0
MAX_RETRIES = 10


class DatSender:
    """Radio to controller (FMP Dump/Read). Driven by controller ACKs.

    Stays armed until the ACK following the final block arrives, so the
    controller can still NAK a corrupted final block (spec section 3.2).
    """

    def __init__(self, data: bytes):
        self.blocks = [data[i:i + BLOCK_SIZE] for i in range(0, len(data), BLOCK_SIZE)]
        self.index = -1                     # first ACK starts block 0
        self.retries = 0
        self.deadline = time.monotonic() + TIMEOUT
        log.info("send transfer armed: %d bytes in %d blocks",
                 len(data), len(self.blocks))

    def handle(self, payload: bytes) -> bytes | None:
        """Returns the next block payload, or None when the transfer is over."""
        self.deadline = time.monotonic() + TIMEOUT
        if payload and payload[0] == ACK:
            self.index += 1
            self.retries = 0
        else:
            self.retries += 1
            log.warning("NAK/garbage, resending block %d (retry %d)",
                        self.index, self.retries)
        if self.index >= len(self.blocks):
            log.info("send transfer complete")
            return None
        block = self.blocks[max(self.index, 0)]
        seq = max(self.index, 0) & 0xFF
        return bytes([seq, seq ^ 0xFF]) + block

    @property
    def done(self) -> bool:
        return self.index >= len(self.blocks)


class DatReceiver:
    """Controller to radio (FMP Flash/Write). Answers ACK/NAK per block.

    A retransmission of the previously acknowledged block means our ACK
    was lost: it is re-acknowledged and its data discarded (spec section
    3.2), which also covers retransmissions of the final block after the
    transfer completed.
    """

    def __init__(self, total: int, sink):
        self.total = total
        self.sink = sink                    # callable(bytes) -> None, on completion
        self.data = bytearray()
        self.expected = 0
        self.retries = 0
        self.deadline = time.monotonic() + TIMEOUT
        log.info("receive transfer armed: expecting %d bytes", total)

    def handle(self, payload: bytes) -> bytes:
        self.deadline = time.monotonic() + TIMEOUT
        if len(payload) < 2 or (payload[0] ^ payload[1]) != 0xFF:
            self.retries += 1
            log.warning("malformed block, NAK (retry %d)", self.retries)
            return bytes([NAK])
        if self.expected > 0 and payload[0] == ((self.expected - 1) & 0xFF):
            log.info("retransmission of block %d, re-ACK", payload[0])
            return bytes([ACK])
        if payload[0] != (self.expected & 0xFF):
            self.retries += 1
            log.warning("bad block (expected seq %d, got %d), NAK (retry %d)",
                        self.expected & 0xFF, payload[0], self.retries)
            return bytes([NAK])
        self.retries = 0
        self.data += payload[2:]
        self.expected += 1
        log.debug("block %d ok, %d/%d bytes", payload[0], len(self.data), self.total)
        if self.done and self.sink is not None:
            self.sink(bytes(self.data[:self.total]))
            self.sink = None                # commit once
            log.info("receive transfer complete, %d bytes", self.total)
        return bytes([ACK])

    @property
    def done(self) -> bool:
        return len(self.data) >= self.total


def expired(transfer) -> bool:
    """Spec: fail after 2 s without a valid frame or 10 retransmissions."""
    return transfer is not None and (
        time.monotonic() > transfer.deadline or transfer.retries > MAX_RETRIES)

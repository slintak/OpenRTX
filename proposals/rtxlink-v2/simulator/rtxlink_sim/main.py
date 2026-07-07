# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Entry point: expose the simulated radio on a PTY.

The simulator prints the PTY path (and can symlink it to a stable path
with --link); point any serial program at it:

    picocom /tmp/radio.pty      # or screen, miniterm, ...

This models a single-UART radio (spec section 4), which exercises the
modal switching. Logs go to stdout.
"""

import argparse
import logging
import os
import select
import sys
import time
from pathlib import Path

from .radio import Radio

log = logging.getLogger("main")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fs", type=Path, default=Path("radio-fs"),
                        help="directory backing the radio filesystem and NVM")
    parser.add_argument("--link", type=Path, default=None,
                        help="create a stable symlink to the PTY at this path")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging, including raw byte dumps")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-6s %(message)s",
        datefmt="%H:%M:%S", stream=sys.stdout)

    args.fs.mkdir(parents=True, exist_ok=True)

    master, slave = os.openpty()
    pty_path = os.ttyname(slave)
    os.set_blocking(master, False)
    if args.link:
        args.link.unlink(missing_ok=True)
        args.link.symlink_to(pty_path)
        log.info("PTY symlinked at %s", args.link)
    log.info("radio serial port: %s", pty_path)

    powered_on = True

    def power_off():
        nonlocal powered_on
        log.info("PS0; received, radio shutting down")
        powered_on = False

    radio = Radio(args.fs, on_power_off=power_off)
    last_env = time.monotonic()

    def write(data: bytes) -> None:
        try:
            os.write(master, data)
        except BlockingIOError:
            log.warning("PTY buffer full (no reader?), dropping %d bytes", len(data))

    while powered_on:
        readable, _, _ = select.select([master], [], [], 0.1)
        if readable:
            try:
                data = os.read(master, 4096)
            except OSError:
                data = b""
            if data:
                log.debug("<- %s", data.hex(" "))
                reply = radio.feed(data)
                if reply:
                    log.debug("-> %s", reply.hex(" "))
                    write(reply)
        radio.tick()
        if time.monotonic() - last_env >= 1.0:
            last_env = time.monotonic()
            out = radio.environment_tick()
            if out:
                write(out)

    radio.state.save(radio.state_file)
    log.info("state saved, bye")


if __name__ == "__main__":
    main()

# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""FMP, file management protocol, spec section 3.1.

Request:  CMD | NParams | len_1..len_N | param_1..param_N
Response: CMD | status  | NArgs | len_1..len_N | arg_1..arg_N

The radio filesystem is backed by a real directory; the nonvolatile
memories are flash-like binary files under <fs-dir>/nvm/.
"""

import errno
import logging
import shutil
import struct
from pathlib import Path

from . import dat
from .frames import MAX_PAYLOAD

log = logging.getLogger("fmp")

CMD_MEMINFO = 0x01
CMD_DUMP = 0x02
CMD_FLASH = 0x03
CMD_READ = 0x04
CMD_WRITE = 0x05
CMD_LIST = 0x06
CMD_MOVE = 0x07
CMD_COPY = 0x08
CMD_MKDIR = 0x09
CMD_REMOVE = 0x0A
CMD_REBOOT = 0x11
CMD_RESET = 0xFF

OK = 0x00
EUNSPEC = 0xFF
MAX_PATH = 128

# Spec section 3.1: available whenever the DATA channel is reachable; the
# rest requires file transfer mode.
ALWAYS_ALLOWED = {CMD_MEMINFO, CMD_LIST, CMD_REBOOT, CMD_RESET}

# Simulated nonvolatile memories: name -> size. Flags are all zero, their
# bit assignments are an open point in the spec.
MEMORIES = [("settings", 64 * 1024), ("eflash", 256 * 1024)]


def parse_request(payload: bytes) -> tuple[int, list[bytes]] | None:
    if len(payload) < 2:
        return None
    cmd, nparams = payload[0], payload[1]
    lengths = payload[2:2 + nparams]
    if len(lengths) != nparams:
        return None
    params, pos = [], 2 + nparams
    for length in lengths:
        params.append(payload[pos:pos + length])
        if len(params[-1]) != length:
            return None
        pos += length
    return cmd, params


def response(cmd: int, status: int, args: list[bytes] = ()) -> bytes:
    out = bytes([cmd, status, len(args)]) + bytes(len(a) for a in args)
    return out + b"".join(args)


class FmpHandler:
    def __init__(self, fs_root: Path):
        self.root = fs_root.resolve()
        self.nvm = self.root / "nvm"
        self.nvm.mkdir(parents=True, exist_ok=True)
        for name, size in MEMORIES:
            backing = self.nvm / f"{name}.bin"
            if not backing.exists():
                backing.write_bytes(b"\xFF" * size)   # erased flash
        self.transfer = None            # active DatSender/DatReceiver
        self.reboot_requested = False

    # ---- helpers -------------------------------------------------------------

    def _resolve(self, raw: bytes) -> Path | None:
        """Sandbox a protocol path into the filesystem root."""
        if len(raw) > MAX_PATH:
            return None
        target = (self.root / raw.decode("ascii", "replace").lstrip("/")).resolve()
        if not target.is_relative_to(self.root) or target.is_relative_to(self.nvm):
            log.warning("path %r escapes the radio filesystem, rejected", raw)
            return None
        return target

    def _memory(self, index: int) -> Path | None:
        if 0 <= index < len(MEMORIES):
            return self.nvm / f"{MEMORIES[index][0]}.bin"
        return None

    # ---- request dispatch ----------------------------------------------------

    def handle(self, payload: bytes, in_file_mode: bool) -> bytes:
        parsed = parse_request(payload)
        if parsed is None:
            log.warning("malformed FMP request: %s", payload.hex(" "))
            return response(payload[0] if payload else 0, errno.EINVAL)
        cmd, params = parsed
        log.info("FMP cmd 0x%02X, %d parameter(s)", cmd, len(params))

        # A new FMP command closes a completed transfer that lingered for
        # re-ACKs of its final block (spec section 3.2).
        if self.transfer is not None and self.transfer.done:
            self.transfer = None

        if cmd not in ALWAYS_ALLOWED and not in_file_mode:
            return response(cmd, errno.EPERM)
        try:
            return self._dispatch(cmd, params)
        except OSError as exc:
            log.warning("FMP I/O error: %s", exc)
            return response(cmd, exc.errno or EUNSPEC)

    def _dispatch(self, cmd: int, p: list[bytes]) -> bytes:
        match cmd:
            case _ if cmd == CMD_MEMINFO:
                args = [struct.pack("<IB27s", size, 0, name.encode())
                        for name, size in MEMORIES]
                return response(cmd, OK, args)

            case _ if cmd == CMD_DUMP:
                mem = self._memory(p[0][0]) if p else None
                if mem is None:
                    return response(cmd, errno.EINVAL)
                data = mem.read_bytes()
                self.transfer = dat.DatSender(data)
                return response(cmd, OK, [struct.pack("<I", len(data))])

            case _ if cmd == CMD_FLASH:
                if len(p) != 2 or len(p[1]) != 4:
                    return response(cmd, errno.EINVAL)
                mem, size = self._memory(p[0][0]), struct.unpack("<I", p[1])[0]
                if mem is None or size > mem.stat().st_size:
                    return response(cmd, errno.EINVAL)
                self.transfer = dat.DatReceiver(size, mem.write_bytes)
                return response(cmd, OK)

            case _ if cmd == CMD_READ:
                path = self._resolve(p[0]) if p else None
                if path is None or not path.is_file():
                    return response(cmd, errno.ENOENT)
                data = path.read_bytes()
                self.transfer = dat.DatSender(data)
                return response(cmd, OK, [struct.pack("<I", len(data))])

            case _ if cmd == CMD_WRITE:
                if len(p) != 2 or len(p[1]) != 4:
                    return response(cmd, errno.EINVAL)
                path, size = self._resolve(p[0]), struct.unpack("<I", p[1])[0]
                if path is None:
                    return response(cmd, errno.EINVAL)
                if size > 10 * 1024 * 1024:
                    return response(cmd, errno.ENOSPC)
                self.transfer = dat.DatReceiver(size, path.write_bytes)
                return response(cmd, OK)

            case _ if cmd == CMD_LIST:
                path = self._resolve(p[0]) if p else self.root
                if path is None or not path.is_dir():
                    return response(cmd, errno.ENOENT)
                index = 0
                if len(p) > 1:
                    if len(p[1]) != 2:
                        return response(cmd, errno.EINVAL)
                    index = struct.unpack("<H", p[1])[0]
                names = sorted(e.name + ("/" if e.is_dir() else "")
                               for e in path.iterdir() if e != self.nvm)
                # Paginated (spec section 3.1): Total, then entries from
                # Index, as many as fit one frame.
                args = [struct.pack("<H", len(names))]
                used = 3 + 1 + 2        # CMD+status+NArgs, len byte + Total
                for name in names[index:]:
                    entry = name.encode()
                    if used + 1 + len(entry) > MAX_PAYLOAD or len(args) == 255:
                        break
                    args.append(entry)
                    used += 1 + len(entry)
                return response(cmd, OK, args)

            case _ if cmd == CMD_MOVE or cmd == CMD_COPY:
                if len(p) != 2:
                    return response(cmd, errno.EINVAL)
                src, dst = self._resolve(p[0]), self._resolve(p[1])
                if dst is None:
                    return response(cmd, errno.EINVAL)
                if src is None or not src.exists():
                    return response(cmd, errno.ENOENT)
                (shutil.move if cmd == CMD_MOVE else shutil.copy)(src, dst)
                return response(cmd, OK)

            case _ if cmd == CMD_MKDIR:
                path = self._resolve(p[0]) if p else None
                if path is None:
                    return response(cmd, errno.EINVAL)
                path.mkdir(parents=True, exist_ok=True)
                return response(cmd, OK)

            case _ if cmd == CMD_REMOVE:
                path = self._resolve(p[0]) if p else None
                if path is None or not path.exists():
                    return response(cmd, errno.ENOENT)
                shutil.rmtree(path) if path.is_dir() else path.unlink()
                return response(cmd, OK)

            case _ if cmd == CMD_REBOOT:
                # Answered before rebooting; radio.py performs the reboot.
                self.reboot_requested = True
                return response(cmd, OK)

            case _ if cmd == CMD_RESET:
                if self.transfer:
                    log.info("FMP reset, aborting active transfer")
                self.transfer = None
                return response(cmd, OK)

            case _:
                return response(cmd, errno.ENOTSUP)

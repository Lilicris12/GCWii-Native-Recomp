"""GameCube (GCM / ISO) disc images and the shared "game data volume" layout.

Both a raw GameCube disc and a *decrypted* Wii game partition share the same
layout (boot.bin, bi2.bin, apploader, main.dol, FST), so :class:`Volume`
implements everything on top of an abstract ``read(offset, size)``.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator

from .fst import FSTEntry, files_only, parse_fst

GC_MAGIC = 0xC2339F3D
WII_MAGIC = 0x5D1C9EA3

REGIONS = {
    "E": "USA", "P": "Europe", "J": "Japan", "K": "Korea", "D": "Germany",
    "F": "France", "S": "Spain", "I": "Italy", "U": "Australia", "X": "Europe",
    "Y": "Europe", "W": "Taiwan",
}


@dataclass
class DiscHeader:
    game_id: str
    maker: str
    disc_number: int
    version: int
    title: str
    is_wii: bool
    is_gamecube: bool
    dol_offset: int
    fst_offset: int
    fst_size: int
    fst_max_size: int
    raw: bytes

    @property
    def region(self) -> str:
        return REGIONS.get(self.game_id[3:4], "Unknown") if len(self.game_id) >= 4 else "Unknown"


def parse_header(raw: bytes, offset_shift: int) -> DiscHeader:
    gid = raw[0:4].decode("ascii", errors="replace")
    maker = raw[4:6].decode("ascii", errors="replace")
    wii_magic, gc_magic = struct.unpack_from(">II", raw, 0x18)
    title = raw[0x20:0x400].split(b"\0", 1)[0].decode("shift_jis", errors="replace").strip()
    dol, fst, fst_size, fst_max = struct.unpack_from(">IIII", raw, 0x420)
    return DiscHeader(
        game_id=gid + maker, maker=maker, disc_number=raw[6], version=raw[7], title=title,
        is_wii=wii_magic == WII_MAGIC, is_gamecube=gc_magic == GC_MAGIC,
        dol_offset=dol << offset_shift, fst_offset=fst << offset_shift,
        fst_size=fst_size << offset_shift, fst_max_size=fst_max << offset_shift, raw=raw[:0x440],
    )


def dol_size_from_header(hdr: bytes) -> int:
    offs = struct.unpack_from(">18I", hdr, 0x00)
    sizes = struct.unpack_from(">18I", hdr, 0x90)
    end = 0x100
    for o, s in zip(offs, sizes):
        if o and s:
            end = max(end, o + s)
    return end


class Volume:
    """A readable game-data volume (GC disc or decrypted Wii partition)."""

    #: Shift applied to offsets stored in boot.bin / FST (0 on GC, 2 on Wii).
    offset_shift = 0
    platform = "gamecube"

    def read(self, offset: int, size: int) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError

    def size(self) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- derived accessors -------------------------------------------------
    @property
    def header(self) -> DiscHeader:
        if not hasattr(self, "_header"):
            self._header = parse_header(self.read(0, 0x440), self.offset_shift)
        return self._header

    def read_bi2(self) -> bytes:
        return self.read(0x440, 0x2000)

    def read_apploader(self) -> bytes:
        hdr = self.read(0x2440, 0x20)
        size, trailer = struct.unpack_from(">II", hdr, 0x14)
        return self.read(0x2440, 0x20 + size + trailer)

    def read_dol(self) -> bytes:
        off = self.header.dol_offset
        hdr = self.read(off, 0x100)
        return self.read(off, dol_size_from_header(hdr))

    def read_fst_raw(self) -> bytes:
        h = self.header
        return self.read(h.fst_offset, h.fst_size)

    def fst(self) -> list[FSTEntry]:
        if not hasattr(self, "_fst"):
            self._fst = parse_fst(self.read_fst_raw(), self.offset_shift)
        return self._fst

    def files(self) -> list[FSTEntry]:
        return files_only(self.fst())

    def read_file(self, entry: FSTEntry) -> bytes:
        return self.read(entry.offset, entry.length)

    def iter_file_chunks(self, entry: FSTEntry, chunk: int = 4 << 20) -> Iterator[bytes]:
        pos = 0
        while pos < entry.length:
            n = min(chunk, entry.length - pos)
            yield self.read(entry.offset + pos, n)
            pos += n

    def extract_all(self, out_dir: str, progress: Callable[[str], None] | None = None) -> None:
        sys_dir = os.path.join(out_dir, "sys")
        os.makedirs(sys_dir, exist_ok=True)
        with open(os.path.join(sys_dir, "boot.bin"), "wb") as f:
            f.write(self.read(0, 0x440))
        with open(os.path.join(sys_dir, "bi2.bin"), "wb") as f:
            f.write(self.read_bi2())
        with open(os.path.join(sys_dir, "apploader.img"), "wb") as f:
            f.write(self.read_apploader())
        with open(os.path.join(sys_dir, "main.dol"), "wb") as f:
            f.write(self.read_dol())
        with open(os.path.join(sys_dir, "fst.bin"), "wb") as f:
            f.write(self.read_fst_raw())
        for e in self.files():
            dst = os.path.join(out_dir, "files", *e.path.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if progress:
                progress(e.path)
            with open(dst, "wb") as f:
                for blk in self.iter_file_chunks(e):
                    f.write(blk)

    def write_flat_image(self, path: str, progress: Callable[[int, int], None] | None = None,
                         chunk: int = 8 << 20) -> None:
        """Write the whole volume as a flat, decrypted image for the runtime's DVD layer."""
        total = self.size()
        with open(path, "wb") as f:
            pos = 0
            while pos < total:
                n = min(chunk, total - pos)
                f.write(self.read(pos, n))
                pos += n
                if progress:
                    progress(pos, total)


class GCMVolume(Volume):
    """Raw GameCube disc image (.iso / .gcm)."""

    def __init__(self, fp: BinaryIO, base: int = 0, length: int | None = None):
        self.fp = fp
        self.base = base
        fp.seek(0, os.SEEK_END)
        self._size = (fp.tell() - base) if length is None else length

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or offset + size > self._size:
            raise ValueError(f"Read out of range: 0x{offset:X}+0x{size:X} (volume 0x{self._size:X})")
        self.fp.seek(self.base + offset)
        data = self.fp.read(size)
        if len(data) != size:
            raise IOError("Short read from disc image")
        return data

    def size(self) -> int:
        return self._size

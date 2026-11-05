"""Disc / executable input handling.

:func:`open_source` accepts any of:

* GameCube disc images (``.iso`` / ``.gcm``)
* Wii disc images (``.iso`` / ``.wii``) - requires your console's common key
* An extracted disc directory (Dolphin "Extract Entire Disc" layout: ``sys/main.dol``)
* A bare ``.dol`` executable (homebrew)

Compressed containers (RVZ, WIA, WBFS, CISO, GCZ, NKit) are detected and
rejected with a hint: convert them to ISO first with Dolphin
("Convert File..." in the game list context menu).
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

from .gcm import GC_MAGIC, WII_MAGIC, GCMVolume, Volume, parse_header
from .wii import load_common_key, open_game_partition, read_partition_table

COMPRESSED_MAGICS = {
    b"RVZ\x01": "RVZ", b"WIA\x01": "WIA", b"WBFS": "WBFS", b"CISO": "CISO",
    b"\x01\xC0\x0B\xB1": "GCZ",
}


class UnsupportedFormat(Exception):
    pass


@dataclass
class Source:
    """Everything the recompiler needs from an input."""

    path: str
    kind: str                 # "gamecube-disc" | "wii-disc" | "directory" | "dol"
    platform: str             # "gamecube" | "wii"
    game_id: str
    title: str
    dol: bytes
    volume: Volume | None = None
    extra: dict = field(default_factory=dict)

    def describe(self) -> str:
        lines = [f"Input     : {self.path}", f"Format    : {self.kind}", f"Platform  : {self.platform}",
                 f"Game ID   : {self.game_id or '-'}", f"Title     : {self.title or '-'}",
                 f"main.dol  : {len(self.dol):,} bytes"]
        if self.volume is not None:
            try:
                lines.append(f"Files     : {len(self.volume.files())} in FST")
                lines.append(f"Region    : {self.volume.header.region}")
            except Exception as exc:  # noqa: BLE001 - informational only
                lines.append(f"FST       : unreadable ({exc})")
        for k, v in self.extra.items():
            lines.append(f"{k:<10}: {v}")
        return "\n".join(lines)


def _looks_like_dol(data: bytes) -> bool:
    if len(data) < 0x100:
        return False
    offs = struct.unpack_from(">18I", data, 0)
    addrs = struct.unpack_from(">18I", data, 0x48)
    entry = struct.unpack_from(">I", data, 0xE0)[0]
    if not 0x80000000 <= entry < 0x81800000 and not 0x80000000 <= entry < 0x94000000:
        return False
    return any(o >= 0x100 for o in offs) and all(a == 0 or a >= 0x80000000 for a in addrs)


def open_source(path: str, common_key: str | bytes | None = None) -> Source:
    path = os.path.abspath(path)
    if os.path.isdir(path):
        dol_path = os.path.join(path, "sys", "main.dol")
        if not os.path.isfile(dol_path):
            dol_path = os.path.join(path, "main.dol")
        if not os.path.isfile(dol_path):
            raise UnsupportedFormat("Directory does not contain sys/main.dol")
        boot = os.path.join(path, "sys", "boot.bin")
        gid = title = ""
        platform = "gamecube"
        if os.path.isfile(boot):
            h = parse_header(open(boot, "rb").read(0x440).ljust(0x440, b"\0"), 0)
            gid, title = h.game_id, h.title
            platform = "wii" if h.is_wii else "gamecube"
        return Source(path, "directory", platform, gid, title, open(dol_path, "rb").read())

    fp = open(path, "rb")
    head = fp.read(0x440)
    if head[:4] in COMPRESSED_MAGICS:
        fp.close()
        raise UnsupportedFormat(
            f"{COMPRESSED_MAGICS[head[:4]]} images are not supported directly. Convert to ISO with Dolphin "
            "(right-click the game > Convert File... > ISO) and try again.")
    if len(head) >= 0x20:
        wii_magic, gc_magic = struct.unpack_from(">II", head, 0x18)
    else:
        wii_magic = gc_magic = 0

    if gc_magic == GC_MAGIC:
        vol = GCMVolume(fp)
        h = vol.header
        return Source(path, "gamecube-disc", "gamecube", h.game_id, h.title, vol.read_dol(), vol)

    if wii_magic == WII_MAGIC:
        h = parse_header(head, 2)
        parts = read_partition_table(fp)
        key = load_common_key(common_key)
        vol = open_game_partition(fp, key)
        ph = vol.header
        return Source(path, "wii-disc", "wii", h.game_id, h.title or ph.title, vol.read_dol(), vol,
                      extra={"Partitions": ", ".join(p.type_name for p in parts)})

    fp.seek(0)
    data = fp.read()
    fp.close()
    if _looks_like_dol(data):
        entry = struct.unpack_from(">I", data, 0xE0)[0]
        name = os.path.splitext(os.path.basename(path))[0]
        # Heuristic: Wii DOLs normally load into/above 0x80004000 and use MEM2; a
        # config can override the platform later.
        return Source(path, "dol", "gamecube", "", name, data, extra={"Entry": f"0x{entry:08X}"})
    raise UnsupportedFormat("Unrecognised input: not a GameCube/Wii disc image, extracted disc or DOL")


__all__ = ["Source", "open_source", "UnsupportedFormat", "Volume"]

"""File System Table (FST) parser shared by GameCube and Wii discs."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


@dataclass
class FSTEntry:
    index: int
    name: str
    is_dir: bool
    offset: int          # file: byte offset on disc/partition. dir: parent index
    length: int          # file: size in bytes. dir: index of next entry outside this dir
    path: str = ""
    children: list["FSTEntry"] = field(default_factory=list)


def parse_fst(data: bytes, offset_shift: int = 0) -> list[FSTEntry]:
    """Parse a raw FST blob.

    ``offset_shift`` is 2 for Wii discs, where file offsets are stored >> 2.
    Returns a flat list of entries (index 0 is the root directory) with
    ``path`` filled in for every entry.
    """
    if len(data) < 12:
        raise ValueError("FST too small")
    count = struct.unpack_from(">I", data, 8)[0]
    if count == 0 or count * 12 > len(data):
        raise ValueError(f"Corrupt FST (entry count {count})")
    strtab = count * 12
    entries: list[FSTEntry] = []
    for i in range(count):
        word0, off, length = struct.unpack_from(">III", data, i * 12)
        is_dir = bool(word0 >> 24)
        name_off = word0 & 0xFFFFFF
        if i == 0:
            name = ""
        else:
            end = data.find(b"\0", strtab + name_off)
            name = data[strtab + name_off:end].decode("shift_jis", errors="replace")
        if not is_dir:
            off <<= offset_shift
        entries.append(FSTEntry(i, name, is_dir, off, length))

    # Resolve paths with an explicit directory stack.
    stack: list[tuple[int, str]] = [(entries[0].length, "")]
    for e in entries[1:]:
        while stack and e.index >= stack[-1][0]:
            stack.pop()
        parent_path = stack[-1][1] if stack else ""
        e.path = f"{parent_path}/{e.name}" if parent_path else e.name
        if e.is_dir:
            stack.append((e.length, e.path))
    return entries


def files_only(entries: list[FSTEntry]) -> list[FSTEntry]:
    return [e for e in entries if not e.is_dir]

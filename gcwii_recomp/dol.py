"""DOL executable format (GameCube/Wii main executables)."""
from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass
class Section:
    name: str
    file_offset: int
    address: int
    size: int
    is_text: bool
    data: bytes

    @property
    def end(self) -> int:
        return self.address + self.size

    def contains(self, addr: int) -> bool:
        return self.address <= addr < self.end


class DOL:
    def __init__(self, data: bytes):
        if len(data) < 0x100:
            raise ValueError("DOL too small")
        self.raw = data
        offs = struct.unpack_from(">18I", data, 0x00)
        addrs = struct.unpack_from(">18I", data, 0x48)
        sizes = struct.unpack_from(">18I", data, 0x90)
        self.bss_address, self.bss_size, self.entry = struct.unpack_from(">III", data, 0xD8)
        self.sections: list[Section] = []
        for i in range(18):
            if not sizes[i] or not addrs[i]:
                continue
            is_text = i < 7
            name = f".text{i}" if is_text else f".data{i - 7}"
            blob = data[offs[i]:offs[i] + sizes[i]]
            if len(blob) != sizes[i]:
                raise ValueError(f"DOL section {name} truncated")
            self.sections.append(Section(name, offs[i], addrs[i], sizes[i], is_text, blob))

    @property
    def text_sections(self) -> list[Section]:
        return [s for s in self.sections if s.is_text]

    @property
    def data_sections(self) -> list[Section]:
        return [s for s in self.sections if not s.is_text]

    def section_for(self, addr: int) -> Section | None:
        for s in self.sections:
            if s.contains(addr):
                return s
        return None

    def is_code(self, addr: int) -> bool:
        s = self.section_for(addr)
        return s is not None and s.is_text

    def read32(self, addr: int) -> int | None:
        s = self.section_for(addr)
        if s is None or addr + 4 > s.end:
            return None
        return struct.unpack_from(">I", s.data, addr - s.address)[0]

    @property
    def uses_mem2(self) -> bool:
        return any(s.address >= 0x90000000 for s in self.sections) or self.bss_address >= 0x90000000

    def summary(self) -> str:
        rows = [f"entry 0x{self.entry:08X}  bss 0x{self.bss_address:08X}+0x{self.bss_size:X}"]
        for s in self.sections:
            rows.append(f"  {s.name:<8} 0x{s.address:08X}-0x{s.end:08X} ({s.size:,} bytes)")
        return "\n".join(rows)


def build_dol(text: list[tuple[int, bytes]], data: list[tuple[int, bytes]], entry: int,
              bss: tuple[int, int] = (0, 0)) -> bytes:
    """Create a DOL image (used by the test-suite and for patched executables)."""
    if len(text) > 7 or len(data) > 11:
        raise ValueError("Too many sections")
    hdr = bytearray(0x100)
    body = bytearray()
    pos = 0x100
    slots = [(i, a, b) for i, (a, b) in enumerate(text)] + [(7 + i, a, b) for i, (a, b) in enumerate(data)]
    for slot, addr, blob in slots:
        blob = bytes(blob) + b"\0" * (-len(blob) % 32)
        struct.pack_into(">I", hdr, slot * 4, pos)
        struct.pack_into(">I", hdr, 0x48 + slot * 4, addr)
        struct.pack_into(">I", hdr, 0x90 + slot * 4, len(blob))
        body += blob
        pos += len(blob)
    struct.pack_into(">III", hdr, 0xD8, bss[0], bss[1], entry)
    return bytes(hdr + body)

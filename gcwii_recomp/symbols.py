"""Symbol map import.

Symbols are optional but strongly recommended: they name the generated
functions, seed function discovery and let SDK functions (OSReport,
VIWaitForRetrace, PADRead, ...) be replaced by native HLE implementations.

Supported formats (auto-detected per line):

* Dolphin ``.map`` exports (``80003100 000040 80003100 0 __start``)
* decomp-toolkit ``symbols.txt`` (``__start = .init:0x80003100; // type:function size:0x40``)
* CodeWarrior linker maps (same column layout as Dolphin)
* Plain ``0x80003100 name [size]`` / ``name 0x80003100`` lines
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Symbol:
    name: str
    address: int
    size: int = 0
    kind: str = "function"   # "function" | "object" | "unknown"


_DTK = re.compile(r"^\s*([^\s=]+)\s*=\s*(?:[.\w]+:)?0x([0-9A-Fa-f]{8})\s*;(.*)$")
_DTK_SIZE = re.compile(r"size:0x([0-9A-Fa-f]+)")
_DTK_TYPE = re.compile(r"type:(\w+)")
_MAP = re.compile(r"^\s*([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{6,8})\s+([0-9A-Fa-f]{8})\s+(?:[0-9A-Fa-f]{8}\s+)?\d+\s+(\S+)")
_PLAIN_A = re.compile(r"^\s*(?:0x)?([0-9A-Fa-f]{8})[\s,;:]+([A-Za-z_.$@?][\w.$@?<>:,~*&\- ]*?)(?:[\s,;]+(?:0x)?([0-9A-Fa-f]+))?\s*$")
_PLAIN_B = re.compile(r"^\s*([A-Za-z_.$@?][\w.$@?<>:~]*)[\s,;=]+0x([0-9A-Fa-f]{8})(?:[\s,;]+(?:0x)?([0-9A-Fa-f]+))?\s*$")


def parse_symbols(text: str) -> list[Symbol]:
    syms: list[Symbol] = []
    section = ""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(("#", "//")):
            continue
        if "section layout" in line:
            section = line.split()[0]
            continue
        m = _DTK.match(line)
        if m:
            rest = m.group(3)
            size = int(_DTK_SIZE.search(rest).group(1), 16) if _DTK_SIZE.search(rest) else 0
            typ = _DTK_TYPE.search(rest).group(1) if _DTK_TYPE.search(rest) else "unknown"
            syms.append(Symbol(m.group(1), int(m.group(2), 16), size,
                               "function" if typ == "function" else ("object" if typ == "object" else "unknown")))
            continue
        m = _MAP.match(line)
        if m:
            size = int(m.group(2), 16)
            if size == 0 or m.group(4).startswith("."):
                continue
            kind = "function" if section in (".text", ".init", "") else "object"
            syms.append(Symbol(m.group(4), int(m.group(3), 16), size, kind))
            continue
        m = _PLAIN_A.match(line)
        if m:
            syms.append(Symbol(m.group(2).strip(), int(m.group(1), 16),
                               int(m.group(3), 16) if m.group(3) else 0, "unknown"))
            continue
        m = _PLAIN_B.match(line)
        if m:
            syms.append(Symbol(m.group(1), int(m.group(2), 16),
                               int(m.group(3), 16) if m.group(3) else 0, "unknown"))
    return syms


def load_symbols(path: str) -> list[Symbol]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return parse_symbols(f.read())


_C_IDENT = re.compile(r"[^0-9A-Za-z_]")


def c_identifier(name: str) -> str:
    ident = _C_IDENT.sub("_", name)
    if not ident or ident[0].isdigit():
        ident = "_" + ident
    return ident[:96]

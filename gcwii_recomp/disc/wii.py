"""Wii disc images: partition table, ticket/title-key handling and decryption.

The Wii common key is **not** shipped with this tool.  Dump it from your own
console (e.g. ``keys.bin`` produced by BootMii) and pass it with
``--common-key`` / the GUI's key field, or set ``GCWII_COMMON_KEY`` to its hex
value or a file path.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import BinaryIO

from .aes import aes_cbc_decrypt
from .gcm import Volume

CLUSTER_SIZE = 0x8000
CLUSTER_HASH = 0x400
CLUSTER_DATA = 0x7C00

PARTITION_TYPES = {0: "DATA", 1: "UPDATE", 2: "CHANNEL"}


@dataclass
class PartitionInfo:
    offset: int
    type: int
    group: int

    @property
    def type_name(self) -> str:
        if self.type in PARTITION_TYPES:
            return PARTITION_TYPES[self.type]
        raw = self.type.to_bytes(4, "big")
        return raw.decode("ascii", errors="replace") if all(32 <= b < 127 for b in raw) else f"0x{self.type:08X}"


def read_partition_table(fp: BinaryIO) -> list[PartitionInfo]:
    fp.seek(0x40000)
    groups = struct.unpack(">8I", fp.read(32))
    parts: list[PartitionInfo] = []
    for g in range(4):
        count, table = groups[g * 2], groups[g * 2 + 1] << 2
        if count == 0 or count > 64:
            continue
        fp.seek(table)
        raw = fp.read(count * 8)
        for i in range(count):
            off, typ = struct.unpack_from(">II", raw, i * 8)
            parts.append(PartitionInfo(off << 2, typ, g))
    return parts


def load_common_key(spec: str | bytes | None, index: int = 0) -> bytes:
    """Resolve a common key from raw bytes, hex text, a 16-byte file or a BootMii keys.bin."""
    if spec is None:
        spec = os.environ.get("GCWII_COMMON_KEY")
    if spec is None:
        raise KeyError(
            "This is a Wii disc. Supply the Wii common key dumped from your console "
            "(--common-key <hex|file> or GCWII_COMMON_KEY).")
    if isinstance(spec, bytes):
        if len(spec) != 16:
            raise ValueError("Common key must be 16 bytes")
        return spec
    s = spec.strip()
    if os.path.isfile(s):
        data = open(s, "rb").read()
        if len(data) == 16:
            return data
        if len(data) == 0x400:  # BootMii keys.bin: common key at 0x114
            return data[0x114:0x124]
        txt = data.decode("ascii", errors="ignore").strip()
        return bytes.fromhex(txt)[:16]
    key = bytes.fromhex(s)
    if len(key) != 16:
        raise ValueError("Common key must be 32 hex digits")
    return key


class WiiPartition(Volume):
    """A decrypted view of one Wii partition's data area."""

    offset_shift = 2
    platform = "wii"

    def __init__(self, fp: BinaryIO, info: PartitionInfo, common_keys: dict[int, bytes] | bytes):
        self.fp = fp
        self.info = info
        fp.seek(info.offset)
        hdr = fp.read(0x2C0)
        self.ticket = hdr[:0x2A4]
        self.title_id = self.ticket[0x1DC:0x1E4]
        key_index = self.ticket[0x1F1]
        if isinstance(common_keys, bytes):
            common = common_keys
        else:
            if key_index not in common_keys:
                raise KeyError(f"Partition needs common key index {key_index} (0=retail, 1=Korean, 2=vWii)")
            common = common_keys[key_index]
        enc_title_key = self.ticket[0x1BF:0x1CF]
        self.title_key = aes_cbc_decrypt(common, self.title_id + b"\0" * 8, enc_title_key)
        (self.tmd_size, tmd_off, self.cert_size, cert_off, h3_off,
         data_off, data_size) = struct.unpack_from(">IIIIIII", hdr, 0x2A4)
        self.data_offset = info.offset + (data_off << 2)
        self.data_size = data_size << 2
        self._cache_idx = -1
        self._cache = b""

    def size(self) -> int:
        return self.data_size // CLUSTER_SIZE * CLUSTER_DATA

    def _cluster(self, idx: int) -> bytes:
        if idx == self._cache_idx:
            return self._cache
        self.fp.seek(self.data_offset + idx * CLUSTER_SIZE)
        raw = self.fp.read(CLUSTER_SIZE)
        if len(raw) != CLUSTER_SIZE:
            raise IOError(f"Truncated Wii cluster {idx}")
        iv = raw[0x3D0:0x3E0]
        self._cache = aes_cbc_decrypt(self.title_key, iv, raw[CLUSTER_HASH:])
        self._cache_idx = idx
        return self._cache

    def read(self, offset: int, size: int) -> bytes:
        out = bytearray()
        while size > 0:
            idx, inner = divmod(offset, CLUSTER_DATA)
            n = min(size, CLUSTER_DATA - inner)
            out += self._cluster(idx)[inner:inner + n]
            offset += n
            size -= n
        return bytes(out)


def open_game_partition(fp: BinaryIO, common_key) -> WiiPartition:
    parts = read_partition_table(fp)
    data = [p for p in parts if p.type == 0]
    if not data:
        raise ValueError("No DATA partition found on Wii disc")
    keys = common_key if isinstance(common_key, (dict, bytes)) else {0: load_common_key(common_key)}
    return WiiPartition(fp, data[0], keys)

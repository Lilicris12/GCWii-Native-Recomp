"""Unit tests: decoder, AES, disc formats, symbols, analysis."""
from __future__ import annotations

import os
import struct

import pytest

from gcwii_recomp.disc import UnsupportedFormat, open_source
from gcwii_recomp.disc.aes import _PyAES, aes_cbc_decrypt, aes_cbc_encrypt
from gcwii_recomp.disc.fst import parse_fst
from gcwii_recomp.dol import DOL, build_dol
from gcwii_recomp.ppc import decode
from gcwii_recomp.symbols import parse_symbols


# --- decoder ---------------------------------------------------------------
@pytest.mark.parametrize("word,name,text", [
    (0x7C0802A6, "mfspr", "mfspr r0, lr"),
    (0x9421FFF0, "stwu", "stwu r1, -16(r1)"),
    (0x4E800020, "bclr", "blr"),
    (0x4E800421, "bcctr", "bctrl"),
    (0x38600000, "addi", "li r3, 0"),
    (0x5463103A, "rlwinm", "rlwinm r3, r3, 2, 0, 29"),
    (0x7C632214, "add", "add r3, r3, r4"),
    (0x7C632614, "add", "addo r3, r3, r4"),
    (0xFC20081E, "fctiwz", "fctiwz f1, f1"),
    (0x10221028, "ps_sub", "ps_sub f1, f2, f2"),
    (0xE0230000, "psq_l", "psq_l f1, 0(r3), 0, qr0"),
    (0x1003000C, "psq_lx", None),
    (0x60000000, "ori", "nop"),
    (0x4C000064, "rfi", "rfi"),
    (0x7C0004AC, "sync", "sync"),
    (0x00000000, "illegal", None),
])
def test_decode(word, name, text):
    i = decode(word, 0x80003100)
    assert i.name == name
    if text:
        assert str(i) == text


def test_branch_targets():
    assert decode(0x4BFFFFF0, 0x80003100).branch_target == 0x800030F0
    assert decode(0x48000011, 0x80003100).branch_target == 0x80003110   # bl +0x10
    bc = decode(0x4182000C, 0x80003100)                                  # beq +12
    assert bc.name == "bc" and bc.branch_target == 0x8000310C and not bc.is_unconditional


# --- AES ---------------------------------------------------------------------
def test_pure_python_aes_matches_fips197():
    key = bytes(range(16))
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    a = _PyAES(key)
    assert bytes(a._encrypt_block(list(pt))) == ct
    assert bytes(a._decrypt_block(list(ct))) == pt


def test_aes_backend_roundtrip():
    key, iv = os.urandom(16), os.urandom(16)
    data = os.urandom(64)
    assert aes_cbc_decrypt(key, iv, aes_cbc_encrypt(key, iv, data)) == data
    assert _PyAES(key).cbc_decrypt(iv, aes_cbc_encrypt(key, iv, data)) == data


# --- FST / DOL / disc ----------------------------------------------------------
def _fst(entries, names):
    return b"".join(struct.pack(">III", *e) for e in entries) + names


def test_fst_paths():
    names = b"dir\0a.bin\0b.bin\0"
    fst = _fst([(0x01000000, 0, 4), (0x01000000, 0, 3), (0x00000004, 0x1000, 10), (0x0000000A, 0x2000, 20)], names)
    e = parse_fst(fst)
    assert [x.path for x in e[1:]] == ["dir", "dir/a.bin", "b.bin"]
    assert e[2].offset == 0x1000 and e[3].length == 20
    assert parse_fst(fst, offset_shift=2)[2].offset == 0x4000


def test_dol_roundtrip():
    dol = DOL(build_dol([(0x80003100, b"\x60\0\0\0" * 8)], [(0x80200000, b"abcd")], 0x80003100, (0x80300000, 0x100)))
    assert dol.entry == 0x80003100
    assert dol.read32(0x80003104) == 0x60000000
    assert dol.bss_size == 0x100
    assert len(dol.text_sections) == 1 and len(dol.data_sections) == 1


def test_gamecube_iso(tmp_path):
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from sample_program import FILE_PAYLOAD, build_disc
    iso = tmp_path / "g.iso"
    build_disc(str(iso))
    src = open_source(str(iso))
    assert src.kind == "gamecube-disc" and src.game_id == "GTSE01"
    f = src.volume.files()[0]
    assert f.path == "hello.txt" and src.volume.read_file(f) == FILE_PAYLOAD
    src.volume.extract_all(str(tmp_path / "x"))
    assert (tmp_path / "x" / "files" / "hello.txt").read_bytes() == FILE_PAYLOAD
    assert open_source(str(tmp_path / "x")).dol == src.dol


def test_compressed_rejected(tmp_path):
    p = tmp_path / "game.rvz"
    p.write_bytes(b"RVZ\x01" + b"\0" * 0x500)
    with pytest.raises(UnsupportedFormat, match="Convert to ISO"):
        open_source(str(p))


def _make_wii_disc(path, common_key: bytes, dol: bytes):
    """Build an encrypted single-partition Wii image (test key only)."""
    title_key = os.urandom(16)
    title_id = bytes.fromhex("0001000052545345")
    # decrypted partition data: header + dol + fst
    part = bytearray(0x7C00 * 4)
    part[0:6] = b"RTSE01"
    struct.pack_into(">I", part, 0x18, 0x5D1C9EA3)
    part[0x20:0x30] = b"Wii Test Disc\0\0\0"
    dol_off, fst_off = 0x3000, 0x2800
    fst = _fst([(0x01000000, 0, 1)], b"")
    struct.pack_into(">III", part, 0x420, dol_off >> 2, fst_off >> 2, len(fst) >> 2)
    part[dol_off:dol_off + len(dol)] = dol
    part[fst_off:fst_off + len(fst)] = fst
    # encrypt clusters
    clusters = bytearray()
    for i in range(0, len(part), 0x7C00):
        h = bytearray(os.urandom(0x400))
        enc_h = aes_cbc_encrypt(title_key, b"\0" * 16, bytes(h))
        # the data IV is read from the *encrypted* hash block
        iv = enc_h[0x3D0:0x3E0]
        clusters += enc_h + aes_cbc_encrypt(title_key, iv, bytes(part[i:i + 0x7C00]))
    img = bytearray(0x50000 + 0x20000 + len(clusters))
    img[0:6] = b"RTSE01"
    struct.pack_into(">I", img, 0x18, 0x5D1C9EA3)
    struct.pack_into(">II", img, 0x40000, 1, 0x40020 >> 2)
    poff = 0x50000
    struct.pack_into(">II", img, 0x40020, poff >> 2, 0)
    ticket = bytearray(0x2A4)
    ticket[0x1BF:0x1CF] = aes_cbc_encrypt(common_key, title_id + b"\0" * 8, title_key)
    ticket[0x1DC:0x1E4] = title_id
    img[poff:poff + 0x2A4] = ticket
    data_off = 0x20000
    struct.pack_into(">IIIIIII", img, poff + 0x2A4, 0, 0, 0, 0, 0, data_off >> 2, len(clusters) >> 2)
    img[poff + data_off:poff + data_off + len(clusters)] = clusters
    path.write_bytes(img)


def test_wii_partition_decryption(tmp_path, monkeypatch):
    key = os.urandom(16)          # stand-in key: the real common key is never bundled
    dol = build_dol([(0x80004000, b"\x4e\x80\x00\x20" * 4)], [], 0x80004000)
    p = tmp_path / "wii.iso"
    _make_wii_disc(p, key, dol)
    monkeypatch.delenv("GCWII_COMMON_KEY", raising=False)
    with pytest.raises(KeyError, match="common key"):
        open_source(str(p), None)
    src = open_source(str(p), key.hex())
    assert src.platform == "wii" and src.game_id == "RTSE01"
    assert src.dol == dol


# --- symbols -------------------------------------------------------------------
def test_symbol_formats():
    text = """
.text section layout
  80003100 000040 80003100  4 __start \tos.a os.c
__init_hardware = .init:0x80003200; // type:function size:0x20 scope:global
0x80004000 OSReport 0x40
memcpy 0x80005000
"""
    syms = {s.name: s for s in parse_symbols(text)}
    assert syms["__start"].address == 0x80003100 and syms["__start"].size == 0x40
    assert syms["__init_hardware"].address == 0x80003200 and syms["__init_hardware"].kind == "function"
    assert syms["OSReport"].address == 0x80004000 and syms["OSReport"].size == 0x40
    assert syms["memcpy"].address == 0x80005000

"""Builds a synthetic GameCube disc whose DOL exercises many instruction classes.

Each check stores a 32-bit word into RESULTS + offset; `EXPECTED` lists the
values the recompiled program must produce.
"""
from __future__ import annotations

import struct

from gcwii_recomp.dol import build_dol
from ppcasm import Asm

TEXT = 0x80003100
DATA = 0x80300000
RESULTS = 0x80400000
FILE_PAYLOAD = b"Hello from the DVD drive!\0\0\0\0\0\0\0"   # 32 bytes


def build_program(file_offset: int):
    a = Asm(TEXT)
    d = Asm(DATA)

    # ---------------- data ----------------
    d.label("ptrtab"); d.emit(lambda pc: a.addr("square"))
    d.label("jtab")
    for c in ("case0", "case1", "case2", "case3"):
        d.emit((lambda name: lambda pc: a.addr(name))(c))
    d.label("dbl"); d.word(0x3FF80000); d.word(0); d.word(0x40020000); d.word(0)       # 1.5, 2.25
    d.label("flt"); d.word(0x3F800000); d.word(0x40000000)                             # 1.0f, 2.0f
    d.label("bytes"); d.word(0x0A140000)                                               # 10, 20
    d.label("fmt")
    s = b"fib=%d %s %.2f\n\0"
    s += b"\0" * (-len(s) % 4)
    for i in range(0, len(s), 4):
        d.word(struct.unpack(">I", s[i:i + 4])[0])
    d.label("okstr"); d.word(0x6F6B0000)                                               # "ok"
    d.label("cbtab"); d.emit(lambda pc: a.addr("callback"))

    # ---------------- text ----------------
    a.label("_start")
    a.load32(1, 0x80F00000)
    a.load32(31, RESULTS)
    # 0: recursive fib(10) = 55
    a.li(3, 10); a.bl("fib"); a.stw(3, 0, 31)
    # 4: counted loop sum 1..100 = 5050
    a.li(3, 0); a.li(4, 100); a.mtctr(4); a.li(5, 1)
    a.label("loop"); a.add(3, 3, 5); a.addi(5, 5, 1); a.bdnz("loop")
    a.stw(3, 4, 31)
    # 8: indirect call through a pointer table in .data: square(7) = 49
    a.load_addr(4, "ptrtab"); a.lwz(12, 0, 4); a.mtctr(12); a.li(3, 7); a.bctrl(); a.stw(3, 8, 31)
    # 12/16: jump-table switch
    a.li(3, 2); a.bl("sw"); a.stw(3, 12, 31)
    a.li(3, 9); a.bl("sw"); a.stw(3, 16, 31)
    # 20: carry chain: 0xFFFFFFFF + 1 -> 0 carry 1 ; adde 0+0+1 = 1
    a.li(6, -1); a.li(7, 1); a.addc(8, 6, 7); a.li(9, 0); a.adde(10, 9, 9); a.stw(10, 20, 31)
    # 24: signed division -7/2 = -3
    a.li(6, -7); a.li(7, 2); a.divw(8, 6, 7); a.stw(8, 24, 31)
    # 28: mulhwu(0xFFFFFFFF, 2) = 1
    a.li(6, -1); a.li(7, 2); a.mulhwu(8, 6, 7); a.stw(8, 28, 31)
    # 32: rlwinm extract top byte of 0x12345678 = 0x12 ; then rlwimi insert
    a.load32(6, 0x12345678); a.rlwinm(7, 6, 8, 24, 31); a.stw(7, 32, 31)
    # 36: fctiwz((1.5*2.25)*2) = 6 ; 40: double 3.375
    a.load_addr(4, "dbl"); a.lfd(1, 0, 4); a.lfd(2, 8, 4); a.fmul(3, 1, 2); a.fadd(4, 3, 3)
    a.fctiwz(5, 4); a.li(6, 36); a.stfiwx(5, 31, 6); a.stfd(3, 40, 31)
    # 48: paired singles (1,2)+(1,2) = (2,4) stored as floats
    a.load_addr(4, "flt"); a.psq_l(3, 0, 4, 0, 0); a.ps_add(4, 3, 3); a.psq_st(4, 48, 31, 0, 0)
    # 56: quantized load u8 (10,20) via GQR1, store as s16 * 2 => (20, 40)
    a.load32(6, 0x00040007); a.mtspr(913, 6)          # GQR1: load u8, store s16, scale 0
    a.load_addr(4, "bytes"); a.psq_l(5, 0, 4, 0, 1); a.ps_add(5, 5, 5); a.psq_st(5, 56, 31, 0, 1)
    # 60: compare + CR logic: (3 < 5) OR (eq) -> mfcr masked
    a.li(6, 3); a.cmpwi(0, 6, 5); a.cmpwi(1, 6, 3); a.cror(31, 0, 6)   # cr7.so = cr0.lt | cr1.eq
    a.mfcr(7); a.andi_(7, 7, 1); a.stw(7, 60, 31)
    # 64: shifts: sraw(-16, 2) = -4, srawi carry
    a.li(6, -16); a.li(7, 2); a.sraw(8, 6, 7); a.stw(8, 64, 31)
    # 68: cntlzw(0x00010000) = 15
    a.load32(6, 0x00010000); a.cntlzw(7, 6); a.stw(7, 68, 31)
    # 72: stmw/lmw round trip
    a.li(29, 0x111); a.li(30, 0x222); a.stmw(29, -12, 1); a.li(29, 0); a.li(30, 0)
    a.lmw(29, -12, 1); a.add(8, 29, 30); a.stw(8, 72, 31)
    a.load32(31, RESULTS)                                 # r31 was clobbered by lmw
    # 76: DVD read through the DI hardware registers
    a.load32(20, 0xCC006000)
    a.load32(6, 0xA8000000); a.stw(6, 0x08, 20)
    a.load32(6, file_offset >> 2); a.stw(6, 0x0C, 20)
    a.li(6, 32); a.stw(6, 0x10, 20)
    a.load32(6, 0x00410000); a.stw(6, 0x14, 20)          # DIMAR (physical)
    a.li(6, 32); a.stw(6, 0x18, 20)
    a.li(6, 1); a.stw(6, 0x1C, 20)                       # DICR: TSTART
    a.load32(4, 0x80410000); a.lwz(6, 0, 4); a.stw(6, 76, 31)   # "Hell"
    # 80: function pointer to a function only referenced from data (cbtab) + blrl
    a.load_addr(4, "cbtab"); a.lwz(12, 0, 4); a.mtlr(12); a.li(3, 20); a.blrl(); a.stw(3, 80, 31)
    # 84: loop with a conditional branch back-edge (interrupt poll point) : count to 1000
    a.li(3, 0)
    a.label("loop2"); a.addi(3, 3, 1); a.cmpwi(0, 3, 1000); a.blt("loop2")
    a.stw(3, 84, 31)
    # OSReport through HLE
    a.load_addr(3, "fmt"); a.li(4, 55); a.load_addr(5, "okstr"); a.load_addr(6, "dbl"); a.lfd(1, 0, 6)
    a.bl("OSReport")
    # done
    a.li(3, 0); a.bl("exit")
    a.label("hang"); a.b("hang")

    # fib(n)
    a.label("fib")
    a.cmpwi(0, 3, 2); a.blt("fib_ret")
    a.mflr(0); a.stw(0, 4, 1); a.stwu(1, -16, 1); a.stw(31, 12, 1); a.stw(30, 8, 1)
    a.mr(31, 3); a.addi(3, 31, -1); a.bl("fib"); a.mr(30, 3); a.addi(3, 31, -2); a.bl("fib")
    a.add(3, 3, 30)
    a.lwz(30, 8, 1); a.lwz(31, 12, 1); a.addi(1, 1, 16); a.lwz(0, 4, 1); a.mtlr(0); a.blr()
    a.label("fib_ret"); a.blr()

    a.label("square"); a.mullw(3, 3, 3); a.blr()

    a.label("sw")
    a.cmplwi(0, 3, 3); a.bgt("sw_def")
    a.load_addr(4, "jtab")
    # reorder to the CodeWarrior pattern: lis, slwi, addi, lwzx
    a.slwi(0, 3, 2); a.lwzx(0, 4, 0); a.mtctr(0); a.bctr()
    a.label("case0"); a.li(3, 100); a.blr()
    a.label("case1"); a.li(3, 200); a.blr()
    a.label("case2"); a.li(3, 300); a.blr()
    a.label("case3"); a.li(3, 400); a.blr()
    a.label("sw_def"); a.li(3, -1); a.blr()

    a.nop()
    a.label("callback"); a.addi(3, 3, 22); a.blr()

    a.label("OSReport"); a.blr()
    a.label("exit"); a.b("exit")

    a.labels.update(d.labels)
    text = a.assemble()
    data = d.assemble()
    symbols = (f"{a.addr('OSReport'):08X} 000004 {a.addr('OSReport'):08X} 0 OSReport\n"
               f"{a.addr('exit'):08X} 000004 {a.addr('exit'):08X} 0 exit\n"
               f"{a.addr('_start'):08X} 000004 {a.addr('_start'):08X} 0 __start\n")
    return build_dol([(TEXT, text)], [(DATA, data)], TEXT), symbols


EXPECTED = {
    0: 55, 4: 5050, 8: 49, 12: 300, 16: 0xFFFFFFFF, 20: 1, 24: 0xFFFFFFFD, 28: 1, 32: 0x12,
    36: 6, 48: 0x40000000, 52: 0x40800000, 56: 0x00140028, 60: 1, 64: 0xFFFFFFFC, 68: 15,
    72: 0x333, 76: 0x48656C6C, 80: 42, 84: 1000,
}
EXPECTED_DOUBLE_AT_40 = 3.375


def build_disc(path: str) -> tuple[str, int]:
    """Write a minimal GameCube ISO. Returns (symbols text, file offset)."""
    file_offset = 0x20000
    dol, symbols = build_program(file_offset)
    dol_offset = 0x10000
    # FST: root + one file "hello.txt"
    names = b"hello.txt\0"
    fst = struct.pack(">III", 0x01000000, 0, 2) + struct.pack(">III", 0x00000000, file_offset, len(FILE_PAYLOAD)) + names
    fst_offset = 0x30000
    size = 0x40000
    img = bytearray(size)
    img[0:6] = b"GTSE01"
    struct.pack_into(">I", img, 0x1C, 0xC2339F3D)
    img[0x20:0x20 + 18] = b"Recomp Test Disc\0\0"
    struct.pack_into(">IIII", img, 0x420, dol_offset, fst_offset, len(fst), len(fst))
    # apploader header (empty)
    img[0x2440:0x2450] = b"2006/05/26\0\0\0\0\0\0"
    img[dol_offset:dol_offset + len(dol)] = dol
    img[file_offset:file_offset + len(FILE_PAYLOAD)] = FILE_PAYLOAD
    img[fst_offset:fst_offset + len(fst)] = fst
    with open(path, "wb") as f:
        f.write(img)
    return symbols, file_offset

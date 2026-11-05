"""A tiny PowerPC assembler used to build synthetic test programs (no devkitPPC needed)."""
from __future__ import annotations

import struct


def _d(op, rt, ra, imm):
    return (op << 26) | (rt << 21) | (ra << 16) | (imm & 0xFFFF)


def _x(op, rt, ra, rb, xo, rc=0):
    return (op << 26) | (rt << 21) | (ra << 16) | (rb << 11) | (xo << 1) | rc


def _a(op, frt, fra, frb, frc, xo, rc=0):
    return (op << 26) | (frt << 21) | (fra << 16) | (frb << 11) | (frc << 6) | (xo << 1) | rc


class Asm:
    def __init__(self, base: int):
        self.base = base
        self.words: list = []          # int or (callable resolving to int)
        self.labels: dict[str, int] = {}

    @property
    def pc(self) -> int:
        return self.base + 4 * len(self.words)

    def label(self, name: str) -> None:
        self.labels[name] = self.pc

    def emit(self, w) -> None:
        self.words.append(w)

    def word(self, v: int) -> None:
        self.emit(v & 0xFFFFFFFF)

    def assemble(self) -> bytes:
        out = []
        for i, w in enumerate(self.words):
            out.append(w(self.base + 4 * i) if callable(w) else w)
        return b"".join(struct.pack(">I", v & 0xFFFFFFFF) for v in out)

    def addr(self, label: str) -> int:
        return self.labels[label]

    # --- integer ---
    def li(self, rt, v): self.emit(_d(14, rt, 0, v))
    def lis(self, rt, v): self.emit(_d(15, rt, 0, v))
    def addi(self, rt, ra, v): self.emit(_d(14, rt, ra, v))
    def addis(self, rt, ra, v): self.emit(_d(15, rt, ra, v))
    def addic(self, rt, ra, v): self.emit(_d(12, rt, ra, v))
    def subfic(self, rt, ra, v): self.emit(_d(8, rt, ra, v))
    def mulli(self, rt, ra, v): self.emit(_d(7, rt, ra, v))
    def ori(self, ra, rs, v): self.emit(_d(24, rs, ra, v))
    def oris(self, ra, rs, v): self.emit(_d(25, rs, ra, v))
    def andi_(self, ra, rs, v): self.emit(_d(28, rs, ra, v))
    def xori(self, ra, rs, v): self.emit(_d(26, rs, ra, v))
    def cmpwi(self, cr, ra, v): self.emit(_d(11, cr << 2, ra, v))
    def cmplwi(self, cr, ra, v): self.emit(_d(10, cr << 2, ra, v))
    def cmpw(self, cr, ra, rb): self.emit(_x(31, cr << 2, ra, rb, 0))
    def cmplw(self, cr, ra, rb): self.emit(_x(31, cr << 2, ra, rb, 32))
    def add(self, rt, ra, rb, rc=0): self.emit(_x(31, rt, ra, rb, 266, rc))
    def addc(self, rt, ra, rb): self.emit(_x(31, rt, ra, rb, 10))
    def adde(self, rt, ra, rb): self.emit(_x(31, rt, ra, rb, 138))
    def addze(self, rt, ra): self.emit(_x(31, rt, ra, 0, 202))
    def subf(self, rt, ra, rb, rc=0): self.emit(_x(31, rt, ra, rb, 40, rc))
    def subfc(self, rt, ra, rb): self.emit(_x(31, rt, ra, rb, 8))
    def subfe(self, rt, ra, rb): self.emit(_x(31, rt, ra, rb, 136))
    def neg(self, rt, ra): self.emit(_x(31, rt, ra, 0, 104))
    def mullw(self, rt, ra, rb): self.emit(_x(31, rt, ra, rb, 235))
    def mulhw(self, rt, ra, rb): self.emit(_x(31, rt, ra, rb, 75))
    def mulhwu(self, rt, ra, rb): self.emit(_x(31, rt, ra, rb, 11))
    def divw(self, rt, ra, rb): self.emit(_x(31, rt, ra, rb, 491))
    def divwu(self, rt, ra, rb): self.emit(_x(31, rt, ra, rb, 459))
    def and_(self, ra, rs, rb, rc=0): self.emit(_x(31, rs, ra, rb, 28, rc))
    def or_(self, ra, rs, rb, rc=0): self.emit(_x(31, rs, ra, rb, 444, rc))
    def xor(self, ra, rs, rb): self.emit(_x(31, rs, ra, rb, 316))
    def nor(self, ra, rs, rb): self.emit(_x(31, rs, ra, rb, 124))
    def andc(self, ra, rs, rb): self.emit(_x(31, rs, ra, rb, 60))
    def mr(self, ra, rs): self.or_(ra, rs, rs)
    def slw(self, ra, rs, rb): self.emit(_x(31, rs, ra, rb, 24))
    def srw(self, ra, rs, rb): self.emit(_x(31, rs, ra, rb, 536))
    def sraw(self, ra, rs, rb): self.emit(_x(31, rs, ra, rb, 792))
    def srawi(self, ra, rs, sh): self.emit(_x(31, rs, ra, sh, 824))
    def cntlzw(self, ra, rs): self.emit(_x(31, rs, ra, 0, 26))
    def extsb(self, ra, rs): self.emit(_x(31, rs, ra, 0, 954))
    def extsh(self, ra, rs): self.emit(_x(31, rs, ra, 0, 922))

    def rlwinm(self, ra, rs, sh, mb, me, rc=0):
        self.emit((21 << 26) | (rs << 21) | (ra << 16) | (sh << 11) | (mb << 6) | (me << 1) | rc)

    def rlwimi(self, ra, rs, sh, mb, me):
        self.emit((20 << 26) | (rs << 21) | (ra << 16) | (sh << 11) | (mb << 6) | (me << 1))

    def slwi(self, ra, rs, n): self.rlwinm(ra, rs, n, 0, 31 - n)
    def srwi(self, ra, rs, n): self.rlwinm(ra, rs, 32 - n, n, 31)

    # --- load/store ---
    def lwz(self, rt, d, ra): self.emit(_d(32, rt, ra, d))
    def lbz(self, rt, d, ra): self.emit(_d(34, rt, ra, d))
    def lhz(self, rt, d, ra): self.emit(_d(40, rt, ra, d))
    def lha(self, rt, d, ra): self.emit(_d(42, rt, ra, d))
    def stw(self, rs, d, ra): self.emit(_d(36, rs, ra, d))
    def stwu(self, rs, d, ra): self.emit(_d(37, rs, ra, d))
    def stb(self, rs, d, ra): self.emit(_d(38, rs, ra, d))
    def sth(self, rs, d, ra): self.emit(_d(44, rs, ra, d))
    def lmw(self, rt, d, ra): self.emit(_d(46, rt, ra, d))
    def stmw(self, rs, d, ra): self.emit(_d(47, rs, ra, d))
    def lwzx(self, rt, ra, rb): self.emit(_x(31, rt, ra, rb, 23))
    def stwx(self, rs, ra, rb): self.emit(_x(31, rs, ra, rb, 151))
    def lwbrx(self, rt, ra, rb): self.emit(_x(31, rt, ra, rb, 534))
    def lfs(self, ft, d, ra): self.emit(_d(48, ft, ra, d))
    def lfd(self, ft, d, ra): self.emit(_d(50, ft, ra, d))
    def stfs(self, fs, d, ra): self.emit(_d(52, fs, ra, d))
    def stfd(self, fs, d, ra): self.emit(_d(54, fs, ra, d))

    def psq_l(self, ft, d, ra, w, i): self.emit((56 << 26) | (ft << 21) | (ra << 16) | (w << 15) | (i << 12) | (d & 0xFFF))
    def psq_st(self, fs, d, ra, w, i): self.emit((60 << 26) | (fs << 21) | (ra << 16) | (w << 15) | (i << 12) | (d & 0xFFF))

    # --- float ---
    def fadd(self, d, a, b): self.emit(_a(63, d, a, b, 0, 21))
    def fsub(self, d, a, b): self.emit(_a(63, d, a, b, 0, 20))
    def fmul(self, d, a, c): self.emit(_a(63, d, a, 0, c, 25))
    def fdiv(self, d, a, b): self.emit(_a(63, d, a, b, 0, 18))
    def fmadd(self, d, a, c, b): self.emit(_a(63, d, a, b, c, 29))
    def fadds(self, d, a, b): self.emit(_a(59, d, a, b, 0, 21))
    def fmuls(self, d, a, c): self.emit(_a(59, d, a, 0, c, 25))
    def fctiwz(self, d, b): self.emit(_x(63, d, 0, b, 15))
    def frsp(self, d, b): self.emit(_x(63, d, 0, b, 12))
    def fcmpu(self, cr, a, b): self.emit(_x(63, cr << 2, a, b, 0))
    def fneg(self, d, b): self.emit(_x(63, d, 0, b, 40))
    def fmr(self, d, b): self.emit(_x(63, d, 0, b, 72))
    def stfiwx(self, fs, ra, rb): self.emit(_x(31, fs, ra, rb, 983))
    def ps_add(self, d, a, b): self.emit(_a(4, d, a, b, 0, 21))
    def ps_mul(self, d, a, c): self.emit(_a(4, d, a, 0, c, 25))
    def ps_madd(self, d, a, c, b): self.emit(_a(4, d, a, b, c, 29))
    def ps_sum0(self, d, a, c, b): self.emit(_a(4, d, a, b, c, 10))
    def ps_merge10(self, d, a, b): self.emit(_x(4, d, a, b, 592))

    # --- spr / cr ---
    def mflr(self, rt): self.emit(_x(31, rt, 8, 0, 339))
    def mtlr(self, rs): self.emit(_x(31, rs, 8, 0, 467))
    def mtctr(self, rs): self.emit(_x(31, rs, 9, 0, 467))
    def mfctr(self, rt): self.emit(_x(31, rt, 9, 0, 339))
    def mtspr(self, spr, rs): self.emit(_x(31, rs, spr & 31, spr >> 5, 467))
    def mfcr(self, rt): self.emit(_x(31, rt, 0, 0, 19))
    def mtcrf(self, crm, rs): self.emit((31 << 26) | (rs << 21) | (crm << 12) | (144 << 1))
    def cror(self, d, a, b): self.emit(_x(19, d, a, b, 449))
    def crxor(self, d, a, b): self.emit(_x(19, d, a, b, 193))
    def mftb(self, rt): self.emit(_x(31, rt, 268 & 31, 268 >> 5, 371))
    def nop(self): self.emit(0x60000000)

    # --- branches ---
    def _rel(self, target, bits_mask, lk, base):
        def f(pc, target=target):
            t = self.labels[target] if isinstance(target, str) else target
            return base | ((t - pc) & bits_mask) | lk
        self.emit(f)

    def b(self, t): self._rel(t, 0x03FFFFFC, 0, 18 << 26)
    def bl(self, t): self._rel(t, 0x03FFFFFC, 1, 18 << 26)
    def bc(self, bo, bi, t, lk=0): self._rel(t, 0xFFFC, lk, (16 << 26) | (bo << 21) | (bi << 16))
    def beq(self, t, cr=0): self.bc(12, cr * 4 + 2, t)
    def bne(self, t, cr=0): self.bc(4, cr * 4 + 2, t)
    def blt(self, t, cr=0): self.bc(12, cr * 4 + 0, t)
    def bge(self, t, cr=0): self.bc(4, cr * 4 + 0, t)
    def bgt(self, t, cr=0): self.bc(12, cr * 4 + 1, t)
    def ble(self, t, cr=0): self.bc(4, cr * 4 + 1, t)
    def bdnz(self, t): self.bc(16, 0, t)
    def blr(self): self.emit(0x4E800020)
    def bctr(self): self.emit(0x4E800420)
    def bctrl(self): self.emit(0x4E800421)
    def blrl(self): self.emit(0x4E800021)
    def beqlr(self, cr=0): self.emit((19 << 26) | (12 << 21) | ((cr * 4 + 2) << 16) | (16 << 1))

    def load32(self, rt, v):
        hi, lo = (v >> 16) & 0xFFFF, v & 0xFFFF
        self.lis(rt, hi)
        self.ori(rt, rt, lo)

    def load_addr(self, rt, label):
        """lis/addi pair (addi with @ha adjustment) resolved after layout."""
        def hi(pc):
            v = self.labels[label]
            return _d(15, rt, 0, ((v + 0x8000) >> 16) & 0xFFFF)

        def lo(pc):
            return _d(14, rt, rt, self.labels[label] & 0xFFFF)
        self.emit(hi)
        self.emit(lo)

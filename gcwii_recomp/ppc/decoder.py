"""Decoder for the Gekko (GameCube) / Broadway (Wii) PowerPC instruction set.

Covers the 32-bit PowerPC user and supervisor instructions implemented by the
750CL core plus the Gekko paired-single extensions (opcode 4, psq_* loads and
stores).  Decoding is table driven and returns an :class:`Insn` whose
``name`` is the canonical mnemonic used by the code generator.
"""
from __future__ import annotations

from dataclasses import dataclass


def _sext(v: int, bits: int) -> int:
    m = 1 << (bits - 1)
    return (v & (m - 1)) - (v & m)


# --- opcode tables ---------------------------------------------------------
PRIMARY = {
    3: "twi", 7: "mulli", 8: "subfic", 10: "cmpli", 11: "cmpi", 12: "addic", 13: "addic.",
    14: "addi", 15: "addis", 16: "bc", 17: "sc", 18: "b", 20: "rlwimi", 21: "rlwinm",
    23: "rlwnm", 24: "ori", 25: "oris", 26: "xori", 27: "xoris", 28: "andi.", 29: "andis.",
    32: "lwz", 33: "lwzu", 34: "lbz", 35: "lbzu", 36: "stw", 37: "stwu", 38: "stb", 39: "stbu",
    40: "lhz", 41: "lhzu", 42: "lha", 43: "lhau", 44: "sth", 45: "sthu", 46: "lmw", 47: "stmw",
    48: "lfs", 49: "lfsu", 50: "lfd", 51: "lfdu", 52: "stfs", 53: "stfsu", 54: "stfd", 55: "stfdu",
    56: "psq_l", 57: "psq_lu", 60: "psq_st", 61: "psq_stu",
}

OP19 = {
    0: "mcrf", 16: "bclr", 33: "crnor", 50: "rfi", 129: "crandc", 150: "isync", 193: "crxor",
    225: "crnand", 257: "crand", 289: "creqv", 417: "crorc", 449: "cror", 528: "bcctr",
}

OP31 = {
    0: "cmp", 4: "tw", 19: "mfcr", 20: "lwarx", 23: "lwzx", 24: "slw", 26: "cntlzw", 28: "and",
    32: "cmpl", 54: "dcbst", 55: "lwzux", 60: "andc", 83: "mfmsr", 86: "dcbf", 87: "lbzx",
    119: "lbzux", 124: "nor", 144: "mtcrf", 146: "mtmsr", 150: "stwcx.", 151: "stwx",
    183: "stwux", 210: "mtsr", 215: "stbx", 242: "mtsrin", 246: "dcbtst", 247: "stbux",
    278: "dcbt", 279: "lhzx", 284: "eqv", 306: "tlbie", 310: "eciwx", 311: "lhzux", 316: "xor",
    339: "mfspr", 343: "lhax", 371: "mftb", 375: "lhaux", 407: "sthx", 412: "orc", 438: "ecowx",
    439: "sthux", 444: "or", 467: "mtspr", 470: "dcbi", 476: "nand", 512: "mcrxr", 533: "lswx",
    534: "lwbrx", 535: "lfsx", 536: "srw", 566: "tlbsync", 567: "lfsux", 595: "mfsr", 597: "lswi",
    598: "sync", 599: "lfdx", 631: "lfdux", 659: "mfsrin", 661: "stswx", 662: "stwbrx",
    663: "stfsx", 695: "stfsux", 725: "stswi", 727: "stfdx", 759: "stfdux", 790: "lhbrx",
    792: "sraw", 824: "srawi", 854: "eieio", 918: "sthbrx", 922: "extsh", 954: "extsb",
    982: "icbi", 983: "stfiwx", 1014: "dcbz",
}

# XO-form (9-bit xo with OE bit) arithmetic.
OP31_XO = {
    8: "subfc", 10: "addc", 11: "mulhwu", 40: "subf", 75: "mulhw", 104: "neg", 136: "subfe",
    138: "adde", 200: "subfze", 202: "addze", 232: "subfme", 234: "addme", 235: "mullw",
    266: "add", 459: "divwu", 491: "divw",
}

OP59 = {18: "fdivs", 20: "fsubs", 21: "fadds", 24: "fres", 25: "fmuls", 28: "fmsubs",
        29: "fmadds", 30: "fnmsubs", 31: "fnmadds"}

OP63_A = {18: "fdiv", 20: "fsub", 21: "fadd", 23: "fsel", 25: "fmul", 26: "frsqrte",
          28: "fmsub", 29: "fmadd", 30: "fnmsub", 31: "fnmadd"}
OP63_X = {0: "fcmpu", 12: "frsp", 14: "fctiw", 15: "fctiwz", 32: "fcmpo", 38: "mtfsb1",
          40: "fneg", 64: "mcrfs", 70: "mtfsb0", 72: "fmr", 134: "mtfsfi", 136: "fnabs",
          264: "fabs", 583: "mffs", 711: "mtfsf"}

OP4_X = {0: "ps_cmpu0", 32: "ps_cmpo0", 40: "ps_neg", 64: "ps_cmpu1", 72: "ps_mr", 96: "ps_cmpo1",
         136: "ps_nabs", 264: "ps_abs", 528: "ps_merge00", 560: "ps_merge01", 592: "ps_merge10",
         624: "ps_merge11", 1014: "dcbz_l"}
OP4_A = {10: "ps_sum0", 11: "ps_sum1", 12: "ps_muls0", 13: "ps_muls1", 14: "ps_madds0",
         15: "ps_madds1", 18: "ps_div", 20: "ps_sub", 21: "ps_add", 23: "ps_sel", 24: "ps_res",
         25: "ps_mul", 26: "ps_rsqrte", 28: "ps_msub", 29: "ps_madd", 30: "ps_nmsub", 31: "ps_nmadd"}
OP4_M = {6: "psq_lx", 7: "psq_stx", 38: "psq_lux", 39: "psq_stux"}

SPR_NAMES = {1: "xer", 8: "lr", 9: "ctr", 18: "dsisr", 19: "dar", 22: "dec", 25: "sdr1",
             26: "srr0", 27: "srr1", 268: "tbl", 269: "tbu", 272: "sprg0", 273: "sprg1",
             274: "sprg2", 275: "sprg3", 282: "ear", 284: "tbl_w", 285: "tbu_w", 287: "pvr",
             912: "gqr0", 913: "gqr1", 914: "gqr2", 915: "gqr3", 916: "gqr4", 917: "gqr5",
             918: "gqr6", 919: "gqr7", 920: "hid2", 921: "wpar", 922: "dma_u", 923: "dma_l",
             1008: "hid0", 1009: "hid1", 1017: "l2cr", 1019: "ictc", 1020: "thrm1",
             1021: "thrm2", 1022: "thrm3", 936: "ummcr0", 952: "mmcr0", 953: "pmc1",
             954: "pmc2", 956: "mmcr1", 957: "pmc3", 958: "pmc4"}


@dataclass(frozen=True)
class Insn:
    addr: int
    word: int
    name: str

    # -- raw fields ------------------------------------------------------
    @property
    def opcd(self) -> int: return self.word >> 26
    @property
    def rd(self) -> int: return (self.word >> 21) & 31
    rs = rd
    frd = rd
    frs = rd
    @property
    def ra(self) -> int: return (self.word >> 16) & 31
    @property
    def rb(self) -> int: return (self.word >> 11) & 31
    @property
    def rc_reg(self) -> int: return (self.word >> 6) & 31
    @property
    def rc(self) -> bool: return bool(self.word & 1)
    @property
    def oe(self) -> bool: return bool((self.word >> 10) & 1)
    @property
    def simm(self) -> int: return _sext(self.word & 0xFFFF, 16)
    @property
    def uimm(self) -> int: return self.word & 0xFFFF
    @property
    def sh(self) -> int: return (self.word >> 11) & 31
    @property
    def mb(self) -> int: return (self.word >> 6) & 31
    @property
    def me(self) -> int: return (self.word >> 1) & 31
    @property
    def bo(self) -> int: return (self.word >> 21) & 31
    @property
    def bi(self) -> int: return (self.word >> 16) & 31
    @property
    def lk(self) -> bool: return bool(self.word & 1)
    @property
    def aa(self) -> bool: return bool((self.word >> 1) & 1)
    @property
    def crfd(self) -> int: return (self.word >> 23) & 7
    @property
    def crfs(self) -> int: return (self.word >> 18) & 7
    @property
    def crbd(self) -> int: return (self.word >> 21) & 31
    @property
    def crba(self) -> int: return (self.word >> 16) & 31
    @property
    def crbb(self) -> int: return (self.word >> 11) & 31
    @property
    def l_bit(self) -> int: return (self.word >> 21) & 1
    @property
    def spr(self) -> int: return ((self.word >> 16) & 31) | (((self.word >> 11) & 31) << 5)
    @property
    def crm(self) -> int: return (self.word >> 12) & 0xFF
    @property
    def fm(self) -> int: return (self.word >> 17) & 0xFF
    @property
    def nb(self) -> int: return (self.word >> 11) & 31
    @property
    def to(self) -> int: return (self.word >> 21) & 31
    # paired-single quantized load/store
    @property
    def ps_w(self) -> int:
        return (self.word >> 10) & 1 if self.opcd == 4 else (self.word >> 15) & 1
    @property
    def ps_i(self) -> int:
        return (self.word >> 7) & 7 if self.opcd == 4 else (self.word >> 12) & 7
    @property
    def ps_d(self) -> int: return _sext(self.word & 0xFFF, 12)

    # -- branch helpers ---------------------------------------------------
    @property
    def is_branch(self) -> bool:
        return self.name in ("b", "bc", "bclr", "bcctr")

    @property
    def branch_target(self) -> int | None:
        if self.name == "b":
            off = _sext(self.word & 0x03FFFFFC, 26)
            return (off if self.aa else self.addr + off) & 0xFFFFFFFF
        if self.name == "bc":
            off = _sext(self.word & 0xFFFC, 16)
            return (off if self.aa else self.addr + off) & 0xFFFFFFFF
        return None

    @property
    def is_unconditional(self) -> bool:
        if self.name == "b":
            return True
        if self.name in ("bc", "bclr", "bcctr"):
            return (self.bo & 0x14) == 0x14
        return False

    def __str__(self) -> str:
        return disasm(self)


def decode(word: int, addr: int = 0) -> Insn:
    op = word >> 26
    name = PRIMARY.get(op)
    if name is None:
        xo10 = (word >> 1) & 0x3FF
        xo5 = (word >> 1) & 0x1F
        if op == 19:
            name = OP19.get(xo10)
        elif op == 31:
            name = OP31.get(xo10)
            if name is None:
                name = OP31_XO.get(xo10 & 0x1FF)
                if name is not None and (xo10 & 0x200) and name in ("mulhw", "mulhwu"):
                    name = None
        elif op == 59:
            name = OP59.get(xo5)
        elif op == 63:
            name = OP63_A.get(xo5) or OP63_X.get(xo10)
        elif op == 4:
            name = OP4_X.get(xo10) or OP4_M.get((word >> 1) & 0x3F) or OP4_A.get(xo5)
    if word == 0:
        name = None
    return Insn(addr, word, name or "illegal")


# --- disassembly (for comments / GUI) ---------------------------------------
_LOADSTORE_D = {"lwz", "lwzu", "lbz", "lbzu", "stw", "stwu", "stb", "stbu", "lhz", "lhzu",
                "lha", "lhau", "sth", "sthu", "lmw", "stmw"}
_FLOAD_D = {"lfs", "lfsu", "lfd", "lfdu", "stfs", "stfsu", "stfd", "stfdu"}


def disasm(i: Insn) -> str:
    n = i.name
    rc = "." if i.rc and not n.endswith(".") else ""
    if n == "illegal":
        return f".word 0x{i.word:08X}"
    if n == "b":
        return f"b{'l' if i.lk else ''}{'a' if i.aa else ''} 0x{i.branch_target:08X}"
    if n == "bc":
        return f"bc{'l' if i.lk else ''} {i.bo},{i.bi},0x{i.branch_target:08X}"
    if n in ("bclr", "bcctr"):
        if i.is_unconditional:
            return ("blr" if n == "bclr" else "bctr") + ("l" if i.lk else "")
        return f"{n}{'l' if i.lk else ''} {i.bo},{i.bi}"
    if n in _LOADSTORE_D:
        return f"{n} r{i.rd}, {i.simm}(r{i.ra})"
    if n in _FLOAD_D:
        return f"{n} f{i.rd}, {i.simm}(r{i.ra})"
    if n in ("psq_l", "psq_lu", "psq_st", "psq_stu"):
        return f"{n} f{i.rd}, {i.ps_d}(r{i.ra}), {i.ps_w}, qr{i.ps_i}"
    if n in ("addi", "addis", "addic", "addic.", "subfic", "mulli"):
        if n == "addi" and i.ra == 0:
            return f"li r{i.rd}, {i.simm}"
        if n == "addis" and i.ra == 0:
            return f"lis r{i.rd}, 0x{i.uimm:04X}"
        return f"{n} r{i.rd}, r{i.ra}, {i.simm}"
    if n in ("ori", "oris", "xori", "xoris", "andi.", "andis."):
        if n == "ori" and i.word == 0x60000000:
            return "nop"
        return f"{n} r{i.ra}, r{i.rs}, 0x{i.uimm:X}"
    if n in ("cmpi", "cmpli"):
        return f"{'cmpwi' if n == 'cmpi' else 'cmplwi'} cr{i.crfd}, r{i.ra}, {i.simm if n == 'cmpi' else i.uimm}"
    if n in ("cmp", "cmpl"):
        return f"{'cmpw' if n == 'cmp' else 'cmplw'} cr{i.crfd}, r{i.ra}, r{i.rb}"
    if n in ("rlwinm", "rlwimi"):
        return f"{n}{rc} r{i.ra}, r{i.rs}, {i.sh}, {i.mb}, {i.me}"
    if n == "rlwnm":
        return f"{n}{rc} r{i.ra}, r{i.rs}, r{i.rb}, {i.mb}, {i.me}"
    if n in ("mfspr", "mtspr"):
        spr = SPR_NAMES.get(i.spr, str(i.spr))
        return f"{n} {'r%d, %s' % (i.rd, spr) if n == 'mfspr' else '%s, r%d' % (spr, i.rd)}"
    if n in OP31_XO.values():
        o = "o" if i.oe else ""
        if n in ("neg", "addze", "addme", "subfze", "subfme"):
            return f"{n}{o}{rc} r{i.rd}, r{i.ra}"
        return f"{n}{o}{rc} r{i.rd}, r{i.ra}, r{i.rb}"
    if n in ("and", "or", "xor", "nand", "nor", "eqv", "andc", "orc", "slw", "srw", "sraw"):
        if n == "or" and i.rs == i.rb:
            return f"mr{rc} r{i.ra}, r{i.rs}"
        return f"{n}{rc} r{i.ra}, r{i.rs}, r{i.rb}"
    if n == "srawi":
        return f"srawi{rc} r{i.ra}, r{i.rs}, {i.sh}"
    if n in ("extsb", "extsh", "cntlzw"):
        return f"{n}{rc} r{i.ra}, r{i.rs}"
    if n.endswith("x") and (n.startswith("l") or n.startswith("st")) and n not in ("stfiwx",):
        reg = "f" if n.startswith(("lf", "stf")) else "r"
        return f"{n} {reg}{i.rd}, r{i.ra}, r{i.rb}"
    if n.startswith(("f", "ps_")):
        regs = [f"f{i.frd}"]
        if n in ("fmr", "fneg", "fabs", "fnabs", "frsp", "fctiw", "fctiwz", "fres", "frsqrte",
                 "ps_mr", "ps_neg", "ps_abs", "ps_nabs", "ps_res", "ps_rsqrte"):
            regs.append(f"f{i.rb}")
        elif n in ("fmul", "fmuls", "ps_mul", "ps_muls0", "ps_muls1"):
            regs += [f"f{i.ra}", f"f{i.rc_reg}"]
        elif "madd" in n or "msub" in n or n in ("fsel", "ps_sel", "ps_sum0", "ps_sum1"):
            regs += [f"f{i.ra}", f"f{i.rc_reg}", f"f{i.rb}"]
        elif n.startswith(("fcmp", "ps_cmp")):
            return f"{n} cr{i.crfd}, f{i.ra}, f{i.rb}"
        else:
            regs += [f"f{i.ra}", f"f{i.rb}"]
        return f"{n}{rc} " + ", ".join(regs)
    return n

"""PowerPC (Gekko/Broadway) -> C translator.

Each discovered guest function becomes one C function ``void name(PPCContext* ctx)``.
Intra-function branches become ``goto``; ``bl`` becomes a direct C call;
indirect branches go through ``gcrt_call`` (a lookup in the generated function
table).  Recognised jump tables become ``switch`` statements.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .analysis import AnalysisResult, Function
from .ppc import Insn

# ---------------------------------------------------------------------------
# helpers producing C expressions
# ---------------------------------------------------------------------------


def R(n: int) -> str:
    return f"ctx->r[{n}]"


def RA0(n: int) -> str:
    return "0u" if n == 0 else R(n)


def F(n: int) -> str:
    return f"ctx->f[{n}]"


def P1(n: int) -> str:
    return f"ctx->ps1[{n}]"


def U(v: int) -> str:
    return f"0x{v & 0xFFFFFFFF:X}u"


def ppc_mask(mb: int, me: int) -> int:
    a = 0xFFFFFFFF >> mb
    b = 0xFFFFFFFF if me == 31 else (~(0xFFFFFFFF >> (me + 1))) & 0xFFFFFFFF
    return (a & b) if mb <= me else (a | b)


class Unsupported(Exception):
    pass


@dataclass
class CodegenOptions:
    interrupt_checks: bool = True      # poll for interrupts on loop back-edges
    comments: bool = True              # emit disassembly comments
    save_context_addr: int = 0         # OSSaveContext (thread support)
    hle: dict[int, str] = field(default_factory=dict)   # guest addr -> native C function


class FunctionEmitter:
    def __init__(self, res: AnalysisResult, fn: Function, opts: CodegenOptions, names: dict[int, str]):
        self.res = res
        self.fn = fn
        self.opts = opts
        self.names = names
        self.out: list[str] = []
        self.unknown: set[str] = set()

    # -- small emit helpers ---------------------------------------------
    def e(self, line: str) -> None:
        self.out.append("    " + line)

    def call_expr(self, target: int) -> str:
        if target in self.names:
            return f"{self.names[target]}(ctx);"
        return f"gcrt_call(ctx, {U(target)});"

    def record(self, reg_expr: str, insn: Insn) -> None:
        if insn.rc:
            self.e(f"PPC_CR0(ctx, {reg_expr});")

    def cond_expr(self, i: Insn) -> str | None:
        """Return C condition for a bc/bclr/bcctr (and emit the CTR decrement)."""
        bo, bi = i.bo, i.bi
        parts = []
        if not bo & 4:
            self.e("ctx->ctr -= 1u;")
            parts.append("ctx->ctr == 0u" if bo & 2 else "ctx->ctr != 0u")
        if not bo & 16:
            want = 1 if bo & 8 else 0
            parts.append(f"ppc_crbit(ctx, {bi})" if want else f"!ppc_crbit(ctx, {bi})")
        return " && ".join(f"({p})" for p in parts) if parts else None

    def back_edge_poll(self, src: int, target: int) -> str:
        return " GCRT_CHECK_INTERRUPTS(ctx);" if self.opts.interrupt_checks and target <= src else ""

    # -- main ---------------------------------------------------------------
    def emit(self) -> list[str]:
        fn = self.fn
        self.out = [f"void {fn.name}(PPCContext* ctx) {{", f"    ctx->cur_func = {U(fn.start)};"]
        addr = fn.start
        last: Insn | None = None
        while addr < fn.end:
            i = self.res.insns.get(addr)
            if i is None:
                break
            if addr in fn.labels:
                self.out.append(f"L_{addr:08X}:;")
            if self.opts.comments:
                self.out.append(f"    /* {addr:08X} {i.word:08X}  {i} */")
            self.insn(i)
            last = i
            addr += 4
        # Fall-through into the following function (no terminating branch).
        if last is None or not self._terminates(last):
            self.e(f"{self.call_expr(fn.end)} return;")
        self.out.append("}")
        return self.out

    @staticmethod
    def _terminates(i: Insn) -> bool:
        if i.name == "illegal" or i.name == "rfi":
            return True
        if i.name == "b":
            return not i.lk
        if i.name in ("bc", "bclr", "bcctr"):
            return i.is_unconditional and not i.lk
        return False

    # -- per-instruction --------------------------------------------------
    def insn(self, i: Insn) -> None:
        n = i.name
        h = getattr(self, "op_" + n.replace(".", "_dot"), None)
        if h is None:
            h = _GENERIC.get(n)
            if h is None:
                self.unknown.add(n)
                self.e(f"gcrt_illegal(ctx, {U(i.addr)}, {U(i.word)});")
                return
            h(self, i)
            return
        h(i)

    # ---- branches ----
    def op_b(self, i: Insn) -> None:
        t = i.branch_target
        if i.lk:
            self.e(f"ctx->lr = {U(i.addr + 4)};")
            if t == i.addr + 4:
                return
            self.e(self._call_with_context_save(t))
            return
        if t in self.fn.labels and self.fn.start <= t < self.fn.end:
            self.e(f"{{{self.back_edge_poll(i.addr, t)} goto L_{t:08X}; }}")
        else:
            self.e(f"{self.call_expr(t)} return;")

    def _call_with_context_save(self, t: int) -> str:
        call = self.call_expr(t)
        if self.opts.save_context_addr and t == self.opts.save_context_addr:
            return ("{ uint32_t _c = ctx->r[3]; " + call +
                    " if (setjmp(*gcrt_context_resume_slot(_c))) gcrt_context_resumed(ctx); }")
        return call

    def op_bc(self, i: Insn) -> None:
        t = i.branch_target
        cond = self.cond_expr(i)
        if i.lk:
            body = f"ctx->lr = {U(i.addr + 4)}; " + ("" if t == i.addr + 4 else self.call_expr(t))
            self.e(f"if ({cond}) {{ {body} }}" if cond else body)
            return
        if t in self.fn.labels and self.fn.start <= t < self.fn.end:
            body = f"{self.back_edge_poll(i.addr, t)} goto L_{t:08X};"
        else:
            body = f"{self.call_expr(t)} return;"
        self.e(f"if ({cond}) {{{body} }}" if cond else f"{{{body} }}")

    def op_bclr(self, i: Insn) -> None:
        cond = self.cond_expr(i)
        if i.lk:
            body = f"{{ uint32_t _t = ctx->lr; ctx->lr = {U(i.addr + 4)}; gcrt_call(ctx, _t); }}"
        else:
            body = "return;"
        self.e(f"if ({cond}) {body}" if cond else body)

    def op_bcctr(self, i: Insn) -> None:
        cond = self.cond_expr(i)
        if i.lk:
            body = f"{{ ctx->lr = {U(i.addr + 4)}; gcrt_call(ctx, ctx->ctr); }}"
        elif i.addr in self.fn.jump_tables:
            cases = " ".join(f"case {U(t)}: goto L_{t:08X};" for t in sorted(set(self.fn.jump_tables[i.addr])))
            body = f"switch (ctx->ctr) {{ {cases} default: gcrt_call(ctx, ctx->ctr); return; }}"
        else:
            body = "{ gcrt_call(ctx, ctx->ctr); return; }"
        self.e(f"if ({cond}) {body}" if cond else body)

    def op_sc(self, i: Insn) -> None:
        self.e(f"gcrt_syscall(ctx, {U(i.addr)});")

    def op_rfi(self, i: Insn) -> None:
        self.e(f"gcrt_rfi(ctx, {U(i.addr)});")

    def op_illegal(self, i: Insn) -> None:
        self.e(f"gcrt_illegal(ctx, {U(i.addr)}, {U(i.word)});")

    def op_tw(self, i: Insn) -> None:
        self.e(f"if (ppc_trap_cond({i.to}, {R(i.ra)}, {R(i.rb)})) gcrt_trap(ctx, {U(i.addr)});")

    def op_twi(self, i: Insn) -> None:
        self.e(f"if (ppc_trap_cond({i.to}, {R(i.ra)}, {U(i.simm)})) gcrt_trap(ctx, {U(i.addr)});")

    # ---- integer arithmetic ----
    def op_addi(self, i):
        self.e(f"{R(i.rd)} = {RA0(i.ra)} + {U(i.simm)};")

    def op_addis(self, i):
        self.e(f"{R(i.rd)} = {RA0(i.ra)} + {U(i.uimm << 16)};")

    def op_addic(self, i, rec=False):
        self.e(f"{{ uint32_t _a = {R(i.ra)}; uint32_t _r = _a + {U(i.simm)}; ctx->xer_ca = _r < _a; {R(i.rd)} = _r; }}")
        if rec:
            self.e(f"PPC_CR0(ctx, {R(i.rd)});")

    def op_addic_dot(self, i):
        self.op_addic(i, True)

    def op_subfic(self, i):
        self.e(f"{{ uint64_t _t = (uint64_t)(uint32_t)~{R(i.ra)} + {U(i.simm)} + 1u; "
               f"ctx->xer_ca = (uint32_t)(_t >> 32); {R(i.rd)} = (uint32_t)_t; }}")

    def op_mulli(self, i):
        self.e(f"{R(i.rd)} = (uint32_t)((int32_t){R(i.ra)} * {i.simm});")

    def _addlike(self, i, x: str, y: str, c: str, carry: bool):
        # r = x + y + c ; x/y are C expressions (uint32), c is "0u"/"1u"/"ctx->xer_ca"
        oe = i.oe
        self.e(f"{{ uint32_t _x = {x}, _y = {y}; uint64_t _t = (uint64_t)_x + _y + {c}; uint32_t _r = (uint32_t)_t;"
               + (" ctx->xer_ca = (uint32_t)(_t >> 32);" if carry else "")
               + (" ppc_set_ov(ctx, ((_x ^ _r) & (_y ^ _r)) >> 31);" if oe else "")
               + f" {R(i.rd)} = _r; }}")
        self.record(R(i.rd), i)

    def op_add(self, i):
        if not i.oe:
            self.e(f"{R(i.rd)} = {R(i.ra)} + {R(i.rb)};")
            self.record(R(i.rd), i)
        else:
            self._addlike(i, R(i.ra), R(i.rb), "0u", False)

    def op_addc(self, i): self._addlike(i, R(i.ra), R(i.rb), "0u", True)
    def op_adde(self, i): self._addlike(i, R(i.ra), R(i.rb), "ctx->xer_ca", True)
    def op_addze(self, i): self._addlike(i, R(i.ra), "0u", "ctx->xer_ca", True)
    def op_addme(self, i): self._addlike(i, R(i.ra), "0xFFFFFFFFu", "ctx->xer_ca", True)

    def op_subf(self, i):
        if not i.oe:
            self.e(f"{R(i.rd)} = {R(i.rb)} - {R(i.ra)};")
            self.record(R(i.rd), i)
        else:
            self._addlike(i, f"~{R(i.ra)}", R(i.rb), "1u", False)

    def op_subfc(self, i): self._addlike(i, f"~{R(i.ra)}", R(i.rb), "1u", True)
    def op_subfe(self, i): self._addlike(i, f"~{R(i.ra)}", R(i.rb), "ctx->xer_ca", True)
    def op_subfze(self, i): self._addlike(i, f"~{R(i.ra)}", "0u", "ctx->xer_ca", True)
    def op_subfme(self, i): self._addlike(i, f"~{R(i.ra)}", "0xFFFFFFFFu", "ctx->xer_ca", True)

    def op_neg(self, i):
        if i.oe:
            self.e(f"ppc_set_ov(ctx, {R(i.ra)} == 0x80000000u);")
        self.e(f"{R(i.rd)} = 0u - {R(i.ra)};")
        self.record(R(i.rd), i)

    def op_mullw(self, i):
        if i.oe:
            self.e(f"{{ int64_t _p = (int64_t)(int32_t){R(i.ra)} * (int32_t){R(i.rb)}; "
                   f"ppc_set_ov(ctx, _p != (int32_t)_p); {R(i.rd)} = (uint32_t)_p; }}")
        else:
            self.e(f"{R(i.rd)} = (uint32_t)((int64_t)(int32_t){R(i.ra)} * (int32_t){R(i.rb)});")
        self.record(R(i.rd), i)

    def op_mulhw(self, i):
        self.e(f"{R(i.rd)} = (uint32_t)(((int64_t)(int32_t){R(i.ra)} * (int32_t){R(i.rb)}) >> 32);")
        self.record(R(i.rd), i)

    def op_mulhwu(self, i):
        self.e(f"{R(i.rd)} = (uint32_t)(((uint64_t){R(i.ra)} * {R(i.rb)}) >> 32);")
        self.record(R(i.rd), i)

    def op_divw(self, i):
        self.e(f"{R(i.rd)} = ppc_divw(ctx, {R(i.ra)}, {R(i.rb)}, {int(i.oe)});")
        self.record(R(i.rd), i)

    def op_divwu(self, i):
        self.e(f"{R(i.rd)} = ppc_divwu(ctx, {R(i.ra)}, {R(i.rb)}, {int(i.oe)});")
        self.record(R(i.rd), i)

    # ---- compare ----
    def op_cmp(self, i): self.e(f"ppc_cmp_s(ctx, {i.crfd}, (int32_t){R(i.ra)}, (int32_t){R(i.rb)});")
    def op_cmpl(self, i): self.e(f"ppc_cmp_u(ctx, {i.crfd}, {R(i.ra)}, {R(i.rb)});")
    def op_cmpi(self, i): self.e(f"ppc_cmp_s(ctx, {i.crfd}, (int32_t){R(i.ra)}, {i.simm});")
    def op_cmpli(self, i): self.e(f"ppc_cmp_u(ctx, {i.crfd}, {R(i.ra)}, {U(i.uimm)});")

    # ---- logical ----
    def _logic(self, i, expr):
        self.e(f"{R(i.ra)} = {expr};")
        self.record(R(i.ra), i)

    def op_and(self, i): self._logic(i, f"{R(i.rs)} & {R(i.rb)}")
    def op_andc(self, i): self._logic(i, f"{R(i.rs)} & ~{R(i.rb)}")
    def op_or(self, i): self._logic(i, R(i.rs) if i.rs == i.rb else f"{R(i.rs)} | {R(i.rb)}")
    def op_orc(self, i): self._logic(i, f"{R(i.rs)} | ~{R(i.rb)}")
    def op_xor(self, i): self._logic(i, f"{R(i.rs)} ^ {R(i.rb)}")
    def op_nand(self, i): self._logic(i, f"~({R(i.rs)} & {R(i.rb)})")
    def op_nor(self, i): self._logic(i, f"~({R(i.rs)} | {R(i.rb)})")
    def op_eqv(self, i): self._logic(i, f"~({R(i.rs)} ^ {R(i.rb)})")
    def op_extsb(self, i): self._logic(i, f"(uint32_t)(int32_t)(int8_t){R(i.rs)}")
    def op_extsh(self, i): self._logic(i, f"(uint32_t)(int32_t)(int16_t){R(i.rs)}")
    def op_cntlzw(self, i): self._logic(i, f"ppc_cntlzw({R(i.rs)})")

    def op_ori(self, i):
        if i.word == 0x60000000:
            return
        self.e(f"{R(i.ra)} = {R(i.rs)} | {U(i.uimm)};")

    def op_oris(self, i): self.e(f"{R(i.ra)} = {R(i.rs)} | {U(i.uimm << 16)};")
    def op_xori(self, i): self.e(f"{R(i.ra)} = {R(i.rs)} ^ {U(i.uimm)};")
    def op_xoris(self, i): self.e(f"{R(i.ra)} = {R(i.rs)} ^ {U(i.uimm << 16)};")

    def op_andi_dot(self, i):
        self.e(f"{R(i.ra)} = {R(i.rs)} & {U(i.uimm)}; PPC_CR0(ctx, {R(i.ra)});")

    def op_andis_dot(self, i):
        self.e(f"{R(i.ra)} = {R(i.rs)} & {U(i.uimm << 16)}; PPC_CR0(ctx, {R(i.ra)});")

    # ---- shifts / rotates ----
    def op_slw(self, i):
        self._logic(i, f"(({R(i.rb)} & 0x20u) ? 0u : ({R(i.rs)} << ({R(i.rb)} & 31u)))")

    def op_srw(self, i):
        self._logic(i, f"(({R(i.rb)} & 0x20u) ? 0u : ({R(i.rs)} >> ({R(i.rb)} & 31u)))")

    def op_sraw(self, i):
        self._logic(i, f"ppc_sraw(ctx, {R(i.rs)}, {R(i.rb)})")

    def op_srawi(self, i):
        sh = i.sh
        if sh == 0:
            self.e(f"ctx->xer_ca = 0u; {R(i.ra)} = {R(i.rs)};")
        else:
            self.e(f"{{ uint32_t _s = {R(i.rs)}; ctx->xer_ca = ((int32_t)_s < 0) && (_s & {U((1 << sh) - 1)}); "
                   f"{R(i.ra)} = (uint32_t)((int32_t)_s >> {sh}); }}")
        self.record(R(i.ra), i)

    def _rot(self, src: str, sh) -> str:
        if isinstance(sh, int):
            return src if sh == 0 else f"ppc_rotl({src}, {sh})"
        return f"ppc_rotl({src}, {sh})"

    def op_rlwinm(self, i):
        m = ppc_mask(i.mb, i.me)
        sh = i.sh
        src = R(i.rs)
        # common idioms produce simpler C
        if i.mb == 0 and i.me == 31 - sh:
            expr = f"({src} << {sh})" if sh else src
        elif sh and i.me == 31 and i.mb == 32 - sh:
            expr = f"({src} >> {32 - sh})"
        else:
            expr = f"({self._rot(src, sh)} & {U(m)})"
        self._logic(i, expr)

    def op_rlwimi(self, i):
        m = ppc_mask(i.mb, i.me)
        self._logic(i, f"(({self._rot(R(i.rs), i.sh)} & {U(m)}) | ({R(i.ra)} & {U(~m)}))")

    def op_rlwnm(self, i):
        m = ppc_mask(i.mb, i.me)
        self._logic(i, f"({self._rot(R(i.rs), R(i.rb))} & {U(m)})")

    # ---- loads / stores ----
    def _ea_d(self, i) -> str:
        return f"{RA0(i.ra)} + {U(i.simm)}" if i.simm else RA0(i.ra)

    def _ea_x(self, i) -> str:
        return f"{RA0(i.ra)} + {R(i.rb)}"

    def _load(self, i, fn: str, cast: str = "", update=False, indexed=False):
        ea = self._ea_x(i) if indexed else self._ea_d(i)
        if update:
            self.e(f"{{ uint32_t _ea = {ea}; {R(i.rd)} = {cast}{fn}(_ea); {R(i.ra)} = _ea; }}")
        else:
            self.e(f"{R(i.rd)} = {cast}{fn}({ea});")

    def _store(self, i, fn: str, val: str, update=False, indexed=False):
        ea = self._ea_x(i) if indexed else self._ea_d(i)
        if update:
            self.e(f"{{ uint32_t _ea = {ea}; {fn}(_ea, {val}); {R(i.ra)} = _ea; }}")
        else:
            self.e(f"{fn}({ea}, {val});")

    def op_lmw(self, i):
        self.e(f"{{ uint32_t _ea = {self._ea_d(i)}; for (int _k = {i.rd}; _k < 32; _k++, _ea += 4) ctx->r[_k] = mem_r32(_ea); }}")

    def op_stmw(self, i):
        self.e(f"{{ uint32_t _ea = {self._ea_d(i)}; for (int _k = {i.rs}; _k < 32; _k++, _ea += 4) mem_w32(_ea, ctx->r[_k]); }}")

    def op_lswi(self, i):
        self.e(f"ppc_lswi(ctx, {i.rd}, {RA0(i.ra)}, {i.nb or 32});")

    def op_stswi(self, i):
        self.e(f"ppc_stswi(ctx, {i.rs}, {RA0(i.ra)}, {i.nb or 32});")

    def op_lwarx(self, i):
        self.e(f"{{ uint32_t _ea = {self._ea_x(i)}; ctx->reserve_addr = _ea; {R(i.rd)} = mem_r32(_ea); }}")

    def op_stwcx_dot(self, i):
        self.e(f"mem_w32({self._ea_x(i)}, {R(i.rs)}); ppc_set_crf(ctx, 0, CR_EQ | ctx->xer_so);")

    def op_lhbrx(self, i):
        self.e(f"{R(i.rd)} = GCRT_BSWAP16(mem_r16({self._ea_x(i)}));")

    def op_lwbrx(self, i):
        self.e(f"{R(i.rd)} = GCRT_BSWAP32(mem_r32({self._ea_x(i)}));")

    def op_sthbrx(self, i):
        self.e(f"mem_w16({self._ea_x(i)}, GCRT_BSWAP16((uint16_t){R(i.rs)}));")

    def op_stwbrx(self, i):
        self.e(f"mem_w32({self._ea_x(i)}, GCRT_BSWAP32({R(i.rs)}));")

    # float loads/stores
    def _fload(self, i, single: bool, update=False, indexed=False):
        ea = self._ea_x(i) if indexed else self._ea_d(i)
        if single:
            val = "(double)u2f(mem_r32(_ea))"
            assign = f"{F(i.frd)} = {P1(i.frd)} = {val};"
        else:
            assign = f"{F(i.frd)} = u2d(mem_r64(_ea));"
        self.e(f"{{ uint32_t _ea = {ea}; {assign}" + (f" {R(i.ra)} = _ea;" if update else "") + " }")

    def _fstore(self, i, single: bool, update=False, indexed=False):
        ea = self._ea_x(i) if indexed else self._ea_d(i)
        st = (f"mem_w32(_ea, f2u((float){F(i.frs)}));" if single else f"mem_w64(_ea, d2u({F(i.frs)}));")
        self.e(f"{{ uint32_t _ea = {ea}; {st}" + (f" {R(i.ra)} = _ea;" if update else "") + " }")

    def op_stfiwx(self, i):
        self.e(f"mem_w32({self._ea_x(i)}, (uint32_t)d2u({F(i.frs)}));")

    # paired-single quantized
    def op_psq_l(self, i, update=False):
        self.e(f"{{ uint32_t _ea = {RA0(i.ra) if not update else R(i.ra)} + {U(i.ps_d)}; "
               f"ppc_psq_load(ctx, {i.frd}, _ea, {i.ps_w}, {i.ps_i});" + (f" {R(i.ra)} = _ea;" if update else "") + " }")

    def op_psq_lu(self, i): self.op_psq_l(i, True)

    def op_psq_st(self, i, update=False):
        self.e(f"{{ uint32_t _ea = {RA0(i.ra) if not update else R(i.ra)} + {U(i.ps_d)}; "
               f"ppc_psq_store(ctx, {i.frs}, _ea, {i.ps_w}, {i.ps_i});" + (f" {R(i.ra)} = _ea;" if update else "") + " }")

    def op_psq_stu(self, i): self.op_psq_st(i, True)

    def op_psq_lx(self, i, update=False):
        self.e(f"{{ uint32_t _ea = {self._ea_x(i)}; ppc_psq_load(ctx, {i.frd}, _ea, {i.ps_w}, {i.ps_i});"
               + (f" {R(i.ra)} = _ea;" if update else "") + " }")

    def op_psq_lux(self, i): self.op_psq_lx(i, True)

    def op_psq_stx(self, i, update=False):
        self.e(f"{{ uint32_t _ea = {self._ea_x(i)}; ppc_psq_store(ctx, {i.frs}, _ea, {i.ps_w}, {i.ps_i});"
               + (f" {R(i.ra)} = _ea;" if update else "") + " }")

    def op_psq_stux(self, i): self.op_psq_stx(i, True)

    # ---- cache / sync ----
    def op_dcbz(self, i): self.e(f"gcrt_dcbz(ctx, {self._ea_x(i)});")
    def op_dcbz_l(self, i): self.e(f"gcrt_dcbz(ctx, {self._ea_x(i)});")

    def _nop(self, i):
        pass

    op_dcbf = op_dcbst = op_dcbi = op_dcbt = op_dcbtst = op_icbi = _nop
    op_sync = op_isync = op_eieio = op_tlbie = op_tlbsync = op_eciwx = op_ecowx = _nop

    # ---- special registers ----
    def op_mfspr(self, i):
        spr = i.spr
        if spr == 8:
            self.e(f"{R(i.rd)} = ctx->lr;")
        elif spr == 9:
            self.e(f"{R(i.rd)} = ctx->ctr;")
        elif spr == 1:
            self.e(f"{R(i.rd)} = ppc_get_xer(ctx);")
        elif 912 <= spr <= 919:
            self.e(f"{R(i.rd)} = ctx->gqr[{spr - 912}];")
        else:
            self.e(f"{R(i.rd)} = gcrt_mfspr(ctx, {spr});")

    def op_mtspr(self, i):
        spr = i.spr
        if spr == 8:
            self.e(f"ctx->lr = {R(i.rs)};")
        elif spr == 9:
            self.e(f"ctx->ctr = {R(i.rs)};")
        elif spr == 1:
            self.e(f"ppc_set_xer(ctx, {R(i.rs)});")
        elif 912 <= spr <= 919:
            self.e(f"ctx->gqr[{spr - 912}] = {R(i.rs)};")
        else:
            self.e(f"gcrt_mtspr(ctx, {spr}, {R(i.rs)});")

    def op_mftb(self, i):
        which = "(uint32_t)(gcrt_timebase() >> 32)" if i.spr == 269 else "(uint32_t)gcrt_timebase()"
        self.e(f"{R(i.rd)} = {which};")

    def op_mfmsr(self, i): self.e(f"{R(i.rd)} = ctx->msr;")
    def op_mtmsr(self, i): self.e(f"gcrt_mtmsr(ctx, {R(i.rs)});")
    def op_mfsr(self, i): self.e(f"{R(i.rd)} = ctx->sr[{i.ra & 15}];")
    def op_mtsr(self, i): self.e(f"ctx->sr[{i.ra & 15}] = {R(i.rs)};")
    def op_mfsrin(self, i): self.e(f"{R(i.rd)} = ctx->sr[{R(i.rb)} >> 28];")
    def op_mtsrin(self, i): self.e(f"ctx->sr[{R(i.rb)} >> 28] = {R(i.rs)};")
    def op_mfcr(self, i): self.e(f"{R(i.rd)} = ctx->cr;")

    def op_mtcrf(self, i):
        m = 0
        for k in range(8):
            if i.crm & (0x80 >> k):
                m |= 0xF0000000 >> (4 * k)
        if m == 0xFFFFFFFF:
            self.e(f"ctx->cr = {R(i.rs)};")
        else:
            self.e(f"ctx->cr = (ctx->cr & {U(~m)}) | ({R(i.rs)} & {U(m)});")

    def op_mcrf(self, i): self.e(f"ppc_set_crf(ctx, {i.crfd}, ppc_get_crf(ctx, {i.crfs}));")

    def op_mcrxr(self, i):
        self.e(f"ppc_set_crf(ctx, {i.crfd}, ppc_get_xer(ctx) >> 28); ctx->xer_so = ctx->xer_ov = ctx->xer_ca = 0;")

    def _crop(self, i, op: str):
        a, b = f"ppc_crbit(ctx, {i.crba})", f"ppc_crbit(ctx, {i.crbb})"
        self.e(f"ppc_set_crbit(ctx, {i.crbd}, {op.format(a=a, b=b)});")

    def op_crand(self, i): self._crop(i, "{a} & {b}")
    def op_cror(self, i): self._crop(i, "{a} | {b}")
    def op_crxor(self, i): self._crop(i, "{a} ^ {b}")
    def op_crnand(self, i): self._crop(i, "~({a} & {b})")
    def op_crnor(self, i): self._crop(i, "~({a} | {b})")
    def op_creqv(self, i): self._crop(i, "~({a} ^ {b})")
    def op_crandc(self, i): self._crop(i, "{a} & ~{b}")
    def op_crorc(self, i): self._crop(i, "{a} | ~{b}")

    # ---- floating point ----
    def _fres(self, i, expr: str, single: bool, temps: str = ""):
        rc = " ppc_cr1_from_fpscr(ctx);" if i.rc else ""
        if single:
            self.e(f"{{ {temps}double _v = ppc_round_single({expr}); {F(i.frd)} = {P1(i.frd)} = _v; }}{rc}")
        else:
            self.e(f"{{ {temps}{F(i.frd)} = {expr}; }}{rc}")

    def op_fmr(self, i): self._fres(i, F(i.rb), False)
    def op_fneg(self, i): self._fres(i, f"-{F(i.rb)}", False)
    def op_fabs(self, i): self._fres(i, f"fabs({F(i.rb)})", False)
    def op_fnabs(self, i): self._fres(i, f"-fabs({F(i.rb)})", False)
    def op_frsp(self, i): self._fres(i, F(i.rb), True)
    def op_fctiw(self, i): self._fres(i, f"ppc_fctiw(ctx, {F(i.rb)}, 0)", False)
    def op_fctiwz(self, i): self._fres(i, f"ppc_fctiw(ctx, {F(i.rb)}, 1)", False)
    def op_fres(self, i): self._fres(i, f"ppc_fres({F(i.rb)})", True)
    def op_frsqrte(self, i): self._fres(i, f"ppc_frsqrte({F(i.rb)})", False)
    def op_fsel(self, i): self._fres(i, f"ppc_fsel({F(i.ra)}, {F(i.rc_reg)}, {F(i.rb)})", False)

    def op_fcmpu(self, i): self.e(f"ppc_fcmp(ctx, {i.crfd}, {F(i.ra)}, {F(i.rb)});")
    op_fcmpo = op_fcmpu

    def op_mffs(self, i):
        self.e(f"{F(i.frd)} = u2d(0xFFF8000000000000ull | ctx->fpscr);")

    def op_mtfsf(self, i):
        self.e(f"ppc_mtfsf(ctx, {i.fm}, d2u({F(i.rb)}));")

    def op_mtfsb0(self, i):
        self.e(f"ctx->fpscr &= ~{U(0x80000000 >> i.crbd)}; ppc_fpscr_changed(ctx);")

    def op_mtfsb1(self, i):
        self.e(f"ctx->fpscr |= {U(0x80000000 >> i.crbd)}; ppc_fpscr_changed(ctx);")

    def op_mtfsfi(self, i):
        imm = (i.word >> 12) & 0xF
        sh = 28 - 4 * i.crfd
        self.e(f"ctx->fpscr = (ctx->fpscr & {U(~(0xF << sh))}) | {U(imm << sh)}; ppc_fpscr_changed(ctx);")

    def op_mcrfs(self, i):
        sh = 28 - 4 * i.crfs
        self.e(f"ppc_set_crf(ctx, {i.crfd}, (ctx->fpscr >> {sh}) & 0xFu);")

    # ---- paired singles ----
    def _ps2(self, i, e0: str, e1: str, rnd: bool = True):
        r = "ppc_round_single" if rnd else ""
        a, b, c = i.ra, i.rb, i.rc_reg
        t = (f"double a0 = {F(a)}, a1 = {P1(a)}, b0 = {F(b)}, b1 = {P1(b)}, "
             f"c0 = {F(c)}, c1 = {P1(c)}; (void)a0; (void)a1; (void)b0; (void)b1; (void)c0; (void)c1; ")
        self.e(f"{{ {t}{F(i.frd)} = {r}({e0}); {P1(i.frd)} = {r}({e1}); }}")
        if i.rc:
            self.e("ppc_cr1_from_fpscr(ctx);")

    def op_ps_add(self, i): self._ps2(i, "a0 + b0", "a1 + b1")
    def op_ps_sub(self, i): self._ps2(i, "a0 - b0", "a1 - b1")
    def op_ps_mul(self, i): self._ps2(i, "a0 * c0", "a1 * c1")
    def op_ps_div(self, i): self._ps2(i, "a0 / b0", "a1 / b1")
    def op_ps_madd(self, i): self._ps2(i, "a0 * c0 + b0", "a1 * c1 + b1")
    def op_ps_msub(self, i): self._ps2(i, "a0 * c0 - b0", "a1 * c1 - b1")
    def op_ps_nmadd(self, i): self._ps2(i, "-(a0 * c0 + b0)", "-(a1 * c1 + b1)")
    def op_ps_nmsub(self, i): self._ps2(i, "-(a0 * c0 - b0)", "-(a1 * c1 - b1)")
    def op_ps_muls0(self, i): self._ps2(i, "a0 * c0", "a1 * c0")
    def op_ps_muls1(self, i): self._ps2(i, "a0 * c1", "a1 * c1")
    def op_ps_madds0(self, i): self._ps2(i, "a0 * c0 + b0", "a1 * c0 + b1")
    def op_ps_madds1(self, i): self._ps2(i, "a0 * c1 + b0", "a1 * c1 + b1")
    def op_ps_sum0(self, i): self._ps2(i, "a0 + b1", "c1")
    def op_ps_sum1(self, i): self._ps2(i, "c0", "a0 + b1")
    def op_ps_sel(self, i): self._ps2(i, "a0 >= 0.0 ? c0 : b0", "a1 >= 0.0 ? c1 : b1", rnd=False)
    def op_ps_res(self, i): self._ps2(i, "ppc_fres(b0)", "ppc_fres(b1)")
    def op_ps_rsqrte(self, i): self._ps2(i, "ppc_frsqrte(b0)", "ppc_frsqrte(b1)")
    def op_ps_neg(self, i): self._ps2(i, "-b0", "-b1", rnd=False)
    def op_ps_abs(self, i): self._ps2(i, "fabs(b0)", "fabs(b1)", rnd=False)
    def op_ps_nabs(self, i): self._ps2(i, "-fabs(b0)", "-fabs(b1)", rnd=False)
    def op_ps_mr(self, i): self._ps2(i, "b0", "b1", rnd=False)
    def op_ps_merge00(self, i): self._ps2(i, "a0", "b0", rnd=False)
    def op_ps_merge01(self, i): self._ps2(i, "a0", "b1", rnd=False)
    def op_ps_merge10(self, i): self._ps2(i, "a1", "b0", rnd=False)
    def op_ps_merge11(self, i): self._ps2(i, "a1", "b1", rnd=False)
    def op_ps_cmpu0(self, i): self.e(f"ppc_fcmp(ctx, {i.crfd}, {F(i.ra)}, {F(i.rb)});")
    op_ps_cmpo0 = op_ps_cmpu0
    def op_ps_cmpu1(self, i): self.e(f"ppc_fcmp(ctx, {i.crfd}, {P1(i.ra)}, {P1(i.rb)});")
    op_ps_cmpo1 = op_ps_cmpu1


# --- table-driven generic instructions --------------------------------------
_INT_LOADS = {"lwz": ("mem_r32", ""), "lbz": ("mem_r8", ""), "lhz": ("mem_r16", ""),
              "lha": ("mem_r16", "(uint32_t)(int32_t)(int16_t)")}
_INT_STORES = {"stw": ("mem_w32", "{}"), "stb": ("mem_w8", "(uint8_t){}"), "sth": ("mem_w16", "(uint16_t){}")}

_GENERIC = {}


def _mk_load(fn, cast, update, indexed):
    return lambda self, i: self._load(i, fn, cast, update, indexed)


def _mk_store(fn, fmt, update, indexed):
    return lambda self, i: self._store(i, fn, fmt.format(R(i.rs)), update, indexed)


for _n, (_fn, _cast) in _INT_LOADS.items():
    _GENERIC[_n] = _mk_load(_fn, _cast, False, False)
    _GENERIC[_n + "u"] = _mk_load(_fn, _cast, True, False)
    _GENERIC[_n + "x"] = _mk_load(_fn, _cast, False, True)
    _GENERIC[_n + "ux"] = _mk_load(_fn, _cast, True, True)
for _n, (_fn, _fmt) in _INT_STORES.items():
    _GENERIC[_n] = _mk_store(_fn, _fmt, False, False)
    _GENERIC[_n + "u"] = _mk_store(_fn, _fmt, True, False)
    _GENERIC[_n + "x"] = _mk_store(_fn, _fmt, False, True)
    _GENERIC[_n + "ux"] = _mk_store(_fn, _fmt, True, True)
for _n, _single in (("lfs", True), ("lfd", False)):
    _GENERIC[_n] = (lambda s: lambda self, i: self._fload(i, s))(_single)
    _GENERIC[_n + "u"] = (lambda s: lambda self, i: self._fload(i, s, update=True))(_single)
    _GENERIC[_n + "x"] = (lambda s: lambda self, i: self._fload(i, s, indexed=True))(_single)
    _GENERIC[_n + "ux"] = (lambda s: lambda self, i: self._fload(i, s, True, True))(_single)
for _n, _single in (("stfs", True), ("stfd", False)):
    _GENERIC[_n] = (lambda s: lambda self, i: self._fstore(i, s))(_single)
    _GENERIC[_n + "u"] = (lambda s: lambda self, i: self._fstore(i, s, update=True))(_single)
    _GENERIC[_n + "x"] = (lambda s: lambda self, i: self._fstore(i, s, indexed=True))(_single)
    _GENERIC[_n + "ux"] = (lambda s: lambda self, i: self._fstore(i, s, True, True))(_single)


def _fpu(expr, single):
    return lambda self, i: self._fres(i, expr.format(a=F(i.ra), b=F(i.rb), c=F(i.rc_reg)), single)


for _base, _expr in (("fadd", "{a} + {b}"), ("fsub", "{a} - {b}"), ("fmul", "{a} * {c}"),
                     ("fdiv", "{a} / {b}"), ("fmadd", "{a} * {c} + {b}"), ("fmsub", "{a} * {c} - {b}"),
                     ("fnmadd", "-({a} * {c} + {b})"), ("fnmsub", "-({a} * {c} - {b})")):
    _GENERIC[_base] = _fpu(_expr, False)
    _GENERIC[_base + "s"] = _fpu(_expr, True)


# ---------------------------------------------------------------------------
# whole-program emission
# ---------------------------------------------------------------------------
@dataclass
class GeneratedCode:
    files: dict[str, str]
    unknown_ops: set[str]
    function_count: int


def generate(res: AnalysisResult, opts: CodegenOptions, game_meta: dict, funcs_per_file: int = 400) -> GeneratedCode:
    fns = res.sorted_functions()
    names = {f.start: f.name for f in fns}
    files: dict[str, str] = {}
    unknown: set[str] = set()

    hle_names = {addr: c for addr, c in opts.hle.items() if addr in names}

    header = ["/* Generated by gcwii-recomp. Do not edit. */", "#pragma once", '#include "gcrt/gcrt.h"', ""]
    for f in fns:
        header.append(f"void {f.name}(PPCContext* ctx);")
        if f.start in hle_names:
            header.append(f"void {f.name}__orig(PPCContext* ctx);")
    for c in sorted(set(hle_names.values())):
        header.append(f"void {c}(PPCContext* ctx);")
    files["recomp.h"] = "\n".join(header) + "\n"

    for chunk_idx in range(0, len(fns), funcs_per_file):
        body = ["/* Generated by gcwii-recomp. Do not edit. */", '#include "recomp.h"', ""]
        for f in fns[chunk_idx:chunk_idx + funcs_per_file]:
            em = FunctionEmitter(res, f, opts, names)
            lines = em.emit()
            unknown |= em.unknown
            if f.start in hle_names:
                lines[0] = lines[0].replace(f"void {f.name}(", f"void {f.name}__orig(", 1)
                body += lines
                body += [f"void {f.name}(PPCContext* ctx) {{ {hle_names[f.start]}(ctx); }}"]
            else:
                body += lines
            body.append("")
        files[f"funcs_{chunk_idx // funcs_per_file:04d}.c"] = "\n".join(body)

    # function table + game info
    tbl = ["/* Generated by gcwii-recomp. Do not edit. */", '#include "recomp.h"', "",
           "static const GCRTFuncEntry g_funcs[] = {"]
    for f in fns:
        tbl.append(f"    {{ {U(f.start)}, {f.name} }},")
    tbl.append("};")
    text = res.dol.text_sections
    lo = min(s.address for s in text) if text else 0
    hi = max(s.end for s in text) if text else 0

    def cstr(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    tbl += ["", "const GCRTGameInfo gcrt_game = {",
            f"    {cstr(game_meta.get('game_id', ''))},",
            f"    {cstr(game_meta.get('title', ''))},",
            f"    {1 if game_meta.get('platform') == 'wii' else 0},",
            f"    {U(res.dol.entry)},",
            f"    {U(lo)}, {U(hi)},",
            "    g_funcs,",
            f"    {len(fns)}u,",
            f"    {U(opts.save_context_addr)},",
            f"    {cstr(game_meta.get('dol', 'main.dol'))},",
            f"    {cstr(game_meta.get('disc', ''))},",
            f"    {1 if opts.interrupt_checks else 0},",
            "};", ""]
    files["func_table.c"] = "\n".join(tbl)
    return GeneratedCode(files, unknown, len(fns))

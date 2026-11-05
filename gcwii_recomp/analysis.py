"""Function discovery and control-flow analysis over a DOL."""
from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass, field

from .dol import DOL
from .ppc import Insn, decode
from .symbols import Symbol, c_identifier

MAX_JUMP_TABLE = 1024


@dataclass
class Function:
    start: int
    end: int = 0
    name: str = ""
    labels: set[int] = field(default_factory=set)
    jump_tables: dict[int, list[int]] = field(default_factory=dict)   # bctr addr -> targets
    origin: str = "analysis"     # entry | symbol | call | pointer | gap | tailcall | config

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclass
class AnalysisResult:
    dol: DOL
    insns: dict[int, Insn]
    functions: dict[int, Function]
    warnings: list[str]

    def sorted_functions(self) -> list[Function]:
        return [self.functions[a] for a in sorted(self.functions)]

    def coverage(self) -> float:
        total = sum(s.size for s in self.dol.text_sections)
        covered = sum(f.size for f in self.functions.values())
        return covered / total if total else 0.0


class Analyzer:
    def __init__(self, dol: DOL, symbols: list[Symbol] | None = None, extra_functions: list[int] | None = None,
                 fill_gaps: bool = True, scan_data_pointers: bool = True, log=print):
        self.dol = dol
        self.symbols = symbols or []
        self.extra = extra_functions or []
        self.fill_gaps = fill_gaps
        self.scan_data_pointers = scan_data_pointers
        self.log = log
        self.insns: dict[int, Insn] = {}
        self.warnings: list[str] = []
        self.names: dict[int, str] = {}
        for sec in dol.text_sections:
            for off in range(0, sec.size - 3, 4):
                a = sec.address + off
                self.insns[a] = decode(struct.unpack_from(">I", sec.data, off)[0], a)

    # ------------------------------------------------------------------
    def _valid_start(self, addr: int) -> bool:
        i = self.insns.get(addr)
        return i is not None and i.name != "illegal"

    def _seeds(self) -> dict[int, str]:
        seeds: dict[int, str] = {}
        if self._valid_start(self.dol.entry):
            seeds[self.dol.entry] = "entry"
        for s in self.symbols:
            if s.kind != "object" and self._valid_start(s.address):
                seeds.setdefault(s.address, "symbol")
                self.names.setdefault(s.address, s.name)
        for a in self.extra:
            if a in self.insns:
                seeds[a] = "config"
        for i in self.insns.values():
            if i.name in ("b", "bc") and i.lk:
                t = i.branch_target
                if self._valid_start(t) and t != i.addr + 4:
                    seeds.setdefault(t, "call")
        if self.scan_data_pointers:
            # Jump-table entries also look like code pointers; keep them inside their function.
            jt_targets: set[int] = set()
            for i in self.insns.values():
                if i.name == "bcctr" and not i.lk and i.is_unconditional:
                    sec = self.dol.section_for(i.addr)
                    tbl = self._jump_table(i, max(sec.address, i.addr - 96), sec.end)
                    if tbl:
                        jt_targets.update(tbl)
            for sec in self.dol.sections:
                for off in range(0, sec.size - 3, 4):
                    v = struct.unpack_from(">I", sec.data, off)[0]
                    if v & 3 or v not in self.insns or not self._valid_start(v) or v in jt_targets:
                        continue
                    prev = self.insns.get(v - 4)
                    # Accept pointers that land on a plausible function start.
                    if (prev is None or prev.name == "illegal" or
                            (prev.is_branch and prev.is_unconditional and not prev.lk) or
                            self.insns[v].name in ("stwu", "mfspr")):
                        seeds.setdefault(v, "pointer")
        return seeds

    # ------------------------------------------------------------------
    def _jump_table(self, bctr: Insn, lo_bound: int, hi_bound: int) -> list[int] | None:
        """Recognise the CodeWarrior/GCC ``lwzx; mtctr; bctr`` switch idiom."""
        addr = bctr.addr - 4
        ctr_reg = tbl_reg = idx_reg = None
        base_hi = base_lo = None
        bound = None
        scaled = None
        steps = 0
        while addr >= lo_bound and steps < 24:
            i = self.insns.get(addr)
            if i is None:
                break
            n = i.name
            if ctr_reg is None:
                if n == "mtspr" and i.spr == 9:
                    ctr_reg = i.rs
            elif tbl_reg is None:
                if n == "lwzx" and i.rd == ctr_reg:
                    tbl_reg, scaled = i.ra, i.rb
            else:
                if n == "addi" and i.rd == tbl_reg and base_lo is None:
                    base_lo = i.simm
                    if i.ra != tbl_reg:
                        tbl_reg = i.ra
                elif n == "addis" and i.rd == tbl_reg and i.ra == 0 and base_hi is None:
                    base_hi = i.uimm
                elif n == "rlwinm" and i.ra == scaled and idx_reg is None:
                    idx_reg = i.rs
                elif n == "cmpli" and bound is None and (i.ra == idx_reg or i.ra == scaled or idx_reg is None):
                    bound = i.uimm
                elif i.is_branch and not i.lk and i.name == "b":
                    break
            addr -= 4
            steps += 1
        if base_hi is None or base_lo is None:
            return None
        base = ((base_hi << 16) + base_lo) & 0xFFFFFFFF
        count = bound + 1 if bound is not None else MAX_JUMP_TABLE
        targets = []
        for k in range(min(count, MAX_JUMP_TABLE)):
            t = self.dol.read32(base + 4 * k)
            if t is None or t not in self.insns or not (lo_bound <= t < hi_bound):
                if bound is not None:
                    return None
                break
            targets.append(t)
        return targets or None

    # ------------------------------------------------------------------
    def _explore(self, fn: Function, limit: int, starts: set[int], new_seeds: dict[int, str]) -> None:
        work = [fn.start]
        seen: set[int] = set()
        end = fn.start
        while work:
            a = work.pop()
            while a not in seen:
                if a >= limit or a not in self.insns:
                    break
                seen.add(a)
                i = self.insns[a]
                end = max(end, a + 4)
                n = i.name
                if n == "illegal":
                    break
                if n == "b":
                    t = i.branch_target
                    if i.lk:
                        a += 4
                        continue
                    if t in starts and t != fn.start:
                        break                                   # tail call
                    if fn.start <= t < limit:
                        fn.labels.add(t)
                        work.append(t)
                    elif t in self.insns:
                        new_seeds.setdefault(t, "tailcall")
                    break
                if n == "bc":
                    t = i.branch_target
                    if i.lk:
                        a += 4
                        continue
                    if t in starts and t != fn.start:
                        pass                                    # conditional tail call
                    elif fn.start <= t < limit:
                        fn.labels.add(t)
                        work.append(t)
                    elif t in self.insns:
                        new_seeds.setdefault(t, "tailcall")
                    if i.is_unconditional:
                        break
                    a += 4
                    continue
                if n == "bclr":
                    if i.is_unconditional and not i.lk:
                        break
                    a += 4
                    continue
                if n == "bcctr":
                    if i.lk:
                        a += 4
                        continue
                    if i.is_unconditional:
                        tbl = self._jump_table(i, fn.start, limit)
                        if tbl:
                            fn.jump_tables[a] = tbl
                            for t in tbl:
                                fn.labels.add(t)
                                work.append(t)
                        break
                    a += 4
                    continue
                if n == "rfi":
                    break
                a += 4
        fn.end = max(end, fn.start + 4)

    def run(self) -> AnalysisResult:
        seeds = self._seeds()
        text_ranges = sorted((s.address, s.end) for s in self.dol.text_sections)

        def section_end(addr: int) -> int:
            for lo, hi in text_ranges:
                if lo <= addr < hi:
                    return hi
            return addr

        functions: dict[int, Function] = {}
        for _round in range(64):
            starts = set(seeds)
            ordered = sorted(starts)
            new_seeds: dict[int, str] = {}
            functions = {}
            for idx, s in enumerate(ordered):
                nxt = ordered[idx + 1] if idx + 1 < len(ordered) else 0xFFFFFFFF
                limit = min(nxt, section_end(s))
                fn = Function(s, origin=seeds[s])
                self._explore(fn, limit, starts, new_seeds)
                functions[s] = fn
            new_seeds = {a: o for a, o in new_seeds.items() if a not in seeds}
            if not new_seeds and self.fill_gaps:
                new_seeds = self._gap_seeds(functions, text_ranges)
            if not new_seeds:
                break
            seeds.update(new_seeds)
        else:
            self.warnings.append("Function discovery did not converge after 64 rounds")

        for fn in functions.values():
            # prefix keeps guest names (exit, memcpy, main, ...) from clashing with the host C library
            fn.name = ("rc_" + c_identifier(self.names[fn.start])) if fn.start in self.names else f"rc_{fn.start:08X}"
        # de-duplicate names
        used: dict[str, int] = {}
        for fn in sorted(functions.values(), key=lambda f: f.start):
            if fn.name in used:
                fn.name = f"{fn.name}_{fn.start:08X}"
            used[fn.name] = fn.start
        return AnalysisResult(self.dol, self.insns, functions, self.warnings)

    def _gap_seeds(self, functions: dict[int, Function], text_ranges) -> dict[int, str]:
        covered = sorted((f.start, f.end) for f in functions.values())
        out: dict[int, str] = {}
        for lo, hi in text_ranges:
            pos = lo
            idx = bisect.bisect_left(covered, (lo, 0))
            ranges = [r for r in covered[max(0, idx - 1):] if r[0] < hi]
            for fs, fe in ranges + [(hi, hi)]:
                gap_end = min(fs, hi)
                a = pos
                while a < gap_end:
                    i = self.insns.get(a)
                    if i is not None and i.name != "illegal" and i.word != 0x60000000:
                        out[a] = "gap"
                        break
                    a += 4
                pos = max(pos, fe)
        return out


def analyze(dol: DOL, symbols=None, extra_functions=None, **kw) -> AnalysisResult:
    return Analyzer(dol, symbols, extra_functions, **kw).run()

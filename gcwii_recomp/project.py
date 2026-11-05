"""End-to-end pipeline: input -> analysis -> C sources -> self-contained CMake project."""
from __future__ import annotations

import json
import os
import shutil
import struct
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import __version__
from .analysis import analyze
from .codegen import CodegenOptions, generate
from .disc import Source, open_source
from .dol import DOL
from .hle import BUILTIN, DEFAULT_HLE, FAST_LIBC, OSSAVECONTEXT_FIRST_WORD
from .symbols import Symbol, load_symbols

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(PKG_DIR)
TEMPLATE_DIR = os.path.join(PKG_DIR, "templates")


def runtime_dir() -> str:
    for cand in (os.path.join(PKG_DIR, "runtime"), os.path.join(REPO_DIR, "runtime")):
        if os.path.isfile(os.path.join(cand, "CMakeLists.txt")):
            return cand
    raise FileNotFoundError("gcrt runtime sources not found (expected ./runtime next to the package)")


@dataclass
class ProjectConfig:
    input: str = ""
    output: str = ""
    name: str = ""
    platform: str = ""                     # "" = auto (from disc), "gamecube" or "wii"
    common_key: str = ""                   # Wii common key (hex or file); never written to disk
    symbols: str = ""                      # symbol map path
    extra_functions: list[int] = field(default_factory=list)
    fill_gaps: bool = True
    scan_data_pointers: bool = True
    interrupt_checks: bool = True
    comments: bool = True
    funcs_per_file: int = 400
    hle_overrides: dict[str, str] = field(default_factory=dict)   # symbol-or-address -> hle C function
    fast_libc: bool = False
    save_context: str = "auto"             # symbol name, 0x address, "auto" or "off"
    copy_disc: bool = False                # GC: copy ISO into assets; Wii: write decrypted partition
    extract_files: bool = False            # extract the FST to assets/files for inspection/modding

    @staticmethod
    def load(path: str) -> "ProjectConfig":
        if tomllib is None:
            raise RuntimeError("Reading TOML configs needs Python 3.11+")
        with open(path, "rb") as f:
            d = tomllib.load(f)
        c = ProjectConfig()
        base = os.path.dirname(os.path.abspath(path))

        def p(v: str) -> str:
            return v if not v or os.path.isabs(v) else os.path.normpath(os.path.join(base, v))

        proj = d.get("project", {})
        c.input = p(proj.get("input", ""))
        c.output = p(proj.get("output", ""))
        c.name = proj.get("name", "")
        c.platform = proj.get("platform", "")
        an = d.get("analysis", {})
        c.symbols = p(an.get("symbols", ""))
        c.extra_functions = [int(x, 0) if isinstance(x, str) else int(x) for x in an.get("extra_functions", [])]
        c.fill_gaps = an.get("fill_gaps", True)
        c.scan_data_pointers = an.get("scan_data_pointers", True)
        cg = d.get("codegen", {})
        c.interrupt_checks = cg.get("interrupt_checks", True)
        c.comments = cg.get("comments", True)
        c.funcs_per_file = int(cg.get("funcs_per_file", 400))
        hle = d.get("hle", {})
        c.fast_libc = hle.get("fast_libc", False)
        c.hle_overrides = dict(hle.get("overrides", {}))
        c.save_context = str(d.get("threads", {}).get("save_context", "auto"))
        disc = d.get("disc", {})
        c.copy_disc = disc.get("copy_disc", False)
        c.extract_files = disc.get("extract_files", False)
        return c

    def to_toml(self) -> str:
        def s(v: str) -> str:
            return json.dumps(v)
        lines = [
            "# gcwii-recomp project configuration", "",
            "[project]", f"name = {s(self.name)}", f"input = {s(self.input)}", f"output = {s(self.output)}",
            f"platform = {s(self.platform)}", "",
            "[analysis]", f"symbols = {s(self.symbols)}",
            "extra_functions = [" + ", ".join(s(f"0x{a:08X}") for a in self.extra_functions) + "]",
            f"fill_gaps = {str(self.fill_gaps).lower()}",
            f"scan_data_pointers = {str(self.scan_data_pointers).lower()}", "",
            "[codegen]", f"interrupt_checks = {str(self.interrupt_checks).lower()}",
            f"comments = {str(self.comments).lower()}", f"funcs_per_file = {self.funcs_per_file}", "",
            "[hle]", f"fast_libc = {str(self.fast_libc).lower()}", "",
            "[hle.overrides]", "# \"SymbolName\" or \"0x80001234\" = one of: " + ", ".join(BUILTIN),
        ]
        for k, v in self.hle_overrides.items():
            lines.append(f"{s(k)} = {s(v)}")
        lines += ["", "[threads]", "# OSSaveContext symbol / address, \"auto\" (symbol or signature scan) or \"off\"",
                  f"save_context = {s(self.save_context)}", "",
                  "[disc]", f"copy_disc = {str(self.copy_disc).lower()}",
                  f"extract_files = {str(self.extract_files).lower()}", ""]
        return "\n".join(lines)


@dataclass
class RecompResult:
    output: str
    functions: int
    coverage: float
    unknown_ops: list[str]
    hle: dict[str, str]
    warnings: list[str]
    seconds: float


Log = Callable[[str], None]


def _resolve_hle(cfg: ProjectConfig, syms: list[Symbol], log: Log) -> dict[int, str]:
    by_name: dict[str, int] = {}
    for s in syms:
        by_name.setdefault(s.name, s.address)
    table = dict(DEFAULT_HLE)
    if cfg.fast_libc:
        table.update(FAST_LIBC)
    out: dict[int, str] = {}
    for name, fn in table.items():
        if name in by_name:
            out[by_name[name]] = fn
    for key, fn in cfg.hle_overrides.items():
        if not fn.replace("_", "").isalnum():
            raise ValueError(f"Invalid HLE function name {fn!r}")
        if key.lower().startswith("0x"):
            out[int(key, 16)] = fn
        elif key in by_name:
            out[by_name[key]] = fn
        else:
            log(f"warning: HLE override for unknown symbol {key!r} ignored")
    return out


def _find_save_context(cfg: ProjectConfig, dol: DOL, syms: list[Symbol], log: Log) -> int:
    sc = cfg.save_context.strip()
    if sc.lower() == "off":
        return 0
    if sc.lower().startswith("0x"):
        return int(sc, 16)
    name = "OSSaveContext" if sc.lower() == "auto" else sc
    for s in syms:
        if s.name == name:
            return s.address
    if sc.lower() != "auto":
        log(f"warning: save_context symbol {sc!r} not found; thread switching disabled")
        return 0
    for sec in dol.text_sections:
        for off in range(0, sec.size - 3, 4):
            if struct.unpack_from(">I", sec.data, off)[0] == OSSAVECONTEXT_FIRST_WORD:
                addr = sec.address + off
                log(f"threads: OSSaveContext located by signature at 0x{addr:08X}")
                return addr
    log("threads: OSSaveContext not found - guest thread switching disabled")
    return 0


def recompile(cfg: ProjectConfig, log: Log = print, progress: Callable[[float], None] | None = None,
              source: Source | None = None) -> RecompResult:
    t0 = time.time()
    prog = progress or (lambda f: None)
    src = source or open_source(cfg.input, cfg.common_key or None)
    log(src.describe())
    platform = cfg.platform or src.platform
    name = cfg.name or (src.game_id or os.path.splitext(os.path.basename(cfg.input))[0] or "game")
    out = os.path.abspath(cfg.output or os.path.join(os.getcwd(), f"{name}-recomp"))
    os.makedirs(out, exist_ok=True)
    prog(0.05)

    dol = DOL(src.dol)
    if platform == "gamecube" and dol.uses_mem2:
        log("note: executable uses MEM2 addresses - treating as Wii")
        platform = "wii"
    log(dol.summary())

    syms: list[Symbol] = []
    if cfg.symbols:
        syms = load_symbols(cfg.symbols)
        log(f"loaded {len(syms)} symbols from {cfg.symbols}")

    save_ctx = _find_save_context(cfg, dol, syms, log)
    hle = _resolve_hle(cfg, syms, log)
    extra = list(cfg.extra_functions) + list(hle) + ([save_ctx] if save_ctx else [])

    log("analyzing control flow...")
    res = analyze(dol, syms, extra, fill_gaps=cfg.fill_gaps, scan_data_pointers=cfg.scan_data_pointers, log=log)
    log(f"found {len(res.functions)} functions, {res.coverage() * 100:.1f}% of code covered")
    for w in res.warnings:
        log("warning: " + w)
    prog(0.35)

    # assets
    assets = os.path.join(out, "assets")
    os.makedirs(assets, exist_ok=True)
    with open(os.path.join(assets, "main.dol"), "wb") as f:
        f.write(src.dol)
    disc_ref = ""
    if src.volume is not None:
        if src.kind == "gamecube-disc" and not cfg.copy_disc:
            disc_ref = os.path.abspath(src.path)
        elif cfg.copy_disc or src.kind == "wii-disc":
            if src.kind == "wii-disc" and not cfg.copy_disc:
                log("note: Wii games read data through the decrypted partition image; "
                    "enable 'copy_disc' to write assets/disc.img (several GB)")
            else:
                log("writing flat disc image to assets/disc.img ...")
                src.volume.write_flat_image(os.path.join(assets, "disc.img"),
                                            lambda a, b: prog(0.35 + 0.25 * a / max(b, 1)))
                disc_ref = "disc.img"
        if cfg.extract_files:
            log("extracting file system to assets/files ...")
            src.volume.extract_all(assets)
    prog(0.6)

    log("generating C code...")
    opts = CodegenOptions(interrupt_checks=cfg.interrupt_checks, comments=cfg.comments,
                          save_context_addr=save_ctx, hle=hle)
    meta = {"game_id": src.game_id, "title": src.title, "platform": platform, "dol": "main.dol", "disc": disc_ref}
    gen = generate(res, opts, meta, funcs_per_file=max(1, cfg.funcs_per_file))
    gen_dir = os.path.join(out, "generated")
    if os.path.isdir(gen_dir):
        shutil.rmtree(gen_dir)
    os.makedirs(gen_dir)
    for fname, text in gen.files.items():
        with open(os.path.join(gen_dir, fname), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    if gen.unknown_ops:
        log("warning: unsupported instructions emitted as traps: " + ", ".join(sorted(gen.unknown_ops)))
    prog(0.85)

    write_cmake_project(out, name, sorted(f for f in gen.files if f.endswith(".c")))
    write_android_project(out, name, src.title or name)

    hle_named = {f"0x{a:08X}": fn for a, fn in hle.items()}
    report = {
        "tool": f"gcwii-recomp {__version__}", "game_id": src.game_id, "title": src.title, "platform": platform,
        "entry": f"0x{dol.entry:08X}", "functions": gen.function_count, "coverage": round(res.coverage(), 4),
        "unknown_ops": sorted(gen.unknown_ops), "hle": hle_named,
        "save_context": f"0x{save_ctx:08X}" if save_ctx else None, "warnings": res.warnings,
        "functions_by_origin": _count_origins(res),
    }
    with open(os.path.join(out, "recomp_report.json"), "w") as f:
        json.dump(report, f, indent=2)
    saved = ProjectConfig(**{**asdict(cfg), "common_key": "", "output": out, "name": name,
                             "input": os.path.abspath(cfg.input) if cfg.input else ""})
    with open(os.path.join(out, "gcwii-project.toml"), "w") as f:
        f.write(saved.to_toml())
    prog(1.0)
    dt = time.time() - t0
    log(f"done in {dt:.1f}s -> {out}")
    return RecompResult(out, gen.function_count, res.coverage(), sorted(gen.unknown_ops), hle_named,
                        res.warnings, dt)


def _count_origins(res) -> dict[str, int]:
    d: dict[str, int] = {}
    for f in res.functions.values():
        d[f.origin] = d.get(f.origin, 0) + 1
    return d


def _safe_target(name: str) -> str:
    t = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
    return t or "game"


def write_cmake_project(out: str, name: str, sources: list[str]) -> None:
    rt_dst = os.path.join(out, "runtime")
    if os.path.isdir(rt_dst):
        shutil.rmtree(rt_dst)
    shutil.copytree(runtime_dir(), rt_dst, ignore=shutil.ignore_patterns("build*", "android"))
    target = _safe_target(name)
    src_list = "\n".join(f"  generated/{s}" for s in sources)
    cm = f"""# Generated by gcwii-recomp {__version__}
cmake_minimum_required(VERSION 3.16)
project({target}_native C)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_subdirectory(runtime)
gcrt_add_game({target}
{src_list}
)
target_include_directories({target} PRIVATE generated)

# Copy the executable's assets next to the binary.
if(NOT ANDROID)
  add_custom_command(TARGET {target} POST_BUILD
    COMMAND ${{CMAKE_COMMAND}} -E copy_directory ${{CMAKE_CURRENT_SOURCE_DIR}}/assets $<TARGET_FILE_DIR:{target}>)
endif()
"""
    with open(os.path.join(out, "CMakeLists.txt"), "w", newline="\n") as f:
        f.write(cm)


def write_android_project(out: str, name: str, title: str) -> None:
    target = _safe_target(name)
    tdir = os.path.join(TEMPLATE_DIR, "android")
    adir = os.path.join(out, "android")
    if not os.path.isdir(tdir):
        return
    for root, _dirs, files in os.walk(tdir):
        rel = os.path.relpath(root, tdir)
        dst = os.path.join(adir, rel)
        os.makedirs(dst, exist_ok=True)
        for fn in files:
            with open(os.path.join(root, fn), encoding="utf-8") as f:
                text = f.read()
            text = (text.replace("@TARGET@", target).replace("@TITLE@", title.replace("&", "&amp;").replace("<", "&lt;"))
                    .replace("@PACKAGE@", "org.gcwiirecomp.game_" + target.lower().replace("-", "_")))
            with open(os.path.join(dst, fn), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)

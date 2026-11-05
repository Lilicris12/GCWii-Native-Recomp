"""Command-line interface.

    gcwii-recomp gui                         launch the desktop app
    gcwii-recomp info  <disc|dol>            show what is on a disc
    gcwii-recomp recomp <disc|dol> -o out    recompile to a CMake project
    gcwii-recomp build <project> --target windows|linux|android|native
    gcwii-recomp extract <disc> -o dir       dump the file system
    gcwii-recomp disasm <disc|dol> 0x80003100 [count]
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__


def _cfg_from_args(a) -> "ProjectConfig":
    from .project import ProjectConfig
    cfg = ProjectConfig.load(a.config) if getattr(a, "config", None) else ProjectConfig()
    if a.input:
        cfg.input = os.path.abspath(a.input)
    if a.output:
        cfg.output = os.path.abspath(a.output)
    if a.symbols:
        cfg.symbols = os.path.abspath(a.symbols)
    if a.common_key:
        cfg.common_key = a.common_key
    if a.name:
        cfg.name = a.name
    if a.platform:
        cfg.platform = a.platform
    if a.no_comments:
        cfg.comments = False
    if a.no_interrupt_checks:
        cfg.interrupt_checks = False
    if a.copy_disc:
        cfg.copy_disc = True
    if a.extract_files:
        cfg.extract_files = True
    if a.fast_libc:
        cfg.fast_libc = True
    for item in a.hle or []:
        k, _, v = item.partition("=")
        cfg.hle_overrides[k] = v
    for f in a.function or []:
        cfg.extra_functions.append(int(f, 0))
    if not cfg.input:
        raise SystemExit("error: no input given")
    return cfg


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gcwii-recomp",
                                description="Turn GameCube/Wii discs you own into native programs by static recompilation.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd")

    pg = sub.add_parser("gui", help="launch the desktop application")
    pg.add_argument("input", nargs="?", help="disc image to open")

    pi = sub.add_parser("info", help="describe a disc image / DOL")
    pi.add_argument("input")
    pi.add_argument("--common-key")

    pr = sub.add_parser("recomp", help="recompile a disc/DOL into a native CMake project")
    pr.add_argument("input", nargs="?")
    pr.add_argument("-o", "--output")
    pr.add_argument("-c", "--config", help="gcwii-project.toml")
    pr.add_argument("-s", "--symbols", help="symbol map (Dolphin .map, decomp-toolkit symbols.txt, ...)")
    pr.add_argument("--common-key", help="Wii common key: hex string or file (keys.bin)")
    pr.add_argument("--name")
    pr.add_argument("--platform", choices=["gamecube", "wii"])
    pr.add_argument("--hle", action="append", metavar="SYMBOL=hle_func", help="replace a function natively")
    pr.add_argument("--function", action="append", metavar="ADDR", help="force a function start")
    pr.add_argument("--fast-libc", action="store_true", help="native memcpy/memset/strlen")
    pr.add_argument("--copy-disc", action="store_true", help="copy/decrypt the disc image into assets/")
    pr.add_argument("--extract-files", action="store_true", help="also extract the file system")
    pr.add_argument("--no-comments", action="store_true", help="omit disassembly comments (smaller C)")
    pr.add_argument("--no-interrupt-checks", action="store_true")
    pr.add_argument("--build", choices=["native", "linux", "windows", "android"], action="append",
                    help="build after generating (repeatable)")

    pb = sub.add_parser("build", help="build a generated project")
    pb.add_argument("project")
    pb.add_argument("--target", default="native", choices=["native", "linux", "windows", "android"])
    pb.add_argument("--debug", action="store_true")
    pb.add_argument("--headless", action="store_true", help="build without SDL (no window)")

    pe = sub.add_parser("extract", help="extract a disc's file system")
    pe.add_argument("input")
    pe.add_argument("-o", "--output", required=True)
    pe.add_argument("--common-key")

    pd = sub.add_parser("disasm", help="disassemble from an address")
    pd.add_argument("input")
    pd.add_argument("address")
    pd.add_argument("count", nargs="?", type=int, default=32)
    pd.add_argument("--common-key")

    a = p.parse_args(argv)
    if a.cmd in (None, "gui"):
        from .gui import run_gui
        return run_gui(getattr(a, "input", None))

    from .disc import UnsupportedFormat, open_source
    try:
        if a.cmd == "info":
            from .dol import DOL
            src = open_source(a.input, a.common_key)
            print(src.describe())
            print(DOL(src.dol).summary())
            return 0
        if a.cmd == "extract":
            src = open_source(a.input, a.common_key)
            if src.volume is None:
                raise SystemExit("error: input has no file system")
            src.volume.extract_all(a.output, progress=lambda p: print("  " + p))
            return 0
        if a.cmd == "disasm":
            from .dol import DOL
            from .ppc import decode
            dol = DOL(open_source(a.input, a.common_key).dol)
            addr = int(a.address, 0)
            for k in range(a.count):
                w = dol.read32(addr + 4 * k)
                if w is None:
                    break
                print(f"{addr + 4 * k:08X}  {w:08X}  {decode(w, addr + 4 * k)}")
            return 0
        if a.cmd == "recomp":
            from .project import recompile
            cfg = _cfg_from_args(a)
            res = recompile(cfg)
            for t in a.build or []:
                from .builder import build
                build(res.output, t)
            return 0
        if a.cmd == "build":
            from .builder import build
            build(os.path.abspath(a.project), a.target, build_type="Debug" if a.debug else "Release",
                  platform_layer="headless" if a.headless else None)
            return 0
    except (UnsupportedFormat, KeyError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        from .builder import BuildError
        if isinstance(exc, BuildError):
            print(f"build error: {exc}", file=sys.stderr)
            return 1
        raise
    return 0

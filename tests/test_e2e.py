"""End-to-end: synthetic disc -> recompile -> build with CMake -> run natively -> check results."""
from __future__ import annotations

import os
import shutil
import struct
import subprocess

import pytest

from gcwii_recomp.project import ProjectConfig, recompile

pytestmark = pytest.mark.skipif(shutil.which("cmake") is None, reason="needs CMake and a C compiler")


def _build_and_run(project: str, exe: str, dump: str, extra=()) -> subprocess.CompletedProcess:
    gen = ["-G", "Ninja"] if shutil.which("ninja") else []
    subprocess.run(["cmake", "-S", project, "-B", os.path.join(project, "build"), *gen,
                    "-DGCRT_PLATFORM=headless", "-DCMAKE_BUILD_TYPE=Release"], check=True, capture_output=True)
    subprocess.run(["cmake", "--build", os.path.join(project, "build"), "--config", "Release"],
                   check=True, capture_output=True)
    candidates = [os.path.join(project, "build", sub, exe + ext) for sub in ("", "Release")
                  for ext in ("", ".exe")]
    path = next(c for c in candidates if os.path.isfile(c))
    return subprocess.run([path, "--headless", "--dump", dump, *extra],
                          capture_output=True, text=True, timeout=60)


def test_instruction_semantics(tmp_path):
    from sample_program import EXPECTED, EXPECTED_DOUBLE_AT_40, build_disc
    iso = tmp_path / "test.iso"
    syms, _ = build_disc(str(iso))
    (tmp_path / "syms.map").write_text(syms)
    res = recompile(ProjectConfig(input=str(iso), output=str(tmp_path / "out"), symbols=str(tmp_path / "syms.map")),
                    log=lambda s: None)
    assert res.unknown_ops == []
    dump = tmp_path / "dump.bin"
    p = _build_and_run(res.output, "GTSE01", f"0x80400000:0x60:{dump}")
    assert p.returncode == 0, p.stderr
    assert "fib=55 ok 1.50" in p.stdout          # OSReport routed to native HLE
    data = dump.read_bytes()
    for off, want in EXPECTED.items():
        got = struct.unpack_from(">I", data, off)[0]
        assert got == want, f"offset {off}: got 0x{got:08X}, expected 0x{want:08X}"
    assert struct.unpack_from(">d", data, 40)[0] == EXPECTED_DOUBLE_AT_40


def test_exceptions_and_thread_switch(tmp_path):
    from os_program import EXPECTED, build
    dol, syms = build()
    (tmp_path / "os.dol").write_bytes(dol)
    (tmp_path / "syms.map").write_text(syms)
    res = recompile(ProjectConfig(input=str(tmp_path / "os.dol"), output=str(tmp_path / "out"), name="ostest",
                                  symbols=str(tmp_path / "syms.map")), log=lambda s: None)
    dump = tmp_path / "dump.bin"
    p = _build_and_run(res.output, "ostest", f"0x80400000:0x20:{dump}")
    assert p.returncode == 0, p.stderr
    data = dump.read_bytes()
    for off, want in EXPECTED.items():
        got = struct.unpack_from(">I", data, off)[0]
        assert got == want, f"offset {off}: got 0x{got:08X}, expected 0x{want:08X}"

"""Drive CMake to build a generated project for Windows, Linux or Android."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from typing import Callable

TARGETS = ("native", "linux", "windows", "android")

MINGW_TOOLCHAIN = """set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)
set(CMAKE_C_COMPILER x86_64-w64-mingw32-gcc)
set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)
set(CMAKE_FIND_ROOT_PATH /usr/x86_64-w64-mingw32)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
"""


class BuildError(Exception):
    pass


def host_os() -> str:
    s = platform.system().lower()
    return "windows" if s.startswith("win") else ("macos" if s == "darwin" else "linux")


def find_ndk() -> str | None:
    for var in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "ANDROID_NDK"):
        v = os.environ.get(var)
        if v and os.path.isdir(v):
            return v
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk and os.path.isdir(os.path.join(sdk, "ndk")):
        versions = sorted(os.listdir(os.path.join(sdk, "ndk")))
        if versions:
            return os.path.join(sdk, "ndk", versions[-1])
    return None


def available_targets() -> dict[str, str]:
    """Return target -> human readable status."""
    st = {}
    have_cmake = shutil.which("cmake") is not None
    h = host_os()
    st["native"] = "ready" if have_cmake else "needs CMake"
    st["linux"] = ("ready" if h == "linux" and have_cmake else
                   "build on a Linux host (or WSL)")
    if h == "windows":
        st["windows"] = "ready" if have_cmake else "needs CMake + Visual Studio or MinGW"
    else:
        st["windows"] = "ready (MinGW cross)" if shutil.which("x86_64-w64-mingw32-gcc") and have_cmake \
            else "install mingw-w64 to cross-compile"
    st["android"] = "ready" if find_ndk() and have_cmake else "set ANDROID_NDK_HOME (Android NDK r23+)"
    return st


def _run(cmd: list[str], cwd: str, log: Callable[[str], None]) -> None:
    log("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip())
    if proc.wait() != 0:
        raise BuildError(f"command failed ({proc.returncode}): {' '.join(cmd)}")


def build(project_dir: str, target: str = "native", log: Callable[[str], None] = print,
          build_type: str = "Release", jobs: int | None = None, extra_args: list[str] | None = None,
          platform_layer: str | None = None) -> str:
    """Configure and build. Returns the build directory."""
    if target not in TARGETS:
        raise BuildError(f"unknown target {target!r}; choose from {', '.join(TARGETS)}")
    if not shutil.which("cmake"):
        raise BuildError("CMake was not found on PATH (https://cmake.org/download/)")
    h = host_os()
    if target == "native":
        target = h if h != "macos" else "native"
    bdir = os.path.join(project_dir, f"build-{target}")
    os.makedirs(bdir, exist_ok=True)
    args = ["cmake", "-S", project_dir, "-B", bdir, f"-DCMAKE_BUILD_TYPE={build_type}"]
    if shutil.which("ninja") and not (target == "windows" and h == "windows"):
        args += ["-G", "Ninja"]
    if platform_layer:
        args.append(f"-DGCRT_PLATFORM={platform_layer}")

    if target == "linux" and h != "linux":
        raise BuildError("Linux builds must run on Linux (or WSL on Windows)")
    if target == "windows" and h != "windows":
        if not shutil.which("x86_64-w64-mingw32-gcc"):
            raise BuildError("Cross-compiling for Windows needs mingw-w64 (x86_64-w64-mingw32-gcc)")
        tc = os.path.join(bdir, "mingw-toolchain.cmake")
        with open(tc, "w") as f:
            f.write(MINGW_TOOLCHAIN)
        args.append(f"-DCMAKE_TOOLCHAIN_FILE={tc}")
    if target == "android":
        ndk = find_ndk()
        if not ndk:
            raise BuildError("Android NDK not found: set ANDROID_NDK_HOME")
        args += [f"-DCMAKE_TOOLCHAIN_FILE={os.path.join(ndk, 'build', 'cmake', 'android.toolchain.cmake')}",
                 "-DANDROID_ABI=arm64-v8a", "-DANDROID_PLATFORM=android-24", "-DGCRT_PLATFORM=android"]
        log("note: this produces lib<game>.so; open the generated 'android' folder in Android Studio "
            "(or run gradle) to package an installable APK.")
    args += extra_args or []
    _run(args, project_dir, log)
    bargs = ["cmake", "--build", bdir, "--config", build_type]
    if jobs:
        bargs += ["--parallel", str(jobs)]
    else:
        bargs += ["--parallel"]
    _run(bargs, project_dir, log)
    log(f"build finished: {bdir}")
    return bdir

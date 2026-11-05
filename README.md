# GC/Wii Native Recomp

A desktop app and command-line tool that turns **GameCube and Wii discs you own** into native
**Windows, Linux and Android** programs by *static recompilation*. Each PowerPC function in the
game's executable is translated ahead of time into C, then compiled by a normal C compiler for the
target platform. It does not interpret the game's code at run time.

```
 disc image ──► read disc / decrypt Wii partition ──► main.dol
            ──► find functions, CFG, jump tables  ──► C code (1 C function per guest function)
            ──► + gcrt runtime (memory, OS/thread glue, hardware HLE, SDL2/Android/headless)
            ──► CMake ──► game.exe / game (ELF) / libgame.so + APK project
```

> **Status: early.** The CPU recompiler, disc handling, build pipeline and the runtime core work
> and are tested end to end. The runtime has no 3D renderer yet, and audio (the DSP) is stubbed, so
> commercial games will not be playable yet. They boot as far as their hardware use allows. See
> [Status](#status) and [docs/ROADMAP.md](docs/ROADMAP.md).

## Quick start

Requirements: Python 3.10+, CMake 3.16+, and a C compiler: Visual Studio or MinGW on Windows,
GCC or Clang on Linux, or the Android NDK for Android. Tkinter is needed for the GUI. It ships with
Python on Windows and macOS; on Linux, install `python3-tk`. Installing `cryptography` makes Wii
decryption much faster; it's optional.

```sh
pip install -e .            # or just run from the checkout: python -m gcwii_recomp
gcwii-recomp                # opens the desktop app
```

In the app, choose your disc image, an output folder and the targets you want, then click
**Recompile & Build**. The result is a self-contained folder:

```
MyGame-native/
  CMakeLists.txt        builds the game for any platform
  generated/            recompiled C (recomp.h, funcs_XXXX.c, func_table.c)
  runtime/              copy of the gcrt runtime sources
  assets/main.dol       the executable's data sections (loaded at start-up)
  android/              Gradle project (NativeActivity) for packaging an APK
  build-linux/ build-windows/ ...
  recomp_report.json    functions found, coverage, HLE hooks, warnings
  gcwii-project.toml    settings to re-run / tweak the recompilation
```

### Command line

```sh
gcwii-recomp info  game.iso                      # disc header, FST, DOL sections
gcwii-recomp recomp game.iso -o out -s game.map --build native --build windows
gcwii-recomp build out --target android          # needs ANDROID_NDK_HOME
gcwii-recomp extract game.iso -o files/          # dump the file system
gcwii-recomp disasm game.iso 0x80003100 40
```

Run the result with `out/build-linux/GAMEID` (or the `.exe`). Runtime options include
`--disc <iso>`, `--headless`, `--scale 3` and `-v` (logs unhandled hardware access). Keyboard
controls: arrow keys = stick, X/Z/C/S = A/B/X/Y, Enter = Start, Q/W = L/R, D = Z, TFGH = D-pad,
IJKL = C-stick, F11 = fullscreen. SDL game controllers are supported.

### Inputs

| Input | Supported |
|---|---|
| GameCube `.iso` / `.gcm` | yes |
| Wii `.iso` (single- or dual-layer) | yes; needs **your** console's common key (see below) |
| Dolphin "Extract Entire Disc" folder (`sys/main.dol`) | yes |
| Bare `.dol` (homebrew) | yes |
| RVZ / WIA / WBFS / CISO / GCZ / NKit | convert to ISO first in Dolphin (*Convert File...*) |

**Wii common key.** Wii game partitions are encrypted. This tool does **not** include any keys.
Dump `keys.bin` from your own console with BootMii and pass the file (or the 32-digit hex key) with
`--common-key` or the GUI field, or set `GCWII_COMMON_KEY`. Keys are never written to the project
config.

### Symbols (strongly recommended)

Retail games ship without symbols. A symbol map names the generated functions, seeds function
discovery, and lets the recompiler replace SDK functions with native code (`OSReport` → host
stdout, `OSPanic` → error dialog, `exit`, and optionally fast `memcpy`/`memset`/`strlen`).
Supported formats:

- Dolphin `.map` exports. *Symbols → Generate Symbols From → Signature Database* in Dolphin's
  debugger, then *Save Symbol Map*.
- decomp-toolkit `symbols.txt` (from the many GameCube/Wii decompilation projects)
- CodeWarrior linker maps, or plain `address name` lists

Extra replacements go in `gcwii-project.toml`:

```toml
[hle.overrides]
"VIWaitForRetrace" = "hle_VIWaitForRetrace"
"0x8000A1B4" = "hle_nop"
```

## How it works

- **Disc layer** (`gcwii_recomp/disc`): GCM header, FST and apploader parsing. For Wii, it parses
  the partition table and ticket, decrypts the title key, and AES-CBC decrypts each 0x8000 cluster.
- **Decoder** (`gcwii_recomp/ppc`): the complete Gekko/Broadway instruction set, including paired
  singles, `psq_*` quantized loads/stores and supervisor instructions.
- **Analysis** (`analysis.py`): function discovery from the entry point, `bl` targets, symbols,
  code pointers in data, and gap filling. It runs until nothing new is found. It also does
  intra-function CFG and recognises CodeWarrior/GCC jump tables.
- **Code generation** (`codegen.py`): one C function per guest function. Branches become `goto`,
  `bl` a direct call, `bctr` a `switch`, and indirect calls a table lookup. CR/XER/FPSCR semantics,
  carry and overflow, single rounding and GQR quantization all follow the hardware. Loop back-edges
  get a cheap interrupt poll.
- **Runtime** (`runtime/`, C11, no dependencies except optional SDL2):
  - Guest memory is one host mapping, kept big-endian. MEM1, MEM2 and locked cache map to fixed
    offsets.
  - The boot code does the IPL/apploader's job: it loads the DOL, places the FST, and sets
    low-memory globals.
  - Exceptions go through the OS's own handler table at `0x80003000`: the decrementer, and external
    interrupts from VI/DI/SI/EXI/DSP/PE/IPC.
  - Guest **threads**: `OSSaveContext` call sites get a `setjmp` resume point, and `rfi`/
    `OSLoadContext` switches between host threads with a baton, so only one guest thread runs at a
    time.
  - Hardware: VI (XFB → window), DI (disc reads), SI (controllers), EXI and AI stubs, ARAM DMA,
    a GX FIFO decoder with exact vertex sizing and PE draw-done/token interrupts, and minimal Wii
    IOS IPC.
  - Platform layers: SDL2 (Windows/Linux/macOS), Android NativeActivity (no Java code), and
    headless.

More detail is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Status

| Area | State |
|---|---|
| Disc reading (GC ISO, Wii ISO decryption, FST extract) | done, tested |
| PowerPC decoder + C generator (integer, FPU, paired singles, system) | done, tested natively |
| Jump tables, indirect calls, tail calls, function pointer discovery | done, tested |
| Exceptions / decrementer / interrupts through the OS handler table | done, tested with a synthetic OS |
| Guest thread switching (`OSSaveContext` / `rfi`) | done, tested with a synthetic OS |
| Windows (MinGW cross build, run under Wine) / Linux builds | done, tested |
| Android NativeActivity layer + Gradle project | written, **not yet built against a real NDK** |
| GUI (Tkinter) | done, tested under Xvfb |
| GX: FIFO decode, PE interrupts | done; **3D renderer backend not implemented** (EFB copies show the clear colour only) |
| DSP / audio (AX ucode HLE) | **stub**: mailbox handshake and ARAM DMA only |
| Memory cards, EXI devices, Wii IOS beyond `/dev/di` | **stub** |
| Commercial game compatibility | **none confirmed yet** |

## Legal

This project contains no Nintendo code, keys or game data. Use it only with discs you own. The
generated projects contain code derived from your game, so don't redistribute them.

## Development

```sh
pip install pytest
python -m pytest tests   # builds synthetic discs, recompiles them, compiles with CMake and runs them
```

The test-suite assembles PowerPC programs with a small built-in assembler (`tests/ppcasm.py`), so no
devkitPPC is needed.

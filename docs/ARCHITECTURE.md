# Architecture

## Pipeline (`gcwii_recomp/project.py`)

1. `disc.open_source()` detects the input (GC ISO, Wii ISO, extracted folder, DOL) and returns
   the DOL bytes and a `Volume` (a readable, decrypted game-data space).
2. `DOL` parses up to 7 text and 11 data sections.
3. `Analyzer` finds functions and their control flow (see below).
4. `codegen.generate()` writes `recomp.h`, `funcs_NNNN.c` (400 functions per file by default) and
   `func_table.c`. The table is a sorted address → function pointer array plus the `gcrt_game`
   descriptor.
5. The runtime is copied next to the generated code, along with a top-level `CMakeLists.txt` and an
   Android Gradle project. The result builds on any machine without this tool installed.

## Function discovery

Seeds are added in this order:

1. the entry point
2. symbols
3. config `extra_functions` and HLE targets
4. every `bl` target
5. 32-bit words in any section that point at a plausible function start in text

For step 5, a start is plausible if the previous instruction is a terminator or padding, or the
target begins with `stwu`/`mflr`. Jump-table entries are excluded.

Each function is explored over its intra-procedural CFG, bounded by the next known start:

- An unconditional `b` to another start is a tail call.
- A `b` that leaves the bounds seeds a new function.
- `bctr` preceded by the `lis/addi/(slwi)/lwzx/mtctr` idiom is decoded as a jump table, using the
  preceding `cmplwi` as the bound.

Gaps not covered by any function are seeded at their first non-padding instruction, and the whole
process repeats until no new seeds appear.

## Generated code contract

```c
void rc_Name(PPCContext* ctx);   // every guest function, prefixed to avoid libc clashes
```

- GPRs, FPRs (ps0) and ps1 live in `ctx`. Every memory access goes through `mem_rN`/`mem_wN`,
  which byte-swap and route `0xCC/0xCD......` addresses to MMIO.
- `bl X` → `ctx->lr = ret; rc_X(ctx);`. `blr` → `return;`. Indirect calls (`bctrl`, `blrl`, a
  `bctr` that is not a jump table) → `gcrt_call(ctx, addr)`. That call is a direct-mapped table
  lookup and stops with a fatal error naming the address if it was not recompiled; add the address
  to `[analysis] extra_functions` if that happens.
- Backward branches call `GCRT_CHECK_INTERRUPTS(ctx)`. It is one predictable load and branch, and
  the host loop sets its flag every millisecond.
- Calls to `OSSaveContext` capture a `setjmp` in the caller's frame, so a later `rfi` into that
  context resumes with `r3 = 1`, just as on hardware.
- HLE-replaced functions are emitted twice: `rc_X__orig` keeps the recompiled body, and `rc_X`
  forwards to `hle_*`.

## Threads and exceptions

The GameCube OS keeps its exception table at `0x80003000` and the current `OSContext*` at
`0x800000D4`.

`gcrt_take_exception()` emulates the low-level vector stub:

1. Save the CPU state into the current context.
2. `setjmp`.
3. Call the OS handler with `r3 = exception number` and `r4 = context`.

The handler ends in `OSLoadContext` → `rfi`. `gcrt_rfi()` then looks at the context being loaded:

- The context has a live resume point on this host thread: `longjmp` to it.
- It has one on another host thread: pass the baton to that thread.
- It has never run before (a new `OSThread`): start a host thread at `SRR0`.

Only the thread holding the baton runs, so the single global `PPCContext` stays coherent. FPRs are
saved and restored per context on the host side, because the OS's lazy FPU switching relies on the
FP-unavailable exception, which recompiled code never raises.

## Hardware (`runtime/src/hw.c`, `gx.c`)

Devices complete requests immediately and raise the matching PI interrupt bit:

- VI scans out the external framebuffer (YUV422) to the platform layer at 59.94 Hz.
- DI and Wii `/dev/di` read the disc image.
- SI reports host controllers in the SDK's pad format.
- The GX front end parses the command stream and calls a `GXBackend` with decoded primitives. It
  handles BP/CP/XF loads, display lists, and draws with exact vertex sizes computed from VCD/VAT.
- PE finish/token interrupts let `GXDrawDone` return.

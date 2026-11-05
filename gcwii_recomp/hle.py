"""Mapping of well-known SDK / libc symbols to the runtime's native HLE functions."""
from __future__ import annotations

# Always applied when the symbol is known: harmless, and they make output visible.
DEFAULT_HLE = {
    "OSReport": "hle_OSReport",
    "OSPanic": "hle_OSPanic",
    "OSFatal": "hle_OSFatal",
    "printf": "hle_printf",
    "puts": "hle_puts",
    "exit": "hle_exit",
    "abort": "hle_abort",
}

# Opt-in ([hle] fast_libc = true): native replacements for hot libc routines.
FAST_LIBC = {
    "memcpy": "hle_memcpy",
    "memmove": "hle_memmove",
    "memset": "hle_memset",
    "strlen": "hle_strlen",
}

# Functions available to `[hle.overrides]` by name.
BUILTIN = sorted(set(DEFAULT_HLE.values()) | set(FAST_LIBC.values()) |
                 {"hle_nop", "hle_return0", "hle_return1", "hle_VIWaitForRetrace"})

# First instruction of the SDK's OSSaveContext: `stmw r13, 0x34(r3)`.
OSSAVECONTEXT_FIRST_WORD = 0xBDA30034

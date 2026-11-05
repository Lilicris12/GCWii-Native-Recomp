"""Synthetic program exercising exception delivery and guest thread switching.

It mimics the pieces of the GameCube OS the runtime relies on:
  * the exception table at 0x80003000 and the current-context pointer at 0x800000D4
  * an OSSaveContext-shaped function (recognised by its first instruction)
  * OSLoadContext-style `rfi` to start a new thread and to switch back
"""
from __future__ import annotations

from gcwii_recomp.dol import build_dol
from ppcasm import Asm

TEXT = 0x80003400
RESULTS = 0x80400000
CTX_A = 0x80500000
CTX_B = 0x80501000
STACK_B = 0x80600000

EXPECTED = {
    0: 1,          # decrementer handler ran once
    4: 0xB,        # second thread ran
    8: 0xA,        # main thread resumed from OSSaveContext returning 1
    12: 1,         # the value OSSaveContext "returned" on resume
    16: 0x1234,    # callee-saved register restored across the switch
}


def build():
    a = Asm(TEXT)
    a.label("_start")
    a.load32(1, 0x80F00000)
    a.load32(31, RESULTS)
    # current context = CTX_A, decrementer handler in the OS exception table
    a.load32(3, CTX_A); a.load32(4, 0x800000D4); a.stw(3, 0, 4)
    a.load32(4, 0x80003000 + 8 * 4); a.load_addr(5, "dec_handler"); a.stw(5, 0, 4)
    # arm decrementer and enable external interrupts (MSR[EE])
    a.li(6, 1000); a.mtspr(22, 6)
    a.emit(0x7C0000A6 | (7 << 21))                       # mfmsr r7
    a.ori(7, 7, 0x8000); a.emit(0x7C000124 | (7 << 21))   # mtmsr r7
    a.label("wait"); a.lwz(8, 0, 31); a.cmpwi(0, 8, 0); a.beq("wait")

    # --- thread switch ---
    a.li(25, 0x1234)
    a.load32(3, CTX_A); a.bl("save_context")
    a.cmpwi(0, 3, 0); a.bne("resumed")
    # prepare context B: srr0 = thread_b, r1 = its stack, then "OSLoadContext(B)"
    a.load32(3, CTX_B); a.load32(4, 0x800000D4); a.stw(3, 0, 4)
    a.load_addr(5, "thread_b"); a.stw(5, 0x198, 3)
    a.lwz(5, 0x198, 3); a.mtspr(26, 5)                    # srr0
    a.emit(0x7C0000A6 | (6 << 21)); a.mtspr(27, 6)        # srr1 = msr
    a.load32(1, STACK_B)
    a.emit(0x4C000064)                                    # rfi
    a.label("resumed")
    a.stw(3, 12, 31)
    a.li(6, 0xA); a.stw(6, 8, 31)
    a.stw(25, 16, 31)
    a.li(3, 0); a.bl("exit")
    a.label("spin"); a.b("spin")

    # decrementer exception handler: r3 = exception, r4 = context
    a.label("dec_handler")
    a.load32(9, RESULTS); a.lwz(10, 0, 9); a.addi(10, 10, 1); a.stw(10, 0, 9)
    # OSLoadContext(r4): srr0/srr1 from the context, restore r9/r10 not needed here
    a.lwz(5, 0x198, 4); a.mtspr(26, 5); a.lwz(5, 0x19C, 4); a.mtspr(27, 5)
    a.lwz(1, 4, 4)
    a.emit(0x4C000064)                                    # rfi

    # second thread
    a.label("thread_b")
    a.load32(9, RESULTS); a.li(10, 0xB); a.stw(10, 4, 9)
    a.li(25, 0)                                           # clobber callee-saved reg
    # switch back to A: current = CTX_A, reload r3/r25/r1/srr0 from it, rfi
    a.load32(4, CTX_A); a.load32(5, 0x800000D4); a.stw(4, 0, 5)
    a.lwz(25, 0x64, 4); a.lwz(1, 4, 4); a.lwz(3, 12, 4)
    a.lwz(5, 0x198, 4); a.mtspr(26, 5); a.lwz(5, 0x19C, 4); a.mtspr(27, 5)
    a.emit(0x4C000064)                                    # rfi

    # OSSaveContext-shaped: stmw r13,0x34(r3) is the signature the recompiler looks for
    a.label("save_context")
    a.stmw(13, 0x34, 3)
    a.mflr(0); a.stw(0, 0x84, 3); a.stw(0, 0x198, 3)
    a.emit(0x7C0000A6); a.stw(0, 0x19C, 3)              # mfmsr r0 -> srr1
    a.stw(1, 4, 3); a.stw(2, 8, 3)
    a.li(0, 1); a.stw(0, 12, 3)
    a.li(3, 0); a.blr()

    a.label("exit"); a.b("exit")
    text = a.assemble()
    symbols = f"{a.addr('exit'):08X} 000004 {a.addr('exit'):08X} 0 exit\n"
    return build_dol([(TEXT, text)], [], TEXT), symbols

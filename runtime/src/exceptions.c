/*
 * Exceptions, interrupts, the decrementer and guest-thread switching.
 *
 * The GameCube/Wii OS keeps a table of exception handlers at 0x80003000 and the
 * current OSContext pointer at 0x800000D4.  Interrupts are delivered at "safe
 * points" (loop back-edges, HLE calls) by saving CPU state into the current
 * OSContext and calling the OS handler, exactly like the low-level vector stub
 * would.  Guest threads (OSThread) each run on their own host thread, but only
 * one runs at a time (a baton is passed on every context switch), so the CPU
 * state in the single global PPCContext stays coherent.
 */
#include "gcrt_internal.h"

#include <stdlib.h>
#include <stdio.h>

#define OS_EXCEPTION_TABLE 0x80003000u
#define OS_CURRENT_CONTEXT 0x800000D4u
#define OS_CURRENT_CONTEXT_PHYS 0x800000C0u

/* OSContext layout */
#define CTX_GPR 0x000
#define CTX_CR 0x080
#define CTX_LR 0x084
#define CTX_CTR 0x088
#define CTX_XER 0x08C
#define CTX_FPSCR 0x194
#define CTX_SRR0 0x198
#define CTX_SRR1 0x19C
#define CTX_STATE 0x1A2
#define CTX_GQR 0x1A4

volatile int gcrt_poll_pending;

/* ------------------------------------------------------------------------ */
/* Timebase / decrementer                                                    */
/* ------------------------------------------------------------------------ */
static uint64_t g_boot_ns;
static uint32_t g_dec_value;
static uint64_t g_dec_set_tb;
static int g_dec_armed;

uint32_t gcrt_tb_hz(void) { return gcrt_game.is_wii ? 60750000u : 40500000u; }

uint64_t gcrt_host_ticks_to_tb(uint64_t ns) {
    uint64_t rel = ns - g_boot_ns;
    uint64_t hz = gcrt_tb_hz();
    return (rel / 1000000000ull) * hz + (rel % 1000000000ull) * hz / 1000000000ull;
}

uint32_t gcrt_get_dec(void) {
    return g_dec_value - (uint32_t)(gcrt_timebase() - g_dec_set_tb);
}

void gcrt_set_dec(uint32_t v) {
    g_dec_value = v;
    g_dec_set_tb = gcrt_timebase();
    g_dec_armed = 1;
    if ((int32_t)v < 0) gcrt_poll_pending = 1;
}

/* ------------------------------------------------------------------------ */
/* Interrupt lines (PI)                                                      */
/* ------------------------------------------------------------------------ */
void gcrt_raise_irq(uint32_t cause) {
    extern uint32_t gcrt_pi_intsr_reg;
    gcrt_pi_intsr_reg |= cause;
    gcrt_poll_pending = 1;
}

void gcrt_clear_irq(uint32_t cause) {
    extern uint32_t gcrt_pi_intsr_reg;
    gcrt_pi_intsr_reg &= ~cause;
}

/* ------------------------------------------------------------------------ */
/* Context records + baton passing                                           */
/* ------------------------------------------------------------------------ */
typedef struct HostThread {
    GCRTCond* cond;
    int run;
    struct CtxRecord* resume;      /* record to longjmp into once woken */
} HostThread;

typedef struct CtxRecord {
    uint32_t addr;
    HostThread* host;
    jmp_buf jb;
    int has_jb;
    double f[32], ps1[32];
    uint32_t fpscr;
    struct CtxRecord* next;
} CtxRecord;

static GCRTMutex* g_baton;
static CtxRecord* g_records;
static CtxRecord* g_running;   /* record of the context whose host thread holds the baton */

#if defined(_MSC_VER)
static __declspec(thread) HostThread* t_self;
#else
static _Thread_local HostThread* t_self;
#endif

static HostThread* host_self(void) {
    if (!t_self) {
        t_self = (HostThread*)calloc(1, sizeof(HostThread));
        t_self->cond = gcrt_cond_create();
        t_self->run = 1;
    }
    return t_self;
}

static CtxRecord* record_for(uint32_t addr, int create) {
    for (CtxRecord* r = g_records; r; r = r->next)
        if (r->addr == addr) return r;
    if (!create) return NULL;
    CtxRecord* r = (CtxRecord*)calloc(1, sizeof *r);
    r->addr = addr;
    r->next = g_records;
    g_records = r;
    return r;
}

static void save_fprs(CtxRecord* r, PPCContext* ctx) {
    if (!r) return;
    memcpy(r->f, ctx->f, sizeof r->f);
    memcpy(r->ps1, ctx->ps1, sizeof r->ps1);
    r->fpscr = ctx->fpscr;
}

static void restore_fprs(CtxRecord* r, PPCContext* ctx) {
    memcpy(ctx->f, r->f, sizeof r->f);
    memcpy(ctx->ps1, r->ps1, sizeof r->ps1);
    ctx->fpscr = r->fpscr;
}

void gcrt_threads_init(void) {
    g_baton = gcrt_mutex_create();
    host_self();
}

/* Block the calling host thread until another context switch hands it the baton. */
static GCRT_NORETURN void park_and_resume(PPCContext* ctx) {
    HostThread* me = host_self();
    gcrt_mutex_lock(g_baton);
    while (!me->run) gcrt_cond_wait(me->cond, g_baton);
    CtxRecord* target = me->resume;
    me->resume = NULL;
    gcrt_mutex_unlock(g_baton);
    g_running = target;
    restore_fprs(target, ctx);
    target->has_jb = 0;
    longjmp(target->jb, 1);
}

static void hand_baton(HostThread* to, CtxRecord* resume) {
    HostThread* me = host_self();
    gcrt_mutex_lock(g_baton);
    me->run = 0;
    to->resume = resume;
    to->run = 1;
    gcrt_cond_broadcast(to->cond);
    gcrt_mutex_unlock(g_baton);
}

typedef struct NewThreadArgs { CtxRecord* rec; HostThread* host; uint32_t pc; } NewThreadArgs;

static void guest_thread_main(void* p) {
    NewThreadArgs* a = (NewThreadArgs*)p;
    HostThread* me = a->host;
    t_self = me;
    gcrt_mutex_lock(g_baton);
    while (!me->run) gcrt_cond_wait(me->cond, g_baton);
    me->resume = NULL;
    gcrt_mutex_unlock(g_baton);

    PPCContext* ctx = gcrt_cpu;
    g_running = a->rec;
    uint32_t pc = a->pc;
    free(a);
    gcrt_call(ctx, pc);
    /* Thread entry returned: continue at LR (OSCreateThread points it at OSExitThread). */
    gcrt_call(ctx, ctx->lr);
    gcrt_fatal("guest thread returned past OSExitThread (LR=0x%08X)", ctx->lr);
}

/* Called from generated code right after a call to OSSaveContext. */
jmp_buf* gcrt_context_resume_slot(uint32_t context_addr) {
    CtxRecord* r = record_for(context_addr, 1);
    r->host = host_self();
    r->has_jb = 1;
    save_fprs(r, gcrt_cpu);
    if (!g_running) g_running = r;
    return &r->jb;
}

void gcrt_context_resumed(PPCContext* ctx) {
    ctx->r[3] = 1;   /* OSSaveContext "returns" 1 when the context is reloaded */
}

/* rfi: return from exception / OSLoadContext. Never returns. */
void gcrt_rfi(PPCContext* ctx, uint32_t addr) {
    (void)addr;
    ctx->msr = ctx->srr1;
    if (ctx->msr & GCRT_MSR_EE) gcrt_poll_pending = 1;
    uint32_t target_addr = mem_r32(OS_CURRENT_CONTEXT);
    uint32_t pc = ctx->srr0;
    CtxRecord* target = target_addr ? record_for(target_addr, 1) : NULL;
    HostThread* me = host_self();

    if (target && target->has_jb) {
        if (target->host == me) {
            g_running = target;
            restore_fprs(target, ctx);
            target->has_jb = 0;
            longjmp(target->jb, 1);
        }
        save_fprs(g_running, ctx);
        hand_baton(target->host, target);
        park_and_resume(ctx);
    }

    if (target && !target->host) {
        /* First time this context runs: start a fresh guest thread at SRR0. */
        save_fprs(g_running, ctx);
        save_fprs(target, ctx);
        NewThreadArgs* a = (NewThreadArgs*)malloc(sizeof *a);
        HostThread* h = (HostThread*)calloc(1, sizeof *h);
        h->cond = gcrt_cond_create();
        target->host = h;
        a->rec = target;
        a->host = h;
        a->pc = pc;
        if (!gcrt_thread_start(guest_thread_main, a)) gcrt_fatal("failed to start host thread for guest context");
        hand_baton(h, NULL);
        park_and_resume(ctx);
    }

    /* No resume point known: treat as a jump on the current host stack. */
    if (gcrt_opts.verbose)
        gcrt_log("rfi to 0x%08X without saved resume point (context 0x%08X) - jumping", pc, target_addr);
    gcrt_call(ctx, pc);
    gcrt_fatal("execution returned from rfi target 0x%08X", pc);
}

/* ------------------------------------------------------------------------ */
/* Exception entry                                                           */
/* ------------------------------------------------------------------------ */
void gcrt_take_exception(PPCContext* ctx, int exc, uint32_t resume_pc) {
    uint32_t handler = mem_r32(OS_EXCEPTION_TABLE + 4u * (uint32_t)exc);
    uint32_t c = mem_r32(OS_CURRENT_CONTEXT);
    if (!handler || !c || !gcrt_lookup(handler)) {
        if (gcrt_opts.verbose) gcrt_log("exception %d ignored (no OS handler installed)", exc);
        if (exc == GCRT_EXC_EXTERNAL) {
            /* nobody is listening yet: drop interrupt lines to avoid a storm */
        }
        return;
    }
    for (int i = 0; i < 32; i++) mem_w32(c + CTX_GPR + 4u * i, ctx->r[i]);
    mem_w32(c + CTX_CR, ctx->cr);
    mem_w32(c + CTX_LR, ctx->lr);
    mem_w32(c + CTX_CTR, ctx->ctr);
    mem_w32(c + CTX_XER, ppc_get_xer(ctx));
    mem_w32(c + CTX_FPSCR, ctx->fpscr);
    mem_w32(c + CTX_SRR0, resume_pc);
    mem_w32(c + CTX_SRR1, ctx->msr);
    mem_w16(c + CTX_STATE, (uint16_t)(mem_r16(c + CTX_STATE) | 0x2u));
    for (int i = 0; i < 8; i++) mem_w32(c + CTX_GQR + 4u * i, ctx->gqr[i]);

    PPCContext saved = *ctx;
    CtxRecord* rec = record_for(c, 1);
    rec->host = host_self();
    save_fprs(rec, ctx);
    rec->has_jb = 1;
    if (!g_running) g_running = rec;
    ctx->srr0 = resume_pc;
    ctx->srr1 = ctx->msr;
    ctx->msr &= ~(GCRT_MSR_EE | 0x0400u /* SE */ | 0x0100u /* BE */);
    if (setjmp(rec->jb) == 0) {
        ctx->r[3] = (uint32_t)exc;
        ctx->r[4] = c;
        gcrt_call(ctx, handler);
        /* handler returned without rfi (e.g. an HLE handler) */
        rec->has_jb = 0;
        uint32_t cur = ctx->cur_func;
        *ctx = saved;
        ctx->cur_func = cur;
        return;
    }
    /* Resumed via rfi: OSLoadContext restored the GPRs/SPRs already. */
}

/* ------------------------------------------------------------------------ */
/* Safe-point polling                                                        */
/* ------------------------------------------------------------------------ */
void gcrt_exceptions_init(void) {
    g_boot_ns = gcrt_platform_ticks_ns();
}

void gcrt_poll(PPCContext* ctx) {
    gcrt_poll_pending = 0;
    gcrt_hw_tick(ctx, gcrt_platform_ticks_ns());
    if (!(ctx->msr & GCRT_MSR_EE)) return;
    if (g_dec_armed && (int32_t)gcrt_get_dec() < 0) {
        g_dec_armed = 0;
        gcrt_take_exception(ctx, GCRT_EXC_DECREMENTER, ctx->cur_func);
        if (!(ctx->msr & GCRT_MSR_EE)) return;
    }
    if (gcrt_pi_intsr() & gcrt_pi_intmr())
        gcrt_take_exception(ctx, GCRT_EXC_EXTERNAL, ctx->cur_func);
}

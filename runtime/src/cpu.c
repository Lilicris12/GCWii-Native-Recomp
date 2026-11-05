/* CPU-side services for recompiled code: dispatch, FPU and paired-single helpers. */
#include "gcrt_internal.h"

#include <float.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <fenv.h>

/* ------------------------------------------------------------------------ */
/* Function dispatch                                                         */
/* ------------------------------------------------------------------------ */
static GCRTFunc* g_fast_table;     /* direct-mapped over [text_lo, text_hi) */
static uint32_t  g_fast_lo, g_fast_words;

void gcrt_dispatch_init(void) {
    g_fast_lo = gcrt_game.text_lo;
    g_fast_words = (gcrt_game.text_hi - gcrt_game.text_lo) / 4u;
    if (g_fast_words && g_fast_words < (64u << 20)) {
        g_fast_table = (GCRTFunc*)calloc(g_fast_words, sizeof(GCRTFunc));
        for (uint32_t i = 0; i < gcrt_game.func_count; i++) {
            uint32_t a = gcrt_game.funcs[i].addr;
            if (a >= g_fast_lo && (a - g_fast_lo) / 4u < g_fast_words)
                g_fast_table[(a - g_fast_lo) / 4u] = gcrt_game.funcs[i].fn;
        }
    }
}

GCRTFunc gcrt_lookup(uint32_t addr) {
    if (g_fast_table && addr >= g_fast_lo && (addr - g_fast_lo) / 4u < g_fast_words && !(addr & 3))
        return g_fast_table[(addr - g_fast_lo) / 4u];
    /* binary search (sorted table) */
    uint32_t lo = 0, hi = gcrt_game.func_count;
    while (lo < hi) {
        uint32_t mid = (lo + hi) / 2;
        uint32_t a = gcrt_game.funcs[mid].addr;
        if (a == addr) return gcrt_game.funcs[mid].fn;
        if (a < addr) lo = mid + 1; else hi = mid;
    }
    return NULL;
}

void gcrt_call(PPCContext* ctx, uint32_t addr) {
    /* Accept uncached / physical aliases of code addresses. */
    GCRTFunc f = gcrt_lookup(addr);
    if (!f && (addr & 0xF0000000u) != 0x80000000u) f = gcrt_lookup((addr & 0x1FFFFFFFu) | 0x80000000u);
    if (GCRT_UNLIKELY(!f)) {
        gcrt_fatal("Indirect branch to 0x%08X which is not a recompiled function "
                   "(called from function 0x%08X, LR=0x%08X).\n"
                   "Add it to [analysis] extra_functions in the project config and recompile.",
                   addr, ctx->cur_func, ctx->lr);
    }
    uint32_t saved = ctx->cur_func;
    f(ctx);
    ctx->cur_func = saved;
}

/* ------------------------------------------------------------------------ */
/* Faults                                                                    */
/* ------------------------------------------------------------------------ */
void gcrt_fatal(const char* fmt, ...) {
    char buf[2048];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    fprintf(stderr, "\n[gcrt] FATAL: %s\n", buf);
    gcrt_platform_message("gcrt fatal error", buf);
    gcrt_dump_state(stderr);
    gcrt_platform_shutdown();
    exit(3);
}

void gcrt_illegal(PPCContext* ctx, uint32_t addr, uint32_t word) {
    gcrt_fatal("Illegal/unsupported instruction 0x%08X at 0x%08X (function 0x%08X)", word, addr, ctx->cur_func);
}

void gcrt_trap(PPCContext* ctx, uint32_t addr) {
    gcrt_log("trap at 0x%08X (function 0x%08X) - continuing", addr, ctx->cur_func);
    gcrt_take_exception(ctx, GCRT_EXC_PROGRAM, addr + 4);
}

void gcrt_syscall(PPCContext* ctx, uint32_t addr) {
    /* The GameCube OS uses `sc` only to toggle HID0 cache bits (OSSync etc). */
    gcrt_take_exception(ctx, GCRT_EXC_SYSCALL, addr + 4);
}

void gcrt_dcbz(PPCContext* ctx, uint32_t ea) {
    (void)ctx;
    ea &= ~31u;
    if (GCRT_IS_MMIO(ea)) return;
    memset(gcrt_ptr(ea), 0, 32);
}

/* ------------------------------------------------------------------------ */
/* Special registers                                                         */
/* ------------------------------------------------------------------------ */
enum { SPR_DEC = 22, SPR_SRR0 = 26, SPR_SRR1 = 27, SPR_TBL_R = 268, SPR_TBU_R = 269,
       SPR_TBL_W = 284, SPR_TBU_W = 285, SPR_HID2 = 920, SPR_WPAR = 921, SPR_DMAU = 922,
       SPR_DMAL = 923, SPR_PVR = 287 };

static int64_t g_tb_offset;

uint64_t gcrt_timebase(void) {
    return (uint64_t)((int64_t)gcrt_host_ticks_to_tb(gcrt_platform_ticks_ns()) + g_tb_offset);
}

uint32_t gcrt_mfspr(PPCContext* ctx, uint32_t spr) {
    switch (spr) {
    case SPR_DEC: return gcrt_get_dec();
    case SPR_SRR0: return ctx->srr0;
    case SPR_SRR1: return ctx->srr1;
    case SPR_TBL_R: return (uint32_t)gcrt_timebase();
    case SPR_TBU_R: return (uint32_t)(gcrt_timebase() >> 32);
    case SPR_PVR: return gcrt_game.is_wii ? 0x00087102u : 0x00083214u;
    default: return ctx->spr[spr & 1023];
    }
}

void gcrt_mtspr(PPCContext* ctx, uint32_t spr, uint32_t v) {
    switch (spr) {
    case SPR_DEC: gcrt_set_dec(v); return;
    case SPR_SRR0: ctx->srr0 = v; return;
    case SPR_SRR1: ctx->srr1 = v; return;
    case SPR_TBL_W: {
        uint64_t tb = gcrt_timebase();
        g_tb_offset += (int64_t)(((tb & 0xFFFFFFFF00000000ull) | v) - tb);
        return;
    }
    case SPR_TBU_W: {
        uint64_t tb = gcrt_timebase();
        g_tb_offset += (int64_t)((((uint64_t)v << 32) | (tb & 0xFFFFFFFFu)) - tb);
        return;
    }
    case SPR_DMAL: {
        /* Locked-cache DMA: DMAU = mem addr | len_hi, DMAL = lc addr | load | len_lo | trigger */
        ctx->spr[spr] = v & ~3u;
        if (v & 2u) {
            uint32_t dmau = ctx->spr[SPR_DMAU];
            uint32_t mem = dmau & ~0x1Fu;
            uint32_t lc = v & ~0x1Fu;
            uint32_t blocks = (((dmau & 0x1Fu) << 2) | ((v >> 2) & 3u));
            if (!blocks) blocks = 128;
            uint32_t len = blocks * 32u;
            if (v & 0x10u) memmove(gcrt_ptr(lc), gcrt_ptr(mem), len);   /* load into LC */
            else           memmove(gcrt_ptr(mem), gcrt_ptr(lc), len);   /* store from LC */
        }
        return;
    }
    default:
        ctx->spr[spr & 1023] = v;
        return;
    }
}

void gcrt_mtmsr(PPCContext* ctx, uint32_t v) {
    uint32_t old = ctx->msr;
    ctx->msr = v;
    if ((v & GCRT_MSR_EE) && !(old & GCRT_MSR_EE)) gcrt_poll_pending = 1;
}

/* ------------------------------------------------------------------------ */
/* FPU                                                                       */
/* ------------------------------------------------------------------------ */
double ppc_fctiw(PPCContext* ctx, double x, int round_to_zero) {
    int32_t r;
    if (x != x) r = (int32_t)0x80000000;
    else if (x >= 2147483647.0) r = 0x7FFFFFFF;
    else if (x <= -2147483648.0) r = (int32_t)0x80000000;
    else if (round_to_zero) r = (int32_t)x;
    else {
        switch (ctx->fpscr & 3u) {
        case 0: r = (int32_t)nearbyint(x); break;   /* host default: round to nearest even */
        case 1: r = (int32_t)x; break;
        case 2: r = (int32_t)ceil(x); break;
        default: r = (int32_t)floor(x); break;
        }
    }
    return u2d(0xFFF8000000000000ull | (uint32_t)r);
}

double ppc_fres(double x) {
    if (x == 0.0) return copysign(HUGE_VAL, x);
    return (double)(1.0f / (float)x);
}

double ppc_frsqrte(double x) {
    if (x == 0.0) return copysign(HUGE_VAL, x);
    if (x < 0.0) return NAN;
    return 1.0 / sqrt(x);
}

void ppc_fpscr_changed(PPCContext* ctx) {
    static const int modes[4] = { FE_TONEAREST, FE_TOWARDZERO, FE_UPWARD, FE_DOWNWARD };
    fesetround(modes[ctx->fpscr & 3u]);
}

void ppc_mtfsf(PPCContext* ctx, uint32_t fm, uint64_t bits) {
    uint32_t m = 0;
    for (int k = 0; k < 8; k++)
        if (fm & (0x80u >> k)) m |= 0xF0000000u >> (4 * k);
    ctx->fpscr = (ctx->fpscr & ~m) | ((uint32_t)bits & m);
    ppc_fpscr_changed(ctx);
}

/* ------------------------------------------------------------------------ */
/* Paired-single quantization (GQR)                                          */
/* ------------------------------------------------------------------------ */
static const float* dq_table(void) {
    static float t[64];
    static int init;
    if (!init) {
        for (int i = 0; i < 64; i++) {
            int s = i < 32 ? i : i - 64;          /* 6-bit signed scale */
            t[i] = ldexpf(1.0f, -s);
        }
        init = 1;
    }
    return t;
}

static float q_load(uint32_t ea, uint32_t type, uint32_t scale, int* size) {
    const float* dq = dq_table();
    switch (type) {
    case 4: *size = 1; return (float)mem_r8(ea) * dq[scale];
    case 5: *size = 2; return (float)mem_r16(ea) * dq[scale];
    case 6: *size = 1; return (float)(int8_t)mem_r8(ea) * dq[scale];
    case 7: *size = 2; return (float)(int16_t)mem_r16(ea) * dq[scale];
    default: *size = 4; return u2f(mem_r32(ea));
    }
}

static int32_t clampi(float v, int32_t lo, int32_t hi) {
    if (v != v) return 0;
    if (v <= (float)lo) return lo;
    if (v >= (float)hi) return hi;
    return (int32_t)v;
}

static void q_store(uint32_t ea, float v, uint32_t type, uint32_t scale, int* size) {
    const float* dq = dq_table();
    float s = 1.0f / dq[scale];
    switch (type) {
    case 4: *size = 1; mem_w8(ea, (uint8_t)clampi(v * s, 0, 255)); break;
    case 5: *size = 2; mem_w16(ea, (uint16_t)clampi(v * s, 0, 65535)); break;
    case 6: *size = 1; mem_w8(ea, (uint8_t)(int8_t)clampi(v * s, -128, 127)); break;
    case 7: *size = 2; mem_w16(ea, (uint16_t)(int16_t)clampi(v * s, -32768, 32767)); break;
    default: *size = 4; mem_w32(ea, f2u(v)); break;
    }
}

void ppc_psq_load(PPCContext* ctx, int frd, uint32_t ea, int w, int qr) {
    uint32_t g = ctx->gqr[qr];
    uint32_t type = (g >> 16) & 7u, scale = (g >> 24) & 0x3Fu;
    int sz;
    float a = q_load(ea, type, scale, &sz);
    float b = w ? 1.0f : q_load(ea + (uint32_t)sz, type, scale, &sz);
    ctx->f[frd] = a;
    ctx->ps1[frd] = b;
}

void ppc_psq_store(PPCContext* ctx, int frs, uint32_t ea, int w, int qr) {
    uint32_t g = ctx->gqr[qr];
    uint32_t type = g & 7u, scale = (g >> 8) & 0x3Fu;
    int sz;
    q_store(ea, (float)ctx->f[frs], type, scale, &sz);
    if (!w) q_store(ea + (uint32_t)sz, (float)ctx->ps1[frs], type, scale, &sz);
}

/* ------------------------------------------------------------------------ */
/* String loads                                                              */
/* ------------------------------------------------------------------------ */
void ppc_lswi(PPCContext* ctx, int rd, uint32_t ea, uint32_t nb) {
    int r = rd - 1;
    uint32_t i = 0;
    while (nb > 0) {
        if (i == 0) { r = (r + 1) & 31; ctx->r[r] = 0; }
        ctx->r[r] |= (uint32_t)mem_r8(ea) << (24 - i);
        i = (i + 8) & 31;
        ea++; nb--;
    }
}

void ppc_stswi(PPCContext* ctx, int rs, uint32_t ea, uint32_t nb) {
    int r = rs - 1;
    uint32_t i = 0;
    while (nb > 0) {
        if (i == 0) r = (r + 1) & 31;
        mem_w8(ea, (uint8_t)(ctx->r[r] >> (24 - i)));
        i = (i + 8) & 31;
        ea++; nb--;
    }
}

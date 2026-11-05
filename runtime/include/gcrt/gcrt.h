/*
 * gcrt - GameCube / Wii static-recompilation runtime.
 *
 * Recompiled functions have the signature `void fn(PPCContext* ctx)` and use the
 * inline helpers in this header for guest memory access, condition-register
 * updates and paired-single math.  Guest memory is kept in big-endian order in
 * one host allocation; MMIO addresses are routed to the HLE hardware layer.
 */
#ifndef GCRT_H
#define GCRT_H

#include <stdint.h>
#include <string.h>
#include <setjmp.h>
#include <math.h>

#ifdef __cplusplus
extern "C" {
#endif

#if defined(_MSC_VER)
#include <stdlib.h>
#include <intrin.h>
#define GCRT_BSWAP16(x) _byteswap_ushort(x)
#define GCRT_BSWAP32(x) _byteswap_ulong(x)
#define GCRT_BSWAP64(x) _byteswap_uint64(x)
#define GCRT_LIKELY(x) (x)
#define GCRT_UNLIKELY(x) (x)
#define GCRT_NORETURN __declspec(noreturn)
#define GCRT_INLINE static __forceinline
#else
#define GCRT_BSWAP16(x) __builtin_bswap16(x)
#define GCRT_BSWAP32(x) __builtin_bswap32(x)
#define GCRT_BSWAP64(x) __builtin_bswap64(x)
#define GCRT_LIKELY(x) __builtin_expect(!!(x), 1)
#define GCRT_UNLIKELY(x) __builtin_expect(!!(x), 0)
#define GCRT_NORETURN __attribute__((noreturn))
#define GCRT_INLINE static inline __attribute__((always_inline))
#endif

/* ------------------------------------------------------------------------ */
/* CPU state                                                                 */
/* ------------------------------------------------------------------------ */
typedef struct PPCContext {
    uint32_t r[32];
    double   f[32];      /* FPR / paired-single slot 0 */
    double   ps1[32];    /* paired-single slot 1 */
    uint32_t cr;
    uint32_t lr, ctr;
    uint32_t xer_so, xer_ov, xer_ca, xer_bc;
    uint32_t fpscr;
    uint32_t msr;
    uint32_t gqr[8];
    uint32_t sr[16];
    uint32_t srr0, srr1;
    uint32_t dec;
    uint32_t reserve_addr;
    uint32_t cur_func;       /* guest address of the running function (diagnostics) */
    uint32_t spr[1024];      /* everything else (HID*, SPRG*, WPAR, DMA*, ...) */
} PPCContext;

typedef void (*GCRTFunc)(PPCContext* ctx);

typedef struct GCRTFuncEntry {
    uint32_t addr;
    GCRTFunc fn;
} GCRTFuncEntry;

typedef struct GCRTGameInfo {
    const char* game_id;
    const char* title;
    int         is_wii;
    uint32_t    entry;
    uint32_t    text_lo, text_hi;      /* covering range of all text sections */
    const GCRTFuncEntry* funcs;
    uint32_t    func_count;
    uint32_t    save_context_addr;     /* OSSaveContext, 0 if unknown */
    const char* default_dol;           /* relative to the asset directory */
    const char* default_disc;          /* flat disc/partition image */
    int         interrupt_checks;
} GCRTGameInfo;

extern const GCRTGameInfo gcrt_game;   /* emitted by the recompiler */

/* ------------------------------------------------------------------------ */
/* Guest memory                                                              */
/* ------------------------------------------------------------------------ */
#define GCRT_MEM_SPAN       0x20000000u
#define GCRT_MEM1_SIZE      0x01800000u
#define GCRT_MEM2_PHYS      0x10000000u
#define GCRT_MEM2_SIZE      0x04000000u
#define GCRT_LOCKED_CACHE   0x1E000000u

extern uint8_t* gcrt_membase;
extern volatile int gcrt_poll_pending;

uint32_t gcrt_mmio_read(uint32_t ea, int size);
void     gcrt_mmio_write(uint32_t ea, uint32_t value, int size);

GCRT_INLINE uint32_t gcrt_off(uint32_t ea) {
    return (ea & 0x1FFFFFFFu) + (((ea >> 28) == 0xEu) ? GCRT_LOCKED_CACHE : 0u);
}
#define GCRT_IS_MMIO(ea) ((uint32_t)((ea) - 0xCC000000u) < 0x02000000u)
GCRT_INLINE uint8_t* gcrt_ptr(uint32_t ea) { return gcrt_membase + gcrt_off(ea); }

GCRT_INLINE uint8_t mem_r8(uint32_t ea) {
    if (GCRT_UNLIKELY(GCRT_IS_MMIO(ea))) return (uint8_t)gcrt_mmio_read(ea, 1);
    return *gcrt_ptr(ea);
}
GCRT_INLINE uint16_t mem_r16(uint32_t ea) {
    uint16_t v;
    if (GCRT_UNLIKELY(GCRT_IS_MMIO(ea))) return (uint16_t)gcrt_mmio_read(ea, 2);
    memcpy(&v, gcrt_ptr(ea), 2);
    return GCRT_BSWAP16(v);
}
GCRT_INLINE uint32_t mem_r32(uint32_t ea) {
    uint32_t v;
    if (GCRT_UNLIKELY(GCRT_IS_MMIO(ea))) return gcrt_mmio_read(ea, 4);
    memcpy(&v, gcrt_ptr(ea), 4);
    return GCRT_BSWAP32(v);
}
GCRT_INLINE uint64_t mem_r64(uint32_t ea) {
    uint64_t v;
    if (GCRT_UNLIKELY(GCRT_IS_MMIO(ea)))
        return ((uint64_t)gcrt_mmio_read(ea, 4) << 32) | gcrt_mmio_read(ea + 4, 4);
    memcpy(&v, gcrt_ptr(ea), 8);
    return GCRT_BSWAP64(v);
}
GCRT_INLINE void mem_w8(uint32_t ea, uint8_t v) {
    if (GCRT_UNLIKELY(GCRT_IS_MMIO(ea))) { gcrt_mmio_write(ea, v, 1); return; }
    *gcrt_ptr(ea) = v;
}
GCRT_INLINE void mem_w16(uint32_t ea, uint16_t v) {
    if (GCRT_UNLIKELY(GCRT_IS_MMIO(ea))) { gcrt_mmio_write(ea, v, 2); return; }
    v = GCRT_BSWAP16(v);
    memcpy(gcrt_ptr(ea), &v, 2);
}
GCRT_INLINE void mem_w32(uint32_t ea, uint32_t v) {
    if (GCRT_UNLIKELY(GCRT_IS_MMIO(ea))) { gcrt_mmio_write(ea, v, 4); return; }
    v = GCRT_BSWAP32(v);
    memcpy(gcrt_ptr(ea), &v, 4);
}
GCRT_INLINE void mem_w64(uint32_t ea, uint64_t v) {
    if (GCRT_UNLIKELY(GCRT_IS_MMIO(ea))) {
        gcrt_mmio_write(ea, (uint32_t)(v >> 32), 4);
        gcrt_mmio_write(ea + 4, (uint32_t)v, 4);
        return;
    }
    v = GCRT_BSWAP64(v);
    memcpy(gcrt_ptr(ea), &v, 8);
}

/* ------------------------------------------------------------------------ */
/* Bit / float helpers                                                       */
/* ------------------------------------------------------------------------ */
GCRT_INLINE uint32_t ppc_rotl(uint32_t x, uint32_t n) {
    n &= 31;
    return n ? (x << n) | (x >> (32 - n)) : x;
}
GCRT_INLINE uint32_t ppc_cntlzw(uint32_t x) {
#if defined(_MSC_VER)
    unsigned long idx;
    return _BitScanReverse(&idx, x) ? 31u - idx : 32u;
#else
    return x ? (uint32_t)__builtin_clz(x) : 32u;
#endif
}
GCRT_INLINE uint32_t ppc_mask(uint32_t mb, uint32_t me) {
    uint32_t a = 0xFFFFFFFFu >> mb;
    uint32_t b = me == 31 ? 0xFFFFFFFFu : ~(0xFFFFFFFFu >> (me + 1));
    return mb <= me ? (a & b) : (a | b);
}
GCRT_INLINE float    u2f(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
GCRT_INLINE uint32_t f2u(float f)    { uint32_t u; memcpy(&u, &f, 4); return u; }
GCRT_INLINE double   u2d(uint64_t u) { double d; memcpy(&d, &u, 8); return d; }
GCRT_INLINE uint64_t d2u(double d)   { uint64_t u; memcpy(&u, &d, 8); return u; }
GCRT_INLINE double   ppc_round_single(double x) { return (double)(float)x; }

/* ------------------------------------------------------------------------ */
/* Condition register                                                        */
/* ------------------------------------------------------------------------ */
#define CR_LT 8u
#define CR_GT 4u
#define CR_EQ 2u
#define CR_SO 1u

GCRT_INLINE void ppc_set_crf(PPCContext* ctx, int field, uint32_t bits) {
    uint32_t sh = 28u - 4u * (uint32_t)field;
    ctx->cr = (ctx->cr & ~(0xFu << sh)) | ((bits & 0xFu) << sh);
}
GCRT_INLINE uint32_t ppc_get_crf(const PPCContext* ctx, int field) {
    return (ctx->cr >> (28u - 4u * (uint32_t)field)) & 0xFu;
}
GCRT_INLINE uint32_t ppc_crbit(const PPCContext* ctx, int bit) {
    return (ctx->cr >> (31 - bit)) & 1u;
}
GCRT_INLINE void ppc_set_crbit(PPCContext* ctx, int bit, uint32_t v) {
    ctx->cr = (ctx->cr & ~(1u << (31 - bit))) | ((v & 1u) << (31 - bit));
}
GCRT_INLINE void ppc_cmp_s(PPCContext* ctx, int field, int32_t a, int32_t b) {
    ppc_set_crf(ctx, field, (a < b ? CR_LT : a > b ? CR_GT : CR_EQ) | ctx->xer_so);
}
GCRT_INLINE void ppc_cmp_u(PPCContext* ctx, int field, uint32_t a, uint32_t b) {
    ppc_set_crf(ctx, field, (a < b ? CR_LT : a > b ? CR_GT : CR_EQ) | ctx->xer_so);
}
#define PPC_CR0(ctx, v) ppc_cmp_s((ctx), 0, (int32_t)(v), 0)
GCRT_INLINE void ppc_fcmp(PPCContext* ctx, int field, double a, double b) {
    uint32_t c = (a != a || b != b) ? CR_SO : a < b ? CR_LT : a > b ? CR_GT : CR_EQ;
    ppc_set_crf(ctx, field, c);
    ctx->fpscr = (ctx->fpscr & ~0xF000u) | (c << 12);
}
GCRT_INLINE void ppc_cr1_from_fpscr(PPCContext* ctx) {
    ppc_set_crf(ctx, 1, ctx->fpscr >> 28);
}
GCRT_INLINE uint32_t ppc_get_xer(const PPCContext* ctx) {
    return (ctx->xer_so << 31) | (ctx->xer_ov << 30) | (ctx->xer_ca << 29) | (ctx->xer_bc & 0x7F);
}
GCRT_INLINE void ppc_set_xer(PPCContext* ctx, uint32_t v) {
    ctx->xer_so = v >> 31; ctx->xer_ov = (v >> 30) & 1; ctx->xer_ca = (v >> 29) & 1; ctx->xer_bc = v & 0x7F;
}
GCRT_INLINE void ppc_set_ov(PPCContext* ctx, uint32_t ov) {
    ctx->xer_ov = ov & 1;
    ctx->xer_so |= ov & 1;
}

/* ------------------------------------------------------------------------ */
/* Integer helpers                                                           */
/* ------------------------------------------------------------------------ */
GCRT_INLINE uint32_t ppc_divw(PPCContext* ctx, uint32_t a, uint32_t b, int oe) {
    int32_t sa = (int32_t)a, sb = (int32_t)b;
    if (sb == 0 || (a == 0x80000000u && sb == -1)) {
        if (oe) ppc_set_ov(ctx, 1);
        return (sa < 0) ? 0xFFFFFFFFu : 0u;
    }
    if (oe) ppc_set_ov(ctx, 0);
    return (uint32_t)(sa / sb);
}
GCRT_INLINE uint32_t ppc_divwu(PPCContext* ctx, uint32_t a, uint32_t b, int oe) {
    if (b == 0) { if (oe) ppc_set_ov(ctx, 1); return 0; }
    if (oe) ppc_set_ov(ctx, 0);
    return a / b;
}
GCRT_INLINE uint32_t ppc_sraw(PPCContext* ctx, uint32_t s, uint32_t n) {
    int32_t v = (int32_t)s;
    n &= 0x3F;
    if (n > 31) { ctx->xer_ca = v < 0; return v < 0 ? 0xFFFFFFFFu : 0u; }
    ctx->xer_ca = (v < 0) && n && ((s & ((1u << n) - 1u)) != 0);
    return (uint32_t)(v >> n);
}
GCRT_INLINE int ppc_trap_cond(uint32_t to, uint32_t a, uint32_t b) {
    int32_t sa = (int32_t)a, sb = (int32_t)b;
    return ((to & 16) && sa < sb) || ((to & 8) && sa > sb) || ((to & 4) && a == b) ||
           ((to & 2) && a < b) || ((to & 1) && a > b);
}

/* ------------------------------------------------------------------------ */
/* Floating point helpers                                                    */
/* ------------------------------------------------------------------------ */
double   ppc_fctiw(PPCContext* ctx, double x, int round_to_zero);
double   ppc_fres(double x);
double   ppc_frsqrte(double x);
void     ppc_mtfsf(PPCContext* ctx, uint32_t fm, uint64_t bits);
void     ppc_fpscr_changed(PPCContext* ctx);
GCRT_INLINE double ppc_fsel(double a, double b, double c) { return (a >= 0.0) ? b : c; }

/* Paired-single quantized loads/stores (psq_l / psq_st). */
void ppc_psq_load(PPCContext* ctx, int frd, uint32_t ea, int w, int qr);
void ppc_psq_store(PPCContext* ctx, int frs, uint32_t ea, int w, int qr);

/* String / multiple word loads */
void ppc_lswi(PPCContext* ctx, int rd, uint32_t ea, uint32_t nb);
void ppc_stswi(PPCContext* ctx, int rs, uint32_t ea, uint32_t nb);

/* ------------------------------------------------------------------------ */
/* Control-flow / system services                                           */
/* ------------------------------------------------------------------------ */
void     gcrt_call(PPCContext* ctx, uint32_t addr);          /* indirect call */
GCRTFunc gcrt_lookup(uint32_t addr);
void     gcrt_dcbz(PPCContext* ctx, uint32_t ea);
void     gcrt_mtspr(PPCContext* ctx, uint32_t spr, uint32_t v);
uint32_t gcrt_mfspr(PPCContext* ctx, uint32_t spr);
uint64_t gcrt_timebase(void);
void     gcrt_mtmsr(PPCContext* ctx, uint32_t v);
void     gcrt_syscall(PPCContext* ctx, uint32_t addr);
void     gcrt_trap(PPCContext* ctx, uint32_t addr);
GCRT_NORETURN void gcrt_rfi(PPCContext* ctx, uint32_t addr);
GCRT_NORETURN void gcrt_illegal(PPCContext* ctx, uint32_t addr, uint32_t word);
GCRT_NORETURN void gcrt_fatal(const char* fmt, ...);
void     gcrt_poll(PPCContext* ctx);

/* Thread/context support: emitted at every call to OSSaveContext.  The
 * setjmp lives in the *caller's* frame so a later resume can return "1". */
jmp_buf* gcrt_context_resume_slot(uint32_t context_addr);
void     gcrt_context_resumed(PPCContext* ctx);

#define GCRT_CHECK_INTERRUPTS(ctx) do { if (GCRT_UNLIKELY(gcrt_poll_pending)) gcrt_poll(ctx); } while (0)

/* HLE registry (functions replaced by native code) */
typedef struct GCRTHleEntry { const char* name; GCRTFunc fn; } GCRTHleEntry;
GCRTFunc gcrt_hle_find(const char* name);

/* Runtime entry used by main(); returns process exit code. */
int  gcrt_main(int argc, char** argv);
GCRT_NORETURN void gcrt_exit(int code);

#ifdef __cplusplus
}
#endif
#endif /* GCRT_H */

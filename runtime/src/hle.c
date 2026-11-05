/*
 * Built-in high-level replacements for SDK functions.
 *
 * The recompiler routes a guest function to one of these when its symbol
 * name matches (see gcwii_recomp/hle.py) or when the project config maps an
 * address to it.  Each takes the guest CPU context and follows the PowerPC
 * EABI: integer args in r3..r10, float args in f1..f8, result in r3 / f1.
 */
#include "gcrt_internal.h"

#include <stdio.h>
#include <stdlib.h>

static void guest_string(uint32_t ea, char* out, size_t cap) {
    size_t i = 0;
    if (!ea) { snprintf(out, cap, "(null)"); return; }
    for (; i + 1 < cap; i++) {
        char c = (char)mem_r8(ea + (uint32_t)i);
        if (!c) break;
        out[i] = c;
    }
    out[i] = 0;
}

typedef struct VaState { PPCContext* ctx; int gpr; int fpr; uint32_t stack; } VaState;

static uint32_t va_int(VaState* v) {
    if (v->gpr <= 10) return v->ctx->r[v->gpr++];
    uint32_t x = mem_r32(v->stack);
    v->stack += 4;
    return x;
}

static uint64_t va_int64(VaState* v) {
    if (!(v->gpr & 1)) v->gpr++;   /* 64-bit values start on an odd register (r3:r4, r5:r6, ...) */
    if (v->gpr <= 9) {
        uint64_t hi = v->ctx->r[v->gpr], lo = v->ctx->r[v->gpr + 1];
        v->gpr += 2;
        return (hi << 32) | lo;
    }
    v->stack = (v->stack + 7u) & ~7u;
    uint64_t x = mem_r64(v->stack);
    v->stack += 8;
    return x;
}

static double va_double(VaState* v) {
    if (v->fpr <= 8) return v->ctx->f[v->fpr++];
    v->stack = (v->stack + 7u) & ~7u;
    double d = u2d(mem_r64(v->stack));
    v->stack += 8;
    return d;
}

/* printf-style formatting of a guest format string with guest varargs. */
size_t gcrt_guest_format(PPCContext* ctx, uint32_t fmt_ea, int first_gpr, char* out, size_t cap) {
    VaState va = { ctx, first_gpr, 1, ctx->r[1] + 8u };
    char fmt[1024];
    guest_string(fmt_ea, fmt, sizeof fmt);
    size_t n = 0;
    for (const char* p = fmt; *p && n + 1 < cap; p++) {
        if (*p != '%') { out[n++] = *p; continue; }
        char spec[32];
        size_t s = 0;
        spec[s++] = *p++;
        while (*p && strchr("-+ #0123456789.*", *p) && s < 24) {
            if (*p == '*') { s += (size_t)snprintf(spec + s, sizeof spec - s, "%d", (int)va_int(&va)); p++; continue; }
            spec[s++] = *p++;
        }
        int lng = 0;
        while (*p == 'l' || *p == 'h' || *p == 'z' || *p == 'j' || *p == 't') { if (*p == 'l') lng++; p++; }
        char conv = *p;
        if (!conv) break;
        char tmp[512];
        tmp[0] = 0;
        switch (conv) {
        case 'd': case 'i':
            if (lng >= 2) { spec[s++] = 'l'; spec[s++] = 'l'; spec[s++] = conv; spec[s] = 0; snprintf(tmp, sizeof tmp, spec, (long long)va_int64(&va)); }
            else { spec[s++] = conv; spec[s] = 0; snprintf(tmp, sizeof tmp, spec, (int)va_int(&va)); }
            break;
        case 'u': case 'x': case 'X': case 'o':
            if (lng >= 2) { spec[s++] = 'l'; spec[s++] = 'l'; spec[s++] = conv; spec[s] = 0; snprintf(tmp, sizeof tmp, spec, (unsigned long long)va_int64(&va)); }
            else { spec[s++] = conv; spec[s] = 0; snprintf(tmp, sizeof tmp, spec, (unsigned)va_int(&va)); }
            break;
        case 'c': spec[s++] = 'c'; spec[s] = 0; snprintf(tmp, sizeof tmp, spec, (int)va_int(&va)); break;
        case 'p': snprintf(tmp, sizeof tmp, "0x%08X", va_int(&va)); break;
        case 's': {
            char str[400];
            guest_string(va_int(&va), str, sizeof str);
            spec[s++] = 's'; spec[s] = 0;
            snprintf(tmp, sizeof tmp, spec, str);
            break;
        }
        case 'f': case 'F': case 'e': case 'E': case 'g': case 'G': case 'a': case 'A':
            spec[s++] = conv; spec[s] = 0;
            snprintf(tmp, sizeof tmp, spec, va_double(&va));
            break;
        case '%': tmp[0] = '%'; tmp[1] = 0; break;
        default: tmp[0] = '%'; tmp[1] = conv; tmp[2] = 0; break;
        }
        for (const char* t = tmp; *t && n + 1 < cap; t++) out[n++] = *t;
    }
    out[n] = 0;
    return n;
}

void hle_OSReport(PPCContext* ctx) {
    char buf[4096];
    gcrt_guest_format(ctx, ctx->r[3], 4, buf, sizeof buf);
    fputs(buf, stdout);
    fflush(stdout);
}

void hle_printf(PPCContext* ctx) {
    char buf[4096];
    size_t n = gcrt_guest_format(ctx, ctx->r[3], 4, buf, sizeof buf);
    fputs(buf, stdout);
    fflush(stdout);
    ctx->r[3] = (uint32_t)n;
}

void hle_puts(PPCContext* ctx) {
    char buf[2048];
    guest_string(ctx->r[3], buf, sizeof buf);
    puts(buf);
    fflush(stdout);
    ctx->r[3] = 0;
}

void hle_OSPanic(PPCContext* ctx) {
    char file[256], msg[2048];
    guest_string(ctx->r[3], file, sizeof file);
    gcrt_guest_format(ctx, ctx->r[5], 6, msg, sizeof msg);
    gcrt_fatal("OSPanic in %s line %u: %s", file, ctx->r[4], msg);
}

void hle_OSFatal(PPCContext* ctx) {
    char msg[2048];
    guest_string(ctx->r[5], msg, sizeof msg);
    gcrt_fatal("OSFatal: %s", msg);
}

void hle_exit(PPCContext* ctx) { gcrt_exit((int)ctx->r[3]); }
void hle_abort(PPCContext* ctx) { (void)ctx; gcrt_fatal("guest called abort()"); }
void hle_nop(PPCContext* ctx) { (void)ctx; }
void hle_return0(PPCContext* ctx) { ctx->r[3] = 0; }
void hle_return1(PPCContext* ctx) { ctx->r[3] = 1; }

/* Present the current XFB and wait for the next 60 Hz tick (single-threaded titles). */
void hle_VIWaitForRetrace(PPCContext* ctx) {
    static uint64_t next;
    uint64_t now = gcrt_platform_ticks_ns();
    if (!next || now > next + 100000000ull) next = now;
    next += 16683333ull;
    if (next > now) gcrt_platform_sleep_ns(next - now);
    gcrt_poll(ctx);
}

/* Fast native replacements for hot libc routines. */
void hle_memcpy(PPCContext* ctx) {
    uint32_t d = ctx->r[3], s = ctx->r[4], n = ctx->r[5];
    if (!GCRT_IS_MMIO(d) && !GCRT_IS_MMIO(s)) memmove(gcrt_ptr(d), gcrt_ptr(s), n);
    else for (uint32_t i = 0; i < n; i++) mem_w8(d + i, mem_r8(s + i));
}
void hle_memmove(PPCContext* ctx) { hle_memcpy(ctx); }
void hle_memset(PPCContext* ctx) {
    uint32_t d = ctx->r[3], n = ctx->r[5];
    if (!GCRT_IS_MMIO(d)) memset(gcrt_ptr(d), (int)(ctx->r[4] & 0xFF), n);
    else for (uint32_t i = 0; i < n; i++) mem_w8(d + i, (uint8_t)ctx->r[4]);
}
void hle_strlen(PPCContext* ctx) {
    uint32_t s = ctx->r[3], n = 0;
    while (mem_r8(s + n)) n++;
    ctx->r[3] = n;
}

static const GCRTHleEntry g_hle[] = {
    { "hle_OSReport", hle_OSReport }, { "hle_printf", hle_printf }, { "hle_puts", hle_puts },
    { "hle_OSPanic", hle_OSPanic }, { "hle_OSFatal", hle_OSFatal }, { "hle_exit", hle_exit },
    { "hle_abort", hle_abort }, { "hle_nop", hle_nop }, { "hle_return0", hle_return0 },
    { "hle_return1", hle_return1 }, { "hle_VIWaitForRetrace", hle_VIWaitForRetrace },
    { "hle_memcpy", hle_memcpy }, { "hle_memmove", hle_memmove }, { "hle_memset", hle_memset },
    { "hle_strlen", hle_strlen },
};

GCRTFunc gcrt_hle_find(const char* name) {
    for (size_t i = 0; i < sizeof g_hle / sizeof g_hle[0]; i++)
        if (!strcmp(g_hle[i].name, name)) return g_hle[i].fn;
    return NULL;
}

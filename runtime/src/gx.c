/*
 * GX command-stream front end (CP / PE / write-gather pipe).
 *
 * Commands written by the game are decoded into high-level calls on a
 * renderer backend (gx_backend.h): register loads, primitive batches with
 * their vertex format, and EFB copies.  Vertex sizes are computed from the
 * VCD/VAT state so the stream stays in sync.  PE draw-done / token
 * interrupts are raised so GXDrawDone / GXSetDrawSync complete.
 */
#include "gcrt_internal.h"
#include "gx_backend.h"

#include <stdlib.h>

static uint16_t cp_regs16[0x80 / 2];
static uint16_t pe_regs16[0x100 / 2];
static uint32_t cp_state[0x100];     /* CP internal registers (VCD/VAT/array bases) */
static uint32_t bp_state[0x100];
static uint32_t xf_state[0x1100];

static uint8_t* fifo;
static uint32_t fifo_len, fifo_cap;

GXStats gcrt_gx_stats;
const GXBackend* gcrt_gx_backend;

void gcrt_gx_init(void) {
    fifo_cap = 1u << 20;
    fifo = (uint8_t*)malloc(fifo_cap);
    fifo_len = 0;
    if (!gcrt_gx_backend) gcrt_gx_backend = gx_backend_null();
    cp_regs16[0] = 0x0014;
}

const uint32_t* gcrt_gx_cp_state(void) { return cp_state; }
const uint32_t* gcrt_gx_bp_state(void) { return bp_state; }
const uint32_t* gcrt_gx_xf_state(void) { return xf_state; }

/* --- vertex size -------------------------------------------------------- */
static const int comp_size[8] = { 1, 1, 2, 2, 4, 0, 0, 0 };
static const int color_size[8] = { 2, 3, 4, 2, 3, 4, 0, 0 };

static int attr_size(uint32_t type, int direct_size, int nbt3) {
    switch (type) {
    case 1: return direct_size;
    case 2: return nbt3 ? 3 : 1;
    case 3: return nbt3 ? 6 : 2;
    default: return 0;
    }
}

int gcrt_gx_vertex_size(int vat) {
    uint32_t vcd_lo = cp_state[0x50], vcd_hi = cp_state[0x60];
    uint32_t a = cp_state[0x70 + vat], b = cp_state[0x80 + vat], c = cp_state[0x90 + vat];
    int size = 0;
    for (int i = 0; i < 9; i++) size += (vcd_lo >> i) & 1;            /* matrix indices */
    /* position */
    size += attr_size((vcd_lo >> 9) & 3, comp_size[(a >> 1) & 7] * ((a & 1) ? 3 : 2), 0);
    /* normal / NBT */
    {
        int nbt = (a >> 9) & 1, idx3 = (a >> 31) & 1;
        size += attr_size((vcd_lo >> 11) & 3, comp_size[(a >> 10) & 7] * (nbt ? 9 : 3), nbt && idx3);
    }
    size += attr_size((vcd_lo >> 13) & 3, color_size[(a >> 14) & 7], 0);
    size += attr_size((vcd_lo >> 15) & 3, color_size[(a >> 18) & 7], 0);
    /* texcoords: (cnt bit, fmt shift) per unit */
    const struct { const uint32_t* reg; int cnt, fmt; } tc[8] = {
        { &a, 21, 22 }, { &b, 0, 1 }, { &b, 9, 10 }, { &b, 18, 19 },
        { &b, 27, 28 }, { &c, 5, 6 }, { &c, 14, 15 }, { &c, 23, 24 } };
    for (int t = 0; t < 8; t++) {
        uint32_t r = *tc[t].reg;
        int comps = ((r >> tc[t].cnt) & 1) ? 2 : 1;
        size += attr_size((vcd_hi >> (t * 2)) & 3, comp_size[(r >> tc[t].fmt) & 7] * comps, 0);
    }
    return size;
}

/* --- register loads ----------------------------------------------------- */
static void pe_raise(int finish, uint16_t token) {
    uint16_t* ctrl = &pe_regs16[0x0A / 2];
    if (finish) {
        *ctrl |= 0x0008u;
        if (*ctrl & 0x0002u) gcrt_raise_irq(PI_INT_PE_FINISH);
    } else {
        pe_regs16[0x0E / 2] = token;
        *ctrl |= 0x0004u;
        if (*ctrl & 0x0001u) gcrt_raise_irq(PI_INT_PE_TOKEN);
    }
}

static void efb_copy(uint32_t ctrl) {
    gcrt_gx_stats.efb_copies++;
    int to_xfb = (ctrl >> 14) & 1;
    if (gcrt_gx_backend->efb_copy && gcrt_gx_backend->efb_copy(bp_state, ctrl)) return;
    if (to_xfb) {
        /* Placeholder until a real renderer is attached: fill the XFB with the clear colour. */
        uint32_t dst = ((bp_state[0x4B] & 0x00FFFFFFu) << 5) | 0x80000000u;
        uint32_t stride = (bp_state[0x4D] & 0x3FFu) << 5;
        uint32_t wh = bp_state[0x4A];
        uint32_t w = (wh & 0x3FFu) + 1, h = ((wh >> 10) & 0x3FFu) + 1;
        uint32_t ar = bp_state[0x4F], gb = bp_state[0x50];
        int r = ar & 0xFF, g = (gb >> 8) & 0xFF, bl = gb & 0xFF;
        int yy = ((66 * r + 129 * g + 25 * bl + 128) >> 8) + 16;
        int u = ((-38 * r - 74 * g + 112 * bl + 128) >> 8) + 128;
        int v = ((112 * r - 94 * g - 18 * bl + 128) >> 8) + 128;
        uint32_t pix = ((uint32_t)yy << 24) | ((uint32_t)u << 16) | ((uint32_t)yy << 8) | (uint32_t)v;
        if (!stride) stride = w * 2;
        if (w > 640) w = 640;
        if (h > 576) h = 576;
        for (uint32_t y = 0; y < h; y++)
            for (uint32_t x = 0; x < w; x += 2) mem_w32(dst + y * stride + x * 2, pix);
    }
}

static void load_bp(uint32_t v) {
    uint32_t reg = v >> 24, val = v & 0x00FFFFFFu;
    if (reg == 0xFE) { bp_state[0xFE] = val; return; }                /* BP mask */
    if (bp_state[0xFE] && bp_state[0xFE] != 0xFFFFFF) {
        val = (bp_state[reg] & ~bp_state[0xFE]) | (val & bp_state[0xFE]);
        bp_state[0xFE] = 0xFFFFFF;
    }
    bp_state[reg] = val;
    gcrt_gx_stats.bp_loads++;
    if (gcrt_gx_backend->load_bp) gcrt_gx_backend->load_bp(reg, val);
    switch (reg) {
    case 0x45: if (val & 2) pe_raise(1, 0); break;                    /* PE_DONE */
    case 0x47: pe_raise(0, (uint16_t)val); break;                     /* PE_TOKEN_INT */
    case 0x48: pe_regs16[0x0E / 2] = (uint16_t)val; break;            /* PE_TOKEN */
    case 0x52: efb_copy(val); break;                                  /* EFB copy trigger */
    default: break;
    }
}

/* --- command parser ----------------------------------------------------- */
static uint32_t rd32(const uint8_t* p) { return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3]; }
static uint16_t rd16(const uint8_t* p) { return (uint16_t)((p[0] << 8) | p[1]); }

static int run_commands(const uint8_t* buf, uint32_t len, int depth);

/* Returns bytes consumed, or 0 if the command is incomplete. */
static uint32_t one_command(const uint8_t* p, uint32_t len, int depth) {
    uint8_t op = p[0];
    switch (op) {
    case 0x00: return 1;                                     /* NOP */
    case 0x08:                                               /* LOAD CP REG */
        if (len < 6) return 0;
        cp_state[p[1]] = rd32(p + 2);
        if (gcrt_gx_backend->load_cp) gcrt_gx_backend->load_cp(p[1], rd32(p + 2));
        return 6;
    case 0x10: {                                             /* LOAD XF REG(S) */
        if (len < 5) return 0;
        uint32_t hdr = rd32(p + 1);
        uint32_t n = ((hdr >> 16) & 0xF) + 1, addr = hdr & 0xFFFF;
        if (len < 5 + n * 4) return 0;
        for (uint32_t i = 0; i < n; i++) {
            uint32_t v = rd32(p + 5 + i * 4);
            if (addr + i < 0x1100) xf_state[addr + i] = v;
        }
        if (gcrt_gx_backend->load_xf) gcrt_gx_backend->load_xf(addr, n, p + 5);
        return 5 + n * 4;
    }
    case 0x20: case 0x28: case 0x30: case 0x38:            /* indexed XF loads */
        if (len < 5) return 0;
        if (gcrt_gx_backend->load_xf_indexed) gcrt_gx_backend->load_xf_indexed(op, rd32(p + 1));
        return 5;
    case 0x40: {                                             /* CALL display list */
        if (len < 9) return 0;
        uint32_t addr = rd32(p + 1) & 0x03FFFFE0u, size = rd32(p + 5);
        gcrt_gx_stats.display_lists++;
        if (depth < 4 && size < (16u << 20)) run_commands(gcrt_ptr(addr | 0x80000000u), size, depth + 1);
        return 9;
    }
    case 0x44: return 1;                                     /* invalidate vertex cache */
    case 0x48: return 1;
    case 0x61:                                               /* LOAD BP REG */
        if (len < 5) return 0;
        load_bp(rd32(p + 1));
        return 5;
    default:
        if (op >= 0x80 && op < 0xC0) {                      /* draw primitive */
            if (len < 3) return 0;
            int vat = op & 7;
            uint32_t count = rd16(p + 1);
            uint32_t vsize = (uint32_t)gcrt_gx_vertex_size(vat);
            uint32_t total = 3 + count * vsize;
            if (len < total) return 0;
            gcrt_gx_stats.draws++;
            gcrt_gx_stats.vertices += count;
            if (gcrt_gx_backend->draw) gcrt_gx_backend->draw(op & 0xF8, vat, count, p + 3, vsize);
            return total;
        }
        gcrt_gx_stats.unknown_opcodes++;
        if (gcrt_opts.verbose) gcrt_log("GX: unknown opcode 0x%02X - resyncing", op);
        return 1;
    }
}

static int run_commands(const uint8_t* buf, uint32_t len, int depth) {
    uint32_t pos = 0;
    while (pos < len) {
        uint32_t n = one_command(buf + pos, len - pos, depth);
        if (!n) break;
        pos += n;
    }
    return (int)pos;
}

void gcrt_gx_run_fifo(void) {
    if (!fifo_len) return;
    uint32_t used = (uint32_t)run_commands(fifo, fifo_len, 0);
    if (used) {
        memmove(fifo, fifo + used, fifo_len - used);
        fifo_len -= used;
    }
}

void gcrt_gx_write(uint32_t value, int size) {
    if (fifo_len + 4 > fifo_cap) {
        gcrt_gx_run_fifo();
        if (fifo_len + 4 > fifo_cap) { fifo_cap *= 2; fifo = (uint8_t*)realloc(fifo, fifo_cap); }
    }
    for (int i = size - 1; i >= 0; i--) fifo[fifo_len++] = (uint8_t)(value >> (i * 8));
    /* Decode eagerly when a full 32-byte burst has accumulated so tokens/draw-done are prompt. */
    if (fifo_len >= 32) gcrt_gx_run_fifo();
}

/* --- CP / PE registers --------------------------------------------------- */
uint32_t gcrt_cp_read(uint32_t off, int size) {
    uint16_t v;
    switch (off & ~1u) {
    case 0x00: v = 0x0014 | 0x0002 | 0x0008; break;   /* idle for reading + commands, underflow */
    case 0x30: case 0x32: v = 0; break;               /* read/write distance: empty */
    case 0x38: v = cp_regs16[0x34 / 2]; break;        /* read ptr = write ptr */
    case 0x3A: v = cp_regs16[0x36 / 2]; break;
    default: v = cp_regs16[(off / 2) & 0x3F]; break;
    }
    if (size == 4) return ((uint32_t)v << 16) | gcrt_cp_read(off + 2, 2);
    return v;
}

void gcrt_cp_write(uint32_t off, uint32_t v, int size) {
    if (size == 4) { gcrt_cp_write(off, v >> 16, 2); gcrt_cp_write(off + 2, v & 0xFFFF, 2); return; }
    cp_regs16[(off / 2) & 0x3F] = (uint16_t)v;
}

uint32_t gcrt_pe_read(uint32_t off, int size) {
    uint16_t v = pe_regs16[(off / 2) & 0x7F];
    if (size == 4) return ((uint32_t)v << 16) | pe_regs16[((off + 2) / 2) & 0x7F];
    return v;
}

void gcrt_pe_write(uint32_t off, uint32_t v, int size) {
    if (size == 4) { gcrt_pe_write(off, v >> 16, 2); gcrt_pe_write(off + 2, v & 0xFFFF, 2); return; }
    if (off == 0x0A) {
        uint16_t cur = pe_regs16[0x0A / 2];
        if (v & 0x0004u) { cur &= ~0x0004u; gcrt_clear_irq(PI_INT_PE_TOKEN); }
        if (v & 0x0008u) { cur &= ~0x0008u; gcrt_clear_irq(PI_INT_PE_FINISH); }
        cur = (uint16_t)((cur & 0x000Cu) | (v & ~0x000Cu));
        pe_regs16[0x0A / 2] = cur;
        return;
    }
    pe_regs16[(off / 2) & 0x7F] = (uint16_t)v;
}

/*
 * Hardware register layer (MMIO at 0xCC000000 / 0xCD000000).
 *
 * The goal is not cycle accuracy: each device completes requests immediately
 * and raises the matching PI interrupt so the SDK's drivers make progress.
 *   VI  0xCC002000  video timing, framebuffer address -> host window
 *   PI  0xCC003000  interrupt cause/mask, CPU FIFO pointers
 *   DSP 0xCC005000  mailboxes / ARAM DMA (ARAM is backed by host memory)
 *   DI  0xCC006000  DVD interface backed by the disc image
 *   SI  0xCC006400  controllers backed by the host gamepad / keyboard
 *   EXI 0xCC006800  memory cards / RTC / SRAM (no devices attached yet)
 *   AI  0xCC006C00  audio streaming / sample counter
 *   IPC 0xCD000000  Wii Starlet IPC (minimal IOS HLE)
 */
#include "gcrt_internal.h"

#include <stdio.h>
#include <stdlib.h>

uint32_t gcrt_pi_intsr_reg;
static uint32_t pi_intmr;
static uint32_t pi_regs[0x100 / 4];

uint32_t gcrt_pi_intsr(void) { return gcrt_pi_intsr_reg; }
uint32_t gcrt_pi_intmr(void) { return pi_intmr; }

/* ------------------------------------------------------------------------ */
/* Disc image                                                                */
/* ------------------------------------------------------------------------ */
static FILE* g_disc;
static int g_disc_wii;

int gcrt_disc_open(const char* path) {
    g_disc = fopen(path, "rb");
    if (!g_disc) return 0;
    uint8_t hdr[0x20];
    if (fread(hdr, 1, sizeof hdr, g_disc) == sizeof hdr) {
        uint32_t wii = ((uint32_t)hdr[0x18] << 24) | ((uint32_t)hdr[0x19] << 16) | ((uint32_t)hdr[0x1A] << 8) | hdr[0x1B];
        g_disc_wii = (wii == 0x5D1C9EA3u) || gcrt_game.is_wii;
    }
    return 1;
}

int gcrt_disc_is_wii(void) { return g_disc_wii; }

int gcrt_disc_read(uint64_t offset, void* dst, uint32_t len) {
    if (!g_disc) return 0;
#if defined(_WIN32)
    if (_fseeki64(g_disc, (long long)offset, SEEK_SET) != 0) return 0;
#else
    if (fseeko(g_disc, (off_t)offset, SEEK_SET) != 0) return 0;
#endif
    size_t n = fread(dst, 1, len, g_disc);
    if (n < len) memset((uint8_t*)dst + n, 0, len - n);
    return n == len;
}

/* ------------------------------------------------------------------------ */
/* VI                                                                        */
/* ------------------------------------------------------------------------ */
static uint16_t vi_regs[0x100 / 2];
static uint64_t vi_next_ns;
static uint32_t vi_frame;

#define VI_REG(off) vi_regs[(off) / 2]

uint32_t gcrt_vi_xfb_addr(void) {
    uint32_t tfbl = ((uint32_t)VI_REG(0x1C) << 16) | VI_REG(0x1E);
    uint32_t addr = tfbl & 0x00FFFFFFu;
    if (tfbl & 0x10000000u) addr <<= 5;     /* POFF: address is in 32-byte units */
    return addr ? (addr | 0x80000000u) : 0;
}

static uint32_t* g_rgba;

static uint8_t clamp8(int v) { return (uint8_t)(v < 0 ? 0 : v > 255 ? 255 : v); }

void gcrt_vi_present(void) {
    uint32_t xfb = gcrt_vi_xfb_addr();
    int w = 640, h = 480;
    uint32_t stride_words = (VI_REG(0x48) & 0xFF) * 16u;   /* HSW: 16-pixel units */
    uint32_t stride = stride_words ? stride_words * 2u : (uint32_t)w * 2u;
    int vact = (VI_REG(0x00) >> 4) & 0x3FF;                /* active lines per field */
    if (vact >= 200 && vact <= 288) h = vact * 2;
    if (!g_rgba) g_rgba = (uint32_t*)malloc(640 * 576 * 4);
    if (!xfb) {
        memset(g_rgba, 0, (size_t)w * h * 4);
    } else {
        for (int y = 0; y < h; y++) {
            const uint8_t* src = gcrt_ptr(xfb + (uint32_t)y * stride);
            uint32_t* dst = g_rgba + y * w;
            for (int x = 0; x < w; x += 2) {
                int y0 = src[0], u = src[1] - 128, y1 = src[2], v = src[3] - 128;
                src += 4;
                for (int k = 0; k < 2; k++) {
                    int yy = (k ? y1 : y0) - 16;
                    int c = 298 * yy;
                    uint8_t r = clamp8((c + 409 * v + 128) >> 8);
                    uint8_t g = clamp8((c - 100 * u - 208 * v + 128) >> 8);
                    uint8_t b = clamp8((c + 516 * u + 128) >> 8);
                    dst[x + k] = 0xFF000000u | ((uint32_t)b << 16) | ((uint32_t)g << 8) | r;
                }
            }
        }
    }
    gcrt_platform_present_from_guest(g_rgba, w, h);
}

static void vi_retrace(void) {
    vi_frame++;
    /* Display interrupt 0 (DI0) fires at the configured line; raise it every field. */
    for (int i = 0; i < 4; i++) {
        uint16_t* hi = &VI_REG(0x30 + i * 4);
        if (*hi & 0x1000u) *hi |= 0x8000u;    /* enabled -> set status */
    }
    if ((VI_REG(0x30) & 0x9000u) == 0x9000u || (VI_REG(0x34) & 0x9000u) == 0x9000u ||
        (VI_REG(0x38) & 0x9000u) == 0x9000u || (VI_REG(0x3C) & 0x9000u) == 0x9000u)
        gcrt_raise_irq(PI_INT_VI);
    gcrt_vi_present();
}

static uint32_t vi_read(uint32_t off, int size) {
    if (off == 0x2C) {   /* VCT/HCT current line: fake a scanning beam */
        uint64_t ns = gcrt_platform_ticks_ns() % 16683333ull;
        return (uint16_t)(1 + ns * 525 / 16683333ull);
    }
    if (size == 4) return ((uint32_t)VI_REG(off) << 16) | VI_REG(off + 2);
    return VI_REG(off);
}

static void vi_write(uint32_t off, uint32_t v, int size) {
    if (size == 4) { vi_write(off, v >> 16, 2); vi_write(off + 2, v & 0xFFFF, 2); return; }
    if (off >= 0x30 && off <= 0x3C && !(off & 3)) {
        /* writing 0 to the status bit acknowledges the display interrupt */
        uint16_t keep = VI_REG(off) & 0x8000u;
        VI_REG(off) = (uint16_t)((v & 0x7FFFu) | (keep & v));
        int any = 0;
        for (int i = 0; i < 4; i++) any |= (VI_REG(0x30 + i * 4) & 0x9000u) == 0x9000u;
        if (!any) gcrt_clear_irq(PI_INT_VI);
        return;
    }
    VI_REG(off) = (uint16_t)v;
}

/* ------------------------------------------------------------------------ */
/* DI                                                                        */
/* ------------------------------------------------------------------------ */
static uint32_t di_regs[0x40 / 4];
enum { DISR = 0, DICVR = 1, DICMD0 = 2, DICMD1 = 3, DICMD2 = 4, DIMAR = 5, DILEN = 6, DICR = 7, DIIMM = 8, DICFG = 9 };

static void di_update_irq(void) {
    uint32_t sr = di_regs[DISR];
    int pend = ((sr & 0x04) && (sr & 0x02)) || ((sr & 0x10) && (sr & 0x08)) || ((sr & 0x40) && (sr & 0x20)) ||
               ((di_regs[DICVR] & 0x04) && (di_regs[DICVR] & 0x02));
    if (pend) gcrt_raise_irq(PI_INT_DI); else gcrt_clear_irq(PI_INT_DI);
}

static void di_execute(void) {
    uint32_t cmd = di_regs[DICMD0] >> 24;
    uint32_t dst = di_regs[DIMAR] | 0x80000000u, len = di_regs[DILEN];
    switch (cmd) {
    case 0xA8: {   /* read */
        uint64_t off = (uint64_t)di_regs[DICMD1] << 2;
        uint32_t n = di_regs[DICMD2] ? di_regs[DICMD2] : len;
        if (n > len) n = len;
        if (!gcrt_disc_read(off, gcrt_ptr(dst), n) && gcrt_opts.verbose)
            gcrt_log("DI: read past end of disc (0x%llX+0x%X)", (unsigned long long)off, n);
        di_regs[DILEN] = 0;
        break;
    }
    case 0x12:    /* inquiry */
        memset(gcrt_ptr(dst), 0, len);
        mem_w32(dst, 0x00000002u); mem_w32(dst + 4, 0x20060526u); mem_w32(dst + 8, 0x41000000u);
        di_regs[DILEN] = 0;
        break;
    case 0xE0:    /* request error */
        di_regs[DIIMM] = 0;
        break;
    default:      /* seek, stop motor, audio, BCA, ... : succeed */
        di_regs[DIIMM] = 0;
        break;
    }
    di_regs[DICR] &= ~1u;
    di_regs[DISR] |= 0x10;   /* TCINT */
    di_update_irq();
}

static void di_write(uint32_t off, uint32_t v) {
    uint32_t r = off / 4;
    if (r == DISR) {
        uint32_t sr = di_regs[DISR];
        sr = (sr & ~0x2Au) | (v & 0x2Au);              /* masks */
        sr &= ~(v & 0x54u);                            /* write 1 to clear INT bits */
        if (v & 1) sr |= 0;                            /* BRK request: ignored */
        di_regs[DISR] = sr;
        di_update_irq();
        return;
    }
    if (r == DICVR) {
        uint32_t cv = di_regs[DICVR];
        cv = (cv & ~0x2u) | (v & 0x2u);
        cv &= ~(v & 0x4u);
        di_regs[DICVR] = cv;
        di_update_irq();
        return;
    }
    if (r < 16) di_regs[r] = v;
    if (r == DICR && (v & 1)) di_execute();
}

/* ------------------------------------------------------------------------ */
/* SI (controllers)                                                          */
/* ------------------------------------------------------------------------ */
static uint32_t si_regs[0x100 / 4];
static uint8_t si_buf[0x80];

static void si_fill_pad(int port) {
    GCRTPadState st;
    memset(&st, 0, sizeof st);
    gcrt_platform_get_pad(port, &st);
    /* SI_InBufH: [err/origin bits | buttons hi] [buttons lo] [stick x] [stick y]
     * SI_InBufL: [c-stick x] [c-stick y] [trigger l] [trigger r] */
    uint32_t b = st.buttons;
    uint32_t h = ((b & 0x1F00u) << 16) | (((b & 0x00FFu) | 0x80u) << 16) |
                 ((uint32_t)(uint8_t)(st.stick_x + 128) << 8) | (uint8_t)(st.stick_y + 128);
    uint32_t l = ((uint32_t)(uint8_t)(st.substick_x + 128) << 24) | ((uint32_t)(uint8_t)(st.substick_y + 128) << 16) |
                 ((uint32_t)st.trigger_l << 8) | st.trigger_r;
    si_regs[(port * 12 + 4) / 4] = h;
    si_regs[(port * 12 + 8) / 4] = l;
    if (!st.connected) si_regs[(port * 12 + 4) / 4] |= 0x80000000u;  /* ERRSTAT: no response */
}

static uint32_t si_read(uint32_t off) {
    if (off >= 0x80) {
        uint32_t i = off - 0x80;
        return ((uint32_t)si_buf[i] << 24) | ((uint32_t)si_buf[i + 1] << 16) | ((uint32_t)si_buf[i + 2] << 8) | si_buf[i + 3];
    }
    if (off < 0x30 && (off % 12) == 4) si_fill_pad((int)(off / 12));
    if (off == 0x38) {  /* SISR: report every connected port as having fresh data */
        uint32_t sr = 0;
        for (int p = 0; p < 4; p++) {
            GCRTPadState st; memset(&st, 0, sizeof st);
            gcrt_platform_get_pad(p, &st);
            if (st.connected) sr |= 0x20000000u >> (p * 8);   /* RDST */
        }
        return sr;
    }
    return si_regs[off / 4];
}

static void si_write(uint32_t off, uint32_t v) {
    if (off >= 0x80) {
        uint32_t i = off - 0x80;
        si_buf[i] = (uint8_t)(v >> 24); si_buf[i + 1] = (uint8_t)(v >> 16);
        si_buf[i + 2] = (uint8_t)(v >> 8); si_buf[i + 3] = (uint8_t)v;
        return;
    }
    if (off == 0x34) {   /* SICOMCSR */
        uint32_t csr = si_regs[off / 4];
        if (v & 0x80000000u) csr &= ~0x80000000u;              /* TCINT ack */
        csr = (csr & 0x80000000u) | (v & 0x7FFFFFFEu);
        if (v & 1u) {   /* TSTART: run the command in the buffer */
            int ch = (int)((v >> 1) & 3);
            uint8_t cmd = si_buf[0];
            GCRTPadState st; memset(&st, 0, sizeof st);
            gcrt_platform_get_pad(ch, &st);
            if (!st.connected) {
                csr |= 0x20000000u;  /* COMERR: no response */
                si_regs[0x38 / 4] |= 0x08000000u >> (ch * 8);
            } else if (cmd == 0x00 || cmd == 0xFF) {   /* reset / get type */
                si_buf[0] = 0x09; si_buf[1] = 0x00; si_buf[2] = 0x00;     /* standard controller */
            } else if (cmd == 0x41 || cmd == 0x42) {   /* origin / calibrate */
                memset(si_buf, 0, 10);
                si_buf[2] = 0x80; si_buf[3] = 0x80; si_buf[4] = 0x80; si_buf[5] = 0x80;
            }
            csr |= 0x80000000u;   /* TCINT */
        }
        si_regs[off / 4] = csr;
        if ((csr & 0x80000000u) && (csr & 0x40000000u)) gcrt_raise_irq(PI_INT_SI);
        else gcrt_clear_irq(PI_INT_SI);
        return;
    }
    if (off == 0x38) { si_regs[off / 4] &= ~(v & 0x0F0F0F0Fu); return; }
    si_regs[off / 4] = v;
}

/* ------------------------------------------------------------------------ */
/* EXI                                                                       */
/* ------------------------------------------------------------------------ */
static uint32_t exi_regs[3][5];

static void exi_write(uint32_t off, uint32_t v) {
    uint32_t ch = off / 0x14, r = (off % 0x14) / 4;
    if (ch > 2) return;
    if (r == 0) {
        uint32_t cur = exi_regs[ch][0];
        cur &= ~(v & 0x80Au);              /* write-1-to-clear interrupt bits (EXT, TC, EXI) */
        cur = (cur & 0x80Au) | (v & ~0x80Au);
        cur &= ~0x1000u;                   /* EXT: nothing attached */
        exi_regs[ch][0] = cur;
        return;
    }
    exi_regs[ch][r] = v;
    if (r == 3 && (v & 1u)) {              /* CR TSTART */
        uint32_t dev = (exi_regs[ch][0] >> 7) & 7u;
        if ((v & 2u) == 0 && !(v & 0x0Cu)) {
            /* immediate read: no device answers -> 0 (ID 0 = nothing) except RTC/SRAM on ch0 dev1 */
            exi_regs[ch][4] = (ch == 0 && dev == 2) ? 0 : 0;
        } else if (v & 2u) {
            /* DMA: reads return zeros */
            if (!(v & 0x0Cu)) {
                uint32_t addr = exi_regs[ch][1] | 0x80000000u, len = exi_regs[ch][2];
                memset(gcrt_ptr(addr), 0, len);
            }
        }
        exi_regs[ch][3] &= ~1u;
        exi_regs[ch][0] |= 0x8u;           /* TCINT */
        if (exi_regs[ch][0] & 0x4u) gcrt_raise_irq(PI_INT_EXI);
    }
}

static uint32_t exi_read(uint32_t off) {
    uint32_t ch = off / 0x14, r = (off % 0x14) / 4;
    if (ch > 2) return 0;
    return exi_regs[ch][r];
}

/* ------------------------------------------------------------------------ */
/* DSP / ARAM / AI                                                           */
/* ------------------------------------------------------------------------ */
static uint16_t dsp_regs[0x200 / 2];
static uint8_t* g_aram;
static int dsp_mail_stage;
#define ARAM_SIZE (16u << 20)

static uint32_t dsp_read(uint32_t off, int size) {
    switch (off) {
    case 0x04: {   /* DSP->CPU mailbox high */
        if (dsp_mail_stage == 0) return 0x8071u;
        if (dsp_mail_stage == 1) return 0x8071u;
        return 0x0000u;
    }
    case 0x06:     /* DSP->CPU mailbox low: reading it consumes the mail */
        if (dsp_mail_stage < 2) { dsp_mail_stage++; return 0xFEEDu; }
        return 0;
    case 0x00: return 0;   /* CPU->DSP mailbox high: DSP has read it */
    case 0x0A: return dsp_regs[0x0A / 2] & ~0x0001u & ~0x0400u;   /* DSPCR: reset done, DMA idle */
    case 0x16: return 0x0001u;    /* ARAM mode: ready */
    case 0x3A: return 0;          /* AR DMA count remaining */
    default:
        if (size == 4) return ((uint32_t)dsp_regs[off / 2] << 16) | dsp_regs[off / 2 + 1];
        return dsp_regs[off / 2];
    }
}

static void dsp_write(uint32_t off, uint32_t v, int size) {
    if (size == 4) { dsp_write(off, v >> 16, 2); dsp_write(off + 2, v & 0xFFFF, 2); return; }
    if (off == 0x0A) {   /* DSPCR: interrupt status bits are write-1-to-clear */
        uint16_t cur = dsp_regs[off / 2];
        uint16_t ack = (uint16_t)(v & 0x00A8u);
        cur = (uint16_t)((cur & 0x00A8u & ~ack) | (v & ~0x00A8u));
        if (v & 0x0001u) dsp_mail_stage = 0;     /* reset */
        dsp_regs[off / 2] = cur;
        if (!((cur & 0x08u) && (cur & 0x10u)) && !((cur & 0x20u) && (cur & 0x40u)) &&
            !((cur & 0x80u) && (cur & 0x100u)))
            gcrt_clear_irq(PI_INT_DSP);
        return;
    }
    dsp_regs[off / 2] = (uint16_t)v;
    if (off == 0x2A) {   /* ARAM DMA count low half written last: run the transfer */
        uint32_t mm = (((uint32_t)dsp_regs[0x20 / 2] << 16) | dsp_regs[0x22 / 2]) & 0x03FFFFE0u;
        uint32_t ar = (((uint32_t)dsp_regs[0x24 / 2] << 16) | dsp_regs[0x26 / 2]) & (ARAM_SIZE - 1);
        uint32_t cnt = ((uint32_t)dsp_regs[0x28 / 2] << 16) | dsp_regs[0x2A / 2];
        uint32_t len = cnt & 0x7FFFFFFFu;
        if (!g_aram) g_aram = (uint8_t*)calloc(1, ARAM_SIZE);
        if (ar + len > ARAM_SIZE) len = ARAM_SIZE - ar;
        if (cnt & 0x80000000u) memcpy(gcrt_ptr(mm | 0x80000000u), g_aram + ar, len);   /* ARAM -> MRAM */
        else                   memcpy(g_aram + ar, gcrt_ptr(mm | 0x80000000u), len);
        dsp_regs[0x0A / 2] |= 0x20u;   /* ARINT */
        if (dsp_regs[0x0A / 2] & 0x40u) gcrt_raise_irq(PI_INT_DSP);
    }
    if (off == 0x36) {   /* AI DMA control: start -> immediately report completion */
        if (v & 0x8000u) {
            dsp_regs[0x0A / 2] |= 0x08u;   /* AIDINT */
            if (dsp_regs[0x0A / 2] & 0x10u) gcrt_raise_irq(PI_INT_DSP);
        }
    }
}

static uint32_t ai_regs[4];
static uint32_t ai_read(uint32_t off) {
    if (off == 0x08) {   /* AISCNT: sample counter at 48 kHz */
        return (uint32_t)(gcrt_platform_ticks_ns() / 20833ull);
    }
    return ai_regs[(off / 4) & 3];
}
static void ai_write(uint32_t off, uint32_t v) {
    if (off == 0x00) {
        if (v & 0x08u) gcrt_clear_irq(PI_INT_AI);   /* AIINT ack */
        v &= ~0x08u;
    }
    ai_regs[(off / 4) & 3] = v;
}

/* ------------------------------------------------------------------------ */
/* Wii IPC (minimal IOS HLE)                                                 */
/* ------------------------------------------------------------------------ */
static uint32_t ipc_ppcmsg, ipc_ppcctrl, ipc_armmsg;
static uint32_t ipc_next_fd = 3;

static void ios_reply(uint32_t req, int32_t result) {
    mem_w32(req + 4, (uint32_t)result);
    /* IOS reply: command field becomes IOS_REPLY (8), original command kept at +8 */
    uint32_t cmd = mem_r32(req);
    mem_w32(req + 8, cmd);
    mem_w32(req, 8);
    ipc_armmsg = req & 0x1FFFFFFFu;
    ipc_ppcctrl |= 0x04u;   /* Y1: reply ready */
    ipc_ppcctrl |= 0x02u;   /* Y2: request acknowledged */
    gcrt_raise_irq(PI_INT_IPC);
}

static void ios_execute(uint32_t req_phys) {
    uint32_t req = req_phys | 0x80000000u;
    uint32_t cmd = mem_r32(req);
    int32_t result = 0;
    switch (cmd) {
    case 1: {  /* open */
        char path[64] = {0};
        uint32_t p = mem_r32(req + 0x0C) | 0x80000000u;
        for (int i = 0; i < 63; i++) { path[i] = (char)mem_r8(p + i); if (!path[i]) break; }
        if (gcrt_opts.verbose) gcrt_log("IOS open %s", path);
        result = (int32_t)ipc_next_fd++;
        break;
    }
    case 6: {  /* ioctl: handle /dev/di reads generically by command byte */
        uint32_t in = mem_r32(req + 0x10) | 0x80000000u;
        uint32_t out = mem_r32(req + 0x18) | 0x80000000u, outlen = mem_r32(req + 0x1C);
        uint32_t c = mem_r8(in);
        if (c == 0x71 || c == 0x8D) {  /* DVDLowRead / UnencryptedRead */
            uint32_t len = mem_r32(in + 4);
            uint64_t off = (uint64_t)mem_r32(in + 8) << 2;
            if (len > outlen) len = outlen;
            gcrt_disc_read(off, gcrt_ptr(out), len);
            result = 1;
        } else {
            result = 1;
        }
        break;
    }
    default:
        result = 0;
        break;
    }
    ios_reply(req, result);
}

static uint32_t ipc_read(uint32_t off) {
    switch (off) {
    case 0x00: return ipc_ppcmsg;
    case 0x04: return ipc_ppcctrl;
    case 0x08: return ipc_armmsg;
    case 0x30: return 0;
    default: return 0;
    }
}

static void ipc_write(uint32_t off, uint32_t v) {
    switch (off) {
    case 0x00: ipc_ppcmsg = v; break;
    case 0x04:
        if (v & 0x02u) ipc_ppcctrl &= ~0x02u;   /* Y2 ack */
        if (v & 0x04u) ipc_ppcctrl &= ~0x04u;   /* Y1 ack */
        ipc_ppcctrl = (ipc_ppcctrl & 0x06u) | (v & 0x30u);
        if (v & 0x01u) ios_execute(ipc_ppcmsg);  /* X1: execute request */
        if (!(ipc_ppcctrl & 0x06u)) gcrt_clear_irq(PI_INT_IPC);
        break;
    default: break;
    }
}

/* ------------------------------------------------------------------------ */
/* PI                                                                        */
/* ------------------------------------------------------------------------ */
static uint32_t pi_read(uint32_t off) {
    switch (off) {
    case 0x00: return gcrt_pi_intsr_reg | (1u << 16);   /* RSWST: reset button not pressed */
    case 0x04: return pi_intmr;
    case 0x2C: return gcrt_game.is_wii ? 0x46A0A0B0u : 0x246500B1u;   /* chip revision */
    default: return pi_regs[(off / 4) & 63];
    }
}

static void pi_write(uint32_t off, uint32_t v) {
    switch (off) {
    case 0x00: gcrt_pi_intsr_reg &= ~(v & (PI_INT_ERROR | PI_INT_RSW | PI_INT_DEBUG | PI_INT_HSP)); break;
    case 0x04: pi_intmr = v; if (gcrt_pi_intsr_reg & v) gcrt_poll_pending = 1; break;
    case 0x24: /* reset */
        if ((v & 7u) == 0 && gcrt_opts.verbose) gcrt_log("PI reset requested (value 0x%X)", v);
        pi_regs[(off / 4) & 63] = v; break;
    default: pi_regs[(off / 4) & 63] = v; break;
    }
}

/* ------------------------------------------------------------------------ */
/* Dispatch                                                                  */
/* ------------------------------------------------------------------------ */
static uint32_t extract(uint32_t word, uint32_t ea, int size) {
    if (size == 4) return word;
    if (size == 2) return (word >> ((2 - (ea & 2)) * 8)) & 0xFFFFu;
    return (word >> ((3 - (ea & 3)) * 8)) & 0xFFu;
}

uint32_t gcrt_mmio_read(uint32_t ea, int size) {
    uint32_t a = ea & 0x00FFFFFFu;
    if ((ea & 0xFF000000u) == 0xCD000000u) return extract(ipc_read(a & ~3u), ea, size);
    if (a < 0x1000) return gcrt_cp_read(a, size);
    if (a < 0x2000) return gcrt_pe_read(a - 0x1000, size);
    if (a < 0x3000) return vi_read(a - 0x2000, size);
    if (a < 0x4000) return extract(pi_read((a - 0x3000) & ~3u), ea, size);
    if (a < 0x5000) return 0;                        /* MI: memory protection */
    if (a < 0x5200) return dsp_read(a - 0x5000, size);
    if (a < 0x6000) return 0;
    if (a < 0x6040) return extract(di_regs[((a - 0x6000) / 4) & 15], ea, size);
    if (a >= 0x6400 && a < 0x6500) return extract(si_read((a - 0x6400) & ~3u), ea, size);
    if (a >= 0x6800 && a < 0x6840) return extract(exi_read((a - 0x6800) & ~3u), ea, size);
    if (a >= 0x6C00 && a < 0x6C20) return extract(ai_read((a - 0x6C00) & ~3u), ea, size);
    if (gcrt_opts.verbose) gcrt_log("unhandled MMIO read%d 0x%08X", size * 8, ea);
    return 0;
}

void gcrt_mmio_write(uint32_t ea, uint32_t v, int size) {
    uint32_t a = ea & 0x00FFFFFFu;
    if ((ea & 0xFF000000u) == 0xCD000000u) { ipc_write(a & ~3u, v); return; }
    if (a >= 0x8000 && a < 0x9000) { gcrt_gx_write(v, size); return; }   /* WGPIPE */
    if (a < 0x1000) { gcrt_cp_write(a, v, size); return; }
    if (a < 0x2000) { gcrt_pe_write(a - 0x1000, v, size); return; }
    if (a < 0x3000) { vi_write(a - 0x2000, v, size); return; }
    if (a < 0x4000) { pi_write((a - 0x3000) & ~3u, v); return; }
    if (a < 0x5000) return;
    if (a < 0x5200) { dsp_write(a - 0x5000, v, size); return; }
    if (a >= 0x6000 && a < 0x6040) { di_write((a - 0x6000) & ~3u, v); return; }
    if (a >= 0x6400 && a < 0x6500) { si_write((a - 0x6400) & ~3u, v); return; }
    if (a >= 0x6800 && a < 0x6840) { exi_write((a - 0x6800) & ~3u, v); return; }
    if (a >= 0x6C00 && a < 0x6C20) { ai_write((a - 0x6C00) & ~3u, v); return; }
    if (gcrt_opts.verbose) gcrt_log("unhandled MMIO write%d 0x%08X = 0x%X", size * 8, ea, v);
}

void gcrt_hw_init(void) {
    memset(vi_regs, 0, sizeof vi_regs);
    VI_REG(0x00) = (240u << 4);           /* 240 active lines/field */
    VI_REG(0x48) = 0x2828;                /* 640-pixel stride */
    vi_next_ns = 0;
    di_regs[DICVR] = 0;                   /* lid closed */
    gcrt_gx_init();
}

void gcrt_hw_tick(PPCContext* ctx, uint64_t now_ns) {
    (void)ctx;
    if (!vi_next_ns) vi_next_ns = now_ns;
    if (now_ns >= vi_next_ns) {
        vi_next_ns += 16683333ull;        /* 59.94 Hz */
        if (now_ns > vi_next_ns + 100000000ull) vi_next_ns = now_ns + 16683333ull;
        vi_retrace();
    }
    gcrt_gx_run_fifo();
}

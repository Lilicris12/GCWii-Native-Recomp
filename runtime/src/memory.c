/* Guest memory allocation and the IPL/apploader-equivalent boot sequence. */
#include "gcrt_internal.h"

#include <stdlib.h>
#include <stdio.h>

#if defined(_WIN32)
#include <windows.h>
#else
#include <sys/mman.h>
#endif

uint8_t* gcrt_membase;

int gcrt_memory_init(void) {
#if defined(_WIN32)
    gcrt_membase = (uint8_t*)VirtualAlloc(NULL, GCRT_MEM_SPAN, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
#else
    void* p = mmap(NULL, GCRT_MEM_SPAN, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS
#ifdef MAP_NORESERVE
                   | MAP_NORESERVE
#endif
                   , -1, 0);
    gcrt_membase = (p == MAP_FAILED) ? NULL : (uint8_t*)p;
#endif
    if (!gcrt_membase) {
        gcrt_log("failed to reserve %u MB of guest address space", GCRT_MEM_SPAN >> 20);
        return 0;
    }
    return 1;
}

static uint8_t* read_file(const char* path, size_t* size) {
    FILE* f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t* buf = (uint8_t*)malloc((size_t)n + 1);
    if (buf && fread(buf, 1, (size_t)n, f) != (size_t)n) { free(buf); buf = NULL; }
    fclose(f);
    if (size) *size = (size_t)n;
    return buf;
}

static uint32_t be32(const uint8_t* p) { return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3]; }

static int load_dol(const uint8_t* dol, size_t size) {
    if (size < 0x100) return 0;
    for (int i = 0; i < 18; i++) {
        uint32_t off = be32(dol + i * 4), addr = be32(dol + 0x48 + i * 4), sz = be32(dol + 0x90 + i * 4);
        if (!sz || !addr) continue;
        if ((size_t)off + sz > size) { gcrt_log("DOL section %d truncated", i); return 0; }
        memcpy(gcrt_ptr(addr), dol + off, sz);
    }
    uint32_t bss = be32(dol + 0xD8), bss_size = be32(dol + 0xDC);
    if (bss && bss_size) {
        /* BSS may overlap sdata sections on CodeWarrior DOLs; only clear real gaps. */
        for (uint32_t a = bss; a < bss + bss_size; a += 4) {
            int covered = 0;
            for (int i = 7; i < 18 && !covered; i++) {
                uint32_t sa = be32(dol + 0x48 + i * 4), ss = be32(dol + 0x90 + i * 4);
                covered = ss && a >= sa && a < sa + ss;
            }
            if (!covered) memset(gcrt_ptr(a), 0, 4);
        }
    }
    return 1;
}

static char* join(const char* dir, const char* name) {
    size_t n = strlen(dir) + strlen(name) + 2;
    char* s = (char*)malloc(n);
    if (!dir[0]) snprintf(s, n, "%s", name);
    else snprintf(s, n, "%s/%s", dir, name);
    return s;
}

/* Recreate what the IPL + apploader leave in low memory before jumping to the DOL. */
int gcrt_boot_load(PPCContext* ctx) {
    const char* dir = gcrt_opts.asset_dir ? gcrt_opts.asset_dir : ".";
    char* dol_path = gcrt_opts.dol_path ? NULL : join(dir, gcrt_game.default_dol);
    const char* dp = gcrt_opts.dol_path ? gcrt_opts.dol_path : dol_path;
    size_t dol_size = 0;
    uint8_t* dol = read_file(dp, &dol_size);
    if (!dol) {
        gcrt_log("cannot read executable '%s'", dp);
        free(dol_path);
        return 0;
    }
    if (!load_dol(dol, dol_size)) { free(dol); free(dol_path); return 0; }
    free(dol);
    free(dol_path);

    /* Disc image (optional for homebrew). */
    const char* disc = gcrt_opts.disc_path;
    char* disc_join = NULL;
    if (!disc && gcrt_game.default_disc && gcrt_game.default_disc[0]) {
        disc = gcrt_game.default_disc;
        FILE* t = fopen(disc, "rb");
        if (t) fclose(t);
        else disc = disc_join = join(dir, gcrt_game.default_disc);
    }
    int have_disc = disc && gcrt_disc_open(disc);
    if (disc && !have_disc) gcrt_log("warning: could not open disc image '%s' - DVD reads will fail", disc);
    free(disc_join);

    const uint32_t mem1_top = 0x81800000u;
    uint8_t hdr[0x440];
    memset(hdr, 0, sizeof hdr);
    if (have_disc) gcrt_disc_read(0, hdr, sizeof hdr);
    memcpy(gcrt_ptr(0x80000000u), hdr, 0x20);                     /* disc ID */
    mem_w32(0x80000020u, 0x0D15EA5Eu);                            /* boot magic */
    mem_w32(0x80000024u, 1u);                                     /* version */
    mem_w32(0x80000028u, GCRT_MEM1_SIZE);                         /* physical memory size */
    mem_w32(0x8000002Cu, gcrt_game.is_wii ? 0x00000023u : 0x00000003u); /* console type: retail */
    mem_w32(0x80000030u, 0);                                      /* arena lo: let OS compute */
    mem_w32(0x800000CCu, 0);                                      /* video mode: NTSC */
    mem_w32(0x800000F0u, GCRT_MEM1_SIZE);                         /* simulated memory size */
    mem_w32(0x800000F8u, gcrt_game.is_wii ? 243000000u : 162000000u);  /* bus clock */
    mem_w32(0x800000FCu, gcrt_game.is_wii ? 729000000u : 486000000u);  /* CPU clock */
    mem_w32(0x800030D8u, 0);

    uint32_t arena_hi = mem1_top;
    if (have_disc) {
        int shift = gcrt_disc_is_wii() ? 2 : 0;
        uint32_t fst_off = be32(hdr + 0x424) << shift, fst_size = be32(hdr + 0x428) << shift;
        uint32_t fst_max = be32(hdr + 0x42C) << shift;
        if (fst_size && fst_size < 0x01000000u) {
            uint32_t addr = (mem1_top - (fst_max > fst_size ? fst_max : fst_size)) & ~31u;
            gcrt_disc_read(fst_off, gcrt_ptr(addr), fst_size);
            mem_w32(0x80000038u, addr);
            mem_w32(0x8000003Cu, fst_max ? fst_max : fst_size);
            arena_hi = addr;
        }
        uint8_t bi2[0x2000];
        if (gcrt_disc_read(0x440, bi2, sizeof bi2)) {
            uint32_t bi2_addr = (arena_hi - sizeof bi2) & ~31u;
            memcpy(gcrt_ptr(bi2_addr), bi2, sizeof bi2);
            mem_w32(0x800000F4u, bi2_addr);
            arena_hi = bi2_addr;
        }
    }
    mem_w32(0x80000034u, arena_hi);                                /* arena hi */
    mem_w32(0x800000ECu, arena_hi);                                /* debug monitor / top */

    if (gcrt_game.is_wii) {
        /* Minimal Wii low-memory globals (normally written by the System Menu / IOS). */
        mem_w32(0x80003100u, GCRT_MEM1_SIZE);                      /* MEM1 physical size */
        mem_w32(0x80003104u, GCRT_MEM1_SIZE);                      /* MEM1 simulated size */
        mem_w32(0x8000310Cu, 0x81800000u);                         /* MEM1 arena end */
        mem_w32(0x80003118u, GCRT_MEM2_SIZE);                      /* MEM2 physical size */
        mem_w32(0x8000311Cu, GCRT_MEM2_SIZE);                      /* MEM2 simulated size */
        mem_w32(0x80003120u, 0x93400000u);                         /* MEM2 end */
        mem_w32(0x80003124u, 0x90000800u);                         /* usable MEM2 start */
        mem_w32(0x80003128u, 0x933E0000u);                         /* usable MEM2 end */
        mem_w32(0x80003130u, 0x933E0000u);                         /* IOS IPC buffer start */
        mem_w32(0x80003134u, 0x93400000u);                         /* IOS IPC buffer end */
        mem_w32(0x80003138u, 0x00000011u);                         /* hollywood version */
        mem_w32(0x80003140u, 0x00240001u);                         /* IOS36 v1 */
        mem_w32(0x80003188u, 0x00240001u);
        memcpy(gcrt_ptr(0x80003180u), hdr, 4);                     /* game id */
        mem_w32(0x800030C0u, 0);
    }

    memset(ctx, 0, sizeof *ctx);
    ctx->msr = 0x00002032u;          /* FP | IR | DR | RI (translation on, interrupts off) */
    ctx->r[1] = arena_hi - 0x100u;   /* safety stack; __start sets its own */
    ctx->spr[920] = 0xE0000000u;     /* HID2: paired singles + locked cache enabled */
    ctx->spr[1008] = 0x0011C664u;    /* HID0 as left by the IPL */
    ctx->spr[1009] = 0x80000000u;
    return 1;
}

static uint32_t parse_u32(const char* s, char** end) { return (uint32_t)strtoul(s, end, 0); }

void gcrt_memory_dump_at_exit(void) {
    if (!gcrt_opts.dump_spec || !gcrt_membase) return;
    char* end;
    uint32_t addr = parse_u32(gcrt_opts.dump_spec, &end);
    if (*end != ':') return;
    uint32_t len = parse_u32(end + 1, &end);
    if (*end != ':') return;
    FILE* f = fopen(end + 1, "wb");
    if (!f) return;
    fwrite(gcrt_ptr(addr), 1, len, f);
    fclose(f);
}

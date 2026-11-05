/* Runtime entry: option parsing, boot, and the host/platform loop. */
#include "gcrt_internal.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

GCRTOptions gcrt_opts;
PPCContext* gcrt_cpu;

static volatile int g_quit;
static volatile int g_exit_code;
static GCRTMutex* g_frame_lock;
static uint32_t* g_frame;
static int g_frame_w, g_frame_h, g_frame_dirty;
static int g_frames_presented;

void gcrt_log(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fputs("[gcrt] ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

void gcrt_dump_state(FILE* f) {
    PPCContext* c = gcrt_cpu;
    if (!c) return;
    fprintf(f, "  function 0x%08X  LR 0x%08X  CTR 0x%08X  CR 0x%08X  MSR 0x%08X\n",
            c->cur_func, c->lr, c->ctr, c->cr, c->msr);
    for (int i = 0; i < 32; i += 4)
        fprintf(f, "  r%-2d %08X  r%-2d %08X  r%-2d %08X  r%-2d %08X\n",
                i, c->r[i], i + 1, c->r[i + 1], i + 2, c->r[i + 2], i + 3, c->r[i + 3]);
}

void gcrt_exit(int code) {
    g_exit_code = code;
    g_quit = 1;
    /* The platform loop on the main thread finishes shutdown; park this guest thread. */
    for (;;) gcrt_platform_sleep_ns(1000000000ull);
}

/* Called from guest threads (VI retrace) - hand the frame to the main thread. */
void gcrt_platform_present_from_guest(const uint32_t* rgba, int w, int h);

static void usage(const char* argv0) {
    printf("%s - natively recompiled %s\n\n"
           "usage: %s [options]\n"
           "  --assets <dir>     directory holding main.dol / disc image (default: executable dir)\n"
           "  --dol <file>       override executable path\n"
           "  --disc <file>      disc image (GameCube ISO or decrypted Wii partition image)\n"
           "  --headless         no window / audio (CI, testing)\n"
           "  --frames <n>       exit after n presented frames\n"
           "  --scale <n>        window scale factor (default 2)\n"
           "  --dump a:len:file  write guest memory to file at exit\n"
           "  -v, --verbose      log unhandled hardware access\n",
           argv0, gcrt_game.title[0] ? gcrt_game.title : "GameCube/Wii program", argv0);
}

static void guest_main(void* arg) {
    PPCContext* ctx = (PPCContext*)arg;
    GCRTFunc entry = gcrt_lookup(gcrt_game.entry);
    if (!entry) gcrt_fatal("entry point 0x%08X was not recompiled", gcrt_game.entry);
    entry(ctx);
    gcrt_log("entry point returned (r3=%d)", (int)ctx->r[3]);
    gcrt_exit((int)ctx->r[3]);
}

static char* exe_dir(const char* argv0) {
    const char* slash = strrchr(argv0, '/');
#ifdef _WIN32
    const char* bs = strrchr(argv0, '\\');
    if (!slash || (bs && bs > slash)) slash = bs;
#endif
    if (!slash) { char* d = (char*)malloc(2); strcpy(d, "."); return d; }
    size_t n = (size_t)(slash - argv0);
    char* d = (char*)malloc(n + 1);
    memcpy(d, argv0, n);
    d[n] = 0;
    return d;
}

int gcrt_main(int argc, char** argv) {
    gcrt_opts.scale = 2;
    const char* env_dump = getenv("GCRT_DUMP");
    if (env_dump) gcrt_opts.dump_spec = env_dump;
    if (getenv("GCRT_HEADLESS")) gcrt_opts.headless = 1;
    for (int i = 1; i < argc; i++) {
        const char* a = argv[i];
        const char* next = i + 1 < argc ? argv[i + 1] : NULL;
        if (!strcmp(a, "--assets") && next) { gcrt_opts.asset_dir = next; i++; }
        else if (!strcmp(a, "--dol") && next) { gcrt_opts.dol_path = next; i++; }
        else if (!strcmp(a, "--disc") && next) { gcrt_opts.disc_path = next; i++; }
        else if (!strcmp(a, "--headless")) gcrt_opts.headless = 1;
        else if (!strcmp(a, "--frames") && next) { gcrt_opts.max_frames = atoi(next); i++; }
        else if (!strcmp(a, "--scale") && next) { gcrt_opts.scale = atoi(next); i++; }
        else if (!strcmp(a, "--dump") && next) { gcrt_opts.dump_spec = next; i++; }
        else if (!strcmp(a, "-v") || !strcmp(a, "--verbose")) gcrt_opts.verbose = 1;
        else if (!strcmp(a, "-h") || !strcmp(a, "--help")) { usage(argv[0]); return 0; }
        else { fprintf(stderr, "unknown option %s\n", a); usage(argv[0]); return 2; }
    }
    if (!gcrt_opts.asset_dir) gcrt_opts.asset_dir = exe_dir(argc ? argv[0] : ".");

    if (!gcrt_memory_init()) return 1;
    gcrt_cpu = (PPCContext*)calloc(1, sizeof(PPCContext));
    gcrt_threads_init();
    gcrt_exceptions_init();
    gcrt_dispatch_init();
    gcrt_hw_init();
    if (!gcrt_boot_load(gcrt_cpu)) return 1;

    char title[256];
    snprintf(title, sizeof title, "%s%s%s", gcrt_game.title[0] ? gcrt_game.title : "gcrt",
             gcrt_game.game_id[0] ? " - " : "", gcrt_game.game_id);
    if (!gcrt_platform_init(title, 640 * gcrt_opts.scale, 480 * gcrt_opts.scale, gcrt_opts.headless)) {
        gcrt_log("platform init failed; continuing headless");
        gcrt_platform_init(title, 640, 480, 1);
    }
    g_frame_lock = gcrt_mutex_create();
    gcrt_log("booting %s (%u functions, entry 0x%08X)", title, gcrt_game.func_count, gcrt_game.entry);

    if (!gcrt_thread_start(guest_main, gcrt_cpu)) { gcrt_log("cannot start guest thread"); return 1; }

    /* Host loop: events, frame presentation and the interrupt-poll timer. */
    while (!g_quit) {
        if (!gcrt_platform_pump()) { g_exit_code = 0; break; }
        gcrt_mutex_lock(g_frame_lock);
        if (g_frame_dirty) {
            gcrt_platform_present(g_frame, g_frame_w, g_frame_h);
            g_frame_dirty = 0;
            g_frames_presented++;
        }
        gcrt_mutex_unlock(g_frame_lock);
        if (gcrt_opts.max_frames && g_frames_presented >= gcrt_opts.max_frames) break;
        gcrt_poll_pending = 1;
        gcrt_platform_sleep_ns(1000000ull);
    }
    gcrt_memory_dump_at_exit();
    gcrt_platform_shutdown();
    fflush(stdout);
    return g_exit_code;
}

void gcrt_platform_present_from_guest(const uint32_t* rgba, int w, int h) {
    if (!g_frame_lock) return;
    gcrt_mutex_lock(g_frame_lock);
    if (!g_frame || g_frame_w != w || g_frame_h != h) {
        free(g_frame);
        g_frame = (uint32_t*)malloc((size_t)w * h * 4);
        g_frame_w = w; g_frame_h = h;
    }
    memcpy(g_frame, rgba, (size_t)w * h * 4);
    g_frame_dirty = 1;
    gcrt_mutex_unlock(g_frame_lock);
}

#ifndef GCRT_NO_MAIN
int main(int argc, char** argv) { return gcrt_main(argc, argv); }
#endif

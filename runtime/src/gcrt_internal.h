/* Internal interfaces shared between runtime modules. */
#ifndef GCRT_INTERNAL_H
#define GCRT_INTERNAL_H

#include "gcrt/gcrt.h"
#include <stdio.h>

#define GCRT_MSR_EE 0x8000u
#define GCRT_MSR_FP 0x2000u

/* Exception numbers used by the GameCube OS exception table at 0x80003000. */
enum {
    GCRT_EXC_SYSTEM_RESET = 0, GCRT_EXC_MACHINE_CHECK = 1, GCRT_EXC_DSI = 2, GCRT_EXC_ISI = 3,
    GCRT_EXC_EXTERNAL = 4, GCRT_EXC_ALIGNMENT = 5, GCRT_EXC_PROGRAM = 6, GCRT_EXC_FP_UNAVAIL = 7,
    GCRT_EXC_DECREMENTER = 8, GCRT_EXC_SYSCALL = 9, GCRT_EXC_TRACE = 10, GCRT_EXC_PERF = 11,
    GCRT_EXC_BREAKPOINT = 12, GCRT_EXC_SYSTEM_INT = 13, GCRT_EXC_THERMAL = 14
};

/* PI interrupt causes (PI_INTSR bits) */
enum {
    PI_INT_ERROR = 1u << 0, PI_INT_RSW = 1u << 1, PI_INT_DI = 1u << 2, PI_INT_SI = 1u << 3,
    PI_INT_EXI = 1u << 4, PI_INT_AI = 1u << 5, PI_INT_DSP = 1u << 6, PI_INT_MEM = 1u << 7,
    PI_INT_VI = 1u << 8, PI_INT_PE_TOKEN = 1u << 9, PI_INT_PE_FINISH = 1u << 10,
    PI_INT_CP = 1u << 11, PI_INT_DEBUG = 1u << 12, PI_INT_HSP = 1u << 13, PI_INT_IPC = 1u << 14,
    PI_INT_RSWST = 1u << 16
};

typedef struct GCRTOptions {
    const char* asset_dir;
    const char* dol_path;
    const char* disc_path;
    int headless;
    int max_frames;          /* exit after N presented frames (0 = unlimited) */
    int scale;
    int verbose;
    const char* dump_spec;   /* addr:len:file written at exit */
} GCRTOptions;

extern GCRTOptions gcrt_opts;
extern PPCContext* gcrt_cpu;

/* logging */
void gcrt_log(const char* fmt, ...);
void gcrt_dump_state(FILE* f);

/* memory / boot */
int  gcrt_memory_init(void);
int  gcrt_boot_load(PPCContext* ctx);
void gcrt_memory_dump_at_exit(void);

/* dispatch */
void gcrt_dispatch_init(void);

/* exceptions, timers, threads */
void     gcrt_exceptions_init(void);
void     gcrt_take_exception(PPCContext* ctx, int exc, uint32_t resume_pc);
void     gcrt_raise_irq(uint32_t pi_cause);
void     gcrt_clear_irq(uint32_t pi_cause);
uint32_t gcrt_get_dec(void);
void     gcrt_set_dec(uint32_t v);
uint64_t gcrt_host_ticks_to_tb(uint64_t ns);
uint32_t gcrt_tb_hz(void);
void     gcrt_threads_init(void);

/* hardware */
void     gcrt_hw_init(void);
void     gcrt_hw_tick(PPCContext* ctx, uint64_t now_ns);
uint32_t gcrt_pi_intsr(void);
uint32_t gcrt_pi_intmr(void);
int      gcrt_disc_read(uint64_t offset, void* dst, uint32_t len);
int      gcrt_disc_open(const char* path);
int      gcrt_disc_is_wii(void);

/* GX command processor */
void gcrt_gx_init(void);
void gcrt_gx_write(uint32_t value, int size);     /* write-gather pipe */
void gcrt_gx_run_fifo(void);
uint32_t gcrt_cp_read(uint32_t off, int size);
void gcrt_cp_write(uint32_t off, uint32_t v, int size);
uint32_t gcrt_pe_read(uint32_t off, int size);
void gcrt_pe_write(uint32_t off, uint32_t v, int size);

/* video */
void gcrt_vi_present(void);
void gcrt_platform_present_from_guest(const uint32_t* rgba, int w, int h);
uint32_t gcrt_vi_xfb_addr(void);

/* platform layer (SDL2 / Android / headless) */
typedef struct GCRTPadState {
    uint16_t buttons;        /* PAD_BUTTON_* layout of the SDK */
    int8_t stick_x, stick_y, substick_x, substick_y;
    uint8_t trigger_l, trigger_r;
    int connected;
} GCRTPadState;

int      gcrt_platform_init(const char* title, int width, int height, int headless);
void     gcrt_platform_shutdown(void);
int      gcrt_platform_pump(void);                    /* returns 0 when the user quits */
void     gcrt_platform_present(const uint32_t* rgba, int w, int h);
void     gcrt_platform_get_pad(int port, GCRTPadState* out);
uint64_t gcrt_platform_ticks_ns(void);
void     gcrt_platform_sleep_ns(uint64_t ns);
void     gcrt_platform_message(const char* title, const char* msg);
void     gcrt_platform_audio_push(const int16_t* samples, int frames, int rate);

/* portable threads (threads.c) */
typedef struct GCRTThread GCRTThread;
typedef void (*GCRTThreadFn)(void* arg);
GCRTThread* gcrt_thread_start(GCRTThreadFn fn, void* arg);
typedef struct GCRTMutex GCRTMutex;
typedef struct GCRTCond GCRTCond;
GCRTMutex* gcrt_mutex_create(void);
void gcrt_mutex_lock(GCRTMutex* m);
void gcrt_mutex_unlock(GCRTMutex* m);
GCRTCond* gcrt_cond_create(void);
void gcrt_cond_wait(GCRTCond* c, GCRTMutex* m);
void gcrt_cond_broadcast(GCRTCond* c);

#endif

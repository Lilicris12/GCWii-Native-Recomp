/* Headless platform: no window, no audio, no controllers. Used for CI and tests. */
#include "../gcrt_internal.h"
#include <stdio.h>

static int g_frames;

int gcrt_platform_init(const char* title, int w, int h, int headless) {
    (void)title; (void)w; (void)h; (void)headless;
    return 1;
}
void gcrt_platform_shutdown(void) {}
int gcrt_platform_pump(void) { return 1; }
void gcrt_platform_present(const uint32_t* rgba, int w, int h) { (void)rgba; (void)w; (void)h; g_frames++; }
void gcrt_platform_get_pad(int port, GCRTPadState* out) {
    memset(out, 0, sizeof *out);
    out->connected = port == 0;
}
void gcrt_platform_message(const char* title, const char* msg) { (void)title; (void)msg; }
void gcrt_platform_audio_push(const int16_t* s, int frames, int rate) { (void)s; (void)frames; (void)rate; }

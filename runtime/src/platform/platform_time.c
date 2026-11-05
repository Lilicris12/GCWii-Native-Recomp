/* Monotonic clock + sleep shared by all platform layers. */
#include "../gcrt_internal.h"

#if defined(_WIN32)
#include <windows.h>
uint64_t gcrt_platform_ticks_ns(void) {
    static LARGE_INTEGER freq;
    LARGE_INTEGER now;
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&now);
    return (uint64_t)((double)now.QuadPart * 1e9 / (double)freq.QuadPart);
}
void gcrt_platform_sleep_ns(uint64_t ns) {
    DWORD ms = (DWORD)(ns / 1000000ull);
    Sleep(ms ? ms : 0);
}
#else
#include <time.h>
uint64_t gcrt_platform_ticks_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}
void gcrt_platform_sleep_ns(uint64_t ns) {
    struct timespec ts = { (time_t)(ns / 1000000000ull), (long)(ns % 1000000000ull) };
    nanosleep(&ts, NULL);
}
#endif

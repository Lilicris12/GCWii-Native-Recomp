/*
 * Android platform layer without SDL: a pure NativeActivity (no Java code).
 * Frames are blitted with ANativeWindow_lock; gamepad keys/axes come from the
 * activity's input queue.  The runtime itself runs on its own thread.
 */
#include "../gcrt_internal.h"

#include <android/log.h>
#include <android/native_activity.h>
#include <android/native_window.h>
#include <android/input.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#define LOG(...) __android_log_print(ANDROID_LOG_INFO, "gcrt", __VA_ARGS__)

static ANativeActivity* g_activity;
static ANativeWindow* g_window;
static AInputQueue* g_queue;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static volatile int g_running = 1;
static GCRTPadState g_pad;

enum { PAD_LEFT = 0x0001, PAD_RIGHT = 0x0002, PAD_DOWN = 0x0004, PAD_UP = 0x0008, PAD_Z = 0x0010,
       PAD_R = 0x0020, PAD_L = 0x0040, PAD_A = 0x0100, PAD_B = 0x0200, PAD_X = 0x0400,
       PAD_Y = 0x0800, PAD_START = 0x1000 };

int gcrt_platform_init(const char* title, int w, int h, int headless) {
    (void)title; (void)w; (void)h; (void)headless;
    g_pad.connected = 1;
    return 1;
}
void gcrt_platform_shutdown(void) { if (g_activity) ANativeActivity_finish(g_activity); }

static uint16_t map_key(int32_t key) {
    switch (key) {
    case AKEYCODE_BUTTON_A: return PAD_A;
    case AKEYCODE_BUTTON_B: return PAD_B;
    case AKEYCODE_BUTTON_X: return PAD_X;
    case AKEYCODE_BUTTON_Y: return PAD_Y;
    case AKEYCODE_BUTTON_START: return PAD_START;
    case AKEYCODE_BUTTON_R1: return PAD_Z;
    case AKEYCODE_BUTTON_L2: return PAD_L;
    case AKEYCODE_BUTTON_R2: return PAD_R;
    case AKEYCODE_DPAD_UP: return PAD_UP;
    case AKEYCODE_DPAD_DOWN: return PAD_DOWN;
    case AKEYCODE_DPAD_LEFT: return PAD_LEFT;
    case AKEYCODE_DPAD_RIGHT: return PAD_RIGHT;
    default: return 0;
    }
}

static int8_t ax(float v) { return (int8_t)(v * 127.0f); }

int gcrt_platform_pump(void) {
    AInputEvent* ev = NULL;
    pthread_mutex_lock(&g_lock);
    AInputQueue* q = g_queue;
    while (q && AInputQueue_getEvent(q, &ev) >= 0) {
        if (AInputQueue_preDispatchEvent(q, ev)) continue;
        int handled = 0;
        if (AInputEvent_getType(ev) == AINPUT_EVENT_TYPE_KEY) {
            uint16_t b = map_key(AKeyEvent_getKeyCode(ev));
            if (b) {
                if (AKeyEvent_getAction(ev) == AKEY_EVENT_ACTION_DOWN) g_pad.buttons |= b;
                else g_pad.buttons &= (uint16_t)~b;
                handled = 1;
            }
        } else if (AInputEvent_getType(ev) == AINPUT_EVENT_TYPE_MOTION) {
            g_pad.stick_x = ax(AMotionEvent_getAxisValue(ev, AMOTION_EVENT_AXIS_X, 0));
            g_pad.stick_y = (int8_t)-ax(AMotionEvent_getAxisValue(ev, AMOTION_EVENT_AXIS_Y, 0));
            g_pad.substick_x = ax(AMotionEvent_getAxisValue(ev, AMOTION_EVENT_AXIS_Z, 0));
            g_pad.substick_y = (int8_t)-ax(AMotionEvent_getAxisValue(ev, AMOTION_EVENT_AXIS_RZ, 0));
            g_pad.trigger_l = (uint8_t)(AMotionEvent_getAxisValue(ev, AMOTION_EVENT_AXIS_LTRIGGER, 0) * 255.0f);
            g_pad.trigger_r = (uint8_t)(AMotionEvent_getAxisValue(ev, AMOTION_EVENT_AXIS_RTRIGGER, 0) * 255.0f);
            handled = 1;
        }
        AInputQueue_finishEvent(q, ev, handled);
    }
    pthread_mutex_unlock(&g_lock);
    return g_running;
}

void gcrt_platform_present(const uint32_t* rgba, int w, int h) {
    pthread_mutex_lock(&g_lock);
    if (g_window) {
        ANativeWindow_setBuffersGeometry(g_window, w, h, WINDOW_FORMAT_RGBA_8888);
        ANativeWindow_Buffer buf;
        if (ANativeWindow_lock(g_window, &buf, NULL) == 0) {
            for (int y = 0; y < h && y < buf.height; y++)
                memcpy((uint32_t*)buf.bits + (size_t)y * buf.stride, rgba + (size_t)y * w,
                       (size_t)(w < buf.width ? w : buf.width) * 4);
            ANativeWindow_unlockAndPost(g_window);
        }
    }
    pthread_mutex_unlock(&g_lock);
}

void gcrt_platform_get_pad(int port, GCRTPadState* out) {
    if (port == 0) *out = g_pad; else memset(out, 0, sizeof *out);
}
void gcrt_platform_message(const char* title, const char* msg) { LOG("%s: %s", title, msg); }
void gcrt_platform_audio_push(const int16_t* s, int frames, int rate) { (void)s; (void)frames; (void)rate; }

/* --- NativeActivity glue --------------------------------------------------- */
static void* runtime_thread(void* arg) {
    char* argv[3];
    static char assets[512];
    (void)arg;
    /* Game data is expected in the app's external files dir (adb push main.dol / disc.img there). */
    snprintf(assets, sizeof assets, "%s", g_activity->externalDataPath ? g_activity->externalDataPath : ".");
    argv[0] = (char*)"gcrt"; argv[1] = (char*)"--assets"; argv[2] = assets;
    int rc = gcrt_main(3, argv);
    LOG("runtime exited with %d", rc);
    return NULL;
}

static void on_window_created(ANativeActivity* a, ANativeWindow* w) { (void)a; pthread_mutex_lock(&g_lock); g_window = w; pthread_mutex_unlock(&g_lock); }
static void on_window_destroyed(ANativeActivity* a, ANativeWindow* w) { (void)a; (void)w; pthread_mutex_lock(&g_lock); g_window = NULL; pthread_mutex_unlock(&g_lock); }
static void on_queue_created(ANativeActivity* a, AInputQueue* q) { (void)a; pthread_mutex_lock(&g_lock); g_queue = q; pthread_mutex_unlock(&g_lock); }
static void on_queue_destroyed(ANativeActivity* a, AInputQueue* q) { (void)a; (void)q; pthread_mutex_lock(&g_lock); g_queue = NULL; pthread_mutex_unlock(&g_lock); }
static void on_destroy(ANativeActivity* a) { (void)a; g_running = 0; }

JNIEXPORT void ANativeActivity_onCreate(ANativeActivity* activity, void* saved, size_t saved_size) {
    (void)saved; (void)saved_size;
    g_activity = activity;
    activity->callbacks->onNativeWindowCreated = on_window_created;
    activity->callbacks->onNativeWindowDestroyed = on_window_destroyed;
    activity->callbacks->onInputQueueCreated = on_queue_created;
    activity->callbacks->onInputQueueDestroyed = on_queue_destroyed;
    activity->callbacks->onDestroy = on_destroy;
    pthread_t t;
    pthread_create(&t, NULL, runtime_thread, NULL);
    pthread_detach(t);
}

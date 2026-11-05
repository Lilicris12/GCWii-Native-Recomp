/* Minimal portable thread primitives (Win32 or pthreads). */
#include "gcrt_internal.h"
#include <stdlib.h>

#if defined(_WIN32)
#include <windows.h>

struct GCRTThread { HANDLE h; GCRTThreadFn fn; void* arg; };
struct GCRTMutex { CRITICAL_SECTION cs; };
struct GCRTCond { CONDITION_VARIABLE cv; };

static DWORD WINAPI thread_tramp(LPVOID p) {
    GCRTThread* t = (GCRTThread*)p;
    t->fn(t->arg);
    return 0;
}
GCRTThread* gcrt_thread_start(GCRTThreadFn fn, void* arg) {
    GCRTThread* t = (GCRTThread*)calloc(1, sizeof *t);
    t->fn = fn; t->arg = arg;
    t->h = CreateThread(NULL, 8u << 20, thread_tramp, t, 0, NULL);
    return t->h ? t : NULL;
}
GCRTMutex* gcrt_mutex_create(void) { GCRTMutex* m = (GCRTMutex*)calloc(1, sizeof *m); InitializeCriticalSection(&m->cs); return m; }
void gcrt_mutex_lock(GCRTMutex* m) { EnterCriticalSection(&m->cs); }
void gcrt_mutex_unlock(GCRTMutex* m) { LeaveCriticalSection(&m->cs); }
GCRTCond* gcrt_cond_create(void) { GCRTCond* c = (GCRTCond*)calloc(1, sizeof *c); InitializeConditionVariable(&c->cv); return c; }
void gcrt_cond_wait(GCRTCond* c, GCRTMutex* m) { SleepConditionVariableCS(&c->cv, &m->cs, INFINITE); }
void gcrt_cond_broadcast(GCRTCond* c) { WakeAllConditionVariable(&c->cv); }

#else
#include <pthread.h>

struct GCRTThread { pthread_t t; GCRTThreadFn fn; void* arg; };
struct GCRTMutex { pthread_mutex_t m; };
struct GCRTCond { pthread_cond_t c; };

static void* thread_tramp(void* p) {
    GCRTThread* t = (GCRTThread*)p;
    t->fn(t->arg);
    return NULL;
}
GCRTThread* gcrt_thread_start(GCRTThreadFn fn, void* arg) {
    GCRTThread* t = (GCRTThread*)calloc(1, sizeof *t);
    pthread_attr_t attr;
    t->fn = fn; t->arg = arg;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 8u << 20);   /* deep recompiled call chains */
    if (pthread_create(&t->t, &attr, thread_tramp, t) != 0) { free(t); t = NULL; }
    pthread_attr_destroy(&attr);
    return t;
}
GCRTMutex* gcrt_mutex_create(void) { GCRTMutex* m = (GCRTMutex*)calloc(1, sizeof *m); pthread_mutex_init(&m->m, NULL); return m; }
void gcrt_mutex_lock(GCRTMutex* m) { pthread_mutex_lock(&m->m); }
void gcrt_mutex_unlock(GCRTMutex* m) { pthread_mutex_unlock(&m->m); }
GCRTCond* gcrt_cond_create(void) { GCRTCond* c = (GCRTCond*)calloc(1, sizeof *c); pthread_cond_init(&c->c, NULL); return c; }
void gcrt_cond_wait(GCRTCond* c, GCRTMutex* m) { pthread_cond_wait(&c->c, &m->m); }
void gcrt_cond_broadcast(GCRTCond* c) { pthread_cond_broadcast(&c->c); }
#endif

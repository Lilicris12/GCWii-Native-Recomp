/* SDL2 platform layer (Windows, Linux, macOS; Android via SDL's Java glue). */
#include "../gcrt_internal.h"
#define SDL_MAIN_HANDLED
#include <SDL.h>

static SDL_Window* g_win;
static SDL_Renderer* g_ren;
static SDL_Texture* g_tex;
static int g_tex_w, g_tex_h;
static SDL_GameController* g_pads[4];
static SDL_AudioDeviceID g_audio;
static int g_headless;
static GCRTPadState g_state[4];

enum { PAD_LEFT = 0x0001, PAD_RIGHT = 0x0002, PAD_DOWN = 0x0004, PAD_UP = 0x0008, PAD_Z = 0x0010,
       PAD_R = 0x0020, PAD_L = 0x0040, PAD_A = 0x0100, PAD_B = 0x0200, PAD_X = 0x0400,
       PAD_Y = 0x0800, PAD_START = 0x1000 };

int gcrt_platform_init(const char* title, int w, int h, int headless) {
    g_headless = headless;
    if (headless) return 1;
    SDL_SetMainReady();
    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_GAMECONTROLLER | SDL_INIT_AUDIO) != 0) {
        gcrt_log("SDL_Init failed: %s", SDL_GetError());
        return 0;
    }
    g_win = SDL_CreateWindow(title, SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, w, h,
                             SDL_WINDOW_RESIZABLE | SDL_WINDOW_ALLOW_HIGHDPI);
    if (!g_win) { gcrt_log("SDL_CreateWindow: %s", SDL_GetError()); return 0; }
    g_ren = SDL_CreateRenderer(g_win, -1, SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC);
    if (!g_ren) g_ren = SDL_CreateRenderer(g_win, -1, 0);
    if (!g_ren) { gcrt_log("SDL_CreateRenderer: %s", SDL_GetError()); return 0; }
    SDL_RenderSetLogicalSize(g_ren, 640, 480);
    SDL_AudioSpec want, have;
    SDL_zero(want);
    want.freq = 48000; want.format = AUDIO_S16SYS; want.channels = 2; want.samples = 1024;
    g_audio = SDL_OpenAudioDevice(NULL, 0, &want, &have, 0);
    if (g_audio) SDL_PauseAudioDevice(g_audio, 0);
    g_state[0].connected = 1;   /* keyboard always acts as controller 1 */
    return 1;
}

void gcrt_platform_shutdown(void) {
    if (g_headless) return;
    if (g_audio) SDL_CloseAudioDevice(g_audio);
    if (g_tex) SDL_DestroyTexture(g_tex);
    if (g_ren) SDL_DestroyRenderer(g_ren);
    if (g_win) SDL_DestroyWindow(g_win);
    SDL_Quit();
}

static int8_t axis8(Sint16 v) { int x = v / 256; return (int8_t)(x < -127 ? -127 : x > 127 ? 127 : x); }

static void update_pads(void) {
    const Uint8* k = SDL_GetKeyboardState(NULL);
    GCRTPadState s;
    memset(&s, 0, sizeof s);
    s.connected = 1;
    if (k[SDL_SCANCODE_X]) s.buttons |= PAD_A;
    if (k[SDL_SCANCODE_Z]) s.buttons |= PAD_B;
    if (k[SDL_SCANCODE_C]) s.buttons |= PAD_X;
    if (k[SDL_SCANCODE_S]) s.buttons |= PAD_Y;
    if (k[SDL_SCANCODE_D]) s.buttons |= PAD_Z;
    if (k[SDL_SCANCODE_Q]) { s.buttons |= PAD_L; s.trigger_l = 255; }
    if (k[SDL_SCANCODE_W]) { s.buttons |= PAD_R; s.trigger_r = 255; }
    if (k[SDL_SCANCODE_RETURN]) s.buttons |= PAD_START;
    if (k[SDL_SCANCODE_T]) s.buttons |= PAD_UP;
    if (k[SDL_SCANCODE_G]) s.buttons |= PAD_DOWN;
    if (k[SDL_SCANCODE_F]) s.buttons |= PAD_LEFT;
    if (k[SDL_SCANCODE_H]) s.buttons |= PAD_RIGHT;
    if (k[SDL_SCANCODE_LEFT]) s.stick_x = -100;
    if (k[SDL_SCANCODE_RIGHT]) s.stick_x = 100;
    if (k[SDL_SCANCODE_UP]) s.stick_y = 100;
    if (k[SDL_SCANCODE_DOWN]) s.stick_y = -100;
    if (k[SDL_SCANCODE_J]) s.substick_x = -100;
    if (k[SDL_SCANCODE_L]) s.substick_x = 100;
    if (k[SDL_SCANCODE_I]) s.substick_y = 100;
    if (k[SDL_SCANCODE_K]) s.substick_y = -100;

    for (int p = 0; p < 4; p++) {
        GCRTPadState out = p == 0 ? s : (GCRTPadState){0};
        SDL_GameController* gc = g_pads[p];
        if (gc) {
            out.connected = 1;
            if (SDL_GameControllerGetButton(gc, SDL_CONTROLLER_BUTTON_A)) out.buttons |= PAD_A;
            if (SDL_GameControllerGetButton(gc, SDL_CONTROLLER_BUTTON_B)) out.buttons |= PAD_B;
            if (SDL_GameControllerGetButton(gc, SDL_CONTROLLER_BUTTON_X)) out.buttons |= PAD_X;
            if (SDL_GameControllerGetButton(gc, SDL_CONTROLLER_BUTTON_Y)) out.buttons |= PAD_Y;
            if (SDL_GameControllerGetButton(gc, SDL_CONTROLLER_BUTTON_START)) out.buttons |= PAD_START;
            if (SDL_GameControllerGetButton(gc, SDL_CONTROLLER_BUTTON_RIGHTSHOULDER)) out.buttons |= PAD_Z;
            if (SDL_GameControllerGetButton(gc, SDL_CONTROLLER_BUTTON_DPAD_UP)) out.buttons |= PAD_UP;
            if (SDL_GameControllerGetButton(gc, SDL_CONTROLLER_BUTTON_DPAD_DOWN)) out.buttons |= PAD_DOWN;
            if (SDL_GameControllerGetButton(gc, SDL_CONTROLLER_BUTTON_DPAD_LEFT)) out.buttons |= PAD_LEFT;
            if (SDL_GameControllerGetButton(gc, SDL_CONTROLLER_BUTTON_DPAD_RIGHT)) out.buttons |= PAD_RIGHT;
            out.stick_x = axis8(SDL_GameControllerGetAxis(gc, SDL_CONTROLLER_AXIS_LEFTX));
            out.stick_y = (int8_t)-axis8(SDL_GameControllerGetAxis(gc, SDL_CONTROLLER_AXIS_LEFTY));
            out.substick_x = axis8(SDL_GameControllerGetAxis(gc, SDL_CONTROLLER_AXIS_RIGHTX));
            out.substick_y = (int8_t)-axis8(SDL_GameControllerGetAxis(gc, SDL_CONTROLLER_AXIS_RIGHTY));
            Sint16 tl = SDL_GameControllerGetAxis(gc, SDL_CONTROLLER_AXIS_TRIGGERLEFT);
            Sint16 tr = SDL_GameControllerGetAxis(gc, SDL_CONTROLLER_AXIS_TRIGGERRIGHT);
            out.trigger_l = (uint8_t)(tl / 128); out.trigger_r = (uint8_t)(tr / 128);
            if (tl > 30000) out.buttons |= PAD_L;
            if (tr > 30000) out.buttons |= PAD_R;
            if (p == 0) { out.buttons |= s.buttons; }
        }
        g_state[p] = out;
    }
}

int gcrt_platform_pump(void) {
    if (g_headless) return 1;
    SDL_Event ev;
    while (SDL_PollEvent(&ev)) {
        switch (ev.type) {
        case SDL_QUIT: return 0;
        case SDL_KEYDOWN:
            if (ev.key.keysym.sym == SDLK_ESCAPE) return 0;
            if (ev.key.keysym.sym == SDLK_F11) {
                Uint32 fs = SDL_GetWindowFlags(g_win) & SDL_WINDOW_FULLSCREEN_DESKTOP;
                SDL_SetWindowFullscreen(g_win, fs ? 0 : SDL_WINDOW_FULLSCREEN_DESKTOP);
            }
            break;
        case SDL_CONTROLLERDEVICEADDED:
            for (int p = 0; p < 4; p++)
                if (!g_pads[p]) { g_pads[p] = SDL_GameControllerOpen(ev.cdevice.which); break; }
            break;
        case SDL_CONTROLLERDEVICEREMOVED:
            for (int p = 0; p < 4; p++)
                if (g_pads[p] && SDL_JoystickInstanceID(SDL_GameControllerGetJoystick(g_pads[p])) == ev.cdevice.which) {
                    SDL_GameControllerClose(g_pads[p]);
                    g_pads[p] = NULL;
                }
            break;
        default: break;
        }
    }
    update_pads();
    return 1;
}

void gcrt_platform_present(const uint32_t* rgba, int w, int h) {
    if (g_headless || !g_ren) return;
    if (!g_tex || g_tex_w != w || g_tex_h != h) {
        if (g_tex) SDL_DestroyTexture(g_tex);
        g_tex = SDL_CreateTexture(g_ren, SDL_PIXELFORMAT_RGBA32, SDL_TEXTUREACCESS_STREAMING, w, h);
        g_tex_w = w; g_tex_h = h;
        SDL_RenderSetLogicalSize(g_ren, w, h);
    }
    SDL_UpdateTexture(g_tex, NULL, rgba, w * 4);
    SDL_RenderClear(g_ren);
    SDL_RenderCopy(g_ren, g_tex, NULL, NULL);
    SDL_RenderPresent(g_ren);
}

void gcrt_platform_get_pad(int port, GCRTPadState* out) {
    if (port < 0 || port > 3) { memset(out, 0, sizeof *out); return; }
    if (g_headless) { memset(out, 0, sizeof *out); out->connected = port == 0; return; }
    *out = g_state[port];
}

void gcrt_platform_message(const char* title, const char* msg) {
    if (!g_headless) SDL_ShowSimpleMessageBox(SDL_MESSAGEBOX_ERROR, title, msg, g_win);
}

void gcrt_platform_audio_push(const int16_t* samples, int frames, int rate) {
    (void)rate;
    if (g_audio && SDL_GetQueuedAudioSize(g_audio) < 48000u * 4u / 5u)
        SDL_QueueAudio(g_audio, samples, (Uint32)frames * 4u);
}

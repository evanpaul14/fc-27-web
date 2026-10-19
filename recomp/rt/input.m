// Host input for the npad HLE (hle/npad.c): player 1's Pro Controller state from the keyboard (window key events),
// any connected gamepad (GameController framework) and FIFA_PRESS scripts, merged each time the game samples.
//
// Keyboard (key positions, so any layout):
//   arrows      d-pad              WASD        left stick       IJKL     right stick
//   Return/Space A                 Esc/Delete  B                X / Y    X / Y
//   Q / E        L / R             1 / 3       ZL / ZR          = / -    Plus / Minus
//   F / H        left / right stick click
// Gamepads map by position: the east face button is A, south B, north X, west Y.
//
// FIFA_PRESS=<frame>:<button>[:<frames held>],... presses buttons at presented-frame numbers, for headless runs:
// FIFA_PRESS=600:a,640:down,680:a. Buttons: a b x y l r zl zr plus minus up down left right lstick rstick
// (held 6 frames by default).
//
// FIFA_CONTROL=<fifo> drives a running game: a thread reads lines of ';'-separated commands from the named pipe
// (created if missing), each finishing before the next starts:
//   <button> [n]                    press n times (6 frames held, 12 apart)
//   hold <button> <frames>
//   stick <l|r> <x> <y> <frames>    x, y in -1..1
//   wait <frames>
//   shot [path]                     save the next presented image (default build/live.png; written whole, then renamed)
// e.g. echo 'down 4; a; wait 60; shot' > /tmp/fifa.ctl
#import <AppKit/AppKit.h>
#import <Carbon/Carbon.h>
#import <GameController/GameController.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>

#include "host.h"
#include "input.h"

enum {
    BTN_A = 1 << 0, BTN_B = 1 << 1, BTN_X = 1 << 2, BTN_Y = 1 << 3, BTN_LSTICK = 1 << 4, BTN_RSTICK = 1 << 5,
    BTN_L = 1 << 6, BTN_R = 1 << 7, BTN_ZL = 1 << 8, BTN_ZR = 1 << 9, BTN_PLUS = 1 << 10, BTN_MINUS = 1 << 11,
    BTN_LEFT = 1 << 12, BTN_UP = 1 << 13, BTN_RIGHT = 1 << 14, BTN_DOWN = 1 << 15,
};
#define STICK_MAX 32767

// ---- keyboard: key-down set, written on the main thread
static _Atomic uint64_t kb_buttons;  // npad button bits
static _Atomic uint32_t kb_dirs;     // stick directions: bits 0-3 left stick L/U/R/D, 4-7 right stick

static const struct { unsigned short key; uint64_t button; uint32_t dir; } keymap[] = {
    {kVK_LeftArrow, BTN_LEFT, 0}, {kVK_UpArrow, BTN_UP, 0}, {kVK_RightArrow, BTN_RIGHT, 0}, {kVK_DownArrow, BTN_DOWN, 0},
    {kVK_Return, BTN_A, 0}, {kVK_Space, BTN_A, 0}, {kVK_Escape, BTN_B, 0}, {kVK_Delete, BTN_B, 0},
    {kVK_ANSI_X, BTN_X, 0}, {kVK_ANSI_Y, BTN_Y, 0}, {kVK_ANSI_Q, BTN_L, 0}, {kVK_ANSI_E, BTN_R, 0},
    {kVK_ANSI_1, BTN_ZL, 0}, {kVK_ANSI_3, BTN_ZR, 0}, {kVK_ANSI_Equal, BTN_PLUS, 0}, {kVK_ANSI_Minus, BTN_MINUS, 0},
    {kVK_ANSI_F, BTN_LSTICK, 0}, {kVK_ANSI_H, BTN_RSTICK, 0},
    {kVK_ANSI_A, 0, 1 << 0}, {kVK_ANSI_W, 0, 1 << 1}, {kVK_ANSI_D, 0, 1 << 2}, {kVK_ANSI_S, 0, 1 << 3},
    {kVK_ANSI_J, 0, 1 << 4}, {kVK_ANSI_I, 0, 1 << 5}, {kVK_ANSI_L, 0, 1 << 6}, {kVK_ANSI_K, 0, 1 << 7},
};

void input_key(unsigned short key, bool down) {
    for (size_t i = 0; i < sizeof keymap / sizeof *keymap; i++) {
        if (keymap[i].key != key) continue;
        if (down) {
            atomic_fetch_or(&kb_buttons, keymap[i].button);
            atomic_fetch_or(&kb_dirs, keymap[i].dir);
        } else {
            atomic_fetch_and(&kb_buttons, ~keymap[i].button);
            atomic_fetch_and(&kb_dirs, ~keymap[i].dir);
        }
    }
}
void input_release_all(void) {  // focus lost: key-up events go elsewhere
    atomic_store(&kb_buttons, 0);
    atomic_store(&kb_dirs, 0);
}

static void dirs_to_stick(uint32_t d, int32_t *x, int32_t *y) {
    *x = (d & 4 ? STICK_MAX : 0) - (d & 1 ? STICK_MAX : 0);
    *y = (d & 2 ? STICK_MAX : 0) - (d & 8 ? STICK_MAX : 0);
}

// ---- gamepads: polled from whichever thread samples (GCController values are plain properties)
static void gamepad_state(InputState *s) {
    for (GCController *c in GCController.controllers) {
        GCExtendedGamepad *g = c.extendedGamepad;
        if (!g) continue;
        uint64_t b = 0;
        if (g.buttonB.pressed) b |= BTN_A;
        if (g.buttonA.pressed) b |= BTN_B;
        if (g.buttonY.pressed) b |= BTN_X;
        if (g.buttonX.pressed) b |= BTN_Y;
        if (g.leftShoulder.pressed) b |= BTN_L;
        if (g.rightShoulder.pressed) b |= BTN_R;
        if (g.leftTrigger.pressed) b |= BTN_ZL;
        if (g.rightTrigger.pressed) b |= BTN_ZR;
        if (g.buttonMenu.pressed) b |= BTN_PLUS;
        if (g.buttonOptions.pressed) b |= BTN_MINUS;
        if (g.leftThumbstickButton.pressed) b |= BTN_LSTICK;
        if (g.rightThumbstickButton.pressed) b |= BTN_RSTICK;
        if (g.dpad.left.pressed) b |= BTN_LEFT;
        if (g.dpad.up.pressed) b |= BTN_UP;
        if (g.dpad.right.pressed) b |= BTN_RIGHT;
        if (g.dpad.down.pressed) b |= BTN_DOWN;
        s->buttons |= b;
        int32_t lx = (int32_t)(g.leftThumbstick.xAxis.value * STICK_MAX), ly = (int32_t)(g.leftThumbstick.yAxis.value * STICK_MAX);
        int32_t rx = (int32_t)(g.rightThumbstick.xAxis.value * STICK_MAX), ry = (int32_t)(g.rightThumbstick.yAxis.value * STICK_MAX);
        if (lx || ly) { s->lx = lx; s->ly = ly; }
        if (rx || ry) { s->rx = rx; s->ry = ry; }
    }
}

// ---- FIFA_PRESS scripts and FIFA_CONTROL commands
typedef struct { uint64_t frame, until; uint64_t button; int32_t x, y; int stick; } Press;  // stick: 0 none, 1 l, 2 r
static Press *presses;
static int npresses;
static pthread_mutex_t presses_lock = PTHREAD_MUTEX_INITIALIZER;  // the control thread appends

static void add_press(Press p) {
    pthread_mutex_lock(&presses_lock);
    presses = realloc(presses, (npresses + 1) * sizeof *presses);
    presses[npresses++] = p;
    pthread_mutex_unlock(&presses_lock);
}

static const struct { const char *name; uint64_t button; } button_names[] = {
    {"a", BTN_A}, {"b", BTN_B}, {"x", BTN_X}, {"y", BTN_Y}, {"l", BTN_L}, {"r", BTN_R}, {"zl", BTN_ZL}, {"zr", BTN_ZR},
    {"plus", BTN_PLUS}, {"minus", BTN_MINUS}, {"up", BTN_UP}, {"down", BTN_DOWN}, {"left", BTN_LEFT},
    {"right", BTN_RIGHT}, {"lstick", BTN_LSTICK}, {"rstick", BTN_RSTICK},
};

static void parse_presses(void) {
    const char *env = getenv("FIFA_PRESS");
    if (!env) return;
    char *s = strdup(env), *save = 0;
    for (char *tok = strtok_r(s, ",", &save); tok; tok = strtok_r(0, ",", &save)) {
        char name[16] = {0};
        unsigned long long frame = 0, held = 6;
        if (sscanf(tok, "%llu:%15[a-z]:%llu", &frame, name, &held) < 2) {
            HLOG("[input] bad FIFA_PRESS entry '%s'\n", tok);
            continue;
        }
        uint64_t button = 0;
        for (size_t i = 0; i < sizeof button_names / sizeof *button_names; i++)
            if (!strcmp(name, button_names[i].name)) button = button_names[i].button;
        if (!button) {
            HLOG("[input] unknown button '%s' in FIFA_PRESS\n", name);
            continue;
        }
        add_press((Press){frame, frame + held, button});
    }
    free(s);
}

static void script_state(InputState *s) {
    static int logged;  // presses before this one already reported (they start in order)
    uint64_t now = renderer_present_count();
    pthread_mutex_lock(&presses_lock);
    for (int i = 0; i < npresses; i++)
        if (now >= presses[i].frame && now < presses[i].until) {
            s->buttons |= presses[i].button;
            if (presses[i].stick == 1) { s->lx = presses[i].x; s->ly = presses[i].y; }
            if (presses[i].stick == 2) { s->rx = presses[i].x; s->ry = presses[i].y; }
            if (i >= logged) {
                logged = i + 1;
                HLOG("[input] frame %llu: press %#llx\n", (unsigned long long)now, (unsigned long long)presses[i].button);
            }
        }
    pthread_mutex_unlock(&presses_lock);
}

// ---- FIFA_CONTROL
static _Atomic(char *) shot_path;  // taken by the next present (input_take_shot)
static _Atomic uint64_t shots_done;

char *input_take_shot(void) { return atomic_exchange(&shot_path, NULL); }
void input_shot_done(void) { atomic_fetch_add(&shots_done, 1); }

static void wait_frame(uint64_t frame) {
    while (renderer_present_count() < frame) usleep(2000);
}

static uint64_t button_named(const char *name) {
    for (size_t i = 0; i < sizeof button_names / sizeof *button_names; i++)
        if (!strcmp(name, button_names[i].name)) return button_names[i].button;
    return 0;
}

static void control_command(char *cmd) {
    char a[64] = {0}, b[256] = {0};
    double x = 0, y = 0;
    long n = 0;
    int k = sscanf(cmd, "%63s", a);
    if (k < 1) return;
    uint64_t now = renderer_present_count();
    if (!strcmp(a, "wait") && sscanf(cmd, "%*s %ld", &n) == 1) {
        wait_frame(now + n);
    } else if (!strcmp(a, "hold") && sscanf(cmd, "%*s %63s %ld", b, &n) == 2 && button_named(b)) {
        add_press((Press){now + 1, now + 1 + n, button_named(b)});
        wait_frame(now + 1 + n + 2);
    } else if (!strcmp(a, "stick") && sscanf(cmd, "%*s %63s %lf %lf %ld", b, &x, &y, &n) == 4 && (*b == 'l' || *b == 'r')) {
        add_press((Press){now + 1, now + 1 + n, 0, (int32_t)(x * STICK_MAX), (int32_t)(y * STICK_MAX), *b == 'l' ? 1 : 2});
        wait_frame(now + 1 + n + 2);
    } else if (!strcmp(a, "shot")) {
        if (sscanf(cmd, "%*s %255s", b) != 1) strcpy(b, "build/live.png");
        uint64_t done = atomic_load(&shots_done);
        free(atomic_exchange(&shot_path, strdup(b)));
        while (atomic_load(&shots_done) == done) usleep(2000);
    } else if (button_named(a)) {
        if (sscanf(cmd, "%*s %ld", &n) != 1) n = 1;
        for (long i = 0; i < n; i++) add_press((Press){now + 1 + i * 12, now + 7 + i * 12, button_named(a)});
        wait_frame(now + 1 + n * 12);
    } else {
        HLOG("[control] bad command '%s'\n", cmd);
        return;
    }
    HLOG("[control] %s (frame %llu)\n", cmd, (unsigned long long)renderer_present_count());
}

static void *control_thread(void *arg) {
    const char *path = arg;
    for (;;) {
        FILE *f = fopen(path, "r");  // blocks until a writer opens the pipe
        if (!f) { HLOG("[control] can't open %s\n", path); return NULL; }
        char *line = NULL, *save;
        size_t cap = 0;
        while (getline(&line, &cap, f) > 0)
            for (char *c = strtok_r(line, ";\n", &save); c; c = strtok_r(NULL, ";\n", &save)) control_command(c);
        free(line);
        fclose(f);
    }
}

static void start_control(void) {
    const char *path = getenv("FIFA_CONTROL");
    if (!path) return;
    if (mkfifo(path, 0600) && errno != EEXIST) { HLOG("[control] mkfifo %s failed\n", path); return; }
    pthread_t t;
    pthread_create(&t, NULL, control_thread, strdup(path));
    HLOG("[control] reading commands from %s\n", path);
}

void input_state(InputState *s) {
    static dispatch_once_t once;
    dispatch_once(&once, ^{ parse_presses(); start_control(); });
    memset(s, 0, sizeof *s);
    s->buttons = atomic_load(&kb_buttons);
    uint32_t d = atomic_load(&kb_dirs);
    dirs_to_stick(d & 15, &s->lx, &s->ly);
    dirs_to_stick(d >> 4, &s->rx, &s->ry);
    script_state(s);
    @autoreleasepool {
        gamepad_state(s);
    }
    // The stick direction bits the game also reads as buttons (StickL/R Left/Up/Right/Down, bits 16-23).
    int32_t t = STICK_MAX / 2;
    if (s->lx < -t) s->buttons |= 1ull << 16;
    if (s->ly > t) s->buttons |= 1ull << 17;
    if (s->lx > t) s->buttons |= 1ull << 18;
    if (s->ly < -t) s->buttons |= 1ull << 19;
    if (s->rx < -t) s->buttons |= 1ull << 20;
    if (s->ry > t) s->buttons |= 1ull << 21;
    if (s->rx > t) s->buttons |= 1ull << 22;
    if (s->ry < -t) s->buttons |= 1ull << 23;
}

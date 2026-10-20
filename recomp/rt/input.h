/* Host input (rt/input.m): player 1's controller state for the npad HLE. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef struct { uint64_t buttons; int32_t lx, ly, rx, ry; } InputState;  /* nn::hid::NpadButton bits, sticks +-32767 */

void input_state(InputState *s);              /* any thread */
void input_key(unsigned short key, bool down);  /* main thread: window key events (NSEvent keyCode) */
void input_release_all(void);
char *input_take_shot(void);                 /* FIFA_CONTROL screenshot path for the next present (free it), or NULL */
bool input_take_debug(void);                /* FIFA_CONTROL "debug": make the next frame the debug frame (once) */
int input_profile_mode(void);               /* FIFA_CONTROL "prof <n>": FIFA_GPU_PROFILE level from now on, or -1 */
void input_shot_done(void);
uint64_t renderer_present_count(void);         /* presented frames so far (nvn/metal.m) */

/* Host input (rt/input.m): player 1's controller state for the npad HLE. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef struct { uint64_t buttons; int32_t lx, ly, rx, ry; } InputState;  /* nn::hid::NpadButton bits, sticks +-32767 */

void input_state(InputState *s);              /* any thread */
void input_key(unsigned short key, bool down);  /* main thread: window key events (NSEvent keyCode) */
void input_release_all(void);
uint64_t renderer_present_count(void);         /* presented frames so far (nvn/metal.m) */

/* nn::hid npad queries at the API level: one Pro Controller (full key style)
 * attached as player 1, nothing else. The state comes from rt/input.m
 * (keyboard, gamepads, FIFA_PRESS), so neither the hid shared memory layout
 * nor the controller support applet is needed. */
#include <time.h>

#include "../rt/input.h"
#include "hle.h"

#define NPAD_ID_NO1 0
#define STYLE_FULL_KEY 1u
#define ATTR_IS_CONNECTED 1u

typedef struct {
  int64_t sampling_number;
  uint64_t buttons;
  int32_t lx, ly, rx, ry;
  uint32_t attributes;
  uint32_t pad;
} NpadState;  /* NpadFullKeyState, NpadHandheldState, NpadJoy*State: 0x28 bytes */

static uint32_t supported_styles = STYLE_FULL_KEY;

static int64_t sampling_number(void) { return (int64_t)(clock_gettime_nsec_np(CLOCK_UPTIME_RAW) / 5000000); }

/* int GetNpadStates(State *out, int count, const NpadIdType &id): newest first */
static void npad_states(CPU *c, int connected) {
  int n = (int)ARG(1);
  uint32_t id = *(uint32_t *)gptr(ARG(2));
  NpadState *out = gptr(ARG(0));
  static int logged;  /* the first few queries, to see which styles and ids the game polls */
  if (logged < 16 && __atomic_fetch_add(&logged, 1, __ATOMIC_RELAXED) < 16)
    HLOG("[hid] GetNpadStates(%s, count %d, id %#x)\n", connected ? "full key" : "other", n, id);
  if (n < 1) { RET(0); return; }
  memset(out, 0, sizeof *out);
  out->sampling_number = sampling_number();
  if (connected && id == NPAD_ID_NO1) {
    InputState in;
    input_state(&in);
    out->buttons = in.buttons;
    out->lx = in.lx; out->ly = in.ly;
    out->rx = in.rx; out->ry = in.ry;
    out->attributes = ATTR_IS_CONNECTED;
  }
  RET(1);
}

HLE(_ZN2nn3hid13GetNpadStatesEPNS0_16NpadFullKeyStateEiRKj) { npad_states(c, 1); }
HLE(_ZN2nn3hid13GetNpadStatesEPNS0_17NpadHandheldStateEiRKj) { npad_states(c, 0); }
HLE(_ZN2nn3hid13GetNpadStatesEPNS0_16NpadJoyDualStateEiRKj) { npad_states(c, 0); }
HLE(_ZN2nn3hid13GetNpadStatesEPNS0_16NpadJoyLeftStateEiRKj) { npad_states(c, 0); }
HLE(_ZN2nn3hid13GetNpadStatesEPNS0_17NpadJoyRightStateEiRKj) { npad_states(c, 0); }

/* void GetNpadState(NpadJoyDualState *out, const NpadIdType &id) */
HLE(_ZN2nn3hid12GetNpadStateEPNS0_16NpadJoyDualStateERKj) {
  NpadState *out = gptr(ARG(0));
  memset(out, 0, sizeof *out);
  out->sampling_number = sampling_number();
}

/* NpadStyleSet GetNpadStyleSet(const NpadIdType &id) */
HLE(_ZN2nn3hid15GetNpadStyleSetERKj) {
  RET(*(uint32_t *)gptr(ARG(0)) == NPAD_ID_NO1 ? STYLE_FULL_KEY & supported_styles : 0);
}
HLE(_ZN2nn3hid24SetSupportedNpadStyleSetENS_4util10BitFlagSetILi32ENS0_12NpadStyleTagEEE) {
  supported_styles = (uint32_t)ARG(0);
  if (!(supported_styles & STYLE_FULL_KEY)) HLOG("[hid] full key style not supported by the game (%#x)\n", supported_styles);
}
HLE(_ZN2nn3hid24GetSupportedNpadStyleSetEv) { RET(supported_styles); }

/* Result ShowControllerSupport(const ControllerSupportArg &): the controller is already there */
HLE(_ZN2nn3hid21ShowControllerSupportERKNS0_20ControllerSupportArgE) { RET(0); }

/* void GetTouchScreenState<16>(TouchScreenState<16> *out): the mouse (rt/input.m) as one finger.
 * TouchScreenState<N> { s64 sampling_number; s32 count, reserved; TouchState touches[N] }, TouchState (0x28):
 * { TimeSpan delta_time; u32 attributes (bit 0 start, bit 1 end); s32 finger_id, x, y, diameter_x, diameter_y,
 * rotation_angle, reserved }, x, y in 1280x720 screen units. */
typedef struct {
  int64_t delta_time;
  uint32_t attributes;
  int32_t finger_id, x, y, diameter_x, diameter_y, rotation_angle, reserved;
} TouchState;

HLE(_ZN2nn3hid19GetTouchScreenStateILm16EEEvPNS0_16TouchScreenStateIXT_EEE) {
  static int was_down;
  struct { int64_t sampling_number; int32_t count, reserved; TouchState touches[16]; } *out = gptr(ARG(0));
  memset(out, 0, sizeof *out);
  out->sampling_number = sampling_number();
  int32_t x, y;
  int down = input_touch(&x, &y);
  if (down) {
    out->count = 1;
    out->touches[0] = (TouchState){.attributes = was_down ? 0 : 1, .x = x, .y = y, .diameter_x = 15, .diameter_y = 15};
  }
  was_down = down;
}

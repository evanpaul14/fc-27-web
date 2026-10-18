/* Host replacements for guest functions.
 *
 * A line starting with HLE(symbol) in recomp/hle/*.c defines the host
 * implementation of that exported symbol; recomp/build.py routes every call
 * to the symbol's address there instead of translating it. Arguments and
 * results follow the AAPCS64: x0-x7 / v0-v7 in, x0 / v0 out. */
#pragma once
#include "../rt/host.h"

#define HLE(sym) void hle_##sym(CPU *c)
#define ARG(i) (c->x[i])
#define RET(v) (c->x[0] = (uint64_t)(v))

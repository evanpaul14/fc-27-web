/* Interface between generated code (build/gen) and the host runtime. */
#pragma once
#include "cpu.h"

typedef void (*A64Func)(CPU *c);
typedef struct { uint64_t addr; A64Func fn; } A64Fn;
typedef struct { uint64_t addr; const char *name; } A64Sym;

extern const A64Fn a64_fn_table[];
extern const unsigned a64_fn_count;
extern const A64Sym a64_missing[];
extern const unsigned a64_missing_count;

/* Indirect call/jump to a guest address (dispatch through a64_fn_table). */
void a64_call(CPU *c, uint64_t target);
/* `br` whose target is not in the function's resolved jump table. */
void a64_bad_jump(CPU *c, uint64_t pc, uint64_t target);

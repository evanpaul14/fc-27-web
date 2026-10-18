#pragma once
#include "../rt/cpu.h"

/* Register file as the asm stubs see it (offsets are baked into gen.py). */
typedef struct {
  uint64_t x[31];
  uint64_t sp;    /* 248 */
  uint64_t nzcv;  /* 256 */
  uint64_t pad;
  V128 v[32];     /* 272 */
} __attribute__((aligned(16))) NState;

typedef struct {
  void (*nat)(void);
  void (*tr)(CPU *);
  uint32_t word;
  const char *tag;
  int mem, rn;
} Test;

extern const Test tests[];
extern const int ntests;

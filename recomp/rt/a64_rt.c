/* Out-of-line helpers for recompiled A64 code. */
#include "cpu.h"
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

/* ---- FRECPE / FRSQRTE (ARMv8.0 pseudocode, round-to-nearest, FZ off) ---- */
#define MASK52 ((1ull << 52) - 1)

static uint32_t recip_est(uint32_t a) { a = a * 2 + 1; uint32_t b = (1u << 19) / a; return (b + 1) / 2; }
static uint32_t rsqrt_est(uint32_t a) {
  if (a < 256) a = a * 2 + 1;
  else { a = (a >> 1) << 1; a = (a + 1) * 2; }
  uint32_t b = 512;
  while ((uint64_t)a * (b + 1) * (b + 1) < (1ull << 28)) b++;
  return (b + 1) / 2;
}

/* Shared core: N = 32 or 64; returns sign-less result fields. */
static uint64_t recpe_core(uint64_t fraction, int e, int bias2) {
  if (e == 0) {
    if (!((fraction >> 51) & 1)) { e = -1; fraction = (fraction << 2) & MASK52; }
    else fraction = (fraction << 1) & MASK52;
  }
  uint32_t scaled = 0x100 | ((fraction >> 44) & 0xff);
  int re = bias2 - e;
  fraction = (uint64_t)(recip_est(scaled) & 0xff) << 44;
  if (re == 0) fraction = (1ull << 51) | (fraction >> 1);
  else if (re == -1) { fraction = (1ull << 50) | (fraction >> 2); re = 0; }
  return ((uint64_t)re << 52) | fraction;  /* exponent above the 52-bit fraction */
}

uint32_t a64_frecpe32(uint32_t x) {
  uint32_t sign = x & 0x80000000u, exp = (x >> 23) & 0xff, frac = x & 0x7fffff;
  if (exp == 0xff) return frac ? x | 0x400000u : sign;
  if (exp == 0 && frac == 0) return sign | 0x7f800000u;
  if (fabsf(u2f(x)) < 0x1p-128f) return sign | 0x7f800000u;
  uint64_t r = recpe_core((uint64_t)frac << 29, exp, 253);
  return sign | (uint32_t)(((r >> 52) & 0xff) << 23) | (uint32_t)((r & MASK52) >> 29);
}
uint64_t a64_frecpe64(uint64_t x) {
  uint64_t sign = x & (1ull << 63), exp = (x >> 52) & 0x7ff, frac = x & MASK52;
  if (exp == 0x7ff) return frac ? x | (1ull << 51) : sign;
  if (exp == 0 && frac == 0) return sign | 0x7ff0000000000000ull;
  if (fabs(u2d(x)) < 0x1p-1024) return sign | 0x7ff0000000000000ull;
  uint64_t r = recpe_core(frac, (int)exp, 2045);
  return sign | (((r >> 52) & 0x7ff) << 52) | (r & MASK52);
}

static uint64_t rsqrte_core(uint64_t fraction, int e, int bias3, uint64_t *re_out) {
  if (e == 0) {
    while (!((fraction >> 51) & 1)) { fraction = (fraction << 1) & MASK52; e--; }
    fraction = (fraction << 1) & MASK52;
  }
  uint32_t scaled = (e & 1) == 0 ? (0x100 | ((fraction >> 44) & 0xff)) : (0x80 | ((fraction >> 45) & 0x7f));
  *re_out = (uint64_t)((bias3 - e) / 2);
  return rsqrt_est(scaled) & 0xff;
}
uint32_t a64_frsqrte32(uint32_t x) {
  uint32_t sign = x & 0x80000000u, exp = (x >> 23) & 0xff, frac = x & 0x7fffff;
  if (exp == 0xff && frac) return x | 0x400000u;
  if (exp == 0 && frac == 0) return sign | 0x7f800000u;
  if (sign) return 0x7fc00000u;
  if (exp == 0xff) return 0;
  uint64_t re, est = rsqrte_core((uint64_t)frac << 29, exp, 380, &re);
  return (uint32_t)((re & 0xff) << 23) | (uint32_t)(est << 15);
}
uint64_t a64_frsqrte64(uint64_t x) {
  uint64_t sign = x & (1ull << 63), exp = (x >> 52) & 0x7ff, frac = x & MASK52;
  if (exp == 0x7ff && frac) return x | (1ull << 51);
  if (exp == 0 && frac == 0) return sign | 0x7ff0000000000000ull;
  if (sign) return 0x7ff8000000000000ull;
  if (exp == 0x7ff) return 0;
  uint64_t re, est = rsqrte_core(frac, (int)exp, 3068, &re);
  return ((re & 0x7ff) << 52) | (est << 44);
}

/* ---- AES ---- */
static uint8_t sbox[256], isbox[256];
static uint8_t gmul(uint8_t a, uint8_t b) {
  uint8_t p = 0;
  while (b) { if (b & 1) p ^= a; a = (uint8_t)((a << 1) ^ ((a & 0x80) ? 0x1b : 0)); b >>= 1; }
  return p;
}
static void aes_init(void) {
  if (sbox[0]) return;
  for (int i = 0; i < 256; i++) {
    uint8_t inv = 0;
    for (int j = 1; j < 256 && i; j++) if (gmul((uint8_t)i, (uint8_t)j) == 1) { inv = (uint8_t)j; break; }
    uint8_t s = inv;
    for (int k = 1; k < 5; k++) s ^= (uint8_t)((inv << k) | (inv >> (8 - k)));
    s ^= 0x63;
    sbox[i] = s; isbox[s] = (uint8_t)i;
  }
}
static const uint8_t SR[16] = {0, 5, 10, 15, 4, 9, 14, 3, 8, 13, 2, 7, 12, 1, 6, 11};
static const uint8_t ISR[16] = {0, 13, 10, 7, 4, 1, 14, 11, 8, 5, 2, 15, 12, 9, 6, 3};
V128 a64_aese(V128 d, V128 n) {
  aes_init(); V128 r;
  for (int i = 0; i < 16; i++) r.b[i] = sbox[d.b[SR[i]] ^ n.b[SR[i]]];
  return r;
}
V128 a64_aesd(V128 d, V128 n) {
  aes_init(); V128 r;
  for (int i = 0; i < 16; i++) r.b[i] = isbox[d.b[ISR[i]] ^ n.b[ISR[i]]];
  return r;
}
static V128 mix(V128 n, const uint8_t m[4]) {
  V128 r;
  for (int col = 0; col < 4; col++)
    for (int row = 0; row < 4; row++) {
      uint8_t v = 0;
      for (int k = 0; k < 4; k++) v ^= gmul(m[(k - row + 4) & 3], n.b[col * 4 + k]);
      r.b[col * 4 + row] = v;
    }
  return r;
}
V128 a64_aesmc(V128 n) { static const uint8_t m[4] = {2, 3, 1, 1}; return mix(n, m); }
V128 a64_aesimc(V128 n) { static const uint8_t m[4] = {14, 11, 13, 9}; return mix(n, m); }

/* ---- 128-bit exclusives ---- */
void a64_ldxp128(CPU *c, uint64_t a, uint64_t *lo, uint64_t *hi) {
  __uint128_t v = __atomic_load_n((__uint128_t *)G(a), __ATOMIC_ACQUIRE);
  c->excl_addr = a; c->excl_val = (uint64_t)v; c->excl_val2 = (uint64_t)(v >> 64);
  *lo = (uint64_t)v; *hi = (uint64_t)(v >> 64);
}
uint32_t a64_stxp128(CPU *c, uint64_t a, uint64_t lo, uint64_t hi) {
  uint32_t r = 1;
  if (c->excl_addr == a) {
    __uint128_t e = ((__uint128_t)c->excl_val2 << 64) | c->excl_val, v = ((__uint128_t)hi << 64) | lo;
    r = !__atomic_compare_exchange_n((__uint128_t *)G(a), &e, v, 0, __ATOMIC_SEQ_CST, __ATOMIC_RELAXED);
  }
  c->excl_addr = ~0ull;
  return r;
}

/* ---- host services ---- */
uint64_t a64_host_cntpct(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 19200000ull + (uint64_t)ts.tv_nsec * 192 / 10000;
}
void a64_yield(void) { sched_yield(); }

__attribute__((weak)) void a64_trap(CPU *c, uint64_t pc, uint32_t word) {
  (void)c;
  fprintf(stderr, "a64_trap: pc=%#llx word=%#x\n", (unsigned long long)pc, word);
  abort();
}
__attribute__((weak)) void a64_svc(CPU *c, uint32_t imm, uint64_t pc) {
  (void)c;
  fprintf(stderr, "a64_svc: #%#x at pc=%#llx\n", imm, (unsigned long long)pc);
  abort();
}

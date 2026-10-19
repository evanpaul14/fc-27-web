/* Guest CPU state and instruction helpers used by recompiled A64 code.
 * Guest addresses are offsets from `mem`; set mem = 0 to run guest pointers
 * as host pointers (difftest, native bring-up). */
#pragma once
#include <math.h>
#include <stdint.h>
#include <string.h>

typedef union V128 {
  uint8_t b[16]; int8_t sb[16];
  uint16_t h[8]; int16_t sh[8];
  uint32_t s[4]; int32_t ss[4];
  uint64_t d[2]; int64_t sd[2];
  float f[4]; double fd[2];
} V128;

/* NZCV. Generated code keeps these in a local `Flags F` so the compiler can
 * drop dead flag computations; CPU.fl only carries them across chunks. */
typedef struct Flags { uint64_t nf, zf, cf, vf; } Flags;

typedef struct CPU {
  uint64_t x[32];          /* x[31] is SP; XZR reads are emitted as 0 */
  Flags fl;
  V128 v[32];
  uint64_t tpidr, tpidrro;
  uint64_t fpcr, fpsr;
  uint64_t excl_addr, excl_val, excl_val2;
} CPU;

extern uint8_t *mem;

#define A64_INLINE static inline __attribute__((always_inline))
#define G(a) ((void *)(mem + (uint64_t)(a)))

/* ---- memory ---- */
A64_INLINE uint8_t  LD8(uint64_t a)  { return *(uint8_t *)G(a); }
A64_INLINE uint16_t LD16(uint64_t a) { uint16_t t; memcpy(&t, G(a), 2); return t; }
A64_INLINE uint32_t LD32(uint64_t a) { uint32_t t; memcpy(&t, G(a), 4); return t; }
A64_INLINE uint64_t LD64(uint64_t a) { uint64_t t; memcpy(&t, G(a), 8); return t; }
A64_INLINE void ST8(uint64_t a, uint8_t v)   { *(uint8_t *)G(a) = v; }
A64_INLINE void ST16(uint64_t a, uint16_t v) { memcpy(G(a), &v, 2); }
A64_INLINE void ST32(uint64_t a, uint32_t v) { memcpy(G(a), &v, 4); }
A64_INLINE void ST64(uint64_t a, uint64_t v) { memcpy(G(a), &v, 8); }
A64_INLINE V128 LDQ(uint64_t a) { V128 t; memcpy(&t, G(a), 16); return t; }
A64_INLINE void STQ(uint64_t a, V128 v) { memcpy(G(a), &v, 16); }

#define LDA(bits, a) __atomic_load_n((uint##bits##_t *)G(a), __ATOMIC_ACQUIRE)
#define STL(bits, a, v) __atomic_store_n((uint##bits##_t *)G(a), (uint##bits##_t)(v), __ATOMIC_RELEASE)

/* Exclusive monitor modelled as compare-and-swap against the value loaded by
 * the matching ldxr. ABA is indistinguishable here, so the CAS is at least an
 * acquire: in `ldaxr; stxr` (an acquire exchange, e.g. a spinlock) another
 * thread can take and release the lock between the two, and the store then
 * succeeds where real LL/SC fails. Only the CAS saw that release, so it has to
 * synchronize with it, or the critical section reads data from before it. */
#define LDX(bits, cpu, a, acq) ({ uint64_t _a = (a); (cpu)->excl_addr = _a; \
  uint64_t _v = __atomic_load_n((uint##bits##_t *)G(_a), (acq) ? __ATOMIC_ACQUIRE : __ATOMIC_RELAXED); \
  (cpu)->excl_val = _v; _v; })
#define STX(bits, cpu, a, val, rel) ({ uint64_t _a = (a); uint32_t _r = 1; \
  if ((cpu)->excl_addr == _a) { uint##bits##_t _e = (uint##bits##_t)(cpu)->excl_val; \
    _r = !__atomic_compare_exchange_n((uint##bits##_t *)G(_a), &_e, (uint##bits##_t)(val), 0, \
        (rel) ? __ATOMIC_SEQ_CST : __ATOMIC_ACQUIRE, __ATOMIC_RELAXED); } \
  (cpu)->excl_addr = ~0ull; _r; })
uint32_t a64_stxp128(CPU *c, uint64_t a, uint64_t lo, uint64_t hi);
void a64_ldxp128(CPU *c, uint64_t a, uint64_t *lo, uint64_t *hi);

/* ---- integer ---- */
A64_INLINE uint64_t adds64(Flags *c, uint64_t a, uint64_t b, uint64_t ci) {
  __uint128_t t = (__uint128_t)a + b + ci; uint64_t r = (uint64_t)t;
  c->nf = r >> 63; c->zf = r == 0; c->cf = (uint64_t)(t >> 64); c->vf = ((a ^ r) & (b ^ r)) >> 63;
  return r;
}
A64_INLINE uint32_t adds32(Flags *c, uint32_t a, uint32_t b, uint32_t ci) {
  uint64_t t = (uint64_t)a + b + ci; uint32_t r = (uint32_t)t;
  c->nf = r >> 31; c->zf = r == 0; c->cf = t >> 32; c->vf = ((a ^ r) & (b ^ r)) >> 31;
  return r;
}
#define SETNZ64(c, r) ({ uint64_t _r = (r); (c)->nf = _r >> 63; (c)->zf = _r == 0; (c)->cf = 0; (c)->vf = 0; _r; })
#define SETNZ32(c, r) ({ uint32_t _r = (r); (c)->nf = _r >> 31; (c)->zf = _r == 0; (c)->cf = 0; (c)->vf = 0; _r; })
#define SETNZCV(c, f) ((c)->nf = ((f) >> 3) & 1, (c)->zf = ((f) >> 2) & 1, (c)->cf = ((f) >> 1) & 1, (c)->vf = (f) & 1)

A64_INLINE uint64_t ror64(uint64_t x, unsigned s) { s &= 63; return s ? (x >> s) | (x << (64 - s)) : x; }
A64_INLINE uint32_t ror32(uint32_t x, unsigned s) { s &= 31; return s ? (x >> s) | (x << (32 - s)) : x; }
A64_INLINE uint64_t clz64(uint64_t x) { return x ? __builtin_clzll(x) : 64; }
A64_INLINE uint32_t clz32(uint32_t x) { return x ? __builtin_clz(x) : 32; }
A64_INLINE uint64_t cls64(uint64_t x) { return clz64(x ^ (uint64_t)((int64_t)x >> 1)) - 1; }
A64_INLINE uint32_t cls32(uint32_t x) { return clz32(x ^ (uint32_t)((int32_t)x >> 1)) - 1; }
A64_INLINE uint64_t udiv64(uint64_t a, uint64_t b) { return b ? a / b : 0; }
A64_INLINE uint32_t udiv32(uint32_t a, uint32_t b) { return b ? a / b : 0; }
A64_INLINE int64_t sdiv64(int64_t a, int64_t b) { return b == 0 ? 0 : (b == -1 ? (int64_t)(0 - (uint64_t)a) : a / b); }
A64_INLINE int32_t sdiv32(int32_t a, int32_t b) { return b == 0 ? 0 : (b == -1 ? (int32_t)(0 - (uint32_t)a) : a / b); }
A64_INLINE uint64_t smulh(int64_t a, int64_t b) { return (uint64_t)(((__int128)a * b) >> 64); }
A64_INLINE uint64_t umulh(uint64_t a, uint64_t b) { return (uint64_t)(((__uint128_t)a * b) >> 64); }
A64_INLINE uint32_t crc32_a64(uint32_t acc, uint64_t val, int bytes, uint32_t poly) {
  for (int i = 0; i < bytes * 8; i++) {
    acc ^= (val >> i) & 1;
    acc = (acc >> 1) ^ ((acc & 1) ? poly : 0);
  }
  return acc;
}

/* ---- floating point ---- */
#define ISSNAN32(x) ({ float _x = (x); uint32_t _u; memcpy(&_u, &_x, 4); isnan(_x) && !(_u & 0x00400000u); })
#define ISSNAN64(x) ({ double _x = (x); uint64_t _u; memcpy(&_u, &_x, 8); isnan(_x) && !(_u & 0x0008000000000000ull); })
/* NaN inputs: a + b gives the architectural NaN propagation on an AArch64 host. */
A64_INLINE float fmax32(float a, float b) { if (isnan(a) || isnan(b)) return a + b; if (a == b) return signbit(a) ? b : a; return a > b ? a : b; }
A64_INLINE float fmin32(float a, float b) { if (isnan(a) || isnan(b)) return a + b; if (a == b) return signbit(a) ? a : b; return a < b ? a : b; }
A64_INLINE double fmax64(double a, double b) { if (isnan(a) || isnan(b)) return a + b; if (a == b) return signbit(a) ? b : a; return a > b ? a : b; }
A64_INLINE double fmin64(double a, double b) { if (isnan(a) || isnan(b)) return a + b; if (a == b) return signbit(a) ? a : b; return a < b ? a : b; }
A64_INLINE float fmaxnm32(float a, float b) { if (isnan(a) && !isnan(b) && !ISSNAN32(a)) return b; if (isnan(b) && !isnan(a) && !ISSNAN32(b)) return a; return fmax32(a, b); }
A64_INLINE float fminnm32(float a, float b) { if (isnan(a) && !isnan(b) && !ISSNAN32(a)) return b; if (isnan(b) && !isnan(a) && !ISSNAN32(b)) return a; return fmin32(a, b); }
A64_INLINE double fmaxnm64(double a, double b) { if (isnan(a) && !isnan(b) && !ISSNAN64(a)) return b; if (isnan(b) && !isnan(a) && !ISSNAN64(b)) return a; return fmax64(a, b); }
A64_INLINE double fminnm64(double a, double b) { if (isnan(a) && !isnan(b) && !ISSNAN64(a)) return b; if (isnan(b) && !isnan(a) && !ISSNAN64(b)) return a; return fmin64(a, b); }

A64_INLINE void fcmp_flags(Flags *c, double a, double b) {
  if (isnan(a) || isnan(b)) SETNZCV(c, 3);
  else if (a == b) SETNZCV(c, 6);
  else if (a < b) SETNZCV(c, 8);
  else SETNZCV(c, 2);
}

/* rounding modes: 0 nearest-even, 1 +inf, 2 -inf, 3 zero, 4 nearest-away */
A64_INLINE double round_mode(double x, int rm) {
  switch (rm) {
  case 0: return __builtin_roundeven(x);
  case 1: return ceil(x);
  case 2: return floor(x);
  case 3: return trunc(x);
  default: return round(x);
  }
}
A64_INLINE float round_mode32(float x, int rm) {
  switch (rm) {
  case 0: return __builtin_roundevenf(x);
  case 1: return ceilf(x);
  case 2: return floorf(x);
  case 3: return truncf(x);
  default: return roundf(x);
  }
}
A64_INLINE int64_t f2s(double x, int rm, int bits) {
  if (isnan(x)) return 0;
  double r = round_mode(x, rm), lim = ldexp(1.0, bits - 1);
  if (r >= lim) return (int64_t)((1ull << (bits - 1)) - 1);
  if (r < -lim) return bits == 64 ? INT64_MIN : -(int64_t)(1ull << (bits - 1));
  return (int64_t)r;
}
A64_INLINE uint64_t f2u(double x, int rm, int bits) {
  if (isnan(x)) return 0;
  double r = round_mode(x, rm), lim = ldexp(1.0, bits);
  if (r <= 0) return 0;
  if (r >= lim) return bits == 64 ? ~0ull : (1ull << bits) - 1;
  return (uint64_t)r;
}

A64_INLINE float u2f(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
A64_INLINE uint32_t f2u32bits(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
A64_INLINE double u2d(uint64_t u) { double f; memcpy(&f, &u, 8); return f; }
A64_INLINE uint64_t d2u64bits(double f) { uint64_t u; memcpy(&u, &f, 8); return u; }

A64_INLINE float h2f(uint16_t h) {
  uint32_t s = (uint32_t)(h & 0x8000) << 16, e = (h >> 10) & 31, m = h & 0x3ff;
  if (e == 31) return u2f(s | 0x7f800000 | (m << 13) | (m ? 0x400000 : 0));
  if (e == 0) { float r = ldexpf((float)m, -24); return s ? -r : r; }
  return u2f(s | ((e + 112) << 23) | (m << 13));
}
A64_INLINE uint16_t f2h(float f) {
  uint32_t u = f2u32bits(f), s = (u >> 16) & 0x8000;
  if (isnan(f)) return s | 0x7e00 | ((u >> 13) & 0x1ff);
  float a = fabsf(f);
  if (a >= 65520.0f) return s | 0x7c00;
  if (a < 6.103515625e-05f) return s | (uint16_t)__builtin_roundevenf(a * 16777216.0f);
  uint32_t au = f2u32bits(a), e = (au >> 23) - 112, m = au & 0x7fffff;
  uint32_t r = (e << 10) | (m >> 13), rem = m & 0x1fff;
  if (rem > 0x1000 || (rem == 0x1000 && (r & 1))) r++;
  return s | r;
}

uint32_t a64_frecpe32(uint32_t x);
uint64_t a64_frecpe64(uint64_t x);
uint32_t a64_frsqrte32(uint32_t x);
uint64_t a64_frsqrte64(uint64_t x);
A64_INLINE float frecps32(float a, float b) { if ((isinf(a) && b == 0) || (a == 0 && isinf(b))) return 2.0f; return fmaf(-a, b, 2.0f); }
A64_INLINE double frecps64(double a, double b) { if ((isinf(a) && b == 0) || (a == 0 && isinf(b))) return 2.0; return fma(-a, b, 2.0); }
/* (3 - a*b) / 2 with one rounding: halve an operand first (exact unless subnormal). */
A64_INLINE float frsqrts32(float a, float b) {
  if ((isinf(a) && b == 0) || (a == 0 && isinf(b))) return 1.5f;
  return fabsf(a) >= 0x1p-125f ? fmaf(-(a * 0.5f), b, 1.5f) : fmaf(-a, b * 0.5f, 1.5f);
}
A64_INLINE double frsqrts64(double a, double b) {
  if ((isinf(a) && b == 0) || (a == 0 && isinf(b))) return 1.5;
  return fabs(a) >= 0x1p-1021 ? fma(-(a * 0.5), b, 1.5) : fma(-a, b * 0.5, 1.5);
}

/* ---- saturation (values computed in 64/128-bit, clamp to esize) ---- */
A64_INLINE int64_t sat_s(__int128 v, int bits, int *q) {
  __int128 hi = ((__int128)1 << (bits - 1)) - 1, lo = -((__int128)1 << (bits - 1));
  if (v > hi) { *q = 1; return (int64_t)hi; }
  if (v < lo) { *q = 1; return (int64_t)lo; }
  return (int64_t)v;
}
A64_INLINE uint64_t sat_u(__int128 v, int bits, int *q) {
  __int128 hi = bits == 64 ? (__int128)~0ull : (((__int128)1 << bits) - 1);
  if (v > hi) { *q = 1; return (uint64_t)hi; }
  if (v < 0) { *q = 1; return 0; }
  return (uint64_t)v;
}
/* SSHL/USHL family: shift by signed low byte of s, optional rounding. */
A64_INLINE __int128 shl_s(int64_t a, int8_t s, int round) {
  if (s >= 0) return s >= 64 ? 0 : (__int128)a << s;
  int r = -s; if (r >= 64) return round ? 0 : (a < 0 ? -1 : 0);
  __int128 v = a; if (round) v += (__int128)1 << (r - 1);
  return v >> r;
}
A64_INLINE __int128 shl_u(uint64_t a, int8_t s, int round) {
  if (s >= 0) return s >= 64 ? 0 : (__int128)((__uint128_t)a << s);
  int r = -s; if (r > 64) return 0;
  __uint128_t v = a; if (round) v += (__uint128_t)1 << (r - 1);
  return (__int128)(v >> r);
}

A64_INLINE uint64_t a64_cntpct(void);
uint64_t a64_host_cntpct(void);
A64_INLINE uint64_t a64_cntpct(void) { return a64_host_cntpct(); }

/* AES */
V128 a64_aese(V128 d, V128 n);
V128 a64_aesd(V128 d, V128 n);
V128 a64_aesmc(V128 n);
V128 a64_aesimc(V128 n);

void a64_trap(CPU *c, uint64_t pc, uint32_t word);
void a64_svc(CPU *c, uint32_t imm, uint64_t pc);
void a64_yield(void);

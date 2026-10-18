/* Differential test driver: run each instruction on the host CPU and through
 * its translation from the same random state, and compare everything. */
#include "difftest.h"
#include <stdio.h>
#include <stdlib.h>

uint8_t *mem = 0;
NState g_state;
uint64_t g_saved_sp;

#define BUF_SIZE (192 << 10)
#define BASE_OFF (64 << 10)
static uint8_t buf[BUF_SIZE] __attribute__((aligned(64)));
static uint8_t init_buf[BUF_SIZE], tr_buf[BUF_SIZE];

static uint64_t rs = 0x9E3779B97F4A7C15ull;
static uint64_t rnd(void) { rs ^= rs << 13; rs ^= rs >> 7; rs ^= rs << 17; return rs; }

static const uint64_t special64[] = {0, 1, ~0ull, 0x7fffffffffffffffull, 0x8000000000000000ull,
                                     0xffffffff, 0x80000000, 0x7fffffff, 0x100000000ull, 2, 3, 0xff, 0x8000};
static uint64_t rand_gpr(void) {
  switch (rnd() % 4) {
  case 0: return special64[rnd() % (sizeof special64 / 8)];
  case 1: return rnd() & 0xffff;
  case 2: return (uint64_t)(int64_t)(int16_t)rnd();
  default: return rnd();
  }
}
static uint32_t rand_f32(void) {
  static const float sp[] = {0.0f, -0.0f, 1.0f, -1.0f, 0.5f, 2.0f, 1e-40f, -1e-40f, 3.4e38f, 1e-30f};
  float f;
  switch (rnd() % 6) {
  case 0: f = sp[rnd() % 10]; break;
  case 1: return (rnd() & 1 ? 0x7f800000u : 0xff800000u) | (rnd() % 4 == 0 ? (uint32_t)rnd() & 0x7fffff : 0);
  case 2: f = (float)((int64_t)(rnd() % 2001) - 1000) / (float)(1 + rnd() % 64); break;
  case 3: f = (float)((int64_t)(rnd() % 200001) - 100000) * 0.37f; break;
  default: return (uint32_t)rnd();
  }
  return f2u32bits(f);
}
static uint64_t rand_f64(void) {
  double f;
  switch (rnd() % 5) {
  case 0: return d2u64bits((double)u2f(rand_f32()));
  case 1: f = (double)((int64_t)(rnd() % 2001) - 1000) / (double)(1 + rnd() % 64); break;
  case 2: return (rnd() & 1 ? 0x7ff0000000000000ull : 0xfff0000000000000ull) | (rnd() % 4 == 0 ? rnd() & ((1ull << 52) - 1) : 0);
  default: return rnd();
  }
  return d2u64bits(f);
}

static void rand_state(const Test *t, NState *s) {
  for (int i = 0; i < 31; i++) s->x[i] = t->mem ? (rnd() & 0xff) : rand_gpr();
  s->sp = (uint64_t)(buf + BASE_OFF + 32768);
  if (t->mem) {
    if (t->rn == 31) s->sp = (uint64_t)(buf + BASE_OFF);
    else s->x[t->rn] = (uint64_t)(buf + BASE_OFF);
  }
  s->nzcv = (rnd() & 15) << 28;
  int mode = rnd() % 3;
  for (int i = 0; i < 32; i++) {
    if (mode == 0) { s->v[i].d[0] = rnd(); s->v[i].d[1] = rnd(); }
    else if (mode == 1) for (int k = 0; k < 4; k++) s->v[i].s[k] = rand_f32();
    else for (int k = 0; k < 2; k++) s->v[i].d[k] = rand_f64();
  }
}

static void to_cpu(const NState *s, CPU *c) {
  memset(c, 0, sizeof *c);
  memcpy(c->x, s->x, sizeof s->x);
  c->x[31] = s->sp;
  c->nf = (s->nzcv >> 31) & 1; c->zf = (s->nzcv >> 30) & 1; c->cf = (s->nzcv >> 29) & 1; c->vf = (s->nzcv >> 28) & 1;
  memcpy(c->v, s->v, sizeof s->v);
  c->excl_addr = ~0ull;
}

/* NaN payloads may differ (the C compiler can commute FP operands); a lane
 * where both sides are NaN of the same width counts as equal. */
static int nan_only;
static int vec_equal(const V128 *a, const V128 *b) {
  if (!memcmp(a, b, 16)) return 1;
  for (int k = 0; k < 2; k++) {
    if (a->d[k] == b->d[k] || (isnan(a->fd[k]) && isnan(b->fd[k]))) continue;
    for (int j = 2 * k; j < 2 * k + 2; j++)
      if (a->s[j] != b->s[j] && !(isnan(a->f[j]) && isnan(b->f[j]))) return 0;
  }
  nan_only++;
  return 1;
}

static int compare(const Test *t, const NState *in, const NState *nat, const CPU *c, int verbose) {
  int bad = 0;
  for (int i = 0; i < 31; i++) {
    if (i == 18) continue;
    if (nat->x[i] != c->x[i]) {
      bad++;
      if (verbose) printf("    x%d: in %#llx native %#llx tr %#llx\n", i, (unsigned long long)in->x[i],
                          (unsigned long long)nat->x[i], (unsigned long long)c->x[i]);
    }
  }
  if (nat->sp != c->x[31]) { bad++; if (verbose) printf("    sp: native %#llx tr %#llx\n", (unsigned long long)nat->sp, (unsigned long long)c->x[31]); }
  uint64_t f = (c->nf << 31) | (c->zf << 30) | (c->cf << 29) | (c->vf << 28);
  if ((nat->nzcv & 0xf0000000u) != f) {
    bad++;
    if (verbose) printf("    nzcv: in %llx native %llx tr %llx\n", (unsigned long long)in->nzcv >> 28,
                        (unsigned long long)(nat->nzcv >> 28) & 15, (unsigned long long)f >> 28);
  }
  for (int i = 0; i < 32; i++) {
    if (!vec_equal(&nat->v[i], &c->v[i])) {
      bad++;
      if (verbose) printf("    v%d: in %016llx%016llx native %016llx%016llx tr %016llx%016llx\n", i,
                          (unsigned long long)in->v[i].d[1], (unsigned long long)in->v[i].d[0],
                          (unsigned long long)nat->v[i].d[1], (unsigned long long)nat->v[i].d[0],
                          (unsigned long long)c->v[i].d[1], (unsigned long long)c->v[i].d[0]);
    }
  }
  /* The native stub pushes x0 just below the final SP; ignore those 16 bytes. */
  uint8_t *push = (uint8_t *)nat->sp - 16;
  for (size_t k = 0; k < BUF_SIZE; k++) {
    uint8_t *p = buf + k;
    if (p >= push && p < push + 16) continue;
    if (buf[k] != tr_buf[k]) {
      bad++;
      if (verbose) printf("    mem[base%+lld]: native %02x tr %02x\n", (long long)k - BASE_OFF, buf[k], tr_buf[k]);
      break;
    }
  }
  return bad;
}

int main(int argc, char **argv) {
  int rounds = argc > 1 ? atoi(argv[1]) : 16;
  int failed = 0, shown = 0;
  for (size_t k = 0; k < BUF_SIZE; k++) init_buf[k] = (uint8_t)rnd();
  for (int i = 0; i < ntests; i++) {
    const Test *t = &tests[i];
    for (int r = 0; r < rounds; r++) {
      NState in;
      rand_state(t, &in);
      CPU c;
      to_cpu(&in, &c);
      memcpy(buf, init_buf, BUF_SIZE);
      t->tr(&c);
      memcpy(tr_buf, buf, BUF_SIZE);
      memcpy(buf, init_buf, BUF_SIZE);
      g_state = in;
      t->nat();
      if (compare(t, &in, &g_state, &c, 0)) {
        failed++;
        if (shown < 400) {
          shown++;
          printf("FAIL %08x %s\n", t->word, t->tag);
          compare(t, &in, &g_state, &c, 1);
        }
        break;
      }
    }
  }
  printf("%d/%d instructions failed (%d NaN-payload-only differences ignored)\n", failed, ntests, nan_only);
  return failed != 0;
}

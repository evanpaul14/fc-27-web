/* setjmp / longjmp. Guest calls nest as host calls, so a guest longjmp has to
 * unwind the host stack too: recomp/build.py wraps every call to setjmp in a
 * host _setjmp in the caller's own frame (a64_pending_jb), and longjmp
 * restores the guest registers and jumps back there with a host _longjmp.
 *
 * The guest jmp_buf (nnmusl: 22 u64 + mask) holds x19-x30, sp and d8-d15. */
#include <setjmp.h>

#include "hle.h"

__thread jmp_buf *a64_pending_jb;

/* guest jmp_buf -> host context, per thread (a longjmp never crosses threads) */
#define MAX_JB 64
static __thread struct { uint64_t guest; jmp_buf *host; } jbs[MAX_JB];
static __thread unsigned n_jbs;

/* Entries whose setjmp caller has returned: the host context sits in that
 * caller's host frame, so below the current host stack pointer it is dead.
 * Unwrapped entries (no host context) go when their guest jmp_buf is on the
 * stack below the guest sp. */
static int dead(unsigned i, uint64_t guest_sp) {
  char here;
  if (jbs[i].host) return (char *)jbs[i].host < &here;
  return jbs[i].guest < guest_sp && guest_sp - jbs[i].guest < (16 << 20);
}

static void remember(uint64_t guest, jmp_buf *host, uint64_t guest_sp) {
  for (unsigned i = 0; i < n_jbs; i++)
    if (jbs[i].guest == guest) { jbs[i].host = host; return; }
  unsigned n = 0;
  for (unsigned i = 0; i < n_jbs; i++)
    if (!dead(i, guest_sp)) jbs[n++] = jbs[i];
  n_jbs = n;
  if (n_jbs == MAX_JB) HDIE("setjmp: more than %d live jmp_bufs on one thread\n", MAX_JB);
  jbs[n_jbs].guest = guest;
  jbs[n_jbs++].host = host;
}

static void do_setjmp(CPU *c) {
  uint64_t *b = gptr(ARG(0));
  for (int i = 0; i < 12; i++) b[i] = c->x[19 + i];  /* x19-x30 */
  b[12] = c->x[31];
  for (int i = 0; i < 8; i++) b[13 + i] = c->v[8 + i].d[0];
  /* NULL when the call wasn't wrapped (through a function pointer): longjmp then stops */
  remember(ARG(0), a64_pending_jb, c->x[31]);
  a64_pending_jb = 0;
  RET(0);
}

static void do_longjmp(CPU *c) {
  uint64_t guest = ARG(0), val = (uint32_t)ARG(1);
  const uint64_t *b = gptr(guest);
  jmp_buf *host = 0;
  for (unsigned i = 0; i < n_jbs; i++)
    if (jbs[i].guest == guest) host = jbs[i].host;
  if (!host) HDIE("longjmp to %#llx: no host context (setjmp not called from a wrapped site)\n", (unsigned long long)guest);
  for (int i = 0; i < 12; i++) c->x[19 + i] = b[i];
  c->x[31] = b[12];
  for (int i = 0; i < 8; i++) c->v[8 + i] = (V128){.d = {b[13 + i], 0}};
  c->x[0] = val ? val : 1;
  _longjmp(*host, 1);
}

HLE(setjmp) { do_setjmp(c); }
HLE(longjmp) { do_longjmp(c); }

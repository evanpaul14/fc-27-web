/* Logging the retail build compiles out, routed to stderr.
 *
 * sub_ffd0744 (update main) is a printf-style logger that returns 0 without
 * doing anything (a thunk to `mov w0, wzr; ret`); EAText's
 * "[FreeType Error] 0x%04x %s\n" goes through it. FIFA_LOG_GAME=1 prints. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "hle.h"

/* AAPCS64 varargs: integers in x[first..7] then 8-byte stack slots, doubles in v0-v7 then the stack */
typedef struct { CPU *c; int gi, vi; uint64_t sp; } VaList;

static uint64_t va_int(VaList *va) {
  if (va->gi < 8) return va->c->x[va->gi++];
  uint64_t v = *(uint64_t *)G(va->sp);
  va->sp += 8;
  return v;
}

static double va_dbl(VaList *va) {
  if (va->vi < 8) return va->c->v[va->vi++].fd[0];
  double v = *(double *)G(va->sp);
  va->sp += 8;
  return v;
}

static int guest_str_ok(uint64_t a) { return a >= 0x08000000 && a < MAP_END; }

/* format guest fmt with the guest's varargs (first vararg in x[first]) */
static void guest_vformat(char *out, size_t n, uint64_t fmt, CPU *c, int first) {
  VaList va = {c, first, 0, c->x[31]};
  const char *f = G(fmt);
  size_t o = 0;
  while (*f && o + 1 < n) {
    if (*f != '%') { out[o++] = *f++; continue; }
    char spec[32];
    size_t s = 0;
    spec[s++] = *f++;
    while (*f && strchr("-+ #0123456789.*", *f) && s < 24) spec[s++] = *f++;
    int lng = 0;
    while (*f && strchr("hlLqjzt", *f)) { if (*f == 'l' || *f == 'j' || *f == 'z' || *f == 't' || *f == 'q') lng = 1; f++; }
    char cv = *f ? *f++ : 0;
    spec[s++] = cv;
    spec[s] = 0;
    int w;
    switch (cv) {
    case 'd': case 'i': {
      int64_t v = (int64_t)va_int(&va);
      if (!lng) v = (int32_t)v;
      spec[s - 1] = 0;
      strcat(spec, "lld");
      w = snprintf(out + o, n - o, spec, (long long)v);
      break;
    }
    case 'u': case 'x': case 'X': case 'o': case 'c': {
      uint64_t v = va_int(&va);
      if (!lng) v = (uint32_t)v;
      if (cv == 'c') { w = snprintf(out + o, n - o, "%c", (int)v); break; }
      spec[s - 1] = 0;
      char tail[4] = {'l', 'l', cv, 0};
      strcat(spec, tail);
      w = snprintf(out + o, n - o, spec, (unsigned long long)v);
      break;
    }
    case 'p': w = snprintf(out + o, n - o, "%#llx", (unsigned long long)va_int(&va)); break;
    case 'f': case 'g': case 'e': case 'F': case 'G': case 'E': w = snprintf(out + o, n - o, spec, va_dbl(&va)); break;
    case 's': {
      uint64_t a = va_int(&va);
      w = snprintf(out + o, n - o, "%s", guest_str_ok(a) ? (const char *)G(a) : "(bad)");
      break;
    }
    case '%': w = snprintf(out + o, n - o, "%%"); break;
    default: w = snprintf(out + o, n - o, "%s", spec); break;
    }
    if (w < 0) break;
    o += (size_t)w < n - o ? (size_t)w : n - o - 1;
  }
  out[o] = 0;
}

HLE(sub_ffd0744) {
  static int on = -1;
  if (on < 0) on = getenv("FIFA_LOG_GAME") != NULL;
  if (on && guest_str_ok(ARG(0))) {
    char buf[1024];
    guest_vformat(buf, sizeof buf, ARG(0), c, 1);
    size_t l = strlen(buf);
    fprintf(stderr, "[game %#llx] %s%s", (unsigned long long)c->x[30], buf, l && buf[l - 1] == '\n' ? "" : "\n");
  }
  RET(0);
}

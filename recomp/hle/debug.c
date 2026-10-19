/* Debugging helpers, called from temporary probes in build/gen units (see NOTES) or from lldb. */
#include "hle.h"
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <sys/mman.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <pthread.h>
#include <unistd.h>
#include <dlfcn.h>

/* print each place a string occurs in resident guest memory, with ctx bytes around it */
void dbg_memscan(const char *needle, int ctx, int max_hits) {
  size_t nl = strlen(needle);
  mach_vm_address_t a = (mach_vm_address_t)(uintptr_t)mem;
  mach_vm_address_t end = a + MAP_END;
  int hits = 0;
  while (a < end && hits < max_hits) {
    mach_vm_size_t size = 0;
    natural_t depth = 1;
    vm_region_submap_info_data_64_t info;
    mach_msg_type_number_t cnt = VM_REGION_SUBMAP_INFO_COUNT_64;
    if (mach_vm_region_recurse(mach_task_self(), &a, &size, &depth, (vm_region_recurse_info_t)&info, &cnt) != KERN_SUCCESS) break;
    if (a >= end) break;
    if ((info.protection & VM_PROT_READ) && info.pages_resident) {
      /* walk 16 KB pages, skipping the ones not resident */
      for (mach_vm_address_t p = a; p < a + size && p < end && hits < max_hits; p += 0x4000) {
        char vec;
        if (mincore((void *)(uintptr_t)p, 0x4000, &vec) || !(vec & 1)) continue;
        const char *b = (const char *)(uintptr_t)p;
        size_t n = 0x4000 + (nl > 1 ? nl - 1 : 0);
        if (p + n > a + size) n = (size_t)(a + size - p);
        for (const char *q = b; (q = memmem(q, n - (size_t)(q - b), needle, nl)) && hits < max_hits; q++) {
          uint64_t g = (uint64_t)((const uint8_t *)q - mem);
          fprintf(stderr, "[memscan] \"%s\" at guest %#llx:\n  ", needle, (unsigned long long)g);
          const char *s0 = q - ctx < (const char *)mem ? q : q - ctx;
          for (const char *t = s0; t < q + nl + ctx; t++) fputc(*t >= 0x20 && *t < 0x7f ? *t : '.', stderr);
          fputc('\n', stderr);
          hits++;
        }
      }
    }
    a += size;
  }
  fprintf(stderr, "[memscan] \"%s\": %d hits\n", needle, hits);
}

/* print the printable strings (>= minlen) in guest [a, a+n) */
void dbg_strings(uint64_t a, uint64_t n, int minlen) {
  const unsigned char *p = mem + a;
  uint64_t i = 0;
  while (i < n) {
    uint64_t j = i;
    while (j < n && p[j] >= 0x20 && p[j] < 0x7f) j++;
    if ((int)(j - i) >= minlen) fprintf(stderr, "[strings] %#llx %.*s\n", (unsigned long long)(a + i), (int)(j - i), p + i);
    i = j + 1;
  }
}

void dbg_watch(uint64_t g, uint64_t n);

/* lldb hooks: dbg_vb_hook gets each debug-frame draw's vertex stream 0; dbg_vb_hit (break on it) only the
 * collapsed glyph quads of the post-title screen (x = 2.0, w = -1.0, see NOTES) */
__attribute__((noinline)) void dbg_vb_hit(const void *host, uint64_t guest) {
  __asm__ volatile("" ::"r"(host), "r"(guest));
  static int armed;
  if (!armed && getenv("FIFA_WATCH_VB")) { armed = 1; dbg_watch(guest, 0x60); }
}
__attribute__((noinline)) void dbg_vb_hook(const void *host, uint64_t guest) {
  const uint32_t *u = host;
  if (u[0] == 0x40000000u && u[3] == 0xbf800000u) dbg_vb_hit(host, guest);
}

/* Write watch: dbg_watch(guest, n) makes the host pages over [guest, guest+n) read-only; the crash handler
 * passes each fault to dbg_watch_fault, which reports the writer (host function = guest function), unprotects
 * the pages so the write goes through, and a helper thread re-protects them a moment later, up to
 * FIFA_WATCH_MAX (default 20) reports. Writes that only touch other data on the same 16 KB page are counted
 * but not reported. */
static uint64_t watch_g, watch_n, watch_plo, watch_phi;
static volatile int watch_armed, watch_reports, watch_other, watch_noaccess;

static void watch_protect(int ro) {
  mprotect((void *)(uintptr_t)watch_plo, watch_phi - watch_plo, !ro ? PROT_READ | PROT_WRITE : watch_noaccess ? PROT_NONE : PROT_READ);
}

static void *watch_rearm(void *arg) {
  (void)arg;
  const char *m = getenv("FIFA_WATCH_MAX");
  int max = m ? atoi(m) : 20;
  while (watch_reports < max) {
    usleep(500);
    if (!watch_armed) { watch_armed = 1; watch_protect(1); }
  }
  fprintf(stderr, "[watch] done: %d reports, %d writes elsewhere on the page\n", watch_reports, watch_other);
  return 0;
}

/* dbg_watch_access: the same for any access, reads included */
void dbg_watch(uint64_t g, uint64_t n);
void dbg_watch_access(uint64_t g, uint64_t n) {
  watch_noaccess = 1;
  dbg_watch(g, n);
}

void dbg_watch(uint64_t g, uint64_t n) {
  watch_g = g, watch_n = n;
  watch_plo = ((uint64_t)(uintptr_t)(mem + g)) & ~0x3fffull;
  watch_phi = ((uint64_t)(uintptr_t)(mem + g + n) + 0x3fff) & ~0x3fffull;
  fprintf(stderr, "[watch] guest %#llx+%#llx\n", (unsigned long long)g, (unsigned long long)n);
  watch_armed = 1;
  watch_protect(1);
  pthread_t t;
  pthread_create(&t, 0, watch_rearm, 0);
}

int dbg_watch_fault(uint64_t fault, uint64_t pc) {
  if (!watch_plo || fault < watch_plo || fault >= watch_phi) return 0;
  uint64_t g = fault - (uint64_t)(uintptr_t)mem;
  if (g + 16 > watch_g && g < watch_g + watch_n) {
    Dl_info di = {0};
    dladdr((void *)(uintptr_t)pc, &di);
    fprintf(stderr, "[watch] %s guest %#llx by %s+%#llx\n", watch_noaccess ? "access" : "write", (unsigned long long)g, di.dli_sname ? di.dli_sname : "?",
            (unsigned long long)(pc - (uint64_t)(uintptr_t)di.dli_saddr));
    if (cur_thread) {
      CPU *c = &cur_thread->cpu;
      fprintf(stderr, "[watch]  x0 %llx x1 %llx x2 %llx x3 %llx x19 %llx x20 %llx x30 %llx\n", (unsigned long long)c->x[0],
              (unsigned long long)c->x[1], (unsigned long long)c->x[2], (unsigned long long)c->x[3],
              (unsigned long long)c->x[19], (unsigned long long)c->x[20], (unsigned long long)c->x[30]);
      host_backtrace();
    }
    watch_reports++;
    /* FIFA_WATCH_FOLLOW=memcpy-symbol: when that function is the writer, watch its source instead */
    const char *fw = getenv("FIFA_WATCH_FOLLOW");
    if (fw && cur_thread && di.dli_sname && !strcmp(di.dli_sname, fw)) {
      CPU *c = &cur_thread->cpu;
      watch_protect(0);
      watch_g = c->x[1], watch_n = c->x[2];
      watch_plo = ((uint64_t)(uintptr_t)(mem + watch_g)) & ~0x3fffull;
      watch_phi = ((uint64_t)(uintptr_t)(mem + watch_g + watch_n) + 0x3fff) & ~0x3fffull;
      fprintf(stderr, "[watch] following to source guest %#llx+%#llx\n", (unsigned long long)watch_g, (unsigned long long)watch_n);
      watch_armed = 0;
      return 1;
    }
  } else
    watch_other++;
  watch_protect(0);
  watch_armed = 0;
  return 1;
}

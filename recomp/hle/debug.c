/* Debugging helpers, called from temporary probes in build/gen units (see NOTES) or from lldb. */
#include "hle.h"
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <sys/mman.h>
#include <stdio.h>
#include <string.h>

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

/* lldb hooks: dbg_vb_hook gets each debug-frame draw's vertex stream 0; dbg_vb_hit (break on it) only the
 * collapsed glyph quads of the post-title screen (x = 2.0, w = -1.0, see NOTES) */
__attribute__((noinline)) void dbg_vb_hit(const void *host, uint64_t guest) { __asm__ volatile("" ::"r"(host), "r"(guest)); }
__attribute__((noinline)) void dbg_vb_hook(const void *host, uint64_t guest) {
  const uint32_t *u = host;
  if (u[0] == 0x40000000u && u[3] == 0xbf800000u) dbg_vb_hit(host, guest);
}

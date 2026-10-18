/* Horizon kernel HLE: supervisor calls made by nnSdk. ABI per switchbrew:
 * arguments in x0-x7, result code in w0, outputs from x1. */
#include "host.h"
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#define R_OK 0
#define R_INVALID_HANDLE 0xE401
#define R_NOT_FOUND 0xF201
#define R_INVALID_ENUM 0xF001
#define R_TIMED_OUT 0xEA01

/* ---- handles ---- */
enum { H_FREE, H_THREAD, H_EVENT, H_PORT, H_SESSION };
typedef struct { int kind; void *obj; } Handle;
static Handle handles[4096];
static uint32_t handle_new(int kind, void *obj) {
  for (uint32_t i = 1; i < 4096; i++)
    if (handles[i].kind == H_FREE) { handles[i].kind = kind; handles[i].obj = obj; return 0x8000 | i; }
  HDIE("out of handles\n");
}
static Handle *handle_get(uint32_t h) {
  if (h == 0xFFFF8000) return 0;  /* pseudo-handle: current thread */
  uint32_t i = h & 0x7fff;
  return (h & 0x8000) && i < 4096 && handles[i].kind != H_FREE ? &handles[i] : 0;
}

/* ---- threads ---- */
static uint64_t next_thread_id = 0x50;
HostThread *thread_create_context(uint64_t stack_size) {
  HostThread *t = calloc(1, sizeof *t);
  uint64_t stack = guest_alloc(stack_size, 0x1000);
  t->stack_top = stack + stack_size;
  t->tlr = guest_alloc(0x1000, 0x1000);
  t->cpu.x[31] = t->stack_top;
  t->cpu.tpidrro = t->tlr;
  t->cpu.excl_addr = ~0ull;
  t->id = next_thread_id++;
  t->handle = handle_new(H_THREAD, t);
  return t;
}
static HostThread *thread_of(uint32_t h) {
  if (h == 0xFFFF8000) return cur_thread;
  Handle *e = handle_get(h);
  return e && e->kind == H_THREAD ? e->obj : 0;
}

/* ---- memory ---- */
static uint64_t heap_size;

static void query_memory(uint64_t info, uint64_t addr) {
  uint64_t base = 0, size = 0;
  uint32_t type = 0, perm = 0;  /* type 0 = unmapped */
  for (unsigned i = 0; i < host_module_count; i++) {
    const HostModule *m = &host_modules[i];
    if (addr >= m->base && addr < m->base + m->size) { base = m->base; size = m->size; type = 3; perm = 7; }
  }
  if (!size && addr >= HEAP_BASE && addr < HEAP_BASE + heap_size) { base = HEAP_BASE; size = heap_size; type = 5; perm = 3; }
  if (!size && addr >= ALLOC_BASE && addr < ALLOC_END) { base = ALLOC_BASE; size = ALLOC_END - ALLOC_BASE; type = 5; perm = 3; }
  if (!size) {
    /* Report the gap around addr as unmapped. */
    base = addr & ~0xfffull;
    size = 0x1000;
  }
  uint8_t *p = gptr(info);
  memset(p, 0, 0x28);
  memcpy(p, &base, 8);
  memcpy(p + 8, &size, 8);
  memcpy(p + 16, &type, 4);
  memcpy(p + 24, &perm, 4);
}

static uint64_t get_info(CPU *c, uint32_t type, uint32_t h, uint64_t sub, uint32_t *res) {
  (void)h; (void)c;
  *res = R_OK;
  switch (type) {
  case 0: return 0xF;                       /* core mask */
  case 1: return 0xFFFFFFFFFFFFFFFFull;     /* priority mask */
  case 2: return MAP_BASE;                  /* alias region */
  case 3: return MAP_END - MAP_BASE;
  case 4: return HEAP_BASE;                 /* heap region */
  case 5: return HEAP_MAX - HEAP_BASE;
  case 6: return 0xCD500000ull;             /* total memory (3.2 GB, typical application pool) */
  case 7: return heap_size + 0x10000000ull;  /* used memory (approx.) */
  case 8: return 0;                         /* debugger attached */
  case 9: return 0;                         /* resource limit handle */
  case 10: return 0;                        /* idle tick count */
  case 11: return (uint64_t)rand() << 32 | (uint64_t)rand();  /* random entropy */
  case 12: return 0x08000000ull;            /* ASLR region */
  case 13: return 0xF8000000ull;
  case 14: return ALLOC_BASE;               /* stack region */
  case 15: return ALLOC_END - ALLOC_BASE;
  case 16: case 17: return 0;               /* system resource size */
  case 18: return 0x0100C49025D3E000ull;    /* program id */
  case 20: return 0;                        /* user exception context */
  case 21: return 0xCD500000ull;            /* total non-system memory */
  case 22: return heap_size + 0x10000000ull;
  case 23: return 1;                        /* is application */
  case 24: return 64;                       /* free thread count */
  case 25: return 0;                        /* thread tick count */
  default:
    HLOG("[svc] GetInfo type %u sub %#llx unimplemented\n", type, (unsigned long long)sub);
    *res = R_INVALID_ENUM;
    return 0;
  }
}

void svc_dispatch(CPU *c, uint32_t imm, uint64_t pc) {
  uint64_t *x = c->x;
  if (host_trace) HLOG("[svc] %#x at %#llx x0=%#llx x1=%#llx x2=%#llx\n", imm, (unsigned long long)pc,
                       (unsigned long long)x[0], (unsigned long long)x[1], (unsigned long long)x[2]);
  switch (imm) {
  case 0x01: {  /* SetHeapSize(size) -> addr */
    uint64_t size = x[1];
    if (HEAP_BASE + size > HEAP_MAX) { x[0] = 0xD001; return; }
    if (size > heap_size) memset(gptr(HEAP_BASE + heap_size), 0, size - heap_size);
    heap_size = size;
    x[0] = R_OK; x[1] = HEAP_BASE;
    return;
  }
  case 0x02: case 0x03:  /* SetMemoryPermission / SetMemoryAttribute */
    x[0] = R_OK;
    return;
  case 0x06: {  /* QueryMemory(info*, pageinfo*, addr) */
    query_memory(x[0], x[2]);
    x[0] = R_OK; x[1] = 0;
    return;
  }
  case 0x07:
    HLOG("[svc] ExitProcess\n");
    exit(0);
  case 0x0B: {  /* SleepThread(ns) */
    int64_t ns = (int64_t)x[0];
    if (ns > 0) { struct timespec ts = {ns / 1000000000, ns % 1000000000}; nanosleep(&ts, 0); }
    else sched_yield();
    return;
  }
  case 0x0C: {  /* GetThreadPriority(handle) -> prio */
    x[0] = R_OK; x[1] = 44;
    return;
  }
  case 0x0D: case 0x0F:  /* SetThreadPriority / SetThreadCoreMask */
    x[0] = R_OK;
    return;
  case 0x0E:  /* GetThreadCoreMask -> core, mask */
    x[0] = R_OK; x[1] = 0; x[2] = 0xF;
    return;
  case 0x10:  /* GetCurrentProcessorNumber */
    x[0] = 0;
    return;
  case 0x16:  /* CloseHandle */
    x[0] = R_OK;
    return;
  case 0x1E:  /* GetSystemTick */
    x[0] = a64_cntpct();
    return;
  case 0x25: {  /* GetThreadId(handle) -> id */
    HostThread *t = thread_of((uint32_t)x[1]);
    if (!t) { x[0] = R_INVALID_HANDLE; return; }
    x[0] = R_OK; x[1] = t->id;
    return;
  }
  case 0x26:  /* Break(reason, arg, size) */
    HDIE("svcBreak reason %#llx arg %#llx size %#llx at %#llx\n", (unsigned long long)x[0],
         (unsigned long long)x[1], (unsigned long long)x[2], (unsigned long long)pc);
  case 0x27:  /* OutputDebugString(str, size) */
    HLOG("[debug] %.*s\n", (int)x[1], gstr(x[0]));
    x[0] = R_OK;
    return;
  case 0x29: {  /* GetInfo(type, handle, subtype) -> info */
    uint32_t r;
    uint64_t v = get_info(c, (uint32_t)x[1], (uint32_t)x[2], x[3], &r);
    x[0] = r; x[1] = v;
    return;
  }
  default:
    HDIE("unimplemented svc %#x at %#llx (x0=%#llx x1=%#llx x2=%#llx x3=%#llx)\n", imm, (unsigned long long)pc,
         (unsigned long long)x[0], (unsigned long long)x[1], (unsigned long long)x[2], (unsigned long long)x[3]);
  }
}

/* Host side of the recompiled program: guest memory, modules, threads, HLE. */
#pragma once
#include "runtime.h"
#include <stdio.h>

/* Guest address space (all offsets from `mem`, 4 GB reserved). */
#define GUEST_SPACE        0x100000000ull
#define HOST_THUNK_BASE    0x07E00000ull  /* host callbacks callable from guest code, 16 bytes each */
#define HOST_THUNK_END     0x07F00000ull
#define MISSING_BASE       0x07F00000ull  /* unresolved imports (recomp/image.py) */
#define MISSING_END        0x08000000ull
#define ALLOC_BASE         0x1C000000ull  /* host-made allocations: stacks, TLRs, misc */
#define ALLOC_END          0x20000000ull
#define HEAP_BASE          0x20000000ull  /* svcSetHeapSize */
#define HEAP_MAX           0xC0000000ull
#define MAP_BASE           0xE0000000ull  /* svcMapMemory / transfer memory / etc. */
#define MAP_END            0x100000000ull

typedef struct { const char *name; uint64_t base, size, init; } HostModule;
extern const HostModule host_modules[];
extern const unsigned host_module_count;
extern const uint64_t host_nn_init_start;

extern int host_trace;  /* FIFA_TRACE env: log svcs and dispatcher misses */

#define HLOG(...) do { fprintf(stderr, __VA_ARGS__); } while (0)
#define HDIE(...) do { fprintf(stderr, "fatal: " __VA_ARGS__); host_backtrace(); abort(); } while (0)

uint64_t guest_alloc(uint64_t size, uint64_t align);
uint64_t host_thunk(A64Func fn, const char *name);
const char *host_symbol(uint64_t addr, uint64_t *off);
void host_backtrace(void);

/* threads */
typedef struct HostThread {
  CPU cpu;
  uint64_t tlr;          /* thread local region (TPIDRRO_EL0) */
  uint64_t stack_top;
  uint32_t handle;
  uint64_t id;
  uint64_t entry, arg;
  int priority, exited, cancel;
  /* kernel wait state, guarded by the kernel lock */
  int wait_kind;
  uint64_t wait_addr, wait_mutex, wait_seq;
  uint32_t wait_tag;
  int woken;
  void *cv;              /* pthread_cond_t */
  struct HostThread *next;
} HostThread;
extern __thread HostThread *cur_thread;

/* Read/write guest memory from host code. */
static inline void *gptr(uint64_t a) { return mem + a; }
static inline const char *gstr(uint64_t a) { return a ? (const char *)(mem + a) : "(null)"; }

void svc_dispatch(CPU *c, uint32_t imm, uint64_t pc);

/* kernel objects (kernel.c) */
enum { H_FREE, H_THREAD, H_EVENT_R, H_EVENT_W, H_SESSION };
uint32_t kobj_new(int kind, void *obj);
void *kobj_get(uint32_t h, int kind);
void kobj_close(uint32_t h);
typedef struct KEvent KEvent;
KEvent *kevent_create(uint32_t *readable, uint32_t *writable);  /* writable may be NULL */
void kevent_signal(KEvent *e);
void kevent_clear(KEvent *e);

/* IPC (ipc.c) */
uint32_t ipc_connect_port(const char *name, uint32_t *out_handle);
uint32_t ipc_request(uint32_t session, uint64_t buf);

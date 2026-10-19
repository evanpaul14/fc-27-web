/* Host side of the recompiled program: guest memory, modules, threads, HLE. */
#pragma once
#include "runtime.h"
#include <stdio.h>
#include <stdlib.h>

/* Guest address space (all offsets from `mem`, 68 GB reserved, committed on touch). */
#define GUEST_SPACE        0x1100000000ull
#define NULL_GUARD_END     0x07E00000ull  /* [0, here) is PROT_NONE so null accesses fault */
#define HOST_THUNK_BASE    0x07E00000ull  /* host callbacks callable from guest code, 16 bytes each */
#define HOST_THUNK_END     0x07F00000ull
#define MISSING_BASE       0x07F00000ull  /* unresolved imports (recomp/image.py) */
#define MISSING_END        0x08000000ull
#define ALLOC_BASE         0x1C000000ull  /* host-made allocations: stacks, TLRs, misc */
#define ALLOC_END          0x20000000ull
#define HEAP_BASE          0x20000000ull  /* svcSetHeapSize */
#define HEAP_MAX           0xC0000000ull
#define STACK_BASE         0xC0000000ull  /* guest stack region (alias stacks via svcMapMemory) */
#define STACK_END          0xE0000000ull
/* 0xE0000000-0x100000000 unused */
/* Alias region: svcMapMemory, svcMapPhysicalMemory. 64 GB as in a 39-bit
 * address space: nn::os VammManager reserves ~1 GB at its start for its own
 * bookkeeping, and Frostbite's VirtualAlloc reserves ~55 GB of address space
 * in one piece (it aborts with os 3-12 when that doesn't fit). */
#define MAP_BASE           0x100000000ull
#define MAP_END            0x1100000000ull
/* NVN virtual memory pools, above what the guest is told about: physical pool pages are aliased in
 * (nvnMemoryPoolMapVirtual), so their GPU addresses are guest addresses like any other pool's. */
#define VPOOL_BASE         0x1100000000ull
#define VPOOL_END          0x1400000000ull

#define APP_PID            0x51ull  /* process id, also the applet resource user id */

typedef struct { const char *name; uint64_t base, size, init; int ro; } HostModule;  /* ro: initialised by nn::ro::LoadModule */
extern const HostModule host_modules[];
extern const unsigned host_module_count;
extern const uint64_t host_nn_init_start;
extern const uint64_t host_ro_lookup_slot_ptr;  /* nn::ro::detail::g_pLookupGlobalManualFunctionPointer */

extern int host_trace;  /* FIFA_TRACE env: log svcs and dispatcher misses */
int host_docked(void);  /* FIFA_DOCKED=1: report a docked console (1920x1080, boost); default handheld (1280x720) */

#define HLOG(...) do { fprintf(stderr, __VA_ARGS__); } while (0)
#define HDIE(...) do { fprintf(stderr, "fatal: " __VA_ARGS__); host_backtrace(); abort(); } while (0)

uint64_t guest_alloc(uint64_t size, uint64_t align);
uint64_t host_thunk(A64Func fn, const char *name);
extern __thread const char *host_thunk_name;  /* name of the thunk being called */
const char *host_symbol(uint64_t addr, uint64_t *off);
uint64_t host_symbol_addr(const char *name, uint64_t lo, uint64_t hi);
void host_backtrace(void);
void host_thread_signals(void);  /* call first on every host thread that runs guest code */

/* threads */
typedef struct HostThread {
  CPU cpu;
  uint64_t tlr;          /* thread local region (TPIDRRO_EL0) */
  uint64_t stack_top;
  uint32_t handle;
  uint64_t id;
  uint64_t entry, arg;
  int priority, core, exited, cancel;
  /* kernel wait state, guarded by the kernel lock */
  int wait_kind;
  uint64_t wait_addr, wait_mutex, wait_seq;
  uint32_t wait_tag;
  int woken;
  void *cv;              /* pthread_cond_t */
  struct HostThread *next;
} HostThread;
extern __thread HostThread *cur_thread;
const char *thread_name(HostThread *t);
void host_list_threads(void);

/* Read/write guest memory from host code. */
static inline void *gptr(uint64_t a) { return mem + a; }
static inline const char *gstr(uint64_t a) { return a ? (const char *)(mem + a) : "(null)"; }

void svc_dispatch(CPU *c, uint32_t imm, uint64_t pc);
void core_enter(void);  /* FIFA_CORES: take / give up a guest core slot */
void core_leave(void);

/* kernel objects (kernel.c) */
enum { H_FREE, H_THREAD, H_EVENT_R, H_EVENT_W, H_SESSION, H_SHMEM, H_TMEM };
/* Transfer memory (svcCreateTransferMemory): guest memory lent to a service. */
typedef struct { uint64_t addr, size; uint32_t perm; } KTransferMemory;
uint32_t kobj_new(int kind, void *obj);
void *kobj_get(uint32_t h, int kind);
void kobj_close(uint32_t h);
typedef struct KEvent KEvent;
KEvent *kevent_create(uint32_t *readable, uint32_t *writable);  /* writable may be NULL */
void kevent_signal(KEvent *e);
void kevent_clear(KEvent *e);
typedef struct KSharedMemory KSharedMemory;
KSharedMemory *kshmem_create(uint64_t size, uint32_t *handle);  /* handle may be NULL */
void *kshmem_ptr(KSharedMemory *s);  /* current contents: guest mapping if mapped, else host buffer */

/* IPC (ipc.c) */
uint32_t ipc_connect_port(const char *name, uint32_t *out_handle);
uint32_t ipc_request(uint32_t session, uint64_t buf);

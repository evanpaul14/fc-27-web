/* Horizon kernel HLE: supervisor calls made by nnSdk. ABI per switchbrew:
 * arguments in x0-x7, result code in w0, outputs from x1.
 *
 * Every svc that touches shared kernel state runs under one global lock;
 * each guest thread is a host pthread that blocks on its own condvar.
 * Mutex/condvar/address-arbiter semantics follow Atmosphère's kernel. */
#include "host.h"
#include <errno.h>
#include <pthread.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#define RES_OK 0
#define R_INVALID_HANDLE 0xE401
#define R_INVALID_ENUM 0xF001
#define R_INVALID_STATE 0xFA01
#define R_TIMED_OUT 0xEA01
#define R_CANCELLED 0xEC01
#define R_OUT_OF_MEMORY 0xD001
#define R_INVALID_SIZE 0xCA01

#define HAS_WAITERS 0x40000000u
#define PSEUDO_THREAD 0xFFFF8000u

static pthread_mutex_t klock = PTHREAD_MUTEX_INITIALIZER;

/* ---- handles ---- */
struct KEvent { int signaled; };
typedef struct KEvent Event;
typedef struct { int kind; void *obj; } Handle;
static Handle handles[8192];
static uint32_t handle_new(int kind, void *obj) {
  for (uint32_t i = 1; i < 8192; i++)
    if (handles[i].kind == H_FREE) { handles[i].kind = kind; handles[i].obj = obj; return 0x10000 | i; }
  HDIE("out of handles\n");
}
static Handle *handle_get(uint32_t h) {
  uint32_t i = h & 0xffff;
  return (h >> 16) == 1 && i < 8192 && handles[i].kind != H_FREE ? &handles[i] : 0;
}

/* ---- threads ---- */
enum { W_NONE, W_MUTEX, W_CVAR, W_ADDR, W_SYNC, W_SLEEP };
static HostThread *all_threads;
static uint64_t next_thread_id = 0x50, wait_seq;

HostThread *thread_create_context(uint64_t stack_size) {
  HostThread *t = calloc(1, sizeof *t);
  if (stack_size) {
    uint64_t stack = guest_alloc(stack_size, 0x1000);
    t->stack_top = stack + stack_size;
  }
  t->tlr = guest_alloc(0x1000, 0x1000);
  t->cpu.x[31] = t->stack_top;
  t->cpu.tpidrro = t->tlr;
  t->cpu.excl_addr = ~0ull;
  t->priority = 44;
  t->cv = malloc(sizeof(pthread_cond_t));
  pthread_cond_init(t->cv, 0);
  pthread_mutex_lock(&klock);
  t->id = next_thread_id++;
  t->handle = handle_new(H_THREAD, t);
  /* nn::os tags mutex/critical-section ownership with the thread handle the
   * kernel leaves at TLR+0x110 (nnSdk only ever reads it). */
  memcpy(gptr(t->tlr + 0x110), &t->handle, 4);
  t->next = all_threads;
  all_threads = t;
  pthread_mutex_unlock(&klock);
  return t;
}
static HostThread *thread_of(uint32_t h) {
  if (h == PSEUDO_THREAD) return cur_thread;
  Handle *e = handle_get(h);
  return e && e->kind == H_THREAD ? e->obj : 0;
}

/* Block the current thread (klock held) until woken or the deadline passes.
 * Returns 0 if woken, R_TIMED_OUT otherwise. timeout_ns < 0 waits forever. */
static uint32_t kwait(HostThread *t, int64_t timeout_ns) {
  t->woken = 0;
  t->wait_seq = ++wait_seq;
  if (timeout_ns < 0) {
    while (!t->woken) pthread_cond_wait(t->cv, &klock);
    return 0;
  }
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  ts.tv_sec += timeout_ns / 1000000000;
  ts.tv_nsec += timeout_ns % 1000000000;
  if (ts.tv_nsec >= 1000000000) { ts.tv_sec++; ts.tv_nsec -= 1000000000; }
  while (!t->woken)
    if (pthread_cond_timedwait(t->cv, &klock, &ts) == ETIMEDOUT) break;
  return t->woken ? 0 : R_TIMED_OUT;
}
static void kwake(HostThread *t) {
  t->woken = 1;
  t->wait_kind = W_NONE;
  pthread_cond_signal(t->cv);
}
/* Oldest waiter of `kind` on `addr`, optionally skipping one. */
static HostThread *first_waiter(int kind, uint64_t addr, HostThread *skip) {
  HostThread *best = 0;
  for (HostThread *t = all_threads; t; t = t->next)
    if (t != skip && t->wait_kind == kind && t->wait_addr == addr && (!best || t->wait_seq < best->wait_seq)) best = t;
  return best;
}

static uint32_t *u32p(uint64_t a) { return (uint32_t *)gptr(a); }

/* Hand the mutex at addr to its next waiter, or free it. (klock held) */
static void mutex_release(uint64_t addr) {
  HostThread *w = first_waiter(W_MUTEX, addr, 0);
  if (!w) { __atomic_store_n(u32p(addr), 0, __ATOMIC_SEQ_CST); return; }
  uint32_t v = w->wait_tag | (first_waiter(W_MUTEX, addr, w) ? HAS_WAITERS : 0);
  __atomic_store_n(u32p(addr), v, __ATOMIC_SEQ_CST);
  kwake(w);
}
/* Try to give mutex at addr to t; otherwise queue t as a mutex waiter. (klock held) */
static void mutex_acquire_for(HostThread *t, uint64_t addr) {
  uint32_t v = __atomic_load_n(u32p(addr), __ATOMIC_SEQ_CST);
  for (;;) {
    if (v == 0) {
      if (__atomic_compare_exchange_n(u32p(addr), &v, t->wait_tag, 0, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST)) {
        kwake(t);
        return;
      }
    } else if (__atomic_compare_exchange_n(u32p(addr), &v, v | HAS_WAITERS, 0, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST)) {
      t->wait_kind = W_MUTEX;
      t->wait_addr = addr;
      return;
    }
  }
}

static void *thread_main(void *p) {
  HostThread *t = p;
  cur_thread = t;
  host_thread_signals();
  CPU *c = &t->cpu;
  c->x[0] = t->arg;
  c->x[30] = 0;
  a64_call(c, t->entry);
  pthread_mutex_lock(&klock);
  t->exited = 1;
  for (HostThread *o = all_threads; o; o = o->next)
    if (o->wait_kind == W_SYNC) pthread_cond_signal(o->cv);
  pthread_mutex_unlock(&klock);
  return 0;
}

/* ---- waiting on handles ---- */
static int handle_signaled(uint32_t h) {
  Handle *e = handle_get(h);
  if (!e) return -1;
  if (e->kind == H_THREAD) return ((HostThread *)e->obj)->exited;
  if (e->kind == H_EVENT_R) return ((Event *)e->obj)->signaled;
  return -1;
}
static void sync_notify(void) {
  for (HostThread *o = all_threads; o; o = o->next)
    if (o->wait_kind == W_SYNC) pthread_cond_signal(o->cv);
}

/* ---- memory ---- */
static uint64_t heap_size;

/* svcMapMemory / svcMapSharedMemory destinations, so QueryMemory shows them as taken. */
enum { MEM_SHARED = 0x6, MEM_STACK = 0xB };  /* Horizon MemoryState */
typedef struct { uint64_t base, size; uint32_t type; } Mapping;
static Mapping mappings[1024];
static unsigned mapping_count;

static void mapping_add(uint64_t base, uint64_t size, uint32_t type) {
  pthread_mutex_lock(&klock);
  if (mapping_count < sizeof mappings / sizeof *mappings) mappings[mapping_count++] = (Mapping){base, size, type};
  else HLOG("[svc] MapMemory: mapping table full\n");
  pthread_mutex_unlock(&klock);
}
static void mapping_remove(uint64_t base) {
  pthread_mutex_lock(&klock);
  for (unsigned i = 0; i < mapping_count; i++)
    if (mappings[i].base == base) { mappings[i] = mappings[--mapping_count]; break; }
  pthread_mutex_unlock(&klock);
}

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
    pthread_mutex_lock(&klock);
    uint64_t lo = 0, hi = GUEST_SPACE;  /* free block around addr, bounded by mappings */
    for (unsigned i = 0; i < mapping_count; i++) {
      const Mapping *m = &mappings[i];
      if (addr >= m->base && addr < m->base + m->size) { base = m->base; size = m->size; type = m->type; perm = 3; }
      else if (m->base + m->size <= addr && m->base + m->size > lo) lo = m->base + m->size;
      else if (m->base > addr && m->base < hi) hi = m->base;
    }
    pthread_mutex_unlock(&klock);
    if (!size && addr >= STACK_BASE && addr < STACK_END) {
      base = lo > STACK_BASE ? lo : STACK_BASE;
      size = (hi < STACK_END ? hi : STACK_END) - base;
    }
  }
  if (!size) { base = addr & ~0xfffull; size = 0x1000; }
  uint8_t *p = gptr(info);
  memset(p, 0, 0x28);
  memcpy(p, &base, 8);
  memcpy(p + 8, &size, 8);
  memcpy(p + 16, &type, 4);
  memcpy(p + 24, &perm, 4);
}

static uint64_t get_info(uint32_t type, uint64_t sub, uint32_t *res) {
  *res = RES_OK;
  switch (type) {
  case 0: return 0xF;                       /* core mask */
  case 1: return 0xFFFFFFFFFFFFFFFFull;     /* priority mask */
  case 2: return MAP_BASE;                  /* alias region */
  case 3: return MAP_END - MAP_BASE;
  case 4: return HEAP_BASE;                 /* heap region */
  case 5: return HEAP_MAX - HEAP_BASE;
  case 6: return 0xCD500000ull;             /* total memory (typical application pool) */
  case 7: return heap_size + 0x10000000ull;  /* used memory (approx.) */
  case 8: return 0;                         /* debugger attached */
  case 9: return 0;                         /* resource limit handle */
  case 10: return 0;                        /* idle tick count */
  case 11: return (uint64_t)rand() << 32 | (uint64_t)rand();
  case 12: return 0x08000000ull;            /* ASLR region: everything mappable */
  case 13: return GUEST_SPACE - 0x08000000ull;
  case 14: return STACK_BASE;               /* stack region */
  case 15: return STACK_END - STACK_BASE;
  /* System resource (kernel page tables for MapPhysicalMemory). Non-zero
   * enables nn::os virtual address memory (VammManager) over the alias region. */
  case 16: return 0x2000000ull;             /* system resource size total */
  case 17: return 0;                        /* system resource size used */
  case 18: return 0x0100C49025D3E000ull;    /* program id */
  case 20: return 0;                        /* user exception context */
  case 21: return 0xCD500000ull;            /* total non-system memory */
  case 22: return heap_size + 0x10000000ull;
  case 23: return 1;                        /* is application */
  case 24: return 64;                       /* free thread count */
  case 25: return 0;                        /* thread tick count */
  case 28: return 0;                        /* alias region extra size (18.0.0+) */
  default:
    HLOG("[svc] GetInfo type %u sub %#llx unimplemented\n", type, (unsigned long long)sub);
    *res = R_INVALID_ENUM;
    return 0;
  }
}

uint32_t kobj_new(int kind, void *obj) {
  pthread_mutex_lock(&klock);
  uint32_t h = handle_new(kind, obj);
  pthread_mutex_unlock(&klock);
  return h;
}
void *kobj_get(uint32_t h, int kind) {
  pthread_mutex_lock(&klock);
  Handle *e = handle_get(h);
  void *o = e && e->kind == kind ? e->obj : 0;
  pthread_mutex_unlock(&klock);
  return o;
}
void kobj_close(uint32_t h) {
  pthread_mutex_lock(&klock);
  Handle *e = handle_get(h);
  if (e && e->kind != H_THREAD) e->kind = H_FREE;
  pthread_mutex_unlock(&klock);
}
KEvent *kevent_create(uint32_t *readable, uint32_t *writable) {
  Event *ev = calloc(1, sizeof *ev);
  pthread_mutex_lock(&klock);
  *readable = handle_new(H_EVENT_R, ev);
  if (writable) *writable = handle_new(H_EVENT_W, ev);
  pthread_mutex_unlock(&klock);
  return ev;
}
/* Shared memory without page aliasing (host pages are 16 KB, guest
 * addresses 4 KB aligned, and wasm can't alias at all): the contents live
 * in a host buffer while unmapped and at the guest address while mapped,
 * and only one guest mapping exists at a time. Host code must go through
 * kshmem_ptr() on every access. */
struct KSharedMemory { uint64_t size, guest; uint8_t *backing; };
KSharedMemory *kshmem_create(uint64_t size, uint32_t *handle) {
  KSharedMemory *s = calloc(1, sizeof *s);
  s->size = size;
  s->backing = calloc(1, size);
  if (handle) *handle = kobj_new(H_SHMEM, s);
  return s;
}
void *kshmem_ptr(KSharedMemory *s) { return s->guest ? gptr(s->guest) : s->backing; }

void kevent_signal(KEvent *e) {
  pthread_mutex_lock(&klock);
  e->signaled = 1;
  sync_notify();
  pthread_mutex_unlock(&klock);
}
void kevent_clear(KEvent *e) {
  pthread_mutex_lock(&klock);
  e->signaled = 0;
  pthread_mutex_unlock(&klock);
}


void svc_dispatch(CPU *c, uint32_t imm, uint64_t pc) {
  uint64_t *x = c->x;
  HostThread *self = cur_thread;
  if (host_trace) HLOG("[svc %llx] %#x at %#llx x0=%#llx x1=%#llx x2=%#llx x3=%#llx\n", (unsigned long long)self->id,
                       imm, (unsigned long long)pc, (unsigned long long)x[0], (unsigned long long)x[1],
                       (unsigned long long)x[2], (unsigned long long)x[3]);
  switch (imm) {
  case 0x01: {  /* SetHeapSize(size) -> addr */
    uint64_t size = x[1];
    if (HEAP_BASE + size > HEAP_MAX) { x[0] = R_OUT_OF_MEMORY; return; }
    if (size > heap_size) memset(gptr(HEAP_BASE + heap_size), 0, size - heap_size);
    heap_size = size;
    x[0] = RES_OK; x[1] = HEAP_BASE;
    return;
  }
  case 0x02: case 0x03:  /* SetMemoryPermission / SetMemoryAttribute */
    x[0] = RES_OK;
    return;
  case 0x04: case 0x05: {  /* MapMemory / UnmapMemory(dst, src, size): flat space, so move the contents */
    uint64_t dst = x[0], src = x[1], size = x[2];
    if (imm == 0x04) { memmove(gptr(dst), gptr(src), size); mapping_add(dst, size, MEM_STACK); }
    else { memmove(gptr(src), gptr(dst), size); mapping_remove(dst); }
    x[0] = RES_OK;
    return;
  }
  case 0x06:  /* QueryMemory(info*, pageinfo*, addr) */
    query_memory(x[0], x[2]);
    x[0] = RES_OK; x[1] = 0;
    return;
  case 0x07:
    HLOG("[svc] ExitProcess\n");
    exit(0);
  case 0x08: {  /* CreateThread(_, entry, arg, stack_top, prio, core) -> handle */
    HostThread *t = thread_create_context(0);
    t->entry = x[1]; t->arg = x[2];
    t->stack_top = x[3]; t->cpu.x[31] = x[3];
    t->priority = (int)x[4];
    x[0] = RES_OK; x[1] = t->handle;
    if (host_trace) HLOG("[kernel] CreateThread %#x entry %#llx\n", t->handle, (unsigned long long)t->entry);
    return;
  }
  case 0x09: {  /* StartThread(handle) */
    HostThread *t = thread_of((uint32_t)x[0]);
    if (!t) { x[0] = R_INVALID_HANDLE; return; }
    pthread_attr_t a;
    pthread_attr_init(&a);
    pthread_attr_setstacksize(&a, 64 << 20);
    pthread_t pt;
    pthread_create(&pt, &a, thread_main, t);
    pthread_detach(pt);
    x[0] = RES_OK;
    return;
  }
  case 0x0A:  /* ExitThread */
    pthread_mutex_lock(&klock);
    self->exited = 1;
    sync_notify();
    pthread_mutex_unlock(&klock);
    pthread_exit(0);
  case 0x0B: {  /* SleepThread(ns) */
    int64_t ns = (int64_t)x[0];
    if (ns > 0) { struct timespec ts = {ns / 1000000000, ns % 1000000000}; nanosleep(&ts, 0); }
    else sched_yield();
    return;
  }
  case 0x0C: {  /* GetThreadPriority(handle) -> prio */
    HostThread *t = thread_of((uint32_t)x[1]);
    x[0] = t ? RES_OK : R_INVALID_HANDLE; x[1] = t ? (uint64_t)t->priority : 0;
    return;
  }
  case 0x0D: {  /* SetThreadPriority(handle, prio) */
    HostThread *t = thread_of((uint32_t)x[0]);
    if (t) t->priority = (int)x[1];
    x[0] = t ? RES_OK : R_INVALID_HANDLE;
    return;
  }
  case 0x0E:  /* GetThreadCoreMask -> core, mask */
    x[0] = RES_OK; x[1] = 0; x[2] = 0xF;
    return;
  case 0x0F:  /* SetThreadCoreMask */
    x[0] = RES_OK;
    return;
  case 0x10:  /* GetCurrentProcessorNumber */
    x[0] = 0;
    return;
  case 0x11: case 0x12: case 0x17: {  /* SignalEvent / ClearEvent / ResetSignal */
    pthread_mutex_lock(&klock);
    Handle *e = handle_get((uint32_t)x[0]);
    if (!e || (e->kind != H_EVENT_R && e->kind != H_EVENT_W)) { x[0] = R_INVALID_HANDLE; }
    else {
      Event *ev = e->obj;
      if (imm == 0x11) { ev->signaled = 1; sync_notify(); x[0] = RES_OK; }
      else if (imm == 0x17 && !ev->signaled) x[0] = R_INVALID_STATE;
      else { ev->signaled = 0; x[0] = RES_OK; }
    }
    pthread_mutex_unlock(&klock);
    return;
  }
  case 0x16: {  /* CloseHandle */
    pthread_mutex_lock(&klock);
    Handle *e = handle_get((uint32_t)x[0]);
    if (e && e->kind != H_THREAD) e->kind = H_FREE;  /* thread contexts stay alive */
    pthread_mutex_unlock(&klock);
    x[0] = RES_OK;
    return;
  }
  case 0x18: {  /* WaitSynchronization(_, handles*, count, timeout) -> index */
    uint64_t hs = x[1];
    int n = (int)x[2];
    int64_t timeout = (int64_t)x[3];
    pthread_mutex_lock(&klock);
    struct timespec dl;
    clock_gettime(CLOCK_REALTIME, &dl);
    if (timeout > 0) {
      dl.tv_sec += timeout / 1000000000;
      dl.tv_nsec += timeout % 1000000000;
      if (dl.tv_nsec >= 1000000000) { dl.tv_sec++; dl.tv_nsec -= 1000000000; }
    }
    uint32_t res = R_TIMED_OUT;
    int idx = -1;
    for (;;) {
      if (self->cancel) { self->cancel = 0; res = R_CANCELLED; break; }
      for (int i = 0; i < n; i++) {
        int s = handle_signaled(*u32p(hs + 4 * i));
        if (s < 0) { res = R_INVALID_HANDLE; idx = i; goto done; }
        if (s) { res = RES_OK; idx = i; goto done; }
      }
      if (timeout == 0) break;
      self->wait_kind = W_SYNC;
      int r = timeout < 0 ? pthread_cond_wait(self->cv, &klock) : pthread_cond_timedwait(self->cv, &klock, &dl);
      self->wait_kind = W_NONE;
      if (r == ETIMEDOUT) break;
    }
  done:
    self->wait_kind = W_NONE;
    pthread_mutex_unlock(&klock);
    x[0] = res; x[1] = (uint64_t)(int64_t)idx;
    return;
  }
  case 0x19: {  /* CancelSynchronization(thread) */
    pthread_mutex_lock(&klock);
    HostThread *t = thread_of((uint32_t)x[0]);
    if (t) { t->cancel = 1; pthread_cond_signal(t->cv); }
    pthread_mutex_unlock(&klock);
    x[0] = t ? RES_OK : R_INVALID_HANDLE;
    return;
  }
  case 0x1A: {  /* ArbitrateLock(owner_handle, addr, tag) */
    uint32_t owner = (uint32_t)x[0], tag = (uint32_t)x[2];
    uint64_t addr = x[1];
    pthread_mutex_lock(&klock);
    if (__atomic_load_n(u32p(addr), __ATOMIC_SEQ_CST) == (owner | HAS_WAITERS)) {
      self->wait_kind = W_MUTEX; self->wait_addr = addr; self->wait_tag = tag;
      kwait(self, -1);
    }
    pthread_mutex_unlock(&klock);
    x[0] = RES_OK;
    return;
  }
  case 0x1B:  /* ArbitrateUnlock(addr) */
    pthread_mutex_lock(&klock);
    mutex_release(x[0]);
    pthread_mutex_unlock(&klock);
    x[0] = RES_OK;
    return;
  case 0x1C: {  /* WaitProcessWideKeyAtomic(mutex, key, tag, timeout) */
    uint64_t maddr = x[0], key = x[1];
    uint32_t tag = (uint32_t)x[2];
    int64_t timeout = (int64_t)x[3];
    pthread_mutex_lock(&klock);
    mutex_release(maddr);
    __atomic_store_n(u32p(key), 1, __ATOMIC_SEQ_CST);
    self->wait_kind = W_CVAR; self->wait_addr = key; self->wait_mutex = maddr; self->wait_tag = tag;
    uint32_t r = timeout == 0 ? R_TIMED_OUT : kwait(self, timeout);
    if (r) {
      /* timed out: leave the condvar queue, then take the mutex back */
      if (self->wait_kind == W_CVAR) {
        self->wait_kind = W_NONE;
        mutex_acquire_for(self, maddr);
      }
      if (!self->woken) kwait(self, -1);
    }
    pthread_mutex_unlock(&klock);
    x[0] = r;
    return;
  }
  case 0x1D: {  /* SignalProcessWideKey(key, count) */
    uint64_t key = x[0];
    int32_t count = (int32_t)x[1];
    pthread_mutex_lock(&klock);
    for (int i = 0; count <= 0 || i < count; i++) {
      HostThread *w = first_waiter(W_CVAR, key, 0);
      if (!w) break;
      w->wait_kind = W_NONE;
      mutex_acquire_for(w, w->wait_mutex);
    }
    if (!first_waiter(W_CVAR, key, 0)) __atomic_store_n(u32p(key), 0, __ATOMIC_SEQ_CST);
    pthread_mutex_unlock(&klock);
    return;
  }
  case 0x1F: {  /* ConnectToNamedPort(_, name) -> session */
    uint32_t h = 0;
    x[0] = ipc_connect_port(gstr(x[1]), &h);
    x[1] = h;
    return;
  }
  case 0x21:  /* SendSyncRequest(session): message in the TLR */
    x[0] = ipc_request((uint32_t)x[0], self->tlr);
    return;
  case 0x22:  /* SendSyncRequestWithUserBuffer(buf, size, session) */
    x[0] = ipc_request((uint32_t)x[2], x[0]);
    return;
  case 0x1E:  /* GetSystemTick */
    x[0] = a64_cntpct();
    return;
  case 0x25: {  /* GetThreadId(handle) -> id */
    HostThread *t = thread_of((uint32_t)x[1]);
    x[0] = t ? RES_OK : R_INVALID_HANDLE; x[1] = t ? t->id : 0;
    return;
  }
  case 0x26:  /* Break(reason, arg, size) */
    HDIE("svcBreak reason %#llx arg %#llx size %#llx at %#llx\n", (unsigned long long)x[0],
         (unsigned long long)x[1], (unsigned long long)x[2], (unsigned long long)pc);
  case 0x27:  /* OutputDebugString(str, size) */
    HLOG("[debug] %.*s\n", (int)x[1], gstr(x[0]));
    x[0] = RES_OK;
    return;
  case 0x29: {  /* GetInfo(_, type, handle, subtype) -> info */
    uint32_t r;
    uint64_t v = get_info((uint32_t)x[1], x[3], &r);
    x[0] = r; x[1] = v;
    return;
  }
  case 0x2C: case 0x2D:  /* MapPhysicalMemory / UnmapPhysicalMemory(addr, size) */
    if (imm == 0x2C) memset(gptr(x[0]), 0, x[1]);
    x[0] = RES_OK;
    return;
  case 0x34: {  /* WaitForAddress(addr, type, value, timeout) */
    uint64_t addr = x[0];
    uint32_t type = (uint32_t)x[1];
    int32_t value = (int32_t)x[2];
    int64_t timeout = (int64_t)x[3];
    pthread_mutex_lock(&klock);
    int32_t v = (int32_t)__atomic_load_n(u32p(addr), __ATOMIC_SEQ_CST);
    uint32_t r = RES_OK;
    int wait = 0;
    if (type == 0) wait = v < value;
    else if (type == 1) { wait = v < value; if (wait) __atomic_store_n(u32p(addr), (uint32_t)(v - 1), __ATOMIC_SEQ_CST); }
    else if (type == 2) wait = v == value;
    else r = R_INVALID_ENUM;
    if (!r && !wait) r = R_INVALID_STATE;
    if (!r) {
      if (timeout == 0) r = R_TIMED_OUT;
      else {
        self->wait_kind = W_ADDR; self->wait_addr = addr;
        r = kwait(self, timeout);
        self->wait_kind = W_NONE;
      }
    }
    pthread_mutex_unlock(&klock);
    x[0] = r;
    return;
  }
  case 0x35: {  /* SignalToAddress(addr, type, value, count) */
    uint64_t addr = x[0];
    uint32_t type = (uint32_t)x[1];
    int32_t value = (int32_t)x[2], count = (int32_t)x[3];
    pthread_mutex_lock(&klock);
    uint32_t r = RES_OK;
    if (type == 1 || type == 2) {
      int32_t nv;
      HostThread *w0 = first_waiter(W_ADDR, addr, 0);
      if (type == 1) nv = value + 1;
      else if (count <= 0) nv = w0 ? value - 2 : value + 1;
      else if (!w0) nv = value + 1;
      else {
        int others = 0;
        for (HostThread *t = all_threads; t; t = t->next)
          if (t != w0 && t->wait_kind == W_ADDR && t->wait_addr == addr) others++;
        nv = others < count ? value - 1 : value;
      }
      uint32_t expect = (uint32_t)value;
      if (!__atomic_compare_exchange_n(u32p(addr), &expect, (uint32_t)nv, 0, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST))
        r = R_INVALID_STATE;
    } else if (type != 0) r = R_INVALID_ENUM;
    if (!r)
      for (int i = 0; count <= 0 || i < count; i++) {
        HostThread *w = first_waiter(W_ADDR, addr, 0);
        if (!w) break;
        kwake(w);
      }
    pthread_mutex_unlock(&klock);
    x[0] = r;
    return;
  }
  case 0x13: case 0x14: {  /* MapSharedMemory / UnmapSharedMemory(handle, addr, size[, perm]) */
    KSharedMemory *s = kobj_get((uint32_t)x[0], H_SHMEM);
    uint64_t addr = x[1], size = x[2];
    if (!s) { x[0] = R_INVALID_HANDLE; return; }
    if (size > s->size) { x[0] = R_INVALID_SIZE; return; }
    pthread_mutex_lock(&klock);
    uint32_t res = RES_OK;
    if (imm == 0x13) {
      if (s->guest) res = R_INVALID_STATE;  /* second simultaneous mapping: unsupported */
      else { memcpy(gptr(addr), s->backing, size); s->guest = addr; }
    } else {
      if (s->guest != addr) res = R_INVALID_STATE;
      else { memcpy(s->backing, gptr(addr), size); memset(gptr(addr), 0, size); s->guest = 0; }
    }
    pthread_mutex_unlock(&klock);
    if (res == R_INVALID_STATE) HLOG("[svc] %sSharedMemory %#llx at %#llx: already %smapped\n", imm == 0x13 ? "Map" : "Unmap",
                                     (unsigned long long)x[0], (unsigned long long)addr, imm == 0x13 ? "" : "un");
    else if (imm == 0x13) mapping_add(addr, size, MEM_SHARED);
    else mapping_remove(addr);
    x[0] = res;
    return;
  }
  case 0x50: {  /* CreateSharedMemory(_, size, local perm, remote perm) -> handle */
    uint32_t h;
    kshmem_create(x[1], &h);
    x[0] = RES_OK; x[1] = h;
    return;
  }
  case 0x45: {  /* CreateEvent -> writable, readable */
    uint32_t w, r;
    kevent_create(&r, &w);
    x[0] = RES_OK; x[1] = w; x[2] = r;
    return;
  }
  default:
    HDIE("unimplemented svc %#x at %#llx (x0=%#llx x1=%#llx x2=%#llx x3=%#llx)\n", imm, (unsigned long long)pc,
         (unsigned long long)x[0], (unsigned long long)x[1], (unsigned long long)x[2], (unsigned long long)x[3]);
  }
}

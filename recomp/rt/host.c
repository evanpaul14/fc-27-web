/* Loader and dispatcher for the recompiled program (stands in for rtld). */
#include "host.h"
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

uint8_t *mem;
int host_trace;
__thread HostThread *cur_thread;

/* ---- guest allocations made by the host ---- */
static uint64_t alloc_next = ALLOC_BASE;
static pthread_mutex_t alloc_lock = PTHREAD_MUTEX_INITIALIZER;
uint64_t guest_alloc(uint64_t size, uint64_t align) {
  pthread_mutex_lock(&alloc_lock);
  uint64_t a = (alloc_next + align - 1) & ~(align - 1);
  if (a + size > ALLOC_END) HDIE("guest_alloc: out of host allocation space\n");
  alloc_next = a + size;
  pthread_mutex_unlock(&alloc_lock);
  memset(gptr(a), 0, size);
  return a;
}

/* ---- host thunks ---- */
static struct { A64Func fn; const char *name; } thunks[(HOST_THUNK_END - HOST_THUNK_BASE) / 16];
static unsigned n_thunks;
uint64_t host_thunk(A64Func fn, const char *name) {
  thunks[n_thunks].fn = fn;
  thunks[n_thunks].name = name;
  return HOST_THUNK_BASE + 16 * n_thunks++;
}

/* ---- symbols for diagnostics (build/symbols.tsv) ---- */
static struct HSym { uint64_t a; char *name; } *hsyms;
static size_t n_hsyms;
static int sym_cmp(const void *x, const void *y) {
  uint64_t a = ((const struct HSym *)x)->a, b = ((const struct HSym *)y)->a;
  return a < b ? -1 : a > b;
}
static void load_symbols(const char *path) {
  FILE *f = fopen(path, "r");
  if (!f) return;
  char line[4096];
  size_t cap = 0;
  while (fgets(line, sizeof line, f)) {
    char *t1 = strchr(line, '\t'), *t2 = t1 ? strchr(t1 + 1, '\t') : 0, *t3 = t2 ? strchr(t2 + 1, '\t') : 0;
    if (!t3) continue;
    *t3 = 0;
    if (n_hsyms == cap) hsyms = realloc(hsyms, (cap = cap ? cap * 2 : 65536) * sizeof *hsyms);
    hsyms[n_hsyms].a = strtoull(line, 0, 16);
    hsyms[n_hsyms++].name = strdup(t2 + 1);
  }
  fclose(f);
  qsort(hsyms, n_hsyms, sizeof *hsyms, sym_cmp);
}
const char *host_symbol(uint64_t addr, uint64_t *off) {
  size_t lo = 0, hi = n_hsyms;
  while (lo < hi) { size_t m = (lo + hi) / 2; if (hsyms[m].a <= addr) lo = m + 1; else hi = m; }
  if (!lo) return 0;
  *off = addr - hsyms[lo - 1].a;
  return hsyms[lo - 1].name;
}

/* ---- dispatch ---- */
static const A64Fn *find_fn(uint64_t t) {
  unsigned lo = 0, hi = a64_fn_count;
  while (lo < hi) {
    unsigned m = (lo + hi) / 2;
    if (a64_fn_table[m].addr < t) lo = m + 1; else hi = m;
  }
  return lo < a64_fn_count && a64_fn_table[lo].addr == t ? &a64_fn_table[lo] : 0;
}

void a64_call(CPU *c, uint64_t t) {
  const A64Fn *f = find_fn(t);
  if (f) { f->fn(c); return; }
  if (t >= HOST_THUNK_BASE && t < HOST_THUNK_BASE + 16 * n_thunks && !(t & 15)) {
    thunks[(t - HOST_THUNK_BASE) / 16].fn(c);
    return;
  }
  if (t >= MISSING_BASE && t < MISSING_END) {
    for (unsigned i = 0; i < a64_missing_count; i++)
      if (a64_missing[i].addr == t) HDIE("call to unresolved import %s (lr %#llx)\n", a64_missing[i].name, (unsigned long long)c->x[30]);
  }
  uint64_t off = 0;
  const char *s = host_symbol(t, &off);
  FILE *f2 = fopen("build/missing_functions.txt", "a");
  if (f2) { fprintf(f2, "%#llx\n", (unsigned long long)t); fclose(f2); }
  HDIE("no translated function at %#llx (%s+%#llx), lr %#llx\n", (unsigned long long)t, s ? s : "?",
       (unsigned long long)off, (unsigned long long)c->x[30]);
}

void a64_bad_jump(CPU *c, uint64_t pc, uint64_t t) {
  HDIE("br at %#llx to %#llx is outside its jump table (lr %#llx)\n", (unsigned long long)pc,
       (unsigned long long)t, (unsigned long long)c->x[30]);
}

void a64_trap(CPU *c, uint64_t pc, uint32_t word) {
  uint64_t off = 0;
  const char *s = host_symbol(pc, &off);
  HDIE("trap at %#llx (%s+%#llx) word %#x, lr %#llx\n", (unsigned long long)pc, s ? s : "?",
       (unsigned long long)off, word, (unsigned long long)c->x[30]);
}

void a64_svc(CPU *c, uint32_t imm, uint64_t pc) { svc_dispatch(c, imm, pc); }

void host_backtrace(void) {
  if (!cur_thread) return;
  CPU *c = &cur_thread->cpu;
  /* Walk the guest frame-pointer chain (x29). */
  uint64_t fp = c->x[29], lr = c->x[30], off = 0;
  for (int i = 0; i < 24; i++) {
    const char *s = host_symbol(lr, &off);
    HLOG("  #%d %#llx %s+%#llx\n", i, (unsigned long long)lr, s ? s : "?", (unsigned long long)off);
    if (!fp || fp >= GUEST_SPACE - 16) break;
    lr = *(uint64_t *)gptr(fp + 8);
    fp = *(uint64_t *)gptr(fp);
  }
}

/* ---- host crashes: report where, in host and guest terms ---- */
static void crash_handler(int sig, siginfo_t *si, void *uc_) {
  ucontext_t *uc = uc_;
  uint64_t pc = uc->uc_mcontext->__ss.__pc, sp = uc->uc_mcontext->__ss.__sp;
  Dl_info di = {0};
  dladdr((void *)pc, &di);
  uint64_t fault = (uint64_t)si->si_addr;
  HLOG("fatal: signal %d at host pc %#llx (%s+%#llx), sp %#llx, fault %#llx (guest %#llx)\n", sig,
       (unsigned long long)pc, di.dli_sname ? di.dli_sname : "?",
       (unsigned long long)(pc - (uint64_t)di.dli_saddr), (unsigned long long)sp, (unsigned long long)fault,
       (unsigned long long)(fault - (uint64_t)mem));
  if (cur_thread)
    for (int i = 0; i < 31; i++)
      HLOG("  x%-2d %016llx%s", i, (unsigned long long)cur_thread->cpu.x[i], i % 4 == 3 || i == 30 ? "\n" : "");
  host_backtrace();
  signal(SIGABRT, SIG_DFL);
  abort();
}

/* Per thread: an alternate signal stack so stack overflows are reported too. */
void host_thread_signals(void) {
  stack_t ss = {.ss_sp = malloc(1 << 16), .ss_size = 1 << 16};
  sigaltstack(&ss, 0);
}

static void install_crash_handler(void) {
  struct sigaction sa = {0};
  sa.sa_sigaction = crash_handler;
  sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
  sigaction(SIGSEGV, &sa, 0);
  sigaction(SIGBUS, &sa, 0);
}

/* ---- rtld stand-in ---- */
static void rtld_set_initialized(CPU *c) { (void)c; }
static void rtld_run_inits(CPU *c) {
  /* Dependencies first: nnSdk before main. Inits follow the AAPCS, so the
   * caller's callee-saved registers survive; x30 is restored for the return. */
  uint64_t lr = c->x[30];
  for (int i = (int)host_module_count - 1; i >= 0; i--) {
    if (!host_modules[i].init) continue;
    if (host_trace) HLOG("[rtld] init %s @ %#llx\n", host_modules[i].name, (unsigned long long)host_modules[i].init);
    a64_call(c, host_modules[i].init);
  }
  c->x[30] = lr;
}
static void rtld_run_finis(CPU *c) { (void)c; }

static void load_module(const HostModule *m) {
  char path[512];
  snprintf(path, sizeof path, "build/%s.img", m->name);
  int fd = open(path, O_RDONLY);
  if (fd < 0) HDIE("cannot open %s\n", path);
  off_t len = lseek(fd, 0, SEEK_END);
  if ((uint64_t)len > m->size) HDIE("%s larger than module size\n", path);
  if (pread(fd, gptr(m->base), len, 0) != len) HDIE("short read on %s\n", path);
  close(fd);
}

HostThread *thread_create_context(uint64_t stack_size);

static void *main_thread(void *p) {
  cur_thread = p;
  host_thread_signals();
  HLOG("[host] calling nn::init::Start\n");
  a64_call(&cur_thread->cpu, host_nn_init_start);
  HLOG("[host] nn::init::Start returned\n");
  return 0;
}

int main(int argc, char **argv) {
  (void)argc; (void)argv;
  host_trace = getenv("FIFA_TRACE") != 0;
  install_crash_handler();
  mem = mmap(0, GUEST_SPACE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0);
  if (mem == MAP_FAILED) HDIE("cannot reserve guest space\n");
  for (unsigned i = 0; i < host_module_count; i++) load_module(&host_modules[i]);
  load_symbols("build/symbols.tsv");

  HostThread *t = thread_create_context(16 << 20);
  CPU *c = &t->cpu;
  c->x[0] = t->handle;
  c->x[1] = guest_alloc(0x1000, 0x1000);  /* rtld's module list block */
  c->x[2] = host_thunk(rtld_set_initialized, "rtld_set_initialized");
  c->x[3] = host_thunk(rtld_run_inits, "rtld_run_inits");
  c->x[4] = host_thunk(rtld_run_finis, "rtld_run_finis");
  c->x[30] = 0;
  /* Guest calls nest as host calls, so give the main thread a deep stack. */
  pthread_attr_t a;
  pthread_attr_init(&a);
  pthread_attr_setstacksize(&a, 512 << 20);
  pthread_t pt;
  pthread_create(&pt, &a, main_thread, t);
  pthread_join(pt, 0);
  return 0;
}

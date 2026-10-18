/* NVN entry points: nvnBootstrapLoader and nvnDeviceGetProcAddress hand out a
 * host thunk per name. Names without an implementation get a thunk that stops
 * with the name when it is first called, so the log shows what to do next. */
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

#include "../hle/hle.h"
#include "nvn.h"

static void nvn_unimplemented(CPU *c) {
  HDIE("nvn: %s is not implemented (lr %#llx)\n", host_thunk_name, (unsigned long long)c->x[30]);
}

static int impl_cmp(const void *k, const void *e) { return strcmp(k, ((const NvnImpl *)e)->name); }

/* name -> thunk, so every lookup of a name gets the same address */
static struct { char *name; uint64_t addr; } *procs;
static unsigned n_procs, cap_procs;
static pthread_mutex_t procs_lock = PTHREAD_MUTEX_INITIALIZER;

static uint64_t nvn_proc(const char *name) {
  if (!name || !*name) return 0;
  pthread_mutex_lock(&procs_lock);
  uint64_t a = 0;
  for (unsigned i = 0; i < n_procs && !a; i++)
    if (!strcmp(procs[i].name, name)) a = procs[i].addr;
  if (!a) {
    const NvnImpl *impl = bsearch(name, nvn_impls, nvn_impl_count, sizeof *nvn_impls, impl_cmp);
    if (n_procs == cap_procs) procs = realloc(procs, (cap_procs = cap_procs ? cap_procs * 2 : 1024) * sizeof *procs);
    procs[n_procs].name = strdup(name);
    a = procs[n_procs].addr = host_thunk(impl ? impl->fn : nvn_unimplemented, procs[n_procs].name);
    n_procs++;
    if (host_trace && !impl) HLOG("[nvn] %s: no implementation\n", name);
  }
  pthread_mutex_unlock(&procs_lock);
  return a;
}

/* PFNNVNGENERICFUNCPTRPROC nvnBootstrapLoader(const char *name) */
HLE(nvnBootstrapLoader) { RET(nvn_proc(gstr(ARG(0)))); }

/* PFNNVNGENERICFUNCPTRPROC nvnDeviceGetProcAddress(const NVNdevice *device, const char *name) */
NVN(nvnDeviceGetProcAddress) { RET(nvn_proc(gstr(A(1)))); }

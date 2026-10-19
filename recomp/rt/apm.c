/* Performance management (apm): apm -> ISession. Command ids per switchbrew
 * "PPC services". */
#include "ipc.h"
#include <pthread.h>

enum { PERF_MODE_NORMAL, PERF_MODE_BOOST };

static pthread_mutex_t apm_lock = PTHREAD_MUTEX_INITIALIZER;
/* PerformanceConfiguration per PerformanceMode; the firmware defaults are
 * 0x00010000 (CPU 1020 MHz, GPU 384 MHz) handheld and 0x00020003 (GPU 768) docked. */
static uint32_t configs[2] = {0x00010000, 0x00020003};

static void unhandled(const char *iface, IpcMsg *m) {
  HLOG("[apm] %s cmd %u unhandled -> success (in %u bytes)\n", iface, m->cmd, m->in_size);
}

/* ---- ISession ---- */
static void session(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0: {  /* SetPerformanceConfiguration(mode, config) */
    uint32_t mode = IN(uint32_t, 0), config = IN(uint32_t, 4);
    if (host_trace) HLOG("[apm] SetPerformanceConfiguration(%u, %#x)\n", mode, config);
    pthread_mutex_lock(&apm_lock);
    if (mode < 2) configs[mode] = config;
    pthread_mutex_unlock(&apm_lock);
    return;
  }
  case 1: {  /* GetPerformanceConfiguration(mode) -> config */
    uint32_t mode = IN(uint32_t, 0);
    pthread_mutex_lock(&apm_lock);
    SET_OUT(uint32_t, 0, mode < 2 ? configs[mode] : 0);
    pthread_mutex_unlock(&apm_lock);
    return;
  }
  case 2: return;  /* SetCpuOverclockEnabled */
  default: unhandled("ISession", m);
  }
}
static const Service session_svc = {"apm:ISession", session};

/* ---- apm ---- */
static void apm(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0: ipc_push_object(m, &session_svc, 0); return;  /* OpenSession */
  case 1: SET_OUT(uint32_t, 0, host_docked() ? PERF_MODE_BOOST : PERF_MODE_NORMAL); return; /* as am reports */
  case 6: SET_OUT(uint8_t, 0, 1); return;                /* IsEnoughPowerSupplied */
  default: unhandled("apm", m);
  }
}
const Service apm_service = {"apm", apm};

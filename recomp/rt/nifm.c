/* Network interface manager (nifm:u, nifm:a, nifm:s) -> IGeneralService: always
 * offline. Requests are never accepted and report NetworkCommunicationDisabled,
 * like a console in airplane mode. Command ids per switchbrew "Network Interface
 * services". */
#include "ipc.h"
#include <stdlib.h>

#define R_NIFM_DISABLED 0x8AE6Eu  /* NIFM 1111: network communication disabled (2110-1111) */

enum { REQ_NOT_SUBMITTED = 1 };

static void unhandled(const char *iface, IpcMsg *m) {
  HLOG("[nifm] %s cmd %u unhandled -> success (in %u bytes)\n", iface, m->cmd, m->in_size);
}

static void copy_event(IpcMsg *m, KEvent *e) { m->copy_out[m->ncopy_out++] = kobj_new(H_EVENT_R, e); }

/* ---- IRequest / IScanRequest ---- */
typedef struct { KEvent *event, *event2; } Request;

static Request *request_new(void) {
  Request *r = calloc(1, sizeof *r);
  uint32_t h;
  r->event = kevent_create(&h, 0);
  r->event2 = kevent_create(&h, 0);
  return r;
}

static void request(void *state, IpcMsg *m) {
  Request *r = state;
  switch (m->cmd) {
  case 0: SET_OUT(uint32_t, 0, REQ_NOT_SUBMITTED); return;  /* GetRequestState */
  case 1: m->result = R_NIFM_DISABLED; return;               /* GetResult */
  case 2: copy_event(m, r->event); copy_event(m, r->event2); return;  /* GetSystemEventReadableHandles */
  case 4: kevent_signal(r->event); return;  /* Submit: resolved (rejected) at once */
  case 3:                                   /* Cancel */
  case 5: case 6: case 8: case 9: case 10: case 11: case 12: case 13: case 14: case 15: case 16: case 17:
  case 18: case 23: case 24: case 25:       /* Set*, Register/UnregisterSocketDescriptor */
    return;
  case 21: m->result = R_NIFM_DISABLED; return;  /* GetAppletInfo */
  default: unhandled("IRequest", m);
  }
}
static void request_close(void *state) { free(state); }
static const Service request_svc = {"IRequest", request, request_close};

static void scan_request(void *state, IpcMsg *m) {
  Request *r = state;
  switch (m->cmd) {
  case 0: kevent_signal(r->event); return;   /* Submit */
  case 1: SET_OUT(uint8_t, 0, 0); return;    /* IsProcessing */
  case 2: m->result = R_NIFM_DISABLED; return;  /* GetResult */
  case 3: copy_event(m, r->event); return;   /* GetSystemEventReadableHandle */
  default: unhandled("IScanRequest", m);
  }
}
static const Service scan_request_svc = {"IScanRequest", scan_request, request_close};

/* ---- IGeneralService ---- */
static void general_service(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 1:  /* GetClientId -> C ClientId (u32) */
    if (m->nc && m->c[0].size >= 4) *(uint32_t *)gptr(m->c[0].addr) = 1;
    return;
  case 2: ipc_push_object(m, &scan_request_svc, request_new()); return;  /* CreateScanRequest */
  case 4: ipc_push_object(m, &request_svc, request_new()); return;       /* CreateRequest(u32 preset) */
  case 5:  /* GetCurrentNetworkProfile */
  case 12: /* GetCurrentIpAddress */
  case 15: /* GetCurrentIpConfigInfo */
  case 18: /* GetInternetConnectionStatus */
    m->result = R_NIFM_DISABLED;
    return;
  case 6: case 7: SET_OUT(int32_t, 0, 0); return;  /* EnumerateNetworkInterfaces/Profiles: none */
  case 16: return;                                 /* SetWirelessCommunicationEnabled */
  case 17: SET_OUT(uint8_t, 0, 0); return;         /* IsWirelessCommunicationEnabled */
  case 21: SET_OUT(uint8_t, 0, 0); return;         /* IsEthernetCommunicationEnabled */
  case 22: SET_OUT(uint8_t, 0, 0); return;         /* IsAnyInternetRequestAccepted */
  case 23: SET_OUT(uint8_t, 0, 0); return;         /* IsAnyForegroundRequestAccepted */
  case 24: case 25: return;                        /* PutToSleep, WakeUp */
  default: unhandled("IGeneralService", m);
  }
}
static const Service general_service_svc = {"IGeneralService", general_service};

/* ---- nifm:u, nifm:a, nifm:s (IStaticService) ---- */
static void nifm(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 4:  /* CreateGeneralServiceOld */
  case 5:  /* CreateGeneralService(u64, pid) */
    ipc_push_object(m, &general_service_svc, 0);
    return;
  default: unhandled("IStaticService", m);
  }
}
const Service nifm_u_service = {"nifm:u", nifm}, nifm_a_service = {"nifm:a", nifm}, nifm_s_service = {"nifm:s", nifm};

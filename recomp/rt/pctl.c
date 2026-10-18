/* Parental controls (pctl, pctl:s, pctl:a, pctl:r) -> IParentalControlService:
 * no restrictions, no pairing, no play timer. Command ids per switchbrew
 * "Parental Control services". */
#include "ipc.h"

static KEvent *sync_event, *suspension_event, *unlinked_event;

static void pctl_init(void) {
  static int done;
  if (done) return;
  done = 1;
  uint32_t r;
  sync_event = kevent_create(&r, 0);
  suspension_event = kevent_create(&r, 0);
  unlinked_event = kevent_create(&r, 0);
}

static void copy_event(IpcMsg *m, KEvent *e) { m->copy_out[m->ncopy_out++] = kobj_new(H_EVENT_R, e); }

static void zero_out(IpcMsg *m, uint32_t n) {
  memset(m->out, 0, n);
  out_size(m, n);
}

/* ---- IParentalControlService ---- */
static void parental_control_service(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 1001: case 1002: case 1003: case 1004: case 1005: case 1006: case 1007: case 1013: case 1017:
    /* Check{FreeCommunication,Application}Permission and friends, ConfirmStereoVisionPermission,
     * EndFreeCommunication: everything is permitted */
    return;
  case 1031: SET_OUT(uint8_t, 0, 0); return;  /* IsRestrictionEnabled */
  case 1032: SET_OUT(uint32_t, 0, 0); return; /* GetSafetyLevel: none */
  case 1035: zero_out(m, 3); return;          /* GetCurrentSettings */
  case 1039: SET_OUT(int32_t, 0, 0); return;  /* GetFreeCommunicationApplicationListCount */
  case 1403: SET_OUT(uint8_t, 0, 0); return;  /* IsPairingActive */
  case 1406: zero_out(m, 0x10); return;       /* GetSettingsLastUpdated */
  case 1432: copy_event(m, sync_event); return;        /* GetSynchronizationEvent */
  case 1456: zero_out(m, 0x34); return;                /* GetPlayTimerSettings */
  case 1457: copy_event(m, suspension_event); return;  /* GetPlayTimerEventToRequestSuspension */
  case 1458: SET_OUT(uint8_t, 0, 0); return;           /* IsPlayTimerAlarmDisabled */
  case 1471: case 1472: return;                        /* NotifyWrongPinCodeInputManyTimes, CancelNetworkRequest */
  case 1473: copy_event(m, unlinked_event); return;    /* GetUnlinkedEvent */
  case 1:     /* Initialize */
  case 1451:  /* StartPlayTimer */
  case 1452:  /* StopPlayTimer */
    return;
  default:
    HLOG("[pctl] IParentalControlService cmd %u unhandled -> success (in %u bytes)\n", m->cmd, m->in_size);
  }
}
static const Service parental_control_service_svc = {"IParentalControlService", parental_control_service};

/* ---- pctl, pctl:s, pctl:a, pctl:r ---- */
static void pctl(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0:  /* CreateService(u64, pid) */
  case 1:  /* CreateServiceWithoutInitialize(u64, pid) */
    pctl_init();
    ipc_push_object(m, &parental_control_service_svc, 0);
    return;
  default: HLOG("[pctl] cmd %u unhandled -> success (in %u bytes)\n", m->cmd, m->in_size);
  }
}
const Service pctl_service = {"pctl", pctl}, pctl_s_service = {"pctl:s", pctl}, pctl_a_service = {"pctl:a", pctl},
              pctl_r_service = {"pctl:r", pctl};

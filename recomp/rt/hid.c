/* Human interface devices (hid): IHidServer, IAppletResource and
 * IActiveVibrationDeviceList. Command ids per switchbrew "HID services".
 * nnSdk reads input straight from the applet resource's shared memory; it
 * stays zeroed for now, which reads as no controllers attached. */
#include "ipc.h"

#define HID_SHMEM_SIZE 0x40000

static KSharedMemory *shmem;
static KEvent *style_event;

static void unhandled(const char *iface, IpcMsg *m) {
  if (host_trace) HLOG("[hid] %s cmd %u -> success (in %u bytes)\n", iface, m->cmd, m->in_size);
}

/* ---- IAppletResource ---- */
static void applet_resource(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0:  /* GetSharedMemoryHandle */
    if (!shmem) shmem = kshmem_create(HID_SHMEM_SIZE, 0);
    m->copy_out[m->ncopy_out++] = kobj_new(H_SHMEM, shmem);
    return;
  default: unhandled("IAppletResource", m);
  }
}
static const Service applet_resource_svc = {"hid:IAppletResource", applet_resource};

/* ---- IActiveVibrationDeviceList ---- */
static void vibration_list(void *state, IpcMsg *m) {
  (void)state;
  unhandled("IActiveVibrationDeviceList", m);  /* 0 ActivateVibrationDevice */
}
static const Service vibration_list_svc = {"hid:IActiveVibrationDeviceList", vibration_list};

/* ---- IHidServer ---- */
static void hid(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0:  /* CreateAppletResource(aruid) */
    ipc_push_object(m, &applet_resource_svc, 0);
    return;
  case 106: {  /* AcquireNpadStyleSetUpdateEventHandle(aruid, npad id, unknown) */
    uint32_t r;
    if (!style_event) style_event = kevent_create(&r, 0);
    m->copy_out[m->ncopy_out++] = kobj_new(H_EVENT_R, style_event);
    return;
  }
  case 203:  /* CreateActiveVibrationDeviceList */
    ipc_push_object(m, &vibration_list_svc, 0);
    return;
  default: unhandled("IHidServer", m);
  }
}
const Service hid_service = {"hid", hid};

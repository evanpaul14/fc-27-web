/* Applet manager (am): appletOE -> IApplicationProxy and its sub-interfaces.
 * Command ids per switchbrew "Applet Manager services". */
#include "ipc.h"
#include <pthread.h>
#include <stdlib.h>

#define R_AM_NO_DATA 0x480u      /* AM 2: channel empty (PopLaunchParameter) */
#define R_AM_NO_MESSAGES 0x680u  /* AM 3: message queue empty */

enum { MSG_FOCUS_STATE_CHANGED = 15, MSG_OPERATION_MODE_CHANGED = 30, MSG_PERFORMANCE_MODE_CHANGED = 31 };
enum { FOCUS_IN_FOCUS = 1 };

static pthread_mutex_t am_lock = PTHREAD_MUTEX_INITIALIZER;
static uint32_t msgs[64];
static int nmsgs;
static KEvent *msg_event, *gpu_error_event, *suspended_tick_event, *launchable_event, *resolution_event,
    *friend_invitation_event, *notification_event, *health_warning_event, *mode_event;

static void am_init(void) {
  static int done;
  if (done) return;
  done = 1;
  uint32_t r;
  KEvent **evs[] = {&msg_event, &gpu_error_event, &suspended_tick_event, &launchable_event, &resolution_event,
                    &friend_invitation_event, &notification_event, &health_warning_event, &mode_event};
  for (unsigned i = 0; i < sizeof evs / sizeof *evs; i++) *evs[i] = kevent_create(&r, 0);
  /* what a freshly launched application sees */
  msgs[nmsgs++] = MSG_FOCUS_STATE_CHANGED;
  msgs[nmsgs++] = MSG_OPERATION_MODE_CHANGED;
  msgs[nmsgs++] = MSG_PERFORMANCE_MODE_CHANGED;
  kevent_signal(msg_event);
}

/* Copy handle to an existing event: each copy gets its own handle so the
 * client can close it independently. */
static void copy_event(IpcMsg *m, KEvent *e) { m->copy_out[m->ncopy_out++] = kobj_new(H_EVENT_R, e); }

static void unhandled(const char *iface, IpcMsg *m) {
  HLOG("[am] %s cmd %u unhandled -> success (in %u bytes)\n", iface, m->cmd, m->in_size);
}

/* ---- ICommonStateGetter ---- */
static void common_state_getter(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0: copy_event(m, msg_event); return;  /* GetEventHandle */
  case 1:                                     /* ReceiveMessage */
    pthread_mutex_lock(&am_lock);
    if (!nmsgs) m->result = R_AM_NO_MESSAGES;
    else {
      SET_OUT(uint32_t, 0, msgs[0]);
      memmove(msgs, msgs + 1, --nmsgs * sizeof *msgs);
      if (!nmsgs) kevent_clear(msg_event);
    }
    pthread_mutex_unlock(&am_lock);
    return;
  case 5: SET_OUT(uint8_t, 0, host_docked()); return;   /* GetOperationMode: 0 handheld, 1 console (docked) */
  case 6: SET_OUT(uint32_t, 0, host_docked()); return;  /* GetPerformanceMode: 0 normal, 1 boost */
  case 8: SET_OUT(uint8_t, 0, 0); return;               /* GetBootMode: normal */
  case 9: SET_OUT(uint8_t, 0, FOCUS_IN_FOCUS); return;  /* GetCurrentFocusState */
  case 50: SET_OUT(uint8_t, 0, 0); return;              /* IsVrModeEnabled */
  case 60:                                              /* GetDefaultDisplayResolution */
    SET_OUT(int32_t, 0, host_docked() ? 1920 : 1280);
    SET_OUT(int32_t, 4, host_docked() ? 1080 : 720);
    return;
  case 61: copy_event(m, resolution_event); return;  /* GetDefaultDisplayResolutionChangeEvent */
  case 300: SET_OUT(uint8_t, 0, 1); return;          /* GetSettingsPlatformRegion: global */
  case 10: case 51: case 52: case 66: case 900:
    /* RequestToAcquireSleepLock, SetVrModeEnabled, SetLcdBacklighOffEnabled, SetCpuBoostMode,
     * SetRequestExitToLibraryAppletAtExecuteNextProgramEnabled */
    return;
  default: unhandled("ICommonStateGetter", m);
  }
}
static const Service common_state_getter_svc = {"ICommonStateGetter", common_state_getter};

/* ---- ISelfController ---- */
static void self_controller(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0:  /* Exit */
    HLOG("[am] application requested exit\n");
    exit(0);
  case 9: copy_event(m, launchable_event); return;  /* GetLibraryAppletLaunchableEvent */
  case 40: SET_OUT(uint64_t, 0, 1); return;          /* CreateManagedDisplayLayer -> layer id */
  case 63: SET_OUT(uint32_t, 0, 0); return;          /* GetIdleTimeDetectionExtension */
  case 69: SET_OUT(uint8_t, 0, 0); return;           /* IsAutoSleepDisabled */
  case 90:                                           /* GetAccumulatedSuspendedTimeInfo */
    memset(m->out, 0, 0x18);
    out_size(m, 0x18);
    return;
  case 91: copy_event(m, suspended_tick_event); return;  /* GetAccumulatedSuspendedTickChangedEvent */
  case 1: case 2: case 10: case 11: case 12: case 13: case 14: case 16: case 19: case 50: case 62: case 65:
  case 68: case 100: case 110: case 130:
    /* LockExit, UnlockExit, SetScreenShotPermission, SetOperationModeChangedNotification,
     * SetPerformanceModeChangedNotification, SetFocusHandlingMode, SetRestartMessageEnabled,
     * SetOutOfFocusSuspendingEnabled, SetAlbumImageOrientation, SetHandlesRequestToDisplay,
     * SetIdleTimeDetectionExtension, ReportUserIsActive, SetAutoSleepDisabled,
     * SetAlbumImageTakenNotificationEnabled, SetApplicationAlbumUserData, SetRecordVolumeMuted */
    return;
  default: unhandled("ISelfController", m);
  }
}
static const Service self_controller_svc = {"ISelfController", self_controller};

/* ---- IWindowController ---- */
static void window_controller(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 1: SET_OUT(uint64_t, 0, APP_PID); return;  /* GetAppletResourceUserId */
  case 2: SET_OUT(uint64_t, 0, 0); return;        /* GetAppletResourceUserIdOfCallerApplet */
  case 10: case 11: case 12: case 20: case 21:
    /* AcquireForegroundRights, ReleaseForegroundRights, RejectToChangeIntoBackground,
     * SetAppletWindowVisibility, SetAppletGpuTimeSlice */
    return;
  default: unhandled("IWindowController", m);
  }
}
static const Service window_controller_svc = {"IWindowController", window_controller};

/* ---- IAudioController ---- */
static void audio_controller(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 1: case 2: SET_OUT(float, 0, 1.0f); return;  /* Get{Main,Library}AppletExpectedMasterVolume */
  case 0: case 3: case 4: return;                   /* SetExpectedMasterVolume, ChangeMainAppletMasterVolume,
                                                       SetTransparentVolumeRate */
  default: unhandled("IAudioController", m);
  }
}
static const Service audio_controller_svc = {"IAudioController", audio_controller};

/* ---- IDisplayController, ILibraryAppletCreator, IDebugFunctions: nothing needed yet ---- */
static void display_controller(void *state, IpcMsg *m) { (void)state; unhandled("IDisplayController", m); }
static const Service display_controller_svc = {"IDisplayController", display_controller};
static void library_applet_creator(void *state, IpcMsg *m) { (void)state; unhandled("ILibraryAppletCreator", m); }
static const Service library_applet_creator_svc = {"ILibraryAppletCreator", library_applet_creator};
static void debug_functions(void *state, IpcMsg *m) { (void)state; unhandled("IDebugFunctions", m); }
static const Service debug_functions_svc = {"IDebugFunctions", debug_functions};

/* ---- IApplicationFunctions ---- */
static void application_functions(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 1: m->result = R_AM_NO_DATA; return;  /* PopLaunchParameter: none */
  case 20: SET_OUT(uint64_t, 0, 0); return;  /* EnsureSaveData -> required size (0 = exists) */
  case 26: case 28:  /* GetSaveDataSize / GetSaveDataSizeMax(type, uid) -> size, journal size: host directories */
    SET_OUT(int64_t, 0, 1ll << 30);
    SET_OUT(int64_t, 8, 1ll << 30);
    return;
  case 21: {                                 /* GetDesiredLanguage -> LanguageCode */
    uint64_t code = 0;
    memcpy(&code, "en-US", 5);
    SET_OUT(uint64_t, 0, code);
    return;
  }
  case 23:  /* GetDisplayVersion -> char[0x10] */
    memset(m->out, 0, 0x10);
    memcpy(m->out, "1.0.0", 5);
    out_size(m, 0x10);
    return;
  case 40: SET_OUT(uint8_t, 0, 0); return;  /* NotifyRunning -> became busy */
  case 50:                                  /* GetPseudoDeviceId -> u128 */
    memset(m->out, 0, 16);
    memcpy(m->out, "fifa-web-device", 15);
    out_size(m, 16);
    return;
  case 65: SET_OUT(uint8_t, 0, 0); return;    /* IsGamePlayRecordingSupported */
  case 123: SET_OUT(int32_t, 0, -1); return;  /* GetPreviousProgramIndex */
  case 130: copy_event(m, gpu_error_event); return;          /* GetGpuErrorDetectedSystemEvent */
  case 140: copy_event(m, friend_invitation_event); return;  /* GetFriendInvitationStorageChannelEvent */
  case 141: case 151: m->result = R_AM_NO_DATA; return;      /* TryPopFrom{FriendInvitation,Notification}StorageChannel */
  case 150: copy_event(m, notification_event); return;       /* GetNotificationStorageChannelEvent */
  case 160: copy_event(m, health_warning_event); return;     /* GetHealthWarningDisappearedSystemEvent */
  case 180: SET_OUT(uint32_t, 0, 0); return;                  /* GetLaunchRequiredVersion */
  case 210: copy_event(m, mode_event); return;  /* nn::oe's operation mode changed event: never fires (no
                                                 * id on switchbrew; GetOperationMode spins on handle 0 without it) */
  case 200: SET_OUT(uint32_t, 0, 0); return;                  /* GetLastApplicationExitReason */
  case 22: case 30: case 31: case 32: case 33: case 60: case 66: case 67: case 90: case 100: case 101: case 102:
  case 124: case 131: case 170:
    /* SetTerminateResult, Begin/End BlockingHomeButton(ShortAndLongPressed),
     * SetMediaPlaybackStateForApplication, InitializeGamePlayRecording, SetGamePlayRecordingState,
     * EnableApplicationCrashReport, InitializeApplicationCopyrightFrameBuffer,
     * SetApplicationCopyrightImage, SetApplicationCopyrightVisibility,
     * EnableApplicationAllThreadDumpOnCrash, SetDelayTimeToAbortOnGpuError,
     * SetHdcpAuthenticationActivated */
    return;
  default: unhandled("IApplicationFunctions", m);
  }
}
static const Service application_functions_svc = {"IApplicationFunctions", application_functions};

/* ---- IApplicationProxy ---- */
static void application_proxy(void *state, IpcMsg *m) {
  (void)state;
  const Service *sub = 0;
  switch (m->cmd) {
  case 0: sub = &common_state_getter_svc; break;
  case 1: sub = &self_controller_svc; break;
  case 2: sub = &window_controller_svc; break;
  case 3: sub = &audio_controller_svc; break;
  case 4: sub = &display_controller_svc; break;
  case 11: sub = &library_applet_creator_svc; break;
  case 20: sub = &application_functions_svc; break;
  case 1000: sub = &debug_functions_svc; break;
  default: unhandled("IApplicationProxy", m); return;
  }
  ipc_push_object(m, sub, 0);
}
static const Service application_proxy_svc = {"IApplicationProxy", application_proxy};

/* ---- appletOE ---- */
static void applet_oe(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0:  /* OpenApplicationProxy(pid, u64, copy process handle) */
    am_init();
    ipc_push_object(m, &application_proxy_svc, 0);
    return;
  default: unhandled("appletOE", m);
  }
}
const Service applet_oe_service = {"appletOE", applet_oe};

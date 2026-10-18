/* Accounts (acc:u0): IAccountServiceForApplication with one user, IProfile
 * and IManagerForApplication (no Nintendo Account: everything network side
 * reports unavailable). Command ids per switchbrew "Account services". */
#include "ipc.h"

#define USER_NICKNAME "Player"

static const uint8_t user_id[16] = {0x46, 0x49, 0x46, 0x41, 0x57, 0x45, 0x42, 0x01,
                                    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01};

static void unhandled(const char *iface, IpcMsg *m) {
  HLOG("[acc] %s cmd %u unhandled -> success (in %u bytes)\n", iface, m->cmd, m->in_size);
}

static IpcBuf *out_buf(IpcMsg *m) { return m->nb ? &m->b[0] : m->nc ? &m->c[0] : 0; }

static void put_uid(IpcMsg *m) {
  memcpy(m->out, user_id, 16);
  out_size(m, 16);
}

/* ProfileBase (0x38): uid, last edit timestamp, nickname[0x20] */
static void profile_base(uint8_t *p) {
  memset(p, 0, 0x38);
  memcpy(p, user_id, 16);
  *(uint64_t *)(p + 0x10) = 1767225600;  /* 2026-01-01 */
  strncpy((char *)p + 0x18, USER_NICKNAME, 0x1f);
}

/* ---- IProfile ---- */
static void profile(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0: {  /* Get -> ProfileBase, UserData (0x80) in buffer */
    IpcBuf *b = out_buf(m);
    if (b && b->size) memset(gptr(b->addr), 0, b->size < 0x80 ? b->size : 0x80);
  } /* fall through */
  case 1:  /* GetBase */
    profile_base(m->out);
    out_size(m, 0x38);
    return;
  case 10: SET_OUT(uint32_t, 0, 0); return;  /* GetImageSize: no profile picture */
  case 11: SET_OUT(uint32_t, 0, 0); return;  /* LoadImage */
  default: unhandled("IProfile", m);
  }
}
static const Service profile_svc = {"acc:IProfile", profile};

/* ---- IManagerForApplication ---- */
static void manager(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0: m->result = 0x7c7c; return;  /* CheckAvailability: no network service account (124-62) */
  case 1: SET_OUT(uint64_t, 0, 0x4649464157454201ull); return;  /* GetAccountId */
  default: unhandled("IManagerForApplication", m);
  }
}
static const Service manager_svc = {"acc:IManagerForApplication", manager};

/* ---- IAccountServiceForApplication ---- */
static void acc(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0: SET_OUT(int32_t, 0, 1); return;                           /* GetUserCount */
  case 1: SET_OUT(uint8_t, 0, !memcmp(m->in, user_id, 16)); return;  /* GetUserExistence */
  case 2: case 3: {                                                 /* ListAllUsers, ListOpenUsers */
    IpcBuf *b = out_buf(m);
    if (b && b->size) {
      memset(gptr(b->addr), 0, b->size);
      if (b->size >= 16) memcpy(gptr(b->addr), user_id, 16);
    }
    return;
  }
  case 4: put_uid(m); return;                       /* GetLastOpenedUser */
  case 5: ipc_push_object(m, &profile_svc, 0); return;  /* GetProfile(uid) */
  case 50: SET_OUT(uint8_t, 0, 0); return;          /* IsUserRegistrationRequestPermitted */
  case 51: case 52: put_uid(m); return;             /* TrySelectUserWithoutInteraction */
  case 100: case 140: case 160: return;             /* InitializeApplicationInfo(V0/V2) */
  case 101: ipc_push_object(m, &manager_svc, 0); return;  /* GetBaasAccountManagerForApplication(uid) */
  case 110: case 111: return;                       /* Store/ClearSaveDataThumbnail */
  case 150: SET_OUT(uint8_t, 0, 0); return;         /* IsUserAccountSwitchLocked */
  default: unhandled("IAccountServiceForApplication", m);
  }
}
const Service acc_u0_service = {"acc:u0", acc};

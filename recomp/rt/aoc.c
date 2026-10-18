/* Add-on content (aoc:u) -> IAddOnContentManager: no DLC installed.
 * Command ids per switchbrew "NS services". */
#include "ipc.h"

static KEvent *list_changed;

static void copy_event(IpcMsg *m, KEvent *e) { m->copy_out[m->ncopy_out++] = kobj_new(H_EVENT_R, e); }

static void aoc(void *state, IpcMsg *m) {
  (void)state;
  uint32_t r;
  switch (m->cmd) {
  case 2: SET_OUT(uint32_t, 0, 0); return;  /* CountAddOnContent */
  case 3: SET_OUT(uint32_t, 0, 0); return;  /* ListAddOnContent(offset, count) -> count, B indices */
  case 5: SET_OUT(uint64_t, 0, 0x0100C49025D3E000ull + 0x1000); return;  /* GetAddOnContentBaseId */
  case 8: case 10:  /* GetAddOnContentListChangedEvent(WithProcessId) */
    if (!list_changed) list_changed = kevent_create(&r, 0);
    copy_event(m, list_changed);
    return;
  case 7:   /* PrepareAddOnContent */
  case 9:   /* GetAddOnContentLostErrorCode */
  case 11: case 12:  /* Notify(Un)MountAddOnContent */
  case 50:  /* CheckAddOnContentMountStatus */
    return;
  default: HLOG("[aoc] IAddOnContentManager cmd %u unhandled -> success (in %u bytes)\n", m->cmd, m->in_size);
  }
}
const Service aoc_u_service = {"aoc:u", aoc};

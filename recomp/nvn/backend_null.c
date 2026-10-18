/* A renderer backend that draws nothing: command lists are accepted and
 * dropped, presents only count frames. Lets the game run past graphics setup
 * until a real backend is in place. */
#include "nvn.h"

static uint64_t frames;

void backend_init(void) { HLOG("[nvn] null renderer\n"); }
void backend_execute(uint64_t handle) { (void)handle; }
void backend_present(NvnTexture *tex) {
  if (frames++ % 60 == 0) HLOG("[nvn] present #%llu (%ux%u)\n", (unsigned long long)frames, tex ? tex->width : 0, tex ? tex->height : 0);
}
void backend_texture_destroyed(NvnTexture *tex) { (void)tex; }
void backend_pool_written(uint64_t addr, uint64_t size) { (void)addr; (void)size; }
void renderer_sync(void) {}
void renderer_flush(void) {}
uint64_t renderer_fence_seq(void) { return 0; }
void renderer_wait_seq(uint64_t seq) { (void)seq; }

/* nn::vi at the API level: one display (1920x1080 docked, 1280x720 handheld) with one layer. The NVN
 * window gets the layer's native window handle and doesn't look inside it, so
 * the vi:/dispdrv services (binder, buffer queues) aren't needed. */
#include "hle.h"

static uint64_t display, layer, native_window;
static uint64_t frame_number;

static uint64_t obj(uint64_t *slot, uint64_t size) {
  if (!*slot) *slot = guest_alloc(size, 16);
  return *slot;
}

HLE(_ZN2nn2vi10InitializeEv) { (void)c; }
HLE(_ZN2nn2vi8FinalizeEv) { (void)c; }

/* Result OpenDefaultDisplay(Display **out) */
HLE(_ZN2nn2vi18OpenDefaultDisplayEPPNS0_7DisplayE) {
  *(uint64_t *)gptr(ARG(0)) = obj(&display, 0x100);
  RET(0);
}
HLE(_ZN2nn2vi12CloseDisplayEPNS0_7DisplayE) { (void)c; }

/* int ListDisplays(DisplayInfo *out, int count): name[64], hasLayerLimit, layerCountMax,
 * layerWidthPixelCountMax, layerHeightPixelCountMax (0x60 bytes) */
HLE(_ZN2nn2vi12ListDisplaysEPNS0_11DisplayInfoEi) {
  if ((int)ARG(1) < 1) { RET(0); return; }
  uint8_t *d = gptr(ARG(0));
  memset(d, 0, 0x60);
  strcpy((char *)d, "Default");
  d[0x40] = 1;
  *(int64_t *)(d + 0x48) = 1;
  *(int64_t *)(d + 0x50) = host_docked() ? 1920 : 1280;
  *(int64_t *)(d + 0x58) = host_docked() ? 1080 : 720;
  RET(1);
}

/* Result CreateLayer(Layer **out, Display *display, const LayerCreationSettings *settings) */
HLE(_ZN2nn2vi11CreateLayerEPPNS0_5LayerEPNS0_7DisplayEPKNS0_21LayerCreationSettingsE) {
  *(uint64_t *)gptr(ARG(0)) = obj(&layer, 0x100);
  RET(0);
}
HLE(_ZN2nn2vi12DestroyLayerEPNS0_5LayerE) { (void)c; }

/* Result GetNativeWindow(void **out, Layer *layer) */
HLE(_ZN2nn2vi15GetNativeWindowEPPvPNS0_5LayerE) {
  *(uint64_t *)gptr(ARG(0)) = obj(&native_window, 0x100);
  RET(0);
}

/* Result GetLatestFrameNumber(uint64_t *out, const Layer *layer) */
HLE(_ZN2nn2vi20GetLatestFrameNumberEPmPKNS0_5LayerE) {
  *(uint64_t *)gptr(ARG(0)) = frame_number;
  RET(0);
}

/* int ListFrameHistory(FrameInfo *out, int count, const Layer *layer) */
HLE(_ZN2nn2vi16ListFrameHistoryEPNS0_9FrameInfoEiPKNS0_5LayerE) { RET(0); }

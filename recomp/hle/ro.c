/* nn::ro: modules the game loads at run time are already in place (recomp/image.py
 * lays them out and relocates them like the others), so LoadModule only checks
 * which one the image is and runs its initializer once. */
#include "hle.h"

#define RO_RESULT_NOT_FOUND 0xA16u  /* what nnSdk returns for a missing symbol */

/* What nnSdk's LookupModuleSymbol reads through Module[0]: the load base is at +0x28. */
#define RO_MODULE_BASE 0x28

static const HostModule *module_of_image(uint64_t image) {
  /* NRO header: "NRO0" at +0x10, build id at +0x40; compare with the module's own header. */
  for (unsigned i = 0; i < host_module_count; i++) {
    const HostModule *m = &host_modules[i];
    if (m->ro && !memcmp(gptr(image + 0x40), gptr(m->base + 0x40), 0x20)) return m;
  }
  return 0;
}

HLE(_ZN2nn2ro10InitializeEv) { RET(0); }
HLE(_ZN2nn2ro18RegisterModuleInfoEPNS0_16RegistrationInfoEPKv) { RET(0); }
HLE(_ZN2nn2ro20UnregisterModuleInfoEPNS0_16RegistrationInfoE) { RET(0); }
HLE(_ZN2nn2ro13GetBufferSizeEPmPKv) {
  *(uint64_t *)gptr(ARG(0)) = 0x1000;
  RET(0);
}

/* LoadModule(Module *module, const void *image, void *buffer, size_t bufferSize, int bindFlag) */
HLE(_ZN2nn2ro10LoadModuleEPNS0_6ModuleEPKvPvmi) {
  uint64_t module = ARG(0), image = ARG(1);
  if (!image || memcmp(gptr(image + 0x10), "NRO0", 4)) HDIE("ro: LoadModule of a non-NRO image\n");
  const HostModule *m = module_of_image(image);
  if (!m) HDIE("ro: LoadModule of an NRO that isn't in the build (recomp/image.py MODULES)\n");
  static uint64_t ro_objs[8];
  unsigned idx = (unsigned)(m - host_modules);
  if (!ro_objs[idx]) {
    ro_objs[idx] = guest_alloc(0x100, 16);
    *(uint64_t *)gptr(ro_objs[idx] + RO_MODULE_BASE) = m->base;
    HLOG("[ro] LoadModule %s at %#llx\n", m->name, (unsigned long long)m->base);
    if (m->init) {
      uint64_t lr = c->x[30];
      a64_call(c, m->init);
      c->x[30] = lr;
    }
  }
  memset(gptr(module), 0, 0x38);
  *(uint64_t *)gptr(module) = ro_objs[idx];
  RET(0);
}

HLE(_ZN2nn2ro12UnloadModuleEPNS0_6ModuleE) { RET(0); }

/* LookupModuleSymbol(uintptr_t *out, const Module *module, const char *name) */
HLE(_ZN2nn2ro18LookupModuleSymbolEPmPKNS0_6ModuleEPKc) {
  uint64_t obj = *(uint64_t *)gptr(ARG(1));
  uint64_t base = obj ? *(uint64_t *)gptr(obj + RO_MODULE_BASE) : 0;
  for (unsigned i = 0; i < host_module_count; i++) {
    const HostModule *m = &host_modules[i];
    if (m->base != base) continue;
    uint64_t a = host_symbol_addr(gstr(ARG(2)), m->base, m->base + m->size);
    if (host_trace) HLOG("[ro] LookupModuleSymbol %s -> %#llx\n", gstr(ARG(2)), (unsigned long long)a);
    if (!a) break;
    *(uint64_t *)gptr(ARG(0)) = a;
    RET(0);
    return;
  }
  RET(RO_RESULT_NOT_FOUND);
}

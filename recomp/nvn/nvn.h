/* NVN implemented on the host.
 *
 * The game gets every NVN entry point by name (nvnBootstrapLoader /
 * nvnDeviceGetProcAddress) and calls it through a function pointer, so each
 * one is a host thunk. NVN(nvnFoo) { ... } defines the implementation of
 * nvnFoo; recomp/nvn/table.py collects them into build/nvn_table.c.
 * Arguments follow the AAPCS64 (x0-x7, v0-v7), as for HLE().
 *
 * objects.c, cmdbuf.c, tegra.c, formats.c and astc.c come from LAS Recomp
 * (github.com/peiikai/LASRecomp, runtime/nvn, MIT). There guest addresses
 * are host pointers; here they go through gptr(), and GP() where 0 must stay
 * NULL. Guest NVN objects hold a host object pointer at offset 0; builders
 * and small state objects keep their fields in guest memory. */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "../rt/host.h"
#include "tegra.h"

#define NVN(name) void nvn_##name(CPU *c)

typedef struct { const char *name; A64Func fn; } NvnImpl;
extern const NvnImpl nvn_impls[];  /* sorted by name */
extern const unsigned nvn_impl_count;

/* ---- arguments and guest memory */
#define A(i) (c->x[i])
#define AP(i) gptr(c->x[i])
#define AF(i) (c->v[i].f[0])
#define RET(v) (c->x[0] = (uint64_t)(v))
#define RETF(x) do { c->v[0] = (V128){0}; c->v[0].f[0] = (x); } while (0)
static inline void *GP(uint64_t a) { return a ? gptr(a) : NULL; }
/* field of a guest object */
#define FLD(T, obj, off) (*(T *)gptr((obj) + (off)))
/* host object behind a guest NVN object (pointer at offset 0) */
#define HOST(T, guest) ((T *)(uintptr_t)FLD(uint64_t, guest, 0))
#define SET_HOST(guest, p) (FLD(uint64_t, guest, 0) = (uint64_t)(uintptr_t)(p))

#define LOG(...) HLOG(__VA_ARGS__)
#define FATAL(...) HDIE(__VA_ARGS__)
#define RT_ON_RESET(fn) static inline void fn(void)  /* LAS in-place reboot hooks: unused here */

/* Call guest code from an NVN implementation (callbacks); x30 survives. */
static inline void guest_call(CPU *c, uint64_t fn) {
  uint64_t lr = c->x[30];
  a64_call(c, fn);
  c->x[30] = lr;
}
static inline uint64_t plat_now_ns(void) { return clock_gettime_nsec_np(CLOCK_UPTIME_RAW); }

/* ---- host objects */
typedef struct { uint64_t voff, size, phys; } NvnPoolMapping;  /* virtual range -> guest address */

typedef struct NvnPool {
  uint64_t storage, size;  /* guest address of the pool's memory */
  bool virt;               /* virtual pool: storage is a VPOOL range that MapVirtual aliases physical pages into */
  uint32_t flags;
  /* virtual pools (flag 0x200): ranges mapped by nvnMemoryPoolMapVirtual, sorted by voff */
  NvnPoolMapping *map;
  int nmap, cap_map;
} NvnPool;
uint64_t nvn_virtual_phys(uint64_t addr, uint64_t size);  /* physical-pool address behind a virtual-pool range, or 0 */

typedef struct NvnTexture {
  TexLayout layout;
  uint32_t target, format, flags, width, height, depth, levels, samples, ds_mode;
  uint8_t swizzle[4];
  uint64_t stride;
  uint64_t addr;  /* guest address of texel storage */
  NvnPool *pool;
  uint64_t pool_offset;
  void *backend;  /* renderer-side texture */
  uint64_t debug_id;
  char label[64];
  uint64_t draw_frame, draw_prev_frame;  /* renderer: the last two frames that drew into it (one_shot_target) */
} NvnTexture;

typedef struct {
  uint32_t min_filter, mag_filter, wrap[3], compare_mode, compare_func, reduction;
  float lod_min, lod_max, lod_bias, max_aniso;
  float border[4];
  void *backend;
} NvnSamplerDesc;

typedef struct {
  uint32_t setmask;  /* which fields are overridden */
  uint16_t base_level, num_levels, min_layer, num_layers;
  uint32_t format, target, ds_mode;
  uint8_t swizzle[4];
} NvnTextureView;  /* our layout of the guest NVNtextureView (0x28 bytes) */

typedef struct {
  NvnTexture *tex;
  NvnTextureView view;
  bool has_view;
} NvnTexEntry;

typedef struct NvnDescPool {
  uint64_t addr;
  int count;
  void *entries;  /* NvnTexEntry[] or NvnSamplerDesc[] */
} NvnDescPool;

typedef struct NvnShaderStage {
  uint32_t stage;                     /* NVNshaderStage */
  uint64_t code_addr;                 /* GPU (== guest) address of the code */
  const uint8_t *control;             /* control section (host pointer into guest memory) */
  uint32_t const_offset, const_size;  /* compiler constants (bound as c1), within the code blob */
  void *backend;
} NvnShaderStage;

typedef struct NvnProgram {
  NvnShaderStage stages[6];
  uint32_t stage_mask;
  void *backend;
} NvnProgram;

typedef struct NvnWindow {
  uint64_t textures[8];  /* guest NVNtexture* */
  int num, active, interval, next;
  int crop[4];
} NvnWindow;

typedef struct NvnSync {
  volatile int signaled;
  volatile uint64_t seq;  /* GPU submission this fence belongs to (renderer_fence_seq), 0 = none */
} NvnSync;

/* ---- command recording (stored in guest command memory) */
typedef struct {
  uint64_t mem;    /* current command memory chunk (guest address) */
  uint64_t size;   /* chunk size */
  uint64_t used;   /* bytes used in chunk */
  uint64_t start;  /* first record of the list being recorded */
  uint64_t callback, callback_data;
  bool recording;
  uint64_t guest;  /* the guest NVNcommandBuffer* */
} NvnCmdBuf;

/* One recorded command: header, the call's integer and float arguments, then copied data. */
typedef struct {
  uint32_t op, size;  /* size includes the header, multiple of 8 */
  uint64_t x[7];      /* x1..x7 of the call */
  float f[4];         /* s0..s3 of the call */
  uint8_t extra[];
} CmdRec;

enum {
  CMD_END = 0, CMD_JUMP, CMD_CALL,
  CMD_BIND_PROGRAM, CMD_BIND_BLEND, CMD_BIND_CHANNEL_MASK, CMD_BIND_COLOR, CMD_BIND_DEPTH_STENCIL,
  CMD_BIND_MULTISAMPLE, CMD_BIND_POLYGON, CMD_BIND_VERTEX_ATTRIB, CMD_BIND_VERTEX_STREAM,
  CMD_BIND_VERTEX_BUFFERS, CMD_BIND_UNIFORM_BUFFERS, CMD_BIND_STORAGE_BUFFERS, CMD_BIND_TEXTURES,
  CMD_BIND_IMAGES, CMD_SET_TEXTURE_POOL, CMD_SET_SAMPLER_POOL, CMD_SET_RENDER_TARGETS,
  CMD_SET_VIEWPORTS, CMD_SET_SCISSORS, CMD_SET_DEPTH_RANGES, CMD_SET_BLEND_COLOR, CMD_SET_STENCIL,
  CMD_SET_PRIMITIVE_RESTART, CMD_SET_ALPHA_REF, CMD_SET_DEPTH_BOUNDS, CMD_SET_POLYGON_OFFSET,
  CMD_SET_LINE_WIDTH, CMD_SET_POINT_SIZE, CMD_SET_SAMPLE_MASK, CMD_SET_RENDER_ENABLE,
  CMD_CLEAR_COLOR, CMD_CLEAR_DEPTH_STENCIL, CMD_CLEAR_TEXTURE, CMD_CLEAR_BUFFER,
  CMD_DRAW_ARRAYS, CMD_DRAW_ELEMENTS, CMD_DRAW_ARRAYS_INDIRECT, CMD_DRAW_ELEMENTS_INDIRECT,
  CMD_DISPATCH, CMD_DISPATCH_INDIRECT,
  CMD_COPY_BUFFER_TO_BUFFER, CMD_COPY_BUFFER_TO_TEXTURE, CMD_COPY_TEXTURE_TO_BUFFER,
  CMD_COPY_TEXTURE_TO_TEXTURE, CMD_DOWNSAMPLE, CMD_UPDATE_UNIFORM_BUFFER, CMD_FENCE_SYNC, CMD_WAIT_SYNC,
  CMD_BARRIER, CMD_SIGNAL_EVENT, CMD_WAIT_EVENT, CMD_REPORT_COUNTER, CMD_DISCARD_COLOR,
  CMD_DISCARD_DEPTH_STENCIL, CMD_SET_COPY_STRIDES, CMD_DRAW_TEXTURE, CMD_RESOLVE_DEPTH,
  CMD_BIND_SEPARATE_TEXTURES, CMD_BIND_SEPARATE_SAMPLERS,
  CMD_COUNT
};

/* ---- renderer backend (metal.m, renderer.m) */
void backend_init(void);
void backend_execute(uint64_t handle);  /* run a recorded command list */
void backend_present(NvnTexture *tex);  /* show a texture in the window */
void backend_texture_destroyed(NvnTexture *tex);
void backend_pool_written(uint64_t addr, uint64_t size);
void backend_pool_created(NvnPool *p);    /* wrap its storage for the GPU */
/* A texture's texels in guest memory: storage of a physical pool, or for a virtual pool the mapped memory
   (gathered into a malloc'd *tmp the caller frees when it spans separate physical ranges). NULL while
   any part is unmapped. */
const uint8_t *nvn_texture_data(NvnTexture *t, uint64_t size, uint8_t **tmp);
void backend_pool_destroyed(NvnPool *p);
void renderer_sync(void);   /* wait for all submitted GPU work */
void renderer_flush(void);  /* finish all recorded GPU work */
uint64_t renderer_fence_seq(void);
void renderer_wait_seq(uint64_t seq);

/* objects.c */
void nvn_view_read(uint64_t guest, NvnTextureView *v);  /* unpack a guest NVNtextureView */
uint32_t nvn_window_origin(void);
uint32_t nvn_depth_mode(void);
void nvn_sync_set_seq(uint64_t guest, uint64_t seq);
NvnDescPool *nvn_find_pool(NvnDescPool **reg, uint64_t guest_pool);
extern NvnDescPool *g_texture_pools[64], *g_sampler_pools[64];

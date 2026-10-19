// Interface between the NVN executor (renderer.m) and the Metal plumbing (metal.m), from LAS Recomp
// (runtime/nvn/renderer.h, MIT).
#pragma once
#include <stdio.h>
#include <sys/stat.h>

// getenv, looked up once per call site
#define ENV(name) ({ static const char *env_v_; static int env_i_; if (!env_i_) { env_v_ = getenv(name); env_i_ = 1; } env_v_; })

#ifdef __OBJC__
#import <Metal/Metal.h>
void renderer_init(id<MTLDevice> dev);
id<MTLCommandQueue> renderer_queue(void);
id<MTLTexture> renderer_texture(NvnTexture *t, id<MTLBlitCommandEncoder> blit, bool for_render);
MTLPixelFormat renderer_pixel_format(uint32_t nvn_format);
bool renderer_format_alpha_one(uint32_t nvn_format);
// The memory pool buffer holding guest [addr, addr + size), with addr's offset in it; nil when no pool holds it all.
id<MTLBuffer> renderer_pool_buffer(uint64_t addr, uint64_t size, uint64_t *offset);
// The buffer of the pool holding addr and the guest address of its first byte (pool buffers start on a host page).
id<MTLBuffer> renderer_pool_window(uint64_t addr, uint64_t *guest_base);
void renderer_save_texture(id<MTLTexture> tex, const char *path);
void renderer_save_texture_alpha(id<MTLTexture> tex, const char *path);
#endif

bool renderer_is_gpu_owned(NvnTexture *t);
void renderer_submit(void);  // commit recorded work without waiting
bool renderer_origin_lower_left(void);
const char *renderer_stats(void);
void renderer_profile_report(void);  // FIFA_GPU_PROFILE: log the top GPU time per pass / compute program
bool renderer_format_is_depth(uint32_t f);
bool renderer_format_has_stencil(uint32_t f);
void renderer_mark_gpu_owned(NvnTexture *t);
void renderer_texture_gone(void *nvn_tex, void *mtl_tex);
void renderer_texture_scale(NvnTexture *t, float *sx, float *sy);  // allocation scale (always 1 here)
bool renderer_texture_presented(NvnTexture *t);
uint64_t renderer_present_count(void);

// Metal backend for the NVN layer: memory pool buffers, textures, presentation and the window. Formats, texture
// uploads and screenshots come from LAS Recomp (runtime/nvn/metal.m, MIT); the window is plain AppKit here.
//
// GPU memory: NVN GPU addresses are guest addresses, and the game's memory pools are spread over ~8.5 GB of the
// alias region, more than one MTLBuffer can wrap (4 GB on an M1). Each pool gets its own no-copy MTLBuffer over
// its guest pages (widened to host pages; overlapping wraps are fine), and renderer.m binds addresses through
// renderer_pool_buffer().
#import <AppKit/AppKit.h>
#import <ImageIO/ImageIO.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>
#include <pthread.h>
#include <stdatomic.h>
#include <unistd.h>

#include "../rt/input.h"
#include "nvn.h"
#include "renderer.h"

static id<MTLDevice> dev;
static id<MTLCommandQueue> queue;
static CAMetalLayer *layer;
static NSWindow *window;
static bool window_shown;  // (read from the render thread)
static atomic_bool ui_ready;
static id<MTLRenderPipelineState> present_pipe;
static id<MTLSamplerState> present_sampler;
bool g_debug_frame;
int g_probe_fs_draws = -1;  // draws of the FIFA_DBG_FS probed program this frame (-1: no probe)

// ================================================================== memory pools
#define HOST_PAGE 0x4000ull
typedef struct { uint64_t lo, hi; void *buf; NvnPool *pool; } PoolBuf;  // [lo, hi): guest range wrapped by buf
static PoolBuf *pools;
static int npools, cap_pools;
static pthread_mutex_t pools_lock = PTHREAD_MUTEX_INITIALIZER;

void backend_pool_created(NvnPool *p) {
    if (p->virt || !p->size || !dev) return;  // virtual pools: through nvn_virtual_phys
    uint64_t lo = p->storage & ~(HOST_PAGE - 1), hi = (p->storage + p->size + HOST_PAGE - 1) & ~(HOST_PAGE - 1);
    id<MTLBuffer> b = [dev newBufferWithBytesNoCopy:gptr(lo) length:hi - lo options:MTLResourceStorageModeShared
                                        deallocator:nil];
    if (!b) { LOG("[metal] cannot wrap pool %#llx+%#llx\n", (unsigned long long)p->storage, (unsigned long long)p->size); return; }
    b.label = [NSString stringWithFormat:@"pool %#llx", (unsigned long long)p->storage];
    pthread_mutex_lock(&pools_lock);
    if (npools == cap_pools) pools = realloc(pools, (cap_pools = cap_pools ? cap_pools * 2 : 256) * sizeof *pools);
    pools[npools++] = (PoolBuf){lo, hi, (void *)CFBridgingRetain(b), p};
    pthread_mutex_unlock(&pools_lock);
}

void backend_pool_destroyed(NvnPool *p) {
    pthread_mutex_lock(&pools_lock);
    for (int i = 0; i < npools; i++)
        if (pools[i].pool == p) {
            CFRelease(pools[i].buf);  // (command buffers still in flight keep their own reference)
            pools[i] = pools[--npools];
            break;
        }
    pthread_mutex_unlock(&pools_lock);
}

// (under pools_lock) The last 4 pools found on this thread first: a draw's buffers alternate between a few pools.
static PoolBuf *pool_find(uint64_t addr, uint64_t size) {
    static __thread int recent[4];
    for (int k = 0; k < 4; k++) {
        int i = recent[k];
        if (i < npools && addr >= pools[i].lo && addr + size <= pools[i].hi) return &pools[i];
    }
    for (int i = 0; i < npools; i++)
        if (addr >= pools[i].lo && addr + size <= pools[i].hi) {
            memmove(recent + 1, recent, 3 * sizeof recent[0]);
            recent[0] = i;
            return &pools[i];
        }
    return NULL;
}

id<MTLBuffer> renderer_pool_buffer(uint64_t addr, uint64_t size, uint64_t *offset) {
    if (addr >= VPOOL_BASE && addr < VPOOL_END && !(addr = nvn_virtual_phys(addr, size))) return nil;
    pthread_mutex_lock(&pools_lock);
    PoolBuf *pb = pool_find(addr, size);
    id<MTLBuffer> b = pb ? (__bridge id<MTLBuffer>)pb->buf : nil;
    if (pb) *offset = addr - pb->lo;
    pthread_mutex_unlock(&pools_lock);
    return b;
}

id<MTLBuffer> renderer_pool_window(uint64_t addr, uint64_t *guest_base) {
    uint64_t phys = addr;  // virtual: the physical pool, based so that addr lands on its page (one mapping's worth)
    if (addr >= VPOOL_BASE && addr < VPOOL_END && !(phys = nvn_virtual_phys(addr, 1))) return nil;
    pthread_mutex_lock(&pools_lock);
    PoolBuf *pb = pool_find(phys, 1);
    id<MTLBuffer> b = pb ? (__bridge id<MTLBuffer>)pb->buf : nil;
    if (pb) *guest_base = pb->lo + (addr - phys);
    pthread_mutex_unlock(&pools_lock);
    return b;
}

// ================================================================== formats
typedef struct { MTLPixelFormat pf; uint8_t expand_rgb; uint8_t force_alpha_one; } MtlFormat;

static MtlFormat mtl_format(uint32_t f) {
    switch (f) {
    case 0x01: return (MtlFormat){MTLPixelFormatR8Unorm};
    case 0x02: return (MtlFormat){MTLPixelFormatR8Snorm};
    case 0x03: return (MtlFormat){MTLPixelFormatR8Uint};
    case 0x04: return (MtlFormat){MTLPixelFormatR8Sint};
    case 0x05: return (MtlFormat){MTLPixelFormatR16Float};
    case 0x06: return (MtlFormat){MTLPixelFormatR16Unorm};
    case 0x07: return (MtlFormat){MTLPixelFormatR16Snorm};
    case 0x08: return (MtlFormat){MTLPixelFormatR16Uint};
    case 0x09: return (MtlFormat){MTLPixelFormatR16Sint};
    case 0x0A: return (MtlFormat){MTLPixelFormatR32Float};
    case 0x0B: return (MtlFormat){MTLPixelFormatR32Uint};
    case 0x0C: return (MtlFormat){MTLPixelFormatR32Sint};
    case 0x0D: return (MtlFormat){MTLPixelFormatRG8Unorm};
    case 0x0E: return (MtlFormat){MTLPixelFormatRG8Snorm};
    case 0x0F: return (MtlFormat){MTLPixelFormatRG8Uint};
    case 0x10: return (MtlFormat){MTLPixelFormatRG8Sint};
    case 0x11: return (MtlFormat){MTLPixelFormatRG16Float};
    case 0x12: return (MtlFormat){MTLPixelFormatRG16Unorm};
    case 0x13: return (MtlFormat){MTLPixelFormatRG16Snorm};
    case 0x14: return (MtlFormat){MTLPixelFormatRG16Uint};
    case 0x15: return (MtlFormat){MTLPixelFormatRG16Sint};
    case 0x16: return (MtlFormat){MTLPixelFormatRG32Float};
    case 0x17: return (MtlFormat){MTLPixelFormatRG32Uint};
    case 0x18: return (MtlFormat){MTLPixelFormatRG32Sint};
    case 0x19: return (MtlFormat){MTLPixelFormatRGBA8Unorm, 1, 1};
    case 0x25: return (MtlFormat){MTLPixelFormatRGBA8Unorm};
    case 0x26: return (MtlFormat){MTLPixelFormatRGBA8Snorm};
    case 0x27: return (MtlFormat){MTLPixelFormatRGBA8Uint};
    case 0x28: return (MtlFormat){MTLPixelFormatRGBA8Sint};
    case 0x29: return (MtlFormat){MTLPixelFormatRGBA16Float};
    case 0x2A: return (MtlFormat){MTLPixelFormatRGBA16Unorm};
    case 0x2B: return (MtlFormat){MTLPixelFormatRGBA16Snorm};
    case 0x2C: return (MtlFormat){MTLPixelFormatRGBA16Uint};
    case 0x2D: return (MtlFormat){MTLPixelFormatRGBA16Sint};
    case 0x2E: return (MtlFormat){MTLPixelFormatRGBA32Float};
    case 0x2F: return (MtlFormat){MTLPixelFormatRGBA32Uint};
    case 0x30: return (MtlFormat){MTLPixelFormatRGBA32Sint};
    case 0x31: return (MtlFormat){MTLPixelFormatStencil8};
    case 0x32: return (MtlFormat){MTLPixelFormatDepth16Unorm};
    case 0x33: return (MtlFormat){MTLPixelFormatDepth32Float};
    case 0x34: return (MtlFormat){MTLPixelFormatDepth32Float};
    case 0x35: return (MtlFormat){MTLPixelFormatDepth32Float_Stencil8};
    case 0x36: return (MtlFormat){MTLPixelFormatDepth32Float_Stencil8};
    case 0x37: return (MtlFormat){MTLPixelFormatRGBA8Unorm_sRGB, 0, 1};
    case 0x38: return (MtlFormat){MTLPixelFormatRGBA8Unorm_sRGB};
    case 0x39: return (MtlFormat){MTLPixelFormatABGR4Unorm};
    case 0x3A: return (MtlFormat){MTLPixelFormatB5G6R5Unorm};
    case 0x3B: return (MtlFormat){MTLPixelFormatA1BGR5Unorm};
    case 0x3C: return (MtlFormat){MTLPixelFormatB5G6R5Unorm};
    case 0x3D: return (MtlFormat){MTLPixelFormatRGB10A2Unorm};
    case 0x3E: return (MtlFormat){MTLPixelFormatRGB10A2Uint};
    case 0x3F: return (MtlFormat){MTLPixelFormatRG11B10Float};
    case 0x40: return (MtlFormat){MTLPixelFormatRGB9E5Float};
    case 0x41: case 0x42: return (MtlFormat){MTLPixelFormatBC1_RGBA};
    case 0x43: return (MtlFormat){MTLPixelFormatBC2_RGBA};
    case 0x44: return (MtlFormat){MTLPixelFormatBC3_RGBA};
    case 0x45: case 0x46: return (MtlFormat){MTLPixelFormatBC1_RGBA_sRGB};
    case 0x47: return (MtlFormat){MTLPixelFormatBC2_RGBA_sRGB};
    case 0x48: return (MtlFormat){MTLPixelFormatBC3_RGBA_sRGB};
    case 0x49: return (MtlFormat){MTLPixelFormatBC4_RUnorm};
    case 0x4A: return (MtlFormat){MTLPixelFormatBC4_RSnorm};
    case 0x4B: return (MtlFormat){MTLPixelFormatBC5_RGUnorm};
    case 0x4C: return (MtlFormat){MTLPixelFormatBC5_RGSnorm};
    case 0x4D: return (MtlFormat){MTLPixelFormatBC7_RGBAUnorm};
    case 0x4E: return (MtlFormat){MTLPixelFormatBC7_RGBAUnorm_sRGB};
    case 0x4F: return (MtlFormat){MTLPixelFormatBC6H_RGBFloat};
    case 0x50: return (MtlFormat){MTLPixelFormatBC6H_RGBUfloat};
    case 0x6D: return (MtlFormat){MTLPixelFormatRGBA8Unorm, 0, 1};
    case 0x71: return (MtlFormat){MTLPixelFormatRGBA16Float, 0, 1};
    case 0x76: return (MtlFormat){MTLPixelFormatRGBA32Float, 0, 1};
    case 0x79: return (MtlFormat){MTLPixelFormatASTC_4x4_LDR};
    case 0x7A: return (MtlFormat){MTLPixelFormatASTC_5x4_LDR};
    case 0x7B: return (MtlFormat){MTLPixelFormatASTC_5x5_LDR};
    case 0x7C: return (MtlFormat){MTLPixelFormatASTC_6x5_LDR};
    case 0x7D: return (MtlFormat){MTLPixelFormatASTC_6x6_LDR};
    case 0x7E: return (MtlFormat){MTLPixelFormatASTC_8x5_LDR};
    case 0x7F: return (MtlFormat){MTLPixelFormatASTC_8x6_LDR};
    case 0x80: return (MtlFormat){MTLPixelFormatASTC_8x8_LDR};
    case 0x81: return (MtlFormat){MTLPixelFormatASTC_10x5_LDR};
    case 0x82: return (MtlFormat){MTLPixelFormatASTC_10x6_LDR};
    case 0x83: return (MtlFormat){MTLPixelFormatASTC_10x8_LDR};
    case 0x84: return (MtlFormat){MTLPixelFormatASTC_10x10_LDR};
    case 0x85: return (MtlFormat){MTLPixelFormatASTC_12x10_LDR};
    case 0x86: return (MtlFormat){MTLPixelFormatASTC_12x12_LDR};
    case 0x87: return (MtlFormat){MTLPixelFormatASTC_4x4_sRGB};
    case 0x88: return (MtlFormat){MTLPixelFormatASTC_5x4_sRGB};
    case 0x89: return (MtlFormat){MTLPixelFormatASTC_5x5_sRGB};
    case 0x8A: return (MtlFormat){MTLPixelFormatASTC_6x5_sRGB};
    case 0x8B: return (MtlFormat){MTLPixelFormatASTC_6x6_sRGB};
    case 0x8C: return (MtlFormat){MTLPixelFormatASTC_8x5_sRGB};
    case 0x8D: return (MtlFormat){MTLPixelFormatASTC_8x6_sRGB};
    case 0x8E: return (MtlFormat){MTLPixelFormatASTC_8x8_sRGB};
    case 0x8F: return (MtlFormat){MTLPixelFormatASTC_10x5_sRGB};
    case 0x90: return (MtlFormat){MTLPixelFormatASTC_10x6_sRGB};
    case 0x91: return (MtlFormat){MTLPixelFormatASTC_10x8_sRGB};
    case 0x92: return (MtlFormat){MTLPixelFormatASTC_10x10_sRGB};
    case 0x93: return (MtlFormat){MTLPixelFormatASTC_12x10_sRGB};
    case 0x94: return (MtlFormat){MTLPixelFormatASTC_12x12_sRGB};
    case 0x95: case 0x97: return (MtlFormat){MTLPixelFormatA1BGR5Unorm};
    case 0x96: return (MtlFormat){MTLPixelFormatA1BGR5Unorm};
    case 0x98: return (MtlFormat){MTLPixelFormatBGRA8Unorm, 0, 1};
    case 0x99: return (MtlFormat){MTLPixelFormatBGRA8Unorm};
    case 0x9A: return (MtlFormat){MTLPixelFormatBGRA8Unorm_sRGB, 0, 1};
    case 0x9B: return (MtlFormat){MTLPixelFormatBGRA8Unorm_sRGB};
    default:
        return (MtlFormat){MTLPixelFormatRGBA8Unorm};
    }
}

bool renderer_format_is_depth(uint32_t f) { return f >= 0x31 && f <= 0x36; }
bool renderer_format_has_stencil(uint32_t f) { return f == 0x31 || f == 0x35 || f == 0x36; }
MTLPixelFormat renderer_pixel_format(uint32_t f) { return mtl_format(f).pf; }
bool renderer_format_alpha_one(uint32_t f) { return mtl_format(f).force_alpha_one; }

// ================================================================== textures
typedef struct {
    __unsafe_unretained id<MTLTexture> tex;  // retained manually (CFBridgingRetain)
    bool gpu_owned;     // written by the GPU (render target / copy dest): never re-uploaded
    bool uploaded;
    bool dynamic;       // contents changed after the first upload: verify fully every frame
    bool presented;     // has been shown by backend_present (a swapchain image)
    bool zero_copy;     // a buffer texture made over its pool's MTLBuffer: always current, nothing to upload
    uint64_t data_hash, sample_hash;
    uint64_t checked_frame, full_frame;
} TexBackend;

static uint64_t frames, total_frames;
int g_tex_live;  // (stats: Metal textures alive and their size)
uint64_t g_tex_live_bytes;

uint64_t renderer_present_count(void) { return total_frames; }

void renderer_texture_scale(NvnTexture *t, float *sx, float *sy) { (void)t; *sx = 1; *sy = 1; }

bool renderer_texture_presented(NvnTexture *t) {
    TexBackend *b = t ? (TexBackend *)t->backend : NULL;
    return b && b->presented;
}

static MTLTextureType mtl_tex_type(uint32_t target, uint32_t samples) {
    switch (target) {
    case 0: return MTLTextureType2D;  // 1D and buffer textures: rows of 16384 texels (translate.c rows1d)
    case 2: return MTLTextureType3D;
    case 3: return MTLTextureType1DArray;
    case 4: return MTLTextureType2DArray;
    case 5: return MTLTextureType2DMultisample;
    case 6: return MTLTextureType2DMultisampleArray;
    case 8: return MTLTextureTypeCube;
    case 9: return MTLTextureTypeCubeArray;
    case 10: return MTLTextureType2D;
    default: return samples > 1 ? MTLTextureType2DMultisample : MTLTextureType2D;
    }
}

// Buffer textures in a physical pool: a linear texture over the pool's no-copy MTLBuffer, so the GPU reads the guest
// memory itself (the UI rewrites them every frame; hashing and re-uploading them was the render thread's largest
// host cost). nil when the alignment or the pool doesn't allow it.
static id<MTLTexture> buffer_texture_in_place(NvnTexture *t, MTLTextureDescriptor *d, MtlFormat mf) {
    if (t->target != 10 || mf.expand_rgb || !t->pool || !t->pool->storage) return nil;
    uint32_t bpb = nvn_format_info(t->format)->bpb;
    uint64_t row = (uint64_t)d.width * bpb, off;
    NSUInteger align = [dev minimumLinearTextureAlignmentForPixelFormat:d.pixelFormat];
    if (!align) return nil;
    if (d.height == 1) row = (row + align - 1) / align * align;  // (one row: the pitch only has to be aligned)
    id<MTLBuffer> pb = renderer_pool_buffer(t->pool->storage + t->pool_offset, row * d.height, &off);
    if (!pb || off % align || row % align) return nil;
    d.storageMode = MTLStorageModeShared;
    d.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite | MTLTextureUsagePixelFormatView;
    return [pb newTextureWithDescriptor:d offset:off bytesPerRow:row];
}

static void create_backend_texture(NvnTexture *t, TexBackend *b) {
    MtlFormat mf = mtl_format(t->format);
    MTLTextureDescriptor *d = [MTLTextureDescriptor new];
    d.textureType = mtl_tex_type(t->target, t->samples);
    d.pixelFormat = mf.pf;
    d.width = t->width ? t->width : 1;
    d.height = t->target == 3 ? 1 : (t->height ? t->height : 1);
    if (t->target == 0 || t->target == 10) {  // Apple GPUs: 1D textures up to 16384 texels; longer ones wrap to rows
        d.width = MIN(d.width, 16384);
        d.height = ((t->width ? t->width : 1) + 16383) / 16384;
    }
    d.depth = t->target == 2 ? (t->depth ? t->depth : 1) : 1;
    if (t->target == 3) d.arrayLength = t->height ? t->height : 1;
    else if (t->target == 4 || t->target == 6) d.arrayLength = t->depth ? t->depth : 1;
    else if (t->target == 9) d.arrayLength = (t->depth ? t->depth : 6) / 6;
    d.mipmapLevelCount = t->levels ? t->levels : 1;
    if (t->samples > 1) { d.sampleCount = t->samples; d.mipmapLevelCount = 1; }
    if (renderer_format_is_depth(t->format) && (d.textureType == MTLTextureType1D || d.textureType == MTLTextureType1DArray ||
                                                d.textureType == MTLTextureType3D)) {
        // Metal has no 1D or 3D depth textures: 2D, layers for a 1D array's rows or a 3D texture's slices
        LOG("[metal] depth texture fmt %#x target %u %ux%ux%u as 2D (array)\n", t->format, t->target, t->width, t->height, t->depth);
        d.arrayLength = d.textureType == MTLTextureType3D ? d.depth : d.arrayLength;
        d.height = t->target == 3 ? 1 : d.height;
        d.depth = 1;
        d.textureType = d.arrayLength > 1 ? MTLTextureType2DArray : MTLTextureType2D;
    }
    d.usage = MTLTextureUsageShaderRead | MTLTextureUsagePixelFormatView | MTLTextureUsageRenderTarget |
              MTLTextureUsageShaderWrite;
    d.storageMode = MTLStorageModePrivate;
    // Shaders sample depth through ordinary texture2d bindings (Maxwell has no separate depth type);
    // keep depth uncompressed so those reads see the stored values.
    if (renderer_format_is_depth(t->format)) d.allowGPUOptimizedContents = NO;
    // (formats without alpha read alpha 1 through the sampled view: renderer.m apply_swizzle. A swizzle on the
    // texture itself isn't allowed with render-target / shader-write usage on Apple GPUs before M2.)
    id<MTLTexture> tex = buffer_texture_in_place(t, d, mf);
    static int nzero, ncopy;
    if (t->target == 10 && (tex ? nzero++ : ncopy++) < 5)
        LOG("[metal] buffer texture %s: fmt %#x %u texels at %#llx (pool %s, align %lu)\n", tex ? "in place" : "copied",
            t->format, t->width, (unsigned long long)(t->pool ? t->pool->storage + t->pool_offset : 0),
            !t->pool ? "none" : t->pool->storage ? "physical" : "virtual",
            (unsigned long)[dev minimumLinearTextureAlignmentForPixelFormat:d.pixelFormat]);
    if (tex) b->zero_copy = b->uploaded = true;
    else {
        d.storageMode = MTLStorageModePrivate;
        d.usage = MTLTextureUsageShaderRead | MTLTextureUsagePixelFormatView | MTLTextureUsageRenderTarget |
                  MTLTextureUsageShaderWrite;
        tex = [dev newTextureWithDescriptor:d];
    }
    if (!tex) {
        LOG("[metal] texture creation failed: fmt %#x %ux%ux%u target %u\n", t->format, t->width, t->height, t->depth,
            t->target);
        d.pixelFormat = MTLPixelFormatRGBA8Unorm;
        tex = [dev newTextureWithDescriptor:d];
    }
    tex.label = [NSString stringWithFormat:@"%s fmt%#x", t->label[0] ? t->label : "tex", t->format];
    b->tex = tex;
    CFBridgingRetain(tex);
    g_tex_live++;
    g_tex_live_bytes += tex.allocatedSize;
}

static TexBackend *tex_backend(NvnTexture *t) {
    if (t->backend) return (TexBackend *)t->backend;
    TexBackend *b = calloc(1, sizeof *b);
    create_backend_texture(t, b);
    t->backend = b;
    return b;
}

void backend_texture_destroyed(NvnTexture *t) {
    TexBackend *b = t->backend;
    renderer_texture_gone(t, NULL);  // its snapshot copy
    if (b) {
        renderer_texture_gone(NULL, (__bridge void *)b->tex);  // the renderer's views of it
        g_tex_live--;
        g_tex_live_bytes -= b->tex.allocatedSize;
        CFBridgingRelease((__bridge CFTypeRef)b->tex);
        free(b);
    }
    t->backend = NULL;
}

static uint64_t g_frame_counter;  // presents so far (texture change detection runs once per frame)
uint64_t g_tex_samples, g_tex_full, g_tex_full_bytes, g_tex_uploads;  // change checks since the last stats line
static uint64_t full_hash_budget, full_hash_budget_frame;

// Both hash a texture's runs in place (nvn_texture_spans); runs other than the last are whole pages, so the value
// doesn't depend on how the memory is split.
static uint64_t hash_full(const NvnSpan *s, int ns, uint64_t n) {
    uint64_t h = 0x9E3779B97F4A7C15ull ^ n;
    for (int k = 0; k < ns; k++) {
        const uint8_t *p = s[k].p;
        uint64_t i = 0, m = s[k].n;
        for (; i + 8 <= m; i += 8) {
            uint64_t v;
            memcpy(&v, p + i, 8);
            h = (h ^ v) * 0x100000001B3ull;
            h ^= h >> 29;
        }
        for (; i < m; i++) h = (h ^ p[i]) * 0x100000001B3ull;
    }
    return h;
}

// Cheap change detector: hash 512 cache lines spread over the data (a strided sample of single bytes touched 4,096
// lines, each a cache and often a TLB miss; a rewrite of a texture changes many of these lines too).
static uint64_t fnv_sample(const NvnSpan *s, int ns, uint64_t n) {
    uint64_t h = 1469598103934665603ull;
    uint64_t lines = n / 64 < 512 ? n / 64 : 512;
    uint64_t step = lines ? (n / lines) & ~63ull : n, base = 0;
    if (!lines) lines = 1;
    int k = 0;
    for (uint64_t i = 0, l = 0; l < lines && i < n; l++, i += step) {
        while (k < ns && i >= base + s[k].n) base += s[k++].n;
        if (k == ns) break;
        const uint8_t *p = s[k].p + (i - base);
        uint64_t m = base + s[k].n - i;
        if (m > 64) m = 64;
        if (m == 64) {
            for (int j = 0; j < 8; j++) {
                uint64_t v;
                memcpy(&v, p + 8 * j, 8);
                h = (h ^ v) * 1099511628211ull;
            }
        } else {
            for (uint64_t j = 0; j < m; j++) h = (h ^ p[j]) * 1099511628211ull;
        }
    }
    return h ^ n;
}

#define MAX_SPANS 64

// Buffer textures: linear texels straight from guest memory, re-checked every frame (the UI rewrites its
// vertex data in them).
static void upload_buffer_texture(NvnTexture *t, TexBackend *b, id<MTLBlitCommandEncoder> blit) {
    if (b->zero_copy) return;
    if (b->uploaded && b->checked_frame == g_frame_counter) return;
    b->checked_frame = g_frame_counter;
    uint32_t bpb = nvn_format_info(t->format)->bpb;
    uint64_t n = (uint64_t)t->width * bpb;
    NvnSpan sp[MAX_SPANS];
    int ns = n ? nvn_texture_spans(t, n, sp, MAX_SPANS) : 0;
    if (!ns) return;
    uint8_t *tmp = NULL;
    const uint8_t *mem = NULL;
    if (ns < 0) {  // too many runs: hash the gathered copy
        if (!(mem = nvn_texture_data(t, n, &tmp))) return;
        sp[0] = (NvnSpan){mem, n}; ns = 1;
    }
    uint64_t h = hash_full(sp, ns, n);
    if (b->uploaded && h == b->data_hash) { free(tmp); return; }
    if (!mem && !(mem = nvn_texture_data(t, n, &tmp))) return;
    b->data_hash = h;
    b->uploaded = true;
    id<MTLCommandBuffer> own_cb = nil;
    if (!blit) { own_cb = [queue commandBuffer]; blit = [own_cb blitCommandEncoder]; }
    // rows of b->tex.width texels (create_backend_texture); the staging copy pads the last row
    uint64_t row = (uint64_t)b->tex.width * bpb, rows = b->tex.height;
    id<MTLBuffer> staging = [dev newBufferWithLength:row * rows options:MTLResourceStorageModeShared];
    memcpy(staging.contents, mem, n);
    [blit copyFromBuffer:staging sourceOffset:0 sourceBytesPerRow:row sourceBytesPerImage:row * rows
              sourceSize:MTLSizeMake(b->tex.width, rows, 1) toTexture:b->tex
        destinationSlice:0 destinationLevel:0 destinationOrigin:MTLOriginMake(0, 0, 0)];
    if (own_cb) { [blit endEncoding]; [own_cb commit]; }
    free(tmp);
}

// Upload all levels/layers from block-linear guest memory.
static void upload_texture(NvnTexture *t, TexBackend *b, id<MTLBlitCommandEncoder> blit) {
    if (t->target == 10) { if (t->pool) upload_buffer_texture(t, b, blit); return; }
    if (!t->pool) return;
    if (b->uploaded && b->checked_frame == g_frame_counter) return;
    // Textures that have never changed since upload (almost all: loaded from files) are re-checked every 8th
    // frame, staggered per texture; ones seen changing (glyph caches, video...) every frame.
    if (b->uploaded && !b->dynamic && ((g_frame_counter + ((uintptr_t)b >> 6)) & 7)) return;
    const FormatInfo *fi = t->layout.fmt;
    MtlFormat mf = mtl_format(t->format);
    uint64_t total = tegra_total_size(&t->layout);
    // CPU-written textures (e.g. font glyph caches) change sparsely, so a strided sample can miss
    // updates. Textures seen changing get a full hash every frame, the rest a full hash on a rotating
    // ~4 MB/frame budget plus a cheap sample otherwise.
    b->checked_frame = g_frame_counter;
    if (t->flags & 0x10) b->dynamic = true;  // linear textures are the CPU-written kind
    if (full_hash_budget_frame != g_frame_counter) { full_hash_budget_frame = g_frame_counter; full_hash_budget = 4 << 20; }
    // hashed in place; gathered (virtual pools over separate physical ranges) only to upload
    NvnSpan sp[MAX_SPANS];
    int ns = nvn_texture_spans(t, total, sp, MAX_SPANS);  // (virtual pools: 0 until mapped)
    if (!ns) return;
    uint8_t *tmp = NULL;
    const uint8_t *mem = NULL;
    if (ns < 0) {
        if (!(mem = nvn_texture_data(t, total, &tmp))) return;
        sp[0] = (NvnSpan){mem, total}; ns = 1;
    }
    bool full = b->dynamic || !b->uploaded || ENV("FIFA_TEX_FULLHASH");
    if (!full && g_frame_counter - b->full_frame >= 120 && full_hash_budget >= total) { full = true; full_hash_budget -= total; }
    if (!full) g_tex_samples++;
    if (!full && fnv_sample(sp, ns, total) == b->sample_hash) { free(tmp); return; }
    g_tex_full++; g_tex_full_bytes += total;
    uint64_t h = hash_full(sp, ns, total);
    b->full_frame = g_frame_counter;
    b->sample_hash = fnv_sample(sp, ns, total);
    if (b->uploaded && h == b->data_hash) { free(tmp); return; }
    if (!mem && !(mem = nvn_texture_data(t, total, &tmp))) return;
    if (b->uploaded) b->dynamic = true;
    g_tex_uploads++;
    b->data_hash = h;
    b->uploaded = true;
    // callers without an encoder get a private upload command buffer, created only when needed
    id<MTLCommandBuffer> own_cb = nil;
    if (!blit) { own_cb = [queue commandBuffer]; blit = [own_cb blitCommandEncoder]; }
    uint32_t layers = t->layout.layers ? t->layout.layers : 1;
    uint64_t layer_size = tegra_layer_size(&t->layout);
    bool linear = (t->flags & 0x10) != 0;
    if (t->layout.depth > 1 && !linear && t->layout.fmt->bpb == 16 && ENV("FIFA_DBG_3D")) {
        // Debug: which block height / depth makes this 3D texture smooth (RGBA32F LUTs vary slowly between texels)
        LevelLayout l0 = tegra_level(&t->layout, 0);
        uint64_t n = (uint64_t)l0.wb * l0.hb * l0.d;
        float *lin = malloc(n * 16);
        for (uint32_t bh = 0; bh <= 4; bh++)
            for (uint32_t bd = 0; bd <= 5; bd++) {
                tegra_deswizzle(mem, (uint8_t *)lin, l0.wb, l0.hb, l0.d, 16, bh, bd);
                double diff = 0; uint64_t zero = 0, cnt = 0;
                for (uint64_t i = 0; i < n; i++) {
                    zero += lin[i * 4] == 0 && lin[i * 4 + 1] == 0 && lin[i * 4 + 2] == 0;
                    if (i % l0.wb + 1 < l0.wb)
                        for (int k = 0; k < 3; k++) { float dd = fabsf(lin[i * 4 + k] - lin[(i + 1) * 4 + k]); if (isfinite(dd)) { diff += dd; cnt++; } }
                }
                LOG("[3d] %ux%ux%u fmt %#x addr %llx bh %u bd %u (default %u %u): mean neighbour diff %.5f, %llu/%llu zero\n",
                    t->width, t->height, t->layout.depth, t->format, t->addr, bh, bd, l0.block_height_log2, l0.block_depth_log2,
                    cnt ? diff / cnt : 0, zero, n);
            }
        free(lin);
    }
    for (uint32_t layer = 0; layer < layers; layer++) {
        for (uint32_t lv = 0; lv < t->layout.levels; lv++) {
            LevelLayout l = tegra_level(&t->layout, lv);
            uint32_t row = l.wb * fi->bpb;
            uint64_t sz = (uint64_t)row * l.hb * l.d;
            uint8_t *lin = malloc(sz);
            const uint8_t *src = mem + layer * layer_size + l.offset;
            if (linear) {
                for (uint32_t y = 0; y < l.hb; y++) memcpy(lin + (uint64_t)y * row, src + y * t->stride, row);
            } else {
                tegra_deswizzle(src, lin, l.wb, l.hb, l.d, fi->bpb, l.block_height_log2, l.block_depth_log2);
            }
            uint8_t *data = lin;
            uint32_t out_row = row;
            if (mf.expand_rgb && fi->bpb == 3) {  // RGB8 -> RGBA8
                uint64_t n = (uint64_t)l.wb * l.hb * l.d;
                data = malloc(n * 4);
                for (uint64_t i = 0; i < n; i++) {
                    data[i * 4] = lin[i * 3]; data[i * 4 + 1] = lin[i * 3 + 1]; data[i * 4 + 2] = lin[i * 3 + 2];
                    data[i * 4 + 3] = 255;
                }
                out_row = l.wb * 4;
                free(lin);
                lin = data;
            }
            uint64_t out_sz = (uint64_t)out_row * l.hb * l.d;
            id<MTLBuffer> staging = [dev newBufferWithBytes:data length:out_sz options:MTLResourceStorageModeShared];
            free(lin);
            NSUInteger lw = MAX(b->tex.width >> lv, 1), lh = MAX(b->tex.height >> lv, 1), ld = MAX(b->tex.depth >> lv, 1);
            MTLSize size = MTLSizeMake(MIN((NSUInteger)l.w, lw), MIN((NSUInteger)l.h, lh), MIN((NSUInteger)l.d, ld));
            [blit copyFromBuffer:staging sourceOffset:0 sourceBytesPerRow:out_row
                 sourceBytesPerImage:(NSUInteger)out_row * l.hb sourceSize:size toTexture:b->tex
                    destinationSlice:layer destinationLevel:lv destinationOrigin:MTLOriginMake(0, 0, 0)];
        }
    }
    if (own_cb) { [blit endEncoding]; [own_cb commit]; }
    free(tmp);
}

id<MTLTexture> renderer_texture(NvnTexture *t, id<MTLBlitCommandEncoder> blit, bool for_render) {
    TexBackend *b = tex_backend(t);
    if (for_render) b->gpu_owned = true;
    else if (!b->gpu_owned) upload_texture(t, b, blit);
    return b->tex;
}

void renderer_mark_gpu_owned(NvnTexture *t) { tex_backend(t)->gpu_owned = true; }
bool renderer_is_gpu_owned(NvnTexture *t) { return tex_backend(t)->gpu_owned; }

void backend_pool_written(uint64_t addr, uint64_t size) { (void)addr; (void)size; }

// ================================================================== init
static const char *present_src =
    "#include <metal_stdlib>\n"
    "using namespace metal;\n"
    "struct V { float4 pos [[position]]; float2 uv; };\n"
    "vertex V vs(uint vid [[vertex_id]], constant float4 *pv [[buffer(0)]]) {\n"
    "  float2 p = float2((vid << 1) & 2, vid & 2);\n"
    "  V o; o.pos = float4(p * 2.0 - 1.0, 0, 1);\n"
    "  o.uv = float2(p.x, pv[0].x > 0.5 ? p.y : 1.0 - p.y); return o; }\n"
    "fragment float4 fs(V in [[stage_in]], texture2d<float> t [[texture(0)]], sampler s [[sampler(0)]]) {\n"
    "  return float4(t.sample(s, in.uv).rgb, 1.0); }\n";

void backend_init(void) {
    while (!atomic_load(&ui_ready)) usleep(1000);
    queue = [dev newCommandQueue];
    NSError *err = nil;
    id<MTLLibrary> lib = [dev newLibraryWithSource:@(present_src) options:nil error:&err];
    if (!lib) FATAL("present shader: %s\n", err.localizedDescription.UTF8String);
    MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
    pd.vertexFunction = [lib newFunctionWithName:@"vs"];
    pd.fragmentFunction = [lib newFunctionWithName:@"fs"];
    pd.colorAttachments[0].pixelFormat = layer.pixelFormat;
    present_pipe = [dev newRenderPipelineStateWithDescriptor:pd error:&err];
    MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
    sd.minFilter = sd.magFilter = MTLSamplerMinMagFilterLinear;
    present_sampler = [dev newSamplerStateWithDescriptor:sd];
    renderer_init(dev);
    LOG("[metal] %s\n", dev.name.UTF8String);
}

id<MTLCommandQueue> renderer_queue(void) { return queue; }

// ================================================================== screenshots
// Scanout shows the bytes the game wrote: sample sRGB textures through a non-sRGB view so the
// present/screenshot shaders pass them through instead of decoding to linear.
static id<MTLTexture> raw_view(id<MTLTexture> t) {
    MTLPixelFormat pf = t.pixelFormat;
    MTLPixelFormat raw = pf == MTLPixelFormatRGBA8Unorm_sRGB ? MTLPixelFormatRGBA8Unorm
                       : pf == MTLPixelFormatBGRA8Unorm_sRGB ? MTLPixelFormatBGRA8Unorm : pf;
    if (raw == pf) return t;
    id<MTLTexture> v = [t newTextureViewWithPixelFormat:raw];
    return v ? v : t;
}

static void save_png(const uint8_t *px, NSUInteger w, NSUInteger h, bool gray, const char *path) {
    CGColorSpaceRef cs = gray ? CGColorSpaceCreateDeviceGray() : CGColorSpaceCreateDeviceRGB();
    CGContextRef ctx = CGBitmapContextCreate((void *)px, w, h, 8, gray ? w : w * 4, cs,
                                             (CGBitmapInfo)(gray ? kCGImageAlphaNone : kCGImageAlphaPremultipliedLast));
    CGImageRef img = CGBitmapContextCreateImage(ctx);
    CFURLRef url = CFURLCreateFromFileSystemRepresentation(NULL, (const UInt8 *)path, strlen(path), false);
    CGImageDestinationRef dest = CGImageDestinationCreateWithURL(url, (__bridge CFStringRef)UTTypePNG.identifier, 1, NULL);
    if (dest) {  // (NULL: the folder isn't there)
        CGImageDestinationAddImage(dest, img, NULL);
        CGImageDestinationFinalize(dest);
        CFRelease(dest);
    }
    CFRelease(url); CGImageRelease(img); CGContextRelease(ctx); CGColorSpaceRelease(cs);
}

// Debug: an 8-bit-per-channel 2D texture's alpha channel as a grey PNG.
void renderer_save_texture_alpha(id<MTLTexture> tex, const char *path) {
    if (tex.pixelFormat != MTLPixelFormatRGBA8Unorm && tex.pixelFormat != MTLPixelFormatRGBA8Unorm_sRGB &&
        tex.pixelFormat != MTLPixelFormatBGRA8Unorm)
        return;
    NSUInteger w = tex.width, h = tex.height;
    id<MTLBuffer> buf = [dev newBufferWithLength:w * h * 4 options:MTLResourceStorageModeShared];
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLBlitCommandEncoder> b = [cb blitCommandEncoder];
    [b copyFromTexture:tex sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0) sourceSize:MTLSizeMake(w, h, 1)
              toBuffer:buf destinationOffset:0 destinationBytesPerRow:w * 4 destinationBytesPerImage:w * h * 4];
    [b endEncoding];
    [cb commit];
    [cb waitUntilCompleted];
    const uint8_t *px = buf.contents;
    uint8_t *a = malloc(w * h);
    for (NSUInteger i = 0; i < w * h; i++) a[i] = px[i * 4 + 3];
    save_png(a, w, h, true, path);
    free(a);
}

// Debug: a float texture's colour channels scaled by their largest finite value (<path>_n.png); logs the maxima
// (HDR targets hold values that an 8-bit dump shows as black).
// The texture converted to RGBA32F through the present shader (malloc'd, w * h * 4 floats).
static float *read_float_rgba(id<MTLTexture> tex) {
    NSUInteger w = tex.width, h = tex.height;
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA32Float
                                                                                 width:w height:h mipmapped:NO];
    d.usage = MTLTextureUsageRenderTarget | MTLTextureUsageShaderRead;
    d.storageMode = MTLStorageModeShared;
    id<MTLTexture> dst = [dev newTextureWithDescriptor:d];
    static id<MTLRenderPipelineState> pipe;
    if (!pipe) {
        MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
        id<MTLLibrary> lib = [dev newLibraryWithSource:@(present_src) options:nil error:nil];
        pd.vertexFunction = [lib newFunctionWithName:@"vs"];
        pd.fragmentFunction = [lib newFunctionWithName:@"fs"];
        pd.colorAttachments[0].pixelFormat = MTLPixelFormatRGBA32Float;
        pipe = [dev newRenderPipelineStateWithDescriptor:pd error:nil];
    }
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
    rp.colorAttachments[0].texture = dst;
    rp.colorAttachments[0].loadAction = MTLLoadActionClear;
    rp.colorAttachments[0].storeAction = MTLStoreActionStore;
    id<MTLRenderCommandEncoder> e = [cb renderCommandEncoderWithDescriptor:rp];
    float pv[4] = {renderer_origin_lower_left() ? 1.0f : 0.0f, 0, 0, 0};
    [e setRenderPipelineState:pipe];
    [e setVertexBytes:pv length:sizeof pv atIndex:0];
    [e setFragmentTexture:raw_view(tex) atIndex:0];
    [e setFragmentSamplerState:present_sampler atIndex:0];
    [e drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [e endEncoding];
    [cb commit];
    [cb waitUntilCompleted];
    float *fp = malloc(w * h * 16);
    [dst getBytes:fp bytesPerRow:w * 16 fromRegion:MTLRegionMake2D(0, 0, w, h) mipmapLevel:0];
    return fp;
}

// Debug: how many pixels of the texture have a non-finite colour channel; *first gets the first one (y * w + x).
float *renderer_read_float_rgba(id<MTLTexture> tex) { return read_float_rgba(tex); }

size_t renderer_count_nonfinite(id<MTLTexture> tex, long *first) {
    NSUInteger n = tex.width * tex.height;
    float *fp = read_float_rgba(tex);
    size_t bad = 0;
    *first = -1;
    for (NSUInteger i = 0; i < n; i++)
        if (!isfinite(fp[i * 4]) || !isfinite(fp[i * 4 + 1]) || !isfinite(fp[i * 4 + 2])) {
            if (!bad++) *first = (long)i;
        }
    free(fp);
    return bad;
}

// Debug (NaN trace, FIFA_DBG_FS=<addr>:fffffffe:0): pixels carry the first instruction index that produced a NaN
// as (index & 63, index >> 6, 1); logs how many pixels found one and the most common indices (pc = index * 8).
void renderer_nantrace_report(id<MTLTexture> tex) {
    NSUInteger n = tex.width * tex.height;
    float *fp = read_float_rgba(tex);
    static uint32_t hist[4096];
    memset(hist, 0, sizeof hist);
    size_t found = 0;
    for (NSUInteger i = 0; i < n; i++) {
        if (!(fp[i * 4 + 2] > 0.5f)) continue;
        uint32_t idx = (uint32_t)lroundf(fp[i * 4]) + 64u * (uint32_t)lroundf(fp[i * 4 + 1]);
        hist[idx & 4095]++;
        found++;
    }
    free(fp);
    LOG("[nantrace] %zu/%lu pixels:", found, (unsigned long)n);
    for (int t = 0; t < 6; t++) {
        int best = -1;
        for (int k = 0; k < 4096; k++) if (hist[k] && (best < 0 || hist[k] > hist[best])) best = k;
        if (best < 0) break;
        LOG(" pc %#x (%u)", best * 8, hist[best]);
        hist[best] = 0;
    }
    LOG("\n");
}

static void save_texture_normalized(id<MTLTexture> tex, const char *path) {
    NSUInteger w = tex.width, h = tex.height;
    float *fp = read_float_rgba(tex);
    float mx[3] = {0, 0, 0};
    size_t nonzero = 0, bad = 0;
    for (NSUInteger i = 0; i < w * h; i++) {
        bool nz = false;
        for (int k = 0; k < 3; k++) {
            float v = fp[i * 4 + k];
            if (!isfinite(v)) { bad++; continue; }
            if (v > mx[k]) mx[k] = v;
            nz |= v != 0;
        }
        nonzero += nz;
    }
    float m = fmaxf(mx[0], fmaxf(mx[1], mx[2]));
    uint8_t *px = malloc(w * h * 4);
    for (NSUInteger i = 0; i < w * h * 4; i++) {
        float v = (i & 3) == 3 ? 1.0f : fp[i];
        px[i] = (uint8_t)(m > 0 && isfinite(v) ? fminf(fmaxf(v / ((i & 3) == 3 ? 1.0f : m), 0), 1) * 255.0f + 0.5f : 0);
    }
    char np[512];
    snprintf(np, sizeof np, "%.*s_n.png", (int)(strlen(path) > 4 ? strlen(path) - 4 : strlen(path)), path);
    save_png(px, w, h, false, np);
    LOG("[metal] %s: max %g %g %g, %zu/%lu pixels nonzero, %zu non-finite\n", np, mx[0], mx[1], mx[2], nonzero,
        (unsigned long)(w * h), bad);
    free(px);
    free(fp);
}

// A texture as the window shows it (upright, alpha ignored), as PNG.
void renderer_save_texture(id<MTLTexture> tex, const char *path) {
    NSUInteger w = tex.width, h = tex.height;
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                                                 width:w height:h mipmapped:NO];
    d.usage = MTLTextureUsageRenderTarget | MTLTextureUsageShaderRead;
    d.storageMode = MTLStorageModeShared;
    id<MTLTexture> dst = [dev newTextureWithDescriptor:d];
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
    rp.colorAttachments[0].texture = dst;
    rp.colorAttachments[0].loadAction = MTLLoadActionClear;
    rp.colorAttachments[0].storeAction = MTLStoreActionStore;
    static id<MTLRenderPipelineState> pipe;
    if (!pipe) {
        MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
        id<MTLLibrary> lib = [dev newLibraryWithSource:@(present_src) options:nil error:nil];
        pd.vertexFunction = [lib newFunctionWithName:@"vs"];
        pd.fragmentFunction = [lib newFunctionWithName:@"fs"];
        pd.colorAttachments[0].pixelFormat = MTLPixelFormatRGBA8Unorm;
        pipe = [dev newRenderPipelineStateWithDescriptor:pd error:nil];
    }
    id<MTLRenderCommandEncoder> e = [cb renderCommandEncoderWithDescriptor:rp];
    float pv[4] = {renderer_origin_lower_left() ? 1.0f : 0.0f, 0, 0, 0};
    [e setRenderPipelineState:pipe];
    [e setVertexBytes:pv length:sizeof pv atIndex:0];
    [e setFragmentTexture:raw_view(tex) atIndex:0];
    [e setFragmentSamplerState:present_sampler atIndex:0];
    [e drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [e endEncoding];
    [cb commit];
    [cb waitUntilCompleted];
    uint8_t *px = malloc(w * h * 4);
    [dst getBytes:px bytesPerRow:w * 4 fromRegion:MTLRegionMake2D(0, 0, w, h) mipmapLevel:0];
    save_png(px, w, h, false, path);
    free(px);
    LOG("[metal] saved %s (%lux%lu)\n", path, (unsigned long)w, (unsigned long)h);
    MTLPixelFormat f = tex.pixelFormat;
    if (f == MTLPixelFormatRG11B10Float || f == MTLPixelFormatRGBA16Float || f == MTLPixelFormatRGBA32Float ||
        f == MTLPixelFormatRG16Float || f == MTLPixelFormatR16Float || f == MTLPixelFormatR32Float || f == MTLPixelFormatRG32Float)
        save_texture_normalized(tex, path);
}

// ================================================================== present
static double last_fps_time;

static double now_s(void) { return (double)clock_gettime_nsec_np(CLOCK_UPTIME_RAW) / 1e9; }

void backend_present(NvnTexture *t) {
    renderer_submit();  // same queue: the present pass runs after the frame's work
    TexBackend *b = t ? t->backend : NULL;
    if (b) b->presented = true;
    @autoreleasepool {
        id<CAMetalDrawable> drawable = window_shown ? [layer nextDrawable] : nil;
        if (drawable) {
            id<MTLCommandBuffer> cb = [queue commandBuffer];
            MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
            rp.colorAttachments[0].texture = drawable.texture;
            rp.colorAttachments[0].loadAction = MTLLoadActionClear;
            rp.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, 1);
            rp.colorAttachments[0].storeAction = MTLStoreActionStore;
            id<MTLRenderCommandEncoder> enc = [cb renderCommandEncoderWithDescriptor:rp];
            if (b && b->tex) {  // the image scaled to fit the window, letterboxed
                double dw = drawable.texture.width, dh = drawable.texture.height;
                double s = fmin(dw / b->tex.width, dh / b->tex.height), vw = b->tex.width * s, vh = b->tex.height * s;
                [enc setViewport:(MTLViewport){(dw - vw) / 2, (dh - vh) / 2, vw, vh, 0, 1}];
                [enc setRenderPipelineState:present_pipe];
                float pv[4] = {renderer_origin_lower_left() ? 1.0f : 0.0f, 0, 0, 0};
                [enc setVertexBytes:pv length:sizeof pv atIndex:0];
                [enc setFragmentTexture:raw_view(b->tex) atIndex:0];
                [enc setFragmentSamplerState:present_sampler atIndex:0];
                [enc drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
            }
            [enc endEncoding];
            [cb presentDrawable:drawable];
            [cb commit];
        }
    }
    frames++;
    total_frames++;
    g_frame_counter++;
    // FIFA_SCREENSHOT_EVERY=<n>: save every nth presented image to build/frame_<n>.png
    static long shot_every = -1;
    if (shot_every < 0) shot_every = ENV("FIFA_SCREENSHOT_EVERY") ? atol(ENV("FIFA_SCREENSHOT_EVERY")) : 0;
    if (shot_every && b && b->tex && total_frames % shot_every == 0) {
        char path[64];
        snprintf(path, sizeof path, "build/frame_%05llu.png", (unsigned long long)total_frames);
        renderer_save_texture(b->tex, path);
    }
    char *shot = input_take_shot();  // FIFA_CONTROL "shot"
    if (shot) {
        char tmp[300];
        snprintf(tmp, sizeof tmp, "%s.tmp", shot);
        if (b && b->tex) {
            renderer_save_texture(b->tex, tmp);
            rename(tmp, shot);
        }
        free(shot);
        input_shot_done();
    }
    static long debug_at = -1;  // FIFA_DEBUG_FRAME=<n>: log (and dump passes of) frame n
    if (debug_at < 0) debug_at = ENV("FIFA_DEBUG_FRAME") ? atol(ENV("FIFA_DEBUG_FRAME")) : 0;
    // or FIFA_CONTROL "debug"; with a FIFA_DBG_FS register probe, the first frame after it whose previous frame drew
    // the probed program at least 4 times (so the probe has pixels to report)
    static bool debug_armed;
    debug_armed |= input_take_debug();
    bool probe_ready = g_probe_fs_draws < 0 || g_probe_fs_draws >= 4;
    g_debug_frame = (debug_at && total_frames == (uint64_t)debug_at) || (debug_armed && probe_ready);
    if (g_debug_frame) debug_armed = false;
    if (g_probe_fs_draws > 0) g_probe_fs_draws = 0;
    if (g_debug_frame) LOG("===== debug frame %llu =====\n", (unsigned long long)total_frames);
    double now = now_s();
    if (now - last_fps_time >= 2.0) {
        if (last_fps_time > 0)
            LOG("[metal] frame %llu %.1f fps (%s)\n", (unsigned long long)total_frames, frames / (now - last_fps_time),
                renderer_stats());
        renderer_profile_report((int)frames);
        frames = 0;
        last_fps_time = now;
    }
}

// ================================================================== window (main thread)
@interface FifaView : NSView
@end
@implementation FifaView
- (BOOL)wantsUpdateLayer { return YES; }
- (CALayer *)makeBackingLayer { return layer; }
- (BOOL)acceptsFirstResponder { return YES; }
- (void)keyDown:(NSEvent *)e { if (!e.isARepeat) input_key(e.keyCode, true); }  // (handled here, so no beep)
- (void)keyUp:(NSEvent *)e { input_key(e.keyCode, false); }
@end

@interface FifaDelegate : NSObject <NSApplicationDelegate, NSWindowDelegate>
@end
@implementation FifaDelegate
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)a { (void)a; return YES; }
- (void)applicationWillTerminate:(NSNotification *)n { (void)n; LOG("window closed\n"); _exit(0); }
- (void)windowDidResize:(NSNotification *)n { (void)n; [self updateDrawable]; }
- (void)windowDidResignKey:(NSNotification *)n { (void)n; input_release_all(); }
- (void)windowDidChangeBackingProperties:(NSNotification *)n { (void)n; [self updateDrawable]; }
- (void)updateDrawable {
    NSSize s = [window.contentView convertSizeToBacking:window.contentView.bounds.size];
    layer.drawableSize = CGSizeMake(s.width, s.height);
}
@end

// Runs the window on the main thread; never returns. FIFA_HIDDEN=1 keeps the window hidden (frames are still
// rendered, only not shown). FIFA_WINDOW=<w>x<h> sets its size (default 1280x720).
void ui_run(void) {
    @autoreleasepool {
        dev = MTLCreateSystemDefaultDevice();
        if (!dev) FATAL("no Metal device\n");
        layer = [CAMetalLayer layer];
        layer.device = dev;
        layer.pixelFormat = MTLPixelFormatBGRA8Unorm;
        layer.framebufferOnly = YES;
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        static FifaDelegate *delegate;
        delegate = [FifaDelegate new];
        app.delegate = delegate;
        int w = 1280, h = 720;
        if (getenv("FIFA_WINDOW")) sscanf(getenv("FIFA_WINDOW"), "%dx%d", &w, &h);
        window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, w, h)
                                             styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                                                       NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
                                               backing:NSBackingStoreBuffered defer:NO];
        window.title = @"fifa-web";
        window.delegate = delegate;
        FifaView *view = [[FifaView alloc] initWithFrame:NSMakeRect(0, 0, w, h)];
        view.wantsLayer = YES;
        window.contentView = view;
        layer.contentsScale = window.backingScaleFactor;
        [delegate updateDrawable];
        [window center];
        if (!getenv("FIFA_HIDDEN")) {
            [window makeKeyAndOrderFront:nil];
            [app activateIgnoringOtherApps:YES];
            window_shown = true;
        }
        // The guest's timer and vsync waits need precise sleeps; App Nap stretches them when the window isn't frontmost.
        static id activity;
        activity = [[NSProcessInfo processInfo] beginActivityWithOptions:NSActivityUserInitiated | NSActivityLatencyCritical
                                                                  reason:@"Game emulation"];
        atomic_store(&ui_ready, true);
        [app run];
    }
    exit(0);
}

// Debug: per mip level, how many slices of the texture have any nonzero byte, and the first texel's bytes.
void renderer_texture_probe(NvnTexture *t) {
    id<MTLTexture> tex = tex_backend(t)->tex;  // (no upload, not marked GPU-owned)
    if (!tex) return;
    bool is3d = tex.textureType == MTLTextureType3D;
    NSUInteger slices = tex.textureType == MTLTextureTypeCube ? 6 : tex.textureType == MTLTextureTypeCubeArray ? 6 * tex.arrayLength
                      : is3d ? tex.depth : tex.arrayLength;
    LOG("[probe] type %lu fmt %lu %lux%lu levels %lu slices %lu:", (unsigned long)tex.textureType, (unsigned long)tex.pixelFormat,
        (unsigned long)tex.width, (unsigned long)tex.height, (unsigned long)tex.mipmapLevelCount, (unsigned long)slices);
    for (NSUInteger l = 0; l < tex.mipmapLevelCount; l++) {
        NSUInteger w = MAX(tex.width >> l, 1), h = MAX(tex.height >> l, 1), bpr = w * 16, len = bpr * h;
        id<MTLBuffer> buf = [dev newBufferWithLength:len * slices options:MTLResourceStorageModeShared];
        id<MTLCommandBuffer> cb = [queue commandBuffer];
        id<MTLBlitCommandEncoder> b = [cb blitCommandEncoder];
        NSUInteger bpp = 0;
        switch (tex.pixelFormat) {
        case MTLPixelFormatRGBA32Float: bpp = 16; break;
        case MTLPixelFormatRGBA16Float: case MTLPixelFormatRG32Float: bpp = 8; break;
        default: bpp = 4; break;
        }
        NSUInteger ls = is3d ? MAX(slices >> l, 1) : slices;  // (3D: the level's depth slices)
        for (NSUInteger s = 0; s < ls; s++)
            [b copyFromTexture:tex sourceSlice:is3d ? 0 : s sourceLevel:l sourceOrigin:MTLOriginMake(0, 0, is3d ? s : 0) sourceSize:MTLSizeMake(w, h, 1)
                      toBuffer:buf destinationOffset:s * len destinationBytesPerRow:w * bpp destinationBytesPerImage:w * h * bpp];
        [b endEncoding];
        [cb commit];
        [cb waitUntilCompleted];
        const uint32_t *p = buf.contents;
        int nz = 0;
        size_t rgb = 0;  // RGB10A2: texels with nonzero colour (alpha alone doesn't count)
        bool a2 = tex.pixelFormat == MTLPixelFormatRGB10A2Unorm || tex.pixelFormat == MTLPixelFormatRGB10A2Uint;
        for (NSUInteger s = 0; s < ls; s++) {
            bool any = false;
            for (NSUInteger i = 0; i < w * h * bpp / 4; i++) {
                uint32_t v = p[s * len / 4 + i];
                any |= v != 0;
                rgb += a2 && (v & 0x3fffffffu);
            }
            nz += any;
        }
        LOG(" L%lu %d/%lu (%08x %08x)", (unsigned long)l, nz, (unsigned long)ls, p[0], bpp > 4 ? p[1] : 0);
        if (a2) LOG(" rgb %zu/%lu", rgb, (unsigned long)(w * h * ls));
    }
    LOG("\n");
}

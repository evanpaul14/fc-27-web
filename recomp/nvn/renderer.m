// Executes recorded NVN command lists with Metal. From LAS Recomp (runtime/nvn/renderer.m, MIT), without its
// game-specific parts (widescreen, mirror mode, highlights, texture patches). Guest addresses go through gptr()
// (GP() where 0 must stay NULL); GPU memory is the memory pools, each wrapped in its own MTLBuffer (metal.m).
#include <pthread.h>
#include <dirent.h>
#include <unistd.h>
#import <Metal/Metal.h>
#include <mach/mach_time.h>
#include <stdlib.h>
#include <string.h>
#include <float.h>
#include <math.h>
#include <sys/stat.h>
#include <mach/mach.h>

#include "maxwell/shader.h"
#include "nvn.h"
#include "renderer.h"
#include "../rt/input.h"

static id<MTLDevice> dev;
static id<MTLCommandBuffer> cmdbuf;
static id<MTLRenderCommandEncoder> enc;
static id<MTLRenderPipelineState> clear_pipes[64];
static NSMutableDictionary<NSString *, id> *cache;
// (stats) GPU wait time by cause: 0 = guest fence (nvnSyncWait), 1 = full flush (CPU reads GPU results / ring), 2 = sync all
static uint64_t stat_wait_by[3], stat_wait_n[3];
static uint64_t skip_why[6];  // no program, program not ready, pipeline compiling, compile started, pipeline failed, draw mode
#define SKIP(n) (stat_skipped++, skip_why[n]++, skip_log(n))
static void skip_log(int n);
static uint64_t stat_draws, stat_skipped, stat_passes, stat_dispatches, stat_wait_ns, stat_flushes, stat_feedback_copies;
static double stat_gpu_s;
static uint64_t g_gpu_total_ns;  // GPU time of all completed command buffers
// (forward: texture-lifetime cleanup and stats, defined further down)
static int g_views_live;
static void views_forget(void *base);
static NSMutableDictionary *g_snap_copies;
static NvnTexture *snap_tex[8];
static bool snap_ok[8];
static uint64_t op_counts[CMD_COUNT];
extern bool g_debug_frame;
static char stats_text[320];

// ------------------------------------------------------------------ state
typedef struct {
    NvnTexture *color[8];
    NvnTextureView cview[8];
    uint8_t has_cview[8];
    int ncolor;
    NvnTexture *depth;
    NvnTextureView dview;
    uint8_t has_dview;
} RtState;

typedef struct { uint64_t addr, size; } Range;

static struct {
    RtState rt;
    bool rt_dirty;
    NvnProgram *program;
    uint8_t blend[8][8];
    uint32_t channel_mask;
    uint8_t color_state[4], ds[8], ms[0x18], poly[4];
    float poly_offset[3];  // factor, units, clamp (nvnCommandBufferSetPolygonOffsetClamp)
    uint32_t attrib[16];
    int nattrib;
    uint32_t stream[16][2];
    int nstream;
    Range vbuf[16], ubuf[6][18], sbuf[6][16];
    uint64_t tex[6][32], img[6][8];
    uint64_t septex[6][128], sepsmp[6][32];  // separate texture / sampler handles
    NvnDescPool *tpool, *spool;
    float viewport[16][4];
    int32_t scissor[16][4];
    float depth_range[16][2];
    float blend_color[4];
    uint32_t stencil_ref[2], stencil_mask[2], stencil_vmask[2];
    bool prim_restart;
    uint32_t restart_index;
    uint64_t copy_row_stride, copy_image_stride;
} st;

uint32_t nvn_window_origin(void);
bool renderer_origin_lower_left(void) { return nvn_window_origin() == 0; }

static double stat_compile_ms;  // time spent building shaders/pipelines (first-use stutter)
static int stat_compiles;
static double stat_compile_wait_ms;  // draws into one-shot targets waiting for their compiles (one_shot_target)
static inline double now_ms(void) { return (double)clock_gettime_nsec_np(CLOCK_UPTIME_RAW) / 1e6; }
#define TIMED_COMPILE(expr) ({ double t0_ = now_ms(); __auto_type r_ = (expr); stat_compile_ms += now_ms() - t0_; stat_compiles++; r_; })

const char *renderer_stats(void) {
    static int dumps;
    if (dumps++ < 3) {
        LOG("[metal] command counts:");
        for (int i = 0; i < CMD_COUNT; i++) if (op_counts[i]) LOG(" %d:%llu", i, op_counts[i]);
        LOG("\n");
    }
    extern int g_tex_live;
    extern uint64_t g_tex_live_bytes;
    struct task_vm_info vmi;
    mach_msg_type_number_t vn = TASK_VM_INFO_COUNT;
    uint64_t foot = task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vmi, &vn) == KERN_SUCCESS ? vmi.phys_footprint : 0;
    snprintf(stats_text, sizeof stats_text, "%llu draws, %llu skipped (%llu/%llu/%llu/%llu/%llu/%llu), %llu passes, %llu dispatches, %llu flushes, gpu %.0fms, wait %.0fms, compile %d/%.0fms wait %.0fms, fbcopy %llu | tex %d (%.0f MB) views %d snaps %lu mem %.0f MB | waits fence %.0fms/%llu flush %.0fms/%llu all %.0fms/%llu", stat_draws, stat_skipped,
             skip_why[0], skip_why[1], skip_why[2], skip_why[3], skip_why[4], skip_why[5],
             stat_passes, stat_dispatches, stat_flushes, stat_gpu_s * 1000.0, (double)stat_wait_ns / 1e6 * 125.0 / 3.0, stat_compiles, stat_compile_ms, stat_compile_wait_ms, stat_feedback_copies,
             g_tex_live, g_tex_live_bytes / 1048576.0, g_views_live, (unsigned long)(g_snap_copies ? g_snap_copies.count : 0), foot / 1048576.0, stat_wait_by[0] / 24e3, (unsigned long long)stat_wait_n[0], stat_wait_by[1] / 24e3, (unsigned long long)stat_wait_n[1], stat_wait_by[2] / 24e3, (unsigned long long)stat_wait_n[2]);
    stat_compiles = 0;
    stat_feedback_copies = 0;
    stat_compile_ms = stat_compile_wait_ms = 0;
    memset(skip_why, 0, sizeof skip_why);
    stat_draws = stat_skipped = stat_passes = stat_dispatches = stat_flushes = stat_wait_ns = 0;
    memset(stat_wait_by, 0, sizeof stat_wait_by); memset(stat_wait_n, 0, sizeof stat_wait_n);
    stat_gpu_s = 0;
    return stats_text;
}

static void st_defaults(void) {
    memset(&st, 0, sizeof st);
    for (int i = 0; i < 16; i++) {
        st.viewport[i][2] = 1280; st.viewport[i][3] = 720;
        st.scissor[i][2] = 16384; st.scissor[i][3] = 16384;
        st.depth_range[i][1] = 1.0f;
    }
    st.channel_mask = 0xFFFFFFFF;
    st.stencil_mask[0] = st.stencil_mask[1] = 0xFF;
    st.stencil_vmask[0] = st.stencil_vmask[1] = 0xFF;
    st.ds[1] = 1; st.ds[2] = 2;
    st.poly[1] = 1; st.poly[2] = 2;
    for (int i = 0; i < 8; i++) { st.blend[i][1] = 2; st.blend[i][2] = 1; st.blend[i][3] = 2; st.blend[i][4] = 1; st.blend[i][5] = 1; st.blend[i][6] = 1; }
}

void renderer_init(id<MTLDevice> d) {
    dev = d;
    cache = [NSMutableDictionary new];
    st_defaults();
}

static int g_passes_in_cb;  // render passes encoded into the open command buffer (chunked commits)
static id<MTLCommandBuffer> cb(void) {
    if (!cmdbuf) cmdbuf = [renderer_queue() commandBuffer];
    return cmdbuf;
}

static float pass_sx = 1, pass_sy = 1;  // allocation scale of the current pass's attachments
// vp_* is the mapping for the current draw: physical = guest * s + off (LAS Recomp's widescreen UI moved draws;
// here it is the attachments' allocation scale, always 1).
static float vp_sx = 1, vp_sy = 1, vp_ox = 0, vp_oy = 0;
static float tex_sc[2][64][2];          // per bound texture slot: allocation scale (for shaders' texel math)
static uint32_t tex_wh[2][64][2];       // per bound texture slot: guest size
#define RSCALE_BUFFER_INDEX 18

// FIFA_GPU_PROFILE=1: every render pass and compute dispatch gets its own command buffer; GPU time is summed per
// tag (the pass's size and first fragment program, or the compute program) and the top ones are logged with the
// fps line (renderer_profile_report). FIFA_GPU_PROFILE=2: every draw ends its pass, tagged with its programs
// (inflated more by reloading the attachments per draw; compare programs within a run).
// FIFA_CONTROL "prof <n>" changes the level while running.
static int gpu_profile(void) {
    static int on = -1;
    if (on < 0) { const char *e = getenv("FIFA_GPU_PROFILE"); on = e ? (atoi(e) == 2 ? 2 : 1) : 0; }
    int c = input_profile_mode();
    return c >= 0 ? c : on;
}
static uint64_t prof_pass_fs, prof_pass_vs;  // first programs drawn in the open pass
static void commit_cb(const char *tag);

static int debug_pass_no;
static void end_pass(void) {
    if (enc) {
        [enc endEncoding];
        enc = nil;
        if (gpu_profile()) {
            char tag[64];
            unsigned w = st.rt.ncolor && st.rt.color[0] ? st.rt.color[0]->width : 0;
            unsigned h = st.rt.ncolor && st.rt.color[0] ? st.rt.color[0]->height : 0;
            if (gpu_profile() == 2 && prof_pass_fs)
                snprintf(tag, sizeof tag, "draw %ux%u vs %llx fs %llx", w, h, (unsigned long long)prof_pass_vs,
                         (unsigned long long)prof_pass_fs);
            else
                snprintf(tag, sizeof tag, "pass %ux%u fs %llx", w, h, (unsigned long long)prof_pass_fs);
            commit_cb(tag);
        }
        prof_pass_fs = prof_pass_vs = 0;
        if (g_debug_frame && st.rt.ncolor && st.rt.color[0] && st.rt.color[0]->backend) {
            id<MTLTexture> t = renderer_texture(st.rt.color[0], nil, true);
            id<MTLCommandBuffer> c = cmdbuf;
            cmdbuf = nil;
            [c commit];
            [c waitUntilCompleted];
            char path[256];
            snprintf(path, sizeof path, "build/pass_%03d.png", debug_pass_no);
            renderer_save_texture(t, path);
            snprintf(path, sizeof path, "build/pass_%03d_a.png", debug_pass_no++);
            renderer_save_texture_alpha(t, path);
        }
    }
}

static void ring_reset(void);
// Submitted work runs asynchronously; last_cb is the most recently committed command buffer (the queue
// executes in order, so waiting for it waits for everything before it). Other guest threads (nvnSyncWait)
// read last_cb, so it is guarded by cb_lock; everything else here runs on the submitting thread.
static id<MTLCommandBuffer> last_cb;
static pthread_mutex_t cb_lock = PTHREAD_MUTEX_INITIALIZER;
// Fences: each committed command buffer gets a sequence number; a fence remembers the number of the submission
// that contains it, and nvnSyncWait waits only for that one (not for everything submitted since), so the game's
// CPU work on frame N+1 overlaps the GPU's frame N as on the Switch.
#define CB_HIST 256
static uint64_t submit_seq;
static id<MTLCommandBuffer> cb_hist[CB_HIST];

static int g_wait_kind;
static void wait_cb(id<MTLCommandBuffer> c) {
    if (!c) return;
    uint64_t t0 = mach_absolute_time();
    [c waitUntilCompleted];
    uint64_t d = mach_absolute_time() - t0;
    stat_wait_ns += d;
    stat_wait_by[g_wait_kind] += d;
    if (d > 24000) stat_wait_n[g_wait_kind]++;  // (> ~1 ms counts as a real wait)
}

// Textures the game destroyed (any thread): their cached views and snapshot copies are dropped on the render thread.
static pthread_mutex_t gone_lock = PTHREAD_MUTEX_INITIALIZER;
static struct { void *nvn, *mtl; } gone[4096];
static int ngone;
void renderer_texture_gone(void *nvn_tex, void *mtl_tex) {
    pthread_mutex_lock(&gone_lock);
    if (ngone < 4096) { gone[ngone].nvn = nvn_tex; gone[ngone].mtl = mtl_tex; ngone++; }
    pthread_mutex_unlock(&gone_lock);
}
static void drain_gone(void) {
    pthread_mutex_lock(&gone_lock);
    int n = ngone;
    static struct { void *nvn, *mtl; } local[4096];
    memcpy(local, gone, sizeof local[0] * n);
    ngone = 0;
    pthread_mutex_unlock(&gone_lock);
    for (int i = 0; i < n; i++) {
        if (local[i].mtl) views_forget(local[i].mtl);
        if (local[i].nvn) {
            if (g_snap_copies) [g_snap_copies removeObjectForKey:[NSValue valueWithPointer:local[i].nvn]];
            for (int k = 0; k < 8; k++) if (snap_tex[k] == local[i].nvn) { snap_tex[k] = NULL; snap_ok[k] = false; }
        }
    }
}

#define PROF_N 512
static struct { char tag[64]; double s; int n; } prof[PROF_N];
static int nprof;
static pthread_mutex_t prof_lock = PTHREAD_MUTEX_INITIALIZER;
static void prof_add(const char *tag, double s) {
    pthread_mutex_lock(&prof_lock);
    int i = 0;
    while (i < nprof && strcmp(prof[i].tag, tag)) i++;
    if (i == nprof && nprof < PROF_N) {
        snprintf(prof[nprof].tag, sizeof prof[0].tag, "%s", tag);
        prof[nprof].s = 0;
        prof[nprof++].n = 0;
    }
    if (i < nprof) { prof[i].s += s; prof[i].n++; }
    pthread_mutex_unlock(&prof_lock);
}

static int prof_cmp(const void *a, const void *b) {
    double x = ((const typeof(prof[0]) *)a)->s, y = ((const typeof(prof[0]) *)b)->s;
    return x < y ? 1 : x > y ? -1 : 0;
}

void renderer_profile_report(void) {
    if (!gpu_profile()) return;
    pthread_mutex_lock(&prof_lock);
    qsort(prof, nprof, sizeof prof[0], prof_cmp);
    for (int i = 0; i < nprof && i < (gpu_profile() == 2 ? 40 : 12); i++) LOG("[prof] %6.1f ms %5d x  %s\n", prof[i].s * 1e3, prof[i].n, prof[i].tag);
    nprof = 0;
    pthread_mutex_unlock(&prof_lock);
}

// Commit recorded work without waiting.
void renderer_submit(void) {
    end_pass();
    drain_gone();
    commit_cb(NULL);
}

// Commit the open command buffer (tag: account its GPU time to that profile entry).
static void commit_cb(const char *tag) {
    if (cmdbuf) {
        id<MTLCommandBuffer> c = cmdbuf;
        cmdbuf = nil;
        char *ptag = tag ? strdup(tag) : NULL;
        [c addCompletedHandler:^(id<MTLCommandBuffer> done) {
            stat_gpu_s += done.GPUEndTime - done.GPUStartTime;
            __atomic_add_fetch(&g_gpu_total_ns, (uint64_t)((done.GPUEndTime - done.GPUStartTime) * 1e9), __ATOMIC_RELAXED);
            if (ptag) { prof_add(ptag, done.GPUEndTime - done.GPUStartTime); free(ptag); }
            if (done.error) LOG("[metal] command buffer error: %s\n", done.error.localizedDescription.UTF8String);
        }];
        [c commit];
        g_passes_in_cb = 0;
        pthread_mutex_lock(&cb_lock);
        last_cb = c;
        submit_seq++;
        cb_hist[submit_seq % CB_HIST] = c;
        pthread_mutex_unlock(&cb_lock);
        stat_flushes++;
    }
}

// Finish all recorded GPU work (the CPU is about to read GPU results, or reuse the ring).
void renderer_flush(void) {
    renderer_submit();
    pthread_mutex_lock(&cb_lock);
    id<MTLCommandBuffer> c = last_cb;
    pthread_mutex_unlock(&cb_lock);
    g_wait_kind = 1;
    wait_cb(c);
    g_wait_kind = 0;
    ring_reset();
}

// Wait for submitted work without committing anything new (nvnSyncWait / QueueFinish).
// Callable from any thread: everything submitted so far was committed by renderer_submit.
void renderer_sync(void) {
    pthread_mutex_lock(&cb_lock);
    id<MTLCommandBuffer> c = last_cb;
    pthread_mutex_unlock(&cb_lock);
    g_wait_kind = 2;
    wait_cb(c);
    g_wait_kind = 0;
}

// Sequence number a fence placed now belongs to: the pending (uncommitted) work's submission, else the last one.
uint64_t renderer_fence_seq(void) {
    pthread_mutex_lock(&cb_lock);
    uint64_t q = submit_seq + (cmdbuf ? 1 : 0);
    pthread_mutex_unlock(&cb_lock);
    return q;
}

// Wait until submission `seq` has completed (0 = nothing to wait for).
void renderer_wait_seq(uint64_t seq) {
    if (!seq) return;
    if (ENV("FIFA_LOG_FENCES")) {  // (debug) how far behind the newest submission the waited fence is
        extern uint64_t renderer_present_count(void);
        static uint64_t f0, hist[6], frames_seen;
        uint64_t f = renderer_present_count();
        uint64_t pend = submit_seq + (cmdbuf ? 1 : 0);
        uint64_t age = pend > seq ? pend - seq : 0;
        bool done_already = false;
        pthread_mutex_lock(&cb_lock);
        if (seq <= submit_seq && submit_seq - seq < CB_HIST && cb_hist[seq % CB_HIST])
            done_already = cb_hist[seq % CB_HIST].status >= MTLCommandBufferStatusCompleted;
        pthread_mutex_unlock(&cb_lock);
        hist[age > 4 ? 4 : age]++;
        if (done_already) hist[5]++;
        if (f - f0 >= 300) {
            frames_seen = f - f0;
            LOG("[fences] per %llu frames: waits by age (submissions behind newest) 0:%llu 1:%llu 2:%llu 3:%llu 4+:%llu, already done %llu; submissions now %llu\n",
                (unsigned long long)frames_seen, hist[0], hist[1], hist[2], hist[3], hist[4], hist[5], (unsigned long long)submit_seq);
            memset(hist, 0, sizeof hist);
            f0 = f;
        }
    }
    pthread_mutex_lock(&cb_lock);
    id<MTLCommandBuffer> c = nil;
    if (seq > submit_seq) c = last_cb;  // not committed yet (other thread): wait for everything committed so far
    else if (submit_seq - seq < CB_HIST) c = cb_hist[seq % CB_HIST];
    pthread_mutex_unlock(&cb_lock);
    wait_cb(c);
}

// ------------------------------------------------------------------ textures with views
// Texture view cache: views are made per bind otherwise (an allocation per draw). Keyed by the base texture and
// the view parameters; a cached view retains its base, so a key's base pointer can't be reused while cached.
#define VIEW_TAB 16384
static struct { uint64_t key; void *base; void *view; } view_tab[VIEW_TAB];

// Views keep their base texture alive: when the game frees a texture its views must go too (they piled up -
// render targets of a 32:9 window held long after the game dropped them - and a new texture at the same address
// could get a stale view). Entries are tombstoned (key ~0) so probe chains stay intact.
static void views_forget(void *base) {
    for (int i = 0; i < VIEW_TAB; i++)
        if (view_tab[i].base == base && view_tab[i].view) {
            CFRelease(view_tab[i].view);
            view_tab[i].view = NULL;
            view_tab[i].base = NULL;
            view_tab[i].key = ~0ull;
            g_views_live--;
        }
}

static id<MTLTexture> view_cached(id<MTLTexture> base, uint64_t params, id<MTLTexture> (^make)(void)) {
    uint64_t k = ((uint64_t)(uintptr_t)(__bridge void *)base * 0x9E3779B97F4A7C15ull) ^ (params * 0xC2B2AE3D27D4EB4Full);
    if (!k) k = 1;
    if (k == ~0ull) k = 2;
    // up to 8 probes; a miss is stored in the first free or tombstoned slot of those, else over the first one.
    // (It used to go to the slot past the 8 probes - never found again - and tombstones were never reused: on the
    // overworld, where textures stream in and out, more and more binds made a new view every time, the slowdown
    // that grew with play time until a level change - user report 2026-10-08, 32:9.)
    uint32_t start = (uint32_t)(k ^ (k >> 32)) & (VIEW_TAB - 1), slot = start;
    int free_slot = -1;
    for (int probe = 0; probe < 8; probe++, slot = (slot + 1) & (VIEW_TAB - 1)) {
        if (view_tab[slot].key == k && view_tab[slot].base == (__bridge void *)base) return (__bridge id<MTLTexture>)view_tab[slot].view;
        if (!view_tab[slot].key || view_tab[slot].key == ~0ull) {
            if (free_slot < 0) free_slot = (int)slot;
            if (!view_tab[slot].key) break;  // (end of the chain)
        }
    }
    slot = free_slot >= 0 ? (uint32_t)free_slot : start;
    id<MTLTexture> v = make();
    if (!v) return base;
    if (ENV("FIFA_LOG_VIEWCREATE")) {  // (debug: what keeps making new views - overworld soak, views pile up)
        extern uint64_t renderer_present_count(void);
        static int n;
        if (renderer_present_count() > (uint64_t)atol(ENV("FIFA_LOG_VIEWCREATE")) && n++ < 400)
            LOG("[viewnew] f%llu base %p %s %lux%lu fmt %lu type %lu params %#llx live %d\n", (unsigned long long)renderer_present_count(),
                (__bridge void *)base, base.label.UTF8String, (unsigned long)base.width, (unsigned long)base.height,
                (unsigned long)base.pixelFormat, (unsigned long)base.textureType, (unsigned long long)params, g_views_live);
    }
    if (view_tab[slot].view) { CFRelease(view_tab[slot].view); g_views_live--; }  // evict (8 probes all taken)
    g_views_live++;
    view_tab[slot].key = k;
    view_tab[slot].base = (__bridge void *)base;
    view_tab[slot].view = (void *)CFBridgingRetain(v);
    return v;
}

static id<MTLTexture> view_texture(NvnTexture *t, const NvnTextureView *v, bool has_view, bool render,
                                   id<MTLBlitCommandEncoder> blit) {
    id<MTLTexture> base = renderer_texture(t, blit, render);
    if (!has_view || !v->setmask) return base;
    MTLPixelFormat pf = (v->setmask & 4) ? renderer_pixel_format(v->format) : base.pixelFormat;
    NSUInteger lv0 = (v->setmask & 1) ? v->base_level : 0;
    NSUInteger nlv = (v->setmask & 1) && v->num_levels ? v->num_levels : base.mipmapLevelCount - lv0;
    NSUInteger ly0 = (v->setmask & 2) ? v->min_layer : 0;
    NSUInteger nly = (v->setmask & 2) && v->num_layers ? v->num_layers : base.arrayLength - ly0;
    bool cube = base.textureType == MTLTextureTypeCube || base.textureType == MTLTextureTypeCubeArray;
    NSUInteger slices = base.arrayLength * (cube ? 6 : 1);
    if (base.textureType == MTLTextureTypeCube && !render) { ly0 = 0; nly = 6; }  // (sampled: the whole cube)
    if (ly0 >= slices) ly0 = 0;
    if (ly0 + nly > slices) nly = slices - ly0;  // (a cube face as a render target: from that face on)
    MTLTextureType type = base.textureType;
    if (render && (type == MTLTextureType2DArray || type == MTLTextureTypeCube) && nly == 1) type = MTLTextureType2D;
    if (lv0 + nlv > base.mipmapLevelCount) nlv = base.mipmapLevelCount - lv0;
    if (pf == base.pixelFormat && lv0 == 0 && nlv == base.mipmapLevelCount && ly0 == 0 && type == base.textureType)
        return base;
    static int vlog;  // FIFA_LOG_VIEWS: texture views that change the pixel format
    if (pf != base.pixelFormat && vlog < 20 && getenv("FIFA_LOG_VIEWS")) {
        vlog++;
        LOG("[view] tex fmt %#x (mtl %lu) viewed as nvn %#x (mtl %lu)\n", t->format, (unsigned long)base.pixelFormat, v->format, (unsigned long)pf);
    }
    NSUInteger ns = type == MTLTextureType2D ? 1 : nly;
    uint64_t params = (uint64_t)pf | (uint64_t)type << 16 | (uint64_t)lv0 << 24 | (uint64_t)nlv << 32 | (uint64_t)ly0 << 40 | (uint64_t)ns << 52;
    id<MTLTexture> got = view_cached(base, params, ^{
        return [base newTextureViewWithPixelFormat:pf textureType:type levels:NSMakeRange(lv0, nlv) slices:NSMakeRange(ly0, ns)];
    });
    if (got == base && pf != base.pixelFormat) {
        static int nfail;
        if (nfail++ < 10) LOG("[view] FAILED %lu -> %lu (nvn %#x -> %#x)\n", (unsigned long)base.pixelFormat, (unsigned long)pf, t->format, v->format);
    }
    return got;
}

// ------------------------------------------------------------------ render passes
static __unsafe_unretained id last_bias_enc;  // encoder the depth bias was last set on

static void begin_pass(void) {
    if (enc && !st.rt_dirty) return;
    end_pass();
    st.rt_dirty = false;
    MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
    int w = 0, h = 0;
    for (int i = 0; i < st.rt.ncolor; i++) {
        NvnTexture *t = st.rt.color[i];
        if (!t) continue;
        renderer_mark_gpu_owned(t);
        id<MTLTexture> tex = view_texture(t, &st.rt.cview[i], st.rt.has_cview[i], true, nil);
        rp.colorAttachments[i].texture = tex;
        rp.colorAttachments[i].loadAction = MTLLoadActionLoad;
        rp.colorAttachments[i].storeAction = MTLStoreActionStore;
        if (!w) renderer_texture_scale(t, &pass_sx, &pass_sy);
        w = (int)tex.width; h = (int)tex.height;
    }
    if (st.rt.depth) {
        renderer_mark_gpu_owned(st.rt.depth);
        id<MTLTexture> tex = view_texture(st.rt.depth, &st.rt.dview, st.rt.has_dview, true, nil);
        if (renderer_format_is_depth(st.rt.depth->format) && st.rt.depth->format != 0x31) {
            rp.depthAttachment.texture = tex;
            rp.depthAttachment.loadAction = MTLLoadActionLoad;
            rp.depthAttachment.storeAction = MTLStoreActionStore;
        }
        if (renderer_format_has_stencil(st.rt.depth->format)) {
            rp.stencilAttachment.texture = tex;
            rp.stencilAttachment.loadAction = MTLLoadActionLoad;
            rp.stencilAttachment.storeAction = MTLStoreActionStore;
        }
        if (!w) { renderer_texture_scale(st.rt.depth, &pass_sx, &pass_sy); w = (int)tex.width; h = (int)tex.height; }
    }
    if (!w) {
        pass_sx = pass_sy = 1;
        rp.renderTargetWidth = 16; rp.renderTargetHeight = 16;
        rp.defaultRasterSampleCount = 1;
    }
    // Commit the frame in chunks: the GPU starts on the first passes while the CPU still encodes the rest. The
    // game waits for its previous frame before it submits the next one, so with one command buffer per frame the
    // GPU sat idle during our encoding and a frame cost GPU + encoding time (5120x1440 with the GPU clocked down:
    // ~15.5 + a few ms -> missed vsync -> 30 fps). FIFA_CHUNK_PASSES=<n> (0 = one buffer per frame).
    static int chunk = -1;
    if (chunk < 0) { const char *e = ENV("FIFA_CHUNK_PASSES"); chunk = e ? atoi(e) : 8; }
    if (chunk > 0 && cmdbuf && ++g_passes_in_cb >= chunk) renderer_submit();
    enc = [cb() renderCommandEncoderWithDescriptor:rp];
    vp_sx = pass_sx; vp_sy = pass_sy; vp_ox = 0; vp_oy = 0;
    stat_passes++;
}

// Physical size of the current pass (guest size times the attachments' allocation scale).
static void pass_size(int *w, int *h) {
    *w = *h = 0;
    for (int i = 0; i < st.rt.ncolor && !*w; i++)
        if (st.rt.color[i]) { *w = (int)st.rt.color[i]->width; *h = (int)st.rt.color[i]->height; }
    if (!*w && st.rt.depth) { *w = (int)st.rt.depth->width; *h = (int)st.rt.depth->height; }
    *w = (int)lroundf(*w * pass_sx);
    *h = (int)lroundf(*h * pass_sy);
}

static void apply_scissor(void) {
    int w, h;
    pass_size(&w, &h);
    if (!w) return;
    int x = st.scissor[0][0], y = st.scissor[0][1], sw = st.scissor[0][2], sh = st.scissor[0][3];
    if (vp_sx != 1 || vp_sy != 1 || vp_ox != 0 || vp_oy != 0) {
        double x0 = floor(x * (double)vp_sx + vp_ox), y0 = floor(y * (double)vp_sy + vp_oy);
        double x1 = ceil(((double)x + sw) * vp_sx + vp_ox), y1 = ceil(((double)y + sh) * vp_sy + vp_oy);
        if (sw >= 16384) x1 = 1e9;  // "unbounded" scissors stay unbounded
        if (sh >= 16384) y1 = 1e9;
        if (x1 > 1e9) x1 = 1e9;
        if (y1 > 1e9) y1 = 1e9;
        x = (int)x0; y = (int)y0; sw = (int)(x1 - x0); sh = (int)(y1 - y0);
    }
    if (x < 0) { sw += x; x = 0; }
    if (y < 0) { sh += y; y = 0; }
    if (x >= w || y >= h) { x = y = 0; sw = sh = 0; }
    if (x + sw > w) sw = w - x;
    if (y + sh > h) sh = h - y;
    if (sw < 0) sw = 0;
    if (sh < 0) sh = 0;
    [enc setScissorRect:(MTLScissorRect){(NSUInteger)x, (NSUInteger)y, (NSUInteger)sw, (NSUInteger)sh}];
}

// ------------------------------------------------------------------ clears (full-screen triangle)
static const char *clear_src =
    "#include <metal_stdlib>\nusing namespace metal;\n"
    "struct V { float4 pos [[position]]; };\n"
    "vertex V vs(uint vid [[vertex_id]], constant float4 &d [[buffer(0)]]) {\n"
    "  float2 p = float2((vid << 1) & 2, vid & 2); V o; o.pos = float4(p * 2.0 - 1.0, d.x, 1); return o; }\n"
    "struct OF { float4 c [[color(INDEX)]]; };\n"
    "fragment OF fs_f(V in [[stage_in]], constant float4 &c [[buffer(0)]]) { OF o; o.c = c; return o; }\n"
    "struct OI { int4 c [[color(INDEX)]]; };\n"
    "fragment OI fs_i(V in [[stage_in]], constant int4 &c [[buffer(0)]]) { OI o; o.c = c; return o; }\n"
    "struct OU { uint4 c [[color(INDEX)]]; };\n"
    "fragment OU fs_u(V in [[stage_in]], constant uint4 &c [[buffer(0)]]) { OU o; o.c = c; return o; }\n"
    "fragment void fs_none(V in [[stage_in]]) {}\n";

static id<MTLLibrary> clear_lib(int index) {
    static id<MTLLibrary> libs[8];
    if (!libs[index]) {
        NSString *src = [@(clear_src) stringByReplacingOccurrencesOfString:@"INDEX" withString:[NSString stringWithFormat:@"%d", index]];
        NSError *err;
        libs[index] = TIMED_COMPILE([dev newLibraryWithSource:src options:nil error:&err]);
        if (!libs[index]) FATAL("clear shader: %s\n", err.localizedDescription.UTF8String);
    }
    return libs[index];
}

static void attachment_formats(MTLRenderPipelineDescriptor *pd) {
    for (int i = 0; i < st.rt.ncolor; i++)
        if (st.rt.color[i]) pd.colorAttachments[i].pixelFormat = view_texture(st.rt.color[i], &st.rt.cview[i], st.rt.has_cview[i], true, nil).pixelFormat;
    if (st.rt.depth) {
        MTLPixelFormat pf = renderer_pixel_format(st.rt.depth->format);
        if (renderer_format_is_depth(st.rt.depth->format) && st.rt.depth->format != 0x31) pd.depthAttachmentPixelFormat = pf;
        if (renderer_format_has_stencil(st.rt.depth->format)) pd.stencilAttachmentPixelFormat = pf;
    }
    for (int i = 0; i < st.rt.ncolor; i++)
        if (st.rt.color[i] && st.rt.color[i]->samples > 1) pd.rasterSampleCount = st.rt.color[i]->samples;
}

static void clear(int index, int kind, const void *color, uint32_t mask, bool depth, float dval, bool stencil,
                  uint32_t sval, uint32_t smask) {
    begin_pass();
    if (g_debug_frame) {
        int pw, ph;
        pass_size(&pw, &ph);
        const float *cf = color;
        const uint32_t *cu = color;
        LOG("[clear] pass %d idx %d depth %d (%g) stencil %d | pass %dx%d scissor %d,%d %dx%d", debug_pass_no, index, depth,
            dval, stencil, pw, ph, st.scissor[0][0], st.scissor[0][1], st.scissor[0][2], st.scissor[0][3]);
        if (index >= 0 && color) {
            if (kind == 0) LOG(" | color %g %g %g %g mask %x", cf[0], cf[1], cf[2], cf[3], mask);
            else LOG(" | color %#x %#x %#x %#x (kind %d) mask %x", cu[0], cu[1], cu[2], cu[3], kind, mask);
            if (index < st.rt.ncolor && st.rt.color[index])
                LOG(" | rt %llx f%#x", (unsigned long long)st.rt.color[index]->addr, st.rt.color[index]->format);
        }
        LOG("\n");
    }
    MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
    id<MTLLibrary> lib = clear_lib(index < 0 ? 0 : index);
    pd.vertexFunction = [lib newFunctionWithName:@"vs"];
    static NSString *const fs[] = {@"fs_f", @"fs_i", @"fs_u"};
    pd.fragmentFunction = [lib newFunctionWithName:index >= 0 ? fs[kind] : @"fs_none"];
    attachment_formats(pd);
    for (int i = 0; i < 8; i++) pd.colorAttachments[i].writeMask = MTLColorWriteMaskNone;
    if (index >= 0) {
        MTLColorWriteMask m = 0;
        if (mask & 1) m |= MTLColorWriteMaskRed;
        if (mask & 2) m |= MTLColorWriteMaskGreen;
        if (mask & 4) m |= MTLColorWriteMaskBlue;
        if (mask & 8) m |= MTLColorWriteMaskAlpha;
        pd.colorAttachments[index].writeMask = m;
    }
    NSError *err;
    NSString *key = [NSString stringWithFormat:@"clear %d %d %u %d %d %lu %lu %lu", index, kind, mask, depth, stencil,
                     (unsigned long)pd.colorAttachments[index < 0 ? 0 : index].pixelFormat,
                     (unsigned long)pd.depthAttachmentPixelFormat, (unsigned long)pd.stencilAttachmentPixelFormat];
    id<MTLRenderPipelineState> pipe = cache[key];
    if (!pipe) {
        pipe = TIMED_COMPILE([dev newRenderPipelineStateWithDescriptor:pd error:&err]);
        if (!pipe) { LOG("[metal] clear pipeline: %s\n", err.localizedDescription.UTF8String); return; }
        cache[key] = pipe;
    }
    MTLDepthStencilDescriptor *dd = [MTLDepthStencilDescriptor new];
    dd.depthCompareFunction = MTLCompareFunctionAlways;
    dd.depthWriteEnabled = depth;
    if (stencil) {
        MTLStencilDescriptor *sd = [MTLStencilDescriptor new];
        sd.stencilCompareFunction = MTLCompareFunctionAlways;
        sd.depthStencilPassOperation = MTLStencilOperationReplace;
        sd.writeMask = smask;
        dd.frontFaceStencil = sd;
        dd.backFaceStencil = sd;
    }
    [enc setRenderPipelineState:pipe];
    [enc setDepthStencilState:[dev newDepthStencilStateWithDescriptor:dd]];
    [enc setStencilReferenceValue:sval];
    [enc setCullMode:MTLCullModeNone];
    int w, h;
    pass_size(&w, &h);
    [enc setViewport:(MTLViewport){0, 0, w, h, 0, 1}];
    vp_sx = pass_sx; vp_sy = pass_sy; vp_ox = 0; vp_oy = 0;
    apply_scissor();
    float d4[4] = {dval, 0, 0, 0};
    [enc setVertexBytes:d4 length:16 atIndex:0];
    if (index >= 0) [enc setFragmentBytes:color length:16 atIndex:0];
    [enc drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
}

// ------------------------------------------------------------------ shaders (debug dump)
static void dump_program(NvnProgram *p) {
    if (!getenv("FIFA_DUMP_SHADERS")) return;
    static int n;
    mkdir("build/shaders", 0755);
    for (int s = 0; s < 6; s++) {
        if (!(p->stage_mask & (1u << s))) continue;
        char path[256];
        snprintf(path, sizeof path, "build/shaders/%llx_s%d_code.bin", p->stages[s].code_addr, s);
        FILE *f = fopen(path, "wb");
        fwrite(gptr(p->stages[s].code_addr), 1, 0x10000, f);
        fclose(f);
        snprintf(path, sizeof path, "build/shaders/%llx_s%d_ctrl.bin", p->stages[s].code_addr, s);
        f = fopen(path, "wb");
        fwrite(p->stages[s].control, 1, 0x4000, f);
        fclose(f);
    }
    n++;
}

// ------------------------------------------------------------------ draw support
uint32_t nvn_window_origin(void);
uint32_t nvn_depth_mode(void);
static int cbuf_base = 3, tex_base = 8;

@interface Prog : NSObject {
@public
    id<MTLFunction> vs, fs;
    ShaderInfo *vi, *fi;
    uint64_t fs_addr;  // fragment code address (debug switches)
    bool failed;
    _Atomic bool ready;  // functions compiled (async compile: draws are skipped until then)
}
@end
@implementation Prog
@end

static id<MTLBuffer> ring;
static uint64_t ring_off;
#define RING_SIZE (64u << 20)

static uint64_t ring_alloc(const void *data, uint64_t size) {
    if (!ring) ring = [dev newBufferWithLength:RING_SIZE options:MTLResourceStorageModeShared];
    uint64_t aligned = (ring_off + 255) & ~255ull;
    if (aligned + size > RING_SIZE) {
        // Start a fresh ring; command buffers in flight keep the old one alive until they complete.
        ring = [dev newBufferWithLength:(size > RING_SIZE ? size : RING_SIZE) options:MTLResourceStorageModeShared];
        aligned = 0;
    }
    memcpy((uint8_t *)ring.contents + aligned, data, size);
    ring_off = aligned + size;
    return aligned;
}

static void ring_reset(void) { ring_off = 0; }

// Bind guest memory [addr, addr+size) as a buffer: zero-copy inside a memory pool, copied otherwise.
static void bind_guest(id<MTLRenderCommandEncoder> e, bool vertex_stage, uint64_t addr, uint64_t size, int index) {
    uint64_t poff;
    id<MTLBuffer> pb = (addr & 3) == 0 ? renderer_pool_buffer(addr, size, &poff) : nil;
    if (pb) {
        if (vertex_stage) [e setVertexBuffer:pb offset:poff atIndex:index];
        else [e setFragmentBuffer:pb offset:poff atIndex:index];
        return;
    }
    uint64_t off = ring_alloc(gptr(addr), size);
    if (vertex_stage) [e setVertexBuffer:ring offset:off atIndex:index];
    else [e setFragmentBuffer:ring offset:off atIndex:index];
}

static int attr_kind(uint32_t fmt) {
    switch (fmt) {
    case 0x03: case 0x08: case 0x0B: case 0x0F: case 0x14: case 0x17: case 0x1B: case 0x20: case 0x23:
    case 0x27: case 0x2C: case 0x2F: case 0x3E:
        return 2;
    case 0x04: case 0x09: case 0x0C: case 0x10: case 0x15: case 0x18: case 0x1C: case 0x21: case 0x24:
    case 0x28: case 0x2D: case 0x30:
        return 1;
    default: return 0;
    }
}

static MTLVertexFormat vertex_format(uint32_t f) {
    switch (f) {
    case 0x01: return MTLVertexFormatUCharNormalized;
    case 0x02: return MTLVertexFormatCharNormalized;
    case 0x03: return MTLVertexFormatUChar;
    case 0x04: return MTLVertexFormatChar;
    case 0x05: return MTLVertexFormatHalf;
    case 0x06: return MTLVertexFormatUShortNormalized;
    case 0x07: return MTLVertexFormatShortNormalized;
    case 0x08: return MTLVertexFormatUShort;
    case 0x09: return MTLVertexFormatShort;
    case 0x0A: return MTLVertexFormatFloat;
    case 0x0B: return MTLVertexFormatUInt;
    case 0x0C: return MTLVertexFormatInt;
    case 0x0D: return MTLVertexFormatUChar2Normalized;
    case 0x0E: return MTLVertexFormatChar2Normalized;
    case 0x0F: return MTLVertexFormatUChar2;
    case 0x10: return MTLVertexFormatChar2;
    case 0x11: return MTLVertexFormatHalf2;
    case 0x12: return MTLVertexFormatUShort2Normalized;
    case 0x13: return MTLVertexFormatShort2Normalized;
    case 0x14: return MTLVertexFormatUShort2;
    case 0x15: return MTLVertexFormatShort2;
    case 0x16: return MTLVertexFormatFloat2;
    case 0x17: return MTLVertexFormatUInt2;
    case 0x18: return MTLVertexFormatInt2;
    case 0x19: return MTLVertexFormatUChar3Normalized;
    case 0x1A: return MTLVertexFormatChar3Normalized;
    case 0x1B: return MTLVertexFormatUChar3;
    case 0x1C: return MTLVertexFormatChar3;
    case 0x1D: return MTLVertexFormatHalf3;
    case 0x1E: return MTLVertexFormatUShort3Normalized;
    case 0x1F: return MTLVertexFormatShort3Normalized;
    case 0x20: return MTLVertexFormatUShort3;
    case 0x21: return MTLVertexFormatShort3;
    case 0x22: return MTLVertexFormatFloat3;
    case 0x23: return MTLVertexFormatUInt3;
    case 0x24: return MTLVertexFormatInt3;
    case 0x25: return MTLVertexFormatUChar4Normalized;
    case 0x26: return MTLVertexFormatChar4Normalized;
    case 0x27: return MTLVertexFormatUChar4;
    case 0x28: return MTLVertexFormatChar4;
    case 0x29: return MTLVertexFormatHalf4;
    case 0x2A: return MTLVertexFormatUShort4Normalized;
    case 0x2B: return MTLVertexFormatShort4Normalized;
    case 0x2C: return MTLVertexFormatUShort4;
    case 0x2D: return MTLVertexFormatShort4;
    case 0x2E: return MTLVertexFormatFloat4;
    case 0x2F: return MTLVertexFormatUInt4;
    case 0x30: return MTLVertexFormatInt4;
    case 0x3D: return MTLVertexFormatUInt1010102Normalized;
    case 0x69: return MTLVertexFormatInt1010102Normalized;
    case 0x3F: return MTLVertexFormatFloatRG11B10;
    case 0x40: return MTLVertexFormatFloatRGB9E5;
    // integer-to-float ("I2F") formats: converted to float by the vertex fetch
    case 0x51: return MTLVertexFormatUChar;   // these read as raw integers converted in shader
    default:
        return MTLVertexFormatFloat4;
    }
}

static bool is_int_format(uint32_t f) { return attr_kind(f) != 0; }

static MTLBlendFactor blend_factor(uint8_t f) {
    switch (f) {
    case 1: return MTLBlendFactorZero;
    case 2: return MTLBlendFactorOne;
    case 3: return MTLBlendFactorSourceColor;
    case 4: return MTLBlendFactorOneMinusSourceColor;
    case 5: return MTLBlendFactorSourceAlpha;
    case 6: return MTLBlendFactorOneMinusSourceAlpha;
    case 7: return MTLBlendFactorDestinationAlpha;
    case 8: return MTLBlendFactorOneMinusDestinationAlpha;
    case 9: return MTLBlendFactorDestinationColor;
    case 10: return MTLBlendFactorOneMinusDestinationColor;
    case 11: return MTLBlendFactorSourceAlphaSaturated;
    case 0x10: return MTLBlendFactorSource1Color;
    case 0x11: return MTLBlendFactorOneMinusSource1Color;
    case 0x12: return MTLBlendFactorSource1Alpha;
    case 0x13: return MTLBlendFactorOneMinusSource1Alpha;
    case 0x61: return MTLBlendFactorBlendColor;
    case 0x62: return MTLBlendFactorOneMinusBlendColor;
    case 0x63: return MTLBlendFactorBlendAlpha;
    case 0x64: return MTLBlendFactorOneMinusBlendAlpha;
    default: {
        static uint8_t warned[256];
        if (!warned[f]++) LOG("[metal] unknown blend factor %#x\n", f);
        return MTLBlendFactorOne;
    }
    }
}

static MTLBlendOperation blend_op(uint8_t e) {
    switch (e) {
    case 2: return MTLBlendOperationSubtract;
    case 3: return MTLBlendOperationReverseSubtract;
    case 4: return MTLBlendOperationMin;
    case 5: return MTLBlendOperationMax;
    default: return MTLBlendOperationAdd;
    }
}

static MTLCompareFunction compare_fn(uint8_t f) {
    switch (f) {
    case 1: return MTLCompareFunctionNever;
    case 2: return MTLCompareFunctionLess;
    case 3: return MTLCompareFunctionEqual;
    case 4: return MTLCompareFunctionLessEqual;
    case 5: return MTLCompareFunctionGreater;
    case 6: return MTLCompareFunctionNotEqual;
    case 7: return MTLCompareFunctionGreaterEqual;
    default: return MTLCompareFunctionAlways;
    }
}

static MTLStencilOperation stencil_op(uint8_t o) {
    switch (o) {
    case 2: return MTLStencilOperationZero;
    case 3: return MTLStencilOperationReplace;
    case 4: return MTLStencilOperationIncrementClamp;
    case 5: return MTLStencilOperationDecrementClamp;
    case 6: return MTLStencilOperationInvert;
    case 7: return MTLStencilOperationIncrementWrap;
    case 8: return MTLStencilOperationDecrementWrap;
    default: return MTLStencilOperationKeep;
    }
}

static int rt_kind(uint32_t f) {
    switch (f) {
    case 0x03: case 0x08: case 0x0B: case 0x0F: case 0x14: case 0x17: case 0x27: case 0x2C: case 0x2F: case 0x3E: return 2;
    case 0x04: case 0x09: case 0x0C: case 0x10: case 0x15: case 0x18: case 0x28: case 0x2D: case 0x30: return 1;
    default: return 0;
    }
}

#define PROG_TAB 4096
static struct { uint64_t key; void *prog; } prog_tab[PROG_TAB];

// Shader warm-up cache: every translated shader's MSL is kept in cache/shaders/ (v_/f_<hash>.metal). At startup
// they're all compiled once on a background queue, which fills Metal's own on-disk shader cache so that a later
// first use in-game costs ~0.1 ms instead of ~40 ms. Delete cache/ to reset it.
static uint64_t fnv64(const char *t) {
    uint64_t h = 0xcbf29ce484222325ull;
    for (; *t; t++) { h ^= (uint8_t)*t; h *= 0x100000001B3ull; }
    return h;
}

static void shader_cache_note(const char *vs, const char *fs) {
    static int made;
    if (!made) { made = 1; mkdir("cache", 0755); mkdir("cache/shaders", 0755); }
    const char *src[2] = {vs, fs};
    for (int i = 0; i < 2; i++) {
        char path[96];
        snprintf(path, sizeof path, "cache/shaders/%c_%016llx.metal", i ? 'f' : 'v', (unsigned long long)fnv64(src[i]));
        if (access(path, F_OK) == 0) continue;
        FILE *f = fopen(path, "w");
        if (f) { fputs(src[i], f); fclose(f); }
    }
}

// Options for translated shaders: vertex programs keep invariance (depth pre-pass + EQUAL test). FIFA_SAFE_MATH=1
// turns fast math off (IEEE NaN/Inf handling: Maxwell's FMNMX drops a NaN operand, fast-math fmax may not).
static MTLCompileOptions *guest_opts(bool vertex) {
    static MTLCompileOptions *o[2];
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        bool safe = getenv("FIFA_SAFE_MATH") != NULL;
        for (int i = 0; i < 2; i++) {
            o[i] = [MTLCompileOptions new];
            o[i].preserveInvariance = i == 1;
            if (safe) o[i].fastMathEnabled = NO;
        }
    });
    return o[vertex];
}

void shader_cache_warm(void) {
    static int started;
    if (started++ || getenv("FIFA_NO_SHADER_WARM")) return;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        DIR *d = opendir("cache/shaders");
        if (!d) return;
        double t0 = now_ms();
        int n = 0;
        struct dirent *e;
        while ((e = readdir(d))) {
            if (e->d_name[0] != 'v' && e->d_name[0] != 'f') continue;
            char path[300];
            snprintf(path, sizeof path, "cache/shaders/%s", e->d_name);
            NSString *src = [NSString stringWithContentsOfFile:@(path) encoding:NSUTF8StringEncoding error:nil];
            if (!src) continue;
            @autoreleasepool { (void)[dev newLibraryWithSource:src options:guest_opts(e->d_name[0] == 'v') error:nil]; }
            n++;
        }
        closedir(d);
        LOG("[shader] warm-up: %d cached shaders compiled in %.0f ms\n", n, now_ms() - t0);
    });
}

static bool sync_shaders(void) {  // FIFA_SYNC_SHADERS: compile on first use, blocking (deterministic tests)
    static int v = -1;
    if (v < 0) v = getenv("FIFA_SYNC_SHADERS") != NULL;
    return v;
}

static void compile_program(Prog *pr) {
    NSError *err = nil;
    double t0 = now_ms();
    // FIFA_SHADER_SALT=<text>: tag sources so Metal's disk cache misses (measure cold first-use compiles)
    NSString *salt = getenv("FIFA_SHADER_SALT") ? [NSString stringWithFormat:@"\n// %s\n", getenv("FIFA_SHADER_SALT")] : @"";
    id<MTLLibrary> vl = [dev newLibraryWithSource:[@(pr->vi->msl) stringByAppendingString:salt] options:guest_opts(true) error:&err];
    if (!vl) { LOG("[shader] VS compile failed: %s\n%s\n", err.localizedDescription.UTF8String, pr->vi->msl); pr->failed = true; return; }
    // FIFA_NANZERO=<fs addr hex>,...: that fragment program's non-finite outputs become 0 (tests whether a NaN source matters)
    NSString *fsrc = @(pr->fi->msl);
    const char *nz = getenv("FIFA_NANZERO");
    char me[32];
    snprintf(me, sizeof me, "%llx", (unsigned long long)pr->fs_addr);
    if (nz && strstr(nz, me)) {
        NSMutableString *m = [NSMutableString string];
        for (int k = 0; k < 8; k++)
            if ([fsrc containsString:[NSString stringWithFormat:@"o.c%d[0] =", k]])
                [m appendFormat:@"  o.c%d = select(o.c%d, float4(0), isnan(o.c%d) || isinf(o.c%d));\n", k, k, k, k];
        fsrc = [fsrc stringByReplacingOccurrencesOfString:@"  return o;\n}" withString:[m stringByAppendingString:@"  return o;\n}"]];
        LOG("[shader] FIFA_NANZERO: fs %s\n", me);
    }
    id<MTLLibrary> fl = [dev newLibraryWithSource:[fsrc stringByAppendingString:salt] options:guest_opts(false) error:&err];
    if (!fl) { LOG("[shader] FS compile failed: %s\n%s\n", err.localizedDescription.UTF8String, pr->fi->msl); pr->failed = true; return; }
    pr->vs = [vl newFunctionWithName:@"main0"];
    pr->fs = [fl newFunctionWithName:@"main0"];
    __atomic_fetch_add(&stat_compiles, 1, __ATOMIC_RELAXED);
    double dt = now_ms() - t0;
    @synchronized(cache) { stat_compile_ms += dt; }
    pr->ready = true;
}

static Prog *get_program(NvnProgram *p, ShaderKey *vkey, ShaderKey *fkey) {
    shader_cache_warm();
    if (!(p->stage_mask & 1) || !(p->stage_mask & 2)) return nil;
    // fast path: hash of the code addresses and keys
    uint64_t h = 0xcbf29ce484222325ull;
    for (int i = 0; i < 2; i++) { uint64_t a = p->stages[i].code_addr; for (int k = 0; k < 8; k++) { h ^= (a >> (k * 8)) & 0xFF; h *= 0x100000001B3ull; } }
    const uint8_t *vb = (const uint8_t *)vkey;
    for (size_t i = 0; i < sizeof *vkey; i++) { h ^= vb[i]; h *= 0x100000001B3ull; }
    for (int i = 0; i < 8; i++) { h ^= fkey->rt_kind[i]; h *= 0x100000001B3ull; }
    if (fkey->dual_source) { h ^= 0xd5; h *= 0x100000001B3ull; }
    if (!h) h = 1;
    uint32_t slot = (uint32_t)(h ^ (h >> 31)) & (PROG_TAB - 1);
    while (prog_tab[slot].key && prog_tab[slot].key != h) slot = (slot + 1) & (PROG_TAB - 1);
    if (prog_tab[slot].key) { Prog *fp = (__bridge Prog *)prog_tab[slot].prog; return fp->failed ? nil : fp; }
    NSString *key = [NSString stringWithFormat:@"%llx/%llx/%@/%@%s%s", p->stages[0].code_addr, p->stages[1].code_addr,
                     [NSData dataWithBytes:vkey length:sizeof *vkey], [NSData dataWithBytes:fkey->rt_kind length:8],
                     vkey->flip_x ? "/mirror" : "", fkey->dual_source ? "/dual" : ""];  // (NSData's description elides the middle of the key's bytes)
    Prog *pr = cache[key];
    if (pr) {
        prog_tab[slot].key = h;
        prog_tab[slot].prog = (__bridge void *)pr;
        return pr->failed ? nil : pr;
    }
    pr = [Prog new];
    pr->fs_addr = p->stages[1].code_addr;
    cache[key] = pr;
    prog_tab[slot].key = h;
    prog_tab[slot].prog = (__bridge void *)pr;  // retained by the cache dictionary
    dump_program(p);
    {
        // FIFA_DBG_FS=<fs code addr hex>:<pc hex>:<reg>
        static int parsed;
        static unsigned long long dbg_addr;
        static unsigned dbg_pc;
        static int dbg_reg;
        if (!parsed) { parsed = 1; const char *e = getenv("FIFA_DBG_FS"); if (e) sscanf(e, "%llx:%x:%d", &dbg_addr, &dbg_pc, &dbg_reg); }
        maxwell_dbg_code = dbg_addr ? (const uint8_t *)gptr(dbg_addr) : NULL;
        static unsigned long long dbg_vs;
        static int vparsed;
        if (!vparsed) { vparsed = 1; const char *e = getenv("FIFA_DBG_VS"); if (e) dbg_vs = strtoull(e, NULL, 16); }
        maxwell_dbg_vs = dbg_vs ? (const uint8_t *)gptr(dbg_vs) : NULL;
        maxwell_dbg_pc = dbg_pc;
        maxwell_dbg_reg = dbg_reg;
    }
    // the compiler constants follow the code, so they bound the instruction scan
    size_t vmax = p->stages[0].const_offset ? p->stages[0].const_offset : 0x10000;
    size_t fmax = p->stages[1].const_offset ? p->stages[1].const_offset : 0x10000;
    pr->vi = maxwell_translate(gptr(p->stages[0].code_addr), vmax, STAGE_VERTEX, vkey);
    if (!pr->vi->msl) {
        LOG("[shader] VS translate failed: %s (code %llx)\n", pr->vi->error, p->stages[0].code_addr);
        char path[128];
        snprintf(path, sizeof path, "build/shaders/fail_%llx_s0_code.bin", p->stages[0].code_addr);
        FILE *f = fopen(path, "wb");
        if (f) { fwrite(gptr(p->stages[0].code_addr), 1, 0x4000, f); fclose(f); }
        pr->failed = true;
        return nil;
    }
    fkey->vs_out_mask = pr->vi->out_mask;
    pr->fi = maxwell_translate(gptr(p->stages[1].code_addr), fmax, STAGE_FRAGMENT, fkey);
    if (!pr->fi->msl) { LOG("[shader] FS translate failed: %s\n", pr->fi->error); pr->failed = true; return nil; }
    if (getenv("FIFA_SAVE_MSL")) {
        mkdir("build/msl", 0755);
        char path[256];
        snprintf(path, sizeof path, "build/msl/%llx_vs.metal", p->stages[0].code_addr);
        FILE *f = fopen(path, "w"); fputs(pr->vi->msl, f); fclose(f);
        snprintf(path, sizeof path, "build/msl/%llx_fs.metal", p->stages[1].code_addr);
        f = fopen(path, "w"); fputs(pr->fi->msl, f); fclose(f);
    }
    shader_cache_note(pr->vi->msl, pr->fi->msl);
    if (sync_shaders()) {
        compile_program(pr);
        return pr->failed ? nil : pr;
    }
    // First-time compiles take ~40 ms each on Metal: build off the render thread; draws using this program are
    // skipped until it's ready (the object appears a few frames later instead of the game stalling).
    static dispatch_queue_t q;
    if (!q) q = dispatch_queue_create("fifa.shaders", DISPATCH_QUEUE_CONCURRENT);
    dispatch_async(q, ^{ compile_program(pr); });
    return pr;
}

static id<MTLSamplerState> sampler_for(NvnSamplerDesc *d) {
    if (d->backend) return (__bridge id<MTLSamplerState>)d->backend;
    MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
    sd.magFilter = d->mag_filter == 1 ? MTLSamplerMinMagFilterLinear : MTLSamplerMinMagFilterNearest;
    uint32_t mf = d->min_filter;
    sd.minFilter = (mf == 1 || mf == 3 || mf == 5) ? MTLSamplerMinMagFilterLinear : MTLSamplerMinMagFilterNearest;
    sd.mipFilter = mf <= 1 ? MTLSamplerMipFilterNotMipmapped : (mf == 2 || mf == 3) ? MTLSamplerMipFilterNearest : MTLSamplerMipFilterLinear;
    MTLSamplerAddressMode modes[3];
    for (int i = 0; i < 3; i++) {
        switch (d->wrap[i]) {
        case 0: case 7: modes[i] = MTLSamplerAddressModeClampToEdge; break;
        case 1: modes[i] = MTLSamplerAddressModeRepeat; break;
        case 2: case 3: modes[i] = MTLSamplerAddressModeMirrorClampToEdge; break;
        case 4: case 5: modes[i] = MTLSamplerAddressModeClampToBorderColor; break;
        case 6: modes[i] = MTLSamplerAddressModeMirrorRepeat; break;
        default: modes[i] = MTLSamplerAddressModeRepeat;
        }
    }
    sd.sAddressMode = modes[0]; sd.tAddressMode = modes[1]; sd.rAddressMode = modes[2];
    sd.lodMinClamp = d->lod_min;
    sd.lodMaxClamp = d->lod_max > 1000 ? FLT_MAX : d->lod_max;
    if (d->max_aniso > 1) sd.maxAnisotropy = (NSUInteger)fminf(d->max_aniso, 16);
    if (d->lod_bias != 0 && getenv("FIFA_LOG_BIAS"))
        LOG("[sampler] lod bias %g (min filter %u, aniso %g)\n", d->lod_bias, mf, d->max_aniso);
    static float lodmin = -1;  // FIFA_LOD_MIN=<n>: skip the n sharpest mips everywhere (aliasing experiments)
    if (lodmin < 0) lodmin = getenv("FIFA_LOD_MIN") ? (float)atof(getenv("FIFA_LOD_MIN")) : 0;
    if (lodmin > 0 && mf > 1) sd.lodMinClamp = fmaxf(sd.lodMinClamp, lodmin);
    static int stest = -1;  // FIFA_SAMPLER_TEST: full mip range + 16x aniso on mipmapped samplers (moire test)
    if (stest < 0) stest = getenv("FIFA_SAMPLER_TEST") != NULL;
    if (stest && mf > 1) { sd.lodMinClamp = 0; sd.lodMaxClamp = FLT_MAX; sd.maxAnisotropy = 16; }
    if (getenv("FIFA_LOG_SAMPLERS"))
        LOG("[sampler] min %u mag %u lod %g..%g aniso %g wrap %u %u\n", mf, d->mag_filter, d->lod_min, d->lod_max, d->max_aniso,
            d->wrap[0], d->wrap[1]);
    if (d->compare_mode) sd.compareFunction = getenv("FIFA_NO_SHADOW") ? MTLCompareFunctionAlways : compare_fn((uint8_t)d->compare_func);
    sd.supportArgumentBuffers = NO;
    id<MTLSamplerState> s = [dev newSamplerStateWithDescriptor:sd];
    d->backend = (void *)CFBridgingRetain(s);
    return s;
}

static id<MTLSamplerState> default_sampler(void) {
    static id<MTLSamplerState> s;
    if (!s) {
        MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
        sd.minFilter = sd.magFilter = MTLSamplerMinMagFilterLinear;
        s = [dev newSamplerStateWithDescriptor:sd];
    }
    return s;
}

static id<MTLTexture> dummy_texture(int type) {
    static id<MTLTexture> t[10];
    if (!t[type]) {
        MTLTextureDescriptor *d = [MTLTextureDescriptor new];
        d.pixelFormat = MTLPixelFormatRGBA8Unorm;
        d.width = d.height = 1;
        d.textureType = type == TEXTYPE_CUBE ? MTLTextureTypeCube : type == TEXTYPE_3D ? MTLTextureType3D
                      : type == TEXTYPE_2D_ARRAY ? MTLTextureType2DArray : MTLTextureType2D;  // (1D: 2D rows)
        t[type] = [dev newTextureWithDescriptor:d];
    }
    return t[type];
}

static MTLTextureType mtl_type_for(int t) {
    switch (t) {
    case TEXTYPE_3D: return MTLTextureType3D;
    case TEXTYPE_CUBE: return MTLTextureTypeCube;
    case TEXTYPE_2D_ARRAY: return MTLTextureType2DArray;
    case TEXTYPE_CUBE_ARRAY: return MTLTextureTypeCubeArray;
    case TEXTYPE_1D_ARRAY: return MTLTextureType1DArray;
    default: return MTLTextureType2D;
    }
}

// NVN texture/view component swizzle (same enum values as MTLTextureSwizzle), applied as a view.
static id<MTLTexture> apply_swizzle(id<MTLTexture> tex, NvnTexture *t, const NvnTextureView *v, bool has_view) {
    const uint8_t *sw = (has_view && (v->setmask & 8)) ? v->swizzle : t->swizzle;
    uint8_t c[4];
    for (int i = 0; i < 4; i++) c[i] = sw[i] <= 5 ? sw[i] : (uint8_t)(2 + i);
    if (!c[0] && !c[1] && !c[2] && !c[3]) c[0] = 2, c[1] = 3, c[2] = 4, c[3] = 5;  // never set: identity
    if (renderer_format_alpha_one(t->format))
        for (int i = 0; i < 4; i++) if (c[i] == MTLTextureSwizzleAlpha) c[i] = MTLTextureSwizzleOne;
    if (c[0] == 2 && c[1] == 3 && c[2] == 4 && c[3] == 5) return tex;
    if (tex.sampleCount > 1 || renderer_format_is_depth(t->format)) return tex;
    uint32_t swz = c[0] | c[1] << 8 | c[2] << 16 | (uint32_t)c[3] << 24;
    MTLTextureSwizzleChannels chans = MTLTextureSwizzleChannelsMake(c[0], c[1], c[2], c[3]);
    id<MTLTexture> view = view_cached(tex, 0x5000000000000000ull | swz, ^{
        NSUInteger slices = tex.textureType == MTLTextureType3D ? 1 : tex.arrayLength * (tex.textureType == MTLTextureTypeCube || tex.textureType == MTLTextureTypeCubeArray ? 6 : 1);
        return [tex newTextureViewWithPixelFormat:tex.pixelFormat textureType:tex.textureType
                                           levels:NSMakeRange(0, tex.mipmapLevelCount) slices:NSMakeRange(0, slices)
                                          swizzle:chans];
    });
    return view;
}

// Upload helper: a separate command buffer, committed now so it runs before the current pass.
// image: bound for SULD / SUST, so no swizzled view (those can't be written)
static id<MTLTexture> sampled_texture(NvnTexture *t, const NvnTextureView *v, bool has_view, int want_type, bool image) {
    // uploads (if the CPU-side contents changed) go into their own command buffer, committed before
    // the current one so they land first
    id<MTLTexture> tex = view_texture(t, v, has_view, false, nil);
    if (!image) tex = apply_swizzle(tex, t, v, has_view);
    MTLTextureType want = mtl_type_for(want_type);
    if (tex.textureType != want && tex.textureType != MTLTextureType2DMultisample) {
        id<MTLTexture> src = tex;
        if (want == MTLTextureType2D && (tex.textureType == MTLTextureType2DArray || tex.textureType == MTLTextureTypeCube))
            tex = view_cached(src, 0x6000000000000000ull | 1, ^{ return [src newTextureViewWithPixelFormat:src.pixelFormat textureType:MTLTextureType2D levels:NSMakeRange(0, src.mipmapLevelCount) slices:NSMakeRange(0, 1)]; });
        else if (want == MTLTextureType2DArray && tex.textureType == MTLTextureType2D)
            tex = view_cached(src, 0x6000000000000000ull | 2, ^{ return [src newTextureViewWithPixelFormat:src.pixelFormat textureType:MTLTextureType2DArray levels:NSMakeRange(0, src.mipmapLevelCount) slices:NSMakeRange(0, 1)]; });
        else if (want == MTLTextureType2DArray && (tex.textureType == MTLTextureTypeCube || tex.textureType == MTLTextureTypeCubeArray))
            // cube faces as array layers (the cube filters and their image stores address faces by layer)
            tex = view_cached(src, 0x6000000000000000ull | 4, ^{ return [src newTextureViewWithPixelFormat:src.pixelFormat textureType:MTLTextureType2DArray levels:NSMakeRange(0, src.mipmapLevelCount) slices:NSMakeRange(0, src.textureType == MTLTextureTypeCube ? 6 : src.arrayLength * 6)]; });
        else if (want == MTLTextureTypeCubeArray && tex.textureType == MTLTextureTypeCube)
            tex = view_cached(src, 0x6000000000000000ull | 5, ^{ return [src newTextureViewWithPixelFormat:src.pixelFormat textureType:MTLTextureTypeCubeArray levels:NSMakeRange(0, src.mipmapLevelCount) slices:NSMakeRange(0, 6)]; });
        else if (want == MTLTextureTypeCube && tex.textureType == MTLTextureType2DArray && tex.arrayLength >= 6)
            tex = view_cached(src, 0x6000000000000000ull | 3, ^{ return [src newTextureViewWithPixelFormat:src.pixelFormat textureType:MTLTextureTypeCube levels:NSMakeRange(0, src.mipmapLevelCount) slices:NSMakeRange(0, 6)]; });
        else {
            static int nlog;
            if (nlog++ < 30) LOG("[tex] type mismatch: want %lu have %lu (%ux%ux%u target %u fmt %#x arr %lu)\n", (unsigned long)want,
                                 (unsigned long)tex.textureType, t->width, t->height, t->depth, t->target, t->format, (unsigned long)tex.arrayLength);
            return dummy_texture(want_type);
        }
    }
    return tex;
}


// ---- render-target feedback: a draw that samples one of the pass's own attachments (e.g. depth
// bound for testing while the shader reads it) gets a snapshot copy, as sampling an attachment of the
// current Metal pass is undefined.
static NvnTexture *feedback_tex[4];
static id<MTLTexture> feedback_copy[4];
static int nfeedback;
// Snapshots stay valid until their texture is written again (a draw writing that attachment, a clear, copy or
// compute). Soft particles (grass-cut leaves, smoke) sample the depth buffer they're tested against but don't
// write it, so a burst of them shares one snapshot instead of a pass break + full-screen copy per draw.
static uint64_t g_cur_fs_hash;
static const char *snap_why[8];
static const char *snap_reason = "?";
static void snapshots_invalidate(NvnTexture *t) {  // NULL = all
    for (int i = 0; i < 8; i++) if ((!t || snap_tex[i] == t) && snap_ok[i]) { snap_ok[i] = false; snap_why[i] = snap_reason; }
}

// NVN's texture binding cbuf (c2, per stage): combined handles at 0x20 + 8 * slot, separate textures at
// 0x168 + 8 * slot, separate samplers at 0x568 + 8 * slot. Bindless shaders OR a separate texture and sampler.
static uint64_t tex_cbuf_word(int stage, uint32_t off) {
    if (off >= 0x20 && off < 0x20 + 8 * 32) return st.tex[stage][(off - 0x20) / 8];
    if (off >= 0x120 && off < 0x120 + 8 * 8) return st.img[stage][(off - 0x120) / 8];
    if (off >= 0x168 && off < 0x168 + 8 * 128) return st.septex[stage][(off - 0x168) / 8];
    if (off >= 0x568 && off < 0x568 + 8 * 32) return st.sepsmp[stage][(off - 0x568) / 8];
    return 0;
}

static uint64_t shader_tex_handle(int stage, const ShaderTexture *t) {
    if (t->bindless) return (uint32_t)(tex_cbuf_word(stage, t->handle_offset) | tex_cbuf_word(stage, t->bindless_offset));
    return tex_cbuf_word(stage, t->handle_offset);
}

static NvnTexture *shader_texture_object(int stage, ShaderTexture *st_) {
    uint64_t h = shader_tex_handle(stage, st_);
    uint32_t tic = h & 0xFFFFF;
    if (!h || !st.tpool || tic >= (uint32_t)st.tpool->count) return NULL;
    return ((NvnTexEntry *)st.tpool->entries)[tic].tex;
}

static bool is_attachment(NvnTexture *t) {
    if (!t) return false;
    if (t == st.rt.depth) return true;
    for (int i = 0; i < st.rt.ncolor; i++) if (t == st.rt.color[i]) return true;
    return false;
}

static void resolve_feedback(Prog *pr) {
    nfeedback = 0;
    static int off = -1;
    if (off < 0) off = getenv("FIFA_NO_FEEDBACK") != NULL;
    if (off) return;
    ShaderInfo *infos[2] = {pr->vi, pr->fi};
    for (int sg = 0; sg < 2; sg++)
        for (int i = 0; i < infos[sg]->ntex; i++) {
            NvnTexture *t = shader_texture_object(sg, &infos[sg]->tex[i]);
            if (!is_attachment(t)) continue;
            bool have = false;
            for (int k = 0; k < nfeedback; k++) have |= feedback_tex[k] == t;
            if (have || nfeedback == 4) continue;
            if (!g_snap_copies) g_snap_copies = [NSMutableDictionary new];
            NSMutableDictionary *copies = g_snap_copies;
            NSValue *key = [NSValue valueWithPointer:t];
            static int nosnap = -1;
            if (nosnap < 0) nosnap = getenv("FIFA_NO_SNAPSHOT_REUSE") != NULL;
            int si = -1;
            for (int k = 0; k < 8; k++) if (snap_tex[k] == t) si = k;
            // A draw that writes the same depth buffer it samples can't depend on what it reads (that is undefined on the
            // Switch too): seen in the shadow-map pass, whose material shader still declares the shadow map from the
            // main pass. Any earlier snapshot does; no copy, no pass break.
            bool self_write = t == st.rt.depth && st.ds[0] && st.ds[1];
            if (!nosnap && si >= 0 && (snap_ok[si] || self_write) && copies[key]) {  // unchanged since the last snapshot
                feedback_tex[nfeedback] = t;
                feedback_copy[nfeedback++] = copies[key];
                continue;
            }
            stat_feedback_copies++;
            static int logfb = -1;
            if (logfb < 0) logfb = getenv("FIFA_LOG_FEEDBACK") != NULL;
            if (logfb && stat_feedback_copies % 50 == 1)
                LOG("[feedback] copy %s %ux%u fmt %#x stage %d fs %016llx (last invalidated by %s) ds %d/%d/%d stencil %d ncolor %d c0 %ux%u fmt %#x mask %x\n", t == st.rt.depth ? "DEPTH" : "COLOR",
                    t->width, t->height, t->format, sg, (unsigned long long)g_cur_fs_hash, si >= 0 ? snap_why[si] : "new",
                    st.ds[0], st.ds[1], st.ds[2], st.ds[3], st.rt.ncolor, st.rt.ncolor && st.rt.color[0] ? st.rt.color[0]->width : 0,
                    st.rt.ncolor && st.rt.color[0] ? st.rt.color[0]->height : 0, st.rt.ncolor && st.rt.color[0] ? st.rt.color[0]->format : 0, st.channel_mask);
            end_pass();
            id<MTLTexture> src = renderer_texture(t, nil, true);
            id<MTLTexture> dst = copies[key];
            if (!dst || dst.width != src.width || dst.height != src.height || dst.pixelFormat != src.pixelFormat) {
                MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:src.pixelFormat width:src.width
                                                                                           height:src.height mipmapped:NO];
                d.textureType = src.textureType == MTLTextureType2DMultisample ? MTLTextureType2DMultisample : MTLTextureType2D;
                d.sampleCount = src.sampleCount;
                d.usage = MTLTextureUsageShaderRead | MTLTextureUsagePixelFormatView;
                d.storageMode = MTLStorageModePrivate;
                dst = [dev newTextureWithDescriptor:d];
                copies[key] = dst;
            }
            id<MTLBlitCommandEncoder> b = [cb() blitCommandEncoder];
            [b copyFromTexture:src sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0)
                    sourceSize:MTLSizeMake(src.width, src.height, 1) toTexture:dst destinationSlice:0 destinationLevel:0
             destinationOrigin:MTLOriginMake(0, 0, 0)];
            [b endEncoding];
            feedback_tex[nfeedback] = t;
            feedback_copy[nfeedback++] = dst;
            if (si < 0) { static int rr; si = rr++ & 7; snap_tex[si] = t; }
            snap_ok[si] = true;
            st.rt_dirty = true;  // the pass resumes (loading) after the copy
        }
}

static bool rtw(const NvnTexture *t);
static void bind_textures(int stage, ShaderInfo *si) {
    for (int i = 0; i < si->ntex; i++) {
        ShaderTexture *st_ = &si->tex[i];
        uint64_t h = shader_tex_handle(stage, st_);
        id<MTLTexture> tex = nil;
        id<MTLSamplerState> smp = nil;
        uint32_t tic = h & 0xFFFFF, tsc = (uint32_t)(h >> 20) & 0xFFF;
        tex_sc[stage][i][0] = tex_sc[stage][i][1] = 1;
        if (i < 64) tex_wh[stage][i][0] = tex_wh[stage][i][1] = 0;
        if (h && st.tpool && tic < (uint32_t)st.tpool->count) {
            NvnTexEntry *e = &((NvnTexEntry *)st.tpool->entries)[tic];
            if (rtw(e->tex)) {
                static int n;
                if (n++ < 40) LOG("[rtw] %s %llx binds it at t%d (image %d)\n", stage ? "fs" : "vs", st.program->stages[stage].code_addr, i, st_->is_image);
            }
            if (e->tex) tex = sampled_texture(e->tex, &e->view, e->has_view, st_->type, st_->is_image);
            if (e->tex && i < 64) {
                renderer_texture_scale(e->tex, &tex_sc[stage][i][0], &tex_sc[stage][i][1]);
                tex_wh[stage][i][0] = e->tex->width; tex_wh[stage][i][1] = e->tex->height;
            }
            for (int k = 0; k < nfeedback; k++)
                if (e->tex == feedback_tex[k]) {
                    id<MTLTexture> fc = feedback_copy[k];
                    MTLTextureType want = mtl_type_for(st_->type);
                    if (fc.textureType != want && want == MTLTextureType2DArray)
                        fc = [fc newTextureViewWithPixelFormat:fc.pixelFormat textureType:MTLTextureType2DArray levels:NSMakeRange(0, 1) slices:NSMakeRange(0, 1)];
                    tex = fc;
                }
        }
        if (h && st.spool && tsc < (uint32_t)st.spool->count) smp = sampler_for(&((NvnSamplerDesc *)st.spool->entries)[tsc]);
        if (!tex) tex = dummy_texture(st_->type);
        if (!smp) smp = default_sampler();
        if (g_debug_frame && tex.textureType == MTLTextureType2D && stage == 1) {
            static int dn;
            char path[128];
            snprintf(path, sizeof path, "build/dtex_%03d_%d.png", dn++, i);
            NvnTexture *dt = h && st.tpool && tic < (uint32_t)st.tpool->count ? ((NvnTexEntry *)st.tpool->entries)[tic].tex : NULL;
            if (dt) {
                size_t nz = 0, n = dt->addr ? (size_t)dt->width * dt->height : 0;  // >= 1 byte per texel
                const uint8_t *g = n ? gptr(dt->addr) : NULL;
                for (size_t k = 0; k < n; k++) nz += g[k] != 0;
                LOG("[dtex] %03d tic %u obj %p %ux%u fmt %#x fl %#x addr %llx pool %p+%llx nonzero %zu/%zu '%s'\n", dn - 1, tic, (void *)dt, dt->width,
                    dt->height, dt->format, dt->flags, dt->addr, (void *)dt->pool, dt->pool_offset, nz, n, dt->label);
            }
            renderer_save_texture(tex, path);
            snprintf(path, sizeof path, "build/dtex_%03d_%d_a.png", dn - 1, i);
            renderer_save_texture_alpha(tex, path);
        }
        if (g_debug_frame && tex.textureType != MTLTextureType2D && stage == 1) {  // arrays, 3D, cubes: no dump, just what is bound
            NvnTexture *dt = h && st.tpool && tic < (uint32_t)st.tpool->count ? ((NvnTexEntry *)st.tpool->entries)[tic].tex : NULL;
            LOG("[dtexa] slot %d tic %u mtl type %lu %lux%lux%lu fmt %lu%s", i, tic, (unsigned long)tex.textureType, (unsigned long)tex.width,
                (unsigned long)tex.height, (unsigned long)tex.arrayLength, (unsigned long)tex.pixelFormat, tex == dummy_texture(st_->type) ? " DUMMY" : "");
            if (dt) LOG(" obj %p target %u fmt %#x %ux%ux%u addr %llx fl %#x", (void *)dt, dt->target, dt->format, dt->width, dt->height, dt->depth,
                        (unsigned long long)dt->addr, dt->flags);
            LOG("\n");
        }
        if (g_debug_frame && stage == 0) {
            NvnTexture *vt = h && st.tpool && tic < (uint32_t)st.tpool->count ? ((NvnTexEntry *)st.tpool->entries)[tic].tex : NULL;
            LOG("[vtex] %d type %d off %#x bindless %d/%#x handle %llx tic %u -> %s", i, st_->type, st_->handle_offset, st_->bindless, st_->bindless_offset, (unsigned long long)h, tic, vt ? "" : "none");
            static int once; if (!once++) { LOG("[vtex] tex0:"); for (int k = 0; k < 32; k++) if (st.tex[0][k]) LOG(" %d=%llx", k, (unsigned long long)st.tex[0][k]); LOG(" septex0:"); for (int k = 0; k < 128; k++) if (st.septex[0][k]) LOG(" %d=%llx", k, (unsigned long long)st.septex[0][k]); LOG(" sepsmp0:"); for (int k = 0; k < 32; k++) if (st.sepsmp[0][k]) LOG(" %d=%llx", k, (unsigned long long)st.sepsmp[0][k]); LOG("\n"); }
            if (vt) LOG("target %u fmt %#x %ux%u addr %llx", vt->target, vt->format, vt->width, vt->height, (unsigned long long)vt->addr);
            LOG(" mtl type %lu %lux%lu\n", (unsigned long)tex.textureType, (unsigned long)tex.width, (unsigned long)tex.height);
        }
        if (stage == 0) { [enc setVertexTexture:tex atIndex:i]; if (!st_->is_image) [enc setVertexSamplerState:smp atIndex:st_->sampler]; }
        else { [enc setFragmentTexture:tex atIndex:i]; if (!st_->is_image) [enc setFragmentSamplerState:smp atIndex:st_->sampler]; }
    }
}

static void bind_cbufs(int stage, ShaderInfo *si) {
    for (int k = 0; k < 18; k++) {
        if (!(si->cbuf_mask & (1u << k))) continue;
        if (k == 0) {
            // driver constants: storage buffer bindings {addr_lo, addr_hi, size, 0} in a 0x100-byte table
            // per hardware stage at 0x110 + 0x100 * stage (vertex 0, fragment 4)
            uint32_t drv[0x800 / 4] = {0};
            uint32_t base = 0x110 + 0x100 * (stage == 1 ? 4 : 0);
            for (int i = 0; i < 16; i++) {
                uint32_t *e = &drv[(base + 16 * i) / 4];
                e[0] = (uint32_t)st.sbuf[stage][i].addr;
                e[1] = (uint32_t)(st.sbuf[stage][i].addr >> 32);
                e[2] = (uint32_t)st.sbuf[stage][i].size;
            }
            uint64_t size = si->cbuf_size[0] > sizeof drv ? sizeof drv : (si->cbuf_size[0] ? si->cbuf_size[0] : 256);
            uint64_t off = ring_alloc(drv, size);
            if (stage == 0) [enc setVertexBuffer:ring offset:off atIndex:0];
            else [enc setFragmentBuffer:ring offset:off atIndex:0];
            continue;
        }
        if (k == 1) {  // compiler constants stored after the shader code
            NvnShaderStage *ss = &st.program->stages[stage];
            uint64_t size = ss->const_size ? ss->const_size : 16;
            if (size < si->cbuf_size[1]) size = si->cbuf_size[1];
            uint64_t off = ring_alloc(gptr(ss->code_addr + ss->const_offset), size);
            if (stage == 0) [enc setVertexBuffer:ring offset:off atIndex:1];
            else [enc setFragmentBuffer:ring offset:off atIndex:1];
            continue;
        }
        int idx = k - cbuf_base;
        Range r = (idx >= 0 && idx < 18) ? st.ubuf[stage][idx] : (Range){0, 0};
        uint64_t size = si->cbuf_size[k];
        if (!r.addr) {
            static const uint8_t zeros[0x10000];
            uint64_t off = ring_alloc(zeros, size ? size : 256);
            if (stage == 0) [enc setVertexBuffer:ring offset:off atIndex:k];
            else [enc setFragmentBuffer:ring offset:off atIndex:k];
            continue;
        }
        if ((si->cbuf_dynamic & (1u << k)) && r.size) size = r.size > 0x10000 ? 0x10000 : r.size;  // indexed: whole buffer
        if (size > r.size && r.size) size = r.size;
        if (size < 16) size = 16;
        uint64_t off = ring_alloc(gptr(r.addr), size);  // snapshot, as later CPU writes must not leak in
        if (stage == 0) [enc setVertexBuffer:ring offset:off atIndex:k];
        else [enc setFragmentBuffer:ring offset:off atIndex:k];
    }
}


// ------------------------------------------------------------------ compute
// Compute driver constants (c0): 0x200 work group counts, 0x210 + 16*i uniform buffers (compute has
// few hardware cbuf slots, so the compiler reads the rest through global memory), 0x310 + 16*i
// storage buffers; each table entry is {addr_lo, addr_hi, size, 0}.
typedef struct { id<MTLComputePipelineState> pipe; ShaderInfo *si; uint32_t local[3]; bool failed; } CsProg;
static NSMutableDictionary *g_cs_progs;  // code address -> CsProg

static CsProg *compute_program(NvnProgram *p) {
    NvnShaderStage *ss = &p->stages[5];
    if (!ss->code_addr) return NULL;
    NSValue *key = [NSNumber numberWithUnsignedLongLong:ss->code_addr];
    NSMutableDictionary *progs = g_cs_progs;
    if (!progs) progs = g_cs_progs = [NSMutableDictionary new];
    NSValue *v = progs[key];
    if (v) { CsProg *cp = v.pointerValue; return cp->failed ? NULL : cp; }
    CsProg *cp = calloc(1, sizeof *cp);
    progs[key] = [NSValue valueWithPointer:cp];
    dump_program(p);
    ShaderKey k = {0};
    maxwell_shared_size = ss->control ? *(const uint32_t *)(ss->control + 0x754) : 0;  // (next to the local size)
    cp->si = maxwell_translate(gptr(ss->code_addr), ss->const_offset ? ss->const_offset : 0x10000, STAGE_COMPUTE, &k);
    if (!cp->si->msl) { LOG("[compute] translate failed: %s (code %llx)\n", cp->si->error, ss->code_addr); cp->failed = true; return NULL; }
    for (int i = 0; i < 3; i++) {
        cp->local[i] = ss->control ? *(const uint32_t *)(ss->control + 0x748 + 4 * i) : 1;
        if (!cp->local[i]) cp->local[i] = 1;
    }
    NSError *err = nil;
    id<MTLLibrary> lib = TIMED_COMPILE([dev newLibraryWithSource:@(cp->si->msl) options:guest_opts(false) error:&err]);
    id<MTLFunction> f = lib ? [lib newFunctionWithName:@"main0"] : nil;
    cp->pipe = f ? TIMED_COMPILE([dev newComputePipelineStateWithFunction:f error:&err]) : nil;
    if (!cp->pipe) {
        LOG("[compute] compile failed: %s\n%s\n", err.localizedDescription.UTF8String, cp->si->msl);
        cp->failed = true;
        return NULL;
    }
    LOG("[compute] program %llx: local size %ux%ux%u, %d cbufs mask %#x\n", ss->code_addr, cp->local[0], cp->local[1],
        cp->local[2], 0, cp->si->cbuf_mask);
    return cp;
}

// Global memory (LDG/STG) window: the memory pool holding the stage's first storage buffer, else its first uniform
// buffer; shaders read zeros outside it. win = {GPU address of the pool buffer, its guest address, its size, 0}.
// gfind windows (GMEM_WINDOWS, shader.h): the memory pools of the stage's storage buffers, then of its uniform
// buffers; unused entries have size 0. Returns the count; bufs gets the pool buffers (for useResource).
static int gmem_windows(int stage, uint64_t win[GMEM_WINDOWS][4], __unsafe_unretained id<MTLBuffer> bufs[GMEM_WINDOWS]) {
    memset(win, 0, sizeof(uint64_t) * 4 * GMEM_WINDOWS);
    int n = 0;
    static int max = -1;  // FIFA_GMEM_ONE=1: only the first window (the old behaviour, for comparisons)
    if (max < 0) max = getenv("FIFA_GMEM_ONE") ? 1 : GMEM_WINDOWS;
    for (int i = 0; i < 16 + 18 && n < max; i++) {
        uint64_t a = i < 16 ? st.sbuf[stage][i].addr : st.ubuf[stage][i - 16].addr, base;
        if (!a) continue;
        bool have = false;
        for (int k = 0; k < n; k++) have |= a - win[k][1] < win[k][2];
        id<MTLBuffer> b = have ? nil : renderer_pool_window(a, &base);
        if (!b) continue;
        win[n][0] = b.gpuAddress; win[n][1] = base; win[n][2] = b.length;
        bufs[n++] = b;
    }
    return n;
}

// Debug: each texture a stage's program binds (handle, size, format, guest bytes, GPU-owned) and which mip levels /
// faces of its Metal texture hold anything (renderer_texture_probe), for the [sbufw] / [perdraw] traces.
static void log_bound_textures(int stage, ShaderInfo *si) {
    for (int i = 0; i < si->ntex; i++) {
        uint64_t h = shader_tex_handle(stage, &si->tex[i]);
        uint32_t tic = h & 0xFFFFF;
        NvnTexEntry *e = h && st.tpool && tic < (uint32_t)st.tpool->count ? &((NvnTexEntry *)st.tpool->entries)[tic] : NULL;
        LOG(" t%d(h %llx type %d img %d tex %p", i, h, si->tex[i].type, si->tex[i].is_image, e ? e->tex : NULL);
        if (e && e->tex) {
            size_t nz = 0, len = e->tex->pool ? (size_t)tegra_total_size(&e->tex->layout) : 0;
            const uint8_t *g = gptr(e->tex->addr);
            for (size_t b = 0; b < len; b++) nz += g[b] != 0;
            LOG(" %ux%u fmt %u addr %llx flags %x label '%s' guest %zu/%zu nonzero, gpu-owned %d", e->tex->width, e->tex->height,
                e->tex->format, e->tex->addr, e->tex->flags, e->tex->label, nz, len, renderer_is_gpu_owned(e->tex));
        }
        LOG(")");
        extern void renderer_texture_probe(NvnTexture *t);
        if (e && e->tex && e->tex->backend) renderer_texture_probe(e->tex);
    }
}

static void dispatch_compute(uint32_t gx, uint32_t gy, uint32_t gz) {
    snap_reason = "compute"; snapshots_invalidate(NULL);
    if (!st.program || !gx || !gy || !gz) return;
    CsProg *cp = compute_program(st.program);
    if (!cp) return;
    static int trace = -1;
    if (trace < 0) trace = getenv("FIFA_TRACE_CS") != NULL;
#define CST(x) do { if (trace) { fprintf(stderr, "[cs] %s\n", x); fflush(stderr); } } while (0)
    CST("end_pass");
    end_pass();
    if (gpu_profile()) commit_cb("other");
    CST("encoder");
    id<MTLComputeCommandEncoder> ce = [cb() computeCommandEncoder];
    CST("pipeline");
    [ce setComputePipelineState:cp->pipe];
    ShaderInfo *si = cp->si;
    for (int k = 0; k < 18; k++) {
        if (!(si->cbuf_mask & (1u << k))) continue;
        uint64_t off;
        if (k == 0) {
            uint32_t drv[0x500 / 4] = {0};  // tables end at 0x310 + 16 * 16
            drv[0x200 / 4] = gx; drv[0x204 / 4] = gy; drv[0x208 / 4] = gz;
            for (int i = 0; i < 16; i++) {
                uint32_t *u = &drv[(0x210 + 16 * i) / 4];
                if (i < 18) { u[0] = (uint32_t)st.ubuf[5][i].addr; u[1] = (uint32_t)(st.ubuf[5][i].addr >> 32); u[2] = (uint32_t)st.ubuf[5][i].size; }
                uint32_t *e = &drv[(0x310 + 16 * i) / 4];
                e[0] = (uint32_t)st.sbuf[5][i].addr; e[1] = (uint32_t)(st.sbuf[5][i].addr >> 32); e[2] = (uint32_t)st.sbuf[5][i].size;
            }
            off = ring_alloc(drv, sizeof drv);
        } else if (k == 1) {
            NvnShaderStage *ss = &st.program->stages[5];
            uint64_t size = ss->const_size ? ss->const_size : 16;
            if (size < si->cbuf_size[1]) size = si->cbuf_size[1];
            off = ring_alloc(gptr(ss->code_addr + ss->const_offset), size);
        } else {
            int idx = k - cbuf_base;
            Range r = (idx >= 0 && idx < 18) ? st.ubuf[5][idx] : (Range){0, 0};
            uint64_t size = si->cbuf_size[k] ? si->cbuf_size[k] : 256;
            if (r.addr) {
                if ((si->cbuf_dynamic & (1u << k)) && r.size) size = r.size > 0x10000 ? 0x10000 : r.size;
                if (size > r.size && r.size) size = r.size;
                if (size < 16) size = 16;
                off = ring_alloc(gptr(r.addr), size);
            } else {
                static const uint8_t zeros[0x10000];
                off = ring_alloc(zeros, size);
            }
        }
        [ce setBuffer:ring offset:off atIndex:k];
    }
    CST("gmem");
    if (si->uses_gmem) {
        uint64_t win[GMEM_WINDOWS][4];
        __unsafe_unretained id<MTLBuffer> bufs[GMEM_WINDOWS];
        int n = gmem_windows(5, win, bufs);
        for (int k = 0; k < n; k++) [ce useResource:bufs[k] usage:MTLResourceUsageRead | MTLResourceUsageWrite];
        [ce setBytes:win length:sizeof win atIndex:GMEM_BUFFER_INDEX];
    }
    CST("textures");
    for (int i = 0; i < si->ntex; i++) {
        ShaderTexture *st_ = &si->tex[i];
        uint64_t h = shader_tex_handle(5, st_);
        id<MTLTexture> tex = nil;
        id<MTLSamplerState> smp = nil;
        uint32_t tic = h & 0xFFFFF, tsc = (uint32_t)(h >> 20) & 0xFFF;
        tex_sc[0][i][0] = tex_sc[0][i][1] = 1;
        if (h && st.tpool && tic < (uint32_t)st.tpool->count) {
            NvnTexEntry *e = &((NvnTexEntry *)st.tpool->entries)[tic];
            if (rtw(e->tex)) {
                static int n;
                if (n++ < 40) LOG("[rtw] cs %llx binds it at t%d (image %d)\n", st.program->stages[5].code_addr, i, st_->is_image);
            }
            if (e->tex) tex = sampled_texture(e->tex, &e->view, e->has_view, st_->type, st_->is_image);
            if (e->tex && i < 64) renderer_texture_scale(e->tex, &tex_sc[0][i][0], &tex_sc[0][i][1]);
            for (int k = 0; k < nfeedback; k++)
                if (e->tex == feedback_tex[k]) {
                    id<MTLTexture> fc = feedback_copy[k];
                    MTLTextureType want = mtl_type_for(st_->type);
                    if (fc.textureType != want && want == MTLTextureType2DArray)
                        fc = [fc newTextureViewWithPixelFormat:fc.pixelFormat textureType:MTLTextureType2DArray levels:NSMakeRange(0, 1) slices:NSMakeRange(0, 1)];
                    tex = fc;
                }
        }
        if (h && st.spool && tsc < (uint32_t)st.spool->count) smp = sampler_for(&((NvnSamplerDesc *)st.spool->entries)[tsc]);
        [ce setTexture:tex ? tex : dummy_texture(st_->type) atIndex:i];
        if (!st_->is_image) [ce setSamplerState:smp ? smp : default_sampler() atIndex:st_->sampler];
    }
    if (si->uses_rscale) {
        float rs[65][4] = {{1, 1, 0, 0}};
        for (int i = 0; i < si->ntex && i < 64; i++) { rs[1 + i][0] = tex_sc[0][i][0]; rs[1 + i][1] = tex_sc[0][i][1]; }
        [ce setBytes:rs length:(NSUInteger)(1 + si->ntex) * 16 atIndex:RSCALE_BUFFER_INDEX];
    }
    CST("dispatch");
    static uint64_t sbuf_watch = ~0ull;
    if (sbuf_watch == ~0ull) sbuf_watch = getenv("FIFA_DBG_SBUF") ? strtoull(getenv("FIFA_DBG_SBUF"), NULL, 16) : 0;
    bool sbuf_hit = false;
    static int csw_on = -1;
    if (csw_on < 0) csw_on = getenv("FIFA_DBG_CSW") != NULL;
    if (sbuf_watch || csw_on) {
        // FIFA_DBG_SBUF=<addr>: log the first dispatches of each program that binds a storage buffer holding <addr>,
        // and the 48 bytes there whenever they changed since the previous dispatch.
        static uint64_t seen_cs[64];
        static int seen_n[64], nseen;
        static uint32_t last[12];
        static const uint32_t none[12];
        const uint32_t *now = sbuf_watch ? (uint32_t *)gptr(sbuf_watch) : none;
        bool changed = memcmp(last, now, sizeof last) != 0;
        if (changed) memcpy(last, now, sizeof last);
        uint64_t cs = st.program->stages[5].code_addr;
        for (int i = 0; i < 16; i++)
            if (st.sbuf[5][i].addr <= sbuf_watch && sbuf_watch < st.sbuf[5][i].addr + st.sbuf[5][i].size) {
                int k = 0;
                while (k < nseen && seen_cs[k] != cs) k++;
                if (k == nseen && nseen < 64) seen_cs[nseen++] = cs;
                if (k < 64 && seen_n[k]++ >= 3 && !changed) break;
                sbuf_hit = true;
                LOG("[sbufw] cs %llx groups %ux%ux%u ssbo %d (%llx,%llu) ubo0 %llx:", cs, gx, gy, gz, i, st.sbuf[5][i].addr,
                    st.sbuf[5][i].size, st.ubuf[5][0].addr);
                if (st.ubuf[5][0].addr) for (int w = 0; w < 56 && w * 4 < (int)st.ubuf[5][0].size; w++) LOG(" %08x", ((uint32_t *)gptr(st.ubuf[5][0].addr))[w]);
                LOG(" | buf:");
                for (int w = 0; w < 12; w++) LOG(" %08x", now[w]);
                LOG("\n");
                break;
            }
        static uint64_t csw[16];
        static int ncsw = -1, csw_n[16];
        if (ncsw < 0) {
            // FIFA_DBG_CSW=<cs>,...: also the first 12 dispatches of these programs (and all in the debug frame), whatever they bind.
            ncsw = 0;
            for (const char *e = getenv("FIFA_DBG_CSW"); e && *e && ncsw < 16;) {
                char *end;
                csw[ncsw++] = strtoull(e, &end, 16);
                e = *end == ',' ? end + 1 : "";
            }
        }
        for (int k = 0; k < ncsw && !sbuf_hit; k++)
            if (csw[k] == cs && (csw_n[k]++ < 12 || g_debug_frame)) {
                extern uint64_t g_rt_watch_addr;
                if (!g_rt_watch_addr && si->ntex && !si->tex[0].is_image) {
                    uint64_t h = shader_tex_handle(5, &si->tex[0]);
                    uint32_t tic = h & 0xFFFFF;
                    NvnTexEntry *e = h && st.tpool && tic < (uint32_t)st.tpool->count ? &((NvnTexEntry *)st.tpool->entries)[tic] : NULL;
                    if (e && e->tex) g_rt_watch_addr = e->tex->addr;
                }
                sbuf_hit = true;
                LOG("[sbufw] cs %llx groups %ux%ux%u\n", cs, gx, gy, gz);
            }
        if (changed) {
            LOG("[sbufw] changed before cs %llx:", cs);
            for (int w = 0; w < 12; w++) LOG(" %08x", now[w]);
            LOG("\n");
        }
    }
    extern bool g_debug_frame;
    if (g_debug_frame) {
        LOG("[dispatch] cs=%llx groups %ux%ux%u ssbo:", st.program->stages[5].code_addr, gx, gy, gz);
        for (int i = 0; i < 16; i++) if (st.sbuf[5][i].addr) LOG(" %d(%llx,%llu)", i, st.sbuf[5][i].addr, st.sbuf[5][i].size);
        LOG(" ubo:");
        for (int i = 0; i < 18; i++) if (st.ubuf[5][i].addr) LOG(" %d(%llx,%llu)", i, st.ubuf[5][i].addr, st.ubuf[5][i].size);
        LOG(" gmem %d\n", si->uses_gmem);
    }
    [ce dispatchThreadgroups:MTLSizeMake(gx, gy, gz) threadsPerThreadgroup:MTLSizeMake(cp->local[0], cp->local[1], cp->local[2])];
    [ce endEncoding];
    if (sbuf_hit) {
        renderer_flush();
        LOG("[sbufw] after (local %ux%ux%u):", cp->local[0], cp->local[1], cp->local[2]);
        log_bound_textures(5, si);
        for (int w = 0; w < 12 && sbuf_watch; w++) LOG(" %08x", ((uint32_t *)gptr(sbuf_watch))[w]);
        for (int i = 0; i < 16; i++)
            if (st.sbuf[5][i].addr && st.sbuf[5][i].size <= 4096) {
                LOG(" | ssbo%d %llx:", i, st.sbuf[5][i].addr);
                for (int w = 0; w < 12 && w * 4 < (int)st.sbuf[5][i].size; w++) LOG(" %08x", ((uint32_t *)gptr(st.sbuf[5][i].addr))[w]);
            }
        LOG("\n");
    }
    if (gpu_profile()) {
        char tag[64];
        snprintf(tag, sizeof tag, "cs %llx", (unsigned long long)st.program->stages[5].code_addr);
        commit_cb(tag);
    }
    CST("done");
    stat_dispatches++;
}

static MTLPrimitiveType prim_type(uint32_t mode, bool *ok) {
    *ok = true;
    switch (mode) {
    case 0: return MTLPrimitiveTypePoint;
    case 1: return MTLPrimitiveTypeLine;
    case 3: return MTLPrimitiveTypeLineStrip;
    case 4: return MTLPrimitiveTypeTriangle;
    case 5: return MTLPrimitiveTypeTriangleStrip;
    default: *ok = false; return MTLPrimitiveTypeTriangle;
    }
}

// Expand quads/fans/polygons into a triangle index list (32-bit) referencing the original vertices.
static id<MTLBuffer> expand_indices(uint32_t mode, uint32_t count, const void *indices, int itype, uint32_t first,
                                    uint32_t *out_count) {
    uint32_t tris = mode == 7 ? count / 4 * 2 : count >= 3 ? count - 2 : 0;
    uint32_t *out = malloc(tris * 3 * 4 + 4);
    uint32_t n = 0;
#define IDX(i) (indices ? (itype == 0 ? ((const uint8_t *)indices)[i] : itype == 1 ? ((const uint16_t *)indices)[i] : ((const uint32_t *)indices)[i]) : first + (i))
    if (mode == 7) {  // quads
        for (uint32_t q = 0; q + 3 < count; q += 4) {
            out[n++] = IDX(q); out[n++] = IDX(q + 1); out[n++] = IDX(q + 2);
            out[n++] = IDX(q); out[n++] = IDX(q + 2); out[n++] = IDX(q + 3);
        }
    } else {  // fan / polygon
        for (uint32_t i = 1; i + 1 < count; i++) { out[n++] = IDX(0); out[n++] = IDX(i); out[n++] = IDX(i + 1); }
    }
#undef IDX
    *out_count = n;
    id<MTLBuffer> b = [dev newBufferWithBytes:out length:n * 4 + 4 options:MTLResourceStorageModeShared];
    free(out);
    return b;
}

// FIFA_DBG_PERDRAW=1: in the debug frame every draw ends its pass, waits for the GPU and logs how many pixels of
// each float colour target are non-finite, so the first draw that writes NaN/Inf can be found.
static Prog *perdraw_prog;  // the draw being checked
static void log_bound_textures(int stage, ShaderInfo *si);
static void perdraw_check(void) {
    static int ndraw;
    [enc endEncoding];
    enc = nil;
    id<MTLCommandBuffer> c = cmdbuf;
    cmdbuf = nil;
    [c commit];
    [c waitUntilCompleted];
    st.rt_dirty = true;
    ndraw++;
    for (int i = 0; i < st.rt.ncolor; i++) {
        NvnTexture *t = st.rt.color[i];
        if (!t || !t->backend) continue;
        id<MTLTexture> tex = renderer_texture(t, nil, true);
        MTLPixelFormat f = tex.pixelFormat;
        if (f != MTLPixelFormatRG11B10Float && f != MTLPixelFormatRGBA16Float && f != MTLPixelFormatRGBA32Float &&
            f != MTLPixelFormatRG16Float && f != MTLPixelFormatR16Float && f != MTLPixelFormatR32Float && f != MTLPixelFormatRG32Float)
            continue;
        static unsigned long long trace_fs = 1;
        if (trace_fs == 1) {
            const char *e = getenv("FIFA_DBG_FS");
            unsigned long long a = 0;
            unsigned pc = 0;
            trace_fs = e && sscanf(e, "%llx:%x", &a, &pc) == 2 && pc == 0xfffffffeu ? a : 0;
        }
        if (trace_fs && st.program && st.program->stages[1].code_addr == trace_fs) {
            renderer_nantrace_report(tex);
            static int nprobe;
            if (perdraw_prog && nprobe++ < 2) { LOG("[perdraw] fs textures:"); log_bound_textures(1, perdraw_prog->fi); LOG("\n"); }
        }
        long first;
        size_t bad = renderer_count_nonfinite(tex, &first);
        LOG("[perdraw] draw %d pass %d rt%d %llx %lux%lu fmt %lu: %zu non-finite (first %ld,%ld) vs %llx fs %llx\n", ndraw,
            debug_pass_no, i, (unsigned long long)t->addr, (unsigned long)tex.width, (unsigned long)tex.height,
            (unsigned long)f, bad, first < 0 ? -1 : first % (long)tex.width, first < 0 ? -1 : first / (long)tex.width,
            st.program ? (unsigned long long)st.program->stages[0].code_addr : 0ull,
            st.program ? (unsigned long long)st.program->stages[1].code_addr : 0ull);
    }
}

// Debug frame: which draws are skipped (reason as in the stats line), with their programs and colour target.
static void skip_log(int n) {
    if (!g_debug_frame) return;
    LOG("[skip] %c vs %llx fs %llx rt0 %llx %ux%u\n", 'a' + n, st.program ? (unsigned long long)st.program->stages[0].code_addr : 0ull,
        st.program ? (unsigned long long)st.program->stages[1].code_addr : 0ull,
        st.rt.ncolor && st.rt.color[0] ? (unsigned long long)st.rt.color[0]->addr : 0ull,
        st.rt.ncolor && st.rt.color[0] ? st.rt.color[0]->width : 0, st.rt.ncolor && st.rt.color[0] ? st.rt.color[0]->height : 0);
}

static void log_bound_textures(int stage, ShaderInfo *si);
static void debug_draw(Prog *pr, CmdRec *r, bool indexed) {
    static uint64_t probe_fs = 1;  // FIFA_DBG_PROBE_FS=<fs>: the debug frame lists that program's textures ([probefs])
    if (probe_fs == 1) { const char *e = getenv("FIFA_DBG_PROBE_FS"); probe_fs = e ? strtoull(e, NULL, 16) : 0; }
    if (probe_fs && g_debug_frame && st.program && st.program->stages[1].code_addr == probe_fs) {
        LOG("[probefs] %llx:", probe_fs);
        log_bound_textures(1, pr->fi);
        LOG("\n");
    }
    static uint64_t dump_fs = 1;
    if (dump_fs == 1) { const char *e = getenv("FIFA_DUMP_UBO"); dump_fs = e ? strtoull(e, NULL, 16) : 0; }
    if (dump_fs && st.program && st.program->stages[1].code_addr == dump_fs) {
        for (int i = 0; i < 18; i++) {
            if (!st.ubuf[1][i].addr) continue;
            const float *f = gptr(st.ubuf[1][i].addr);
            LOG("[ubo] f%d (%llu):", i, st.ubuf[1][i].size);
            for (uint64_t k = 0; k < st.ubuf[1][i].size / 4 && k < 1200; k++) LOG(" %g", f[k]);
            LOG("\n");
        }
    }
    static int dump_vs = -1;  // FIFA_DUMP_VSUBO: every vertex-stage UBO (first 48 floats) of debug-frame draws
    if (dump_vs < 0) dump_vs = getenv("FIFA_DUMP_VSUBO") != NULL;
    for (int i = 0; dump_vs && i < 18; i++) {
        if (!st.ubuf[0][i].addr) continue;
        const float *f = gptr(st.ubuf[0][i].addr);
        LOG("[vsubo] v%d (%llu):", i, st.ubuf[0][i].size);
        for (uint64_t k = 0; k < st.ubuf[0][i].size / 4 && k < 48; k++) LOG(" %g", f[k]);
        LOG("\n");
    }
    static int dump_vb = -1;  // FIFA_DUMP_VB: the first 6 vertices of stream 0 (raw words) of debug-frame draws
    if (dump_vb < 0) dump_vb = getenv("FIFA_DUMP_VB") != NULL;
    if (dump_vb && st.vbuf[0].addr && st.stream[0][0]) {
        const uint32_t *u = gptr(st.vbuf[0].addr);
        extern void dbg_vb_hook(const void *host, uint64_t guest);
        dbg_vb_hook(u, st.vbuf[0].addr);
        uint32_t words = st.stream[0][0] / 4;
        for (uint32_t v = 0; v < 6 && (v + 1) * st.stream[0][0] <= st.vbuf[0].size; v++) {
            LOG("[vb] v%u:", v);
            for (uint32_t k = 0; k < words && k < 16; k++) LOG(" %08x", u[v * words + k]);
            LOG("\n");
        }
    }
    for (int i = 0; i < 16; i++) {  // small fragment storage buffers, whole (light / parameter records)
        if (!st.sbuf[1][i].addr || st.sbuf[1][i].size > 256) continue;
        const uint32_t *u = gptr(st.sbuf[1][i].addr);
        LOG("[fssbo] f%d %llx:", i, (unsigned long long)st.sbuf[1][i].addr);
        for (uint64_t k = 0; k < st.sbuf[1][i].size / 4; k++) LOG(" %g(%08x)", *(const float *)&u[k], u[k]);
        LOG("\n");
    }
    if (st.sbuf[0][0].addr && st.sbuf[0][0].size >= 64 && st.sbuf[0][0].size <= 8192) {
        int recw = (st.sbuf[0][0].size % 96 == 0 && st.sbuf[0][0].size % 64 != 0) ? 24 : 16;
        const float *f = gptr(st.sbuf[0][0].addr);
        const uint32_t *u = (const uint32_t *)f;
        for (int g = 0; g < (int)(st.sbuf[0][0].size / (recw * 4)) && g < 6; g++) {
            LOG("[ssbo] rec%d:", g);
            for (int k = 0; k < recw; k++) LOG(" %g(%08x)", f[g * recw + k], u[g * recw + k]);
            LOG("\n");
        }
    }
    LOG("[draw] %s inst %llu", indexed ? "e" : "a", indexed ? r->x[6] : r->x[4]);
    LOG(" %s mode %llu count %llu prog vs=%llx fs=%llx | vs cb %#x fs cb %#x | ubufs:",
        indexed ? "elems" : "arrays", r->x[0], r->x[2], st.program->stages[0].code_addr, st.program->stages[1].code_addr,
        pr->vi->cbuf_mask, pr->fi->cbuf_mask);
    for (int s = 0; s < 2; s++)
        for (int i = 0; i < 18; i++)
            if (st.ubuf[s][i].addr) LOG(" %c%d(%llx,%llu)", s ? 'f' : 'v', i, st.ubuf[s][i].addr, st.ubuf[s][i].size);
    LOG(" | fubo0:");
    for (int i = 0; i < 6; i++)
        if (st.ubuf[1][i].addr) { const float *f = gptr(st.ubuf[1][i].addr); LOG(" f%d=[%g %g %g %g]", i, f[0], f[1], f[2], f[3]); }
    LOG(" | ssbo:");
    for (int sg = 0; sg < 2; sg++)
        for (int i = 0; i < 16; i++)
            if (st.sbuf[sg][i].addr) LOG(" %c%d(%llx,%llu)", sg ? 'f' : 'v', i, st.sbuf[sg][i].addr, st.sbuf[sg][i].size);
    LOG(" | tex:");
    for (int i = 0; i < pr->fi->ntex; i++) LOG(" off%u", pr->fi->tex[i].handle_offset);
    LOG(" bound:");
    for (int i = 0; i < 32; i++) {
        if (!st.tex[1][i]) continue;
        uint32_t tic = st.tex[1][i] & 0xFFFFF;
        NvnTexture *t = st.tpool && tic < (uint32_t)st.tpool->count ? ((NvnTexEntry *)st.tpool->entries)[tic].tex : NULL;
        if (t) LOG(" %d=[%ux%ux%u t%u f%#x fl%#x %s]", i, t->width, t->height, t->depth, t->target, t->format, t->flags, t->label);
        else LOG(" %d=%llx", i, st.tex[1][i]);
    }
    LOG(" | rt %d", st.rt.ncolor);
    for (int i = 0; i < st.rt.ncolor; i++) if (st.rt.color[i]) LOG(" %ux%u/f%#x@%llx obj %p", st.rt.color[i]->width, st.rt.color[i]->height, st.rt.color[i]->format, (unsigned long long)st.rt.color[i]->addr, (void *)st.rt.color[i]);
    if (st.rt.depth) LOG(" d%#x", st.rt.depth->format);
    LOG(" | vp %.0f %.0f %.0f %.0f | attrs %#x streams:", st.viewport[0][0], st.viewport[0][1], st.viewport[0][2], st.viewport[0][3], pr->vi->attr_mask);
    for (int i = 0; i < st.nattrib; i++) LOG(" a%d(f%#x s%u o%u)", i, st.attrib[i] & 0xFF, (st.attrib[i] >> 8) & 0x1F, st.attrib[i] >> 13);
    for (int i = 0; i < 12; i++) if (st.vbuf[i].addr) LOG(" vb%d(%llx,%llu,stride %u div %u)", i, st.vbuf[i].addr, st.vbuf[i].size, st.stream[i][0], st.stream[i][1]);
    if (st.ubuf[0][0].addr && st.ubuf[0][0].size == 560) {
        const float *f = gptr(st.ubuf[0][0].addr);
        LOG("\n  vs ubo0:");
        for (int i = 0; i < 36; i++) LOG("%s[%d] %g %g %g %g", i % 2 ? " " : "\n   ", i, f[i*4], f[i*4+1], f[i*4+2], f[i*4+3]);
    }
    LOG(" blend %#x ds %02x%02x%02x poly %02x%02x scissor %d,%d %dx%d chmask %08x stencil %02x%02x%02x%02x%02x ref %u/%u wm %x vm %x bf %d,%d,%d,%d,%d,%d\n", st.color_state[0], st.ds[0], st.ds[1], st.ds[2], st.poly[0], st.poly[1],
        st.scissor[0][0], st.scissor[0][1], st.scissor[0][2], st.scissor[0][3], st.channel_mask, st.ds[3], st.ds[4], st.ds[5], st.ds[6],
        st.ds[7], st.stencil_ref[0], st.stencil_ref[1], st.stencil_mask[0], st.stencil_vmask[0], st.blend[0][1], st.blend[0][2],
        st.blend[0][3], st.blend[0][4], st.blend[0][5], st.blend[0][6]);
}


// ---- pipeline cache keyed by a hash of the raw state that shapes the pipeline (avoids building a
// descriptor and a string key per draw)
// Pipelines are keyed by their shader functions' addresses: a reboot frees the old functions, so keys carry the
// boot generation (a build still in flight can't land on a new function at a reused address).
static unsigned g_boot_gen;
static NSMutableDictionary *g_ready_pipes;

static uint64_t hmix(uint64_t h, const void *p, size_t n) {
    const uint8_t *b = p;
    for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 0x100000001B3ull; }
    return h;
}
#define PIPE_TAB 8192
static struct { uint64_t key; void *pipe; } pipe_tab[PIPE_TAB];

static uint64_t pipeline_state_hash(Prog *pr) {
    uint64_t h = 0xcbf29ce484222325ull;
    h = hmix(h, &pr, sizeof pr);
    h = hmix(h, &st.rt.ncolor, sizeof st.rt.ncolor);
    for (int i = 0; i < st.rt.ncolor && i < 8; i++) {
        NvnTexture *t = st.rt.color[i];
        uint32_t v[4] = {t ? t->format : 0, t ? t->samples : 0, st.rt.has_cview[i], st.rt.has_cview[i] ? st.rt.cview[i].format | (st.rt.cview[i].setmask << 16) : 0};
        h = hmix(h, v, sizeof v);
        h = hmix(h, st.blend[i], 8);
    }
    uint32_t dfmt = st.rt.depth ? st.rt.depth->format : 0;
    h = hmix(h, &dfmt, 4);
    h = hmix(h, &st.nattrib, sizeof st.nattrib);
    h = hmix(h, st.attrib, sizeof st.attrib[0] * (size_t)(st.nattrib < 32 ? st.nattrib : 32));
    h = hmix(h, &st.nstream, sizeof st.nstream);
    h = hmix(h, st.stream, sizeof st.stream[0] * (size_t)(st.nstream < 16 ? st.nstream : 16));
    h = hmix(h, &st.channel_mask, sizeof st.channel_mask);
    h = hmix(h, &st.color_state[0], sizeof st.color_state[0]);
    h = hmix(h, &st.ms[0xC], 1);
    return h ? h : 1;
}

// Draws whose program or pipeline is still compiling are skipped, which is harmless for a target the game redraws
// every frame. Others (an environment cube captured once, a BRDF lookup table drawn at boot) would stay empty, and
// the lighting divides by them: those draws wait for the compile. True when a bound target was not drawn into
// during the previous frame (marks this frame's use).
static bool one_shot_target(void) {
    uint64_t now = renderer_present_count();
    bool once = false;
    for (int i = 0; i <= st.rt.ncolor; i++) {
        NvnTexture *t = i < st.rt.ncolor ? st.rt.color[i] : st.rt.depth;
        if (!t) continue;
        if (t->draw_frame != now) { t->draw_prev_frame = t->draw_frame; t->draw_frame = now; }
        once |= !t->draw_prev_frame || t->draw_prev_frame + 1 < now;
    }
    return once;
}

uint64_t g_rt_watch_addr;  // debug: draws into the colour target at this address are logged ([rtw])
// FIFA_DBG_RTW=<addr>: every render-target bind, copy, clear and image binding of the texture at <addr> ([rtw]).
// FIFA_DBG_RTW_SIZE=<w>x<h>x<d>: the same for every texture of that size (addresses that change between runs).
static bool rtw(const NvnTexture *t) {
    static int init;
    static unsigned ww, wh, wd;
    if (!init) {
        init = 1;
        const char *e = getenv("FIFA_DBG_RTW");
        if (e) g_rt_watch_addr = strtoull(e, NULL, 16);
        if ((e = getenv("FIFA_DBG_RTW_SIZE"))) sscanf(e, "%ux%ux%u", &ww, &wh, &wd);
    }
    if (t && ww && t->width == ww && t->height == wh && t->depth == wd) return true;
    return t && g_rt_watch_addr && t->addr == g_rt_watch_addr;
}
static void draw(CmdRec *r, bool indexed) {
    if (g_rt_watch_addr) {
        static int n;
        for (int i = 0; i < st.rt.ncolor && n < 80; i++)
            if (st.rt.color[i] && st.rt.color[i]->addr == g_rt_watch_addr) {
                n++;
                LOG("[rtw] frame %llu draw rt%d %ux%u vs %llx fs %llx\n", (unsigned long long)renderer_present_count(), i,
                    st.rt.color[i]->width, st.rt.color[i]->height, st.program ? st.program->stages[0].code_addr : 0,
                    st.program ? st.program->stages[1].code_addr : 0);
                break;
            }
    }
    if (!st.program) { SKIP(0); return; }
    static bool logged_modes;
    if (!logged_modes) {
        logged_modes = true;
        const char *cb = getenv("FIFA_CBUF_BASE"), *tb = getenv("FIFA_TEX_BASE");
        if (cb) cbuf_base = atoi(cb);
        if (tb) tex_base = atoi(tb);
    }
    ShaderKey vkey = {0}, fkey = {0};
    vkey.flip_y = fkey.flip_y = nvn_window_origin() == 0;
    vkey.depth_minus_one = nvn_depth_mode() == 0;
    for (int i = 0; i < st.nattrib; i++) vkey.attr_kind[i] = (uint8_t)attr_kind(st.attrib[i] & 0xFF);
    for (int i = 0; i < st.rt.ncolor; i++) if (st.rt.color[i]) fkey.rt_kind[i] = (uint8_t)rt_kind(st.rt.color[i]->format);
    if (st.color_state[0] & 1)  // target 0 blends with SRC1 factors: the shader's target 1 output is that source
        for (int k = 1; k <= 4; k++) fkey.dual_source |= st.blend[0][k] >= 0x10 && st.blend[0][k] <= 0x13;
    Prog *pr = get_program(st.program, &vkey, &fkey);
    bool must_draw = one_shot_target();
    if (pr && !pr->ready && must_draw) {
        double t0 = now_ms();
        bool slow = false;
        while (!pr->ready && !__atomic_load_n(&pr->failed, __ATOMIC_ACQUIRE)) {
            usleep(100);
            if (!slow && now_ms() - t0 > 2000) {  // (a pathological Metal compile shows here, not as a hang)
                slow = true;
                LOG("[metal] waiting > 2 s for program vs %llx fs %llx to compile\n",
                    (unsigned long long)st.program->stages[0].code_addr, (unsigned long long)st.program->stages[1].code_addr);
            }
        }
        stat_compile_wait_ms += now_ms() - t0;
    }
    if (!pr || !pr->ready) { SKIP(1); return; }
    if (!prof_pass_fs) { prof_pass_fs = st.program->stages[1].code_addr; prof_pass_vs = st.program->stages[0].code_addr; }
    static int log_rt_w = -1, log_rt_h;
    if (log_rt_w < 0) { const char *e = getenv("FIFA_LOG_RT"); log_rt_w = 0; if (e) sscanf(e, "%dx%d", &log_rt_w, &log_rt_h); }
    static uint64_t log_rt_addr = 1;
    if (log_rt_addr == 1) { const char *e = getenv("FIFA_LOG_RT_ADDR"); log_rt_addr = e ? strtoull(e, NULL, 16) : 0; }
    bool addr_hit = false;
    for (int i = 0; log_rt_addr && i < st.rt.ncolor; i++)
        if (st.rt.color[i] && st.rt.color[i]->addr <= log_rt_addr + 0x40000 && st.rt.color[i]->addr + 0x40000 >= log_rt_addr) addr_hit = true;
    if (addr_hit) LOG("[rtaddr] rt0 addr %llx\n", st.rt.color[0] ? st.rt.color[0]->addr : 0);
    if (g_debug_frame || addr_hit || (log_rt_w && st.rt.ncolor && st.rt.color[0] && (int)st.rt.color[0]->width == log_rt_w &&
                          (int)st.rt.color[0]->height == log_rt_h))
        debug_draw(pr, r, indexed);
    resolve_feedback(pr);
    // this draw's writes make snapshots of its attachments stale
    snap_reason = "draw-depthwrite";
    if (st.rt.depth && st.ds[0] && st.ds[1]) snapshots_invalidate(st.rt.depth);  // stencil-only writes: shaders sample depth
    snap_reason = "draw-colorwrite";
    for (int i = 0; i < st.rt.ncolor; i++)
        if (st.rt.color[i] && ((st.channel_mask >> (i * 4)) & 0xF)) snapshots_invalidate(st.rt.color[i]);
    begin_pass();

    // vertex streams this draw reads (also computed inside the pipeline build on a cache miss)
    uint32_t used_streams = 0;
    for (int i = 0; i < 32; i++) {
        if (!(pr->vi->attr_mask & (1u << i))) continue;
        uint32_t a = i < st.nattrib ? st.attrib[i] : 0x2E;
        if (i >= st.nattrib || (a & 0xFF) == 0) used_streams |= 1u << 31;
        else used_streams |= 1u << ((a >> 8) & 0x1F);
    }
    // pipeline
    uint64_t phash = pipeline_state_hash(pr);
    uint32_t pslot = (uint32_t)(phash ^ (phash >> 29)) & (PIPE_TAB - 1);
    while (pipe_tab[pslot].key && pipe_tab[pslot].key != phash) pslot = (pslot + 1) & (PIPE_TAB - 1);
    id<MTLRenderPipelineState> pipe = pipe_tab[pslot].key ? (__bridge id<MTLRenderPipelineState>)pipe_tab[pslot].pipe : nil;
    if (!pipe) {
    MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
    pd.vertexFunction = pr->vs;
    pd.fragmentFunction = pr->fs;
    attachment_formats(pd);
    MTLVertexDescriptor *vd = [MTLVertexDescriptor vertexDescriptor];
    for (int i = 0; i < 32; i++) {
        if (!(pr->vi->attr_mask & (1u << i))) continue;
        uint32_t a = i < st.nattrib ? st.attrib[i] : 0x2E;
        uint32_t fmt = a & 0xFF, stream = (a >> 8) & 0x1F, off = a >> 13;
        if (i >= st.nattrib || fmt == 0) {
            // unbound attribute: read zeros from a constant stream
            vd.attributes[i].format = MTLVertexFormatFloat4;
            vd.attributes[i].offset = 0;
            vd.attributes[i].bufferIndex = 30;
            vd.layouts[30].stepFunction = MTLVertexStepFunctionConstant;
            vd.layouts[30].stepRate = 0;
            vd.layouts[30].stride = 16;
            used_streams |= 1u << 31;
            continue;
        }
        vd.attributes[i].format = vertex_format(fmt);
        vd.attributes[i].offset = off;
        vd.attributes[i].bufferIndex = 18 + stream;
        used_streams |= 1u << stream;
        (void)is_int_format;
    }
    for (int s = 0; s < 12; s++) {
        if (!(used_streams & (1u << s))) continue;
        uint32_t stride = s < st.nstream ? st.stream[s][0] : 0, div = s < st.nstream ? st.stream[s][1] : 0;
        if (stride == 0) { vd.layouts[18 + s].stepFunction = MTLVertexStepFunctionConstant; vd.layouts[18 + s].stepRate = 0; vd.layouts[18 + s].stride = 16; }
        else {
            vd.layouts[18 + s].stride = (stride + 3) & ~3u;
            if (div) { vd.layouts[18 + s].stepFunction = MTLVertexStepFunctionPerInstance; vd.layouts[18 + s].stepRate = div; }
        }
    }
    pd.vertexDescriptor = vd;
    for (int i = 0; i < st.rt.ncolor && i < 8; i++) {
        if (!st.rt.color[i]) continue;
        MTLRenderPipelineColorAttachmentDescriptor *ca = pd.colorAttachments[i];
        uint32_t m = (st.channel_mask >> (i * 4)) & 0xF;
        ca.writeMask = (m & 1 ? MTLColorWriteMaskRed : 0) | (m & 2 ? MTLColorWriteMaskGreen : 0) |
                       (m & 4 ? MTLColorWriteMaskBlue : 0) | (m & 8 ? MTLColorWriteMaskAlpha : 0);
        bool blend_on = (st.color_state[0] >> i) & 1;
        if (blend_on && !rt_kind(st.rt.color[i]->format)) {
            uint8_t *b = st.blend[i];
            ca.blendingEnabled = YES;
            ca.sourceRGBBlendFactor = blend_factor(b[1]);
            ca.destinationRGBBlendFactor = blend_factor(b[2]);
            ca.sourceAlphaBlendFactor = blend_factor(b[3]);
            ca.destinationAlphaBlendFactor = blend_factor(b[4]);
            ca.rgbBlendOperation = blend_op(b[5]);
            ca.alphaBlendOperation = blend_op(b[6]);
        }
    }
    pd.alphaToCoverageEnabled = st.ms[0xC] != 0;
    NSMutableString *pk = [NSMutableString stringWithFormat:@"%u %p %p %lu %lu", g_boot_gen, pr->vs, pr->fs,
                           (unsigned long)pd.depthAttachmentPixelFormat, (unsigned long)pd.stencilAttachmentPixelFormat];
    for (int i = 0; i < 8; i++) {
        MTLRenderPipelineColorAttachmentDescriptor *ca = pd.colorAttachments[i];
        [pk appendFormat:@" %lu/%lu/%d/%lu/%lu/%lu/%lu/%lu/%lu", (unsigned long)ca.pixelFormat, (unsigned long)ca.writeMask,
                         ca.blendingEnabled, (unsigned long)ca.sourceRGBBlendFactor, (unsigned long)ca.destinationRGBBlendFactor,
                         (unsigned long)ca.sourceAlphaBlendFactor, (unsigned long)ca.destinationAlphaBlendFactor,
                         (unsigned long)ca.rgbBlendOperation, (unsigned long)ca.alphaBlendOperation];
    }
    for (int i = 0; i < 32; i++)
        if (pr->vi->attr_mask & (1u << i))
            [pk appendFormat:@" a%d:%lu/%lu/%lu", i, (unsigned long)vd.attributes[i].format, (unsigned long)vd.attributes[i].offset, (unsigned long)vd.attributes[i].bufferIndex];
    for (int s = 18; s < 31; s++)
        [pk appendFormat:@" l%lu/%lu/%lu", (unsigned long)vd.layouts[s].stride, (unsigned long)vd.layouts[s].stepFunction, (unsigned long)vd.layouts[s].stepRate];
    [pk appendFormat:@" s%lu a%d", (unsigned long)pd.rasterSampleCount, pd.alphaToCoverageEnabled];
    id pobj = cache[pk];
    NSMutableDictionary *ready_pipes = g_ready_pipes;  // async pipeline builds finished on Metal's threads
    static id pending;
    if (!ready_pipes) { ready_pipes = g_ready_pipes = [NSMutableDictionary new]; pending = [NSObject new]; }
    if (pobj == pending) {
        @synchronized(ready_pipes) {
            id done = ready_pipes[pk];
            if (done) { [ready_pipes removeObjectForKey:pk]; cache[pk] = done; pobj = done; }
        }
        if (pobj == pending && must_draw) {
            double t0 = now_ms();
            bool slow = false;
            while (pobj == pending) {
                usleep(100);
                if (!slow && now_ms() - t0 > 2000) {
                    slow = true;
                    LOG("[metal] waiting > 2 s for the pipeline of vs %llx fs %llx\n",
                        (unsigned long long)st.program->stages[0].code_addr, (unsigned long long)st.program->stages[1].code_addr);
                }
                @synchronized(ready_pipes) {
                    id done = ready_pipes[pk];
                    if (done) { [ready_pipes removeObjectForKey:pk]; cache[pk] = done; pobj = done; }
                }
            }
            stat_compile_wait_ms += now_ms() - t0;
        }
        if (pobj == pending) { SKIP(2); return; }
    }
    if (!pobj) {
        if (sync_shaders() || must_draw) {
            NSError *err;
            pobj = TIMED_COMPILE([dev newRenderPipelineStateWithDescriptor:pd error:&err]);
            if (!pobj) {
                static int n;
                if (n++ < 10) LOG("[metal] pipeline failed: %s\n", err.localizedDescription.UTF8String);
                pobj = [NSNull null];
            }
            cache[pk] = pobj;
        } else {
            cache[pk] = pending;
            NSString *k = [pk copy];
            double t0 = now_ms();
            [dev newRenderPipelineStateWithDescriptor:pd completionHandler:^(id<MTLRenderPipelineState> ps, NSError *e) {
                if (!ps) { static int n; if (n++ < 10) LOG("[metal] pipeline failed: %s\n", e.localizedDescription.UTF8String); }
                @synchronized(ready_pipes) { ready_pipes[k] = ps ? (id)ps : (id)[NSNull null]; }
                @synchronized(cache) { stat_compile_ms += now_ms() - t0; }
                __atomic_fetch_add(&stat_compiles, 1, __ATOMIC_RELAXED);
            }];
            SKIP(3);
            return;
        }
    }
    if (pobj == [NSNull null]) { SKIP(4); return; }
    pipe = pobj;
    pipe_tab[pslot].key = phash;
    pipe_tab[pslot].pipe = (__bridge void *)pipe;  // retained by the cache dictionary
    }
    [enc setRenderPipelineState:pipe];

    // depth / stencil
    static uint64_t last_dsk;
    static id<MTLDepthStencilState> last_dss;
    uint64_t dsk = hmix(hmix(hmix(0xcbf29ce484222325ull, st.ds, 8), st.stencil_mask, sizeof st.stencil_mask), st.stencil_vmask, sizeof st.stencil_vmask);
    id<MTLDepthStencilState> dss = dsk == last_dsk ? last_dss : nil;
    NSString *dk = nil;
    if (!dss) {
        dk = [NSString stringWithFormat:@"ds %02x%02x%02x%02x%02x%02x%02x%02x %x %x %x %x", st.ds[0], st.ds[1], st.ds[2], st.ds[3],
                    st.ds[4], st.ds[5], st.ds[6], st.ds[7], st.stencil_mask[0], st.stencil_vmask[0], st.stencil_mask[1], st.stencil_vmask[1]];
        dss = cache[dk];
    }
    if (!dss) {
        MTLDepthStencilDescriptor *dd = [MTLDepthStencilDescriptor new];
        static int nodt = -1;
        if (nodt < 0) nodt = getenv("FIFA_NO_DEPTH_TEST") != NULL;
        if (st.ds[0] && !nodt) { dd.depthCompareFunction = compare_fn(st.ds[2]); dd.depthWriteEnabled = st.ds[1] != 0; }
        else { dd.depthCompareFunction = MTLCompareFunctionAlways; dd.depthWriteEnabled = NO; }
        if (st.ds[3]) {
            uint32_t ops = st.ds[5] | st.ds[6] << 8 | st.ds[7] << 16;
            for (int f = 0; f < 2; f++) {
                MTLStencilDescriptor *sd = [MTLStencilDescriptor new];
                sd.stencilCompareFunction = compare_fn(f ? st.ds[4] >> 4 : st.ds[4] & 0xF);
                uint32_t o = f ? ops >> 12 : ops;
                sd.stencilFailureOperation = stencil_op(o & 0xF);
                sd.depthFailureOperation = stencil_op((o >> 4) & 0xF);
                sd.depthStencilPassOperation = stencil_op((o >> 8) & 0xF);
                sd.readMask = st.stencil_vmask[f] & 0xFF;
                sd.writeMask = st.stencil_mask[f] & 0xFF;
                if (f) dd.backFaceStencil = sd; else dd.frontFaceStencil = sd;
            }
        }
        dss = [dev newDepthStencilStateWithDescriptor:dd];
        cache[dk] = dss;
    }
    [enc setDepthStencilState:dss];
    last_dsk = dsk;
    last_dss = dss;
    [enc setStencilFrontReferenceValue:st.stencil_ref[0] backReferenceValue:st.stencil_ref[1]];
    [enc setBlendColorRed:st.blend_color[0] green:st.blend_color[1] blue:st.blend_color[2] alpha:st.blend_color[3]];

    // rasterizer
    uint8_t cull = st.poly[0];
    if (vkey.flip_x) cull = 0;  // (mirror mode's composite: one full-screen quad, either winding)
    [enc setCullMode:cull == 1 ? MTLCullModeFront : cull == 2 ? MTLCullModeBack : MTLCullModeNone];
    bool ccw = st.poly[1] == 1;
    [enc setFrontFacingWinding:(ccw != vkey.flip_y) ? MTLWindingCounterClockwise : MTLWindingClockwise];
    [enc setTriangleFillMode:st.poly[2] == 1 ? MTLTriangleFillModeLines : MTLTriangleFillModeFill];
    {   // polygon offset (decals on floors: without it they z-fight). Enables: bit 2 = fill (NVN PolygonOffsetEnable)
        static float last[3] = {0, 0, 0};
        float f[3] = {0, 0, 0};
        static int off = -1;  // FIFA_NO_DEPTH_BIAS: ignore the game's polygon offset (A/B tests)
        if (off < 0) off = getenv("FIFA_NO_DEPTH_BIAS") != NULL;
        if ((st.poly[3] & 4) && !off) memcpy(f, st.poly_offset, sizeof f);
        static int nlog;
        if ((st.poly[3] & 4) && nlog < 3 && getenv("FIFA_LOG_BIAS")) { nlog++; LOG("[bias] factor %g units %g clamp %g\n", st.poly_offset[0], st.poly_offset[1], st.poly_offset[2]); }
        if (memcmp(f, last, sizeof f) || enc != last_bias_enc) {
            [enc setDepthBias:f[1] slopeScale:f[0] clamp:f[2]];
            memcpy(last, f, sizeof f);
            last_bias_enc = enc;
        }
    }
    float *vp = st.viewport[0];
    float zn = st.depth_range[0][0], zf = st.depth_range[0][1];

    // resources
    bind_cbufs(0, pr->vi);
    bind_cbufs(1, pr->fi);
    for (int sg = 0; sg < 2; sg++) {
        if (!(sg ? pr->fi : pr->vi)->uses_gmem) continue;
        uint64_t win[GMEM_WINDOWS][4];
        __unsafe_unretained id<MTLBuffer> bufs[GMEM_WINDOWS];
        int n = gmem_windows(sg, win, bufs);
        for (int k = 0; k < n; k++)
            [enc useResource:bufs[k] usage:MTLResourceUsageRead | MTLResourceUsageWrite stages:sg ? MTLRenderStageFragment : MTLRenderStageVertex];
        if (sg) [enc setFragmentBytes:win length:sizeof win atIndex:GMEM_BUFFER_INDEX];
        else [enc setVertexBytes:win length:sizeof win atIndex:GMEM_BUFFER_INDEX];
    }
    static id<MTLBuffer> dbgvs;
    bool dbg_this = maxwell_dbg_vs && (const uint8_t *)gptr(st.program->stages[0].code_addr) == maxwell_dbg_vs;
    if (dbg_this) {
        if (!dbgvs) dbgvs = [dev newBufferWithLength:2 * 64 * 8 * 16 options:MTLResourceStorageModeShared];
        memset(dbgvs.contents, 0xff, 2 * 64 * 8 * 16);
        [enc setVertexBuffer:dbgvs offset:0 atIndex:DBG_VS_BUFFER_INDEX];
    }
    bind_textures(0, pr->vi);
    bind_textures(1, pr->fi);
    vp_sx = pass_sx; vp_sy = pass_sy; vp_ox = 0; vp_oy = 0;
    [enc setViewport:(MTLViewport){vp[0] * vp_sx + vp_ox, vp[1] * vp_sy + vp_oy, vp[2] * vp_sx, vp[3] * vp_sy, zn, zf}];
    apply_scissor();
    if (pr->fi->uses_rscale) {  // [0] = render target scale, [1 + i] = scale of texture i
        float rs[65][4] = {{vp_sx, vp_sy, vp_ox, vp_oy}};
        for (int i = 0; i < pr->fi->ntex && i < 64; i++) { rs[1 + i][0] = tex_sc[1][i][0]; rs[1 + i][1] = tex_sc[1][i][1]; }
        [enc setFragmentBytes:rs length:(NSUInteger)(1 + pr->fi->ntex) * 16 atIndex:RSCALE_BUFFER_INDEX];
    }
    for (int s = 0; s < 12; s++) {
        if (!(used_streams & (1u << s))) continue;
        Range v = st.vbuf[s];
        if (v.addr) bind_guest(enc, true, v.addr, v.size ? v.size : 16, 18 + s);
        else { static const float zero[4]; [enc setVertexBytes:zero length:16 atIndex:18 + s]; }
    }
    if (used_streams & (1u << 31)) { static const float zero[4] = {0, 0, 0, 1}; [enc setVertexBytes:zero length:16 atIndex:30]; }

    uint32_t inst = indexed ? (uint32_t)r->x[6] : (uint32_t)r->x[4], base_inst = indexed ? (uint32_t)r->x[5] : (uint32_t)r->x[3];
    if (!inst) inst = 1;
    // draw
    bool native;
    uint32_t mode = (uint32_t)r->x[0];
    MTLPrimitiveType pt = prim_type(mode, &native);
    if (!indexed) {
        uint32_t first = (uint32_t)r->x[1], count = (uint32_t)r->x[2];
        if (native) {
            [enc drawPrimitives:pt vertexStart:first vertexCount:count instanceCount:inst baseInstance:base_inst];
        } else if (mode == 6 || mode == 7 || mode == 9) {
            uint32_t n;
            id<MTLBuffer> ib = expand_indices(mode, count, NULL, 0, first, &n);
            [enc drawIndexedPrimitives:MTLPrimitiveTypeTriangle indexCount:n indexType:MTLIndexTypeUInt32 indexBuffer:ib
                     indexBufferOffset:0 instanceCount:inst baseVertex:0 baseInstance:base_inst];
        } else { SKIP(5); return; }
    } else {
        uint32_t itype = (uint32_t)r->x[1], count = (uint32_t)r->x[2];
        uint64_t iaddr = r->x[3];
        int32_t base_vertex = (int32_t)r->x[4];
        if (native && itype != 0) {
            MTLIndexType it = itype == 1 ? MTLIndexTypeUInt16 : MTLIndexTypeUInt32;
            uint32_t isz = itype == 1 ? 2 : 4;
            id<MTLBuffer> ib;
            uint64_t ioff;
            ib = (iaddr % isz) == 0 ? renderer_pool_buffer(iaddr, (uint64_t)count * isz, &ioff) : nil;
            if (!ib) { ioff = ring_alloc(gptr(iaddr), (uint64_t)count * isz); ib = ring; }
            [enc drawIndexedPrimitives:pt indexCount:count indexType:it indexBuffer:ib indexBufferOffset:ioff
                         instanceCount:inst baseVertex:base_vertex baseInstance:base_inst];
        } else {
            uint32_t n;
            id<MTLBuffer> ib;
            if (native) {  // 8-bit indices: widen
                uint32_t *w = malloc(count * 4 + 4);
                for (uint32_t i = 0; i < count; i++) w[i] = ((const uint8_t *)gptr(iaddr))[i];
                ib = [dev newBufferWithBytes:w length:count * 4 + 4 options:MTLResourceStorageModeShared];
                free(w);
                n = count;
            } else {
                ib = expand_indices(mode, count, gptr(iaddr), (int)itype, 0, &n);
                pt = MTLPrimitiveTypeTriangle;
            }
            [enc drawIndexedPrimitives:pt indexCount:n indexType:MTLIndexTypeUInt32 indexBuffer:ib indexBufferOffset:0
                         instanceCount:inst baseVertex:base_vertex baseInstance:base_inst];
        }
    }
    stat_draws++;
    static int perdraw = -1;
    if (perdraw < 0) perdraw = getenv("FIFA_DBG_PERDRAW") != NULL;
    if (g_debug_frame && perdraw) { perdraw_prog = pr; perdraw_check(); }
    else if (gpu_profile() == 2 && enc) { end_pass(); st.rt_dirty = true; }
    if (dbg_this) {
        static int nlog;
        if (nlog++ < 4) {
            renderer_flush();
            const float *f = dbgvs.contents;
            LOG("[dbgvs] draw %d:", nlog);
            for (int i = 0; i < 2; i++)
                for (int v = 0; v < 4; v++) LOG(" i%d v%d (%g %g %g %g) uv(%g %g %g)", i, v, f[(i * 8 + v) * 4], f[(i * 8 + v) * 4 + 1], f[(i * 8 + v) * 4 + 2], f[(i * 8 + v) * 4 + 3],
                                                f[(512 + i * 8 + v) * 4], f[(512 + i * 8 + v) * 4 + 1], f[(512 + i * 8 + v) * 4 + 2]);
            LOG("\n");
        }
    }
}

// ------------------------------------------------------------------ texture copies
typedef struct { uint64_t tex; NvnTextureView view; uint8_t has_view; int32_t region[6]; } TexRefRec;

static const char *copy_src =
    "#include <metal_stdlib>\nusing namespace metal;\n"
    "struct V { float4 pos [[position]]; float2 uv; };\n"
    "vertex V vs(uint vid [[vertex_id]], constant float4 &r [[buffer(0)]]) {\n"
    "  float2 p = float2((vid << 1) & 2, vid & 2); V o; o.pos = float4(p.x * 2 - 1, 1 - p.y * 2, 0, 1);\n"
    "  o.uv = r.xy + p * r.zw; return o; }\n"
    "fragment float4 fs(V in [[stage_in]], texture2d<float> t [[texture(0)]], sampler s [[sampler(0)]]) {\n"
    "  return t.sample(s, in.uv); }\n"
    "struct D { float d [[depth(any)]]; };\n"
    "fragment D fs_depth(V in [[stage_in]], depth2d<float> t [[texture(0)]], sampler s [[sampler(0)]]) {\n"
    "  D o; o.d = t.sample(s, in.uv); return o; }\n";

// Bytes per block of the Metal texture backing an NVN format (RGB8 is expanded to RGBA8 on the host).
static uint64_t renderer_pixel_format_bytes(MTLPixelFormat pf, uint64_t nvn_bpb) {
    (void)pf;
    return nvn_bpb == 3 ? 4 : nvn_bpb;
}

// Linear guest memory <-> texture region. Row/image strides of 0 mean tightly packed.
static void copy_region_dims(NvnTexture *t, const TexRefRec *ref, const FormatInfo **fi, int32_t rg[6], int *level,
                             uint64_t *row, uint64_t *img) {
    *fi = nvn_format_info(t->format);
    memcpy(rg, ref->region, sizeof ref->region);
    if (!rg[3]) rg[3] = (int32_t)t->width;
    if (!rg[4]) rg[4] = (int32_t)t->height;
    if (!rg[5]) rg[5] = 1;
    *level = ref->has_view && (ref->view.setmask & 1) ? ref->view.base_level : 0;
    uint32_t wb = ((uint32_t)rg[3] + (*fi)->bw - 1) / (*fi)->bw, hb = ((uint32_t)rg[4] + (*fi)->bh - 1) / (*fi)->bh;
    *row = st.copy_row_stride ? st.copy_row_stride : (uint64_t)wb * (*fi)->bpb;
    *img = st.copy_image_stride ? st.copy_image_stride : *row * hb;
}

static void copy_buffer_to_texture(uint64_t src, const TexRefRec *dst) {
    snap_reason = "buf2tex"; snapshots_invalidate(NULL);
    NvnTexture *t = (NvnTexture *)(uintptr_t)dst->tex;
    if (!t || !src) return;
    end_pass();
    const FormatInfo *fi;
    int32_t rg[6];
    int level;
    uint64_t row, img;
    copy_region_dims(t, dst, &fi, rg, &level, &row, &img);
    if (!fi || !fi->bpb) return;
    static int nlog;
    if (nlog++ < 20) LOG("[copy] buf %llx -> tex %ux%u f%#x region %d,%d,%d %dx%dx%d row %llu lvl %d\n", src, t->width, t->height, t->format, rg[0], rg[1], rg[2], rg[3], rg[4], rg[5], row, level);
    if (t->target == 2 || rtw(t)) {  // (3D textures: colour-grading LUTs)
        static int n3;
        size_t nz = 0, n = (size_t)img * (rg[5] > 0 ? rg[5] : 1);
        for (size_t k = 0; k < n; k++) nz += ((const uint8_t *)gptr(src))[k] != 0;
        if (n3++ < 40)
            LOG("[copy3d] buf %llx -> tex %ux%ux%u f%#x target %u region %d,%d,%d %dx%dx%d row %llu img %llu lvl %d: %zu/%zu bytes nonzero\n",
                src, t->width, t->height, t->depth, t->format, t->target, rg[0], rg[1], rg[2], rg[3], rg[4], rg[5], row, img, level, nz, n);
    }
    id<MTLBlitCommandEncoder> b = [cb() blitCommandEncoder];
    id<MTLTexture> tex = renderer_texture(t, b, false);  // make sure prior CPU contents are uploaded first
    renderer_mark_gpu_owned(t);
    bool array = tex.textureType == MTLTextureType2DArray || tex.textureType == MTLTextureTypeCube;
    uint32_t hb = ((uint32_t)rg[4] + fi->bh - 1) / fi->bh, wb = ((uint32_t)rg[3] + fi->bw - 1) / fi->bw;
    uint64_t mtl_bpb = renderer_pixel_format_bytes(tex.pixelFormat, fi->bpb);
    for (int z = 0; z < (array ? rg[5] : 1); z++) {
        const uint8_t *s = gptr(src + (uint64_t)z * img);
        uint64_t out_row = (uint64_t)wb * mtl_bpb;
        uint8_t *data = malloc(out_row * hb * (array ? 1 : rg[5]));
        for (int d = 0; d < (array ? 1 : rg[5]); d++)
            for (uint32_t y = 0; y < hb; y++) {
                const uint8_t *sr = s + (uint64_t)d * img + (uint64_t)y * row;
                uint8_t *dr = data + ((uint64_t)d * hb + y) * out_row;
                if (mtl_bpb == fi->bpb) memcpy(dr, sr, out_row);
                else if (fi->bpb == 3 && mtl_bpb == 4)
                    for (uint32_t x = 0; x < wb; x++) { memcpy(dr + x * 4, sr + x * 3, 3); dr[x * 4 + 3] = 255; }
                else memset(dr, 0, out_row);
            }
        id<MTLBuffer> staging = [dev newBufferWithBytes:data length:out_row * hb * (array ? 1 : rg[5])
                                                options:MTLResourceStorageModeShared];
        free(data);
        [b copyFromBuffer:staging sourceOffset:0 sourceBytesPerRow:out_row sourceBytesPerImage:out_row * hb
               sourceSize:MTLSizeMake(rg[3], rg[4], array ? 1 : rg[5]) toTexture:tex
         destinationSlice:array ? rg[2] + z : 0 destinationLevel:level
        destinationOrigin:MTLOriginMake(rg[0], rg[1], array ? 0 : rg[2])];
    }
    [b endEncoding];
}

// Color copy with scaling: draw src rect into dst rect (physical texels).
static id<MTLTexture> copy_raw_view(id<MTLTexture> t) {
    MTLPixelFormat pf = t.pixelFormat;
    MTLPixelFormat raw = pf == MTLPixelFormatRGBA8Unorm_sRGB ? MTLPixelFormatRGBA8Unorm
                       : pf == MTLPixelFormatBGRA8Unorm_sRGB ? MTLPixelFormatBGRA8Unorm : pf;
    if (raw == pf) return t;
    id<MTLTexture> v = [t newTextureViewWithPixelFormat:raw];
    return v ? v : t;
}

static void draw_copy(id<MTLTexture> s, int sx0, int sy0, int sw, int sh, id<MTLTexture> d, int dx0, int dy0, int dw, int dh,
                      bool linear) {
    static int raw = -1;  // FIFA_COPY_RAW_SRGB: copies move values unchanged (no sRGB decode/encode on either side)
    if (raw < 0) raw = getenv("FIFA_COPY_RAW_SRGB") != NULL;
    if (raw) { s = copy_raw_view(s); d = copy_raw_view(d); }
    MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
    rp.colorAttachments[0].texture = d;
    rp.colorAttachments[0].loadAction = MTLLoadActionLoad;
    rp.colorAttachments[0].storeAction = MTLStoreActionStore;
    id<MTLRenderCommandEncoder> e = [cb() renderCommandEncoderWithDescriptor:rp];
    NSString *key = [NSString stringWithFormat:@"copy %lu", (unsigned long)d.pixelFormat];
    id<MTLRenderPipelineState> pipe = cache[key];
    if (!pipe) {
        static id<MTLLibrary> lib;
        if (!lib) lib = TIMED_COMPILE([dev newLibraryWithSource:@(copy_src) options:nil error:nil]);
        MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
        pd.vertexFunction = [lib newFunctionWithName:@"vs"];
        pd.fragmentFunction = [lib newFunctionWithName:@"fs"];
        pd.colorAttachments[0].pixelFormat = d.pixelFormat;
        pipe = TIMED_COMPILE([dev newRenderPipelineStateWithDescriptor:pd error:nil]);
        cache[key] = pipe;
    }
    static id<MTLSamplerState> lin, near;
    if (!lin) {
        MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
        near = [dev newSamplerStateWithDescriptor:sd];
        sd.minFilter = sd.magFilter = MTLSamplerMinMagFilterLinear;
        lin = [dev newSamplerStateWithDescriptor:sd];
    }
    float r[4] = {(float)sx0 / s.width, (float)sy0 / s.height, (float)sw / s.width, (float)sh / s.height};
    [e setRenderPipelineState:pipe];
    [e setViewport:(MTLViewport){dx0, dy0, dw, dh, 0, 1}];
    [e setVertexBytes:r length:16 atIndex:0];
    [e setFragmentTexture:s atIndex:0];
    [e setFragmentSamplerState:linear ? lin : near atIndex:0];
    [e drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [e endEncoding];
    stat_passes++;
}

static void copy_texture_to_buffer(const TexRefRec *src, uint64_t dst) {
    NvnTexture *t = (NvnTexture *)(uintptr_t)src->tex;
    if (!t || !dst) return;
    end_pass();
    const FormatInfo *fi;
    int32_t rg[6];
    int level;
    uint64_t row, img;
    copy_region_dims(t, src, &fi, rg, &level, &row, &img);
    if (!fi || !fi->bpb) return;
    {
        static int nlog;
        if (nlog++ < 12 || getenv("FIFA_LOG_READBACK"))
            LOG("[readback] tex %ux%u f%#x level %d region %d,%d %dx%d -> %llx row %llu\n", t->width, t->height,
                t->format, level, rg[0], rg[1], rg[3], rg[4], dst, row);
    }
    id<MTLTexture> tex = renderer_texture(t, nil, false);
    float tsx, tsy;
    renderer_texture_scale(t, &tsx, &tsy);
    if ((tsx != 1 || tsy != 1) && level == 0 && !renderer_format_is_depth(t->format)) {
        // the CPU expects guest-sized texels: filter the scaled allocation down first
        MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:tex.pixelFormat width:t->width
                                                                                      height:t->height mipmapped:NO];
        td.usage = MTLTextureUsageRenderTarget | MTLTextureUsageShaderRead;
        td.storageMode = MTLStorageModePrivate;
        id<MTLTexture> small = [dev newTextureWithDescriptor:td];
        draw_copy(tex, 0, 0, tex.width, tex.height, small, 0, 0, t->width, t->height, true);
        tex = small;
    }
    if (renderer_pixel_format_bytes(tex.pixelFormat, fi->bpb) != fi->bpb) return;
    bool array = tex.textureType == MTLTextureType2DArray || tex.textureType == MTLTextureTypeCube;
    uint64_t total = (uint64_t)(array ? rg[5] : 1) * img, boff;
    // into the pool's own buffer, else through a staging buffer copied out once the GPU is done
    id<MTLBuffer> out = renderer_pool_buffer(dst, total, &boff);
    bool staged = !out;
    if (staged) { out = [dev newBufferWithLength:total options:MTLResourceStorageModeShared]; boff = 0; }
    id<MTLBlitCommandEncoder> b = [cb() blitCommandEncoder];
    for (int z = 0; z < (array ? rg[5] : 1); z++)
        [b copyFromTexture:tex sourceSlice:array ? rg[2] + z : 0 sourceLevel:level
              sourceOrigin:MTLOriginMake(rg[0], rg[1], array ? 0 : rg[2])
                sourceSize:MTLSizeMake(rg[3], rg[4], array ? 1 : rg[5]) toBuffer:out
         destinationOffset:boff + (uint64_t)z * img destinationBytesPerRow:row destinationBytesPerImage:img];
    [b endEncoding];
    renderer_flush();  // the CPU may read the result right after the fence
    if (staged) memcpy(gptr(dst), out.contents, total);
}

static void copy_texture(const TexRefRec *src, const TexRefRec *dst, uint64_t flags) {
    snap_reason = "tex2tex"; snapshots_invalidate(NULL);
    NvnTexture *st_ = (NvnTexture *)(uintptr_t)src->tex, *dt = (NvnTexture *)(uintptr_t)dst->tex;
    if (rtw(dt) || rtw(st_))
        LOG("[rtw] copy %llx (%ux%u f%#x) -> %llx (%ux%u f%#x) flags %llx\n", st_ ? st_->addr : 0, st_ ? st_->width : 0, st_ ? st_->height : 0,
            st_ ? st_->format : 0, dt ? dt->addr : 0, dt ? dt->width : 0, dt ? dt->height : 0, dt ? dt->format : 0, flags);
    if (!st_ || !dt) return;
    end_pass();
    id<MTLTexture> s = sampled_texture(st_, &src->view, src->has_view, TEXTYPE_2D, false);
    renderer_mark_gpu_owned(dt);
    id<MTLTexture> d = view_texture(dt, &dst->view, dst->has_view, true, nil);
    const int32_t *sr = src->region, *dr = dst->region;
    if (g_debug_frame) {
        static int nl;
        uint64_t total = tegra_total_size(&st_->layout), nz = 0;
        for (uint64_t k = 0; k < total && st_->addr; k++) nz += ((const uint8_t *)gptr(st_->addr))[k] != 0;
        if (nl < 400) LOG("[copytex] src addr %llx size %llu nonzero %llu tflags %#x stride %u bh %u\n", st_->addr, total, nz, st_->flags, st_->stride, st_->layout.block_height_log2);
        if (nl++ < 400) LOG("[copytex] %ux%u f%#x (%d,%d %dx%d) -> %ux%u f%#x (%d,%d %dx%d) flags %llx src_gpu %d\n", st_->width, st_->height, st_->format,
            sr[0], sr[1], sr[3], sr[4], dt->width, dt->height, dt->format, dr[0], dr[1], dr[3], dr[4], flags, renderer_is_gpu_owned(st_));
    }
    // regions are in guest texels; scaled allocations (output resolution) need physical texels
    float ssx, ssy, dsx, dsy;
    renderer_texture_scale(st_, &ssx, &ssy);
    renderer_texture_scale(dt, &dsx, &dsy);
    int sx0 = (int)lroundf(sr[0] * ssx), sy0 = (int)lroundf(sr[1] * ssy);
    int dx0 = (int)lroundf(dr[0] * dsx), dy0 = (int)lroundf(dr[1] * dsy);
    int sw = sr[3] ? (int)lroundf(sr[3] * ssx) : (int)s.width, sh = sr[4] ? (int)lroundf(sr[4] * ssy) : (int)s.height;
    int dw = dr[3] ? (int)lroundf(dr[3] * dsx) : (int)d.width, dh = dr[4] ? (int)lroundf(dr[4] * dsy) : (int)d.height;
    static int no_depth_scale = -1;
    if (no_depth_scale < 0) no_depth_scale = getenv("FIFA_NO_DEPTH_SCALE") != NULL;
    if (!no_depth_scale && renderer_format_is_depth(dt->format) && (sw != dw || sh != dh) && s.sampleCount == 1 && dt->format != 0x31) {
        // scaled depth copy (the game builds its depth pyramid this way): draw, writing fragment depth
        MTLRenderPassDescriptor *drp = [MTLRenderPassDescriptor renderPassDescriptor];
        drp.depthAttachment.texture = d;
        drp.depthAttachment.loadAction = MTLLoadActionLoad;
        drp.depthAttachment.storeAction = MTLStoreActionStore;
        if (renderer_format_has_stencil(dt->format)) {
            drp.stencilAttachment.texture = d;
            drp.stencilAttachment.loadAction = MTLLoadActionLoad;
            drp.stencilAttachment.storeAction = MTLStoreActionStore;
        }
        id<MTLRenderCommandEncoder> e = [cb() renderCommandEncoderWithDescriptor:drp];
        NSString *key = [NSString stringWithFormat:@"copydepth %lu", (unsigned long)d.pixelFormat];
        id<MTLRenderPipelineState> pipe = cache[key];
        if (!pipe) {
            static id<MTLLibrary> dlib;
            NSError *derr = nil;
            if (!dlib) dlib = TIMED_COMPILE([dev newLibraryWithSource:@(copy_src) options:nil error:&derr]);
            if (!dlib) LOG("[copy] depth copy library: %s\n", derr.localizedDescription.UTF8String);
            MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
            pd.vertexFunction = [dlib newFunctionWithName:@"vs"];
            pd.fragmentFunction = [dlib newFunctionWithName:@"fs_depth"];
            pd.depthAttachmentPixelFormat = d.pixelFormat;
            if (renderer_format_has_stencil(dt->format)) pd.stencilAttachmentPixelFormat = d.pixelFormat;
            NSError *perr = nil;
            pipe = TIMED_COMPILE([dev newRenderPipelineStateWithDescriptor:pd error:&perr]);
            if (!pipe) { LOG("[copy] depth copy pipeline: %s\n", perr.localizedDescription.UTF8String); return; }
            cache[key] = pipe;
        }
        static id<MTLDepthStencilState> always;
        if (!always) {
            MTLDepthStencilDescriptor *dd = [MTLDepthStencilDescriptor new];
            dd.depthCompareFunction = MTLCompareFunctionAlways;
            dd.depthWriteEnabled = YES;
            always = [dev newDepthStencilStateWithDescriptor:dd];
        }
        static id<MTLSamplerState> dnear;
        if (!dnear) dnear = [dev newSamplerStateWithDescriptor:[MTLSamplerDescriptor new]];
        id<MTLTexture> sdepth = s;
        if (s.pixelFormat == MTLPixelFormatDepth32Float_Stencil8 || s.pixelFormat == MTLPixelFormatDepth24Unorm_Stencil8)
            sdepth = [s newTextureViewWithPixelFormat:s.pixelFormat];
        float r[4] = {(float)sx0 / s.width, (float)sy0 / s.height, (float)sw / s.width, (float)sh / s.height};
        [e setRenderPipelineState:pipe];
        [e setDepthStencilState:always];
        [e setViewport:(MTLViewport){dx0, dy0, dw, dh, 0, 1}];
        [e setVertexBytes:r length:16 atIndex:0];
        [e setFragmentTexture:sdepth atIndex:0];
        [e setFragmentSamplerState:dnear atIndex:0];
        [e drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
        [e endEncoding];
        stat_passes++;
        static int dbgc = -1;
        if (dbgc < 0) dbgc = getenv("FIFA_DEBUG_DEPTHCOPY") != NULL;
        if (dbgc) {
            static int nlog;
            if (nlog++ < 6) {
                id<MTLBuffer> rb = [dev newBufferWithLength:64 options:MTLResourceStorageModeShared];
                id<MTLBlitCommandEncoder> bb = [cb() blitCommandEncoder];
                MTLBlitOption opt = (s.pixelFormat == MTLPixelFormatDepth32Float_Stencil8) ? MTLBlitOptionDepthFromDepthStencil : MTLBlitOptionNone;
                [bb copyFromTexture:s sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(s.width / 2, s.height / 2, 0)
                         sourceSize:MTLSizeMake(1, 1, 1) toBuffer:rb destinationOffset:0 destinationBytesPerRow:4 destinationBytesPerImage:4 options:opt];
                opt = (d.pixelFormat == MTLPixelFormatDepth32Float_Stencil8) ? MTLBlitOptionDepthFromDepthStencil : MTLBlitOptionNone;
                [bb copyFromTexture:d sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(d.width / 2, d.height / 2, 0)
                         sourceSize:MTLSizeMake(1, 1, 1) toBuffer:rb destinationOffset:16 destinationBytesPerRow:4 destinationBytesPerImage:4 options:opt];
                [bb endEncoding];
                renderer_flush();
                const float *f = rb.contents;
                LOG("[copy] depth %lux%lu fmt %lu -> %lux%lu fmt %lu: centre src %g dst %g\n", (unsigned long)s.width, (unsigned long)s.height,
                    (unsigned long)s.pixelFormat, (unsigned long)d.width, (unsigned long)d.height, (unsigned long)d.pixelFormat, f[0], f[4]);
            }
        }
        return;
    }
#define COMPRESSED_FMT(f) (((f) >= MTLPixelFormatBC1_RGBA && (f) <= MTLPixelFormatBC7_RGBAUnorm_sRGB) || \
                           ((f) >= MTLPixelFormatPVRTC_RGB_2BPP && (f) <= MTLPixelFormatASTC_12x12_HDR))
    if (COMPRESSED_FMT(d.pixelFormat) && s.pixelFormat != d.pixelFormat) {
        // GPU texture compression: one source texel per 4x4 block (RG32/RGBA32 uint), or another compressed
        // format with the same blocks. Metal only copies between equal formats, so go through a buffer.
        MTLPixelFormat f = d.pixelFormat;
        NSUInteger bpb = (f == MTLPixelFormatBC1_RGBA || f == MTLPixelFormatBC1_RGBA_sRGB || f == MTLPixelFormatBC4_RUnorm ||
                          f == MTLPixelFormatBC4_RSnorm) ? 8 : 16;
        bool scompressed = COMPRESSED_FMT(s.pixelFormat);
        NSUInteger bw = ((NSUInteger)dw + 3) / 4, bh = ((NSUInteger)dh + 3) / 4;
        NSUInteger sx = (NSUInteger)sx0, sy = (NSUInteger)sy0;
        NSUInteger cw = scompressed ? bw * 4 : bw, ch = scompressed ? bh * 4 : bh;  // source extent, its own texels
        if (sx + cw > s.width) cw = s.width - sx;
        if (sy + ch > s.height) ch = s.height - sy;
        NSUInteger ow = scompressed ? (cw + 3) / 4 : cw, oh = scompressed ? (ch + 3) / 4 : ch;  // blocks actually copied
        NSUInteger dxw = MIN(ow * 4, d.width - (NSUInteger)dx0), dxh = MIN(oh * 4, d.height - (NSUInteger)dy0);
        if (!ow || !oh || (NSUInteger)dx0 >= d.width || (NSUInteger)dy0 >= d.height) return;
        id<MTLBuffer> tmp = [dev newBufferWithLength:ow * oh * bpb options:MTLResourceStorageModePrivate];
        id<MTLBlitCommandEncoder> b = [cb() blitCommandEncoder];
        [b copyFromTexture:s sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(sx, sy, 0) sourceSize:MTLSizeMake(cw, ch, 1)
                  toBuffer:tmp destinationOffset:0 destinationBytesPerRow:ow * bpb destinationBytesPerImage:ow * oh * bpb];
        [b copyFromBuffer:tmp sourceOffset:0 sourceBytesPerRow:ow * bpb sourceBytesPerImage:ow * oh * bpb
               sourceSize:MTLSizeMake(dxw, dxh, 1) toTexture:d destinationSlice:0 destinationLevel:0
        destinationOrigin:MTLOriginMake(dx0, dy0, 0)];
        [b endEncoding];
        static int nlog;
        if (nlog++ < 4)
            LOG("[copytex] %lu (%lux%lu) -> compressed %lu (%lux%lu) at %d,%d: %lux%lu blocks\n", (unsigned long)s.pixelFormat,
                (unsigned long)s.width, (unsigned long)s.height, (unsigned long)f, (unsigned long)d.width,
                (unsigned long)d.height, dx0, dy0, (unsigned long)ow, (unsigned long)oh);
        return;
    }
    if (renderer_format_is_depth(dt->format) || (s.pixelFormat == d.pixelFormat && sw == dw && sh == dh && s.sampleCount == d.sampleCount)) {
        id<MTLBlitCommandEncoder> b = [cb() blitCommandEncoder];
        [b copyFromTexture:s sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(sx0, sy0, 0)
                sourceSize:MTLSizeMake(MIN(sw, dw), MIN(sh, dh), 1) toTexture:d destinationSlice:0 destinationLevel:0
         destinationOrigin:MTLOriginMake(dx0, dy0, 0)];
        [b endEncoding];
        return;
    }
    draw_copy(s, sx0, sy0, sw, sh, d, dx0, dy0, dw, dh, flags & 1);
}

// ------------------------------------------------------------------ executor
// Compressed formats can't be Metal render targets. Frostbite's GPU texture compression renders BC blocks
// into a BC texture (through a uint view, on NVN); until that's emulated, such targets are left unbound.
static bool drop_unrenderable_targets(void) {
    bool dropped = false;
    for (int i = 0; i < st.rt.ncolor && i < 8; i++) {
        NvnTexture *t = st.rt.color[i];
        if (!t) continue;
        uint32_t f = st.rt.has_cview[i] && (st.rt.cview[i].setmask & 4) ? st.rt.cview[i].format : t->format;
        MTLPixelFormat base = renderer_pixel_format(t->format), pf = renderer_pixel_format(f);
#define COMPRESSED(f) (((f) >= MTLPixelFormatBC1_RGBA && (f) <= MTLPixelFormatBC7_RGBAUnorm_sRGB) || \
                       ((f) >= MTLPixelFormatPVRTC_RGB_2BPP && (f) <= MTLPixelFormatASTC_12x12_HDR))
        bool compressed = COMPRESSED(base) || COMPRESSED(pf);
        if (!compressed) continue;
        static int nlog;
        if (nlog++ < 10)
            LOG("[rt] compressed render target dropped: %ux%u fmt %#x (mtl %lu) view %d fmt %#x (mtl %lu) levels %u\n",
                t->width, t->height, t->format, (unsigned long)base, st.rt.has_cview[i], f, (unsigned long)pf, t->levels);
        st.rt.color[i] = NULL;
        dropped = true;
    }
    return dropped;
}

static void exec_list(uint64_t handle, int depth) {
    uint64_t p = handle;
    for (int guard = 0; guard < 10000000; guard++) {
        CmdRec *r = gptr(p);
        void *x = r->extra;
        if (r->op < CMD_COUNT) op_counts[r->op]++;
        switch (r->op) {
        case CMD_END: return;
        case CMD_JUMP: p = r->x[0]; continue;
        case CMD_CALL: {
            const uint64_t *h = x;
            for (uint64_t i = 0; i < r->x[0] && depth < 8; i++) exec_list(h[i], depth + 1);
            break;
        }
        case CMD_BIND_PROGRAM: st.program = (NvnProgram *)(uintptr_t)r->x[0]; break;
        case CMD_BIND_BLEND: { uint8_t *b = x; memcpy(st.blend[b[0] & 7], b, 8); break; }
        case CMD_BIND_CHANNEL_MASK: memcpy(&st.channel_mask, x, 4); break;
        case CMD_BIND_COLOR: memcpy(st.color_state, x, 4); break;
        case CMD_BIND_DEPTH_STENCIL: memcpy(st.ds, x, 8); break;
        case CMD_BIND_MULTISAMPLE: memcpy(st.ms, x, 0x18); break;
        case CMD_BIND_POLYGON: memcpy(st.poly, x, 4); break;
        case CMD_BIND_VERTEX_ATTRIB:
            st.nattrib = (int)r->x[0] > 16 ? 16 : (int)r->x[0];
            memcpy(st.attrib, x, st.nattrib * 4);
            break;
        case CMD_BIND_VERTEX_STREAM:
            st.nstream = (int)r->x[0] > 16 ? 16 : (int)r->x[0];
            memcpy(st.stream, x, st.nstream * 8);
            break;
        case CMD_BIND_VERTEX_BUFFERS:
            for (uint64_t i = 0; i < r->x[1] && r->x[0] + i < 16; i++) memcpy(&st.vbuf[r->x[0] + i], (uint8_t *)x + i * 16, 16);
            break;
        case CMD_BIND_UNIFORM_BUFFERS:
            for (uint64_t i = 0; i < r->x[2] && r->x[1] + i < 18 && r->x[0] < 6; i++)
                memcpy(&st.ubuf[r->x[0]][r->x[1] + i], (uint8_t *)x + i * 16, 16);
            break;
        case CMD_BIND_STORAGE_BUFFERS:
            for (uint64_t i = 0; i < r->x[2] && r->x[1] + i < 16 && r->x[0] < 6; i++)
                memcpy(&st.sbuf[r->x[0]][r->x[1] + i], (uint8_t *)x + i * 16, 16);
            break;
        case CMD_BIND_TEXTURES:
            for (uint64_t i = 0; i < r->x[2] && r->x[1] + i < 32 && r->x[0] < 6; i++)
                memcpy(&st.tex[r->x[0]][r->x[1] + i], (uint8_t *)x + i * 8, 8);
            break;
        case CMD_BIND_SEPARATE_TEXTURES:
            for (uint64_t i = 0; i < r->x[2] && r->x[1] + i < 128 && r->x[0] < 6; i++)
                memcpy(&st.septex[r->x[0]][r->x[1] + i], (uint8_t *)x + i * 8, 8);
            break;
        case CMD_BIND_SEPARATE_SAMPLERS:
            for (uint64_t i = 0; i < r->x[2] && r->x[1] + i < 32 && r->x[0] < 6; i++)
                memcpy(&st.sepsmp[r->x[0]][r->x[1] + i], (uint8_t *)x + i * 8, 8);
            break;
        case CMD_BIND_IMAGES:
            for (uint64_t i = 0; i < r->x[2] && r->x[1] + i < 8 && r->x[0] < 6; i++)
                memcpy(&st.img[r->x[0]][r->x[1] + i], (uint8_t *)x + i * 8, 8);
            break;
        case CMD_SET_TEXTURE_POOL: st.tpool = (NvnDescPool *)(uintptr_t)r->x[0]; break;
        case CMD_SET_SAMPLER_POOL: st.spool = (NvnDescPool *)(uintptr_t)r->x[0]; break;
        case CMD_SET_RENDER_TARGETS: {
            {
                const uint64_t *tx = x;
                for (int i = 0; i < (int)r->x[0] && i < 8; i++) {
                    NvnTexture *t = (NvnTexture *)(uintptr_t)tx[i];
                    static int nlog;
                    if (t && (t->flags & 2) && nlog++ < 5) LOG("[metal] video texture %p used as render target\n", t);
                }
            }
            const struct { uint64_t tex[8]; NvnTextureView views[8]; uint8_t has_view[8]; uint64_t depth;
                           NvnTextureView dview; uint8_t has_dview; } *rt = x;
            memset(&st.rt, 0, sizeof st.rt);
            st.rt.ncolor = (int)r->x[0];
            for (int i = 0; i < st.rt.ncolor; i++) {
                st.rt.color[i] = (NvnTexture *)(uintptr_t)rt->tex[i];
                st.rt.cview[i] = rt->views[i];
                st.rt.has_cview[i] = rt->has_view[i];
            }
            st.rt.depth = (NvnTexture *)(uintptr_t)rt->depth;
            st.rt.dview = rt->dview;
            st.rt.has_dview = rt->has_dview;
            for (int i = 0; i < st.rt.ncolor; i++)
                if (st.rt.color[i] && st.rt.color[i]->target == 2) {
                    static int n3d;
                    if (n3d++ < 80)
                        LOG("[rt3d] frame %llu rt%d %ux%ux%u addr %llx view %d (setmask %x min layer %u layers %u level %u)\n",
                            (unsigned long long)renderer_present_count(), i, st.rt.color[i]->width, st.rt.color[i]->height,
                            st.rt.color[i]->depth, st.rt.color[i]->addr, st.rt.has_cview[i], st.rt.cview[i].setmask,
                            st.rt.cview[i].min_layer, st.rt.cview[i].num_layers, st.rt.cview[i].base_level);
                }
            for (int i = 0; i < st.rt.ncolor; i++)
                if (rtw(st.rt.color[i]))
                    LOG("[rtw] frame %llu set rt%d of %d view %d (min layer %u layers %u level %u)\n", (unsigned long long)renderer_present_count(),
                        i, st.rt.ncolor, st.rt.has_cview[i], st.rt.cview[i].min_layer, st.rt.cview[i].num_layers, st.rt.cview[i].base_level);
            drop_unrenderable_targets();
            for (int i = 0; i < 8; i++)
                if (rtw((NvnTexture *)(uintptr_t)rt->tex[i]) && st.rt.color[i] != (NvnTexture *)(uintptr_t)rt->tex[i]) LOG("[rtw] dropped rt%d\n", i);
            st.rt_dirty = true;
            break;
        }
        case CMD_SET_VIEWPORTS:
            for (uint64_t i = 0; i < r->x[1] && r->x[0] + i < 16; i++) memcpy(st.viewport[r->x[0] + i], (uint8_t *)x + i * 16, 16);
            break;
        case CMD_SET_SCISSORS:
            for (uint64_t i = 0; i < r->x[1] && r->x[0] + i < 16; i++) memcpy(st.scissor[r->x[0] + i], (uint8_t *)x + i * 16, 16);
            break;
        case CMD_SET_DEPTH_RANGES:
            for (uint64_t i = 0; i < r->x[1] && r->x[0] + i < 16; i++) memcpy(st.depth_range[r->x[0] + i], (uint8_t *)x + i * 8, 8);
            break;
        case CMD_SET_BLEND_COLOR: memcpy(st.blend_color, x, 16); break;
        case CMD_SET_STENCIL: {
            int face = (int)r->x[0];
            for (int f = 0; f < 2; f++) {
                if (!(face & (1 << f))) continue;
                if (r->x[6] == 0) st.stencil_ref[f] = (uint32_t)r->x[1];
                else if (r->x[6] == 1) st.stencil_mask[f] = (uint32_t)r->x[1];
                else st.stencil_vmask[f] = (uint32_t)r->x[1];
            }
            break;
        }
        case CMD_SET_POLYGON_OFFSET: memcpy(st.poly_offset, r->f, sizeof st.poly_offset); break;
        case CMD_SET_PRIMITIVE_RESTART: st.prim_restart = r->x[0] != 0; st.restart_index = (uint32_t)r->x[1]; break;
        case CMD_SET_COPY_STRIDES: st.copy_row_stride = r->x[0]; st.copy_image_stride = r->x[1]; break;
        case CMD_CLEAR_COLOR: {
            snap_reason = "clear-color"; snapshots_invalidate(NULL);
            int index = (int)r->x[0];
            static int log_clears = -1;  // FIFA_LOG_CLEARS: every colour clear (frame, target size, mask, scissor)
            if (log_clears < 0) log_clears = getenv("FIFA_LOG_CLEARS") != NULL;
            if (log_clears && index < st.rt.ncolor && st.rt.color[index])
                LOG("[cclear] frame %llu idx %d %ux%u f%#x mask %#x scissor %d,%d %dx%d\n", (unsigned long long)renderer_present_count(),
                    index, st.rt.color[index]->width, st.rt.color[index]->height, st.rt.color[index]->format, (uint32_t)r->x[2],
                    st.scissor[0][0], st.scissor[0][1], st.scissor[0][2], st.scissor[0][3]);
            else if (log_clears) LOG("[cclear] frame %llu idx %d: no target bound (ncolor %d)\n", (unsigned long long)renderer_present_count(), index, st.rt.ncolor);
            if (index < st.rt.ncolor && st.rt.color[index]) clear(index, (int)r->x[5], x, (uint32_t)r->x[2], false, 0, false, 0, 0);
            break;
        }
        case CMD_CLEAR_TEXTURE: {
            // ClearTexture(cmd, texture, view, region, color, mask) (runtime/nvn/cmdbuf.c clear_texture). Was never
            // executed: the game clears its text-box textures this way before drawing a new message, so the old
            // text stayed behind the new one (user 2026-10-08: "Load this file?" over "Please select a file.").
            // Done as a render-target clear: bind the texture alone, scissor = region, clear(), restore.
            const struct { uint64_t tex; NvnTextureView view; uint8_t has_view; int32_t region[6]; float color[4]; } *ct = x;
            NvnTexture *t = (NvnTexture *)(uintptr_t)ct->tex;
            if (rtw(t)) LOG("[rtw] clear texture\n");
            if (!t) break;
            snap_reason = "clear-texture"; snapshots_invalidate(NULL);
            RtState saved = st.rt;
            int32_t sc[4];
            memcpy(sc, st.scissor[0], sizeof sc);
            end_pass();
            memset(&st.rt, 0, sizeof st.rt);
            st.rt.ncolor = 1;
            st.rt.color[0] = t;
            if (ct->has_view) { st.rt.cview[0] = ct->view; st.rt.has_cview[0] = 1; }
            if (drop_unrenderable_targets()) { st.rt = saved; st.rt_dirty = true; break; }
            st.rt_dirty = true;
            const int32_t *rg = ct->region;
            if (rg[3] > 0 && rg[4] > 0) { st.scissor[0][0] = rg[0]; st.scissor[0][1] = rg[1]; st.scissor[0][2] = rg[3]; st.scissor[0][3] = rg[4]; }
            else { st.scissor[0][0] = st.scissor[0][1] = 0; st.scissor[0][2] = st.scissor[0][3] = 16384; }
            static int nlog;
            if (nlog++ < 8 || g_debug_frame)
                LOG("[cleartex] %ux%u f%#x region %d,%d %dx%d color %g %g %g %g mask %#llx kind %llu\n", t->width, t->height, t->format,
                    rg[0], rg[1], rg[3], rg[4], ct->color[0], ct->color[1], ct->color[2], ct->color[3], r->x[4], r->x[6]);
            clear(0, (int)r->x[6], ct->color, (uint32_t)r->x[4] ? (uint32_t)r->x[4] : 0xF, false, 0, false, 0, 0);
            end_pass();
            st.rt = saved;
            memcpy(st.scissor[0], sc, sizeof sc);
            st.rt_dirty = true;
            break;
        }
        case CMD_CLEAR_DEPTH_STENCIL:
            snap_reason = "clear-depth"; snapshots_invalidate(NULL);
            if (st.rt.depth) {
                // (depth s0, depthMask x1, stencil x2, stencilMask x3)
                bool has_stencil = renderer_format_has_stencil(st.rt.depth->format);
                clear(-1, 0, NULL, 0, r->x[0] != 0, r->f[0], has_stencil && r->x[2] != 0, (uint32_t)r->x[1],
                      (uint32_t)r->x[2]);
            }
            break;
        case CMD_DISPATCH:
            dispatch_compute((uint32_t)r->x[0], (uint32_t)r->x[1], (uint32_t)r->x[2]);
            if (st.program) {
                static uint64_t seen[16];
                static int nseen;
                uint64_t cs = st.program->stages[5].code_addr;
                bool known = false;
                for (int i = 0; i < nseen; i++) known |= seen[i] == cs;
                if (!known && nseen < 16) {
                    seen[nseen++] = cs;
                    char path[64];
                    snprintf(path, sizeof path, "build/cs_%d.bin", nseen - 1);
                    FILE *f = fopen(path, "wb");  // (no build/ outside a source checkout: the app's data root)
                    if (f) fwrite(gptr(cs), 1, 0x4000, f), fclose(f);
                    LOG("[dispatch] new compute program %d: cs %llx groups %llu %llu %llu images:", nseen - 1, cs, r->x[0], r->x[1], r->x[2]);
                    for (int i = 0; i < 8; i++) if (st.img[5][i]) LOG(" %d=%llx", i, st.img[5][i]);
                    LOG(" tex:");
                    for (int i = 0; i < 8; i++) if (st.tex[5][i]) LOG(" %d=%llx", i, st.tex[5][i]);
                    LOG(" ubuf:");
                    for (int i = 0; i < 8; i++) if (st.ubuf[5][i].addr) LOG(" %d=%llx/%llu", i, st.ubuf[5][i].addr, st.ubuf[5][i].size);
                    LOG(" ssbo:");
                    for (int i = 0; i < 8; i++) if (st.sbuf[5][i].addr) LOG(" %d=%llx/%llu", i, st.sbuf[5][i].addr, st.sbuf[5][i].size);
                    LOG("\n");
                }
            }
            if (g_debug_frame && st.program) {
                LOG("[dispatch] %llu %llu %llu prog %p cs %llx\n", r->x[0], r->x[1], r->x[2], st.program, st.program->stages[5].code_addr);
                FILE *f = fopen("build/cs_code.bin", "wb");
                if (f) fwrite(gptr(st.program->stages[5].code_addr), 1, 0x4000, f), fclose(f);
                f = fopen("build/cs_ctrl.bin", "wb");
                if (f) fwrite(st.program->stages[5].control, 1, 0x900, f), fclose(f);
                LOG("[dispatch] images:");
                for (int i = 0; i < 8; i++) if (st.img[5][i]) LOG(" %d=%llx", i, st.img[5][i]);
                LOG(" tex:");
                for (int i = 0; i < 8; i++) if (st.tex[5][i]) LOG(" %d=%llx", i, st.tex[5][i]);
                LOG(" ubuf:");
                for (int i = 0; i < 8; i++) if (st.ubuf[5][i].addr) LOG(" %d=%llx/%llu", i, st.ubuf[5][i].addr, st.ubuf[5][i].size);
                LOG("\n");
            }
            break;
        case CMD_DRAW_ARRAYS: draw(r, false); break;
        case CMD_DRAW_ELEMENTS: draw(r, true); break;
        case CMD_UPDATE_UNIFORM_BUFFER:
            // (buffer, size, offset, dataSize, data): apply the update in command order
            memcpy(gptr(r->x[0] + r->x[2]), x, r->x[3]);
            break;
        case CMD_FENCE_SYNC: {
            // The CPU may read GPU-written memory (compute results) once the fence is signaled.
            static int fence_flush = -1;
            if (fence_flush < 0) fence_flush = getenv("FIFA_FENCE_FLUSH") ? 1 : 0;
            if (fence_flush) renderer_flush();
            extern void nvn_sync_set_seq(uint64_t guest, uint64_t seq);
            if (r->x[1]) nvn_sync_set_seq(r->x[1], renderer_fence_seq());
            break;
        }
        case CMD_REPORT_COUNTER: {
            uint64_t *out = GP(r->x[1]);
            static uint32_t seen;
            uint32_t type = (uint32_t)r->x[0];
            if (type < 32 && !(seen & (1u << type))) { seen |= 1u << type; LOG("[nvn] ReportCounter type %u\n", type); }
            // (Switch GPU ticks, 614.4 MHz: the game converts the difference to ns itself - see the Vulkan exec.c)
            if (out) { out[0] = 0; out[1] = (uint64_t)((unsigned __int128)plat_now_ns() * 384 / 625); }
            break;
        }
        case CMD_COPY_TEXTURE_TO_TEXTURE: {
            const TexRefRec *t = x;
            copy_texture(&t[0], &t[1], r->x[6]);
            break;
        }
        case CMD_DOWNSAMPLE: {
            const TexRefRec *t = x;
            copy_texture(&t[0], &t[1], 1);
            break;
        }
        case CMD_COPY_BUFFER_TO_TEXTURE: copy_buffer_to_texture(r->x[0], x); break;
        case CMD_COPY_TEXTURE_TO_BUFFER: copy_texture_to_buffer(x, r->x[3]); break;
        case CMD_COPY_BUFFER_TO_BUFFER:
            renderer_flush();
            memmove(gptr(r->x[1]), gptr(r->x[0]), r->x[2]);
            break;
        case CMD_CLEAR_BUFFER: {  // (address, size, 32-bit value)
            renderer_flush();
            uint32_t *u = gptr(r->x[0]), v = (uint32_t)r->x[2];
            for (uint64_t k = 0; k < r->x[1] / 4; k++) u[k] = v;
            if (g_debug_frame) LOG("[clearbuf] %llx size %#llx value %#x\n", (unsigned long long)r->x[0], (unsigned long long)r->x[1], v);
            break;
        }
        default: break;
        }
        p += r->size;
    }
}

// In-place reboot (runtime/nvn/metal.m backend_reset; GPU idle, the game's textures already destroyed): drop
// everything keyed by guest addresses or guest-owned objects - programs (by code address), pipelines (by program),
// views, snapshots, bound state - so the next boot can't hit a stale entry. Metal's own shader cache stays warm.
void renderer_reset(void) {
    renderer_flush();
    pthread_mutex_lock(&gone_lock);
    ngone = 0;
    pthread_mutex_unlock(&gone_lock);
    for (int i = 0; i < VIEW_TAB; i++) {
        if (view_tab[i].view) CFRelease(view_tab[i].view);
        view_tab[i].view = view_tab[i].base = NULL;
        view_tab[i].key = 0;
    }
    g_views_live = 0;
    memset(prog_tab, 0, sizeof prog_tab);
    memset(pipe_tab, 0, sizeof pipe_tab);
    [cache removeAllObjects];
    [g_cs_progs removeAllObjects];
    @synchronized(g_ready_pipes) { [g_ready_pipes removeAllObjects]; }
    g_boot_gen++;
    [g_snap_copies removeAllObjects];
    memset(snap_tex, 0, sizeof snap_tex);
    memset(snap_ok, 0, sizeof snap_ok);
    for (int i = 0; i < 4; i++) { feedback_tex[i] = NULL; feedback_copy[i] = nil; }
    nfeedback = 0;
    g_cur_fs_hash = 0;
    last_bias_enc = nil;
    pthread_mutex_lock(&cb_lock);
    last_cb = nil;
    for (int i = 0; i < CB_HIST; i++) cb_hist[i] = nil;  // (they retain the old guest heap buffer)
    pthread_mutex_unlock(&cb_lock);
    st_defaults();
}

void backend_execute(uint64_t handle) {
    @autoreleasepool {
        exec_list(handle, 0);
        static int sync_submit = -1;
        if (sync_submit < 0) sync_submit = getenv("FIFA_SYNC_SUBMIT") ? 1 : 0;
        if (sync_submit) renderer_flush();  // old behaviour: finish the GPU work before returning
        else renderer_submit();  // the guest fences (nvnSyncWait) before reusing memory the GPU reads
    }
}

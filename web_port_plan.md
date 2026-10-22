# Web port plan

Draft, 2026-10-21. Where we are: the native build (`build/fifa`, M1) boots, reaches the menus and
plays a full match at the game's 30 fps cap (NOTES.md, "Status"). This plan covers getting the same
program into a browser tab: wasm for the recompiled code and runtime, WebGPU for the renderer, game
data read at runtime from the user's own NSPs. It follows driver-web's browser build where that
carries over (`../driver-web/PLAN.md`, `tools/build_web.sh`, `web/`) and lists what doesn't.

Nothing from the game is served or committed: the page asks for the NSPs and `prod.keys`, as the
native build reads them from disk now.

## What's different from driver-web

| | driver-web | fifa-web |
|---|---|---|
| Guest | x86-32, 10 MB text | AArch64, 194 MB main + 7 MB nnSdk + 0.7 MB NRO |
| Native code size | ~65 MB wasm | 491 MB `__TEXT` natively (338k functions, 50.5M instructions) |
| Guest addresses | < 4 GB, identity-mapped in wasm32 | 64-bit; alias region 0x1_0000_0000-0x11_0000_0000, GPU pools over ~8.5 GB |
| Footprint | < 4 GB | ~1.6 GB resident, but 68 GB reserved (`MAP_NORESERVE`) |
| Game data | a folder of files | 34 GB virtual RomFS inside two encrypted NSPs (AES-CTR, BKTR) |
| GPU | D3D9 + SM2/3 → WGSL | NVN + Maxwell binaries → MSL today (`recomp/nvn/maxwell/translate.c`) |
| CPU per frame (native) | ~24 ms at 42 fps | ~28-30 ms at the 30 fps cap |

The three items that decide whether this works at all are code size, the address space and CPU
speed. They come first (phase 0) and are measured before anything is built on them.

## Phase 0: feasibility (measure, decide)

Each item ends in a number in NOTES.md and a decision recorded here.

1. **Code size and compile time.** Build a wasm object for a sample of units (say 20 of the 768,
   largest and median) with emcc at -O1/-O2/-Os and extrapolate the module size. Limits that
   matter in V8: 1 GB module size, 1M functions (338k is fine), 7.6 MB per function body (chunked
   functions are 4k instructions, check the largest). Then time a synthetic ~500 MB module in
   Chrome: Liftoff compile, TurboFan tier-up, peak memory, and whether the code cache keeps it
   between visits.
   Levers if it's too big or slow:
   - Only the closure from the roots (78% of instructions) plus what a run actually reaches
     (log `a64_call` misses natively over menus + a match, like driver-web's
     `missing_funcs.txt`).
   - Split main into several modules sharing one memory and table, the core loaded up front, the
     rest on the first `a64_call` miss into them (Emscripten dynamic linking, or our own table
     patching).
   - Smaller C from the generator: fewer flag materialisations, register sync already cut 5%.
2. **Address space.** Record which guest pages a native run touches (menus + a full match):
   walk the 68 GB mapping with `mincore` at intervals and at exit, and log every VAM / svcMap*
   call with size and caller. Questions: what's the highest address touched, how sparse is the
   55 GB Frostbite reservation, how many distinct ranges are live at once.
   Options, best first:
   - **wasm32, 4 GB.** All touched memory moved under 4 GB: hand out alias/VAM ranges from a
     compacting allocator (reserve lazily, commit on map), and shrink the 55 GB
     `VirtualAlloc_Posix` reservation by HLE or a patched size if it's only sparse use. Guard
     pages keep bounds checks free. Needs total live memory well under ~3.5 GB (1.6 GB now, plus
     the GPU pools' 671 MB that must stay CPU-visible).
   - **memory64, up to 16 GB.** Fewer layout changes, but V8 bounds-checks every memory64 access;
     measure the cost on a sample (expect a double-digit percentage on load/store-heavy code,
     30% of main's instructions).
   - Guest-to-wasm address translation through a page table: last resort, costs every access.
   Also check: guest pointers are 64-bit, so with wasm32 `G(addr)` truncates; anything that
   relies on high bits (tagged pointers, `0x1_0000_0000 + symidx*16` fake imports) has to be
   remapped below 4 GB.
3. **CPU speed.** Native is at ~28-30 ms per frame with little slack. wasm is typically 1.3-2x
   slower than native for this kind of code. Run a CPU-only slice in both (e.g. the boot to the
   main menu on a null renderer, `FIFA_HIDDEN=1` and null audio, which needs neither WebGPU nor
   the full port) and compare. That number decides how much of phase 5 is required before the
   match is playable, and whether 30 fps in a match is realistic at all.
4. **Data access.** Check that reading a 30+ GB `File` from a picked NSP with random 64 KB-1 MB
   reads from a worker is fast enough (Frostbite streams CAS chunks during play), and AES-CTR
   in wasm (or WebCrypto from the I/O worker) keeps up. Measure bytes read per second during a
   native match (`FIFA_TRACE` or a counter in `fs.c`) to have a target.

Go/no-go after phase 0. If code size or address space doesn't fit, the plan changes (e.g. a
different split of the program, or memory64 plus extra performance work) before phase 1.

## Phase 1: wasm build of the runtime, null renderer

Goal: `nnMain` reaches the main menu loop in headless Chrome with a null renderer (presents
counted, nothing drawn), like the native null-renderer runs before the Metal executor.

- Build: emcc toolchain for `build/gen` (same generator; the `-mllvm` flags are clang-specific,
  keep what emcc's clang accepts) plus `recomp/rt`, `recomp/hle`, `recomp/nvn` without the `.m`
  files. CMake or a second Makefile target `web`. `-pthread`, `-msimd128`,
  `-sSUPPORT_LONGJMP=wasm` (guest setjmp), `PROXY_TO_PTHREAD`, shared memory, COOP/COEP from
  `tools/serve.py` (copy from driver-web).
- Translator (`recomp/a64.py`, `a64_simd.py`, `cpu.h`): nothing AArch64-host specific should be
  in generated code. Check: host FRECPE/FRSQRTE on AArch64 (the table path is the wasm one), NEON
  via `__builtin` vectors (map to wasm SIMD128; difftest stays native), 128-bit exclusives
  (`ldxp`/`stxp`: wasm has no 128-bit CAS, use a striped lock), `CNTPCT_EL0` from
  `emscripten_get_now`, `yield` → nothing.
- Kernel (`recomp/rt/kernel.c`): pthreads in a worker pool sized for the game's threads plus
  ours, mutex/condvar/arbiter on `Atomics.wait` through emscripten's futex. No `mmap` tricks:
  `MAP_NORESERVE`, `zero_pages`, `PROT_NONE` guard and 16 KB page assumptions go behind the
  allocator chosen in phase 0. Null pointer guard: wasm has none; catch the common case with a
  check in debug builds only.
- Shared memory (`kshmem_ptr`) is already copy-based (no aliasing), so it carries over.
- `nn::ro` / NRO: the NRO is a third module in the same image, no change.
- Debug: console output from all workers, `FIFA_*` switches from the URL (`?env=` as in
  driver-web), a headless runner (`tools/webrun.mjs` from driver-web).

## Phase 2: game data and saves in the browser

- Page asks for the base NSP, the update NSP and `prod.keys` (File System Access API, remembered
  in IndexedDB like driver-web). Optional: the dev server serves them from disk (`--game`).
- An I/O worker owns the `File`s; guest threads issue reads through a shared queue and block on
  `Atomics.wait` (driver-web `io-worker.js`).
- Port the NSP side of `tools/nsp_extract.py` to C/JS: PFS0, NCA header (XTS with `header_key`),
  ticket → title key, ExeFS (main/sdk/rtld are compiled in, so ExeFS isn't needed at runtime, only
  checked by hash), RomFS listing and BKTR (`recomp/rt/fs.c` already does the reads). This replaces
  the `romfs_program.json` step.
- Wrong or missing keys, wrong title/version: clear error on the page before anything boots. The
  build is for update v262144 only; refuse other versions.
- Saves and the cache partition (save data, `cache/`) in OPFS, write overlay as in driver-web
  `persist-worker.js`.

## Phase 3: renderer on WebGPU

The NVN HLE path stays (option 1 in NOTES); the Metal executor (`renderer.m`, `metal.m`) gets a
WebGPU twin in C on emdawnwebgpu, with a native wgpu-native/Dawn build for debugging off the
browser, as driver-web does. The WebGPU RenderCore2 backend (option 2) stays later.

- **Shaders**: a WGSL backend in `translate.c` next to MSL. Things WGSL makes harder, each to be
  checked against the 437 dumped programs (`build/shtool` can run the WGSL path over the dumps and
  compile through Tint natively):
  - barriers in non-uniform control flow (WGSL's uniformity analysis rejects them; the divergent
    barrier fix for `16dff9100` will need restructuring),
  - lane id / lane masks / shuffles (`SHFL`, `VOTE`): need the `subgroups` feature, with a fallback,
  - image atomics (`SUATOM`, boot only), storage texture formats and read-write access,
  - no 64-bit integers; texel buffers become storage buffers.
- **Memory**: Metal no-copy `MTLBuffer`s over guest pages have no WebGPU equivalent. Pools become
  GPU buffers synced from guest memory: track CPU writes per pool page (dirty bits set from the
  places that write pools, or hashing at submit as `upload_texture` does now), `writeBuffer` dirty
  ranges before each submit. Measure how much changes per frame natively first (vertex/constant
  rings dominate). Buffer textures in place (`buffer_texture_in_place`) become storage-buffer reads.
- **Readbacks and blits**: GPU → guest copies are asynchronous in WebGPU (`mapAsync`). The game
  waits on its own fences, so the GPU thread signals the NVN sync object only after the map
  completes and the bytes are copied into guest memory. Avoid stalling the guest where the game
  doesn't wait.
- **Threads**: one GPU thread owns the device and the `OffscreenCanvas` (driver-web layout); the
  NVN queue submit hands it command buffers.
- Formats: ASTC/BCn decode (`astc.c`, Tegra deswizzle) stays on the CPU; use
  `texture-compression-bc` where the device has it to skip BCn decode.
- Exit: menus and a match render in Chrome; visual diff against native screenshots
  (`FIFA_SCREENSHOT_EVERY`) on a fixed `FIFA_PRESS` script.

## Phase 4: audio and input

- `audout:u` → AudioWorklet over a SharedArrayBuffer ring, 5.1 → stereo as now; the worklet's
  consumption releases buffers and signals the buffer event (same pacing model as CoreAudio).
  Browsers need a user gesture to start audio: the "Start" click that picks files does it.
- `hwopus`: libopus to wasm with emcc (`tools/build_opus.sh` gets a web target).
- Input: DOM keyboard (FIFA PC layout from `input.m`), Gamepad API → `host_npad`, mouse/touch →
  touch screen; pointer lock not needed.
- Fix the null-sink Select Sides hang (NOTES "Next steps" item 7) before this, since headless
  browser runs will use the null sink too.

## Phase 5: performance in the browser

Targets after phase 3: the menus at 30 fps, then a match. Tools: Chrome `--cpuprofile` per worker
(driver-web `webrun.mjs` + `cpuprofile.py`), `tools/bench.py` extended to drive the browser, runs
recorded in `runs/` as now.

Expected work, by what phase 0 shows:
- Generated code: arguments/results in C parameters (next step already in NOTES), fewer `CPU`
  syncs, `a64_call` dispatch (wasm `call_indirect` through a dense table instead of a hash).
- Memory64 bounds checks if phase 0 chose memory64.
- GPU sync: dirty tracking granularity, `writeBuffer` batching, pipeline/bind-group caches.
- The game's own settings: Frostbite render scale and quality knobs (lower resolution in the
  browser if the GPU side is the limit).

## Phase 6: packaging

- Static site: `index.html`, the wasm module(s), workers. Served with COOP/COEP; a service worker
  can add the headers where the host can't (e.g. GitHub Pages).
- Wasm module caching (V8 code cache / IndexedDB) so the second visit doesn't recompile.
- Memory: Chrome only. Safari and Firefox lack pieces (memory64 limits, WebGPU features) and are
  out of scope until it works in Chrome.
- Loading screen with progress for module compile and key/NSP checks.

## Order and milestones

- [ ] W0: phase 0 numbers in NOTES.md, decisions recorded above.
- [ ] W1: headless Chrome, null renderer, reaches the main menu loop (phases 1-2).
- [ ] W2: menus render in Chrome (phase 3, WGSL for the menu programs).
- [ ] W3: Kick Off plays a match in Chrome, with audio and input (phases 3-4).
- [ ] W4: playable frame rate in a match (phase 5).
- [ ] W5: hosted build anyone with the NSPs and keys can open (phase 6).

Native stays the reference throughout: every web change keeps the native build working, and a
web bug is reproduced natively first where it can be.

## Open questions

- How much guest memory is really touched, and can the 55 GB reservation be shrunk safely?
- Does a ~0.5 GB wasm module compile in acceptable time and memory in Chrome, or must it be split?
- How far is wasm from native on this code (decides whether a match at 30 fps is reachable)?
- WebGPU limits on the target machines: `maxStorageBuffersPerShaderStage`,
  `maxBufferSize` (the pools are 671 MB in ~200 pools, each fine on its own), subgroups support.
- Online stays stubbed (offline notice), as natively.

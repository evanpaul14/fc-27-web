# fifa-web

Goal: port EA Sports FC 27 (Switch, user-owned copy) to the web by static
recompilation of the ARM64 executable, in the same style as driver-web.
Game data is read from local files at runtime and is never committed.

## Setup

- Keys: `~/.switch/prod.keys` (needs keygen 22: `master_key_15`,
  `key_area_key_application_15`, `titlekek_15`, `header_key`). The title key
  comes from the ticket inside the NSP, so `title.keys` is not needed.
- `tools/build_zbic.sh` builds `tools/bin/zbic_dec` (needed for `main`).
- `python3 -I tools/nsp_extract.py GAME.nsp extracted [--romfs-get /path ...]`
  decrypts the ExeFS in place from the NSP (~0.5 s), writes RomFS listings to
  `extracted/romfs_*.tsv` (path, section offset, size), and extracts selected
  RomFS files. The 13.9 GB of game data stays packed.
- `python3 -I tools/nso.py extracted/exefs/main extracted/code/main` writes a
  flat memory image (`.bin`), segment/MOD0/dynamic info (`.json`) and the
  dynamic symbols (`.syms.tsv`). Segment SHA-256s match the NSO header.
- Update (v262144, see "The update"): `python3 -I tools/nsp_extract.py
  UPDATE.nsp extracted/update --base GAME.nsp [--romfs-get ...]` writes the
  new ExeFS, the patched RomFS listing, `romfs_program.json` (both NSPs) and
  `romfs_program.bktr` (relocation + subsection tables). Then
  `nso.py` per module into `extracted/update/code` (NRO/NRS via
  `--romfs-get`, `elfsyms.py` for the NRS), `analyze.py` for main and nro
  into `extracted/update/analysis` (sdk/rtld are identical: copy), and
  `recomp/image.py extracted/update/code build`; `layout.json` records the
  extracted dir, so `build.py` reads the matching analysis. Run with
  `FIFA_ROMFS=extracted/update/romfs_program.json`.

## What's in the NSP

Title 0100C49025D3E000 v0, keygen 22, SDK 22.2.0 (nnSdk and NVN).
Frostbite 2025.1.2, branch `//fifa/27-g4/rl`, CL 9154273, built 2026-08-11.

| module | text | ro | data+bss | notes |
|---|---|---|---|---|
| `main` (`FIFAGame.Main_Nx_retail`) | 194.5 MB | 11.0 MB | 15.7 + 12.2 MB | stripped; 848 imports, 31 exports; **ZBIC**-compressed |
| `sdk` (nnSdk) | 6.9 MB | 5.0 MB | 0.3 + 0.8 MB | 23.5k exported symbols |
| `rtld` | 9 KB | | | |
| RomFS `Engine.Render.Core2.PlatformNvn.nro` | 0.7 MB | | | RenderCore2 NVN backend, loaded via `nn::ro` |
| RomFS `Engine.Render.Core2.PlatformNvn.nrs` | | | | ELF for the NRO **with full symtab + DWARF** (Nintendo clang 21.1.8) |

ZBIC: NSO flag bit 7 means segments use zstd 1.5.7 with magic `ZBIC` and a
BIC-coded FSE table reader (new in 22.0.0). Decoder: kinnay/zbic @ 11b08f2,
the same commit Eden uses.

RomFS (116 files) is standard Frostbite: `Data/initfs_NX`, `layout.toc`,
`chunkmanifest`, superbundle `.toc`s under `Data/NX/`, and ~13.9 GB of `.cas`
under `Data/NX/superbundlelayout/installpackage_*`. `oo2tex.key` means Oodle
Texture is in use; expect Oodle (Kraken/etc.) compressed chunks.

### The update (0100C49025D3E800, v262144)

Branch `//fifa/27-g4/patcha`, CL 9226929, data CL 9214591, built
2026-09-18 (base: `rl`, CL 9154273, data CL 9153338, 2026-08-11). New
`main` (same size class, 300,735 function starts vs 300,643) and a new
RenderCore2 NRO/NRS; nnSdk and rtld are byte-identical.

**The base RomFS is not the whole game.** The base build's own
`Data/layout.toc` lists superbundles that only the update ships:
`contentsb`, `globalsfull`, `careersba`, `storycharsb`, `worldssb`,
`storysba`, `worldcupsb`, commentary, and `installpackage_05`..`08`,
`installpackage_licensed` (~16 GB of CAS). The game parses RomFS itself
through `IStorage`, so the missing files never surfaced as fs errors.
The update's RomFS (193 files, 34 GB virtual: 14 GB relocated from the base
section, 20 GB new) also adds a Frostbite patch layer under `/Patch/`
(`initfs_NX` (encrypted), `layout.toc`, patch TOCs and small CAS files).

BKTR: the update's RomFS section is AES-CTR-Ex (encryption 4). Its section
header holds two bucket trees at 0x100 (relocations: virtual offset,
physical offset, is_patch; 231 entries) and 0x120 (subsections: offset,
counter; 101 entries), stored at the end of the section under the plain
section counter. Base reads use the base section's key and counter; patch
reads use the update's counter with bytes 4..7 replaced by the subsection's
counter (big endian). `tools/nsp_extract.py` (`PatchedSection`) and
`recomp/rt/fs.c` implement it.

## Seams for the port

1. **SDK boundary**: `main` imports 832 functions from nnSdk (std 137, os 47,
   fs 45, hid 37, friends 37, codec 36, account 33, audio 29, socket 28, oe 24,
   ec 21, ...). Implement these as host shims (HLE at the nn:: API level, like
   the Win32 shims in driver-web) instead of emulating Horizon syscalls.
2. **Renderer**: the NRO exports a small C entry API (`rc2DeviceInit`,
   `rc2DeviceInitTemporary`, `rc2DeviceShutdown`, `rc2DeviceVersion`,
   `rc2Get/SetSystemServiceProvider`) and consumes RenderCore2's platform-neutral
   opcode IR (`fb::Opcode*`, `fb::OpcodeProcessorBase<PlatformEncoderNvn>`).
   Two ways to put WebGPU under it, compared in Status (renderer options):
   implement NVN on the host and run the recompiled NRO on top, or replace
   the NRO with a WebGPU RenderCore2 backend (no NVN emulation; the DWARF
   gives its exact types). `main` itself uses 14 NVN entry points directly
   (texture/memory-pool setup and readback), so either way needs those.

## Static analysis of main

```
python3 -I tools/analyze.py    extracted/code/main extracted/analysis/main   # ~2 s
python3 -I tools/reflection.py extracted/code/main extracted/analysis/main   # ~2 s
python3 -I tools/mkelf.py      extracted/code/main extracted/analysis/main extracted/analysis/main/main.elf
~/emsdk/upstream/bin/llvm-objdump -d --start-address=0x791d60 --stop-address=0x791e40 extracted/analysis/main/main.elf
```

- Relocations: 1.23M RELR relative + 12k RELA (ABS64/GLOB_DAT) + 770 JUMP_SLOT.
  No R_AARCH64_RELATIVE in RELA; the relative ones are all in DT_RELR.
  Imports get fake addresses at `0x1_0000_0000 + symidx*16`.
- No C++ RTTI (`-fno-rtti`), and eh_frame only covers 405 functions, so it is
  no help for function discovery.
- Function starts: 272,915 from BL targets (64k), text pointers in data (216k)
  and vtables (42k vtables, 577k slots, recognised by a run of text pointers
  after two zero qwords). 99.75% of starts directly follow a terminator. The
  whole 194 MB is real code (~1,500 RETs per MB everywhere).
- Address-taken code (source `a`, +27.7k starts → 300,643): targets of `adr`
  or `adrp`+`add`, kept only if 16-byte aligned (99.4% of known starts are)
  and right after ret/b/udf/nop. `adr`s feeding a register `add` are
  jump-table bases, not functions, and are excluded. Needed for callbacks
  that are only ever materialised in code, e.g. the memalign hook main
  hands nnmusl (0x97f6990).
- Frostbite reflection survives: 19,188 TypeInfoData records (names, size,
  GUID, super, fields), 7,697 classes with 52,488 fields → `fb_types.h`,
  `types.tsv`, `fields.tsv`. Record layout is in `tools/reflection.py`.
  Kinds seen: 4 (value types), 6 (classes, 12k), 16 (enums), 22 (interfaces),
  25 (delegates).
- Not yet resolved: primitive types and a few core bases (`DataContainer`,
  `Asset`, ...) have no static record with a runtime pointer (they're
  registered from init code), so they show up as `T_<runtime addr>`. Most common:
  `T_de8a2a0` (10.7k fields, likely Float32), `T_de8a2c0` (Boolean),
  `T_de82cc0`/`T_de82df0` (Int32/Uint32), `T_de5beb8` (array/pointer).
  Infer them from field size/name statistics, or trace the init code.
- `main.elf` (233 MB, 312k symbols: sub_*, ti_*/rt_* reflection, imports) is
  ready for Ghidra, but a full Ghidra analysis of 194 MB of code needs many GB
  of project space and isn't possible with ~2 GB free disk.

## Instruction census

```
python3 -I tools/insn_stats.py extracted/code/main extracted/analysis/main   # ~25 s, needs main.elf
```

Linear sweep of the text segment with llvm-objdump in 4 MB chunks →
`insn_mnemonics.tsv` and `insn_stats.json`. 48.2M instructions, 367 mnemonics.

| class | share | notes |
|---|---|---|
| integer ALU | 40.0% | |
| load/store (GPR) | 29.8% | |
| branch | 17.3% | |
| FP scalar + FP load/store | 7.2% | fmov, fcmp, fadd, fmul, fcsel, fdiv, fminnm, fsqrt, conversions |
| NEON vector | 2.9% | mostly `4s`/`2s` float math (Vec3/Vec4), `16b` masks/shuffles |
| system | 1.5% | 638k `nop` (alignment), 82k `udf #0` (zero words) |
| exclusives | 1.0% | ldxr/stxr/ldaxr/stlxr/clrex, 700 ldxp/stxp pairs |
| acquire/release, dmb | 0.3% | ldar*/stlr*, 62k dmb |

What this means for the recompiler:
- Plain ARMv8.0 (Cortex-A57): **no LSE atomics, no SVC** (all kernel
  access goes through nnSdk, so the import boundary really is the only OS
  seam), no SVE. A few extensions show up rarely: CRC32 (122), AES (69).
- System registers: only `TPIDR_EL0` (6.6k reads, user TLS),
  `TPIDRRO_EL0` (3.0k, Horizon thread-local region), `CNTPCT_EL0` (3.0k,
  timer), plus 6 FPCR and 1 NZCV save/restore pairs. TLS needs a per-thread
  context field, not real thread-local storage tricks.
- Atomics are LL/SC loops. Translate whole ldxr…stxr loops into C11 atomic
  CAS (pattern match) rather than emulating the monitor; ~235k sites.
- NEON is small enough to translate per instruction to wasm SIMD128 (or
  scalar C first); ~1.35M vector ops.
- The 56 undecodable words are data (literal pools / tables) right after
  terminators, so linear sweep is mostly but not entirely safe: decode by
  recursive descent from the known starts.
- `yield` (4k) appears in spin loops; map to a host yield/no-op.

## Translator (recomp/)

`recomp/a64.py` + `recomp/a64_simd.py` decode one A64 word into C against the
runtime in `recomp/rt/` (`cpu.h`: CPU struct, memory and flag helpers;
`a64_rt.c`: FRECPE/FRSQRTE tables, AES, 128-bit exclusives, traps). Guest
memory is `mem + addr`; with `mem = 0` guest pointers are host pointers.

- Coverage: `python3 -I recomp/coverage.py extracted/code/main extracted/analysis/main`
  (~3 s over 3.8M unique words). Everything in main's text translates except
  56 words that are data (`0xe7ffdefe` etc.).
- Exclusives are modelled as CAS against the value the ldxr saw (`LDX`/`STX`
  in cpu.h), so no monitor emulation or loop pattern matching is needed.
- **Difftest** (needs an AArch64 host; this Mac is an M1):
  `DIFFTEST_OUT=/some/dir recomp/difftest/run.sh [per-tag] [seed] [rounds]`
  samples words from main per decoder tag, runs each one natively inside an
  asm stub that loads/stores the whole register file, runs the translation
  from the same random state, and compares GPRs, SP, NZCV, all V registers
  and a 192 KB memory window. Not covered: branches, PC-relative, exclusives,
  system registers. NaN payload differences are ignored (clang may commute
  FP operands; wasm won't preserve payloads anyway).
  Status: 0 failures on 32,798 distinct instructions x 32 states (seeds 2, 3).

## Recompiled program (build/)

```
python3 -I recomp/image.py extracted/code build      # relocated main.img + sdk.img, layout.json, symbols.tsv
python3 -I recomp/build.py build --all                # C for all 338k functions -> build/gen (2.6 GB)
make -C recomp -j6                                    # build/fifa (native, AArch64 host)
./build/fifa                                          # FIFA_TRACE=1 logs svcs
```

Always pass `--all`: without it `build.py` emits only the closure reachable
from the roots (215k functions), which rewrites every unit and costs a full
~43 min rebuild, and running `--all` again afterwards rewrites them back.

- Layout: `[0, 0x07E00000)` is `PROT_NONE` (null guard), main at
  0x08000000, nnSdk at 0x18000000, host thunks 0x07E00000,
  unresolved imports 0x07F00000, host allocations (stacks, TLRs)
  0x1C000000, heap (svcSetHeapSize) 0x20000000, stack region (GetInfo
  14/15) 0xC0000000, alias region (GetInfo 2/3: svcMapMemory,
  svcMapPhysicalMemory) 0x1_0000_0000-0x11_0000_0000 (64 GB, see below).
  The guest space is a 68 GB `MAP_NORESERVE` mapping, so only touched
  pages cost memory.
- The alias region has to be big: nn::os VammManager (enabled by GetInfo 16)
  reserves 0x3fe00000 at the start of it for its own node pool before
  handing out anything, and the game sets a 1.2 GB heap out of 3.2 GB total
  and gets the rest through `nn::os::AllocateAddressRegion` +
  `AllocateMemoryPages`. With the old 512 MB region the search found no gap
  and returned address 0 with a success result; Frostbite's "BootArena"
  then failed its first allocation and init wrote through null pointers.
  Frostbite then reserves another ~55 GB in one piece, hence 64 GB.
  For wasm this needs a compacting HLE of the VAM calls (reserve lazily,
  commit what's touched); memory64 alone won't cover it.
- `nn::os` threads map their stacks into the stack region at a random
  address (`MapAliasStack`: QueryMemory until free, then svcMapMemory). The
  kernel records svcMapMemory destinations so QueryMemory reports them as
  taken (type Stack) and the rest of the region as free.
- Startup: the host plays rtld and calls `nn::init::Start(thread, rtld_data,
  f1, f2, f3)` with host thunks; f2 runs DT_INIT of nnSdk then main. nnSdk
  itself calls `nninitStartup` and `nnMain`. Like the real rtld, the host
  also points `nn::ro::detail::g_pLookupGlobalManualFunctionPointer` at an
  8-byte slot first.
- Both main and nnSdk are recompiled; libc, libc++ and nn::os come from
  nnSdk's own code. The OS boundary is the svc layer in
  `recomp/rt/kernel.c` (threads are host pthreads; mutex/condvar/address
  arbiter semantics follow Atmosphère). `HLE(symbol)` in `recomp/hle/*.c`
  replaces any function by a host implementation.
- Build speed:
  - Regeneration: 80 s for everything (8 processes; translations are
    cached per encoding word, ~12x fewer than addresses), under 1 s when
    nothing changed: each 256 KB bucket is regenerated only if its input
    hash (generator sources, images, the function starts in it and the
    next bucket) changes (`build/gen/keys.json`, `--force` to ignore).
  - Host replacements need no regeneration: translated functions are weak
    symbols and `build/gen/hle_table.c` (`build.py build --hle-only`)
    defines strong ones that win at link time.
  - Compilation is ~linear in code volume (~100 us per guest instruction
    with `-O1 -mllvm -enable-misched=false -mllvm -global-isel`, 2.5x faster
    than plain -O1; clang 24 vs Apple clang 21, -disable-lsr and smaller
    function chunks made no measurable difference). 256 KB buckets keep
    each job under ~0.5 GB so `make -j8` fits in 8 GB; largest units build
    first. Flags live in a function-local `Flags F` (14% smaller code,
    same compile time).
  - A full rebuild is ~43 min (50.5M instructions, 768 units). Only C
    files whose text changed are rewritten, so a generator fix that touches
    few functions recompiles in minutes. New function starts only touch
    the buckets that contain them or call them (180 new starts: 97 units,
    ~6 min). Things measured on a median unit (3.9 MB C, 18 s) that don't
    pay off: `-fno-inline` (functions are WEAK, nothing inlines anyway),
    no unwind tables (no change); `-O0` 1.6x faster but 3.5x the code;
    emitting LLVM IR (parsing `.ll` costs as much as parsing the C, `.bc`
    saves ~10%); a pass plugin keeping only sroa/early-cse/instcombine/
    simplifycfg (-13% time, +24% code; codegen gets slower on the bigger
    IR, and no -O1 pass is over 16% of pass time). Only 4.3% of
    instructions are translated twice (overlapping bodies), and compiling
    just the closure from the roots would keep 78% of the instructions.
- IPC: `recomp/rt/ipc.c` parses HIPC messages (special header, X/A/B/W/C
  descriptors, CMIF `SFCI`/`SFCO`, domains, TIPC for `sm:`) from the TLR on
  `svcSendSyncRequest` and routes them to host services registered in
  `recomp/rt/services.c`. `sm:GetServiceHandle` for an unknown service
  returns a stub session that logs each command and answers success.
- nnSdk's start sequence (from `nn::init::Start`): `_init_libc0`,
  `nnosInitialize(thread, rtld_data)`, rtld f1, `_init_libc1`,
  `nninitInitializeSdkModule`, `nninitStartup` (game), `_init_libc2`,
  rtld f2 (module inits), `nnMain`, rtld f3 (finis).
- Very large functions (383 over 6k instructions, the biggest a 143k
  instruction static initializer) are emitted as 4k-instruction chunk
  functions that return the next guest address to a driver loop; compile
  time is superlinear in function size otherwise.

## Status (2026-10-18)

`./build/fifa` gets through nnSdk init, `nninitStartup`, all 3,325 of
main's `init_array` entries and into `nnMain`, then through Frostbite's
boot: RomFS and save data, config parsing, memory setup (the 64 GB alias
region), network (offline), DLC (none), operation mode, audio
(`nn::audio::OpenDefaultAudioOut` on `audout:u`) and the start of graphics:
it opens `nvdrv` (still a stub: commands 3, 8, 13 answer success) and calls
`nn::ro::LoadModule` for `Engine.Render.Core2.PlatformNvn.nro`, which aborts
(`svcBreak 0x80000003`) because `ldr:ro` is a stub. Footprint ~1.6 GB,
about 14 s to get there.

The NRO gets NVN through `nvnBootstrapLoader` (in nnSdk), by name: it
contains 1,065 `nvn*` names. Renderer options at this point:
1. Recompile the NRO as a third module, HLE `nn::ro::LoadModule`, and
   implement NVN on the host by HLE'ing `nvnBootstrapLoader` (the LAS
   Recomp approach; its NVN objects, Tegra deswizzle, ASTC and Maxwell
   translator are reusable). Fastest to a first frame, and the game hands
   us every shader at program creation.
2. Don't load the NRO: implement the `rc2*` interface it exposes directly
   on WebGPU. Coarser calls and pipelines built up front, so less CPU per
   draw and no NVN state reconstruction (better for the web, where CPU is
   the likely bottleneck). Shaders and textures are the same work either
   way: both get Maxwell binaries and block-linear/ASTC data. Still needs
   the 14 NVN entry points `main` calls itself (Seams for the port).
Chosen: (1) first, as a quick path to a first frame and a shader dump, then
(2) as the main backend.

Progress on (1) (2026-10-18): the game boots past graphics init and runs
its frame loop, now on the Metal executor (below; before that on a null
renderer, 8.7k presents in 300 s, no crash). Pieces:
- The NRO is a third module at 0x1A000000 (`tools/nso.py` reads NRO,
  `tools/elfsyms.py` takes function starts and names from the `.nrs`).
  `recomp/hle/ro.c`: `nn::ro::LoadModule` checks the build id and runs the
  module's `_init` once (nnmusl's `__nnmusl_init_dso` sets up its TLS and
  applies its two TLSDESC relocations itself; image.py leaves them alone).
  main's 14 NVN imports are the NRO's `nvn::pfncpp_*` pointers, so they
  resolve statically.
- `recomp/nvn/`: `nvnBootstrapLoader` / `nvnDeviceGetProcAddress` give a host
  thunk per name (`boot.c`; unimplemented names stop with the name).
  objects/cmdbuf/tegra/formats/astc come from LAS Recomp (MIT), with guest
  pointers through `gptr()`/`GP()`. SDK 22 sizes differ from LAS's:
  `NVNtextureView` is 0x10 (LAS wrote 0x28 and smashed the caller's saved
  registers) and `NVNsamplerBuilder` 0x40, so both are packed. Struct sizes
  and all NVN enums are in the NRO's DWARF (`tools/dwarf_enums.py` ->
  `extracted/analysis/nro/enums.tsv`).
- `recomp/hle/vi.c` (one display/layer, no binder), `recomp/rt/hid.c`
  (applet resource with zeroed shared memory), `recomp/hle/npad.c` (one
  full-key controller as player 1 at the API level, `host_npad` holds its
  state), `recomp/rt/acc.c` (one user).
- setjmp/longjmp: calls to setjmp (direct or via PLT) get a host `_setjmp` in
  the caller's frame (`build.py`), and `recomp/hle/setjmp.c` longjmps there.
- Jump tables: SIMD/FP instructions were read as GPR definitions, and
  `cfg_const` gave up on any other definition; now 0 of 1,224 unresolved.
- Metal executor: `recomp/nvn/renderer.m` (LAS `renderer.m` without its
  game-specific parts: widescreen, mirror mode, highlight tint, texture
  patches, output scaling), `recomp/nvn/metal.m` (formats, texture uploads,
  pool buffers, present; an AppKit window instead of SDL) and the Maxwell
  translator `recomp/nvn/maxwell/` (LAS, MIT). The host main thread now runs
  the window (`ui_run`); guest `nnMain` stays on its own thread and the
  process exits when it returns. `FIFA_HIDDEN=1` renders without showing
  the window, `FIFA_WINDOW=WxH`, `FIFA_SCREENSHOT_EVERY=n`,
  `FIFA_DEBUG_FRAME=n` (logs every draw of frame n and dumps each pass's
  colour target to `build/pass_*.png` and sampled 2D textures to
  `build/dtex_*.png`), `FIFA_DUMP_SHADERS=1` (code and control sections to
  `build/shaders/<addr>_s<stage>_*.bin`), `FIFA_DBG_FS=addr:pc:reg`
  (register probe; pc `ffffffff` = solid white), `FIFA_DUMP_VB=1` (first
  6 raw vertices of stream 0 per debug-frame draw; also calls
  `dbg_vb_hook(host, guest)` for lldb), `FIFA_DUMP_VSUBO=1`,
  `FIFA_LOG_POOLS=1`; the debug frame's `[dtex]` lines give each sampled
  texture's tic, format, guest address and how many of its guest bytes are
  nonzero (CPU-written data vs GPU-only contents); LAS's
  `LARECOMP_*` switches are `FIFA_*`. Compute programs are also written to
  `build/cs_<n>.bin` as they're first dispatched.
  - GPU memory: the game's ~200 memory pools (671 MB) lie spread over
    0x26000000-0x222400000, ~8.5 GB, and an M1's `maxBufferLength` is
    4 GB, so LAS's one buffer over the heap can't work. Each pool gets a
    no-copy `MTLBuffer` over its pages (widened to 16 KB host pages;
    overlapping wraps are allowed) at `nvnMemoryPoolInitialize`; vertex,
    index and readback copies bind through `renderer_pool_buffer()`,
    anything outside a pool is copied into the ring. Virtual pools (no
    storage, 512 MB, flags 0x221) are not wrapped; their textures are
    read through the pool's mappings (see below).
  - Shader global memory (LDG/STG): the translator no longer bakes a heap
    base into MSL; shaders take `constant ulong4 &gmem_win [[buffer(17)]]` =
    {GPU address of a pool buffer, its guest address, its size} and form a
    `device uchar *` from the integer (Metal 3 `gpuAddress`, checked on the
    M1). The window is the pool of the stage's first storage buffer (else
    uniform buffer); reads outside it give 0. GLSL output is unchanged.
  - Fixes over LAS: float `ClearColor` recorded the guest's x6 as the clear
    kind (garbage -> nil fragment function); depth textures with 1D/3D
    targets (Metal has none) become 2D (arrays); cube views for one face
    asked for 6 slices from that face; texture-level swizzles (alpha-one
    formats) aren't allowed with render-target usage on M1, so the swizzle
    moved to the sampled view.
  - State when the executor landed: 30 fps but every draw skipped
    (programs failed to translate). Fixed by the compute header and
    bindless items below; current state at the end of this section.
  - Compute blobs from GLSLC have a 0x100-byte header (magic 0x12345679,
    then zeros; graphics is 0x12345678 + 0x30 header + 0x50 SPH) that the
    translator read as code ("unknown instruction 0 at 0x8"); it now skips
    it when the magic is there. Of the 16 compute programs dumped to
    `build/cs_*.bin`, 3 translate and build pipelines; the rest stop at
    bindless tex (3), SUST `eb20…` (5), SUATOM `ea70…` (2), STS `ef5c…`
    (2) and VOTE `50d9…` (1). (Opcode names are from the encoding
    patterns, not checked against the translator's tables.)
  - Bindless textures (TEX.B): every handle in the game's shaders (135 in
    the first 16 fragment programs, plus compute) is `c[2][tex] |
    c[2][smp]`, NVN separate textures and samplers combined in the
    shader. Layout of c2 per stage: combined handles at 0x20 + 8 * slot,
    separate textures at 0x168 + 8 * slot (128), separate samplers at
    0x568 + 8 * slot. The translator traces the handle register back
    through MOV / LOP.OR to the two cbuf words (`trace_handle`,
    straight-line, ignoring branches) and gives each pair a slot; the
    renderer ORs the two words (`shader_tex_handle`). Separate samplers
    used to be dropped and separate textures shared the combined table;
    they now have their own commands and tables. Fragment programs: 1 ->
    14 of 16 translate; the frame loop went from 0 to ~9,000 draws per
    2 s, and the FC 27 title screen renders (`FIFA_SCREENSHOT_EVERY`).
    After it the presented image goes black.
  - Virtual memory pools (flag 0x200, no storage): two 512 MB pools,
    Frostbite's texture streaming heaps, filled by
    `nvnMemoryPoolMapVirtual` with whole ~2 MB physical pools after the
    textures are placed in them. The stub left texture addresses at the
    pool offset, and the first upload after the title read guest
    0x16xxxxx (null-region crash in `renderer_texture`). Pools now keep
    their mapping table; `nvn_texture_data` resolves a texture through it
    on each upload (in place when the range is physically contiguous,
    gathered otherwise, skipped while unmapped). Virtual-pool textures
    have `addr` 0. Runs 3+ min at 30 fps, ~11,500 draws and ~120
    skipped per 2 s; still black after the title.
  - Black after the title (frame ~100 on), on all three window buffers.
    Not found yet; ruled out so far:
    - present/acquire: indices rotate 0,1,2 and the composite
      (`fs=16ebcaf00`) renders into the buffer that is presented.
      `FIFA_SCREENSHOT_EVERY` with a multiple of 3 only ever sees
      buffer 0 - use e.g. 31.
    - depth (`FIFA_NO_DEPTH_TEST` changes nothing), blend factor mapping,
      vertex format 0x25/0x2E mapping.
    - bindless resolution: the handles OR to valid tic/tsc ids; the 1x1
      textures some passes bind are the game's own black/white defaults
      (tic 260/301/302 black, 299 white), not unresolved handles.
    - skipped draws: besides one-off async compiles, only the programs
      that fail to translate (FS 16f50ba00 SUST, FS 16e8db200 `3b34…`,
      16dea1200 with VS `1887…`), ~2 per frame.
    What the frame does instead: the title background is only drawn into
    a 320x180 downsample; 135 UI quads (`vs=16e001500 fs=16ea35100`)
    have all four vertices identical and colour attribute 0 (collapsed /
    alpha 0, i.e. hidden), so nothing visible lands in the UI target. It
    looks like the game legitimately shows nothing - a fade or a screen
    waiting on something (input: hid is a stub; a movie; a load) - but
    that isn't confirmed.
    Findings (2026-10-18, second pass):
    - The white probe works: `FIFA_DBG_FS=16ebcaf00:ffffffff:0` makes
      every presented frame white, black ones included, so the composite
      runs every frame and its inputs are black. (The earlier all-zero
      result was stale.)
    - The 135 quads are text. Format: a0 = (x, y, 0, -1) with w = -1
      meaning "alpha from the texture's red channel", a1 = uv, a2 = add
      colour 0, a3 = multiply colour (0,0,0,1). They sample tic 472, a
      1364x1364 R8 glyph atlas (flags 0) whose *guest* bytes are all zero,
      and all six vertices of every quad are byte-identical in guest
      memory (`FIFA_DUMP_VB`). The UVs step by exactly one texel per glyph
      (1/1364, 2/1364, ...): the glyph cache hands out 0x0 slots plus
      1 px padding. So glyphs come out empty in the game's own data, not
      in our renderer. The screen is the press-start screen
      (`FONT_DEMO_PRESS_START_SCREEN` is one of the styles).
    - UI is EA Twinkle (JS) with EAText/FreeType fonts and GPU SDF glyph
      generation (`Twinkle/GlyphGenSdf_Inside/_Outside`, programs set up
      in `sub_921cfd0`).
    - Font lookup: the font server's vtable slot 2 (`sub_5d20540`) walks
      its list of registered fonts (`[this+8]`, next `+0x18`, names
      `+0x28`/`+0x48`), then falls back to the file
      `"%s/%s.ttf"` under `data/ui/fonts` through Frostbite's VFS. Only
      `EABold` (from the file) is ever registered. `EACondBlackItalic`,
      used by most styles (`FONT_HEADER_*`, `FONT_LABEL_*_BOLD`,
      `FONT_TILE_*`), fails every frame ("Error: font %s does not exist!"
      at 0x5d20c58). It ships as an asset (`Fonts/EACondBlackItalic`,
      `Fonts/EACondBlackItalicCollection`, `TrueTypeFontAsset` /
      `UITtfFontFile`), not as a loose `.ttf`, so the asset-side font
      provider never gets it. Why is open.
    - VFS lookup (`sub_1b95f00` -> `sub_1b95ff0`): a list of mounted
      layers (vector at 0x15426f40 at run time, 0x58-byte records; per
      layer `P=[L+0x10]`, `T=[P+8]`, count `u32 [T+0xc]`, entries
      `[T+0x10]` of 0x1c bytes starting with the u64 FNV-1 hash of the
      ASCII-lowercased path, sorted, binary search). 11 layers: initfs
      (85 files), 11,373 files, and nine single-file layers. Fonts in them:
      `data/ui/fonts/{EABold,EAMedium,EAExpandedHeavy}.ttf` plus Japanese/
      SChinese variants; no `EACond*` at all, on hardware either.
    - Substituting EABold for every missing font (all fonts then load)
      changes nothing: same zero-size quads, same empty atlas. So the
      missing font is real but not sufficient. Glyph generation itself
      fails. Next: find who writes the quads, then the glyph metrics and
      GPU SDF path. Without lldb the first quad's vertices sit at guest
      0x201b1b200 in every run; under lldb (~15 fps) they land elsewhere
      (0x1e9c14000) and the screen appears much later than frame 150.
      Breaking on `dbg_vb_hit` (FIFA_DUMP_VB + FIFA_DEBUG_FRAME=1200) finds
      them, but lldb then refuses a watchpoint on that address ("Watchpoint
      creation failed"), probably because the pool's pages are wrapped by
      a no-copy MTLBuffer by then. A watchpoint set at startup on a fixed
      address did get created. Untried alternative: mprotect the page and
      report the first faulting guest pc.
    - RomFS reads (`FIFA_LOG_ROMFS=1`): every superbundle TOC except
      `storyingamesba` and the unused `loc/*.toc` gets read; no fs errors.
  - Seen once (no debug options, ~frame 200): Frostbite small-block
    allocator trap `udf #0x8002` at 0x18172fc, reached from `sub_181b310`
    when a freelist's next pointer lies outside its arena, i.e. heap
    corruption. Not investigated.
  - Seen once (with heavy debug logging, ~frame 144): guest null read
    (0x18) in `F_116768b0`; a 3-minute run without logging didn't hit it.

- `recomp/rt/apm.c`: `apm` → `ISession` (Set/GetPerformanceConfiguration,
  per-mode configs; reports boost mode to match am's docked state).
- `recomp/rt/pctl.c`: `pctl`, `pctl:s/a/r` → `IParentalControlService`
  with no restrictions, no pairing, no play timer.
- Guest `[0, 0x07E00000)` is `PROT_NONE`; crash reports flag null-region
  accesses. It immediately found that rtld must set
  `nn::ro::detail::g_pLookupGlobalManualFunctionPointer` to a slot (the
  host does this before `nn::init::Start`; `build.py` emits its address as
  `host_ro_lookup_slot_ptr` in `layout.c`).
- `svcWaitProcessWideKeyAtomic` that times out returns 0xEA01 *without*
  the mutex: nnSdk reacquires it itself and aborts if it already owns it.

- `recomp/rt/fs.c` (`fsp-srv`): `IFileSystemProxy` → `IStorage` for the
  RomFS (`OpenDataStorageByCurrentProcess`), `IFileSystem`/`IFile`/
  `IDirectory` for save data and the SD card, `ISaveDataInfoReader` (empty).
  - RomFS is read straight out of the NSP and AES-128-CTR decrypted per
    read (CommonCrypto); `tools/nsp_extract.py` writes the image offset, key
    and counter to `extracted/romfs_program.json` (`FIFA_ROMFS` overrides).
  - Save data and SD card are host directories under `build/fs`
    (`FIFA_FS` overrides): `save/<program id>/<type>_<user id>_<index>`,
    `save/system/<id>`, `sdcard`.
- IPC domains are shared between a session and its clones: nn::sf converts
  the `fsp-srv` session to a domain, clones it and sends every request on
  the clone. A `Service` can have a `close` hook, called when its domain
  object is closed, to free per-object state (open files, directories).
- Host crashes (SIGSEGV/SIGBUS, including stack overflow via an alternate
  signal stack on every guest thread) print the host pc and symbol, the
  fault address in guest terms, the guest registers and the guest
  backtrace. Guest registers live in `CPU`, so they are exact at the fault.
- Debugging recipe that worked here: add `fprintf`s to one `build/gen/f_*.c`
  unit around a call (`c->x[30] = 0x<ret>ull; F_<target>(c);`), `make`
  rebuilds just that unit, then restore it (`build.py build --all` leaves
  it alone because only inputs are hashed, so copy the original back).
  Probes can read guest memory with `G(addr)` (cpu.h) and call the helpers
  in `recomp/hle/debug.c`: `dbg_memscan(str, ctx, max)` (find a string in
  resident guest memory; this is how the font asset names and UI style
  data were found), `dbg_strings(addr, len, minlen)`. A guest address A in
  `main` is A - 0x08000000 in `main.elf`.

- `recomp/rt/am.c`: `appletOE` → `IApplicationProxy` and its
  sub-interfaces. Each interface is its own `Service`, so unknown commands
  log as `[am] <Interface> cmd N unhandled`. A launched application gets
  FocusStateChanged / OperationModeChanged / PerformanceModeChanged queued,
  reports in focus, docked, 1920x1080. Event getters return a fresh copy
  handle to a shared `KEvent`.
- Stub-service pitfall: a command that returns an interface object or a
  handle can't be stubbed, because the client then reads garbage. In a
  domain this shows up as `[ipc] bad domain object 1229145683` (`SFCI`, the
  leftover request magic read as an object id), e.g. pctl `CreateService`.
- Shared memory (`svcCreateSharedMemory`, `svcMapSharedMemory`/`Unmap`):
  no page aliasing (host pages are 16 KB, guest maps 4 KB-aligned, and
  wasm can't alias). Contents live in a host buffer while unmapped and at
  the guest address while mapped (one mapping at a time); host code goes
  through `kshmem_ptr()` on every access.
- `recomp/rt/time.c`: `time:u/a/s` in UTC. nnSdk reads clocks straight from
  the service's shared memory (`nn::time::detail::SharedMemoryClient`);
  layout taken from its readers and documented at the top of the file
  (steady clock is a `ContinuousAdjustmentTimePoint` at 0xD0, system
  clock contexts at 0x38/0x80, time = steady seconds + context offset).
- Kernel facts nnSdk relies on: the thread handle at TLR+0x110 (nn::os
  tags mutex/critical-section ownership with it and never writes it; with
  0 there, every lock looked unlocked), and a non-zero system resource
  size (GetInfo 16) to enable `nn::os` virtual address memory
  (VammManager over the alias region, backed by svcMapPhysicalMemory).
- A function whose body reaches code *below* its entry (a `b` into code
  that isn't a known start) was entered at its lowest address, because the
  body is emitted sorted: 1,285 functions in 295 units ran the wrong code.
  `emit_func` now starts with `goto L_<entry>`. This was the "garbage
  pointer after an FNV hash of CraniumSecondary" fault.
- Functions reached only through a pointer made with `adr`/`adrp+add` (one
  placed right after a call to a noreturn function looks like the caller's
  tail to the start analysis, or a leaf placed after another function's
  final `br`) were never translated. `build.py` adds such targets as starts
  when they begin with a prologue or follow a terminator, skipping `adr`s
  that are jump table bases (260 of them). A wrong start is harmless here:
  falling or branching into it becomes a call followed by a return.
- Jump tables whose base register is set far from the `br` (a callee-saved
  register loaded once at the top, or a path through another table's
  target) were left unresolved, and the `a64_call` fallback died on a
  non-start target. The backward walk now follows branches into a block,
  and `cfg_const` searches predecessors (fall-through, direct branches,
  resolved tables) for the definitions that reach the load; `discover`
  retries failed tables when others add code. 1,216 -> 1,409 tables; of the
  1,224 table-shaped `br`s in main 8 are still unresolved
  (`python3 -I tools/jtcheck.py`). Without a bound (`cmp` on the index or a
  register it was copied from) tables are read until an entry leaves the
  text or hits another start, which both cut tables short and over-read
  them into junk code.
- Address space: Frostbite's `VirtualAlloc_Posix` reserves ~55 GB
  (0xdbc9e0000) of address space in one piece and aborts with os 3-12
  otherwise, so the alias region is now 64 GB as in a 39-bit space
  (`MAP_END` 0x11_0000_0000, 68 GB reserved). svcMapPhysicalMemory used to
  `memset` what it mapped, which committed everything (49 GB footprint and
  the process got killed); it now replaces the range with fresh anonymous
  pages (`zero_pages`), and Unmap does the same to give pages back. Running
  footprint is ~1.6 GB. For wasm this makes the address-space problem
  bigger: the VAM calls will have to be compacted (reserve lazily), memory64
  alone won't cover 64 GB.
- `aoc:u` (`recomp/rt/aoc.c`): no add-on content; fsp-srv 202
  (OpenDataStorageByDataId) returns TargetNotFound instead of a missing object.
- `audout:u` (`recomp/rt/audout.c`): a null sink that releases each
  appended buffer after its duration at 48 kHz on a host thread and signals
  the buffer event; samples are dropped (a CoreAudio/WebAudio backend goes
  in `sink_thread`).
- `svcCreateTransferMemory` makes an `H_TMEM` handle (`KTransferMemory`:
  address, size, permission) for services to read; the memory stays
  accessible to the guest. nv:: creates one for `nvdrv`.
- GetInfo 20 (user exception context) must be a real page: nn::os keeps the
  dying message region at +0x1c0 of it.
- `IApplicationFunctions` 210 returns an event that nn::oe::GetOperationMode
  caches and TryWaits on every call; with the stub's handle 0 TryWait spun
  forever (it retries anything but TimedOut). A stubbed command that should
  return a handle can hang as well as crash.
- `FIFA_TRACE=1` also prints the handles passed to WaitSynchronization.
  For a hang, `lldb -p <pid> --batch -o 'thread backtrace all -c 30'`: the
  `F_<addr>` frames are guest functions.
- `nifm:u/a/s` (`recomp/rt/nifm.c`): `IGeneralService` always offline;
  requests stay NotSubmitted and `GetResult` is 2110-1111 (communication
  disabled).
- Shaders: the RenderCore2 NRO embeds a single compute shader (GLSLC `VNVN`
  container); the game's shaders are inside the CAS archives, whose
  Frostbite 2025 container isn't the documented one. LAS Recomp's Maxwell
  translator (github.com/peiikai/LASRecomp, MIT, `runtime/nvn/maxwell`)
  builds standalone but fails on that shader at `SUST` (image store). The
  NVN HLE path was taken; the game's shaders are captured as it creates
  them (`FIFA_DUMP_SHADERS`), see the renderer notes above.
- Jump tables with sign-extended offsets (`ldrsw`/`ldrsb`/`ldrsh`) were
  never resolved: `is_store` read only bit 22, which is 0 for those loads.
  Unresolved `br`s fall back to `a64_call`, which only works when the
  target happens to be a function start.

## Next steps

- Black frames after the title screen (see Status, "Findings"): the
  press-start screen's text renders as zero-size glyphs from an empty
  atlas. Find the quad writer, then why glyphs are empty (GPU SDF glyph
  generation, FreeType metrics) and why the `Fonts/EACondBlackItalic`
  asset never registers. Then SUST/SUATOM/STS/VOTE
  and the other unknown instructions (FS `3b34…`, VS `1887…`).
- Later: the WebGPU RenderCore2 backend (option 2 in Status).
- `nvdrv`: real responses for Open/Initialize/ioctls the driver init needs
  (only if the NVN HLE path doesn't bypass it).
- Measure translator coverage over a longer run (`FIFA_DUMP_SHADERS`,
  then translate the dumps offline).
- Then keep pushing: hid, vi, the rest of graphics, and an audio backend
  for `audout.c`'s sink thread.
- The 8 table-shaped `br`s in main the resolver still misses (a register
  reused along some path); fix when one is hit.
- Name the primitive `T_*` types; tie vtables to reflection classes.
- Dump the DWARF types for the `rc2*` interface and the opcode structs from
  the `.nrs` (needed for option 2).

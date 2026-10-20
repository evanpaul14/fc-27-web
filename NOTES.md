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

Current state: with the update (`FIFA_ROMFS=extracted/update/romfs_program.json`)
the game plays the EA SPORTS intro, the title ("Let's Play!"), the offline
notice, the favourite-team setup and reaches the main menu (FC News,
Ultimate Team, Kick Off, ...) at 30 fps on the Metal executor, driven by
keyboard, gamepad or `FIFA_PRESS` scripts (see "Input" below). The
worker-thread crashes were an exclusive-store ordering bug (see "Worker
thread crashes" below); 8 runs of 2-3 minutes since, none crashed. The rest of
this section is the history that led here, oldest first.

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
    (The screen after it was black until the device-info fix below.)
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
    skipped per 2 s.
  - Black after the title, first two passes (superseded by the third
    pass below; the conclusion that glyphs come out empty was wrong, the
    collapsed quads were only spaces). Facts from them that still hold:
    - present/acquire rotate buffers 0,1,2; `FIFA_SCREENSHOT_EVERY` with a
      multiple of 3 only ever sees buffer 0, so use e.g. 31. The final
      composite is `fs=16ebcaf00`; `FIFA_DBG_FS=16ebcaf00:ffffffff:0`
      turns every presented frame white.
    - The 1x1 textures many passes bind are the game's own defaults
      (tic 260/301/302 black, 299 white), not unresolved handles.
    - Glyph quads (`vs=16e001500 fs=16ea35100`): a0 = (x, y, 0, -1) with
      w = -1 meaning "alpha from the texture's red channel", a1 = uv,
      a2 = add colour, a3 = multiply colour; one 6-vertex draw per glyph.
    - Font lookup (update `main`: font server vtable 0x1489d5f0, slot 2
      `sub_dd38dd0`; base build `sub_5d20540`) walks the registered fonts,
      then `"%s/%s.ttf"` under `data/ui/fonts` through Frostbite's VFS, and
      falls back to the server's default font (`[this+8]`, EABold).
      EABold, EAMedium, EAExpandedHeavy and EAExpandedLight load;
      `EACondBlackItalic` exists nowhere in the VFS (an asset,
      `Fonts/EACondBlackItalic`) and resolves to EABold. Harmless so far.
    - VFS lookup (`sub_1b95f00` -> `sub_1b95ff0`, base build): mounted
      layers, each a sorted table of 0x1c-byte entries keyed by the u64
      FNV-1 hash of the lower-cased path, binary search.
    - lldb refuses watchpoints on pages wrapped by a no-copy MTLBuffer;
      the mprotect watch in `recomp/hle/debug.c` works instead.
  - With the update (2026-10-18; `build/` is now the update's `main` on
    the patched RomFS, `FIFA_ROMFS=extracted/update/romfs_program.json`):
    same picture then. Title at frame ~93, black from ~150 on, 30 fps and
    ~11,800 draws / ~120 skipped per 2 s through frame 7,100 (4 min). So
    the base cart's missing superbundles are not the cause, or not the
    only one. Function addresses in this section are the base build's;
    they differ in the update's `main`. Seen once in the first update run
    (~frame 124): guest null read (0x58) in `F_136b2cc0` on a worker
    thread; the second 4-minute run didn't hit it.
  - Black after the title, third pass (2026-10-18, update build). Most of
    the "Findings" above were wrong about the cause:
    - The screen after the title is the first-boot **Accessibility
      Settings** screen ("Select your preferences, before continuing to the
      game", Narration, Subtitles, Brightness, Continue, an on-screen
      keyboard). Text goes through EAText's own OutlineFont + FreeType
      (`EAText/FT_Stream`, guest 0xc5b0000-0xc5e0000; the FreeTypeImpl at
      0x1208xxxx is a second copy, EATextRNA, that never runs). Fonts load
      intact (TTFs dumped from guest memory parse; the table checksums are
      all stale in the shipped files), GetGlyphIds / GetGlyphMetrics give
      correct ids and metrics for real letters.
    - The collapsed quads that `dbg_vb_hit` catches are spaces and U+200B:
      only those go through CPU RenderGlyphBitmap (0x0 bitmaps, so -2,-2
      padding-only quads). Real letters use the GPU SDF path.
    - The UI target (1920x1080 fmt 0x25, the composite's tic 885) is the
      same host NvnTexture the UI draws into, yet forcing the panel shader
      white (`FIFA_DBG_FS=16f536a00:ffffffff:0`) still leaves it black: the
      panels have no coverage.
    - Cause, fixed: the panel VS `16eaa0900` has no vertex attributes. It
      pulls per-draw data by LDG through c0[17] (the SSBO pointer) and its
      vertices by `t0.read()` from a buffer texture (RGBA32F). Its handle
      was 0x100000000: RenderCore2 builds separate-texture handles itself
      as `1 << 32 | descriptor id` (`cacheShaderResources`), and the id was
      0 because the resource view was null. `AutoViewCacheHelper` rejects a
      buffer view whose element count exceeds the device's
      MAX_BUFFER_TEXTURE_SIZE, and `nvnDeviceGetInteger` answered 1:
      `device_integer` was LAS Recomp's table from an older SDK and
      drifted from index 50 on (MAX_TEXTURE_SIZE 1, MEMORY_POOL_PAGE_SIZE
      1, 0 for everything past 64). The table now follows SDK 22's
      `nvn::DeviceInfo` (enums.tsv) with Tegra X1 values. Buffer textures
      (target 10) also get a 1D Metal texture (shaders fetch them as
      texture1d; clamped to 16384 texels) uploaded from guest memory when
      their hash changes, at most once per frame
      (`upload_buffer_texture`). With both, the Accessibility Settings
      screen renders (frame ~560 on). Still wrong: the Continue button's
      contents are dark.
    - Debugging aids from this: `FIFA_LOG_SEPTEX=1` (vertex-stage separate
      texture binds with the NRO caller), `FIFA_LOG_HANDLES=1` (texture
      pool registrations of buffer textures and ids 0-3). The NRO's
      `.nrs` has full symbols: `nro.elfsyms.tsv` maps an `lr` 0x1a0xxxxx
      to `fb::` functions (address - 0x1a000000), and objdump of the
      `.nrs` itself gives demangled disassembly. Identical functions are
      folded (getAsSrv and getAsUav share 0x67430).
    - Debug switches added: `FIFA_LOG_GAME=1` (a printf-style logger the
      retail build compiles out, `HLE(sub_ffd0744)` in `recomp/hle/log.c`;
      `HLE(sub_<addr>)` now works for stripped functions), write/access
      watches in `recomp/hle/debug.c` (`dbg_watch`, `dbg_watch_access`;
      `FIFA_WATCH_VB=1` arms one on the collapsed glyph quad,
      `FIFA_WATCH_FOLLOW=<memcpy symbol>` follows a memcpy to its source,
      `FIFA_WATCH_MAX`), RT address + object in `[draw]` lines, `[vtex]`
      lines for vertex-stage textures in the debug frame.
    - The update build now crashes often on worker threads (guest
      null reads around frames 120-560, a different function each time:
      F_10232510, F_12460cc0, F_13698650, F_118b0720, F_12440280,
      F_1243f4b0, F_136b0720), in about half the 30-60 s runs, often
      before frame 300. Not caused by the logger HLE (crashes with it
      removed too).
    - RomFS reads (`FIFA_LOG_ROMFS=1`; image offsets + 0x3f9c000 are
      `romfs_program.tsv` offsets for the update): the `en/loc/en.toc`
      of installpackage_00/02/05 are read; no fs errors.
  - Seen once (no debug options, ~frame 200): Frostbite small-block
    allocator trap `udf #0x8002` at 0x18172fc, reached from `sub_181b310`
    when a freelist's next pointer lies outside its arena, i.e. heap
    corruption. Not investigated.
  - Seen once (with heavy debug logging, ~frame 144): guest null read
    (0x18) in `F_116768b0`; a 3-minute run without logging didn't hit it.

- Worker thread crashes (2026-10-18, fixed). The update build died in about
  half of all 30-60 s runs with guest null reads on worker threads (Job1
  etc.), a different function each time. Two of them were the same
  intrusive list (prev/next at +0x418/+0x420) walked into a null `next`:
  shared data seen half-updated. Not core assignment (every game thread
  asks for mask 0xf; `FIFA_LOG_THREADS=1` logs CreateThread prio/core and
  mask/priority changes), not ABA-prone LL/SC pops (none in main: no load
  ever sits between an exclusive load and its store), not the kernel's
  mutex/condvar/arbiter code. `FIFA_CORES=N` (at most N guest threads run
  guest code at once; the slot is given up in blocking svcs and `yield`,
  and taken anyway after 50 ms) separated it: N=3 still crashed, N=1 never
  did (6/6 runs of 150 s). Cause: `stxr` was a *relaxed* CAS. In
  `ldaxr; ...; stxr` (an acquire exchange: spinlocks and lock fast paths,
  13.8k sites in main) another thread can take and release the lock
  between the two; the CAS still sees the old value and succeeds where
  LL/SC would fail, and since only the CAS read that release, the critical
  section was not ordered after the other thread's. STX's CAS is now at
  least an acquire (cpu.h; full rebuild). `FIFA_CORES` stays as a tool.
- Crash reports now name the crashing thread (nn::os name: TLR+0x1f8 is the
  ThreadType, +0x1a0 its name pointer) and list all threads with priority
  and wait state.
- Input: `recomp/rt/input.m` feeds the npad HLE (the game polls
  `GetNpadStates(NpadFullKeyState, 16, No1)`). Keyboard in the window:
  arrows d-pad, WASD/IJKL sticks, Return/Space A, Esc/Delete B, X, Y, Q/E
  L/R, 1/3 ZL/ZR, =/- Plus/Minus, F/H stick clicks. Gamepads through the
  GameController framework, by position (east = A). Headless:
  `FIFA_PRESS=<frame>:<button>[:<frames held>],...`, e.g.
  `FIFA_PRESS=300:a,600:a,900:a,1200:a,1800:a,2400:a,3000:a,3600:a`
  (with `FIFA_SCREENSHOT_EVERY=300`) walks from the intro to the main menu.
  Keyboard and gamepad paths are untested so far (runs were hidden).
  `FIFA_CONTROL=<fifo>` drives a running game: `echo 'down 4; a; wait 60;
  shot' > fifo` (commands in `input.m`: `<button> [n]`, `hold`, `stick`,
  `wait`, `shot [path]`, default `build/live.png`, `debug`), so menus can be
  walked one screenshot at a time without rebooting; `tools/tomatch.py`
  drives it from boot to Kick Off's match.
- Kick Off (2026-10-19): main menu -> Kick Off -> welcome pages -> gameplay
  type picker -> Match Preview (Bournemouth v Brighton) -> Start Match ->
  difficulty notice -> match load. Fixed on the way:
  - `0xa0aaf80` (an allocator callback) was never found: it follows a
    `blr` to a noreturn out-of-memory handler, and address-taken starts were
    only accepted after ret/b/udf/nop. bl/blr count now (+98 starts; the
    ones that are labels inside a function only split it).
  - `br 0xcc3b8fc` had a 2-entry table instead of 14: the bound search
    scanned back by address and took `cmp w10, #1` (feeding a b.ne, above
    `mov w10, w14`) as the bound. It now walks `seq` (the path back from the
    table load), follows the index through mov / `and #2^k-1`, needs the
    cmp to reach a b.hs/b.lo/b.hi/b.ls (skipping cb/tb and non-flag-setting
    instructions), and accepts a cmp on an equal register (`and w8, w1,
    #0xff; cmp w8, #N` above `and x8, x1, #0xff`). 57 of 1,433 tables
    changed; most shrank (the old scan read past ~40 tables' ends), and the
    rest are now unbounded scans where no bound is on the path, which only
    adds labels.
  - Copies to null: RenderCore2 computes copy addresses as
    `BufferGetAddress + data - BufferMap`, with `data` an offset for
    buffers it knows aren't CPU-visible. `nvnBufferMap` / `nvnMemoryPoolMap`
    must return NULL for `CPU_NO_ACCESS` (flag 1) and virtual pools.
  - Virtual pools (flag 0x200, the texture streaming heaps) get a range in
    [VPOOL_BASE, VPOOL_END) = 0x11_0000_0000..0x14_0000_0000, above what the
    guest is told about; `nvnMemoryPoolMapVirtual` aliases the physical pool
    pages there with `mach_vm_remap(copy=FALSE)` (zero pages on unmap), so
    their buffer/texture addresses are ordinary guest addresses. Metal
    wraps memory at buffer creation and wouldn't see remapped pages, so
    virtual pools aren't wrapped: `renderer_pool_buffer` / `_window` go
    through `nvn_virtual_phys` to the physical pool's buffer.
  - BC targets: a CopyTextureToTexture from a uint texture (one texel per
    block) into a BC texture goes through a buffer (Metal only copies equal
    formats); compressed render targets are dropped (logged `[rt]`).
  - LEA / LEA.HI (reg, cbuf, imm) in the Maxwell translator: 211 vertex
    programs in the match used LEA.HI.
  - Crash reports print host frames (fp chain) before the guest ones.
- Match shaders (2026-10-19): skipped draws in the match went from ~7,000
  to ~230 per 2 s (the 3D scene is still black, see Next steps).
  - Offline check: `build/shtool [-c] [-o dir] build/shaders/*_code.bin`
    (build line in `recomp/nvn/maxwell/shtool.m`) translates every dump
    from `FIFA_DUMP_SHADERS` and counts failures by error/opcode; `-c` also
    compiles the MSL through Metal (no offline `metal` without Xcode). It
    scans 0x10000 bytes instead of up to the program's constants, so a
    few "unknown instructions" (`3b03`, `bf52`, ...) are constant data that
    the game never reaches. Compute programs are dumped too now (`_s5_`).
  - TLD (`dc38`, texel fetch, all `.LZ` 2D so far, handles in the separate
    texture range of c[2]) and TLD4.B (`defa`/`defe`/`defc`, bindless
    gather: handle first at Rb, offset type bits 36-37 with 6-bit AOFFI
    offsets, component bits 38-39, `.DC` bit 50 -> `gather_compare`).
  - Bindless handle tracing gave up on most of the "not from c[2]" cases
    because `writes_reg` took FSETP/ISETP (bits 0-5 are predicates: P0, PT
    reads as r7), DEPBAR, stores and LDG.32 (counted as 4 registers) for
    writes to the handle register. 44 -> 7 failures; the rest are an
    indexed `LDC c[2][r + 0x168]` texture array and handles written under a
    predicate.
  - Metal allows 16 samplers per stage; some fragment programs sample 20
    textures. Bindless textures with the same sampler word share one
    `[[sampler(n)]]` (`ShaderTexture.sampler`), the renderer binds by it.
  - `FIFA_CONTROL`'s `shot <path>` silently truncated paths over 63 chars.
  - Compute: shared memory (LDS/STS -> `threadgroup uint smem[]`, size
    from the control section at +0x754 (`maxwell_shared_size`), masked to
    a power of two), SULD/SUST (bindless; handles are MOVs of the NVN
    image bindings at c[2][0x120 + 8 * slot], now in `tex_cbuf_word`;
    Metal `access::read/write/read_write` textures, no sampler, bound
    without swizzle views), ATOMS / ATOM / RED (32-bit int ops), VOTE
    (`simd_ballot`/`simd_all`/`simd_any`). 14 -> 47 of 56 compute
    programs translate. Dual-source blending: `ShaderKey.dual_source`
    when target 0 blends with SRC1 factors; the FS's target 1 becomes
    `[[color(0), index(1)]]`.
  - **The 3D scene's geometry collapsed to a point**: `FIFA_DBG_VS` showed
    every vertex of a player draw at the same clip position. Shaders got
    one global-memory window (the pool of the stage's first storage
    buffer) and read 0 outside it, but they read several storage buffers
    in different pools (vertex data, instance transforms). Now
    `gmem_win[GMEM_WINDOWS]` (8) holds the pools of all bound storage and
    uniform buffers and `gfind()` in the MSL picks the window
    (`FIFA_GMEM_ONE=1` restores the single window for comparisons).
    Vertices are distinct and in the frustum since, but the main pass is
    still black, and GPU time went up 3x (the Kick Off notice: a light-list
    loop, FS `170461700`, now reads real records; in the match ~3 fps).
  - Debugging aids: `debug` control command (the next frame is the debug
    frame), `FIFA_GPU_PROFILE=1` (each pass / dispatch in its own command
    buffer, GPU time per pass size + first FS / compute program logged as
    `[prof]` every 2 s; absolute numbers are inflated by the extra command
    buffers, compare runs instead), float targets in the debug frame are
    also saved scaled to their maximum as `pass_NNN_n.png` with the
    maxima logged.
  - The main 3D pass is **NaN/Inf in every channel of every pixel**
    (`pass_007_n.png: ... 1354752 non-finite` at 896x504), so the
    geometry is drawn now and its shading produces non-finite values,
    which show as black. Later passes inherit it.
- Handheld by default (2026-10-19): am GetOperationMode/GetPerformanceMode
  report handheld/normal, default display resolution and the vi display are
  1280x720 (`host_docked()`, `FIFA_DOCKED=1` for the old docked setup). The
  title costs ~575 ms GPU per 2 s instead of ~1,000 ms and 251 MB of textures
  instead of 714 MB; the match is still ~3 fps (GPU-bound) and its 3D
  resolution scales down dynamically (896x504 seen).
- NaN hunt (2026-10-19), first round; the match is still black:
  - Variable-latency results: Maxwell texture fetches, loads, MUFU etc.
    write their destination when their write barrier (sched bits 5-7) is
    released, and only an instruction that waits on it (bits 11-16) sees the
    new value. The compiler relies on this: in the exposure histogram FS
    `16dda7600` a TLDS writes r4 and the next TLDS still uses the old r4 as
    a coordinate. The translator ran results at once. Now each such
    instruction's assigned registers (`rN = ...` statements in its output)
    are stashed in `lwB_K` and the old values restored; they are committed
    before the first instruction waiting on B, a branch target, flow
    control (+ SYNC, DEPBAR) or the end (`defer_late`/`commit_late`).
    `FIFA_LATE_OFF=1` restores the old behaviour. Same 398/437 shtool
    totals either way.
  - BFE.BREV translates (`reverse_bits` first): 2 more compute programs.
  - `nvnCommandBufferClearBuffer` was recorded but never executed; it now
    fills the range with the 32-bit value in submission order.
  - Where the NaNs come from: the exposure chain is 256x256 R16F
    luminance (FS `16fa54a00`) -> histograms (`16dda7600`,
    `16e3c8b00`, 256x1 RG16F) -> compute `16ed0fd00` -> LUT FS
    `16e3a1a00` (RGBA16F 256x1, `t0[x] / t0[N-1]`: an empty histogram
    gives 0/0). In the match nothing is drawn into the 256x256 target (it is
    cleared to 0), so the LUT is NaN and every lit draw samples it. Zeroing
    the LUT (`FIFA_NANZERO=16e3a1a00`) is not enough: the light-list FS
    `170461700` divides by a word of a 48-byte fragment storage buffer
    (c[0][0x510] -> 179d7fb00, the same address every run) that is all
    zero: 1/0 at pc 0x2778, then Inf*0. No CPU write ever reaches that
    buffer (`FIFA_WATCH`), no ClearBuffer/copy targets it, and none of the
    logged dispatches writes it: next, find its GPU writer (a dispatch that
    reaches it through a pointer, one of the compute programs that still
    fail: `ea70` x2, `3a2c` x2, surface type 1 / handle not an image
    binding), or why the game never fills it.
  - Debug aids: `FIFA_DBG_PERDRAW=1` (debug frame: each draw ends its pass
    and logs the non-finite pixels of every float colour target,
    `[perdraw]`), `FIFA_DBG_FS=<fs>:fffffffe:0` (NaN trace: the fragment
    outputs the index of the first float result that is NaN/Inf; logged as
    `[nantrace] ... pc <x>`), `FIFA_NANZERO=<fs>,...` (that program's
    non-finite outputs become 0), `FIFA_SAFE_MATH=1` (fast math off; made
    no difference), `FIFA_WATCH=<addr>:<len>` (CPU write watch armed at
    boot), `[skip]` (skipped draws with programs), `[dtexa]` (array/3D/
    cube textures), `[fssbo]` (fragment storage buffers up to 256 bytes),
    clear colours and targets in `[clear]`, `[clearbuf]`.
  - Menu input timing varies between runs ("down 4" from the main menu
    lands on Player Career or one entry off); check with `shot` before `a`.
- NaN hunt, second round (2026-10-19): **the match's main 3D pass is finite
  and shows the scene** (pitch, players, stands, ad boards in the HDR
  target; `pass_008_n.png` max ~5). The final image is still nearly black
  (exposure, below).
  - Where the 48-byte buffer comes from: it is not a fixed buffer but a
    transient allocation (179d7fb00 is reused by several programs). Chain:
    sky FS `16f50ba00` draws the six faces of a 128x128 RG11B10F cube ->
    `CMD_DOWNSAMPLE` mips -> compute `16dbfe800` / `16e472300` filter it
    into a second cube (image stores, one handle per mip) -> `16ee81e00` /
    `16e895800` prefilter -> `16da34400` projects 96 SH records ->
    `16f732700` reduces them into 3 x 16 bytes (the buffer the light-list
    FS divides by). Every step read zeros:
  - The sky FS never translated: its SUST handle is `@P3 MOV r10,
    c[2][0x120]` under the same predicate as the `@P3 SUST`, and
    `trace_handle` refused all predicated writes. A write under the using
    instruction's predicate counts now, when no SETP/R2P in between sets
    that predicate (`pred_written_between`). shtool: 398 -> 407/437.
  - The cube filters bind the cube as `texture2d_array` (faces as layers)
    and one shader as `texturecube_array`; `sampled_texture` had no
    Cube -> 2DArray / CubeArray views and bound the dummy texture
    (`[tex] type mismatch: want 3 have 5`), so reads were 0 and image
    stores went nowhere.
  - One-shot draws were lost to async compiles: the sky cube (drawn once at
    boot and once at match load) and a 128x128 RGBA16F lookup table (FS
    `16e7daa00`, drawn once at frame ~73; the object shaders take
    `1 / t14.x` of it at pc 0x3ac8 -> Inf) were skipped while their
    pipelines compiled, and never drawn again. `one_shot_target()`: a draw
    whose colour/depth target was not drawn during the previous frame waits
    for its program and pipeline (`compile N/Mms wait Kms` in the stats
    line; ~40 ms in a whole boot-to-match run). Targets drawn every frame
    still skip.
  - Debug aids: `FIFA_DBG_SBUF=<addr>` (compute dispatches binding a storage
    buffer holding addr: uniforms, the buffer before and after the GPU ran
    it, `[sbufw]`), `FIFA_DBG_CSW=<cs>,...` (the first 12 dispatches of
    those programs, same readback plus every bound texture's size, guest
    bytes and which mips/faces of the Metal texture hold anything),
    `FIFA_DBG_RTW=<tex addr>` (render-target binds, draws, copies, clears and
    shader bindings of that texture, `[rtw]`; the sky cube is at 1786a8c00
    and the LUT at 178240000 every run), and with `FIFA_DBG_PERDRAW` +
    `FIFA_DBG_FS=<fs>:fffffffe:0` the traced program's textures are listed
    too. The "sign in to the EA Servers" notice turns up at a different point
    in each run and swallows presses, so fixed-timing menu macros break:
    `python3 -I tools/tomatch.py <fifo>` (game started with
    `FIFA_CONTROL=<fifo>`) goes from boot to the match, classifying a
    screenshot after each step (menu = teal side panel, notice = Ok button on
    neutral grey; `--classify PNG...` to check) and stepping to Kick Off one
    press at a time. ~8 minutes boot to match. The main menu list scrolls, so
    the highlighted entry is told by its label's size (`MENU_LABELS`: width
    and white pixels at 720p), not its row; the highlight lags presses, so
    each step waits for two agreeing screenshots. Each step's screenshot is
    kept as `<shot>-NN.png`; `--from-menu` skips the boot wait.
- **The match renders** (2026-10-19): broadcast camera, lit pitch, players,
  stands, replays with the goal graphics, at the same ~3-4 fps. After the
  NaN fixes the image was still nearly black with only highlights:
  - Colour grading: compute `16dd60c00` turns a 33x33x33 RGBA32F LUT asset
    into a 33^3 RGB10A2 one, `16e714900` resamples it to 32^3, and the
    post FS `16de63d00` decodes its lookups (`exp2(...) * 2^-15`). The asset
    is swizzled by the game on the CPU (no packaged layout), and with our
    3D default (8 x 32 GOBs) a third of it read as zero. Deswizzling the
    guest data with every block height / depth (`FIFA_DBG_3D=1`, RGBA32F 3D
    textures: mean neighbour difference and zero texels per choice) left one
    clean answer, 1 GOB high and 16 deep. `tegra_layout_init`: 3D blocks
    hold at most 16 GOBs, depth first.
  - Exposure: compute `16e884800` (histogram) -> `16f5aa200` -> `16ed56900`
    writes {e / e_prev, e} to an 8-byte buffer (f0 of the post FS); e_prev
    is a uniform the CPU fills from the previous frame's result. One run had
    e_prev = 0 (word 0 inf); `FIFA_FENCE_FLUSH=1` (fences wait for the GPU)
    kept it at ~1.0. Not seen without it in the other runs, so left as is.
  - Debug aids: `FIFA_DBG_PROBE_FS=<fs>` (debug frame: that program's
    textures, `[probefs]`), `FIFA_DBG_RTW_SIZE=<w>x<h>x<d>` (the texture
    watch by size), `[rt3d]` (3D render targets), `[copy3d]` (buffer copies
    into 3D textures), RGB10A2 colour counts in the texture probe;
    `FIFA_DBG_CSW` also fires in the debug frame.
- Match performance, first round (2026-10-19): ~3-4 fps, ~1.7 s GPU per 2 s.
  - `FIFA_GPU_PROFILE=2` (each draw its own pass, tagged `draw WxH vs fs`)
    and the `prof <n>` control command (profile level while running) show
    one program: compute `16dff9100` is ~88% of all GPU time, ~98 ms per
    dispatch, ~2 per frame. It links mesh edges into contours (64 threads
    read triangle indices from a buffer texture, classify edges, write
    links to shared memory), then thread 0 alone walks each contour
    (`next = smem[i] & 0x3ff` until back at the start) and emits it.
  - That walk runs to the `switch (pc)` loop's 100000-iteration guard
    (~1 us per serial iteration): the index buffer texture has 65000
    texels and was clamped to 16384 (Apple 1D limit), so the links read
    past it are junk and a contour never closes. Fix (not yet verified in
    a run): 1D and buffer textures are 2D textures with rows of 16384
    texels; MSL declares them `texture2d` and wraps them in `rows1d<T>`
    (same `.read(x)` / `.sample(s, u)` / `.get_width()` syntax). The
    guard is `FIFA_SHADER_GUARD` (to confirm: the cost should scale with it).
    Buffer uploads copy whole rows (staging padded). Limits: only the MSL
    path has `rows1d` (the GLSL dialect still declares 1D samplers); a real
    1D texture wider than 16384 reads fine but samples only its first row;
    1D images (SULD/SUST) use the same wrapper (`write(v, x)`).
  - LOP3 `.P` (register form: predicate op bits 36-37, predicate 48-50)
    wrote no predicate; in `16dff9100` the contour emit was dead code
    behind a stale P0. 5 programs use it.
  - Changing the translator changes every program's MSL, so Metal's disk
    cache misses, and a few pipelines then take 10+ minutes to build
    (`MTLCompilerService` at 100%) while the render thread waits for a
    one-shot target: Player Career's screen, and the match's VS `1e3ec9400`
    / FS `16fb79200` (FS is 2784 lines, 44 cases; the library compiles in
    0.3 s, the pipeline back-end is the slow part). The game looks frozen.
    `[metal] waiting > 2 s for ...` now names the program. On an older build,
    `lldb -p $(pgrep -x fifa) --batch -o "p/x st.program->stages[0].code_addr"
    -o "p/x st.program->stages[1].code_addr" -o detach` reads it from the
    frozen process.
- setjmp: the guest-jmp_buf -> host-context table never dropped entries,
  and EAText's FreeType setjmps in every call, so it overflowed (64) on the
  screen after the offline notice. Entries whose host context lies below
  the current host sp (caller returned) are dropped now.
- `mrs ctr_el0` (A57 value 0x8444c004) and DC/IC ops (`dc zva` zeroes 64
  bytes, the rest are no-ops) translate now; `nn::os::FlushDataCache` from
  the hardware Opus decoder init hit them on picking a favourite team.

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

- Kick Off now loads the match: the Premier League loading cards ("Player
  To Watch" with streamed portraits) and the line-up graphics render, then a
  black screen with the PL logo, and then the match itself runs: HUD
  (BOU 0-0 BHA, clock running, radar with moving players, player names,
  "Difficulty changed to Beginner") and the 3D scene renders (see "The
  match renders" in Status). Next: performance (see "Match performance" in
  Status): run with the 1D-rows fix and confirm `16dff9100` drops from ~98
  ms (needs getting past the 10+ minute pipeline builds after translator
  changes: find what makes `16fb79200`'s pipeline so slow, likely the
  `switch (pc)` form; structured control flow would help both), then
  profile again with `FIFA_GPU_PROFILE=2`; the occasional e_prev = 0 exposure readback, and a visual pass over the
  match (shadows, reflections, the remaining ~10 skipped draws per frame:
  `16fd3ac00`, `171855700` TXD.B). Shader coverage (`build/shtool` over
  the dumps): 407/437 translate; the rest are unknown 3b03 (7 FS), bindless
  handles not from c[2] (4 FS, 1 CS: indexed `LDC c[2][r + ...]`), TXD.B
  `de7a` (2), surface type 1 (2 CS), 3a2c (2 CS), and a few words that are
  probably constant data past the code (bf52, bf82, 4016, ...).
- Try the keyboard and a gamepad in the visible window;
  audio output for `audout.c`'s sink thread; the dark Continue button on
  the accessibility screen (seen before the threading fix; check if it
  still happens). Then SUST/SUATOM/STS/VOTE and the other unknown shader
  instructions (FS `3b34…`, VS `1887…`).
- Later: the WebGPU RenderCore2 backend (option 2 in Status).
- `nvdrv`: real responses for Open/Initialize/ioctls the driver init needs
  (only if the NVN HLE path doesn't bypass it).
- Measure translator coverage over a longer run (`FIFA_DUMP_SHADERS`,
  then translate the dumps offline).
- The 8 table-shaped `br`s in main the resolver still misses (a register
  reused along some path); fix when one is hit.
- Name the primitive `T_*` types; tie vtables to reflection classes.
- Dump the DWARF types for the `rc2*` interface and the opcode structs from
  the `.nrs` (needed for option 2).

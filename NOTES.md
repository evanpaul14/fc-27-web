# fifa-web

Goal: port EA Sports FC 27 (Switch, user-owned copy) to the web by static
recompilation of the ARM64 executable, in the same style as driver-web.
Game data is read from local files at runtime and is never committed.

## Setup

- `tools/build_opus.sh` builds libopus (needed by `make -C recomp`).
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
FIFA_ROMFS=extracted/update/romfs_program.json ./build/fifa   # FIFA_TRACE=1 logs svcs
```

The build is of the update (`image.py extracted/update/code build`, see
Setup), so runs need the update's RomFS: without `FIFA_ROMFS` the base
RomFS's RenderCore2 NRO doesn't match and boot dies with `ro: LoadModule of
an NRO that isn't in the build`.

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
  - A full rebuild is ~43 min (50.5M instructions, 768 units; ~61 min since
    the switch to -O2, see "Guest registers across guest stores"). Only C
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
- Guest X registers and sp live in C locals `X0`..`X31` (`recomp/regs.py`); V registers stay in `CPU`. Each
  function has a summary: R (registers it may read before writing them) and W (registers that may differ at its
  return; a callee-saved one restored from its save slot on every path, or sp back at entry, doesn't count). The
  `CPU` struct is synced only at calls, svcs and returns: before a call the changed locals the callee reads, or may
  change and are needed after it; after it, reloads where live; at a return every changed local. Indirect calls,
  host replacements and unbuilt code are taken as AAPCS64 (read `ABI_IN`, change `CLOB`). Summaries are computed
  callee-first over the call graph's SCCs (~70 s, cached in `gen/summaries.pkl`); a unit's key includes the
  summaries of what it calls. Chunked functions and setjmp callers keep registers in `CPU`. Switches:
  `build.py --no-locals` (all in `CPU`), `--check-locals` (store everything changed before calls, report kept
  registers that differ after: `locals check` lines), `FIFA_NO_LOCALS=lo-hi,...` (hex; those functions in `CPU`,
  for bisecting). `python3 -I tools/verify_locals.py` checks every function's stores/reloads statically (~4 min,
  must report 0 problems after a change to regs.py).

## Status (2026-10-21)

Current state: with the update (`FIFA_ROMFS=extracted/update/romfs_program.json`)
the game plays the EA SPORTS intro, the title ("Let's Play!"), the offline
notice, the favourite-team setup and reaches the main menu (FC News,
Ultimate Team, Kick Off, ...) at 30 fps on the Metal executor, driven by
keyboard, gamepad, `FIFA_PRESS` scripts or `FIFA_CONTROL` (see "Input"
below). Kick Off plays a full match (Bournemouth v Brighton: 3D scene, HUD,
half-time) at 28-30 fps in open play (the game's 30 fps cap) with buffer
copies and readbacks as GPU blits (see "Blits" and "Match profile"). Per frame
in open play (hidden, no thermal pressure) the CPU takes ~28-30 ms and the GPU
is busy ~11.5 ms of the 33 ms ("GPU busy vs idle", "Host thunks in
`a64_call`"). The visual pass over the match is under way (skin, faces, jackets
crowd, grass grain and reflection cubes fixed, night and day shadows checked). The
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
  `build/shaders/<addr>_s<stage>_*.bin`), `FIFA_DBG_FS=addr:pc:reg[,pc:reg[,pc:reg]]`
  (register probe, up to three registers to R/G/B; pc `ffffffff` = solid white; see "Visual pass, second round"),
  `FIFA_DBG_VS=<vs>` with `FIFA_DBG_VSREG`/`FIFA_DBG_VSTEX` (vertex dumps and probes), `FIFA_WATCH=<addr>:<len>` /
  `FIFA_WATCH_TEX=<w>x<h>x<d>:<fmt>` (CPU write watchpoint), `FIFA_WATCH_GPU`/`FIFA_WATCH_FMT` (copies into a range or
  format; all in "Visual pass, fourth round"), `FIFA_DUMP_VB=1` (first
  6 raw vertices of stream 0 per debug-frame draw; also calls
  `dbg_vb_hook(host, guest)` for lldb), `FIFA_DUMP_VSUBO=1`,
  `FIFA_LOG_POOLS=1`, `FIFA_DBG_PROBE_FS=<fs>[,...]` with `FIFA_DBG_PROBE_PNG=1` (cube/array faces as PNGs, "Visual
  pass, seventh round"); the debug frame's `[dtex]` lines give each sampled
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
  `GetNpadStates(NpadFullKeyState, 16, No1)`). Keyboard in the window
  (2026-10-21): FIFA PC's classic keyboard layout, with Switch buttons by
  position: arrows left stick, S B (short pass), D A (shoot), W X (through
  ball), A Y (lob), Shift ZR (sprint, via flagsChanged), Q/E L/R, Z ZL,
  numpad 8456/2 or IJKL right stick, TFGH d-pad, C/V stick clicks,
  Return/Space A, Esc/Backspace B, P Plus, Tab Minus (Ctrl is avoided:
  Ctrl+arrows switches Spaces). The mouse is the touch screen
  (`GetTouchScreenState<16>` HLE in `hle/npad.c`, letterbox-corrected to
  1280x720); right button B. Checked by hand in a visible run: menus,
  mouse and a match all respond. Gamepads through the
  GameController framework, by position (east = A). Headless:
  `FIFA_PRESS=<frame>:<button>[:<frames held>],...`, e.g.
  `FIFA_PRESS=300:a,600:a,900:a,1200:a,1800:a,2400:a,3000:a,3600:a`
  (with `FIFA_SCREENSHOT_EVERY=300`) walks from the intro to the main menu.
  Synthetic keystrokes (System Events) are too short to be sampled; use
  `FIFA_CONTROL` for scripted input.
  `FIFA_CONTROL=<fifo>` drives a running game: `echo 'down 4; a; wait 60;
  shot' > fifo` (commands in `input.m`: `<button> [n]`, `hold`, `stick`,
  `wait`, `shot [path]`, default `build/live.png`, `debug`, `prof <n>`,
  `csdump [n]`, `blits <0|1>`), so menus can be
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
  - Why those pipelines were slow: not the FS (`16fb79200` alone builds a
    pipeline in 0.2 s) but BRX. GLSLC compiles a `switch` to `LDC
    c[1][index + table]` + BRX (target = the loaded offset), and the
    translator made every instruction a `case` for a BRX: 79 of the cached
    VS (skinning-style switches near the top) had 800-860 cases, 2922 for
    the biggest FS. Such a VS took 1.2 s to compile and 6.4 s for the
    pipeline with `16fb79200` (macOS, one at a time; the game builds many at
    once); with labels only where branches go, 0.2 s + 0.3 s. Every
    instruction being a block start also costs at runtime (each `case`
    commits all late writes). The translator now reads the table from the
    program's compiler constants (`maxwell_consts`, c[1] at `const_offset`,
    set per stage before translating; shtool reads 0x6FC/0x700 from the
    control dump) and labels its entries only (`brx_targets`; falls back to
    every instruction if the LDC isn't in the BRX's block). All 68 BRX
    programs among the dumps find their tables (e.g. 856 -> 14 cases) and
    compile; the other 339 translate byte-identically. Confirmed in a run
    (below): no pipeline waits over 2 s from boot to the match.
- Match performance, second round (2026-10-19, run with the BRX and rows1d
  fixes, `FIFA_ROMFS=extracted/update/romfs_program.json`; without it the
  base RomFS's RenderCore2 NRO fails `ro: LoadModule ... isn't in the
  build`):
  - BRX fix confirmed: boot to second half compiled 754 programs (47 s of
    compiles, 12.4 s waited on, at most 6 s in one 2 s window during match
    load) and logged no `waiting > 2 s`; the old 10+ minute freezes are gone.
  - Still ~3-4 fps, GPU-bound (gpu ~1.9 s per 2 s). `prof 1` in open play
    (51:06, broadcast camera): `cs 16dff9100` 1,695 ms of ~1,780 ms (95%),
    ~120-140 ms per dispatch, 2 per frame; everything else is ~85 ms per
    2 s (the biggest a 896x504 pass, FS `16fea2400`, ~4 ms/frame). In a
    half-time close-up it was ~170 ms per dispatch; once (with `prof 2`)
    ~28 ms. So the rows1d fix did not end the long walk: ~100k guard
    iterations at ~1 us is the measured cost, so it likely still runs to
    the guard in most frames. Without it the match would be ~15-20 fps
    on GPU time alone.
  - The walk (MSL from `build/shtool -o`): blocks 2584/2760 are thread 0
    following `smem[512 + (link & 0x3ff)]` until `link == start`, bounded
    by `cb4[0][1]`. The program's control section says 0x1000 bytes of
    shared memory (`smem[1024]`), but links sit at byte 2048 + 4 * index
    with a 10-bit index, so if `cb4[0][1]` > 512 the `& 1023u` wrap reads
    the low half and a contour never closes. Next: one run with
    `FIFA_SHADER_GUARD=10000` (cost should drop ~10x if the guard is
    what ends it) and `FIFA_DBG_CSW=16dff9100` (uniforms: `cb4[0][1]`,
    the edge count; buffer before/after).
  - Other costs, in order of what they'd be worth once that is fixed:
    - Every flush drains the GPU: `CMD_COPY_BUFFER_TO_BUFFER` and
      `CMD_COPY_TEXTURE_TO_BUFFER` call `renderer_flush` (~2 per frame,
      `flush 1.9 s/12-14` in the stats), so CPU and GPU never overlap.
      A buffer-to-buffer copy can be a blit in order; the readback only
      needs a wait when the CPU reads it (on its fence, like nvnSyncWait).
    - `gfind()` linearly scans 8 windows on every global load/store;
      programs that touch one storage buffer could bind the window as a
      constant, or the scan could start at the last hit.
    - `switch (pc)` dispatch: each loop iteration goes back through the
      switch and commits late writes at every case; structured loops for
      simple back-edges (2584/2760 here, 2952) would let Metal keep values
      in registers.
  - The 3D scene was black for a stretch of the first half (HUD fine,
    ~37:00 to half time) and came back at the half-time cutscene; probably
    the e_prev = 0 exposure readback (see "The match renders"), not
    checked. Also 4x `[tex] type mismatch: want 2 have 7 (32x32x32 ...)`:
    a program samples a 3D LUT as 2D and gets the dummy texture (not the
    post FS `16de63d00`, which declares `texture3d`).
- Match performance, third round (2026-10-19): **`16dff9100` fixed, the
  match runs at ~12 fps** (gpu ~330 ms per 2 s; was 3-4 fps, gpu ~1.9 s).
  The contour walk was not the problem:
  - `FIFA_SHADER_GUARD=10000` cut it from ~120 to ~15 ms per dispatch, so
    something ran to the guard. In the match it runs on 12-triangle meshes
    (`cb4[0]` = {1, 12, 12}: max slot, count, first triangle);
    `tools/contour_replay.py` replays the link build and walk from a dump
    and every walk ends in 3-5 steps.
  - Barriers: Metal's `threadgroup_barrier` only holds when all threads reach
    it in uniform control flow. In the `switch (pc)` loop, lanes that reach a
    BAR after fewer iterations than the others pass straight through (a
    standalone test: 63 of 64 lanes read shared memory before lane 0 wrote
    it, including lanes in the other SIMD group). Here lanes 1-63 read the
    emit count `smem[1]` before thread 0's walk stored it, and the emit loop
    `for (i = tid; i < smem[1]; i += 64)` ran to the guard on stale shared
    memory. Translator: in MSL compute with branches, a BAR now leaves the
    loop with pc past it (`bar_now`), and the threads meet at one barrier
    outside it, rounds until all have exited (a threadgroup counter;
    exited threads keep taking part, as on Maxwell). 11 of the dumped
    programs use it.
  - `S2R` SR_LANEID was 0 and SR_EQMASK/LTMASK/LEMASK/GTMASK/GEMASK (0x38-
    0x3c) fell through to 0: warp-compacted appends (ballot, leader =
    highest lane does the atomic add, `popcount(ltmask & ballot)` as the
    slot) never ran the atomic and gave every lane the same slot. Now
    `lane` and the masks (vertex shaders: lane 0 of 1). 7 programs, among
    them the exposure chain `16e884800` / `16f5aa200`.
  - Debug aids: `FIFA_DBG_CSDUMP=<cs>:<skip>[:<count>]` writes the bound
    uniform buffers, storage buffers and textures of a dispatch (GPU drained
    first) and the storage buffers after it to `build/csdump/<n>/`
    (`meta.txt`; `u<i>` is c[i + 3]); the control command `csdump [n]`
    dumps the next n dispatches of that program. `FIFA_DBG_CSW` lines also
    print the bound uniform buffers.
- Match check after the barrier fix (2026-10-19, run `build/r4`, update
  RomFS, screenshots every 45 s through the first half): **the 3D scene no
  longer goes black**; normal brightness at 39:34 and in stoppage time
  (46:04 +2'), where it was black from ~37:00 before. So it was the exposure
  chain's lane masks, not the e_prev readback. The `want 2 have 7`
  (32x32x32 as 2D) mismatch still shows (2x).
  - Without profiling: 15-28 fps in open play and cutscenes; the fps line
    shows ~0.8-1.0 s of every 2 s blocked in `renderer_flush` over ~100
    waits (`waits flush`), fences 0. With `prof 1` open play drops to 7-9
    fps (a command buffer per pass), so compare fps with profiling off.
  - `prof 1` top entries: depth-only passes (`pass 0x0 fs 0`, ~5.5 ms each,
    ~3 per frame), 1280x720 FS `16ff87600` (~17 ms each), `170a27300`,
    `16ffd7600`; `16dff9100` is gone from the list.
- Blits (2026-10-19, run `build/r5`): buffer copies, clears and pool
  readbacks as in-order GPU blits (`blit_buffer`, pending ranges checked by
  `renderer_cpu_access` before the renderer's own CPU reads/writes of guest
  memory). On by default since the profile below (17 -> 30 fps); the
  control command `blits 0` restores the drain + CPU copy. With blits off
  the match runs at ~17 fps in open play (gpu ~1.1 s and `waits flush` ~0.76 s/105 per 2 s;
  stats now print `blits <n>/<drains> waited`). With blits off each run
  showed ~4,300 skipped draws per 2 s, all reason 1 (no program: failed
  translation or not compiled); with blits on in the same match ~120, not
  explained yet.
  - The `[tex] type mismatch: want 2 have 7 (32x32x32 ...)` line now names
    the program and slot: FS `16ebcaf00` t2 (separate texture 1, c[2]
    0x170). The FS computes `t2.rgb * cb3[4].y + lut * (1 - t2.a *
    cb3[4].z)` with the 3D LUT at t1; t2 is a 2D overlay layer, and the
    game leaves the LUT bound in its slot when the overlay is off (weights
    0), so the dummy texture changes nothing. The only TXQ-only slots
    (typed 2D by guess) are CS `16e11bb00` / `16ee5ac00` t0, not this.
- Match profile (2026-10-19, run `build/r6`, `/usr/bin/sample` 10 s plus
  `prof 1`; the Python `sample` on PATH shadows Apple's):
  - CPU, blits off: three guest threads ~54% busy each, everything else
    idle. The thread running command lists spends ~29% of its samples in
    `renderer_flush` from `exec_list`, mostly `CMD_COPY_TEXTURE_TO_BUFFER`
    readbacks (~1,500 of 6,311) and `CMD_COPY_BUFFER_TO_BUFFER` (~330).
  - `blits 1` live, same match: ~17-18 fps in open play goes to 28-30 (the
    game's 30 fps cap); `waits flush` drops from ~0.76 s to 0 per 2 s and
    the game's own fence waits rise to ~0.5 s. Screenshots with it on look
    right (open play, a set-piece camera).
  - With blits on, the render thread is ~70% busy; its largest host costs
    are `renderer_texture` (~12%: `upload_texture`'s per-texture change
    checks, `fnv_sample`'s 4,096 strided reads and `hash_full` over
    ~3,100 live textures, inlined) and `memmove` (~8%).
  - GPU (`prof 1`, ~57 frames per 2 s): main pass `16f973400` ~9 ms per
    frame, depth-only passes (1280x720 x3 and 0x0) ~12 ms, `16e6d5200`
    ~3 ms; the rest is small.
  - shtool now stops at the compiler constants (control section 0x6FC/
    0x700) like the renderer: 426/437 translate; the 3b03/bf52/4016/...
    "unknown instructions" were constant data. Real failures: bindless
    handles not from c[2] (4 FS, 1 CS), 3a2c (2 CS), surface type 1
    (2 CS), TXD.B de7a (2 FS).
  - Shader coverage, next round: 431/437. TXD.B (`de7a`: handle at gpr8,
    then the coordinates; gpr20 the derivatives dx.x, dy.x, dx.y, dy.y ->
    `gradient2d`; 2D only, .AOFFI/.LC fail) for `171855700` `1718fdb00`.
    `writes_reg` took a one-component TLDS/TEXS as writing d+1 too, so
    handle traces stopped on it (`16fcd1300`, `16fd3ac00`); it counts from
    the component mask now (`texs_mask`, shared with `emit_texs`). The
    trace walked straight back across an unconditional BRK into
    `171838a00`'s loop; across a terminator it now continues from the one
    BRA that enters the block (several: straight back as before). Every
    shader that translated before gives byte-identical MSL.
    `FIFA_SKIP_PROGS=<fs>,...` fails chosen fragment programs for A/B runs.
    In open play the 5 programs took skipped draws from ~65 to 0 per 2 s.
    Seen in both A/B runs, so not from these: noisy pink/multicolour
    arms, legs and hands (skin) in the walkout/handshake and close-ups,
    a black sky, and 12-17 fps in open play with the window shown
    (28-30 above was with blits; not re-measured hidden).
- fps vs blits, benchmarked (2026-10-20, runs `build/b1`..`b4`, hidden, `tools/bench.py`: boots, tomatch,
  waits 120 s, then timed `blits` segments; logs fps, P/E-core time and clocks every 2 s). The "blits on later
  is lower than toggling it on" result was **thermal throttling of the P cores**, not blits:
  - Open play, same run (b1): blits 0 ~26 fps, blits 1 ~29 (GPU fence waits replace `waits flush`). A real
    but small gain (~+12%), not 17 -> 30.
  - The slow runs (b2-b4, back to back after b1) start at 29-30 and slide to 12-14 fps over 1-2 minutes. The
    P-core clock (`proc_pid_rusage` v6 `ri_pcycles / ri_user_ptime`) falls from ~2.95 GHz to a flat 0.83 GHz
    with IPC unchanged (~2.0-2.1), and the scheduler moves part of the work to E cores; fps tracks the clock.
    It recovers over ~5 minutes to 2.7 GHz / 30 fps whatever `blits` is (b4: `blits 0` at 0.83-1.1 GHz gave
    16 fps). `notifyutil -g com.apple.system.thermalpressurelevel` read 2 (heavy) with the game closed.
  - So the earlier 17 -> 30 "toggle" and the 12-17 fps "visible window" numbers were probably taken at
    different clocks. Benchmark only at thermal pressure 0, and check `cores.log` P GHz before comparing.
  - Per frame in the slow state every function costs ~2.5x (guest code as much as `renderer_texture`): a
    clock, not work, difference. Same draws per frame, same blits per frame (~7).
- CPU cost per frame, first round (2026-10-20, runs `build/b5` baseline, `b7`; compare runs with
  `python3 -I tools/benchsum.py build/b5 build/b7`: giga-instructions, CPU ms and GPU ms per frame, which hold up
  under throttling far better than fps; the game sits at its 30 fps cap anyway). `sample` in open play (b5): ~1.9
  cores busy, of the busy samples ~19% `a64_call`, ~8% `renderer_texture`, ~5% `memmove`. Fixed:
  - `a64_call`: every indirect call (vtables, function pointers) binary-searched the 340k-entry function table
    (5 MB, ~19 dependent misses). A 64k-slot direct-mapped cache in front (`fn_cache`, one atomic word per slot:
    address << 32 | index + 1, shared by all threads without locks).
  - Texture change checks hashed virtual-pool textures through `nvn_texture_data`, which gathers (mallocs and
    copies) the whole texture when it spans separate physical ranges, even for the 4,096-byte sample.
    `nvn_texture_spans` hands out the mapped runs in place; the hashes walk them (same values as before) and the
    gather happens only to upload.
  - The 64 MB uniform ring was only reset by `renderer_flush`, which blits made rare, so each wrap allocated a fresh
    shared buffer (zero-fill faults on first touch). Full rings now wait in `ring_spare` until their submission
    completes (`g_done_seq`) and are reused: `rings <wraps>/<new>` in the stats line shows ~1-9 wraps and 0 new
    per 2 s (so ~10 MB of uniform snapshots per frame go through `ring_alloc`).
  - `bind_cbufs` rebuilt and copied the driver-constant table (up to 0x800 bytes) and the compiler constants for
    each stage of every draw; the last copy is reused while it's the same (`drv_last`, `const_last`, `ring_gen`).
  - Result at the cap, same thermal state (pressure 0-1): 0.363-0.367 -> 0.314 Gi/frame (-14%), 65 -> 52.5 CPU
    ms/frame (-19%); fence waits 8.5 -> 0.9 ms/frame.
- The 30 fps cap (2026-10-20, `FIFA_LOG_WAITS=1` logs timed waits by guest call chain every 5 s): the frame pacer
  `F_0ad4a030` (its own thread) picks 33.3 ms or 16.7 ms by a mode flag (`fcsel d0, d10, d9, ne` at `ad4b4d8`) and
  sleeps the rest of the period each frame (in open play ~18 ms on 60-100 of 150 frames). `recomp/build.py` now has
  `PATCHES` (address -> expected word, C); that `fcsel` goes through `host_frame_period_ms`, set by `FIFA_FPS=<n>` or
  the control command `fps <n>` (-1 the game's, default; 0 none). Findings (b7): `fps 60` still gives exactly 30
  fps, so something else holds 30 too (probably the simulation rate: the image has `SimRate_Locked`,
  `SimulationTime.ForceSimRate 30.0f`, `TargetFpsIs60`); `fps 0` stops the pacer sleeping and costs ~2x CPU and GPU
  per frame (0.55 Gi, 131 ms CPU) for ~20 fps, and heats the machine into throttling. So the cap stays the game's.
  Other timed waits are polling threads with fixed sleeps (2, 2.7, 4, 5, 16, 30, 50 ms), ~3k wakeups/s.
  Second lock (b8, b9): with `fps 60` the pacer does tick at 60 Hz (and polls 1 ms waits at `ad4b4bc` for the frame
  to finish), the main loop thread (`1202ec18`, which slept ~7.7 ms per frame at the cap) no longer sleeps, and frames
  still land on 30; `fps 40` gives 29.7. The critical path is the three job workers: command-list submission
  (`nvnQueueSubmitCommands` -> `exec_list`) on them, plus ~1,900 of ~19k samples in `nvnSyncWait` on the GPU.
  Second lock found (2026-10-21, u1: `fps -1` 60 s then `fps 60` 120 s, `FIFA_LOG_WAITS=1`): at today's cost (CPU
  ~22-24 ms, GPU ~22 ms per frame, P cores ~15% busy at 1.6 GHz) `fps 60` still gives exactly 30.0, so it's a timed
  lock, not throughput. The main loop thread (0x6c, `1202ec18`) does sleep at `fps 60`, once per frame, ~24 ms (21 ms
  at `fps -1`), from `119e4084`: `F_119e3e70` (one caller, `119e3310`) is a second limiter of the pacer's shape, period
  `1.0 / [x0+8]` (a rate in Hz, 30.0) and the rest of it slept at `119e4080`. Its `ldr d0, [x0, #8]` (`119e3f0c`)
  is now a patch through `host_rate_hz` (same `FIFA_FPS` / `fps` setting; 0 means no limit).
  The pacer's own 30/60 choice is a game flag: byte `+0xda` of the object at `[0x155b46d8]` (ELF `0xd5b46d8`;
  main's ELF is based at 0, the guest at `0x8000000`), 1 = 33.3 ms. FIFA sets it to 1 at `0xfc6a9e0` and to 0 at
  `0xfc73a94`, each unless a query (message 0x1c9) says otherwise; `0xae9cdd0` / `0xae9ce00` set 0 / 1 (likely
  console commands). Its other readers (`0xaff98a0`, `0xaffa260`) only feed a 10-entry ring (telemetry). So the
  game has a 60 fps mode of its own.
  With the patch (u2) `fps 60` gives 59.3 fps, the main loop sleeping once per 60 Hz frame (~9 ms) - but u1 and u2
  never reached the match: hidden runs (u1, u2, u4, u5) stick on Classic Match's Select Sides screen, `a` presses
  arriving and ignored. u3 (`FIFA_AUDIO=1` while hidden: CoreAudio instead of the null sink) got through, and b20
  (17:27, before hwopus decoded for real) did with the null sink, so the trigger looks like real Opus audio through
  the null sink. Not wait logging (u5 had none). The null sink now paces buffers on absolute deadlines (relative
  sleeps drifted slow), which didn't fix it (u4, u5). So 60 fps in the match is still unmeasured.
- CPU cost per frame, second round (b9 counters, b10): per 2 s ~460-2,400 sampled texture checks, 8-317 full
  hashes (1-55 MB), ~0-1 uploads; 240-760 MB through `ring_alloc`. `fnv_sample` now hashes 512 whole cache lines
  instead of 4,096 bytes on 4,096 lines, and uniform snapshots reuse the last copy of the same range in the ring
  while the memory still matches it (`ring_snapshot`: ~270 of ~560 MB per 2 s reused). Result: 0.308-0.325 Gi and
  52.7-53.7 CPU ms per frame, within noise of b7 (0.314 / 52.5). Next: line-level profile of `renderer_texture`
  (b8: ~1,450 of 6,261 samples on the busiest worker, under `bind_textures`).
- Buffer textures in place (b11 profile, b12): `renderer_texture`'s self time (~1,160 samples) was `hash_full` over
  buffer textures (target 10: UI vertex data), re-hashed every frame they're bound, a serial multiply chain. In a
  physical pool they're now linear textures over the pool's no-copy MTLBuffer (`buffer_texture_in_place`:
  `newTextureWithDescriptor:offset:bytesPerRow:`, offset and row pitch aligned to
  `minimumLinearTextureAlignmentForPixelFormat`), so the GPU reads guest memory itself: no hash, no upload. Others
  fall back to the copy (the first 5 of each kind are logged as `buffer texture in place / copied`). b12 vs b10:
  0.308-0.325 -> 0.285-0.289 Gi/frame, 52.7-53.7 -> 46.4-47.1 CPU ms/frame; HUD, radar, banners look right. Since the
  b5 baseline: -22% instructions, -28% CPU time per frame.
  One-row buffer textures round their pitch up to the alignment (16), so none fall back now (b13).
- FRECPE / FRSQRTE on an AArch64 host use the host instructions (`vrecpes_f32` etc.): bit-identical to the
  pseudocode versions over all 2^32 float32 inputs and 200M random float64 ones (NaNs, subnormals), and the software
  FRSQRTE's search loop was ~200 samples per 10 s. `a64_call`'s cache now holds the host function itself (offset from
  `a64_call` in 4-byte units) so a hit is one load. b14: 0.274-0.284 Gi, 46.2-46.5 CPU ms per frame (b12: 0.285-0.289,
  46.4-47.1). What's left on the CPU is mostly guest code (the hottest single function, `F_0e58fab0`, ~700 samples
  per 10 s, is a 24 KB-stack game function), i.e. translator quality: structured loops, keeping guest registers in
  host registers across a function.
- Guest registers across guest stores (b15 profile, b16): in b15 72-80% of busy samples are guest code over 300+
  functions (`a64_call` 4-5%, the rest of the runtime 1-2% each). The generated code reloaded the global `mem` and
  every guest register it had read from `c` after each guest store (~3 extra instructions per store): a store through
  `mem` may alias both. Now each generated function copies `mem` into a local `M` (cpu.h `MEMB`; `LD64` etc. are
  macros over `ld64(M, a)`) and takes `CPU *restrict c` (the `CPU` lives in the host `Thread`, never in guest memory).
  Functions with a guest setjmp keep plain `CPU *c`: a longjmp would bring back host registers cached before it.
  `build.py --no-restrict` turns it off for crash diagnosis (with restrict, guest registers in `CPU` can be stale at a
  fault mid-block; calls and returns still write them back). Two catches: `-O1` has no GVN, so restrict changes nothing
  there (the generated code is `-O2` now); and capture tracking gives up on pointers with over 100 uses and then
  ignores restrict (`-mllvm -capture-tracking-max-uses-to-explore=1000000`). `F_0e58fab0`: 20,489 -> 13,062
  instructions, 2,603 -> 1,125 loads from `c`. Full rebuild 43 -> 61 min, peak memory per unit unchanged (~1.1 GB on
  the largest), binary 726 -> 536 MB. b16 vs b15: 0.279-0.284 -> 0.166-0.169 Gi/frame (-40%); CPU ms per frame about
  the same (52.0 / 45.7 vs 46.3) but with the P cores at 1.29-1.46 GHz instead of 2.73, i.e. ~half the cycles per
  frame; fence waits 0 -> 6-7 ms/frame (the CPU now waits on the GPU). No faults; open play and close-ups look as
  before (the black arms/hands in close-ups were already there in b8).
- GPU profile with stage timestamps (2026-10-20, runs `build/g1`: prof 0/1/2, `build/g2`: prof 0/3/0). New level
  `FIFA_GPU_PROFILE=3` / `prof 3`: an `MTLCounterSampleBuffer` (4,096-sample ring) with stage-boundary timestamps on
  every render pass (vertex start/end, fragment start/end) and compute dispatch, resolved in the command buffer's
  completion handler. Submission is unchanged, so unlike levels 1/2 the numbers are real: GPU 19.2 ms/frame with it vs
  18.7-18.8 without. Logged per tag as total (first stage start to last stage end), `vtx`, `frag`. (g2's log has the
  totals 41.67x too big: `sampleTimestamps`' CPU value is already ns; fixed after the run.)
  - Without thermal pressure (g1, g2: thm 0-1, P 2.5 GHz) open play is CPU 27-28 ms, GPU ~19 ms per frame, fence
    waits 0: both fit the 30 fps cap with room. b16's 6-7 ms of fence waits were at a 1.3-1.5 GHz P clock.
  - Per frame (g2, 3,390 frames): stage time (vtx + frag; compute: start to end) sums to 12.5 ms of the 19 ms in
    command buffers. Main pass (first FS `16f973400`, ~287 draws, 896x504 drawn) **6.5 ms: vertex 2.6, fragment 3.9**,
    half of all stage time. Then FS `17119b900` 1.0, the two 0x0 depth passes 0.96, `16de63d00` 0.78, the three
    1280x720 depth passes 0.64, `16e7d0e00` 0.59, the 512x512 shadow pass `16f9f9000` 0.52 (vertex), `16f124700`
    0.31; every compute dispatch together ~0.4 (`16dff9100` 0.07 for 2).
  - Gaps: several passes span far more than their stages (0x0 depth 4.8 ms span for 0.96 of work, 1280x720 depth
    5.3 for 0.64, `16e6d5200` 2.4 for 0.08, `16fc74000` 0.73 for 0.02): they wait for earlier work before starting
    (Metal's hazard tracking on resources written just before, or the blits between passes). With the ~6.5 ms of
    command-buffer time that isn't stage time, that's the other place to look. (Superseded by "GPU busy vs idle"
    below: those spans include stages of other passes running at the same time; the GPU is idle only ~1 ms a frame.)
  - `prof 1` misranked: the depth-only passes (~10 ms/frame there) are ~1.6 ms of real work; their own command buffer
    each added the wait to them. `prof 2`'s per-draw passes reload the attachments per draw (glyph quads `16ea35100`:
    11.3 ms/frame there, 0.15 in reality); use it only to rank draws within one pass.
- GPU busy vs idle (2026-10-20, runs `build/g3`, `g4`; prof 3:120, prof 0:60). `prof 3` now also takes the union of
  every stage interval (vertex, fragment, compute) per report: `busy` is GPU time with some stage running, `idle between
  stages` the gaps under 1 ms (longer ones are between frames), and each tag gets the idle right before its stages.
  **Open play: busy 11.5 ms per frame, idle between stages 1.05 ms** (g3). So the "~6.5 ms of command-buffer time that
  isn't stage time" above is not idle GPU: command buffers overlap and their GPUStart..GPUEnd includes queueing, and
  per-pass spans include the next pass's vertex stage running early (tiles). The stats line's `gpu` (sum of command
  buffer times, 19-20 ms) overstates GPU work; the GPU sits idle ~2/3 of a 33 ms frame and would fit 60 fps.
  - gmem windows were declared `MTLResourceUsageRead | Write` for every shader using LDG, which makes Metal order
    later encoders using those pools after it. Now Write only for shaders with STG/ATOM/RED (`uses_gmem_store`, which the
    MSL STG path never set); `FIFA_GMEM_RW=1` restores the old usage. g3 (new) vs g4 (old): busy 11.5 vs 13.3 ms and
    idle 1.05 vs 1.43 per frame, but g4's scene drew 23% more (34.8k vs 28.2k draws per 2 s, CPU +15% too); per draw
    the same (0.025 vs 0.023 ms). Correct, not a measurable win. Open play renders as before.
- Host thunks in `a64_call` (runs `build/b17`, `b18`; the stats line now ends with `fn misses`: calls that missed
  `fn_cache`). b17: ~1.13M misses per 2 s (~9k a frame), almost all calls to host thunks (the NVN API through function
  pointers): they aren't in the function table, so each missed the cache and binary-searched before reaching the thunk
  check. The thunk range is now tested first. b18: 14k misses per 2 s; 0.189 -> 0.177 Gi and 31.3 -> 29.6 CPU ms per
  frame (b18's scene drew 3.5% fewer draws, so roughly half of that is the scene). `a64_call` self samples 476 -> 383:
  the rest is the indirect call. `pool_find` got a 4-entry per-thread MRU (`renderer_pool_window` 58 -> 61 samples:
  no change, it's the lock).
- Item 1's "structured loops instead of `switch (pc)`": already the case; only the chunks of functions over ~6,000
  instructions (1,207 chunks) dispatch with `switch (pc)`. The hottest guest function `F_0fc1e9c0` is the job system's
  LDXR/STXR work-queue poll (spinning, not work). Busy samples (b17): ~50% guest code over hundreds of functions,
  `a64_call` 6-8%, every runtime piece (texture checks, ring snapshots, pool lookups, objc) ~1% each.
- Guest registers in locals across calls (2026-10-21, runs `build/b19` new, `b20` the binary before it, back to back,
  thermal 0 both): the translator change from item 2. First try (V registers as locals too) made large functions up
  to ~40% bigger (128-bit V spills on the host); X only: -11% memory ops on a sample unit. Two protocol bugs crashed
  the boot, found with a unit then function bisect (`FIFA_NO_LOCALS`): a local not reloaded after a call (dead) was
  later stored at a return joined by a path that had changed it, overwriting the callee's result in `CPU`
  (`F_0ac73480`); and a register the callee may change but that's needed after the call has to be stored before it,
  or `CPU` holds a stale value on paths where the callee leaves it alone. `tools/verify_locals.py` checks both on all
  339,737 functions (and reports thousands of problems on either earlier version). b19 vs b20: 0.164 vs 0.173 Gi/frame
  (-5%), 27.0 vs 27.7 CPU ms/frame at a lower P clock (2.68 vs 2.80 GHz): ~72 vs ~78 M P-cycles/frame (-7%); b20's
  scene drew ~9% more (32.7k vs 29.8k draws per 2 s), so part of that is the scene. Cores busy 1.63 vs 1.68, swap
  ~1.0 GB both, game RSS ~760-850 MB in open play. Binary 536 -> 529 MB (also `-fno-stack-protector`). Full rebuild
  ~25-28 min with -j6. Regeneration ~4-8 min (was 80 s).
- Visual pass, first round (2026-10-20, runs `build/w1` hidden, `build/v1`..`v3` visible: `VISIBLE=1 LOAD=4
  tools/bench.py ... 'fps -1:3' x12` takes walkout shots every ~3 s). Shader coverage 437/437 (shtool):
  - The 5 compute programs that failed are dispatched at match load: VMNMX (`3a2c`, `16ea01d00` `16ebcb400`, and an
    FS: `d = min(min(a, b), c)`, now general per yuzu's decoding: selectors, widths, MRG/ACC/MIN/MAX; .SAT/.CC fail)
    and SUST to buffer images (surface type 1, `16ea3ec00` `16eca9800` `16f469b00`; bound as 1D images: both are
    rows of 16384 texels). With them the **black arms/legs/hands are fixed** (the skin was composited by these).
  - FS `16e40c500` fetched through a table of separate textures indexed in a loop (`LDC c[2][r + 0x168]` OR the
    sampler word at 0x570). `trace_handle` flags the indexed LDC; `bindless_slot` gives TEX.B 8 candidate slots
    (0x168 + 8k) and `emit_tex` picks at run time the one whose `c[2]` words equal the handle register.
  - The ~10% of draws skipped in cutscenes (reason b, `!pr`, 0 in open play) were vertex-only programs
    (stage mask 0x1: depth pre-passes / shadow casters, position only, no stores), dropped by `get_program`.
    They now draw with a stub fragment stage and colour writes off (`Prog.depth_only`); skips ~0. That did
    not change the black jackets or the flat black/white crowd (they were drawn, just wrong).
  - Black jackets (run `build/d4`, debug frame triggered on the dark walkout shot; `FIFA_DBG_PERDRAW=1
    FIFA_DBG_PIXELS=x,y;...` logs every draw that changes those pixels of colour target 0): the jacket pixels
    are written by FS `16fa89700` with ~3e-4 (black). Its textures look right; its slot 9 is the 256x1
    RGBA16F LUT at 1f91d4000 from FS `16e3a1a00`, which is NaN in all 256 texels: `t0[x] / t0[255]` per
    channel, and channel G of its inputs (256x1 RG16F at 1f91d2000 / 1f91d0000, from the max-reduction FS
    `16dda7600` / `16e3c8b00` over the 256x256 R16F target that FS `16fa54a00` draws the players into, and
    compute `16ed0fd00`) is all zero: G = 0 * (1/0). (The 256x256 target is drawn in the match now, unlike
    the 2026-10-19 note above.) `FIFA_NANZERO=16e3a1a00` (LUT NaN -> 0) leaves the jackets black and the
    face patches, so 0 isn't the value either: next, why G is zero (the reduction's diagonal `t0[(i, i)]`
    TLDS reads, per yuzu's 2D-array TLDS layout x = a+1, y = b) or what the jacket FS does with the LUT.
    (Both answered in "Visual pass, second round": the layout was wrong, and the jackets aren't the LUT.)
  - Still open: grey/white sharp-edged patches on faces and necks; crowd drawn flat black/white; grass
    shimmer/grain (no LOD bias is applied: Metal samplers have none; or TAA).
  - Cutscene fps in the visible window (v3, v4) dropped to 2-16 with the CPU at ~1 core and the GPU mostly
    idle while the window was not frontmost (screenshots, chat): presentation throttling, not rendering
    cost; v1/v2 (window left alone) held 30.
  - Visible window, open play, thermal 0 (v1, v2): 30.0 fps, P cores ~2.6 GHz; the old 12-17 fps was throttling.
  - (`pgrep -f tools/bench.py` in a wait loop matches the loop's own shell: wait on the bench's PID instead.)
- Visual pass, second round (2026-10-20, runs `build/w2`..`w4`, `d5`..`d19`):
  - **TLDS ARRAY_2D.LZ (texture_info 8) is layer Ra, x Rb, y Rb+1**, not TEXS's x Ra+1, y Rb. The dumps show it:
    every such TLDS has its layer in Ra and (x, y) as a pair at Rb, often contiguous after Ra (r3 / r4,r5). The old
    decode made the exposure chain's max reduction (`16dda7600`) read `t0[(i, i)]` for its G channel, so G was 0, the
    prefix sum (`16ed0fd00`) was all zero and the 256x1 LUT (`16e3a1a00`, `cdf[x] / cdf[255]`) NaN. Now G is set
    and the LUT is finite (0 of 256 non-finite). It did **not** change the black jackets.
  - Black jackets, narrowed with register probes on FS `16fa89700` (output = `lit * cb6[300].w * (1 - fog.a) +
    fog * cb6[300].w`; `cb6[300].w` ~0.009 is the exposure every lit FS shares, white shorts reach ~140 before it):
    the light term is fine (~200), fog 0, but the diffuse colour `r9 = base * (1 - r36)` is 0 on most jacket
    pixels, so only specular (~0.05) is left. The base colour is a palette blend, `sum(mask_i * cb4[i]^2)` with the
    masks from texture slot 1 (512x512 fmt 0x46, mostly R) and the team colours in cb4 (ubuf 1): Brighton's jacket
    colour `cb4[2]` is 0.1176 (sRGB-ish, ~0.014 linear), so the jacket is meant to be near-black but should
    still read as dark grey with shading (~0.014 * 200 * 0.009). Next: probe the mask sample (t1 at pc ~0x188,
    r12-r14) and the palette sum (r21/r22, r3 at 0x468) on the same pixels: either the mask's channels are
    wrong (fmt 0x46 decode / swizzle) or a branch picks the black path. Slot 3 is bound to an EA SPORTS logo
    (256x256 fmt 0x9a, GPU-written): a decal rectangle (cb5[1]), worth a look after.
  - Some walkouts skip the jackets (kits only), so a run can have no jacket draws at all.
  - Face/neck patches: the same with the old TLDS decode (`w4`), so not from this change.
  - Register probe, extended: `FIFA_DBG_FS=<fs>:<pc>:<reg>[,<pc>:<reg>[,<pc>:<reg>]]` writes up to three
    registers to R, G, B. With `FIFA_DBG_PERDRAW=1` the debug frame logs `[probe] draw N: <pixels>, mean/min/max`
    over the pixels each draw of that FS changed in a >= 1280-wide colour target 0 (GPU copy of the target after
    every draw, read back only around probed draws; R11G11B10F targets clamp negatives to 0). A `debug` request
    with a probe waits for a frame after one that drew the FS at least once (was 4: that skipped close-ups). Pc's:
    `LARECOMP_SAVE_MSL=1 build/shtool -o <dir> <dump>` annotates the MSL with `// pc`.
  - `tools/tomatch.py`: the match intro's stand shot (white crowd cards mid-screen) passed as the notice; the
    notice check also wants the bottom edge dark now.
- Visual pass, third round (2026-10-20, runs `build/f1`..`f25`, `j1`..`j5`): **face/neck patches and black jackets fixed**.
  - The patches were in the head FS `16fabce00` (not `16fafed00`, which only draws a few pixels): its light loop
    ends with `FSETP p0 = r54 <= 0; FSET.CC RZ = r48 <= 0; @P0 BRA CC.NEU <exit>`. The translator emitted nothing
    for an FSET into RZ and ignored the BRA's condition-code test, so the loop left after the first light on every
    pixel where `r54 <= 0`; the per-light specular sum (`r39`) was then ~0.05 or ~10 in sharp-edged regions.
    FSET/ISET `.CC` now set the zero/sign flags (also into RZ) and BRA tests CC (`cc_test`, shared with CSETP).
    1848 BRAs in 366 of the 437 dumps carry CC.NEU (all others CC.T); 1815 follow an FSET.CC, 13 an ISET.CC, 20 no
    producer in reach (flags stay clear: taken, as before). 437/437 still translate and compile.
  - Found with the new debug aids: `FIFA_DBG_WRITERS=1` (with `FIFA_DBG_PERDRAW=1`, debug frame: per pixel of each
    full-size colour target 0 the last draw that changed it, `build/writers_<addr>_<present>.bin` (uint32 draw numbers,
    matching `[writer] draw N ... fs` lines) and `.f32` (RGBA floats)); the register probe also writes
    `build/probe_<frame>.f32` (RGBA floats the probed draws wrote, 0 elsewhere: raw values, unlike the tonemapped
    screen); `FIFA_DBG_NODTEX=1` skips the debug frame's texture dumps, so several `debug` requests fit in one
    walkout (`'fps -1:12,debug:1,fps -1:4,debug:1,...'`); `FIFA_DUMP_TEX_FS=<fs>` dumps that program's textures
    on every draw (like the debug frame); `FIFA_LOG_RT=WxH` also matches the depth target, and draw lines print
    the depth target's address (`d0x32@<addr>`); `FIFA_LOG_CLEARS` logs depth clears (`[dclear]`) and colour
    clear values/addresses. The walkout's shot order varies from run to run (line-ups first or handshakes first).
  - `nvnCommandBufferSetDepthClamp` was a no-op; it now sets Metal's depth clip mode per draw. The game toggles it
    (shadow casters, the load-time top-down captures). It did not change the faces.
  - **Black jackets fixed** (runs `build/j1`..`j5`): probing FS `16fa89700` after pc 0x468 gave decal alpha
    `r7` = 1 on every jacket pixel, so the base colour was the decal's (black) instead of the palette (`r14`, fine).
    The decal (slot 3) is the club crest, 256x256 fmt 0x9a (BGRX8 sRGB, no alpha) written by CopyBufferToTexture,
    under a CLAMP_TO_BORDER sampler with border 0 (tsc 260): outside the crest rectangle it should read alpha 0.
    Alpha-less formats read alpha 1 through an alpha -> ONE view swizzle, and Metal swizzles the border colour
    too, so the whole jacket got the crest's opaque black corners. Now those formats keep the swizzle except
    under a border-clamped sampler (`sampler_border`), and CPU uploads / buffer-to-texture copies store alpha 1
    (`renderer_fill_alpha_one`). The `[dtex]` lines print the sampler's wrap modes and border colour. (A BGRX
    texture rendered to on the GPU and sampled with a border sampler would read whatever alpha was rendered.)
  - Ruled out on the way: the head's world-space height-field lookup (t0, a 1024x1024 R11G11B10F packed at match
    load by FS `16ede1800` from two 2048x2048 D16 top-down captures and an R8 mask; its G input is cleared in the
    same frame before its own pass, so G is 0 as recorded); the step test is uniform over the heads. The sun's PCF
    shadow (pc 0xbf8-0xf28) is ~0 (unshadowed) on faces.
- Visual pass, fourth round (2026-10-21, runs `build/c1`..`c14`, `k1`..`k5`): **flat black/white crowd fixed**.
  - The crowd is instanced sprites: VS `170197000` + FS `16fe83400` (far), VS `16f9a2200` + FS `16fd3ac00` (near).
    The VS checked out (every vertex of 4,191 instances is one affine map of its bone-skinned position); the spiky
    crowd in `c1`/`c10` came from the per-draw-readback debug frames, which stall for seconds while the game's
    animation threads keep refilling the CPU-written bone ring (t4), not from normal frames.
  - The FS's albedo is `atlas.rgb * (1 + atlas.a * (tint - 1))`, the atlas `t4` being a 512x512x32 BC7 sRGB array with
    10 levels, the layer and tints packed per instance in `t2`. That atlas was 2% non-zero in guest memory and never
    written by the GPU. A CPU watchpoint on it showed Frostbite copying each layer/level itself to an address from
    **`nvnTextureGetViewOffset`, which was stubbed to 0**: all 32 layers x 10 levels landed on offset 0. It now
    returns `layer * tegra_layer_size + tegra_level(level).offset` for the view's base level / min layer. Atlas 87%
    non-zero, crowd drawn in colour in the walkout and open play (`k5`, `k6`; the open-play stands were dark and
    mostly empty before). Every texture Frostbite loads per subresource went through this. The grass grain is unchanged.
  - Debug aids: `FIFA_WATCH_TEX=<w>x<h>x<d>:<fmt hex>` arms the CPU write watchpoint (`FIFA_WATCH`, `hle/debug.c`) on
    the first such texture when it is initialized (addresses differ between runs); `FIFA_WATCH_GPU=<addr>:<size>` and
    `FIFA_WATCH_FMT=<fmt hex>` log copies/buffer clears into that range or into textures of that format (`[watch]`,
    with view levels/layers and region). `FIFA_DBG_VSREG=<pc>:<reg>[,...]` (up to 4) adds vertex-stage register
    probes to `FIFA_DBG_VS`, which now dumps the debug frame's draws of that VS to `build/dbgvs_<n>.bin` (per instance -
    base < 1024 and vertex < 64: position, first four attributes' raw bits, the probes; `FIFA_DBG_VSTEX=<slot>` adds
    three texels of that buffer texture at probe 0's value). `FIFA_NO_INPLACE=1` copies vertex/index buffers and
    buffer textures instead of reading guest memory in place. `[probefs]` lines print levels, views and sampler state;
    writer maps are named `build/writers_<addr>_<present>.bin`.
- Visual pass, fifth round (2026-10-21, runs `build/g1`..`g10`): **grass grain fixed; it was the colour grading LUT** (also the black sky:
  the night sky now renders dark navy with the trees and floodlights, `g10`).
  - The scene target after the pitch draw (FS `170958600`) was smooth (writer map + `.f32`); the two-tone grain came
    from the post FS `16de63d00`, which reads the scene directly (no TAA; its grain slot t7 is a 1x1 dummy). Its 32^3
    RGB10A2 grading LUT (t6) had colour in 4381 of 32768 texels: colours that fell in an empty cell went black, their
    neighbours didn't, so the dark mowing stripes (scene noise ~8%) broke into black/green pixels and read too dark.
    (The pitch's tiled detail textures t7/t8, 512x512 BC with one level, tiled 4x and 24x over the pitch, were the
    first suspect; the game creates them that way.)
  - The LUT is rebuilt at match load: compute `16dd60c00` (33^3 RGBA32F asset -> 33^3 RGB10A2) then `16e714900`
    (resample to 32^3 with white balance and an Oklab-style gamut map). Two translator bugs, found by replaying
    `16e714900` offline (`tools/lutreplay.m`, bit-identical to the game, with `tools/msl_probe.py` register probes):
    - **IADD3 ignored its shift and halves** (register form: bit 37-38 `.RS`/`.LS`, 35/33/31 operand halves). The
      gamut map's cube roots divide float bits by 3 with a multiply-high (XMADs + `IADD3.RS`), so `(a + b) >> 16`
      came out as `a + b`. `.RS` now shifts the 33-bit sum. 25 of the 437 dumps use `.RS` (among them the exposure
      chain `16e884800` / `16e3c8b00` and `16dff9100`); none used the halves.
    - **Predicated variable-latency results were written back when the predicate was off.** A deferred result
      (`lwB_K`, see `defer_late`) is committed at the scoreboard wait; with `@P0 MUFU.LG2 r4` off, the commit put
      back r4's old value over the `@!P0 FFMA r4` that ran in between (the ACEScc encode's linear segment), so every
      dark channel was garbage (dark greys -> green). Guarded deferrals now keep the guard in `lwvB_K` and commit
      only if it held. 268 of 437 dumps have such slots (4060 commits); results differ only where the old code
      was wrong.
  - After both: 33^3 and 32^3 LUTs are full and smooth along the neutral axis (32^3 diag 75 .. 999, slightly cool
    from the white balance `cb3[6]` = 0.94, 0.96, 1.10); the grass has no grain, the dark stripes are the right shade,
    stands and tunnels are neutral. With only the IADD3 fix (`g7`) dark tones went green (bright green sky in the
    reverse angles); that was the second bug.
  - Debug aids: `FIFA_DBG_PROBE_3D=1` writes level 0 of 3D textures the probe touches (`[probefs]`, `FIFA_DBG_CSW`)
    to `build/probe3d_<w>x<h>x<d>_<mtl fmt>.bin`.
- Visual pass, sixth round (2026-10-21, runs `build/s1`, `s2`): **night-match shadows work; nothing to fix**.
  - Player shadows at night are a screen-space buffer, not a shadow map: a 512x512 RG target (fmt 0xd) cleared to
    white, the players drawn into it 5 instances per draw (FS `16f9f9000` and three others: per-floodlight cone
    falloff from the light table at `cb0[81]`, 0xa0 per light) with MIN blending (bf ONE,ONE, eq 4,4), then blurred
    twice by FS `16e2df900` (1f3590000 -> 1f3bd0000 -> 17872cc00). The pitch FS `170958600` samples it as t3 at
    pc 0x268 with coordinates from the fragment position. The buffer holds the X-shaped shadows (one arm per
    floodlight), the scene pass shows them under every player, and the final frame keeps them, softer after the
    tonemap: they only look absent in wide open-play shots.
  - The sun's PCF (`gather_compare` on a depth array, ~90 lit FS, e.g. the head FS's t6) is bound to a 1x1
    Depth32Float at night: no sun shadow map in these matches. Untested until a day match is played (the
    cascades, TLD4.DC with offsets, compare samplers: `sampler_for` maps the compare mode/function).
  - (A `debug` frame with `FIFA_DBG_PERDRAW=1` takes ~0.1 s per draw: give the segment after it a minute or more,
    or the frame is cut off before the main pass.)
- Visual pass, seventh round (2026-10-21, runs `build/r1`..`r5`): **reflections: cube faces written through views or
  z offsets all landed on face 0**.
  - No screen-space reflections on Switch: the reflections are cube maps. Bound in the match (head FS `16fabce00`
    t9-t11, ~60 lit FS): the sky/environment cube (128^2 RG11B10F, 6 levels, from the sky FS and the compute
    filters), a 32^2 RGBA16F cube array of 10 local probes (2 captured: stadium faces with floodlights and pitch;
    the rest empty), a single-channel cube array sampled with the same probe index as t9 (only `.r` kept: a
    per-probe factor), and a 256^2 cube on a few props (FS `16fcddd00`). The sky cube and the 2 probes check out
    (all faces, horizon and glow continuous across shared edges).
  - The factor cube defaults to a 1x1 RGBA8 cube (fmt 0x25, 175e12400 every run) the game fills at boot with six
    1x1 buffer-to-texture copies, one per face **through a one-face texture view at region z 0**.
    `copy_region_dims` ignored the view's layer, so all six landed on face 0 (white) and faces 1-5 stayed 0: the
    local-probe reflections on heads were multiplied by 0 for 5 of 6 directions. Now z counts from the view's min
    layer for arrays/cubes (buffer-to-texture and texture-to-buffer); cube arrays count as layered too. 6/6 faces
    now (`r5`). Not compared on screen (the cube only feeds the head FS family; no matching close-up between runs).
  - Same class, fixed on the way (no hits seen in the match, but wrong): texture-to-texture copies (and
    `CMD_DOWNSAMPLE`) used layer 0 of both sides whatever the region's z / depth (`copy_texture` now copies each
    layer as 2D, all layers both have when the region has no depth; a cube source view's face counts);
    `nvnTextureWriteTexels` ignored its view (level, layer) and the z of layered textures; clears of a whole
    cube/array target reached layer 0 only (passes with such attachments set `renderTargetArrayLength`, the clear
    draws one instance per layer through `[[render_target_array_index]]`, `ClearTexture` limited to its region's
    z range).
  - Debug aids: `FIFA_DBG_PROBE_FS` takes up to 8 programs (`<fs>,<fs>,...`); `FIFA_DBG_PROBE_PNG=1` writes level 0
    of every probed cube/array (each face or layer, up to 12) to `build/probeslice_<addr>_<slice>.png` (+ `_n.png`
    for float formats), once per texture. The `[copy3d]` lines (also printed for `FIFA_DBG_RTW` textures) are what
    showed the six per-face copies; `[rtw]` doesn't log buffer-to-texture copies.
- Visual pass, eighth round (2026-10-21, run `build/d1`, hidden): **a day match; sun shadows work, nothing to fix**.
  - To get one: Kick Off -> Match Preview -> X (Game Settings, Match tab) -> Conditions -> Dynamic Time of Day, left
    in 30-minute steps from 8:00 PM to 3:00 PM (Time of Day then reads DAY); Match Preview shows "Sunny | 3:00 PM".
    `tools/tomatch.py --to-kickoff` stops on Kick Off's first page (Classic Match) for this.
  - Open play (`k10`..`k13`): the main stand's roof casts a sharp-edged shadow over the far third of the pitch with
    the roof's lit strips in it, every player casts a sun shadow the same way, close-ups (`k09`) are sun-lit (hair
    highlights, skin). 30.0 fps, 0 skipped draws, no faults. The sky isn't in frame from the broadcast camera, so
    the day sky cube was only seen through the lighting.
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
- `audout:u` (`recomp/rt/audout.c`): the game opens one 48 kHz 6-channel
  output. Buffers are copied at append and played by a CoreAudio output unit
  (5.1 mixed down to stereo); the render callback releases each buffer when
  played and signals the buffer event, so the device clock paces the audio
  thread. Works (2026-10-21, checked by ear). With `FIFA_HIDDEN=1` (unless
  `FIFA_AUDIO=1`) or `FIFA_AUDIO=0` the old null sink releases each buffer
  after its duration instead, so benchmarks stay silent.
- `hwopus` (`recomp/rt/hwopus.c`, libopus v1.5.2 from `tools/build_opus.sh`
  into `third_party/opus`): music and commentary are Opus streams the game
  decodes through ~68 multistream decoders (manager cmds 9/6, decoder cmd 9,
  thousands of packets a minute). The stub answered with zero samples, so
  only sound effects played; with libopus music and commentary are heard
  (2026-10-21). Packets have an 8-byte header (BE size, final range).
  Work buffers (transfer memory) are ignored. For the web build, libopus
  compiles to wasm as is.
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

Kick Off plays a full match (loading cards, line-ups, 3D scene, HUD,
half-time) at 28-30 fps since the barrier fix ("Match performance, third
round") and blits on by default ("Blits", "Match profile"). Performance is
at the cap with room on both sides (CPU ~28-30 ms, GPU busy ~11.5 ms of 33),
so the visual pass (4) comes next. In order:

1. ~~`16dff9100`~~ done (divergent barriers, SR_LANEID / lane masks).
2. Then the remaining performance items from the second round: ~~GPU drains
   on `CMD_COPY_BUFFER_TO_BUFFER` / `CMD_COPY_TEXTURE_TO_BUFFER`~~ (blits,
   on by default), ~~`a64_call` lookups, texture gathers, ring allocation,
   constant copies~~ ("CPU cost per frame, first round"), `upload_texture`'s
   per-frame change checks and the ~10 MB/frame of uniform snapshots (~1%
   of busy samples each now), ~~guest registers across guest stores~~
   (`restrict`, -O2), ~~structured loops~~ (already gotos), ~~thunk calls
   missing `a64_call`'s cache~~. What's left is spread thin (see "Host thunks
   in `a64_call`"): ~50% guest code over hundreds of functions, `a64_call`'s
   indirect calls ~6%. ~~Guest registers in locals across calls~~ (-5% instructions,
   "Guest registers in locals across calls"); next step on that line: pass x0-x7
   and the results as C arguments/return values so they skip `CPU` too.
   Measure with `tools/bench.py` + `tools/benchsum.py` (cost per frame; draws
   per 2 s differ between runs, compare them too), never while building. Each bench run is recorded in git under
   `runs/<name>/` (info.json: command, settings, commit, uncommitted diffstat, outcome; summary, marks, tomatch,
   cores, game.log.gz); a name already used in `build/` or `runs/` is refused. Runs before u1 aren't recorded, and
   the 2026-10-20 `f1`..`f5` were overwritten by today's u1..u5 (named f1..f5 then).
   The second 30 fps lock (see "The 30 fps cap"): found, the main loop's limiter
   `F_119e3e70` (patched through `host_rate_hz`). Next: bench `fps 60` with it, and check the match doesn't run at
   double speed (a fixed step per tick would need `ForceSimRate` / `ForceDeltaTime` to follow). Blocked first on
   hidden runs sticking at Select Sides (null-sink audio, "The 30 fps cap"); `FIFA_AUDIO=1` gets through.
3. ~~The black 3D scene for part of the first half~~ fixed by the lane
   masks. ~~The 32^3 3D texture sampled as 2D~~ is the game's own stale
   binding, harmless (see "Blits").
4. Visual pass over the match ("Visual pass, first round"): ~~black/noisy skin on
   arms/legs/hands~~ (compute fixes), ~~draws skipped in cutscenes~~
   (vertex-only programs), ~~the NaN LUT~~ (TLDS array layout), ~~grey patches on
   faces~~ (BRA CC tests, "Visual pass, third round"), ~~black jackets~~ (border colour
   through the alpha-one swizzle, same section), ~~flat black/white crowd~~
   (`nvnTextureGetViewOffset`, "Visual pass, fourth round"), ~~grass grain~~ (the grading LUT: IADD3.RS and
   predicated late writes, "Visual pass, fifth round"). ~~Shadows~~: the night floodlight
   shadows work ("Visual pass, sixth round"), ~~sun shadows~~ in a day match ("eighth round"). ~~Reflections~~: cube maps; the
   per-probe factor cube had 5 black faces (copies through one-face views, "Visual pass, seventh round"). Nothing
   known left; the day sky cube hasn't been seen directly (not in the broadcast camera's frame).
5. Shader coverage (`build/shtool`): 437/437 of the dumps translate. Seen
   at boot only: SUATOM (`ea70`, CS `16e61ea00`, `16f0ce900`).
   GPU side: not a bottleneck (busy ~11.5 ms per frame, ~1 ms idle between stages: "GPU busy vs idle"); the main
   pass's vertex (2.6 ms, incl. `gfind`) and fragment (3.9 ms) stages only matter for 60 fps.
6. ~~Open play at 12-17 fps in the visible window~~: 30.0 fps visible at
   thermal 0 (v1, v2); it was P-core throttling.
7. ~~Keyboard~~ (FIFA PC layout) ~~and mouse~~ (touch screen) ~~audio output~~
   (CoreAudio), ~~music and commentary~~ (hwopus on libopus): all checked in the
   visible window. Still to try: a gamepad;
   the dark Continue button on the accessibility screen (seen before the
   threading fix).

Later / as needed:

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

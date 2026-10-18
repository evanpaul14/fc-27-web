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

## Seams for the port

1. **SDK boundary**: `main` imports 832 functions from nnSdk (std 137, os 47,
   fs 45, hid 37, friends 37, codec 36, account 33, audio 29, socket 28, oe 24,
   ec 21, ...). Implement these as host shims (HLE at the nn:: API level, like
   the Win32 shims in driver-web) instead of emulating Horizon syscalls.
2. **Renderer**: the NRO exports a small C entry API (`rc2DeviceInit`,
   `rc2DeviceInitTemporary`, `rc2DeviceShutdown`, `rc2DeviceVersion`,
   `rc2Get/SetSystemServiceProvider`) and consumes RenderCore2's platform-neutral
   opcode IR (`fb::Opcode*`, `fb::OpcodeProcessorBase<PlatformEncoderNvn>`).
   Replacing the NRO with a WebGPU RenderCore2 backend avoids emulating NVN,
   and the DWARF gives us its exact types. `main` itself only uses 14 NVN
   entry points directly (texture/memory-pool setup and readback).

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
python3 -I recomp/build.py build --all                # ~14 min: C for all 310k functions -> build/gen (2.6 GB)
make -C recomp -j6                                    # build/fifa (native, AArch64 host)
./build/fifa                                          # FIFA_TRACE=1 logs svcs
```

- Layout: main at 0x08000000, nnSdk at 0x18000000, host thunks 0x07E00000,
  unresolved imports 0x07F00000, host allocations (stacks, TLRs)
  0x1C000000, heap (svcSetHeapSize) 0x20000000, stack region (GetInfo
  14/15) 0xC0000000, mapped memory 0xE0000000.
- `nn::os` threads map their stacks into the stack region at a random
  address (`MapAliasStack`: QueryMemory until free, then svcMapMemory). The
  kernel records svcMapMemory destinations so QueryMemory reports them as
  taken (type Stack) and the rest of the region as free.
- Startup: the host plays rtld and calls `nn::init::Start(thread, rtld_data,
  f1, f2, f3)` with host thunks; f2 runs DT_INIT of nnSdk then main. nnSdk
  itself calls `nninitStartup` and `nnMain`.
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

`./build/fifa` gets through libc/os init and all of `nn::oe::Initialize`
(applet proxy, GPU error handler thread, focus/message queue), then opens
lm, apm, aoc:u, ectx:aw, pctl*, time:u as stubs and dies on the
unimplemented `svcMapSharedMemory` (0x13) with handle 0: `time:u` cmd 20
(GetSharedMemoryNativeHandle) returned no handle.

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

## Next steps

- Shared memory: a kernel object (`svcCreateSharedMemory`,
  `svcMapSharedMemory`/`Unmap`) backed by host memory that host services
  can write, then `time:u` (GetStandardUserSystemClock etc. return
  ISystemClock/ISteadyClock/ITimeZoneService objects; cmd 20 the shared
  memory with the clock contexts).
- `pctl`/`pctl:*` `CreateService(WithoutInitialize)` → IParentalControlService.
- Then keep pushing through init: the remaining services nnSdk opens
  (fs, hid, vi, nvdrv, audio, …) and the 3,325 init_array entries.
- Name the primitive `T_*` types; tie vtables to reflection classes.
- Dump the DWARF types for the `rc2*` interface and the opcode structs from
  the `.nrs` (needed before the WebGPU RenderCore2 backend).

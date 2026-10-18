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

## Next steps

- Name the primitive `T_*` types; tie vtables to reflection classes (class
  records likely point at their vtable or create function).
- Dump the DWARF types for the `rc2*` interface and the opcode structs from
  the `.nrs`.
- Recompiler design: recursive descent from the 273k starts to find the rest,
  split the generated C by address range, and decide what to do about
  NEON/atomics/TLS (tpidr_el0) and nn::os threads.
- Free disk space before Ghidra or asset work (need ~10–15 GB).

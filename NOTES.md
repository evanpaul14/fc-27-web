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

## Next steps

- Load `main.bin` with symbols/relocs into Ghidra (11.2.1 is in
  LibertyRecomp-web-test/tools); recover RTTI class names and Frostbite asserts.
- Dump the DWARF types for the `rc2*` interface and the opcode structs.
- Measure the recompile scale: 194 MB of text is ~48M instructions, roughly
  10x driver-web. Plan function discovery (eh_frame covers most functions)
  and how to split the generated code.
- Disk is nearly full (2 GB free); asset extraction needs about 15 GB more.

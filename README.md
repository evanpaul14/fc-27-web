# fc-27-web

Static-recompilation project aimed at running **EA Sports FC 27 (Nintendo Switch)** on the web.

> This project only works with a **user-owned game dump**. Game binaries/assets are read from local files at runtime and are not stored in this repository.

## What this repo contains

- `recomp/` — AArch64 decoder/translator, runtime shims, and native build glue
- `tools/` — extraction/analysis helpers for NSP/NSO/RomFS workflows
- `NOTES.md` — detailed reverse-engineering notes, architecture, and status

## Quick start

1. Read `/home/runner/work/fc-27-web/fc-27-web/NOTES.md` for full setup and prerequisites.
2. Build the ZBIC decoder tool:
   - `tools/build_zbic.sh`
3. Extract and decode game binaries from your own NSP:
   - `python3 -I tools/nsp_extract.py GAME.nsp extracted`
   - `python3 -I tools/nso.py extracted/exefs/main extracted/code/main`
4. Generate images and translated C:
   - `python3 -I recomp/image.py extracted/code build`
   - `python3 -I recomp/build.py build --all`
5. Build the native host binary:
   - `make -C recomp -j8`

## Current status

- Core static analysis and translation pipeline is in place.
- Current runnable target is macOS native only.
- Graphics are not fully correct yet and there are known rendering issues.
- Runtime/HLE and renderer work are ongoing.
- See `NOTES.md` for up-to-date details and deep technical context.

## License

This repository is licensed under the terms in `LICENSE`.

#!/bin/sh
# Generate, build and run the A64 translator difftest (AArch64 host only).
# usage: recomp/difftest/run.sh [per-tag] [seed] [rounds] [tags]
set -e
cd "$(dirname "$0")/../.."
out=${DIFFTEST_OUT:-/tmp/fifa-difftest}
python3 -I recomp/difftest/gen.py extracted/code/main "$out" --per-tag "${1:-100}" --seed "${2:-1}" ${4:+--tags "$4"}
cc -O1 -w -I recomp/difftest -o "$out/difftest" \
  recomp/difftest/main.c recomp/rt/a64_rt.c "$out/tests.c" "$out/tests.S"
"$out/difftest" "${3:-16}"

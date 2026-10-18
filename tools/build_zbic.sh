#!/bin/sh
# Fetch kinnay/zbic (the commit Eden pins) and build tools/bin/zbic_dec.
set -e
cd "$(dirname "$0")/.."
REV=11b08f2712264bbed731545085cbd9702096ceb7
if [ ! -d third_party/zbic ]; then
    git clone -q https://github.com/kinnay/zbic.git third_party/zbic
fi
git -C third_party/zbic checkout -q $REV
mkdir -p tools/bin
cc -O2 -w -DZSTD_MULTITHREAD=0 -Ithird_party/zbic/src tools/zbic_dec.c -o tools/bin/zbic_dec
echo built tools/bin/zbic_dec

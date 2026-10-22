#!/bin/sh
# Fetch xiph/opus v1.5.2 and build the static library recomp/Makefile links (hwopus, recomp/rt/hwopus.c).
set -e
cd "$(dirname "$0")/.."
REV=v1.5.2
if [ ! -d third_party/opus ]; then
    git clone -q https://github.com/xiph/opus.git third_party/opus
fi
git -C third_party/opus checkout -q $REV
cmake -S third_party/opus -B third_party/opus/build -DCMAKE_BUILD_TYPE=Release -DOPUS_BUILD_TESTING=OFF \
    -DOPUS_BUILD_PROGRAMS=OFF -DBUILD_SHARED_LIBS=OFF > /dev/null
cmake --build third_party/opus/build -j8 > /dev/null
echo built third_party/opus/build/libopus.a

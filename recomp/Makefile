# Build the recompiled program natively: make -C recomp -j8
# (run recomp/image.py and recomp/build.py first).
B := ../build
CC := cc
CFLAGS := -O1 -w -mllvm -enable-misched=false -mllvm -global-isel -mllvm -global-isel-abort=0
# Largest units first so the long ones don't finish last.
GEN := $(shell ls -S $(B)/gen/*.c 2>/dev/null)
OBJ := $(GEN:$(B)/gen/%.c=$(B)/obj/%.o)
RT := rt/host.c rt/kernel.c rt/ipc.c rt/services.c rt/am.c rt/time.c rt/fs.c rt/apm.c rt/pctl.c rt/nifm.c rt/aoc.c rt/audout.c rt/hid.c rt/acc.c rt/a64_rt.c $(wildcard hle/*.c)
NVN := $(wildcard nvn/*.c) nvn/maxwell/translate.c
NVN_M := $(wildcard nvn/*.m)
FRAMEWORKS := -framework Foundation -framework AppKit -framework Metal -framework QuartzCore -framework ImageIO \
  -framework CoreGraphics -framework UniformTypeIdentifiers
HDR := rt/cpu.h rt/runtime.h rt/host.h rt/ipc.h nvn/nvn.h nvn/renderer.h nvn/maxwell/shader.h

$(B)/fifa: $(OBJ) $(RT) $(NVN) $(NVN_M) $(B)/nvn_table.c $(HDR)
	$(CC) -O1 -g -fobjc-arc -o $@ $(OBJ) $(RT) $(NVN) $(NVN_M) $(B)/nvn_table.c $(FRAMEWORKS)

$(B)/nvn_table.c: $(NVN) nvn/table.py
	python3 -I nvn/table.py $@ $(NVN)

$(B)/obj/%.o: $(B)/gen/%.c rt/cpu.h rt/runtime.h | $(B)/obj
	$(CC) $(CFLAGS) -c $< -o $@

$(B)/obj:
	mkdir -p $@

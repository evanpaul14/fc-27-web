# Build the recompiled program natively: make -C recomp -j8
# (run recomp/image.py and recomp/build.py first).
B := ../build
CC := cc
CFLAGS := -O1 -w -mllvm -enable-misched=false -mllvm -global-isel -mllvm -global-isel-abort=0
# Largest units first so the long ones don't finish last.
GEN := $(shell ls -S $(B)/gen/*.c 2>/dev/null)
OBJ := $(GEN:$(B)/gen/%.c=$(B)/obj/%.o)
RT := rt/host.c rt/kernel.c rt/ipc.c rt/services.c rt/am.c rt/time.c rt/a64_rt.c $(wildcard hle/*.c)
HDR := rt/cpu.h rt/runtime.h rt/host.h rt/ipc.h

$(B)/fifa: $(OBJ) $(RT) $(HDR)
	$(CC) -O1 -g -o $@ $(OBJ) $(RT)

$(B)/obj/%.o: $(B)/gen/%.c rt/cpu.h rt/runtime.h | $(B)/obj
	$(CC) $(CFLAGS) -c $< -o $@

$(B)/obj:
	mkdir -p $@

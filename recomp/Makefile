# Build the recompiled program natively: make -C recomp -j6
# (run recomp/image.py and recomp/build.py first).
B := ../build
CC := cc
CFLAGS := -O1 -w -mllvm -enable-misched=false -mllvm -global-isel -mllvm -global-isel-abort=0
GEN := $(wildcard $(B)/gen/*.c)
OBJ := $(GEN:$(B)/gen/%.c=$(B)/obj/%.o)
RT := rt/host.c rt/kernel.c rt/ipc.c rt/services.c rt/a64_rt.c
HDR := rt/cpu.h rt/runtime.h rt/host.h rt/ipc.h

$(B)/fifa: $(OBJ) $(RT) $(HDR)
	$(CC) -O1 -g -o $@ $(OBJ) $(RT)

$(B)/obj/%.o: $(B)/gen/%.c rt/cpu.h rt/runtime.h | $(B)/obj
	$(CC) $(CFLAGS) -c $< -o $@

$(B)/obj:
	mkdir -p $@

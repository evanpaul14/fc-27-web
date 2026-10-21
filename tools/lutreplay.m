// Replays compute 16e714900 (colour grading: 33^3 RGB10A2 LUT -> 32^3) offline, on the translated MSL.
//   cc -O1 -fobjc-arc -o build/lutreplay tools/lutreplay.m -framework Metal -framework Foundation
//   build/lutreplay <msl> <code.bin> <const_off> <const_size> <cb3.bin> <in33.bin> <out32.bin> [dbg.bin]
// msl: `LARECOMP_SAVE_MSL=1 build/shtool -o <dir> build/shaders/16e714900_s5_code.bin`; code.bin: that dump, whose
// compiler constants (cb1) are at <const_off> (control section word 0x700, size at 0x6FC: 6912 / 240); cb3.bin: the
// dispatch's u0 from FIFA_DBG_CSDUMP=16e714900:0; in33: the 33^3 input as FIFA_DBG_PROBE_3D writes it (with
// FIFA_DBG_CSW=16e714900: build/probe3d_33x33x33_90.bin). out32 is bit-identical to the game's. With
// tools/msl_probe.py probes, 64 float4 per thread go to dbg.bin.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdio.h>

static NSData *rd(const char *p) { NSData *d = [NSData dataWithContentsOfFile:@(p)]; if (!d) { fprintf(stderr, "can't read %s\n", p); exit(1); } return d; }

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc < 8) { fprintf(stderr, "usage\n"); return 1; }
        id<MTLDevice> dev = MTLCreateSystemDefaultDevice();
        NSError *err = nil;
        NSString *src = [NSString stringWithContentsOfFile:@(argv[1]) encoding:NSUTF8StringEncoding error:nil];
        id<MTLLibrary> lib = [dev newLibraryWithSource:src options:nil error:&err];
        if (!lib) { fprintf(stderr, "%s\n", err.localizedDescription.UTF8String); return 1; }
        id<MTLComputePipelineState> ps = [dev newComputePipelineStateWithFunction:[lib newFunctionWithName:@"main0"] error:&err];
        if (!ps) { fprintf(stderr, "%s\n", err.localizedDescription.UTF8String); return 1; }
        NSData *code = rd(argv[2]);
        unsigned coff = (unsigned)strtoul(argv[3], NULL, 0), csz = (unsigned)strtoul(argv[4], NULL, 0);
        uint8_t cb1[4096] = {0}, cb2[4096] = {0}, cb3[4096] = {0};
        memcpy(cb1, (const uint8_t *)code.bytes + coff, csz);
        NSData *u = rd(argv[5]);
        memcpy(cb3, u.bytes, MIN(u.length, sizeof cb3));
        NSData *in = rd(argv[6]);
        MTLTextureDescriptor *d = [MTLTextureDescriptor new];
        d.textureType = MTLTextureType3D; d.pixelFormat = MTLPixelFormatRGB10A2Unorm;
        d.width = d.height = d.depth = 33; d.usage = MTLTextureUsageShaderRead;
        id<MTLTexture> t0 = [dev newTextureWithDescriptor:d];
        [t0 replaceRegion:MTLRegionMake3D(0, 0, 0, 33, 33, 33) mipmapLevel:0 slice:0 withBytes:in.bytes bytesPerRow:33 * 4 bytesPerImage:33 * 33 * 4];
        d.width = d.height = d.depth = 32; d.usage = MTLTextureUsageShaderWrite;
        id<MTLTexture> t1 = [dev newTextureWithDescriptor:d];
        MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
        sd.minFilter = sd.magFilter = MTLSamplerMinMagFilterLinear;
        sd.sAddressMode = sd.tAddressMode = sd.rAddressMode = MTLSamplerAddressModeClampToEdge;
        id<MTLSamplerState> s = [dev newSamplerStateWithDescriptor:sd];
        id<MTLBuffer> dbg = [dev newBufferWithLength:32 * 32 * 32 * 64 * 16 options:MTLResourceStorageModeShared];
        id<MTLCommandQueue> q = [dev newCommandQueue];
        id<MTLCommandBuffer> cb = [q commandBuffer];
        id<MTLComputeCommandEncoder> e = [cb computeCommandEncoder];
        [e setComputePipelineState:ps];
        [e setBytes:cb1 length:sizeof cb1 atIndex:1];
        [e setBytes:cb2 length:sizeof cb2 atIndex:2];
        [e setBytes:cb3 length:sizeof cb3 atIndex:3];
        [e setBuffer:dbg offset:0 atIndex:7];
        [e setTexture:t0 atIndex:0];
        [e setTexture:t1 atIndex:1];
        [e setSamplerState:s atIndex:0];
        [e dispatchThreadgroups:MTLSizeMake(1, 16, 32) threadsPerThreadgroup:MTLSizeMake(32, 2, 1)];
        [e endEncoding];
        [cb commit];
        [cb waitUntilCompleted];
        NSMutableData *out = [NSMutableData dataWithLength:32 * 32 * 32 * 4];
        [t1 getBytes:out.mutableBytes bytesPerRow:32 * 4 bytesPerImage:32 * 32 * 4 fromRegion:MTLRegionMake3D(0, 0, 0, 32, 32, 32) mipmapLevel:0 slice:0];
        [out writeToFile:@(argv[7]) atomically:NO];
        if (argc > 8) [[NSData dataWithBytes:dbg.contents length:dbg.length] writeToFile:@(argv[8]) atomically:NO];
        printf("ok\n");
    }
    return 0;
}

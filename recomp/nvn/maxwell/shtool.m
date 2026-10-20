// Offline Maxwell translator check over shader dumps (FIFA_DUMP_SHADERS: build/shaders/<addr>_s<stage>_code.bin).
//   cc -O1 -fobjc-arc -o build/shtool recomp/nvn/maxwell/shtool.m recomp/nvn/maxwell/translate.c -framework Metal -framework Foundation
//   build/shtool [-v] [-c] [-o msl_dir] build/shaders/*_code.bin
// The scan stops at the compiler constants when the control section dump (<addr>_s<stage>_ctrl.bin) gives them.
// Prints one line per shader that fails (all of them with -v), then a summary by error with the opcode
// (top 16 bits) of unknown instructions. -c also compiles the MSL with Metal (there is no offline `metal`
// without Xcode), -o writes the MSL.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "shader.h"

#define MAXERR 512
typedef struct { char key[128]; int n; char example[256]; } Err;
static Err errs[MAXERR];
static int nerr;

static void count(const char *key, const char *file) {
    for (int i = 0; i < nerr; i++)
        if (!strcmp(errs[i].key, key)) { errs[i].n++; return; }
    if (nerr == MAXERR) return;
    snprintf(errs[nerr].key, sizeof errs[nerr].key, "%s", key);
    snprintf(errs[nerr].example, sizeof errs[nerr].example, "%s", file);
    errs[nerr++].n = 1;
}

static int by_count(const void *a, const void *b) { return ((const Err *)b)->n - ((const Err *)a)->n; }

// first "error:" line of a Metal compiler log, without the file prefix
static void first_error(NSString *log, char *out, size_t n) {
    snprintf(out, n, "%s", "?");
    for (NSString *l in [log componentsSeparatedByString:@"\n"]) {
        NSRange r = [l rangeOfString:@"error: "];
        if (r.location == NSNotFound) continue;
        snprintf(out, n, "%s", [[l substringFromIndex:r.location + r.length] UTF8String]);
        return;
    }
}

int main(int argc, char **argv) {
    int verbose = 0, check = 0, ok = 0, total = 0;
    const char *out = NULL;
    int i = 1;
    for (; i < argc && argv[i][0] == '-'; i++) {
        if (!strcmp(argv[i], "-v")) verbose = 1;
        else if (!strcmp(argv[i], "-c")) check = 1;
        else if (!strcmp(argv[i], "-o") && i + 1 < argc) out = argv[++i];
    }
    id<MTLDevice> dev = check ? MTLCreateSystemDefaultDevice() : nil;
    for (; i < argc; i++) @autoreleasepool {
        const char *path = argv[i];
        const char *s = strstr(path, "_s");
        int stage = s ? atoi(s + 2) : STAGE_COMPUTE;
        if (strstr(path, "cs_")) stage = STAGE_COMPUTE;
        FILE *f = fopen(path, "rb");
        if (!f) { perror(path); continue; }
        uint8_t *code = calloc(1, 0x10000 + 0x100);
        size_t n = fread(code, 1, 0x10000, f);
        fclose(f);
        maxwell_shared_size = 0;  // compute: from the control section dump, as the renderer does
        maxwell_consts = NULL;    // compiler constants (BRX jump tables): offset and size from the control section
        uint32_t cs[2] = {0};
        char cpath[1024];
        snprintf(cpath, sizeof cpath, "%s", path);
        char *cc = strstr(cpath, "_code.bin");
        if (cc) {
            memcpy(cc, "_ctrl", 5);
            FILE *cf = fopen(cpath, "rb");
            if (stage == STAGE_COMPUTE && cf && fseek(cf, 0x754, SEEK_SET) == 0 && fread(&maxwell_shared_size, 4, 1, cf) != 1)
                maxwell_shared_size = 0;
            if (cf && fseek(cf, 0x6FC, SEEK_SET) == 0 && fread(cs, 4, 2, cf) == 2 && cs[1] && cs[1] + cs[0] <= n) {
                maxwell_consts = code + cs[1];
                maxwell_consts_size = cs[0];
            }
            if (cf) fclose(cf);
        }
        ShaderKey key = {0};
        // the compiler constants follow the code: they bound the instruction scan, as in the renderer
        ShaderInfo *si = maxwell_translate(code, maxwell_consts ? cs[1] : n, stage, &key);
        total++;
        char why[300], k[128];
        if (si->msl && dev) {
            NSError *err = nil;
            if (![dev newLibraryWithSource:@(si->msl) options:nil error:&err]) {
                first_error(err.localizedDescription, why, sizeof why);
                printf("MSL  %s: %s\n", path, why);
                snprintf(k, sizeof k, "s%d msl: %.100s", stage, why);
                count(k, path);
                goto next;
            }
        }
        if (si->msl) {
            ok++;
            if (verbose) printf("ok   %s (%u insts)\n", path, si->num_insts);
        } else {
            printf("FAIL %s: %s\n", path, si->error);
            const char *u = strstr(si->error, "unknown instruction ");
            if (u) snprintf(k, sizeof k, "s%d unknown %.4s", stage, u + 20);
            else if (strstr(si->error, "bindless tex")) snprintf(k, sizeof k, "s%d bindless handle not from c[2]", stage);
            else snprintf(k, sizeof k, "s%d %.100s", stage, si->error);
            count(k, path);
        }
    next:
        if (si->msl && out) {
            const char *base = strrchr(path, '/');
            char p[1024];
            snprintf(p, sizeof p, "%s/%s.metal", out, base ? base + 1 : path);
            FILE *o = fopen(p, "w");
            if (o) { fputs(si->msl, o); fclose(o); }
        }
        maxwell_free(si);
        free(code);
    }
    qsort(errs, nerr, sizeof errs[0], by_count);
    printf("\n%d/%d translate%s\n", ok, total, check ? " and compile" : "");
    for (int e = 0; e < nerr; e++) printf("%5d  %-40s  e.g. %s\n", errs[e].n, errs[e].key, errs[e].example);
    return 0;
}

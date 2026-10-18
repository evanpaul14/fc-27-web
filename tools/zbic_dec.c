/* ZBIC (Switch 22.0+ zstd variant) block decompressor for NSO segments.
 * usage: zbic_dec IN OUT SIZE
 * Built against kinnay/zbic's zstd.c by tools/build_zbic.sh. */
#define ZSTD_ZBIC_SUPPORT 1
#include <stdio.h>
#include <stdlib.h>
#include "zstd.h"
#include "zstd.c"

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: zbic_dec IN OUT SIZE\n");
        return 2;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror(argv[1]); return 1; }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    void *src = malloc(n);
    if (fread(src, 1, n, f) != (size_t)n) { perror("read"); return 1; }
    fclose(f);
    size_t size = strtoull(argv[3], NULL, 0);
    void *dst = malloc(size);
    size_t r = ZSTD_decompress(dst, size, src, n);
    if (ZSTD_isError(r)) {
        fprintf(stderr, "zbic: %s\n", ZSTD_getErrorName(r));
        return 1;
    }
    if (r != size) {
        fprintf(stderr, "zbic: got %zu bytes, expected %zu\n", r, size);
        return 1;
    }
    f = fopen(argv[2], "wb");
    if (!f) { perror(argv[2]); return 1; }
    fwrite(dst, 1, size, f);
    fclose(f);
    return 0;
}

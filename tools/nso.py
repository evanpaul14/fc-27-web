"""Decompress an NSO into a flat memory image and dump its module metadata.

usage: python3 -I tools/nso.py exefs/main OUT_PREFIX
writes OUT_PREFIX.bin (text|ro|data laid out at their memory offsets, bss excluded)
and OUT_PREFIX.json (segments, build id, MOD0/dynamic info).
"""
import json, os, struct, subprocess, sys, tempfile

ZBIC_DEC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'zbic_dec')


def zbic_block(src, out_size):
    """Decompress via tools/bin/zbic_dec (build with tools/build_zbic.sh)."""
    with tempfile.TemporaryDirectory() as t:
        i, o = os.path.join(t, 'in'), os.path.join(t, 'out')
        open(i, 'wb').write(src)
        subprocess.run([ZBIC_DEC, i, o, str(out_size)], check=True)
        return open(o, 'rb').read()


def lz4_block(src, out_size):
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]; i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]; i += lit
        if i >= n:
            break
        off = src[i] | src[i + 1] << 8; i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]; i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(out) - off
        if off >= ml:
            out += out[start:start + ml]
        else:
            chunk = out[start:]
            out += (chunk * (ml // off + 1))[:ml]
    assert len(out) == out_size, (len(out), out_size)
    return bytes(out)


def main():
    path, prefix = sys.argv[1], sys.argv[2]
    d = open(path, 'rb').read()
    assert d[:4] == b'NSO0'
    flags = struct.unpack_from('<I', d, 0xC)[0]
    segs = []
    for k, name in enumerate(('text', 'ro', 'data')):
        foff, moff, size, extra = struct.unpack_from('<IIII', d, 0x10 + k * 0x10)
        csize = struct.unpack_from('<I', d, 0x60 + k * 4)[0]
        segs.append(dict(name=name, file_off=foff, mem_off=moff, size=size, csize=csize,
                         compressed=bool(flags >> k & 1), extra=extra))
    build_id = d[0x40:0x60].hex()
    img = bytearray(segs[2]['mem_off'] + segs[2]['size'])
    for s in segs:
        raw = d[s['file_off']:s['file_off'] + s['csize']]
        if not s['compressed']:
            data = raw
        elif flags & 0x80:  # ZBIC (zstd variant, 22.0.0+)
            data = zbic_block(raw, s['size'])
        else:
            data = lz4_block(raw, s['size'])
        img[s['mem_off']:s['mem_off'] + s['size']] = data
    bss = segs[2]['extra']
    info = dict(segments=segs, bss_size=bss, build_id=build_id, image_size=len(img))
    # MOD0 header: text+4 holds the offset of MOD0 relative to the image base.
    mod0 = struct.unpack_from('<I', img, 4)[0]
    if img[mod0:mod0 + 4] == b'MOD0':
        dyn, bss_s, bss_e, eh_s, eh_e, mobj = struct.unpack_from('<6i', img, mod0 + 4)
        info['mod0'] = dict(off=mod0, dynamic=mod0 + dyn, bss_start=mod0 + bss_s, bss_end=mod0 + bss_e,
                            eh_frame_hdr=(mod0 + eh_s, mod0 + eh_e), module_object=mod0 + mobj)
        # Walk .dynamic for the symbol/string tables.
        tags = {1: 'NEEDED', 5: 'STRTAB', 6: 'SYMTAB', 10: 'STRSZ', 7: 'RELA', 8: 'RELASZ',
                0x17: 'JMPREL', 2: 'PLTRELSZ', 0x6ffffef5: 'GNU_HASH', 4: 'HASH', 12: 'INIT', 25: 'INIT_ARRAY',
                27: 'INIT_ARRAYSZ'}
        p, dynd = info['mod0']['dynamic'], {}
        while True:
            t, v = struct.unpack_from('<qQ', img, p); p += 16
            if t == 0:
                break
            if t in tags:
                dynd.setdefault(tags[t], []).append(v)
        info['dynamic'] = {k: v if len(v) > 1 else v[0] for k, v in dynd.items()}
        if 'STRTAB' in dynd and 'SYMTAB' in dynd:
            strtab, symtab = dynd['STRTAB'][0], dynd['SYMTAB'][0]
            nsyms = (strtab - symtab) // 24 if strtab > symtab else 0
            syms = []
            for k in range(nsyms):
                nm, inf, oth, shn, val, sz = struct.unpack_from('<IBBHQQ', img, symtab + k * 24)
                e = img.index(b'\0', strtab + nm)
                syms.append((img[strtab + nm:e].decode('utf-8', 'replace'), val, sz, shn, inf))
            info['nsyms'] = len(syms)
            info['nsyms_defined'] = sum(1 for s in syms if s[3] != 0)
            info['needed'] = [img[strtab + o:img.index(b'\0', strtab + o)].decode() for o in dynd.get('NEEDED', [])]
            with open(prefix + '.syms.tsv', 'w') as o:
                for s in syms:
                    o.write('%s\t%#x\t%d\t%d\t%d\n' % s)
    open(prefix + '.bin', 'wb').write(img)
    json.dump(info, open(prefix + '.json', 'w'), indent=1)
    print(json.dumps({k: v for k, v in info.items() if k != 'segments'}, indent=1))
    for s in segs:
        print(s)


if __name__ == '__main__':
    main()

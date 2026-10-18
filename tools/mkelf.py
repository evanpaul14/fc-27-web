"""Wrap a relocated NSO image as an AArch64 ELF (for objdump / Ghidra).

Sections .text/.rodata/.data/.bss at their image offsets (base 0), relocations
already applied (imports point at fake addresses in an .imports section), and a
.symtab with exports, imports, discovered functions (sub_XXXX) and reflection
records (ti_<Name>).

usage: python3 -I tools/mkelf.py extracted/code/main extracted/analysis/main OUT.elf
"""
import json, os, struct, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import IMPORT_BASE  # noqa: E402


def main():
    prefix, ana, out = sys.argv[1:4]
    info = json.load(open(prefix + '.json'))
    img = bytearray(open(prefix + '.bin', 'rb').read())
    for a, v, _ in np.load(os.path.join(ana, 'relocs.npy')).tolist():
        struct.pack_into('<Q', img, a, v & (2**64 - 1))
    segs = {s['name']: s for s in info['segments']}
    t, r, d = segs['text'], segs['ro'], segs['data']
    bss_addr, bss_size = d['mem_off'] + d['size'], info['bss_size']

    syms = []  # (name, addr, size, is_func, shndx)
    imports = [l.rstrip('\n').split('\t') for l in open(os.path.join(ana, 'imports.tsv'))]
    nimp = int(imports[-1][0]) + 1 if imports else 0
    for si, addr, nm in imports:
        syms.append((nm, int(addr, 16), 16, True, 5))
    for l in open(os.path.join(ana, 'functions.tsv')):
        a, size, src, nm = l.rstrip('\n').split('\t')
        a = int(a, 16)
        syms.append((nm or f'sub_{a:x}', a, int(size), True, 1))
    tpath = os.path.join(ana, 'types.tsv')
    if os.path.exists(tpath):
        for l in list(open(tpath))[1:]:
            f = l.rstrip('\n').split('\t')
            nm = f[2].replace('::', '__')
            syms.append((f'ti_{nm}', int(f[0], 16), 0x48, False, 3))
            syms.append((f'rt_{nm}', int(f[1], 16), 8, False, 4))

    strtab = bytearray(b'\0')
    symtab = bytearray(24)  # null symbol
    for nm, a, size, fn, shn in syms:
        no = len(strtab)
        strtab += nm.encode() + b'\0'
        symtab += struct.pack('<IBBHQQ', no, 0x10 | (2 if fn else 1), 0, shn, a, size)

    shstr = bytearray(b'\0')

    def sname(s):
        o = len(shstr); shstr.extend(s.encode() + b'\0'); return o

    # Layout: ELF header, then the image verbatim, then tables.
    EH = 0x40
    body_off = 0x1000
    blob = bytearray(body_off) + img
    imp_size = nimp * 16

    def add(b):
        nonlocal blob
        while len(blob) % 8:
            blob.append(0)
        o = len(blob); blob += b; return o

    symo, stro = add(symtab), add(strtab)
    names = ['', '.text', '.rodata', '.data', '.bss', '.imports', '.symtab', '.strtab', '.shstrtab']
    nidx = [sname(n) for n in names]
    shstro = add(shstr)
    # (name, type, flags, addr, off, size, link, info, align, entsize)
    sh = [
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (nidx[1], 1, 6, t['mem_off'], body_off + t['mem_off'], t['size'], 0, 0, 16, 0),
        (nidx[2], 1, 2, r['mem_off'], body_off + r['mem_off'], r['size'], 0, 0, 16, 0),
        (nidx[3], 1, 3, d['mem_off'], body_off + d['mem_off'], d['size'], 0, 0, 16, 0),
        (nidx[4], 8, 3, bss_addr, 0, bss_size, 0, 0, 16, 0),
        (nidx[5], 8, 3, IMPORT_BASE, 0, imp_size, 0, 0, 16, 0),
        (nidx[6], 2, 0, 0, symo, len(symtab), 7, 1, 8, 24),
        (nidx[7], 3, 0, 0, stro, len(strtab), 0, 0, 1, 0),
        (nidx[8], 3, 0, 0, shstro, len(shstr), 0, 0, 1, 0),
    ]
    while len(blob) % 8:
        blob.append(0)
    sho = len(blob)
    for s in sh:
        blob += struct.pack('<IIQQQQIIQQ', *s)
    # Program headers: RX text, R ro, RW data+bss.
    ph = [
        (1, 5, body_off + t['mem_off'], t['mem_off'], t['size'], t['size']),
        (1, 4, body_off + r['mem_off'], r['mem_off'], r['size'], r['size']),
        (1, 6, body_off + d['mem_off'], d['mem_off'], d['size'], d['size'] + bss_size),
    ]
    pho = EH
    hdr = bytearray(b'\x7fELF\x02\x01\x01' + bytes(9))
    hdr += struct.pack('<HHIQQQIHHHHHH', 3, 183, 1, 0, pho, sho, 0, EH, 56, len(ph), 64, len(sh), len(sh) - 1)
    for typ, fl, off, va, fs, ms in ph:
        hdr += struct.pack('<IIQQQQQQ', typ, fl, off, va, va, fs, ms, 0x1000)
    blob[:len(hdr)] = hdr
    open(out, 'wb').write(blob)
    print(f'{out}: {len(blob):,} bytes, {len(syms):,} symbols')


if __name__ == '__main__':
    main()

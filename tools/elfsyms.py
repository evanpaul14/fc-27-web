"""Dump the static symbol table of an ELF (e.g. the .nrs that ships with an NRO)
in the .syms.tsv format of tools/nso.py: name, value, size, shndx, info.

recomp/image.py adds these names to symbols.tsv, and FUNC symbols become
function starts for recomp/build.py.

usage: python3 -I tools/elfsyms.py Engine.Render.Core2.PlatformNvn.nrs extracted/code/nro.elfsyms.tsv
"""
import struct, sys


def main():
    path, out = sys.argv[1], sys.argv[2]
    d = open(path, 'rb').read()
    assert d[:4] == b'\x7fELF' and d[4] == 2, 'ELF64 only'
    shoff, = struct.unpack_from('<Q', d, 0x28)
    shentsize, shnum = struct.unpack_from('<HH', d, 0x3A)
    secs = [struct.unpack_from('<IIQQQQIIQQ', d, shoff + k * shentsize) for k in range(shnum)]
    n = 0
    with open(out, 'w') as o:
        for name, typ, flags, addr, off, size, link, info, align, entsize in secs:
            if typ != 2:  # SHT_SYMTAB
                continue
            stroff = secs[link][4]
            for k in range(size // 24):
                nm, inf, oth, shn, val, sz = struct.unpack_from('<IBBHQQ', d, off + k * 24)
                e = d.index(b'\0', stroff + nm)
                s = d[stroff + nm:e].decode('utf-8', 'replace')
                if s and not s.startswith('$'):
                    o.write('%s\t%#x\t%d\t%d\t%d\n' % (s, val, sz, shn, inf))
                    n += 1
    print(f'{n} symbols')


if __name__ == '__main__':
    main()

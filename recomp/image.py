"""Lay out and relocate the program's modules in one guest address space.

Modules (decompressed by tools/nso.py) are placed at fixed bases, symbols are
resolved across them in load order (main first, like rtld), and relocations
are applied. Undefined weak symbols resolve to 0; other unresolved symbols
get a 16-byte stub slot in a reserved page so calls and data reads land
somewhere identifiable.

Writes OUT/<module>.img (image incl. bss), OUT/layout.json and
OUT/symbols.tsv (address, module, name, size, type).

usage: python3 -I recomp/image.py extracted/code OUT
"""
import json, os, struct, sys

MODULES = [('main', 0x08000000), ('sdk', 0x18000000)]
MISSING_BASE = 0x07F00000  # stub slots for unresolved symbols, 16 bytes each
PAGE = 0x1000

R_ABS64, R_GLOB_DAT, R_JUMP_SLOT, R_RELATIVE = 257, 1025, 1026, 1027


def load(code_dir, name, base):
    prefix = os.path.join(code_dir, name)
    info = json.load(open(prefix + '.json'))
    raw = open(prefix + '.bin', 'rb').read()
    size = (info['mod0']['bss_end'] + PAGE - 1) & ~(PAGE - 1)
    img = bytearray(size)
    img[:len(raw)] = raw
    syms = []
    for line in open(prefix + '.syms.tsv'):
        nm, val, sz, shn, inf = line.rstrip('\n').split('\t')
        syms.append((nm, int(val, 16), int(sz), int(shn), int(inf)))
    dyn, p = {}, info['mod0']['dynamic']
    while True:
        t, v = struct.unpack_from('<qQ', img, p)
        p += 16
        if t == 0:
            break
        dyn.setdefault(t, v)
    return {'name': name, 'base': base, 'info': info, 'img': img, 'syms': syms, 'dyn': dyn, 'size': size}


def main():
    code_dir, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    mods = [load(code_dir, n, b) for n, b in MODULES]

    exports = {}
    for m in mods:
        for nm, val, sz, shn, inf in m['syms']:
            if nm and shn != 0 and nm not in exports:
                exports[nm] = m['base'] + val
    missing = {}

    def resolve(m, si):
        nm, val, sz, shn, inf = m['syms'][si]
        if shn != 0:
            return m['base'] + val
        if nm in exports:
            return exports[nm]
        if inf >> 4 == 2:  # weak undefined
            return 0
        if nm not in missing:
            missing[nm] = MISSING_BASE + 16 * len(missing)
        return missing[nm]

    for m in mods:
        img, dyn, base = m['img'], m['dyn'], m['base']
        n_rel = 0

        def rela(off, size):
            nonlocal n_rel
            for k in range(size // 24):
                o, inf, add = struct.unpack_from('<QQq', img, off + 24 * k)
                typ, si = inf & 0xFFFFFFFF, inf >> 32
                if typ == R_RELATIVE:
                    v = base + add
                elif typ in (R_ABS64, R_GLOB_DAT, R_JUMP_SLOT):
                    v = resolve(m, si) + add
                else:
                    raise ValueError(f'{m["name"]}: reloc type {typ} at {o:#x}')
                struct.pack_into('<Q', img, o, v & (2**64 - 1))
                n_rel += 1
        rela(dyn[7], dyn[8])
        if 0x17 in dyn:
            rela(dyn[0x17], dyn[2])
        if 0x24 in dyn:  # DT_RELR
            where = 0
            for k in range(dyn[0x23] // 8):
                e = struct.unpack_from('<Q', img, dyn[0x24] + 8 * k)[0]
                if e & 1 == 0:
                    struct.pack_into('<Q', img, e, struct.unpack_from('<Q', img, e)[0] + base)
                    where = e + 8
                    n_rel += 1
                else:
                    b, j = e >> 1, 0
                    while b:
                        if b & 1:
                            a = where + 8 * j
                            struct.pack_into('<Q', img, a, struct.unpack_from('<Q', img, a)[0] + base)
                            n_rel += 1
                        b >>= 1
                        j += 1
                    where += 8 * 63
        open(os.path.join(out, m['name'] + '.img'), 'wb').write(img)
        print(f'{m["name"]}: base {base:#x} size {m["size"]:#x}, {n_rel} relocations')

    def arr(m, tag_addr, tag_size):
        d = m['dyn']
        if tag_addr not in d:
            return []
        return [struct.unpack_from('<Q', m['img'], d[tag_addr] + 8 * k)[0] for k in range(d.get(tag_size, 0) // 8)]

    layout = {'modules': [], 'missing': {nm: a for nm, a in missing.items()}}
    for m in mods:
        segs = [{'name': s['name'], 'addr': m['base'] + s['mem_off'], 'size': s['size']} for s in m['info']['segments']]
        mod = {'name': m['name'], 'base': m['base'], 'size': m['size'], 'segments': segs,
               'init': m['base'] + m['dyn'][12] if 12 in m['dyn'] else None,
               'init_array': arr(m, 25, 27), 'fini_array': arr(m, 26, 28)}
        layout['modules'].append(mod)
    json.dump(layout, open(os.path.join(out, 'layout.json'), 'w'), indent=1)
    with open(os.path.join(out, 'symbols.tsv'), 'w') as f:
        for m in mods:
            for nm, val, sz, shn, inf in m['syms']:
                if nm and shn != 0:
                    f.write(f'{m["base"] + val:#x}\t{m["name"]}\t{nm}\t{sz}\t{inf & 15}\n')
    print(f'{len(missing)} unresolved: {", ".join(list(missing)[:8])}{" ..." if len(missing) > 8 else ""}')


if __name__ == '__main__':
    main()

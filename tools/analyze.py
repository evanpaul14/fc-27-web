"""First-pass static analysis of a decompressed NSO image (from tools/nso.py).

Applies relocations, discovers function starts and vtables, and writes:
  OUT/relocs.npy     pointer slots: (slot, value, kind) kind 0=relative 1=import
  OUT/imports.tsv    import index, fake address, symbol
  OUT/functions.tsv  start, size, sources (b=bl target, p=data pointer, v=vtable,
                     e=eh_frame, i=init_array, x=export), name
  OUT/vtables.tsv    address point, n slots, slot targets
  OUT/stats.json

usage: python3 -I tools/analyze.py extracted/code/main extracted/analysis/main
"""
import json, os, struct, sys
import numpy as np

IMPORT_BASE = 0x1_0000_0000  # fake addresses for imported symbols, 16 bytes apart

R_ABS64, R_GLOB_DAT, R_JUMP_SLOT, R_RELATIVE = 257, 1025, 1026, 1027


def main():
    prefix, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    info = json.load(open(prefix + '.json'))
    img = bytearray(open(prefix + '.bin', 'rb').read())
    segs = {s['name']: s for s in info['segments']}
    text_end = segs['text']['mem_off'] + segs['text']['size']
    ro = segs['ro']
    data = segs['data']
    image_end = data['mem_off'] + data['size'] + info['bss_size']
    syms = [l.rstrip('\n').split('\t') for l in open(prefix + '.syms.tsv')]

    # Read raw dynamic tags (nso.py keeps only a subset).
    dyn, p = {}, info['mod0']['dynamic']
    while True:
        t, v = struct.unpack_from('<qQ', img, p); p += 16
        if t == 0:
            break
        dyn.setdefault(t, v)

    slots = []  # (slot, value, kind)

    def rela(off, size):
        for k in range(size // 24):
            o, inf, add = struct.unpack_from('<QQq', img, off + 24 * k)
            typ, si = inf & 0xFFFFFFFF, inf >> 32
            if typ == R_RELATIVE:
                v, kind = add, 0
            elif typ in (R_ABS64, R_GLOB_DAT, R_JUMP_SLOT):
                if syms[si][3] != '0':  # defined in this module
                    v, kind = int(syms[si][1], 16) + add, 0
                else:
                    v, kind = IMPORT_BASE + si * 16 + add, 1
            else:
                raise ValueError(f'reloc type {typ} at {o:#x}')
            struct.pack_into('<Q', img, o, v & (2**64 - 1))
            slots.append((o, v, kind))

    rela(dyn[7], dyn[8])
    if 0x17 in dyn:
        rela(dyn[0x17], dyn[2])
    # RELR: relative relocations, addend stored in place; base is 0 so values stay.
    if 0x24 in dyn:
        relr = np.frombuffer(img, '<u8', dyn[0x23] // 8, dyn[0x24])
        where = 0
        for e in relr.tolist():
            if e & 1 == 0:
                slots.append((e, struct.unpack_from('<Q', img, e)[0], 0))
                where = e + 8
            else:
                bits, k = e >> 1, 0
                while bits:
                    if bits & 1:
                        a = where + 8 * k
                        slots.append((a, struct.unpack_from('<Q', img, a)[0], 0))
                    bits >>= 1; k += 1
                where += 8 * 63
    slots.sort()
    sl = np.array(slots, dtype=np.int64)
    np.save(os.path.join(out, 'relocs.npy'), sl)

    with open(os.path.join(out, 'imports.tsv'), 'w') as o:
        for si, s in enumerate(syms):
            if s[0] and s[3] == '0':
                o.write(f'{si}\t{IMPORT_BASE + si * 16:#x}\t{s[0]}\n')

    # --- function starts
    src = {}

    def mark(a, c):
        if 0 <= a < text_end and a % 4 == 0:
            src[a] = src.get(a, '') + ('' if c in src.get(a, '') else c)

    words = np.frombuffer(img, '<u4', text_end // 4, 0)
    idx = np.arange(len(words), dtype=np.int64)
    bl = (words & 0xFC000000) == 0x94000000
    imm = (words[bl] & 0x03FFFFFF).astype(np.int64)
    imm = np.where(imm & 0x02000000, imm - 0x04000000, imm)
    tgt = idx[bl] * 4 + imm * 4
    bl_targets, bl_counts = np.unique(tgt[(tgt >= 0) & (tgt < text_end)], return_counts=True)
    for a in bl_targets.tolist():
        mark(a, 'b')

    text_ptr = (sl[:, 1] >= 0) & (sl[:, 1] < text_end) & (sl[:, 2] == 0)
    for a in sl[text_ptr, 1].tolist():
        mark(a, 'p')

    # eh_frame_hdr binary search table (datarel sdata4 pairs).
    eh0, eh1 = info['mod0']['eh_frame_hdr']
    ver, ptr_enc, cnt_enc, tbl_enc = img[eh0:eh0 + 4]
    nfde = 0
    if ver == 1 and tbl_enc == 0x3B and cnt_enc == 0x03:
        nfde = struct.unpack_from('<I', img, eh0 + 8)[0]
        for k in range(nfde):
            loc, _ = struct.unpack_from('<ii', img, eh0 + 12 + 8 * k)
            mark(eh0 + loc, 'e')

    for k in range(dyn[0x1B] // 8):
        mark(struct.unpack_from('<Q', img, dyn[0x19] + 8 * k)[0], 'i')
    names = {}
    for s in syms:
        if s[0] and s[3] != '0' and int(s[4]) & 0xF == 2:
            a = int(s[1], 16)
            mark(a, 'x')
            names[a] = s[0]

    # --- vtables: runs of >=2 consecutive relocated slots pointing into text (or at
    # __cxa_pure_virtual), whose run start is preceded by two unrelocated zero qwords
    # (offset-to-top and the RTTI pointer, which is null with -fno-rtti).
    pure = next((IMPORT_BASE + i * 16 for i, s in enumerate(syms) if s[0] == '__cxa_pure_virtual'), None)
    slotset = {}
    for a, v, kind in slots:
        slotset[a] = v
    fnlike = lambda v: (0 <= v < text_end) or v == pure
    vtables = []
    a_sorted = sl[:, 0].tolist()
    k = 0
    n = len(a_sorted)
    while k < n:
        a = a_sorted[k]
        if not fnlike(slotset[a]) or a < ro['mem_off']:
            k += 1
            continue
        j = k
        while j + 1 < n and a_sorted[j + 1] == a_sorted[j] + 8 and fnlike(slotset[a_sorted[j + 1]]):
            j += 1
        cnt = j - k + 1
        pre0 = a - 16
        if (cnt >= 2 and pre0 not in slotset and pre0 + 8 not in slotset
                and struct.unpack_from('<qQ', img, pre0) == (0, 0)):
            vtables.append((a, [slotset[a_sorted[q]] for q in range(k, j + 1)]))
        k = j + 1
    vslots = 0
    with open(os.path.join(out, 'vtables.tsv'), 'w') as o:
        for a, fns in vtables:
            for f in fns:
                if f != pure:
                    mark(f, 'v')
            vslots += len(fns)
            o.write(f'{a:#x}\t{len(fns)}\t' + ','.join('pure' if f == pure else f'{f:#x}' for f in fns) + '\n')

    starts = sorted(src)
    with open(os.path.join(out, 'functions.tsv'), 'w') as o:
        for q, a in enumerate(starts):
            end = starts[q + 1] if q + 1 < len(starts) else text_end
            o.write(f'{a:#x}\t{end - a}\t{src[a]}\t{names.get(a, "")}\n')

    # Import call counts through PLT stubs aren't direct; record BL fan-in instead.
    hot = sorted(zip(bl_counts.tolist(), bl_targets.tolist()), reverse=True)[:40]
    stats = dict(
        text_size=text_end, n_slots=len(slots), n_import_slots=int((sl[:, 2] == 1).sum()),
        n_text_ptrs=int(text_ptr.sum()), n_bl=int(bl.sum()), n_bl_targets=len(bl_targets),
        n_eh_fde=nfde, n_init=dyn[0x1B] // 8, n_vtables=len(vtables), n_vtable_slots=vslots,
        n_functions=len(starts),
        by_source={c: sum(1 for v in src.values() if c in v) for c in 'bpveix'},
        only_p=sum(1 for v in src.values() if v == 'p'),
        hottest_bl_targets=[(f'{t:#x}', c) for c, t in hot],
    )
    json.dump(stats, open(os.path.join(out, 'stats.json'), 'w'), indent=1)
    print(json.dumps(stats, indent=1))


if __name__ == '__main__':
    main()

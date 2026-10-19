"""Extract the ExeFS (code) of an NSP's Program NCA without unpacking the whole NSP.

Reads NCAs in place from the NSP, decrypts only the byte ranges needed, and lists
the RomFS tree (without extracting it) so assets can be pulled selectively later.

usage: python3 -I tools/nsp_extract.py GAME.nsp OUTDIR [--keys ~/.switch/prod.keys]
       python3 -I tools/nsp_extract.py UPDATE.nsp OUTDIR --base GAME.nsp   (patched RomFS)
"""
import argparse, bisect, json, os, struct, sys
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MEDIA = 0x200


def load_keys(path):
    keys = {}
    for line in open(os.path.expanduser(path)):
        if '=' in line:
            k, v = line.split('=', 1)
            keys[k.strip()] = bytes.fromhex(v.strip())
    return keys


def aes_ecb_dec(key, data):
    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return d.update(data) + d.finalize()


def xts_dec(key, data, sector0, sector_size=MEDIA):
    out = bytearray()
    for i in range(0, len(data), sector_size):
        # Nintendo uses a big-endian sector number as the tweak.
        tweak = (sector0 + i // sector_size).to_bytes(16, 'big')
        d = Cipher(algorithms.AES(key), modes.XTS(tweak)).decryptor()
        out += d.update(data[i:i + sector_size]) + d.finalize()
    return bytes(out)


def read_pfs0(f, base):
    f.seek(base)
    magic, n, strsz, _ = struct.unpack('<4sIII', f.read(16))
    assert magic == b'PFS0', magic
    ents = [struct.unpack('<QQII', f.read(24)) for _ in range(n)]
    strs = f.read(strsz)
    data = base + 16 + 24 * n + strsz
    return [(strs[no:strs.index(b'\0', no)].decode(), data + off, size) for off, size, no, _ in ents]


class Section:
    """One CTR-encrypted NCA section, readable at section-relative offsets."""

    def __init__(self, f, nca_base, start, end, fsh, key):
        self.f, self.nca_base, self.start, self.end = f, nca_base, start, end
        self.fsh, self.key = fsh, key
        self.enc = fsh[4]
        self.ctr_hi = fsh[0x140:0x148][::-1]

    def read(self, off, size):
        abs_off = self.start + off  # offset within the NCA
        if self.enc == 1:
            self.f.seek(self.nca_base + abs_off)
            return self.f.read(size)
        assert self.enc == 3, f'unsupported encryption type {self.enc}'
        a = abs_off & ~0xF
        pad = abs_off - a
        self.f.seek(self.nca_base + a)
        raw = self.f.read((pad + size + 0xF) & ~0xF)
        iv = self.ctr_hi + (a >> 4).to_bytes(8, 'big')
        d = Cipher(algorithms.AES(self.key), modes.CTR(iv)).decryptor()
        return (d.update(raw) + d.finalize())[pad:pad + size]


class Nca:
    def __init__(self, f, base, keys, title_key_enc=None):
        self.f, self.base = f, base
        f.seek(base)
        hdr = xts_dec(keys['header_key'], f.read(0xC00), 0)
        assert hdr[0x200:0x204] == b'NCA3', hdr[0x200:0x204]
        self.hdr = hdr
        self.content_type = hdr[0x205]
        keygen = max(hdr[0x206], hdr[0x220])
        mk = max(keygen - 1, 0)
        self.keygen = keygen
        self.title_id = struct.unpack_from('<Q', hdr, 0x210)[0]
        rights = hdr[0x230:0x240]
        if any(rights):
            assert title_key_enc, 'NCA has a rights ID but no ticket title key was given'
            key = aes_ecb_dec(keys[f'titlekek_{mk:02x}'], title_key_enc)
        else:
            kak = keys[f'key_area_key_application_{mk:02x}']
            key = aes_ecb_dec(kak, hdr[0x300:0x340])[0x20:0x30]  # CTR key slot
        self.sections = []
        for i in range(4):
            s, e = struct.unpack_from('<II', hdr, 0x240 + i * 0x10)
            if s == e == 0:
                continue
            fsh = hdr[0x400 + i * 0x200:0x600 + i * 0x200]
            self.sections.append((i, Section(f, base, s * MEDIA, e * MEDIA, fsh, key)))


def section_info(sec):
    fsh = sec.fsh
    return {
        'fs_type': ['romfs', 'pfs0'][fsh[2]] if fsh[2] < 2 else fsh[2],
        'hash_type': fsh[3], 'encryption': fsh[4],
        'size': sec.end - sec.start,
        'has_sparse': any(fsh[0x148:0x178]),
        'has_compression': any(fsh[0x178:0x1A0]),
    }


def section_pfs0(sec):
    master, bsz, nlayers, ht_off, ht_size, p_off, p_size = struct.unpack_from('<32sIIQQQQ', sec.fsh, 8)
    hdr = sec.read(p_off, 16)
    _, n, strsz, _ = struct.unpack('<4sIII', hdr)
    blob = sec.read(p_off, 16 + 24 * n + strsz)
    assert blob[:4] == b'PFS0'
    strs = blob[16 + 24 * n:]
    data = p_off + 16 + 24 * n + strsz
    out = []
    for i in range(n):
        off, size, no, _ = struct.unpack_from('<QQII', blob, 16 + 24 * i)
        out.append((strs[no:strs.index(b'\0', no)].decode(), data + off, size))
    return out


def romfs_level(sec):
    """(offset, size) of the RomFS image in the section: the last of 6 IVFC levels."""
    assert sec.fsh[8:12] == b'IVFC', sec.fsh[8:12]
    return struct.unpack_from('<QQ', sec.fsh, 8 + 0x10 + 5 * 0x18)


def romfs_storage(nsp, sec):
    """Where the host (recomp/rt/fs.c) finds the RomFS image: file offset of
    the image in the NSP, and AES-128-CTR parameters. The counter for a byte
    at NSP offset o is ctr_hi (big endian, high 64 bits) followed by
    (o - nca_offset) >> 4."""
    lvl_off, lvl_size = romfs_level(sec)
    return {'nsp': os.path.abspath(nsp), 'nca_offset': sec.nca_base,
            'offset': sec.nca_base + sec.start + lvl_off, 'size': lvl_size,
            'encryption': sec.enc, 'key': sec.key.hex(), 'ctr_hi': sec.ctr_hi.hex()}


def bucket_tree(sec, hdr_off, esize):
    """Entries of a BKTR bucket tree (one offset node, then 0x4000-byte entry
    sets) whose header is at hdr_off in the section header. The tables sit at
    the end of the section under the section's own counter."""
    off, size = struct.unpack_from('<QQ', sec.fsh, hdr_off)
    magic, _, count = struct.unpack_from('<4sII', sec.fsh, hdr_off + 0x10)
    assert magic == b'BKTR', magic
    sec.enc, enc = 3, sec.enc
    blob = sec.read(off, size)
    sec.enc = enc
    nsets, total = struct.unpack_from('<IQ', blob, 4)
    ents = []
    for s in range(nsets):
        b = 0x4000 * (s + 1)
        n = struct.unpack_from('<I', blob, b + 4)[0]
        ents += [blob[b + 0x10 + i * esize:b + 0x10 + (i + 1) * esize] for i in range(n)]
    assert len(ents) == count, (len(ents), count)
    return ents, total


class PatchedSection:
    """An update's RomFS section (AES-CTR-Ex, encryption 4) laid over the base
    game's: the relocation table maps virtual offsets to the base section or to
    patch data, and the subsection table gives the patch data's counters."""

    def __init__(self, sec, base):
        self.sec, self.base, self.fsh = sec, base, sec.fsh
        rel, self.size = bucket_tree(sec, 0x100, 0x14)
        self.rel = [struct.unpack('<QQI', e) for e in rel]
        sub, _ = bucket_tree(sec, 0x120, 0x10)
        self.sub = [struct.unpack('<QII', e) for e in sub]
        self.patch_end = struct.unpack_from('<Q', sec.fsh, 0x100)[0]  # relocation table offset

    def _patch(self, off, size):
        out = bytearray()
        while size:
            i = bisect.bisect_right(self.sub, (off, 1 << 32, 0)) - 1
            end = self.sub[i + 1][0] if i + 1 < len(self.sub) else self.patch_end
            n = min(size, end - off)
            self.sec.ctr_hi = self.sec.fsh[0x140:0x148][::-1][:4] + self.sub[i][2].to_bytes(4, 'big')
            self.sec.enc = 3
            out += self.sec.read(off, n)
            off, size = off + n, size - n
        return bytes(out)

    def read(self, off, size):
        out = bytearray()
        while size:
            i = bisect.bisect_right(self.rel, (off, 1 << 64, 0)) - 1
            v, p, is_patch = self.rel[i]
            end = self.rel[i + 1][0] if i + 1 < len(self.rel) else self.size
            n = min(size, end - off)
            out += (self._patch if is_patch else self.base.read)(p + off - v, n)
            off, size = off + n, size - n
        return bytes(out)

    def storage(self, nsp, base_nsp):
        """romfs_storage for fs.c, plus the base section and the two tables
        (written next to the JSON as <tag>.bktr)."""
        lvl_off, lvl_size = romfs_level(self.sec)
        b = self.base
        return {'nsp': os.path.abspath(nsp), 'nca_offset': self.sec.nca_base, 'section': self.sec.start,
                'image': lvl_off, 'size': lvl_size, 'patch_end': self.patch_end,
                'key': self.sec.key.hex(), 'ctr_hi': self.sec.fsh[0x140:0x148][::-1].hex(),
                'base_nsp': os.path.abspath(base_nsp), 'base_nca_offset': b.nca_base, 'base_section': b.start,
                'base_key': b.key.hex(), 'base_ctr_hi': b.ctr_hi.hex(),
                'relocations': len(self.rel), 'subsections': len(self.sub)}

    def tables(self):
        """Relocations as (u64 virtual, u64 physical, u32 is_patch, u32 0), then
        subsections as (u64 offset, u32 counter, u32 0)."""
        return (b''.join(struct.pack('<QQII', v, p, i, 0) for v, p, i in self.rel) +
                b''.join(struct.pack('<QII', o, c, 0) for o, _, c in self.sub))


def base_program_romfs(path, keys):
    f = open(path, 'rb')
    ents = read_pfs0(f, 0)
    tik = next((e for e in ents if e[0].endswith('.tik')), None)
    title_key = None
    if tik:
        f.seek(tik[1] + 0x180)
        title_key = f.read(16)
    for name, off, _ in ents:
        if name.endswith('.nca'):
            nca = Nca(f, off, keys, title_key)
            if nca.content_type == 0:
                return next(s for _, s in nca.sections if section_info(s)['fs_type'] == 'romfs')
    raise SystemExit(f'{path}: no Program NCA')


def romfs_tree(sec):
    lvl_off, lvl_size = romfs_level(sec)
    h = struct.unpack('<10Q', sec.read(lvl_off, 0x50))
    _, _, _, dm_off, dm_size, _, _, fm_off, fm_size, data_off = h
    dirs = sec.read(lvl_off + dm_off, dm_size)
    files = sec.read(lvl_off + fm_off, fm_size)

    def dname(o):
        n = struct.unpack_from('<I', dirs, o + 20)[0]
        return dirs[o + 24:o + 24 + n].decode()

    out = []

    def walk(d, path):
        child, fo = struct.unpack_from('<II', dirs, d + 8)
        while fo != 0xFFFFFFFF:
            parent, sib, off, size, _, nlen = struct.unpack_from('<IIQQII', files, fo)
            name = files[fo + 32:fo + 32 + nlen].decode()
            out.append((path + name, lvl_off + data_off + off, size))
            fo = sib
        while child != 0xFFFFFFFF:
            walk(child, path + dname(child) + '/')
            child = struct.unpack_from('<I', dirs, child + 4)[0]

    walk(0, '/')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('nsp')
    ap.add_argument('out')
    ap.add_argument('--keys', default='~/.switch/prod.keys')
    ap.add_argument('--base', metavar='BASE.nsp', help="for an update NSP: the base game, whose RomFS the patch applies to")
    ap.add_argument('--romfs-get', nargs='*', default=[], metavar='PATH',
                    help='Program RomFS files to extract into OUT/romfs/')
    a = ap.parse_args()
    keys = load_keys(a.keys)
    os.makedirs(a.out, exist_ok=True)
    f = open(a.nsp, 'rb')
    ents = read_pfs0(f, 0)
    tik = next((e for e in ents if e[0].endswith('.tik')), None)
    title_key = None
    if tik:
        f.seek(tik[1] + 0x180)
        title_key = f.read(16)
    report = {}
    for name, off, size in ents:
        if not name.endswith('.nca'):
            continue
        nca = Nca(f, off, keys, title_key)
        info = {'content_type': nca.content_type, 'keygen': nca.keygen,
                'title_id': f'{nca.title_id:016x}', 'size': size, 'sections': {}}
        for i, sec in nca.sections:
            si = section_info(sec)
            info['sections'][i] = si
            if nca.content_type == 0 and si['fs_type'] == 'pfs0':  # Program ExeFS
                exefs = os.path.join(a.out, 'exefs')
                os.makedirs(exefs, exist_ok=True)
                si['files'] = {}
                for fname, foff, fsize in section_pfs0(sec):
                    with open(os.path.join(exefs, fname), 'wb') as o:
                        for p in range(0, fsize, 1 << 24):
                            o.write(sec.read(foff + p, min(1 << 24, fsize - p)))
                    si['files'][fname] = fsize
                    print(f'exefs/{fname}: {fsize:,}')
            elif si['fs_type'] == 'romfs' and not si['has_compression'] and not si['has_sparse']:
                tag = {0: 'program', 2: 'control'}.get(nca.content_type, f'type{nca.content_type}')
                if si['encryption'] == 4:  # update: BKTR over the base game's RomFS
                    if nca.content_type != 0 or not a.base:
                        print(f'{name} section {i}: patch RomFS skipped (needs --base, Program only)', file=sys.stderr)
                        continue
                    sec = PatchedSection(sec, base_program_romfs(a.base, keys))
                    with open(os.path.join(a.out, f'romfs_{tag}.bktr'), 'wb') as o:
                        o.write(sec.tables())
                    storage = sec.storage(a.nsp, a.base)
                else:
                    storage = romfs_storage(a.nsp, sec)
                tree = romfs_tree(sec)
                with open(os.path.join(a.out, f'romfs_{tag}.tsv'), 'w') as o:
                    for p, fo, fs in tree:
                        o.write(f'{p}\t{fo}\t{fs}\n')
                si['romfs_files'] = len(tree)
                with open(os.path.join(a.out, f'romfs_{tag}.json'), 'w') as o:
                    json.dump(storage, o, indent=1)
                print(f'{name} romfs: {len(tree):,} files')
                if nca.content_type == 0:
                    for p, fo, fs in tree:
                        if p not in a.romfs_get:
                            continue
                        dst = os.path.join(a.out, 'romfs', p.lstrip('/'))
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        with open(dst, 'wb') as o:
                            for q in range(0, fs, 1 << 24):
                                o.write(sec.read(fo + q, min(1 << 24, fs - q)))
                        print(f'romfs{p}: {fs:,}')
            print(f'{name} section {i}: {si}', file=sys.stderr)
        report[name] = info
    json.dump(report, open(os.path.join(a.out, 'nsp_report.json'), 'w'), indent=1)


if __name__ == '__main__':
    main()

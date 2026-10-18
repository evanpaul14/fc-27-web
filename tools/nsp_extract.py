"""Extract the ExeFS (code) of an NSP's Program NCA without unpacking the whole NSP.

Reads NCAs in place from the NSP, decrypts only the byte ranges needed, and lists
the RomFS tree (without extracting it) so assets can be pulled selectively later.

usage: python3 -I tools/nsp_extract.py GAME.nsp OUTDIR [--keys ~/.switch/prod.keys]
"""
import argparse, json, os, struct, sys
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
                tree = romfs_tree(sec)
                tag = {0: 'program', 2: 'control'}.get(nca.content_type, f'type{nca.content_type}')
                with open(os.path.join(a.out, f'romfs_{tag}.tsv'), 'w') as o:
                    for p, fo, fs in tree:
                        o.write(f'{p}\t{fo}\t{fs}\n')
                si['romfs_files'] = len(tree)
                with open(os.path.join(a.out, f'romfs_{tag}.json'), 'w') as o:
                    json.dump(romfs_storage(a.nsp, sec), o, indent=1)
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

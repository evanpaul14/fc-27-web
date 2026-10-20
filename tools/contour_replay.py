#!/usr/bin/env python3
"""Replay compute 16dff9100's contour walk from a FIFA_DBG_CSDUMP dump (build/csdump/<n>/).

  python3 -I tools/contour_replay.py build/csdump/<n> [...]

Mirrors the translated program (build/shtool -o): each of cb4[0][1] threads builds the link of triangle
i = tid + 64k (neighbour across the edge the contour leaves by, low 10 bits), then thread 0 follows links from
the start triangle until it comes back (closed), leaves the batch (index >= count) or runs into a cycle that
doesn't hold the start (what the GPU spins on until the shader guard). Output slots and outcodes are left out:
the walk doesn't read them.
"""
import struct
import sys


def u32s(b):
    return struct.unpack('<%dI' % (len(b) // 4), b[:len(b) // 4 * 4])


def replay(d):
    meta = {}
    for line in open(d + '/meta.txt'):
        f = line.split()
        meta[f[0]] = f[1:]
    rd = lambda n: open('%s/%s.bin' % (d, n), 'rb').read()
    cb3, cb4 = u32s(rd('u0')), u32s(rd('u1'))  # c[3], c[4]: bound slots 0, 1 (cbuf_base 3)
    s0, s1, s3 = u32s(rd('s0')), u32s(rd('s1')), u32s(rd('s3'))
    tfmt, twidth = int(meta['t0'][3]), int(meta['t0'][4].split('x')[0])
    t0 = rd('t0')
    assert tfmt == 8, 'expected R16_UI indices, got fmt %d' % tfmt
    idx = lambda x: struct.unpack_from('<H', t0, 2 * x)[0] if x < twidth else 0  # out of range reads 0

    count, base = cb4[1], cb4[2]
    k = min((cb3[4 * 4 + 2] - 1) & 0xffffffff, cb4[0])
    start_g = s0[k] if k < len(s0) else 0
    print('%s: count %d base %d start slot %d -> %#x, index texture %d texels' % (d, count, base, k, start_g, twidth))
    if start_g == 0xffffffff or start_g < base:
        print('  exits before the walk')
        return
    start = (start_g - base) & 0xffffffff

    def flagword(v):
        w = v >> 2
        return s3[w] if w < len(s3) else 0

    link = {}
    odd = 0
    for i in range(count):
        a = (i + base) * 3 & 0xffffffff
        v0, v1, v2 = idx(a), idx(a + 1), idx(a + 2)
        f0, f1, f2 = flagword(v0), flagword(v1), flagword(v2)
        c0 = (f0 >> ((v0 & 3) * 8)) & 0x10
        c1 = (f1 >> ((v1 & 3) * 8)) & 0x10
        r4 = f2 >> ((v2 & 3) * 8)
        c2 = r4 & 0x10
        adj = s1[i] if i < len(s1) else 0
        n0, n1, n2 = adj & 0x3ff, (adj >> 10) & 0x3ff, (adj >> 20) & 0x3ff
        r9 = f1
        e01 = bool(c0 | c1) and not (c0 & c1)
        e12 = bool(c2 | c1) and not (c2 & c1)
        e20 = bool(c0 | c2) and not (c0 & c2)
        if e01:
            if c1 == 0x10:
                r9, r4 = 1, n0
            else:
                odd += 1  # leaves r9 = v1's flag word, r4 = v2's shifted flag word
        elif e12:
            if c2 == 0x10:
                r9, r4 = 1, n1
        elif c0 == 0x10 and e20:
            r9, r4 = 1, n2
        else:
            r9, r4 = 0, 0x3ff
        link[i] = r4  # only the low 10 bits matter to the walk
    print('  %d links, %d through the edge 0-1 "wrong way" path' % (len(link), odd))

    cur = link.get(start, 0) & 0x3ff
    seen = {start: 0}
    path = [start]
    steps = 1
    while True:
        if cur >= count:
            print('  walk leaves the batch at %d after %d steps (ends)' % (cur, steps))
            return
        if cur == start:
            print('  closed after %d steps' % steps)
            return
        if cur in seen:
            print('  CYCLE without the start: %d steps in, re-entered %d (first seen at step %d); path head %s' %
                  (steps, cur, seen[cur], path[:12]))
            return
        seen[cur] = steps
        path.append(cur)
        cur = link[cur] & 0x3ff
        steps += 1


if __name__ == '__main__':
    for d in sys.argv[1:]:
        replay(d.rstrip('/'))

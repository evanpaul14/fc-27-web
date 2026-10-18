"""Check main's table-shaped indirect branches against build.py's resolver.

A `br xR` is table-shaped when xR comes from `add xR, xB, xO, lsl/sxtw` and
xO from a register-offset load within a few instructions. Prints how many
the resolver leaves unresolved, with their containing function.

usage: python3 -I tools/jtcheck.py   (from the repo root, after image.py)
"""
import bisect, os, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'recomp'))
os.chdir(ROOT)
import build  # noqa: E402

p = build.Program('build')
m = p.layout['modules'][0]
base = m['base']
text = next(s for s in m['segments'] if s['name'] == 'text')
d = np.fromfile('build/main.img', dtype='<u4')[:(text['addr'] + text['size'] - base) // 4]


def table_shaped(i):
    r = (int(d[i]) >> 5) & 31
    for j in range(i - 1, max(i - 7, 0), -1):
        w = int(d[j])
        if (w & 31) == r and ((w >> 21) & 0x7ff) in (0b10001011000, 0b10001011001):
            ro = (w >> 16) & 31
            return any((int(d[k]) & 31) == ro and ((int(d[k]) >> 27) & 7) == 7 and (int(d[k]) >> 21) & 1
                       and ((int(d[k]) >> 10) & 3) == 2 for k in range(j - 1, max(j - 7, 0), -1))
    return False


cands = [base + int(i) * 4 for i in np.nonzero((d & 0xfffffc1f) == 0xd61f0000)[0] if table_shaped(int(i))]
starts = sorted(s for s in p.starts if base <= s < base + len(d) * 4)
fns, bad, unreached = {}, [], 0
for a in cands:
    f = starts[bisect.bisect_right(starts, a) - 1]
    if f not in fns:
        fns[f] = build.discover(p, f)
    if a not in fns[f].jt:
        unreached += 1
    elif not fns[f].jt[a]:
        bad.append((a, f))
print(f'{len(cands)} table-shaped; {unreached} not in the containing body; {len(bad)} unresolved')
for a, f in bad:
    print(f'  br {a:#x} in {f:#x}')

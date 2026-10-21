#!/usr/bin/env python3
"""Insert register probes into a translated compute shader (MSL from `build/shtool -o`) for tools/lutreplay.

  python3 -I tools/msl_probe.py <in.metal> <out.metal> <pc hex>[,<pc hex>...]

After the instruction at each pc, r0..r15 (as floats) go to dbg[thread * 64 + 4 * k + 0..3] (k: the pc's index in the
list, up to 16 pcs), thread = linear 32^3 position as lutreplay dispatches it. The kernel gets
`device float4 *dbg [[buffer(7)]]`. A probe that sits in a branch not taken leaves zeros.
"""
import re
import sys

src = open(sys.argv[1]).read().split('\n')
pts = [int(p, 16) for p in sys.argv[3].split(',')]
mark = re.compile(r'\s*// pc ([0-9a-f]+):')
pcs = [int(mark.match(l).group(1), 16) for l in src if mark.match(l)]
nxt = {pcs[i]: pcs[i + 1] for i in range(len(pcs) - 1)}
ins = {}
for k, p in enumerate(pts):
    ins.setdefault(nxt[p], []).append(k)
idx = '((ctaid.z * 32u + ctaid.y * 2u + tid.y) * 32u + ctaid.x * 32u + tid.x) * 64u'
out = []
for l in src:
    l = l.replace('texture3d<float, access::write> t1 [[texture(1)]])',
                  'texture3d<float, access::write> t1 [[texture(1)]], device float4 *dbg [[buffer(7)]])')
    m = mark.match(l)
    for k in ins.get(int(m.group(1), 16), []) if m else []:
        for q in range(4):
            regs = ','.join('as_type<float>(r%d)' % (4 * q + j) for j in range(4))
            out.append('    dbg[%s + %du] = float4(%s);' % (idx, 4 * k + q, regs))
    out.append(l)
open(sys.argv[2], 'w').write('\n'.join(out))

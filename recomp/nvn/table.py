"""Collect NVN(name) implementations from recomp/nvn/*.c into a table sorted by
name (recomp/nvn/boot.c looks names up in it).

usage: python3 -I recomp/nvn/table.py OUT.c SRC.c...
"""
import re, sys

# NVN(name) { ... }, and cmdbuf.c's PLAIN(name, op) one-line recorders
names = sorted({n for f in sys.argv[2:] for t in re.findall(r'^(?:NVN\((\w+)\)|PLAIN\((\w+),)', open(f).read(), re.M)
                for n in t if n})
out = ['#include "../recomp/nvn/nvn.h"']
out += [f'void nvn_{n}(CPU *c);' for n in names]
out.append('const NvnImpl nvn_impls[] = {')
out += [f'  {{"{n}", nvn_{n}}},' for n in names]
out.append('};')
out.append(f'const unsigned nvn_impl_count = {len(names)};')
text = '\n'.join(out) + '\n'
try:
    old = open(sys.argv[1]).read()
except OSError:
    old = None
if old != text:
    open(sys.argv[1], 'w').write(text)

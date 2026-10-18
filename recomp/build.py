"""Recompile guest functions to C.

Discovers each function's body by recursive descent from its start, resolves
switch jump tables, and emits one C function per guest function
(`void F_<addr>(CPU *c)`), split into translation units, plus a sorted
dispatch table for indirect calls.

Which functions get built:
  --all           every known function start (tools/analyze.py) of every module
  default         closure from the entry points over direct calls, tail calls
                  and code-referenced function/vtable addresses
Functions named in recomp/hle/*.c with HLE(symbol) are not translated; calls
to them go to the host implementation instead.

usage: python3 -I recomp/build.py build [--all] [--extra-roots FILE]
"""
import argparse, bisect, collections, glob, json, os, re, struct, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from a64 import translate, Unsupported, bits, sx  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class Program:
    def __init__(self, bdir):
        self.layout = json.load(open(os.path.join(bdir, 'layout.json')))
        self.mods = []
        for m in self.layout['modules']:
            img = open(os.path.join(bdir, m['name'] + '.img'), 'rb').read()
            text = next(s for s in m['segments'] if s['name'] == 'text')
            self.mods.append((m['base'], m['base'] + len(img), img, text['addr'], text['addr'] + text['size'], m))
        self.syms = collections.defaultdict(list)
        self.by_name = {}
        for line in open(os.path.join(bdir, 'symbols.tsv')):
            a, mod, nm, sz, typ = line.rstrip('\n').split('\t')
            self.syms[int(a, 16)].append(nm)
            self.by_name.setdefault(nm, int(a, 16))
        self.missing = {a: nm for nm, a in self.layout['missing'].items()}
        self.starts = set()
        self.vtables = {}
        for base, end, img, t0, t1, m in self.mods:
            adir = os.path.join(ROOT, 'extracted', 'analysis', m['name'])
            for line in open(os.path.join(adir, 'functions.tsv')):
                self.starts.add(base + int(line.split('\t', 1)[0], 16))
            for line in open(os.path.join(adir, 'vtables.tsv')):
                a, n, slots = line.rstrip('\n').split('\t')
                self.vtables[base + int(a, 16)] = [base + int(s, 16) for s in slots.split(',') if s.startswith('0x')]
        self.vt_sorted = sorted(self.vtables)
        self.cache = {}

    def mod_of(self, a):
        for m in self.mods:
            if m[0] <= a < m[1]:
                return m
        return None

    def is_text(self, a):
        m = self.mod_of(a)
        return m is not None and m[3] <= a < m[4] and a % 4 == 0

    def word(self, a):
        m = self.mod_of(a)
        return struct.unpack_from('<I', m[2], a - m[0])[0]

    def read(self, a, n):
        m = self.mod_of(a)
        if m is None or a + n > m[1]:
            return None
        return m[2][a - m[0]:a - m[0] + n]

    def insn(self, a):
        r = self.cache.get(a)
        if r is None:
            w = self.word(a)
            try:
                r = translate(w, a)
            except Unsupported as e:
                from a64 import Insn
                r = Insn('trap', f'a64_trap(c, {a:#x}, {w:#x}); return;', tag='unsupported:' + str(e))
            self.cache[a] = r
        return r

    def name(self, a):
        n = self.syms.get(a)
        return n[0] if n else None


# ------------------------------------------------------------ jump tables

def reg_writes(w):
    """Destination register of the simple integer instructions we track."""
    return bits(w, 4, 0)


def resolve_jump_table(p, br_pc, body):
    """Return the possible targets of the `br` at br_pc, or None."""
    w = p.word(br_pc)
    r = bits(w, 9, 5)
    seq = []  # (pc, word) walking backwards through straight-line code
    a = br_pc - 4
    while len(seq) < 40 and a in body:
        seq.append((a, p.word(a)))
        a -= 4
    consts = {}

    def find_def(reg, start_idx):
        for k in range(start_idx, len(seq)):
            pc, ww = seq[k]
            if reg_writes(ww) == reg and not is_store(ww) and not is_cmp(ww) and not is_branch(ww):
                return k, pc, ww
        return None

    d = find_def(r, 0)
    if not d:
        return None
    k_add, _, wadd = d
    # add xR, xB, xO, lsl #s   |   add xR, xB, wO, sxtw #s
    if bits(wadd, 30, 21) == 0b0001011000 and bits(wadd, 31, 31):  # shifted reg, LSL
        if bits(wadd, 23, 22) != 0:
            return None
        rb, ro, sh = bits(wadd, 9, 5), bits(wadd, 20, 16), bits(wadd, 15, 10)
    elif bits(wadd, 30, 21) == 0b0001011001 and bits(wadd, 31, 31):  # extended reg
        rb, ro, sh = bits(wadd, 9, 5), bits(wadd, 20, 16), bits(wadd, 12, 10)
    else:
        return None
    d = find_def(ro, k_add + 1)
    if not d:
        return None
    k_ld, _, wld = d
    # ldr{b,h,sw,w} wO, [xT, xI{, lsl #n}] (register offset)
    if bits(wld, 29, 27) != 0b111 or bits(wld, 26, 26) or bits(wld, 24, 24) or not bits(wld, 21, 21) \
            or bits(wld, 11, 10) != 2:
        return None
    size, opc = bits(wld, 31, 30), bits(wld, 23, 22)
    signed = opc in (2, 3)
    esz = 1 << size
    rt, ri = bits(wld, 9, 5), bits(wld, 20, 16)

    def const_of(reg, start_idx):
        """Value of reg set by adrp(+add) or adr before start_idx."""
        d = find_def(reg, start_idx)
        if not d:
            return None
        k, pc, ww = d
        if bits(ww, 28, 24) == 0b10000:  # adr / adrp
            imm = sx((bits(ww, 23, 5) << 2) | bits(ww, 30, 29), 21)
            return (pc & ~0xfff) + (imm << 12) if ww >> 31 else pc + imm
        if bits(ww, 31, 22) == 0b1001000100 and bits(ww, 9, 5) == reg:  # add xR, xR, #imm
            base = const_of(reg, k + 1)
            return None if base is None else base + bits(ww, 21, 10)
        return None

    table = const_of(rt, k_ld + 1)
    base = table if rb == rt else const_of(rb, k_add + 1)
    if table is None or base is None:
        return None
    # bound: cmp wI/xI, #N (subs with rd=31) somewhere before, followed by b.hi/b.hs
    n = None
    a = br_pc - 4
    for _ in range(64):
        if a not in body:
            a -= 4
            continue
        ww = p.word(a)
        if bits(ww, 30, 23) == 0b11100010 and bits(ww, 4, 0) == 31 and bits(ww, 9, 5) == ri and not bits(ww, 22, 22):
            n = bits(ww, 21, 10) + 1
            break
        a -= 4
    targets = []
    for k in range(n if n is not None else 1024):
        raw = p.read(table + k * esz, esz)
        if raw is None:
            break
        v = int.from_bytes(raw, 'little', signed=signed)
        t = base + (v << sh)
        if n is None and (not p.is_text(t) or (t in p.starts and t not in body)):
            break
        if not p.is_text(t):
            return None
        targets.append(t)
    return sorted(set(targets)) or None


def is_store(w):
    return bits(w, 27, 27) == 1 and bits(w, 25, 25) == 0 and bits(w, 22, 22) == 0


def is_cmp(w):
    return bits(w, 4, 0) == 31 and bits(w, 29, 29) == 1


def is_branch(w):
    return bits(w, 28, 26) == 0b101


# ------------------------------------------------------------ function discovery

class Func:
    __slots__ = ('start', 'body', 'labels', 'jt', 'calls', 'refs', 'falls')

    def __init__(self, start):
        self.start, self.body, self.labels, self.jt = start, set(), {start}, {}
        self.calls, self.refs, self.falls = set(), set(), {}


def walk(p, fn, entries):
    """Grow fn.body from entries by recursive descent within the function."""
    f = fn.start
    work = list(entries)
    while work:
        a = work.pop()
        prev = None
        while a not in fn.body:
            if (a != f and a in p.starts) or not p.is_text(a):
                if prev is not None:  # falls through into another function
                    fn.falls[prev] = a
                    fn.calls.add(a)
                break
            fn.body.add(a)
            ins = p.insn(a)
            k = ins.kind
            if k == 'bl':
                fn.calls.add(ins.target)
            elif k in ('b', 'bcond'):
                t = ins.target
                if (t in p.starts and t != f) or not p.is_text(t):
                    fn.calls.add(t)  # tail call
                else:
                    fn.labels.add(t)
                    work.append(t)
            elif k == 'br':
                fn.jt.setdefault(a, None)
            if k not in ('op', 'bl', 'blr', 'bcond'):
                break  # b, br, ret, trap end the block
            prev, a = a, a + 4


def discover(p, f):
    fn = Func(f)
    walk(p, fn, [f])
    # jump tables are resolved against the body found so far; their targets
    # can add code containing further tables
    while any(v is None for v in fn.jt.values()):
        for a in [a for a, v in fn.jt.items() if v is None]:
            ts = resolve_jump_table(p, a, fn.body) or []
            fn.jt[a] = ts
            fn.labels.update(ts)
            walk(p, fn, ts)
    return fn


def code_refs(p, fn):
    """Function and vtable addresses materialised by adrp/add/adr in the body."""
    out = set()
    regs = {}
    for a in sorted(fn.body):
        w = p.word(a)
        if bits(w, 28, 24) == 0b10000:
            imm = sx((bits(w, 23, 5) << 2) | bits(w, 30, 29), 21)
            v = (a & ~0xfff) + (imm << 12) if w >> 31 else a + imm
            regs[bits(w, 4, 0)] = v
            if not w >> 31:
                out.add(v)
        elif bits(w, 31, 22) == 0b1001000100 and bits(w, 9, 5) in regs:
            v = regs[bits(w, 9, 5)] + bits(w, 21, 10)
            out.add(v)
    res = set()
    for v in out:
        if v in p.starts:
            res.add(v)
        i = bisect.bisect_right(p.vt_sorted, v + 16) - 1
        if i >= 0:
            vt = p.vt_sorted[i]
            if vt - 16 <= v <= vt + 16:
                res.update(t for t in p.vtables[vt] if t in p.starts)
    return res


# ------------------------------------------------------------ emission

def cname(a):
    return f'F_{a:08x}'


def emit_func(p, fn, built, hle, decls):
    out = [f'void {cname(fn.start)}(CPU *c) {{']
    nm = p.name(fn.start)
    if nm:
        out[0] = f'/* {nm} */\n' + out[0]

    def call(t):
        if t in hle:
            decls.add(f'void H_{t:08x}(CPU *c);')
            return f'H_{t:08x}(c);'
        if t in built:
            decls.add(f'void {cname(t)}(CPU *c);')
            return f'{cname(t)}(c);'
        return f'a64_call(c, {t:#x}ull);'

    for a in sorted(fn.body):
        if a in fn.labels:
            out.append(f'L_{a:x}:;')
        ins = p.insn(a)
        k = ins.kind
        if k == 'op':
            if ins.c:
                out.append('  ' + ins.c)
        elif k == 'b':
            t = ins.target
            out.append(f'  goto L_{t:x};' if t in fn.labels and t in fn.body else f'  {call(t)} return;')
        elif k == 'bcond':
            t = ins.target
            if t in fn.labels and t in fn.body:
                out.append(f'  if ({ins.c}) goto L_{t:x};')
            else:
                out.append(f'  if ({ins.c}) {{ {call(t)} return; }}')
        elif k == 'bl':
            out.append(f'  c->x[30] = {a + 4:#x}ull; {call(ins.target)}')
        elif k == 'blr':
            out.append(f'  {{ uint64_t t = {ins.c}; c->x[30] = {a + 4:#x}ull; a64_call(c, t); }}')
        elif k == 'br':
            ts = fn.jt.get(a)
            if ts:
                cases = ' '.join(f'case {t:#x}ull: goto L_{t:x};' for t in ts)
                out.append(f'  {{ uint64_t t = {ins.c}; switch (t) {{ {cases} default: a64_bad_jump(c, {a:#x}ull, t); return; }} }}')
            else:
                out.append(f'  a64_call(c, {ins.c}); return;')
        elif k == 'ret':
            out.append('  return;')
        else:
            out.append('  ' + ins.c)
        if a in fn.falls:
            t = fn.falls[a]
            if k in ('op', 'bl', 'blr', 'bcond'):
                if p.is_text(t):
                    out.append(f'  {call(t)} return;')
                else:
                    out.append(f'  a64_trap(c, {t:#x}ull, 0); return;')
    out.append('}')
    return '\n'.join(out)


def load_hle(p):
    hle = {}
    for path in glob.glob(os.path.join(HERE, 'hle', '*.c')):
        for m in re.finditer(r'^HLE\((\w+)\)', open(path).read(), re.M):
            a = p.by_name.get(m.group(1))
            if a is None:
                print(f'warning: HLE symbol {m.group(1)} not found')
                continue
            hle[a] = m.group(1)
    return hle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bdir')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--extra-roots', default=None)
    args = ap.parse_args()
    t0 = time.time()
    p = Program(args.bdir)
    hle = load_hle(p)

    roots = set()
    for m in p.layout['modules']:
        if m['init']:
            roots.add(m['init'])
        roots.update(a for a in m['init_array'] if p.is_text(a))
        roots.update(a for a in m['fini_array'] if p.is_text(a))
    for nm in ('nninitStartup', 'nnMain'):
        roots.add(p.by_name[nm])
    extra = os.path.join(args.bdir, 'extra_roots.txt') if args.extra_roots is None else args.extra_roots
    if os.path.exists(extra):
        for line in open(extra):
            if line.strip():
                roots.add(int(line.split()[0], 16))
    for a in roots:
        p.starts.add(a)

    funcs = {}
    work = sorted(p.starts) if args.all else sorted(roots)
    seen = set(work)
    while work:
        f = work.pop()
        if f in hle or not p.is_text(f):
            continue
        fn = discover(p, f)
        funcs[f] = fn
        if args.all:
            continue
        nxt = set(fn.calls) | code_refs(p, fn)
        for t in nxt:
            if t not in seen and p.is_text(t):
                if t not in p.starts:
                    p.starts.add(t)
                seen.add(t)
                work.append(t)
    built = set(funcs)
    n_ins = sum(len(f.body) for f in funcs.values())
    n_jt = sum(len(f.jt) for f in funcs.values())
    n_jt_ok = sum(1 for f in funcs.values() for v in f.jt.values() if v)
    print(f'{len(funcs)} functions, {n_ins} instructions, {n_jt_ok}/{n_jt} indirect jumps resolved as tables '
          f'({time.time() - t0:.1f}s)')

    gdir = os.path.join(args.bdir, 'gen')
    os.makedirs(gdir, exist_ok=True)
    # Translation units are fixed 512 KB address buckets, so adding functions
    # only changes the units they land in and rebuilds stay incremental.
    order = sorted(funcs)
    buckets = collections.defaultdict(list)
    for f in order:
        buckets[f >> 19].append(f)
    written = set()
    for key, fs in sorted(buckets.items()):
        decls = set(f'void {cname(f)}(CPU *c);' for f in fs)
        bodies = [emit_func(p, funcs[f], built, hle, decls) for f in fs]
        name = f'f_{key << 19:08x}.c'
        path = os.path.join(gdir, name)
        text = '#include "gen.h"\n' + '\n'.join(sorted(decls)) + '\n\n' + '\n\n'.join(bodies) + '\n'
        if not os.path.exists(path) or open(path).read() != text:
            open(path, 'w').write(text)
        written.add(name)
    for old in glob.glob(os.path.join(gdir, 'f_*.c')):
        if os.path.basename(old) not in written:
            os.remove(old)
    tu_i = len(written)
    with open(os.path.join(gdir, 'table.c'), 'w') as o:
        o.write('#include "gen.h"\n')
        for f in order:
            o.write(f'void {cname(f)}(CPU *c);\n')
        for a in sorted(hle):
            o.write(f'void H_{a:08x}(CPU *c);\n')
        o.write('const A64Fn a64_fn_table[] = {\n')
        entries = sorted([(f, cname(f)) for f in order] + [(a, f'H_{a:08x}') for a in hle])
        for a, n in entries:
            o.write(f'  {{{a:#x}ull, {n}}},\n')
        o.write('};\n')
        o.write(f'const unsigned a64_fn_count = {len(entries)};\n')
        o.write('const A64Sym a64_missing[] = {\n')
        for a, nm in sorted(p.missing.items()):
            o.write(f'  {{{a:#x}ull, "{nm}"}},\n')
        o.write('};\n')
        o.write(f'const unsigned a64_missing_count = {len(p.missing)};\n')
    with open(os.path.join(gdir, 'hle_table.c'), 'w') as o:
        o.write('#include "gen.h"\n')
        for a, nm in sorted(hle.items()):
            o.write(f'void hle_{nm}(CPU *c);\nvoid H_{a:08x}(CPU *c) {{ hle_{nm}(c); }}\n')
    with open(os.path.join(gdir, 'layout.c'), 'w') as o:
        o.write('#include "gen.h"\n#include "../../recomp/rt/host.h"\n')
        mods = p.layout['modules']
        o.write('const HostModule host_modules[] = {\n')
        for m in mods:
            o.write(f'  {{"{m["name"]}", {m["base"]:#x}ull, {m["size"]:#x}ull, {m["init"] or 0:#x}ull}},\n')
        o.write('};\n')
        o.write(f'const unsigned host_module_count = {len(mods)};\n')
        o.write(f'const uint64_t host_nn_init_start = {p.by_name["_ZN2nn4init5StartEmmPFvvES2_S2_"]:#x}ull;\n')
    with open(os.path.join(gdir, 'gen.h'), 'w') as o:
        o.write('#include "../../recomp/rt/runtime.h"\n')
    stats = {'functions': len(funcs), 'instructions': n_ins, 'tus': tu_i,
             'unsupported': collections.Counter(p.cache[a].tag for f in funcs.values() for a in f.body
                                                if p.cache[a].tag.startswith('unsupported')).most_common(20)}
    json.dump(stats, open(os.path.join(args.bdir, 'build_stats.json'), 'w'), indent=1)
    print(f'{tu_i} translation units written ({time.time() - t0:.1f}s)')


if __name__ == '__main__':
    main()

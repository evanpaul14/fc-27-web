"""Instruction census of the text segment, to scope the recompiler.

Disassembles main.elf (from tools/mkelf.py) with llvm-objdump in parallel
chunks and counts mnemonics, instruction classes, NEON arrangements and
system register accesses. Writes:
  OUT/insn_mnemonics.tsv  mnemonic, class, count
  OUT/insn_stats.json     class totals, system registers, unknown encodings

usage: python3 -I tools/insn_stats.py extracted/code/main extracted/analysis/main
"""
import collections, json, os, re, subprocess, sys
from concurrent.futures import ProcessPoolExecutor

OBJDUMP = os.path.expanduser('~/emsdk/upstream/bin/llvm-objdump')
CHUNK = 4 << 20

BRANCH = {'b', 'bl', 'br', 'blr', 'ret', 'cbz', 'cbnz', 'tbz', 'tbnz'}
EXCLUSIVE = re.compile(r'^(ld|st)a?l?x[rp][bh]?$|^clrex$')
ACQREL = re.compile(r'^(ldar|stlr|ldapr|ldapur|stlur)[bh]?$')
LSE = re.compile(r'^(cas|casp|swp|ld(add|clr|eor|set|smax|smin|umax|umin)|st(add|clr|eor|set|smax|smin|umax|umin))(a|l|al)?[bh]?$')
BARRIER = {'dmb', 'dsb', 'isb'}
SYSTEM = {'mrs', 'msr', 'svc', 'hvc', 'smc', 'brk', 'hlt', 'hint', 'nop', 'yield',
          'wfe', 'wfi', 'sev', 'sevl', 'sys', 'sysl', 'ic', 'dc', 'tlbi', 'at',
          'udf', 'bti', 'paciasp', 'autiasp', 'pacibsp', 'autibsp', 'xpaclri'}
VEC = re.compile(r'\bv\d+\.|\{\s*v\d+')            # NEON vector register operand
FPREG = re.compile(r'(^|[\s,\[])([bhsdq]\d+)\b')  # scalar FP/SIMD register operand


def classify(mn, ops):
    base = mn.split('.')[0]
    if base in BRANCH:
        return 'branch'
    if EXCLUSIVE.match(mn):
        return 'exclusive'
    if ACQREL.match(mn):
        return 'acqrel'
    if LSE.match(mn):
        return 'lse_atomic'
    if mn in BARRIER:
        return 'barrier'
    if mn in SYSTEM:
        return 'system'
    if mn == '<unknown>':
        return 'unknown'
    vec, fp = VEC.search(ops), FPREG.search(ops)
    mem = (mn.startswith('ld') or mn.startswith('st')) and '[' in ops
    if mem:
        return 'mem_neon' if vec else 'mem_fp' if fp else 'mem'
    if vec:
        return 'neon'
    if fp or mn.startswith('f') or mn in ('scvtf', 'ucvtf'):
        return 'fp'
    return 'int'


def work(args):
    elf, lo, hi = args
    p = subprocess.Popen([OBJDUMP, '-d', '--no-show-raw-insn', '--no-leading-addr',
                          f'--start-address={lo:#x}', f'--stop-address={hi:#x}', elf],
                         stdout=subprocess.PIPE, text=True, bufsize=1 << 20)
    mns, classes = collections.Counter(), collections.Counter()
    arr, sysregs, svcs = collections.Counter(), collections.Counter(), collections.Counter()
    for line in p.stdout:
        if not line.startswith(' ') or '\t' not in line:
            continue
        parts = line.strip().split('\t', 1)
        mn = parts[0]
        ops = parts[1] if len(parts) > 1 else ''
        c = classify(mn, ops)
        mns[(mn, c)] += 1
        classes[c] += 1
        if c in ('neon', 'mem_neon'):
            for a in re.findall(r'v\d+\.(\d*[bhsdq])', ops):
                arr[a] += 1
        elif mn in ('mrs', 'msr'):
            sysregs[f'{mn} {ops.split(",")[0 if mn == "msr" else 1].strip()}'] += 1
        elif mn in ('svc', 'brk', 'hlt', 'udf'):
            svcs[f'{mn} {ops.strip()}'] += 1
    p.wait()
    return mns, classes, arr, sysregs, svcs


def main():
    prefix, out = sys.argv[1], sys.argv[2]
    info = json.load(open(prefix + '.json'))
    text = next(s for s in info['segments'] if s['name'] == 'text')
    elf = os.path.join(out, 'main.elf')
    lo, hi = text['mem_off'], text['mem_off'] + text['size']
    jobs = [(elf, a, min(a + CHUNK, hi)) for a in range(lo, hi, CHUNK)]
    tot = [collections.Counter() for _ in range(5)]
    with ProcessPoolExecutor(os.cpu_count()) as ex:
        for res in ex.map(work, jobs):
            for t, r in zip(tot, res):
                t.update(r)
    mns, classes, arr, sysregs, svcs = tot
    n = sum(classes.values())
    with open(os.path.join(out, 'insn_mnemonics.tsv'), 'w') as f:
        for (mn, c), k in mns.most_common():
            f.write(f'{mn}\t{c}\t{k}\n')
    by_class = collections.defaultdict(collections.Counter)
    for (mn, c), k in mns.items():
        by_class[c][mn] += k
    stats = {
        'instructions': n,
        'distinct_mnemonics': len({m for m, _ in mns}),
        'classes': {c: [k, round(100 * k / n, 3)] for c, k in classes.most_common()},
        'top_by_class': {c: dict(v.most_common(25)) for c, v in by_class.items()},
        'neon_arrangements': dict(arr.most_common()),
        'sysregs': dict(sysregs.most_common()),
        'traps': dict(svcs.most_common()),
    }
    json.dump(stats, open(os.path.join(out, 'insn_stats.json'), 'w'), indent=1)
    print(f'{n} instructions, {stats["distinct_mnemonics"]} mnemonics')
    for c, (k, pct) in stats['classes'].items():
        print(f'  {c:11s} {k:10d} {pct:7.3f}%')


if __name__ == '__main__':
    main()

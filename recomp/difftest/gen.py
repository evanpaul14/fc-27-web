"""Generate a differential test of the A64 translator against the host CPU.

Samples instruction words from main's text (up to N per decoder tag), and
writes OUT/tests.S (each word wrapped in a stub that loads/stores the full
register state) and OUT/tests.c (the translated C for each word). Build and
run with recomp/difftest/run.sh.

usage: python3 -I recomp/difftest/gen.py extracted/code/main OUT [--per-tag N] [--seed S] [--tags a,b]
"""
import argparse, collections, json, os, random, sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from a64 import translate, Unsupported, bits  # noqa: E402

# PC-relative, exclusive-monitor, system-register and PAC-hint encodings are
# not comparable against the host in a single-instruction test.
SKIP = {'adr', 'adrp', 'ldr_lit', 'ldrsw_lit', 'ldr_lit_v', 'ldxr', 'stxr', 'ldxp', 'stxp', 'clrex',
        'mrs', 'msr', 'mrs_cntpct', 'mrs_cntfrq', 'mrs_dczid', 'yield', 'hint'}
WRITES_SP = {'add_imm', 'sub_imm', 'add_reg', 'sub_reg', 'and_imm', 'orr_imm', 'eor_imm'}

V_OFF = 272  # offsetof(NState, v)


def stub(i, w):
    s = [f'.globl _nat_{i}', '.p2align 2', f'_nat_{i}:',
         'stp x29, x30, [sp, #-16]!', 'stp x27, x28, [sp, #-16]!', 'stp x25, x26, [sp, #-16]!',
         'stp x23, x24, [sp, #-16]!', 'stp x21, x22, [sp, #-16]!', 'stp x19, x20, [sp, #-16]!',
         'stp d14, d15, [sp, #-16]!', 'stp d12, d13, [sp, #-16]!', 'stp d10, d11, [sp, #-16]!',
         'stp d8, d9, [sp, #-16]!',
         'adrp x9, _g_saved_sp@PAGE', 'mov x10, sp', 'str x10, [x9, _g_saved_sp@PAGEOFF]',
         'adrp x0, _g_state@PAGE', 'add x0, x0, _g_state@PAGEOFF']
    s += [f'ldp q{k}, q{k + 1}, [x0, #{V_OFF + 16 * k}]' for k in range(0, 32, 2)]
    s += ['ldr x1, [x0, #256]', 'msr nzcv, x1', 'ldr x1, [x0, #248]', 'mov sp, x1']
    s += [f'ldp x{k}, x{k + 1}, [x0, #{8 * k}]' for k in range(1, 31, 2)]
    s += ['ldr x0, [x0]', f'.inst {w:#010x}', 'str x0, [sp, #-16]!',
          'adrp x0, _g_state@PAGE', 'add x0, x0, _g_state@PAGEOFF']
    s += [f'stp x{k}, x{k + 1}, [x0, #{8 * k}]' for k in range(1, 31, 2)]
    s += ['ldr x1, [sp], #16', 'str x1, [x0]', 'mov x1, sp', 'str x1, [x0, #248]',
          'mrs x1, nzcv', 'str x1, [x0, #256]']
    s += [f'stp q{k}, q{k + 1}, [x0, #{V_OFF + 16 * k}]' for k in range(0, 32, 2)]
    s += ['adrp x9, _g_saved_sp@PAGE', 'ldr x9, [x9, _g_saved_sp@PAGEOFF]', 'mov sp, x9',
          'ldp d8, d9, [sp], #16', 'ldp d10, d11, [sp], #16', 'ldp d12, d13, [sp], #16',
          'ldp d14, d15, [sp], #16', 'ldp x19, x20, [sp], #16', 'ldp x21, x22, [sp], #16',
          'ldp x23, x24, [sp], #16', 'ldp x25, x26, [sp], #16', 'ldp x27, x28, [sp], #16',
          'ldp x29, x30, [sp], #16', 'ret', '']
    return '\n'.join(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('prefix')
    ap.add_argument('out')
    ap.add_argument('--per-tag', type=int, default=100)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--tags', default='')
    args = ap.parse_args()
    rng = random.Random(args.seed)
    info = json.load(open(args.prefix + '.json'))
    text = next(s for s in info['segments'] if s['name'] == 'text')
    img = np.fromfile(args.prefix + '.bin', dtype='<u4', count=text['size'] // 4, offset=text['mem_off'])
    words = np.unique(img).tolist()
    rng.shuffle(words)
    want = set(args.tags.split(',')) if args.tags else None
    by_tag = collections.defaultdict(list)
    for w in words:
        try:
            ins = translate(w, 0x1000)
        except Unsupported:
            continue
        if ins.kind != 'op' or ins.tag in SKIP or (want and ins.tag not in want):
            continue
        if any(bits(w, h, h - 4) == 18 for h in (4, 9, 14, 20)):
            continue
        if bits(w, 4, 0) == 31 and ins.tag in WRITES_SP:
            continue
        lst = by_tag[ins.tag]
        if len(lst) < args.per_tag:
            lst.append((w, ins))
    tests = [t for lst in by_tag.values() for t in lst]
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, 'tests.S'), 'w') as f:
        f.write('.text\n')
        for i, (w, _) in enumerate(tests):
            f.write(stub(i, w))
    with open(os.path.join(args.out, 'tests.c'), 'w') as f:
        f.write('#include "difftest.h"\n\n')
        for i, (w, ins) in enumerate(tests):
            f.write(f'void nat_{i}(void);\nstatic void tr_{i}(CPU *c) {{ Flags F = c->fl; {{ {ins.c} }} c->fl = F; }}\n')
        f.write('\nconst Test tests[] = {\n')
        for i, (w, ins) in enumerate(tests):
            mem = int(bits(w, 27, 27) == 1 and bits(w, 25, 25) == 0)
            f.write(f'  {{nat_{i}, tr_{i}, {w:#010x}, "{ins.tag}", {mem}, {bits(w, 9, 5)}}},\n')
        f.write('};\n')
        f.write(f'const int ntests = {len(tests)};\n')
    print(f'{len(tests)} tests over {len(by_tag)} tags')


if __name__ == '__main__':
    main()

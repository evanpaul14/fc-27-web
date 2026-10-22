"""Recompile guest functions to C.

Discovers each function's body by recursive descent from its start, resolves
switch jump tables, and emits one C function per guest function
(`void F_<addr>(CPU *c)`), split into translation units, plus a sorted
dispatch table for indirect calls.

Which functions get built:
  --all           every known function start (tools/analyze.py) of every module
  default         closure from the entry points over direct calls, tail calls
                  and code-referenced function/vtable addresses
Functions named in recomp/{hle,nvn}/*.c with HLE(symbol) are not translated; calls
to them go to the host implementation instead.

usage: python3 -I recomp/build.py build [--all] [--extra-roots FILE]
"""
import argparse, bisect, collections, glob, hashlib, json, multiprocessing, os, re, struct, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from a64 import translate, Unsupported, bits, sx  # noqa: E402
import regs  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PC_RELATIVE = {'adr', 'adrp', 'ldr_lit', 'ldrsw_lit', 'ldr_lit_v', 'svc'}

# Instructions replaced by C of our own: {address: (expected word, C)}. The word check keeps a patch from landing on
# other code if the image changes.
PATCHES = {
    # The frame pacer's period: `fcsel d0, d10, d9, ne` picks 33.3 ms (30 Hz) or 16.7 ms by a mode flag, then sleeps
    # what's left of it each frame. host_frame_period_ms applies FIFA_FPS / the `fps` control command.
    0x0ad4b4d8: (0x1e691d40, '{ extern double host_frame_period_ms(double); V128 R = {0}; R.fd[0] = host_frame_period_ms(!F.zf ? c->v[10].fd[0] : c->v[9].fd[0]); '
                             'c->v[0] = R; }'),
    # The main loop's own limiter (F_119e3e70, sleeps at 119e4080): period 1 / rate with `ldr d0, [x0, #8]` the rate
    # in Hz (30), a second 30 fps lock under the pacer's. host_rate_hz applies the same FIFA_FPS / `fps` setting.
    0x119e3f0c: (0xfd400400, '{ extern double host_rate_hz(double); V128 R = {0}; R.d[0] = LD64(X0 + 8ull); '
                             'R.fd[0] = host_rate_hz(R.fd[0]); c->v[0] = R; }'),
}
BUCKET_SHIFT = 18  # 256 KB of code per translation unit
ROOT = os.path.dirname(HERE)
RO_MODULES = {'nro'}  # loaded through nn::ro::LoadModule


def code_pointer_starts(img, base, t0, t1):
    """Functions only reached through a pointer built with adr or adrp+add
    (e.g. one placed right after a call to a noreturn function, which the
    start analysis takes for the caller's tail, or a leaf right after another
    function's final branch). adr targets that are jump table bases (the adr
    register is added to a scaled table entry) are skipped; the rest are kept
    when they begin with a prologue or follow an unconditional terminator (or the
    store of 0xdeadc0de to address 0 that some crash paths end with)."""
    import numpy as np
    w = np.frombuffer(img, dtype='<u4', count=(t1 - base) // 4).astype(np.int64)
    pc = base + np.arange(len(w), dtype=np.int64) * 4
    imm = (((w >> 5) & 0x7ffff) << 2) | ((w >> 29) & 3)
    imm = np.where(imm & (1 << 20), imm - (1 << 21), imm)
    adr = np.nonzero((w & 0x9f000000) == 0x10000000)[0]
    jt_base = np.zeros(len(adr), dtype=bool)
    for k in range(1, 5):  # add xD, xADR, xM, lsl/sxtw #s
        j = np.minimum(adr + k, len(w) - 1)
        x = w[j]
        jt_base |= ((((x >> 21) & 0x7ff) == 0b10001011000) | (((x >> 21) & 0x7ff) == 0b10001011001)) \
            & (((x >> 5) & 31) == (w[adr] & 31))
    adr = adr[~jt_base]
    targets = [pc[adr] + imm[adr]]
    adrp = np.nonzero((w & 0x9f000000) == 0x90000000)[0]
    page = (pc[adrp] & ~0xfff) + (imm[adrp] << 12)
    for k in range(1, 4):  # add xD, xN, #imm using the adrp's register
        ok = adrp + k < len(w)
        x = w[adrp[ok] + k]
        add = ((x & 0xffc00000) == 0x91000000) & (((x >> 5) & 31) == (w[adrp[ok]] & 31))
        targets.append((page[ok] + ((x >> 10) & 0xfff))[add])
    t = np.unique(np.concatenate(targets))
    t = t[(t > t0) & (t < t1) & (t % 4 == 0)]
    first, prev = w[(t - base) // 4], w[(t - base) // 4 - 1]
    prev2 = w[(t - base) // 4 - 2]
    prologue = (((first & 0xffc07fff) == 0xa9807bfd)     # stp x29, x30, [sp, #-n]!
                | ((first & 0xff0003ff) == 0xd10003ff)   # sub sp, sp, #n
                | (first == 0xd503237f))                 # pacibsp
    after_end = ((prev == 0xd65f03c0)                    # ret
                 | ((prev & 0xfc000000) == 0x14000000)   # b
                 | ((prev & 0xfffffc1f) == 0xd61f0000)   # br
                 | ((prev & 0xffff0000) == 0)            # udf
                 | ((prev & 0xffe0001f) == 0xd4200000)   # brk
                 # crash idiom: movk wK, #0xdead, lsl #16; str wK, [xN] (xN = 0)
                 | (((prev & 0xffc00000) == 0xb9000000) & ((prev2 & 0xffffffe0) == 0x72bbd5a0)))
    return set(int(a) for a in t[prologue | after_end])


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
        ext = self.layout.get('extracted', os.path.join(ROOT, 'extracted'))
        for base, end, img, t0, t1, m in self.mods:
            adir = os.path.join(ext, 'analysis', m['name'])
            for line in open(os.path.join(adir, 'functions.tsv')):
                self.starts.add(base + int(line.split('\t', 1)[0], 16))
            self.starts.update(code_pointer_starts(img, base, t0, t1))
            elf = os.path.join(ext, 'code', m['name'] + '.elfsyms.tsv')
            if os.path.exists(elf):  # FUNC symbols of an unstripped ELF (tools/elfsyms.py)
                for line in open(elf):
                    nm, val, sz, shn, inf = line.rstrip('\n').split('\t')
                    if int(inf) & 15 == 2 and shn != '0' and int(sz):
                        self.starts.add(base + int(val, 16))
            for line in open(os.path.join(adir, 'vtables.tsv')):
                a, n, slots = line.rstrip('\n').split('\t')
                self.vtables[base + int(a, 16)] = [base + int(s, 16) for s in slots.split(',') if s.startswith('0x')]
        self.vt_sorted = sorted(self.vtables)
        # setjmp, directly or through a PLT stub (adrp x16; ldr x17, [x16, #o]; add x16, x16, #o; br x17)
        sj = {self.by_name[n] for n in ('setjmp', '_setjmp', '__setjmp', 'sigsetjmp') if n in self.by_name}
        self.setjmp_calls = set(sj)
        for f in self.starts:
            if not self.is_text(f) or not self.is_text(f + 12):
                continue
            w0, w1, w3 = self.word(f), self.word(f + 4), self.word(f + 12)
            if (w0 & 0x9f00001f) == 0x90000010 and (w1 & 0xffc003ff) == 0xf9400211 and w3 == 0xd61f0220:
                page = (f & ~0xfff) + (sx((bits(w0, 23, 5) << 2) | bits(w0, 30, 29), 21) << 12)
                raw = self.read(page + (bits(w1, 21, 10) << 3), 8)
                if raw and int.from_bytes(raw, 'little') in sj:
                    self.setjmp_calls.add(f)
        self.cache = {}
        self.wcache = {}

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
        if r is None and a in PATCHES:
            w, code = PATCHES[a]
            if self.word(a) != w:
                sys.exit(f'patch at {a:#x}: word {self.word(a):#x}, expected {w:#x}')
            from a64 import Insn
            r = self.cache[a] = Insn('op', code, tag='patch')
        if r is None:
            w = self.word(a)
            r = self.wcache.get(w)
            if r is None:
                try:
                    r = translate(w, a)
                except Unsupported as e:
                    from a64 import Insn
                    r = Insn('trap', f'a64_trap(c, {a:#x}, {w:#x}); return;', tag='unsupported:' + str(e))
                # Most encodings mean the same thing at any address; share those.
                if r.kind in ('op', 'br', 'blr', 'ret') and r.tag not in PC_RELATIVE:
                    self.wcache[w] = r
            self.cache[a] = r
        return r

    def name(self, a):
        n = self.syms.get(a)
        return n[0] if n else None


# ------------------------------------------------------------ jump tables

def reg_writes(w):
    """Destination register of the simple integer instructions we track."""
    return bits(w, 4, 0)


def writes_vreg(w):
    """SIMD&FP data processing whose destination is a V register, not a GPR (its
    Rd field must not be read as one)."""
    if bits(w, 27, 25) != 0b111:
        return False
    if bits(w, 30, 29) == 0 and bits(w, 28, 24) == 0b11110 and bits(w, 21, 21) and bits(w, 15, 10) == 0:
        return bits(w, 18, 16) in (2, 3, 7)  # FP<->int: scvtf, ucvtf, fmov to FP write V; the rest a GPR
    if not bits(w, 31, 31) and bits(w, 29, 21) == 0b001110000 and not bits(w, 15, 15) and bits(w, 10, 10):
        return bits(w, 14, 11) not in (0b0101, 0b0111)  # copy: smov/umov write a GPR
    return True


def resolve_jump_table(p, br_pc, body, jt=None):
    """Return the possible targets of the `br` at br_pc, or None. jt: the
    function's already resolved tables (br -> targets), edges for cfg_const."""
    w = p.word(br_pc)
    r = bits(w, 9, 5)
    seq = []  # (pc, word) walking backwards through straight-line code
    a = br_pc - 4
    while len(seq) < 64:
        if a not in body:
            break
        seq.append((a, p.word(a)))
        if a - 4 not in body or p.insn(a - 4).kind in ('b', 'br', 'ret', 'trap'):
            # not entered by falling through: continue from a branch to it, the
            # nearest one above if there are several
            srcs = [s for s in body if p.insn(s).kind in ('b', 'bcond') and p.insn(s).target == a]
            above = [s for s in srcs if s < a]
            if not srcs:
                break
            a = max(above) if above else min(srcs)
            continue
        a -= 4
    consts = {}

    def find_def(reg, start_idx):
        for k in range(start_idx, len(seq)):
            pc, ww = seq[k]
            if reg_writes(ww) == reg and not is_store(ww) and not is_cmp(ww) and not is_branch(ww) \
                    and not writes_vreg(ww):
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

    preds = None

    def cfg_const(reg, use_pc):
        """reg's value at use_pc when the definitions reaching it backwards through
        the body (fall-through and direct branches) give one adrp(+add #imm)
        constant: a table base kept in a register set up far from the br. Paths
        through other definitions (the register reused elsewhere in a loop) are
        ignored; a wrong base only adds case labels, since the emitted switch
        dispatches on the target computed at run time."""
        nonlocal preds
        if preds is None:
            preds = collections.defaultdict(list)
            for s in body:
                ins = p.insn(s)
                if ins.kind in ('b', 'bcond') and ins.target in body:
                    preds[ins.target].append(s)
                if ins.kind not in ('b', 'br', 'ret', 'trap') and s + 4 in body:
                    preds[s + 4].append(s)
            for s, ts in (jt or {}).items():
                for t in ts or ():
                    preds[t].append(s)
        vals, seen, work = set(), set(), [(use_pc, 0)]
        while work:
            a, off = work.pop()
            for s in preds.get(a, ()):
                if (s, off) in seen:
                    continue
                seen.add((s, off))
                if len(seen) > 4000:
                    return None
                ww, ins = p.word(s), p.insn(s)
                if ins.kind in ('bl', 'blr') and reg < 19:
                    return None  # clobbered by the call
                if is_branch(ww) or is_store(ww) or is_cmp(ww) or writes_vreg(ww) or reg_writes(ww) != reg:
                    work.append((s, off))
                elif bits(ww, 31, 31) and bits(ww, 28, 24) == 0b10000:  # adrp
                    vals.add((s & ~0xfff) + (sx((bits(ww, 23, 5) << 2) | bits(ww, 30, 29), 21) << 12) + off)
                elif bits(ww, 31, 22) == 0b1001000100 and bits(ww, 9, 5) == reg:  # add xR, xR, #imm
                    work.append((s, off + bits(ww, 21, 10)))
                if len(vals) > 1:
                    return None
        return vals.pop() if len(vals) == 1 else None

    table = const_of(rt, k_ld + 1)
    if table is None:
        table = cfg_const(rt, seq[k_ld][0])
    base = table if rb == rt else const_of(rb, k_add + 1)
    if table is None or base is None:
        return None
    # bound: cmp wI/xI, #N (subs with rd=31) somewhere before, followed by b.hi/b.hs
    # (or b.ls/b.lo) on the index register or one it was copied from (mov wI, wS).
    # Above a mov only its source holds the index, and above any other write to it
    # the register is something else (0x4c3b8fc in the update: a `cmp w10, #1`
    # above `mov w10, w14` cut a 14-entry table to 2). `and xI, xS, #2^k-1` bounds
    # the index itself, and a cmp on a register later set by the same `and` of xS
    # counts too (and w8, w1, #0xff; cmp w8, #0x15; b.hi; and x8, x1, #0xff). The
    # walk follows seq (fall-through and branches into each block), not addresses.
    n = None
    idx = ri
    pending = {}    # register -> bound from a cmp on it, if it turns out to equal the index
    for a, ww in seq[k_ld + 1:]:  # back along the path from the table load, where ri is the index
        if bits(ww, 30, 23) == 0b11100010 and bits(ww, 4, 0) == 31 and not bits(ww, 22, 22):
            cond = bound_cond(p, body, a + 4)
            if cond is not None:
                bound = bits(ww, 21, 10) + (0 if cond in (2, 3) else 1)
                if bits(ww, 9, 5) == idx:
                    n = bound if n is None else min(n, bound)
                    break
                pending.setdefault(bits(ww, 9, 5), bound)
        rd = reg_writes(ww) if not (is_store(ww) or is_cmp(ww) or is_branch(ww) or writes_vreg(ww)) else None
        low = low_mask(ww)
        if rd is not None and rd in pending and rd != idx:
            if low and bits(ww, 9, 5) == idx:  # the cmp was on idx & mask, and idx indexes the table unmasked
                n = pending[rd] if n is None else min(n, pending[rd])
                break
            del pending[rd]
        if bits(ww, 30, 21) == 0b0101010000 and bits(ww, 15, 5) == 31 and bits(ww, 4, 0) == idx:  # mov (orr Rd, zr, Rm)
            idx = bits(ww, 20, 16)
            if idx in pending:  # mov w25, w0; cmp w0, #0x12; b.hi; mov w8, w25
                n = pending[idx] if n is None else min(n, pending[idx])
                break
        elif rd == idx:
            if not low:
                break
            n = low[0] if n is None else min(n, low[0])
            idx = bits(ww, 9, 5)
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


def bound_cond(p, body, a):
    """Condition of the b.hs/b.lo/b.hi/b.ls that the flags set just before a
    reach: the next branch after up to 32 instructions that leave the flags
    alone, or one in the run of b.cond it starts (cmp; b.gt; b.hi)."""
    for _ in range(32):
        if a not in body:
            return None
        w = p.word(a)
        if is_branch(w):
            if bits(w, 30, 25) in (0b011010, 0b011011):  # cbz/cbnz, tbz/tbnz: flags untouched
                a += 4
                continue
            if bits(w, 31, 24) != 0x54:
                return None
            if bits(w, 3, 0) in (2, 3, 8, 9):
                return bits(w, 3, 0)
        elif sets_flags(w):
            return None
        a += 4
    return None


def low_mask(w):
    """(2^k, imms) for and Rd, Rn, #2^k-1, else None."""
    if bits(w, 30, 23) != 0b00100100 or bits(w, 21, 16):  # and (immediate), immr = 0
        return None
    sf, n, imms = bits(w, 31, 31), bits(w, 22, 22), bits(w, 15, 10)
    if (sf and n and imms < 63) or (not sf and not n and imms < 31):
        return (1 << (imms + 1), imms)
    return None


def sets_flags(w):
    """ands/tst, adds/subs/cmp/cmn (also with carry), ccmp/ccmn, fcmp/fccmp."""
    if bits(w, 28, 23) == 0b100100 or bits(w, 28, 24) == 0b01010:  # logical (immediate / shifted register)
        return bits(w, 30, 29) == 3
    if bits(w, 28, 24) in (0b10001, 0b01011) or bits(w, 28, 21) == 0b11010000:  # add/sub (imm / reg / carry)
        return bool(bits(w, 29, 29))
    if bits(w, 28, 21) == 0b11010010:  # ccmp / ccmn
        return True
    return bits(w, 31, 24) == 0x1E and bool(bits(w, 21, 21)) and (bits(w, 13, 10) == 0b1000 or bits(w, 11, 10) == 1)


def is_store(w):
    """Loads/stores whose Rt is not a GPR destination: stores, and SIMD&FP loads."""
    if bits(w, 27, 27) != 1 or bits(w, 25, 25) != 0:
        return False
    if bits(w, 26, 26):
        return True  # SIMD&FP: Rt is a V register
    cls = bits(w, 29, 28)
    if cls == 0b11:  # register forms: opc 00 = store; 01/10/11 = loads (10/11 sign-extending)
        return bits(w, 23, 22) == 0
    if cls == 0b01:  # ldr (literal)
        return False
    return bits(w, 22, 22) == 0  # pairs and exclusives: L bit


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
    # can add code containing further tables, and paths that let a failed
    # table resolve on a later round
    while True:
        todo = [a for a, v in fn.jt.items() if not v]
        progress = False
        for a in todo:
            ts = resolve_jump_table(p, a, fn.body, fn.jt) or []
            progress |= fn.jt[a] is None or bool(ts)
            fn.jt[a] = ts
            fn.labels.update(ts)
            walk(p, fn, ts)
        if not progress:
            break
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
        elif bits(w, 31, 22) == 0b1111100101 and bits(w, 9, 5) in regs:  # ldr xT, [xN, #imm]: GOT/PLT slot
            raw = p.read(regs[bits(w, 9, 5)] + (bits(w, 21, 10) << 3), 8)
            if raw:
                out.add(int.from_bytes(raw, 'little'))
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


RESTRICT = True  # --no-restrict
LOCALS = True  # --no-locals
# Only the X registers and sp become locals: V registers live across calls spill whole q registers on the host
# (only d8-d15's low halves are callee-saved), which made large functions up to ~40% bigger.
LOCAL_MASK = regs.xs(0, 31)
# Debug: FIFA_NO_LOCALS=lo-hi[,lo-hi...] (hex) emits the functions starting there with registers in the CPU struct.
NO_LOCALS = [tuple(int(x, 16) for x in r.split('-')) for r in os.environ.get('FIFA_NO_LOCALS', '').split(',') if r]
CHUNK = 4000  # functions above 1.5x this are split into chunk functions


def direct(t):
    """A call to t goes to a translated function (one with a register summary)."""
    return t in _BUILT and t not in _HLE


def emit_func(p, fn, built, hle, decls):
    """C for one guest function. Very large functions are split into chunks
    `uint64_t F_x_k(CPU *c, uint64_t pc)` that run until control leaves the
    chunk and return the next guest address (0 = return), driven by a loop."""
    nm = p.name(fn.start)
    head = f'/* {nm} */\n' if nm else ''
    body = sorted(fn.body)
    if len(body) <= CHUNK * 3 // 2:
        lines = emit_region(p, fn, body, built, hle, decls, None)
        if body[0] != fn.start:  # the body reaches code below the entry (b to a non-start)
            lines.insert(0, f'  goto L_{fn.start:x};')
        jbs = ''.join(f'  jmp_buf JB_{a:x};\n' for a in body
                      if p.insn(a).kind == 'bl' and p.insn(a).target in p.setjmp_calls)
        if LOCALS and not jbs and not any(lo <= fn.start < hi for lo, hi in NO_LOCALS):
            # guest X registers in locals X0..X31 (regs.py); `c` is synced at calls and returns
            blocks, entry = regs.cfg(p, fn, direct)
            pl = regs.plan(blocks, entry, _SUMM, _SUMM[fn.start][1])
            lines = emit_region(p, fn, body, built, hle, decls, None, pl)
            if body[0] != fn.start:
                lines.insert(0, f'  goto L_{fn.start:x};')
            text = '\n'.join(lines)
            used = sorted({int(m[1]) for m in re.finditer(r'\bX(\d\d?)\b', text)})
            dec = '  uint64_t ' + ', '.join(f'X{i} = c->x[{i}]' for i in used) + ';\n' if used else ''
            return (head + f'WEAK void {cname(fn.start)}(CPU *restrict c) {{\n  uint8_t *const M = mem;\n'
                    f'  Flags F = {{0}};\n' + dec + text + '\n}')
        # restrict: guest stores (through M) never reach the CPU struct, so guest registers can stay in host
        # registers between calls. Not with a setjmp: longjmp would bring back host registers cached before it.
        cq = 'CPU *' if jbs or not RESTRICT else 'CPU *restrict '
        return (head + f'WEAK void {cname(fn.start)}({cq}c) {{\n  uint8_t *const M = mem;\n  Flags F = {{0}};\n'
                + jbs + '\n'.join(lines) + '\n}')
    chunks = [body[i:i + CHUNK] for i in range(0, len(body), CHUNK)]
    out = []
    for k, ch in enumerate(chunks):
        inside = set(ch)
        entries = sorted({ch[0]} | (fn.labels & inside))
        lines = emit_region(p, fn, ch, built, hle, decls, inside)
        if any(p.insn(a).kind == 'bl' and p.insn(a).target in p.setjmp_calls for a in ch):
            raise ValueError(f'setjmp call in chunked function {fn.start:#x}: its jmp_buf would not outlive the chunk')
        cases = ' '.join(f'case {t:#x}ull: goto L_{t:x};' for t in entries)
        out.append(f'static uint64_t {cname(fn.start)}_{k}(CPU *{"restrict " if RESTRICT else ""}c, uint64_t pc) {{\n'
                   f'  uint8_t *const M = mem;\n  Flags F = c->fl;\n'
                   f'  switch (pc) {{ {cases} default: a64_bad_jump(c, {fn.start:#x}ull, pc); return 0; }}\n'
                   + '\n'.join(lines) + '\n  return 0;\n}')
    disp = ' else '.join(f'if (pc < {chunks[k + 1][0]:#x}ull) pc = {cname(fn.start)}_{k}(c, pc);'
                         for k in range(len(chunks) - 1))
    disp += f' else pc = {cname(fn.start)}_{len(chunks) - 1}(c, pc);'
    out.append(head + f'WEAK void {cname(fn.start)}(CPU *c) {{\n  uint64_t pc = {fn.start:#x}ull;\n'
               f'  while (pc) {{ {disp} }}\n}}')
    return '\n\n'.join(out)


def emit_region(p, fn, addrs, built, hle, decls, inside, pl=None):
    """Statements for the instructions at addrs. inside is None for a whole
    function, else the chunk's address set: jumps leaving it return the target.
    pl: guest registers in locals, regs.plan's stores/reloads per site."""
    out = []
    ret = 'return;' if inside is None else 'return 0;'
    if pl is None:
        def st(key): return ''
        def rl(key): return ''
        def C(code): return code
    else:
        def st(key): return regs.stores(pl.get(key, (0, 0))[0] & LOCAL_MASK).lstrip()
        def rl(key):
            e = pl.get(key, (0, 0))
            chk = ''.join(f' if (c->x[{i}] != X{i}) a64_locals_check(c, {key[0]:#x}ull, {i}, X{i});'
                          for i in range(32) if len(e) > 2 and e[2] >> i & 1)
            return regs.reloads(e[1] & LOCAL_MASK) + chk
        C = regs.rename

    def call(t):
        if t in built:
            decls.add(f'void {cname(t)}(CPU *c);')
            return f'{cname(t)}(c);'
        return f'a64_call(c, {t:#x}ull);'

    def jump(t):
        if inside is None or t in inside:
            return f'goto L_{t:x};'
        return f'{{ c->fl = F; return {t:#x}ull; }}'

    def local(t):
        return t in fn.labels and t in fn.body

    for a in addrs:
        if a in fn.labels or (inside is not None and a == addrs[0]):
            out.append(f'L_{a:x}:;')
        ins = p.insn(a)
        k = ins.kind
        if k == 'op':
            if ins.c:
                if pl is not None and 'a64_svc(' in ins.c:
                    out.append(f'  {st((a, "s"))} {C(ins.c)}{rl((a, "s"))}')
                else:
                    out.append('  ' + C(ins.c))
        elif k == 'b':
            t = ins.target
            out.append(f'  {jump(t)}' if local(t) else f'  {st((a, "t"))} {call(t)} {ret}')
        elif k == 'bcond':
            t = ins.target
            if local(t):
                out.append(f'  if ({C(ins.c)}) {jump(t)}')
            else:
                out.append(f'  if ({C(ins.c)}) {{ {st((a, "t"))} {call(t)} {ret} }}')
        elif k == 'bl' and ins.target in p.setjmp_calls:
            # setjmp returns twice: the host context is taken here, in the caller's
            # frame, and recomp/hle/setjmp.c's longjmp comes back to it
            out.append(f'  c->x[30] = {a + 4:#x}ull; if (!_setjmp(JB_{a:x})) {{ a64_pending_jb = &JB_{a:x}; {call(ins.target)} }}')
        elif k == 'bl':
            if pl is None:
                out.append(f'  c->x[30] = {a + 4:#x}ull; {call(ins.target)}')
            else:
                out.append(f'  X30 = {a + 4:#x}ull; {st((a, "c"))} {call(ins.target)}{rl((a, "c"))}')
        elif k == 'blr':
            out.append(f'  {{ uint64_t t = {C(ins.c)}; {C("c->x[30]")} = {a + 4:#x}ull; {st((a, "c"))} a64_call(c, t);'
                       f'{rl((a, "c"))} }}')
        elif k == 'br':
            ts = fn.jt.get(a)
            if ts:
                cases = ' '.join(f'case {t:#x}ull: {jump(t)}' if local(t) else
                                 f'case {t:#x}ull: {{ {st((a, t))} {call(t)} {ret} }}' for t in ts)
                out.append(f'  {{ uint64_t t = {C(ins.c)}; switch (t) {{ {cases} default: a64_bad_jump(c, {a:#x}ull, t); {ret} }} }}')
            else:
                out.append(f'  {{ uint64_t t = {C(ins.c)}; {st((a, "t"))} a64_call(c, t); {ret} }}')
        elif k == 'ret':
            out.append(f'  {st((a, "x"))} {ret}')
        else:
            out.append(f'  {st((a, "x"))} ' + C(ins.c).replace('return;', ret))
        falls_through = k in ('op', 'bl', 'blr', 'bcond')
        if a in fn.falls:
            t = fn.falls[a]
            if falls_through:
                if p.is_text(t):
                    out.append(f'  {st((a, "f"))} {call(t)} {ret}')
                else:
                    out.append(f'  {st((a, "f"))} a64_trap(c, {t:#x}ull, 0); {ret}')
        elif falls_through and inside is not None and a == addrs[-1]:
            out.append(f'  c->fl = F; return {a + 4:#x}ull;')
    return out


def load_hle(p):
    hle = {}
    for path in glob.glob(os.path.join(HERE, 'hle', '*.c')) + glob.glob(os.path.join(HERE, 'nvn', '*.c')):
        for m in re.finditer(r'^HLE\((\w+)\)', open(path).read(), re.M):
            a = p.by_name.get(m.group(1))
            if a is None and re.fullmatch(r'sub_[0-9a-f]+', m.group(1)):
                a = int(m.group(1)[4:], 16)  # stripped function, by guest address
            if a is None:
                print(f'warning: HLE symbol {m.group(1)} not found')
                continue
            hle[a] = m.group(1)
    return hle


_P = _BUILT = _HLE = _GDIR = None  # shared with forked workers
_SUMM = {}  # register summaries (regs.py): function -> (R, W)


def closure(p, roots, hle):
    """Functions reachable from roots via calls, tail calls and code references."""
    funcs = {}
    work = sorted(roots)
    seen = set(work)
    while work:
        f = work.pop()
        if f in hle or not p.is_text(f):
            continue
        fn = discover(p, f)
        funcs[f] = fn
        for t in set(fn.calls) | code_refs(p, fn):
            if t not in seen and p.is_text(t):
                p.starts.add(t)
                seen.add(t)
                work.append(t)
    return funcs


def write_bucket(job):
    key, fs, funcs = job
    p = _P
    if funcs is None:
        funcs = {f: discover(p, f) for f in fs}
    decls = set(f'void {cname(f)}(CPU *c);' for f in fs)
    bodies = [emit_func(p, funcs[f], _BUILT, _HLE, decls) for f in fs]
    name = bucket_name(key)
    path = os.path.join(_GDIR, name)
    text = '#include "gen.h"\n' + '\n'.join(sorted(decls)) + '\n\n' + '\n\n'.join(bodies) + '\n'
    if not os.path.exists(path) or open(path).read() != text:
        open(path, 'w').write(text)
    st = collections.Counter(insns=sum(len(f.body) for f in funcs.values()),
                             jt=sum(len(f.jt) for f in funcs.values()),
                             jt_ok=sum(1 for f in funcs.values() for v in f.jt.values() if v))
    uns = collections.Counter(p.cache[a].tag for f in funcs.values() for a in f.body
                              if p.cache[a].tag.startswith('unsupported'))
    return name, st, uns


def direct_callees(fn):
    """Translated functions fn calls or jumps to (their summaries shape fn's code)."""
    ts = set(fn.calls)
    for v in fn.jt.values():
        ts.update(v or ())
    return sorted(t for t in ts if direct(t))


def scan_callees(fs):
    _P.cache.clear()  # per-address translations: a worker sees most of the program, keep its memory down
    return [(f, direct_callees(discover(_P, f))) for f in fs]


def summarize_sccs(job):
    """Summaries of a list of (SCC, recursive) whose outside callees are in
    `known`. A recursive SCC iterates from (0, 0) up to the least fixed point."""
    sccs, known = job
    _P.cache.clear()
    summ = dict(known)
    out = {}
    for scc, rec in sccs:
        cfgs = [(f, regs.cfg(_P, discover(_P, f), direct)) for f in scc]
        while True:
            changed = False
            for f, (blocks, entry) in cfgs:
                s = regs.summarize(blocks, entry, summ)
                if s != summ.get(f, (0, 0)):
                    summ[f] = s
                    changed = True
            if not changed or not rec:
                break
        for f in scc:
            out[f] = summ.get(f, (0, 0))
    return out


def sccs_by_level(order, callees):
    """Strongly connected components of the call graph (Tarjan, iterative),
    grouped by level: a component's callees are all in lower levels."""
    index, low, on, stack, comp = {}, {}, set(), [], {}
    comps = []
    n = 0
    for root in order:
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, i = work[-1]
            if i == 0:
                index[v] = low[v] = n
                n += 1
                stack.append(v)
                on.add(v)
            cs = callees.get(v, ())
            if i < len(cs):
                work[-1] = (v, i + 1)
                w = cs[i]
                if w not in index:
                    work.append((w, 0))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
            if low[v] == index[v]:
                c = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp[w] = len(comps)
                    c.append(w)
                    if w == v:
                        break
                comps.append(c)
    # Tarjan emits components callees-first
    level = [0] * len(comps)
    for ci, c in enumerate(comps):
        lv = 0
        for f in c:
            for t in callees.get(f, ()):
                if comp[t] != ci:
                    lv = max(lv, level[comp[t]] + 1)
        level[ci] = lv
    by = collections.defaultdict(list)
    for ci, c in enumerate(comps):
        by[level[ci]].append(c)
    return [by[k] for k in sorted(by)]


def summaries(order, buckets, key, gdir, pool):
    """Callees and register summaries of every function, cached in gen/summaries.pkl under key."""
    import pickle
    path = os.path.join(gdir, 'summaries.pkl')
    if os.path.exists(path):
        try:
            d = pickle.load(open(path, 'rb'))
            if d['key'] == key:
                return d['callees'], d['summ']
        except Exception:
            pass
    t0 = time.time()
    callees = {}
    for res in pool.imap_unordered(scan_callees, [fs for fs in buckets.values()]):
        callees.update(res)
    levels = sccs_by_level(order, callees)
    t1 = time.time()
    summ = {}
    for lv in levels:
        # chunks of ~equal size; each carries the summaries its SCCs' outside callees have
        n = max(1, min(len(lv) // (4 * os.cpu_count()), 256))
        jobs = []
        for i in range(0, len(lv), n):
            ch = [(scc, len(scc) > 1 or scc[0] in callees[scc[0]]) for scc in lv[i:i + n]]
            need = {t for scc, _ in ch for f in scc for t in callees[f]}
            jobs.append((ch, {t: summ[t] for t in need if t in summ}))
        if len(jobs) == 1:
            res = [summarize_sccs(jobs[0])]
        else:
            res = pool.imap_unordered(summarize_sccs, jobs)
        for r in res:
            summ.update(r)
    print(f'register summaries: {len(summ)} functions, {len(levels)} levels '
          f'(callees {t1 - t0:.1f}s, summaries {time.time() - t1:.1f}s)')
    pickle.dump({'key': key, 'callees': callees, 'summ': summ}, open(path, 'wb'))
    return callees, summ


def bucket_name(key):
    return f'f_{key << BUCKET_SHIFT:08x}.c'


def inputs_hash(bdir):
    """Everything that can change generated code apart from function starts."""
    h = hashlib.sha1()
    for f in ('a64.py', 'a64_simd.py', 'build.py', 'regs.py'):
        h.update(open(os.path.join(HERE, f), 'rb').read())
    for f in sorted(glob.glob(os.path.join(bdir, '*.img'))) + [os.path.join(bdir, 'symbols.tsv')]:
        st = os.stat(f)
        h.update(f'{f}:{st.st_size}:{st.st_mtime_ns}'.encode())
    return h.hexdigest()


def write_if_changed(path, text):
    if not os.path.exists(path) or open(path).read() != text:
        open(path, 'w').write(text)


def table_c(p, order):
    out = ['#include "gen.h"']
    out += [f'void {cname(f)}(CPU *c);' for f in order]
    out.append('const A64Fn a64_fn_table[] = {')
    out += [f'  {{{f:#x}ull, {cname(f)}}},' for f in order]
    out.append('};')
    out.append(f'const unsigned a64_fn_count = {len(order)};')
    out.append('const A64Sym a64_missing[] = {')
    out += [f'  {{{a:#x}ull, "{nm}"}},' for a, nm in sorted(p.missing.items())]
    out.append('};')
    out.append(f'const unsigned a64_missing_count = {len(p.missing)};')
    return '\n'.join(out) + '\n'


def write_hle_table(gdir, hle):
    """Strong definitions of F_<addr> for host replacements; they override the
    weak translated functions at link time, so no regeneration is needed."""
    out = ['#include "gen.h"']
    for a, nm in sorted(hle.items()):
        out.append(f'void hle_{nm}(CPU *c);\nvoid {cname(a)}(CPU *c) {{ hle_{nm}(c); }}')
    write_if_changed(os.path.join(gdir, 'hle_table.c'), '\n'.join(out) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bdir')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--extra-roots', default=None)
    ap.add_argument('--force', action='store_true', help='regenerate every unit')
    ap.add_argument('--hle-only', action='store_true', help='only rewrite hle_table.c')
    ap.add_argument('--no-restrict', action='store_true',
                    help='plain CPU *c: guest registers exact in CPU at a fault (crash dumps), slower')
    ap.add_argument('--check-locals', action='store_true',
                    help='store all changed locals before calls and check the ones the callee keeps after them')
    ap.add_argument('--no-locals', action='store_true',
                    help='guest registers in the CPU struct throughout instead of C locals (regs.py)')
    args = ap.parse_args()
    t0 = time.time()
    p = Program(args.bdir)
    hle = load_hle(p)
    if args.hle_only:
        write_hle_table(os.path.join(args.bdir, 'gen'), hle)
        print(f'{len(hle)} host replacements')
        return

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

    gdir = os.path.join(args.bdir, 'gen')
    os.makedirs(gdir, exist_ok=True)
    global _P, _BUILT, _HLE, _GDIR, _SUMM, RESTRICT, LOCALS
    RESTRICT = not args.no_restrict
    LOCALS = not args.no_locals
    regs.CHECK = args.check_locals
    if args.all:
        order = sorted(f for f in p.starts if p.is_text(f))
        funcs = None
    else:
        funcs = closure(p, roots, {})
        order = sorted(funcs)
    _P, _BUILT, _HLE, _GDIR = p, set(order), hle, gdir
    # Translation units are fixed address buckets, so a change only touches
    # the units it lands in. A bucket is regenerated only when its inputs
    # (generator sources, images, the function starts it and its successor
    # hold) change.
    buckets = collections.defaultdict(list)
    for f in order:
        buckets[f >> BUCKET_SHIFT].append(f)
    base = inputs_hash(args.bdir) + ('' if RESTRICT else ' no-restrict') + ('' if LOCALS else ' no-locals') + (' check' if regs.CHECK else '')
    callees = {}
    if LOCALS:
        # a unit's code depends on the summaries of what its functions call
        skey = base + hashlib.sha1(repr(order).encode()).hexdigest()
        with multiprocessing.get_context('fork').Pool(os.cpu_count()) as pool:
            callees, _SUMM = summaries(order, buckets, skey, gdir, pool)
    keys_path = os.path.join(gdir, 'keys.json')
    old_keys = json.load(open(keys_path)) if os.path.exists(keys_path) and not args.force else {}
    new_keys, jobs = {}, []
    for key, fs in buckets.items():
        h = hashlib.sha1(base.encode())
        h.update(repr((fs, buckets.get(key + 1, [])[:1], funcs is None)).encode())
        if LOCALS:
            h.update(repr(sorted((t, _SUMM[t]) for f in fs for t in callees[f] + [f])).encode())
            h.update(repr([r for r in NO_LOCALS if r[0] < (key + 1) << BUCKET_SHIFT and r[1] > key << BUCKET_SHIFT]).encode())
        name = bucket_name(key)
        new_keys[name] = h.hexdigest()
        if old_keys.get(name) != new_keys[name] or not os.path.exists(os.path.join(gdir, name)):
            jobs.append((key, fs, None if funcs is None else {f: funcs[f] for f in fs}))
    jobs.sort(key=lambda j: -len(j[1]))  # biggest first for load balance
    stats = collections.Counter()
    unsupported = collections.Counter()
    if funcs is None and len(jobs) > 1:
        with multiprocessing.get_context('fork').Pool(os.cpu_count()) as pool:
            results = list(pool.imap_unordered(write_bucket, jobs))
    else:
        results = [write_bucket(j) for j in jobs]
    for name, st, uns in results:
        stats.update(st)
        unsupported.update(uns)
    for old in glob.glob(os.path.join(gdir, 'f_*.c')):
        if os.path.basename(old) not in new_keys:
            os.remove(old)
    json.dump(new_keys, open(keys_path, 'w'))
    tu_i = len(new_keys)
    print(f'{len(order)} functions in {tu_i} units; regenerated {len(jobs)} units: {stats["insns"]} instructions, '
          f'{stats["jt_ok"]}/{stats["jt"]} indirect jumps resolved as tables ({time.time() - t0:.1f}s)')
    write_if_changed(os.path.join(gdir, 'table.c'), table_c(p, order))
    write_hle_table(gdir, hle)
    mods = p.layout['modules']
    layout = ['#include "gen.h"', '#include "../../recomp/rt/host.h"', 'const HostModule host_modules[] = {']
    # modules nn::ro loads at run time (recomp/hle/ro.c) are initialised by LoadModule, not at startup
    layout += [f'  {{"{m["name"]}", {m["base"]:#x}ull, {m["size"]:#x}ull, {m["init"] or 0:#x}ull, '
               f'{int(m["name"] in RO_MODULES)}}},' for m in mods]
    layout += ['};', f'const unsigned host_module_count = {len(mods)};',
               f'const uint64_t host_nn_init_start = {p.by_name["_ZN2nn4init5StartEmmPFvvES2_S2_"]:#x}ull;',
               'const uint64_t host_ro_lookup_slot_ptr = '
               f'{p.by_name["_ZN2nn2ro6detail36g_pLookupGlobalManualFunctionPointerE"]:#x}ull;']
    write_if_changed(os.path.join(gdir, 'layout.c'), '\n'.join(layout) + '\n')
    # (not a make dependency of the units, so additions here don't rebuild them all)
    write_if_changed(os.path.join(gdir, 'gen.h'), '#define MEMB M  /* the local copy of mem in each generated function (cpu.h) */\n'
                     '#include "../../recomp/rt/runtime.h"\n#include <setjmp.h>\n'
                     '/* host context of the setjmp call being made (recomp/hle/setjmp.c) */\n'
                     'extern __thread jmp_buf *a64_pending_jb;\n')
    json.dump({'functions': len(order), 'instructions': stats['insns'], 'tus': tu_i,
               'unsupported': unsupported.most_common(20)},
              open(os.path.join(args.bdir, 'build_stats.json'), 'w'), indent=1)


if __name__ == '__main__':
    main()

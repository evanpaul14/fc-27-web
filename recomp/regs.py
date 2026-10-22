"""Guest registers in C locals (build.py): register summaries per function and
the CPU-struct stores and reloads around calls.

Register sets are bit masks: bit n is xn (n < 31), 31 is sp, 32 + n is vn.
For each built function F, over all paths and through its callees:
  R(F)  registers F may read before writing them
  W(F)  registers that may hold another value than at entry when F returns
A callee-saved register (x19-x29, d8-d15) that F saves to its frame and
restores from the same slot on every return path is unchanged, as is sp back
at its entry offset; callers keep those in host registers across the call.

Protocol for the CPU struct `c`: at a function's entry it holds every guest
register. A function keeps registers in locals and stores the ones it changed
(`dirty`) before a call (only those in R(callee)), an svc and a return (all of
them); after a call it reloads W(callee) where still live. So when a function
returns, `c` holds every register, and callers that read `c` directly (chunked
functions, setjmp callers, the host) see the right values.
Indirect calls, host replacements and calls to unbuilt code follow AAPCS64:
they read ABI_IN and change CLOB (d8-d15's upper halves are not tracked).
"""
import re

from a64 import bits, sx


def xs(lo, hi):
    return ((1 << (hi + 1)) - 1) & ~((1 << lo) - 1)


def vs(lo, hi):
    return xs(lo + 32, hi + 32)


SP, X29, X30 = 1 << 31, 1 << 29, 1 << 30
ALL = (1 << 64) - 1
ABI_IN = xs(0, 8) | xs(18, 31) | vs(0, 15)
CLOB = xs(0, 18) | X30 | vs(0, 7) | vs(16, 31)
TRACK = xs(19, 29) | vs(8, 15)  # callee-saved: preserved when restored from their save slot
SVC_W = xs(0, 18)
CHECK = False  # build.py --check-locals

_REG = re.compile(r'c->(x|v)\[(\d+)\]((?:\.\w+\[[^\]]*\])*)(\s*=(?!=))?')
_eff = {}


def effects(code):
    """(uses, defs) of the guest registers in an instruction's C. A write to an
    element of a V register (`c->v[n].d[1] = `) also reads the rest of it."""
    r = _eff.get(code)
    if r is None:
        uses = defs = 0
        for m in _REG.finditer(code):
            b = 1 << (int(m[2]) + (32 if m[1] == 'v' else 0))
            if m[4]:
                defs |= b
                if m[3]:
                    uses |= b
            else:
                uses |= b
        r = _eff[code] = (uses, defs)
    return r


def rename(code):
    """X registers to their locals (V registers stay in the CPU struct, see build.py LOCAL_MASK)."""
    return _REN.sub(r'X\1', code)


_REN = re.compile(r'c->x\[(\d+)\]')


def names(mask):
    return [('X%d' % i) if i < 32 else ('V%d' % (i - 32)) for i in range(64) if mask >> i & 1]


def stores(mask):
    return ''.join(f' c->{"x" if i < 32 else "v"}[{i % 32}] = {n};' for i, n in zip(
        [i for i in range(64) if mask >> i & 1], names(mask)))


def reloads(mask):
    return ''.join(f' {n} = c->{"x" if i < 32 else "v"}[{i % 32}];' for i, n in zip(
        [i for i in range(64) if mask >> i & 1], names(mask)))


# ------------------------------------------------------------ frame ops

def memop(w):
    """Loads/stores with base sp or x29 and sp/x29 arithmetic, for tracking
    the save slots of callee-saved registers:
      ('ls', load, regs, size, base, off, mode)  regs: bit indices (None: not a
          64-bit X/D register); mode 0 offset, 1 post-index, 2 pre-index
      ('add', rd, rn, delta)  rd in (sp, x29)"""
    if (w & 0x7f800000) in (0x11000000, 0x51000000) and w >> 31 and not bits(w, 29, 29):  # add/sub imm, 64-bit
        rd, rn = bits(w, 4, 0), bits(w, 9, 5)
        if rd in (29, 31):
            imm = bits(w, 21, 10) << (12 if bits(w, 22, 22) else 0)
            return ('add', rd, rn, -imm if bits(w, 30, 30) else imm)
        return None
    if bits(w, 29, 27) == 0b101 and not bits(w, 25, 25):  # load/store pair
        rn = bits(w, 9, 5)
        if rn not in (29, 31):
            return None
        v, opc, mode, load = bits(w, 26, 26), bits(w, 31, 30), bits(w, 24, 23), bits(w, 22, 22)
        size = (16 if opc == 2 else 8 if opc == 1 else 4) if v else (8 if opc == 2 else 4)
        mode = {1: 1, 2: 0, 3: 2}.get(mode, 0)
        off = sx(bits(w, 21, 15), 7) * size
        rt, rt2 = bits(w, 4, 0), bits(w, 14, 10)
        regs = [(r + 32 if v else r) if size == 8 and not (not v and opc == 1) and (v or r != 31) else None
                for r in (rt, rt2)]  # (X register 31 is xzr here)
        return ('ls', load, regs, size, rn, off, mode)
    if bits(w, 29, 27) == 0b111 and not bits(w, 25, 25):  # load/store register
        rn = bits(w, 9, 5)
        if rn not in (29, 31):
            return None
        v, size, opc = bits(w, 26, 26), bits(w, 31, 30), bits(w, 23, 22)
        nbytes = 1 << size
        if v and opc >= 2:
            if size:
                return None
            nbytes = 16
        load = opc != 0
        if bits(w, 24, 24):  # unsigned offset
            off, mode = bits(w, 21, 10) * nbytes, 0
        elif not bits(w, 21, 21) and bits(w, 11, 10) in (0, 1, 3):  # unscaled, post, pre
            off, mode = sx(bits(w, 20, 12), 9), {0: 0, 1: 1, 3: 2}[bits(w, 11, 10)]
        else:
            return None
        rt = bits(w, 4, 0)
        if not v and opc >= 2:  # prfm, ldrsw: no 64-bit X result to track
            reg = None
        else:
            reg = (rt + 32 if v else rt) if nbytes == 8 and (v or opc < 2) and (v or rt != 31) else None
        return ('ls', load, [reg], nbytes, rn, off, mode)
    return None


# ------------------------------------------------------------ CFG

class Block:
    __slots__ = ('steps', 'succ', 'term')

    def __init__(self):
        self.steps, self.succ, self.term = [], [], None


def cfg(p, fn, direct):
    """Blocks of fn for the analyses. direct(t) -> True if a call to t goes to a
    translated function with a summary. Steps:
      ('g', uses, defs)                  straight-line code
      ('m', uses, defs, memop)           a frame op (see memop)
      ('c', key, target|None)            call (None: indirect, AAPCS64)
      ('s', key)                         svc
    Terminators: ('ret', key), ('tail', key, target|None), ('trap', key), None
    (fall into succ). key identifies the site for emission: (address, tag)."""
    body = fn.body
    order = sorted(body)
    leaders = {fn.start} | (fn.labels & body)
    for a in order:
        if p.insn(a).kind in ('b', 'bcond', 'br', 'ret', 'trap') and a + 4 in body:
            leaders.add(a + 4)
    ids = {a: i for i, a in enumerate(sorted(leaders))}
    blocks = [Block() for _ in ids]

    def tblock(key, t):
        b = Block()
        b.term = ('tail', key, t if t is not None and direct(t) else None) if t is None or p.is_text(t) \
            else ('trap', key)
        blocks.append(b)
        return len(blocks) - 1

    def local(t):
        return t in fn.labels and t in body

    for a0, bi in ids.items():
        blk = blocks[bi]
        a = a0
        while True:
            ins = p.insn(a)
            k = ins.kind
            if k == 'op':
                u, d = effects(ins.c)
                if 'a64_svc(' in ins.c:
                    blk.steps.append(('s', (a, 's')))
                else:
                    mo = memop(p.word(a)) if ins.tag != 'patch' else None
                    blk.steps.append(('m', u, d, mo) if mo else ('g', u, d))
            elif k == 'bl':
                blk.steps.append(('g', 0, X30))
                t = ins.target
                blk.steps.append(('c', (a, 'c'), t if direct(t) else None))
            elif k == 'blr':
                u, d = effects(ins.c)
                blk.steps.append(('g', u, X30))
                blk.steps.append(('c', (a, 'c'), None))
            elif k == 'b':
                t = ins.target
                if local(t):
                    blk.succ.append(ids[t])
                else:
                    blk.term = ('tail', (a, 't'), t if direct(t) else None)
                break
            elif k == 'bcond':
                u, d = effects(ins.c)
                blk.steps.append(('g', u, d))
                t = ins.target
                blk.succ.append(ids[t] if local(t) else tblock((a, 't'), t))
            elif k == 'br':
                u, d = effects(ins.c)
                blk.steps.append(('g', u, d))
                ts = fn.jt.get(a)
                if ts:
                    for t in ts:
                        blk.succ.append(ids[t] if local(t) else tblock((a, t), t))
                    if not blk.succ:
                        blk.term = ('trap', (a, 'x'))
                else:
                    blk.term = ('tail', (a, 't'), None)
                break
            elif k == 'ret':
                blk.term = ('ret', (a, 'x'))
                break
            else:  # trap
                blk.term = ('trap', (a, 'x'))
                break
            # op, bl, blr, bcond fall through
            if a in fn.falls:
                t = fn.falls[a]
                blk.succ.append(tblock((a, 'f'), t))
                break
            a += 4
            if a in ids:
                blk.succ.append(ids[a])
                break
            if a not in body:  # (can't happen: falls covers leaving the body)
                blk.term = ('trap', (a - 4, 'x'))
                break
    return blocks, ids[fn.start]


# ------------------------------------------------------------ analyses

def callee(summ, t):
    """(R, W) of a call target (None: indirect)."""
    if t is None:
        return ABI_IN, CLOB
    return summ.get(t, (0, 0))


def liveness(blocks, summ, sites=None, uses=None, exit_live=0):
    """Live-in per block; with sites, also the live set after each call site.
    uses: registers each site (key) also reads, its stores of locals to the CPU struct.
    exit_live: registers live at a return (for emission: the function's own W, see plan)."""
    uses = uses or {}
    n = len(blocks)
    preds = [[] for _ in range(n)]
    for i, b in enumerate(blocks):
        for s in b.succ:
            preds[s].append(i)
    live_in = [0] * n
    work = list(range(n))
    inq = [True] * n
    while work:
        i = work.pop()
        inq[i] = False
        b = blocks[i]
        live = 0
        for s in b.succ:
            live |= live_in[s]
        t = b.term
        if t is not None:
            if t[0] == 'tail':
                live |= callee(summ, t[2])[0] | exit_live
            elif t[0] == 'ret':
                live |= exit_live
            else:  # trap: fatal (a64_trap, a64_bad_jump); its stores are only for the crash report
                live = 0
            live |= uses.get(t[1], 0)
        for st in reversed(b.steps):
            k = st[0]
            if k == 'g' or k == 'm':
                live = st[1] | (live & ~st[2])
            elif k == 'c':
                if sites is not None:
                    sites[st[1]] = live
                live |= callee(summ, st[2])[0] | uses.get(st[1], 0)
            else:  # svc
                if sites is not None:
                    sites[st[1]] = live
                live |= ABI_IN | uses.get(st[1], 0)
        if live != live_in[i]:
            live_in[i] = live
            for q in preds[i]:
                if not inq[q]:
                    inq[q] = True
                    work.append(q)
    return live_in


def _join(a, b):
    if a is None:
        return b
    sp = a[0] if a[0] == b[0] else None
    fp = a[1] if a[1] == b[1] else None
    slots = {k: v for k, v in a[3].items() if b[3].get(k) == v}
    return (sp, fp, a[2] & b[2], slots)


def summarize(blocks, entry, summ):
    """(R, W) of a function from its CFG and its callees' summaries."""
    R = liveness(blocks, summ)[entry]
    writes = 0
    for b in blocks:
        for st in b.steps:
            k = st[0]
            if k == 'g' or k == 'm':
                writes |= st[2]
            elif k == 'c':
                writes |= callee(summ, st[2])[1]
            else:
                writes |= SVC_W
        if b.term is not None and b.term[0] == 'tail':
            writes |= callee(summ, b.term[2])[1]
    # forward: callee-saved registers holding their entry value, sp/x29 offsets from the entry sp
    n = len(blocks)
    state = [None] * n
    state[entry] = (0, None, TRACK, {})
    work = [entry]
    exit_ok, sp_ok = TRACK, True
    while work:
        i = work.pop()
        sp, fp, ok, slots = state[i]
        slots = dict(slots)
        b = blocks[i]
        for st in b.steps:
            k = st[0]
            if k == 'g':
                d = st[2]
                ok &= ~d
                if d & SP:
                    sp = None
                if d & X29:
                    fp = None
            elif k == 'm':
                mo = st[3]
                if mo[0] == 'add':
                    _, rd, rn, delta = mo
                    v = sp if rn == 31 else fp if rn == 29 else None
                    v = None if v is None else v + delta
                    if rd == 31:
                        sp = v
                    else:
                        fp = v
                        ok &= ~X29
                    ok &= ~(st[2] & ~(SP | X29))
                    continue
                _, load, regs, size, base, off, mode = mo
                bv = sp if base == 31 else fp
                d = st[2]
                if bv is None:
                    ok &= ~d
                else:
                    ea = bv + off if mode != 1 else bv
                    for j, r in enumerate(regs):
                        addr = ea + j * size
                        if load:
                            if r is not None and slots.get(addr) == r:
                                ok |= 1 << r
                            elif r is not None:
                                ok &= ~(1 << r)
                        else:
                            for s in [s for s in slots if s < addr + size and addr < s + 8]:
                                del slots[s]
                            if r is not None and ok >> r & 1 and (1 << r) & TRACK:
                                slots[addr] = r
                    if load:  # the other registers it writes (32-bit, q, ldpsw)
                        ok &= ~(d & ~sum(1 << r for r in regs if r is not None))
                    if mode:
                        if base == 31:
                            sp = bv + off
                        else:
                            fp = bv + off
                            ok &= ~X29
                if load and d & X29:
                    fp = None
            elif k == 'c':
                W = callee(summ, st[2])[1]
                ok &= ~W
                if W & SP:
                    sp = None
                if W & X29:
                    fp = None
            else:
                ok &= ~SVC_W
        t = b.term
        if t is not None and t[0] in ('ret', 'tail'):
            if t[0] == 'tail':
                W = callee(summ, t[2])[1]
                ok &= ~W
                if W & SP:
                    sp = None
            exit_ok &= ok
            sp_ok = sp_ok and sp == 0
        out = (sp, fp, ok, slots)
        for s in b.succ:
            j = _join(state[s], out)
            if j != state[s]:
                state[s] = j
                work.append(s)
    W = writes & ~exit_ok & ~(SP if sp_ok else 0)
    return R, W


def plan(blocks, entry, summ, own_w):
    """Stores and reloads per site: {key: (stores, reloads)}, from the changed
    (dirty) locals, forward, and liveness after calls. own_w: W of this function.

    Each register is in one of these states: changed (the local is right, the
    CPU struct maybe not), in sync, or CPU-only (after a call that may change it
    and no reload: the CPU struct is right, the local stale). And at entry, a
    register outside R(F) may be right in neither (the caller didn't store it),
    which is fine because F doesn't read it and stores it only after writing it.
    The rules that keep the CPU struct right where anyone reads it:
    - at a return the caller may reload any of W(F): those are live there;
    - before a call: changed registers the callee reads, and changed registers
      it may change that are live after the call (whoever reloads one after it
      needs the value it had if the callee leaves it alone: the CPU struct only
      holds that if it was stored);
    - after a call: reload what it may change where live;
    - a store of a local reads it: a register stored further on is live, so a
      CPU-only one is reloaded first (second round, with the first's stores)."""
    first = _plan(blocks, entry, summ, None, own_w)
    return _plan(blocks, entry, summ, {k: v[0] for k, v in first.items()}, own_w)


def _plan(blocks, entry, summ, uses, own_w):
    sites = {}
    live_in = liveness(blocks, summ, sites, uses, own_w)
    n = len(blocks)
    din = [None] * n
    din[entry] = 0
    work = [entry]
    out = {}
    while work:
        i = work.pop()
        dirty = din[i]
        b = blocks[i]
        for st in b.steps:
            k = st[0]
            if k == 'g' or k == 'm':
                dirty |= st[2]
            elif k == 'c':
                Rc, Wc = callee(summ, st[2])
                if CHECK:  # store everything changed; verify the kept registers after the call
                    out[st[1]] = (dirty, Wc & sites[st[1]], sites[st[1]] & ~Wc)
                    dirty = 0
                else:
                    out[st[1]] = (dirty & (Rc | (Wc & sites[st[1]])), Wc & sites[st[1]])
                dirty &= ~(Rc | Wc)
            else:
                out[st[1]] = (dirty & (ABI_IN | (SVC_W & sites[st[1]])), SVC_W & sites[st[1]])
                dirty &= ~(ABI_IN | SVC_W)
        t = b.term
        if t is not None:
            out[t[1]] = (out.get(t[1], (0, 0))[0] | dirty, 0)
        for s in b.succ:
            if din[s] is None or (din[s] | dirty) != din[s]:
                din[s] = dirty if din[s] is None else din[s] | dirty
                work.append(s)
    return out

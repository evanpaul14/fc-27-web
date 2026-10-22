"""python3 -I tools/verify_locals.py [n]: after recomp/build.py, check every locals-emitted function's plan: no stale local is read, callee
inputs are right in the CPU struct at calls, and at returns the CPU struct is
right (or right-if-the-caller-cares) for every X register."""
import collections, multiprocessing, os, pickle, sys
sys.path.insert(0, 'recomp')
import build, regs
XM = regs.xs(0, 31)

def meet(a, b):
    if a is None: return b
    t = [a[i] & b[i] for i in (0, 2)]
    c = [(a[i] | a[i + 1]) & (b[i] | b[i + 1]) & ~t[k] for k, i in enumerate((0, 2))]
    return (t[0], c[0], t[1], c[1])

def check(f):
    p = build._P
    fn = build.discover(p, f)
    body = sorted(fn.body)
    if len(body) > build.CHUNK * 3 // 2: return []
    if any(p.insn(a).kind == 'bl' and p.insn(a).target in p.setjmp_calls for a in body): return []
    summ = build._SUMM
    blocks, entry = regs.cfg(p, fn, build.direct)
    R, W = summ[f]
    pl = regs.plan(blocks, entry, summ, W)
    errs = []
    def err(kind, key, mask):
        errs.append((f, kind, key, regs.names(mask & XM)))
    st0 = (R & XM, XM & ~R, R & XM, XM & ~R)  # (L right, L right-if-cares, C right, C right-if-cares)
    state = [None] * len(blocks)
    state[entry] = st0
    work = [entry]
    seen = set()
    def stores(s, m):
        LT, LC, CT, CC = s
        return (LT, LC, (CT & ~m) | (LT & m), (CC & ~m) | (LC & m))
    def call(s, key, Rc, Wc):
        sm, rl = pl.get(key, (0, 0))[:2]
        s = stores(s, sm & XM)
        LT, LC, CT, CC = s
        if Rc & XM & ~CT: err('callee input not right', key, Rc & ~CT)
        rl &= XM
        LT = (LT & ~Wc) | (CT & rl)
        LC = (LC & ~Wc) | (CC & rl)
        return (LT, LC, CT, CC)
    def ret(s, key):
        LT, LC, CT, CC = s
        if XM & ~(CT | CC): err('CPU wrong at return', key, XM & ~(CT | CC))
    iters = 0
    while work:
        i = work.pop()
        iters += 1
        s = state[i]
        b = blocks[i]
        for stp in b.steps:
            k = stp[0]
            if k in ('g', 'm'):
                u, d = stp[1], stp[2]
                if u & XM & ~s[0] and (i, id(stp)) not in seen:
                    seen.add((i, id(stp))); err('stale read', i, u & ~s[0])
                LT, LC, CT, CC = s
                s = (LT | d, LC & ~d, CT & ~d, CC & ~d)
            elif k == 'c':
                Rc, Wc = regs.callee(summ, stp[2])
                s = call(s, stp[1], Rc, Wc)
            else:
                s = call(s, stp[1], regs.ABI_IN, regs.SVC_W)
        t = b.term
        if t is not None:
            if t[0] == 'ret':
                s = stores(s, pl.get(t[1], (0, 0))[0] & XM); ret(s, t[1])
            elif t[0] == 'tail':
                Rc, Wc = regs.callee(summ, t[2])
                s = call(s, t[1], Rc, Wc); ret(s, t[1])
        for nb in b.succ:
            m = meet(state[nb], s)
            if m != state[nb]:
                state[nb] = m
                work.append(nb)
    # report each distinct error once
    out, seenk = [], set()
    for e in errs:
        if (e[1], e[2]) not in seenk:
            seenk.add((e[1], e[2])); out.append(e)
    return out

def job(fs):
    res = []
    for f in fs:
        try:
            res += check(f)
        except Exception as e:
            res.append((f, 'exception', repr(e)[:200], []))
    return res

def main():
    p = build.Program('build')
    hle = build.load_hle(p)
    order = sorted(f for f in p.starts if p.is_text(f))
    d = pickle.load(open('build/gen/summaries.pkl', 'rb'))
    build._P, build._BUILT, build._HLE, build._SUMM = p, set(order), hle, d['summ']
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else len(order)
    fs = order[:: max(1, len(order) // lim)]
    chunks = [fs[i::256] for i in range(256)]
    errs = []
    with multiprocessing.get_context('fork').Pool(os.cpu_count()) as pool:
        for r in pool.imap_unordered(job, chunks):
            errs += r
    print(len(fs), 'functions checked;', len(errs), 'problems')
    by = collections.Counter(e[1] for e in errs)
    print(by)
    for e in errs[:25]:
        print(hex(e[0]), e[1], e[2], e[3])
if __name__ == '__main__':
    main()

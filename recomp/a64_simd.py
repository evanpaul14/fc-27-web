"""A64 SIMD and floating-point instructions -> C (companion to a64.py).

Vector and scalar AdvSIMD forms share one lane-loop emitter: a scalar op is
a one-lane op whose result register starts zeroed.
"""
from a64 import Insn, Unsupported, bits, sx, rx, wx, COND, hexc

VU = {8: 'b', 16: 'h', 32: 's', 64: 'd'}
VS = {8: 'sb', 16: 'sh', 32: 'ss', 64: 'sd'}
VF = {32: 'f', 64: 'fd'}
CU = {8: 'uint8_t', 16: 'uint16_t', 32: 'uint32_t', 64: 'uint64_t'}
CS = {8: 'int8_t', 16: 'int16_t', 32: 'int32_t', 64: 'int64_t'}
CF = {32: 'float', 64: 'double'}
FS = {32: '32', 64: '64'}  # helper suffix


def op(c, tag):
    return Insn('op', c, tag=tag)


def emit(rd, srcs, L, stmts, init_d=False, written=16, pre=''):
    """Lane loop: R starts zeroed (or as Vd), lanes 0..L-1 run `stmts` with i,
    bytes from `written` up are cleared when R started as Vd."""
    decl = ''.join(f'{k} = c->v[{r}], ' for k, r in srcs.items())
    init = f'c->v[{rd}]' if init_d else '{0}'
    tail = f' memset(R.b + {written}, 0, {16 - written});' if init_d and written < 16 else ''
    return (f'{{ V128 {decl}R = {init}; int q = 0; (void)q; {pre}'
            f'for (int i = 0; i < {L}; i++) {{ {stmts} }}{tail} c->v[{rd}] = R; }}')


def fview(sz):
    return 64 if sz else 32


def mask_of(cond, e):
    return f'({CU[e]})-(int64_t)({cond})'


# ---------------------------------------------------------------- dispatch

def simd_fp(w, pc):
    if bits(w, 31, 24) == 0b01001110 and bits(w, 23, 17) == 0b0010100 and bits(w, 11, 10) == 2:
        return aes(w)
    b28_24 = bits(w, 28, 24)
    if not bits(w, 30, 30) and b28_24 == 0b11110:
        return fp_scalar(w)
    if not bits(w, 31, 31) and not bits(w, 30, 30) and b28_24 == 0b11111:
        return fp_3src(w)
    if bits(w, 31, 31):
        raise Unsupported('simd')
    scalar = bool(bits(w, 28, 28))
    if scalar and not bits(w, 30, 30):
        raise Unsupported('simd')
    if b28_24 & 0xf == 0b1110:
        if bits(w, 21, 21):
            if bits(w, 10, 10):
                return three_same(w, scalar)
            if bits(w, 11, 10) == 0:
                if scalar:
                    raise Unsupported('scalar_3diff')
                return three_diff(w)
            if bits(w, 20, 17) == 0:
                return two_misc(w, scalar)
            if bits(w, 20, 17) == 0b1000:
                return pairwise_scalar(w) if scalar else across(w)
            raise Unsupported('simd_21')
        if bits(w, 23, 21) == 0 and not bits(w, 15, 15) and bits(w, 10, 10):
            return copy(w, scalar)
        if scalar:
            raise Unsupported('simd_scalar')
        if not bits(w, 29, 29) and not bits(w, 15, 15) and bits(w, 11, 10) == 2:
            return permute(w)
        if bits(w, 29, 29) and bits(w, 23, 21) == 0 and not bits(w, 15, 15) and not bits(w, 10, 10):
            return ext(w)
        if not bits(w, 29, 29) and bits(w, 23, 21) == 0 and not bits(w, 15, 15) and bits(w, 11, 10) == 0:
            return tbl(w)
        raise Unsupported('simd_other')
    if b28_24 & 0xf == 0b1111:
        if bits(w, 10, 10):
            if bits(w, 23, 19) == 0:
                if scalar:
                    raise Unsupported('scalar_modimm')
                return modimm(w)
            if bits(w, 23, 23):
                raise Unsupported('shift_imm')
            return shift_imm(w, scalar)
        return by_elem(w, scalar)
    raise Unsupported('simd')


# ---------------------------------------------------------------- three same

def three_same(w, scalar):
    q, u, size, rm, opcode, rn, rd = (bits(w, 30, 30), bits(w, 29, 29), bits(w, 23, 22), bits(w, 20, 16),
                                      bits(w, 15, 11), bits(w, 9, 5), bits(w, 4, 0))
    srcs = {'A': rn, 'B': rm}
    if opcode >= 0b11000:
        return three_same_fp(w, scalar, q, u, size, opcode, rn, rm, rd)
    e = 8 << size
    if scalar:
        if size != 3 and opcode not in (1, 5, 9, 11, 0x16):
            raise Unsupported('scalar_3same')
        L = 1
    else:
        if size == 3 and not q and opcode != 0b00011:
            raise Unsupported('3same_1d')
        L = (16 if q else 8) * 8 // e
    nb = 16 if q else 8
    if scalar:
        nb = e // 8
    vu, vs, cu, cs = VU[e], VS[e], CU[e], CS[e]
    a_u, b_u, a_s, b_s, d_u = f'A.{vu}[i]', f'B.{vu}[i]', f'A.{vs}[i]', f'B.{vs}[i]', f'D.{vu}[i]'
    a, b = (a_u, b_u) if u else (a_s, b_s)
    W = 'uint64_t' if u else 'int64_t'
    r = f'R.{vu}[i]'

    if opcode == 0b00011:  # logic on 64-bit lanes
        if scalar:
            raise Unsupported('scalar_logic')
        L64 = 2 if q else 1
        A, B, D = 'A.d[i]', 'B.d[i]', 'D.d[i]'
        e64 = {(0, 0): f'{A} & {B}', (0, 1): f'{A} & ~{B}', (0, 2): f'{A} | {B}', (0, 3): f'{A} | ~{B}',
               (1, 0): f'{A} ^ {B}', (1, 1): f'({D} & {A}) | (~{D} & {B})',
               (1, 2): f'({D} & ~{B}) | ({A} & {B})', (1, 3): f'({D} & {B}) | ({A} & ~{B})'}[(u, size)]
        names = {(0, 0): 'and', (0, 1): 'bic', (0, 2): 'orr', (0, 3): 'orn', (1, 0): 'eor', (1, 1): 'bsl',
                 (1, 2): 'bit', (1, 3): 'bif'}
        return op(emit(rd, {'A': rn, 'B': rm, 'D': rd}, L64, f'R.d[i] = {e64};'), 'v_' + names[(u, size)])

    if opcode in (0b10100, 0b10101, 0b10111):  # pairwise integer
        if scalar or (opcode == 0b10111 and u) or size == 3 and opcode != 0b10111:
            raise Unsupported('pairwise')
        ct = cu if u else cs
        vv = vu if u else vs
        f = {0b10100: 'x > y ? x : y', 0b10101: 'x < y ? x : y', 0b10111: f'({cu})(x + y)'}[opcode]
        pre = (f'{ct} cat[{2 * L}]; for (int k = 0; k < {L}; k++) {{ cat[k] = A.{vv}[k]; cat[{L} + k] = B.{vv}[k]; }} ')
        return op(emit(rd, srcs, L, f'{ct} x = cat[2 * i], y = cat[2 * i + 1]; R.{vv}[i] = {f};', pre=pre),
                  'v_pairwise')

    ex = None
    if opcode == 0b00000 and e < 64:
        ex = f'(({W}){a} + {b}) >> 1'
    elif opcode == 0b00010 and e < 64:
        ex = f'(({W}){a} + {b} + 1) >> 1'
    elif opcode == 0b00100 and e < 64:
        ex = f'(({W}){a} - {b}) >> 1'
    elif opcode == 0b00001:
        ex = f'sat_{"u" if u else "s"}((__int128){a} + {b}, {e}, &q)'
    elif opcode == 0b00101:
        ex = f'sat_{"u" if u else "s"}((__int128){a} - {b}, {e}, &q)'
    elif opcode == 0b00110:
        ex = mask_of(f'{a} > {b}', e)
    elif opcode == 0b00111:
        ex = mask_of(f'{a} >= {b}', e)
    elif opcode in (0b01000, 0b01010):
        rnd = int(opcode == 0b01010)
        ex = f'shl_{"u" if u else "s"}({a}, (int8_t){b_u}, {rnd})'
    elif opcode == 0b01100 and e < 64:
        ex = f'{a} > {b} ? {a} : {b}'
    elif opcode == 0b01101 and e < 64:
        ex = f'{a} < {b} ? {a} : {b}'
    elif opcode in (0b01110, 0b01111) and e < 64:
        ex = f'{a} > {b} ? ({W}){a} - {b} : ({W}){b} - {a}'
        if opcode == 0b01111:
            srcs['D'] = rd
            ex = f'{d_u} + ({ex})'
    elif opcode == 0b10000:
        ex = f'{a_u} {"-" if u else "+"} {b_u}'
    elif opcode == 0b10001:
        ex = mask_of(f'{a_u} == {b_u}' if u else f'({a_u} & {b_u}) != 0', e)
    elif opcode == 0b10010 and e < 64:
        srcs['D'] = rd
        ex = f'{d_u} {"-" if u else "+"} ({cu})({a_u} * {b_u})'
    elif opcode == 0b10011 and not u and e < 64:
        ex = f'({cu})({a_u} * {b_u})'
    elif opcode == 0b10110 and e in (16, 32):
        rnd = f' + ((__int128)1 << {e - 1})' if u else ''
        ex = f'sat_s(((__int128)2 * {a_s} * {b_s}{rnd}) >> {e}, {e}, &q)'
    if ex is None:
        raise Unsupported(f'3same_{u}_{opcode:05b}')
    return op(emit(rd, srcs, L, f'{r} = ({cu})({ex});'), f'v3s_{u}_{opcode:05b}')


def three_same_fp(w, scalar, q, u, size, opcode, rn, rm, rd):
    sz, o1 = size & 1, size >> 1
    e = fview(sz)
    if not scalar and sz and not q:
        raise Unsupported('fp_1d')
    L = 1 if scalar else (16 if q else 8) * 8 // e
    vf, vu, s = VF[e], VU[e], FS[e]
    a, b, d = f'A.{vf}[i]', f'B.{vf}[i]', f'D.{vf}[i]'
    srcs = {'A': rn, 'B': rm}
    fma = 'fmaf' if e == 32 else 'fma'
    k = (u, o1, opcode)
    if k in ((1, 0, 0b11010), (1, 0, 0b11110), (1, 1, 0b11110), (1, 0, 0b11000), (1, 1, 0b11000)):
        if scalar:
            raise Unsupported('fp_pairwise_scalar')
        f = {(1, 0, 0b11010): 'x + y', (1, 0, 0b11110): f'fmax{s}(x, y)', (1, 1, 0b11110): f'fmin{s}(x, y)',
             (1, 0, 0b11000): f'fmaxnm{s}(x, y)', (1, 1, 0b11000): f'fminnm{s}(x, y)'}[k]
        ct = CF[e]
        pre = f'{ct} cat[{2 * L}]; for (int k = 0; k < {L}; k++) {{ cat[k] = A.{vf}[k]; cat[{L} + k] = B.{vf}[k]; }} '
        return op(emit(rd, srcs, L, f'{ct} x = cat[2 * i], y = cat[2 * i + 1]; R.{vf}[i] = {f};', pre=pre),
                  'v_fpairwise')
    mask = None
    ex = None
    if k == (0, 0, 0b11000):
        ex = f'fmaxnm{s}({a}, {b})'
    elif k == (0, 1, 0b11000):
        ex = f'fminnm{s}({a}, {b})'
    elif k == (0, 0, 0b11001):
        srcs['D'] = rd
        ex = f'{fma}({a}, {b}, {d})'
    elif k == (0, 1, 0b11001):
        srcs['D'] = rd
        ex = f'{fma}(-{a}, {b}, {d})'
    elif k == (0, 0, 0b11010):
        ex = f'{a} + {b}'
    elif k == (0, 1, 0b11010):
        ex = f'{a} - {b}'
    elif k == (0, 0, 0b11100):
        mask = f'{a} == {b}'
    elif k == (0, 0, 0b11110):
        ex = f'fmax{s}({a}, {b})'
    elif k == (0, 1, 0b11110):
        ex = f'fmin{s}({a}, {b})'
    elif k == (0, 0, 0b11111):
        ex = f'frecps{s}({a}, {b})'
    elif k == (0, 1, 0b11111):
        ex = f'frsqrts{s}({a}, {b})'
    elif k == (1, 0, 0b11011):
        ex = f'{a} * {b}'
    elif k == (1, 0, 0b11100):
        mask = f'{a} >= {b}'
    elif k == (1, 0, 0b11101):
        mask = f'fabs({a}) >= fabs({b})'
    elif k == (1, 0, 0b11111):
        ex = f'{a} / {b}'
    elif k == (1, 1, 0b11010):
        ex = f'fabs({a} - {b})'
    elif k == (1, 1, 0b11100):
        mask = f'{a} > {b}'
    elif k == (1, 1, 0b11101):
        mask = f'fabs({a}) > fabs({b})'
    if mask is not None:
        st = f'R.{vu}[i] = {mask_of(mask, e)};'
    elif ex is not None:
        st = f'R.{vf}[i] = {ex};'
    else:
        raise Unsupported(f'3same_fp_{u}{o1}_{opcode:05b}')
    return op(emit(rd, srcs, L, st), f'v3sf_{u}{o1}_{opcode:05b}')


# ---------------------------------------------------------------- three different

def three_diff(w):
    q, u, size, rm, opcode, rn, rd = (bits(w, 30, 30), bits(w, 29, 29), bits(w, 23, 22), bits(w, 20, 16),
                                      bits(w, 15, 12), bits(w, 9, 5), bits(w, 4, 0))
    if size == 3:
        raise Unsupported('3diff')
    e, e2 = 8 << size, 16 << size
    L = 64 // e
    off = L if q else 0
    v, v2 = (VU[e] if u else VS[e]), (VU[e2] if u else VS[e2])
    W = 'uint64_t' if u else 'int64_t'
    a, b = f'({W})A.{v}[i + {off}]', f'({W})B.{v}[i + {off}]'
    srcs = {'A': rn, 'B': rm}
    r2 = f'R.{VU[e2]}[i]'
    st = None
    if opcode == 0b0000:
        st = f'{r2} = {a} + {b};'
    elif opcode == 0b0001:
        st = f'{r2} = ({W})A.{v2}[i] + {b};'
    elif opcode == 0b0010:
        st = f'{r2} = {a} - {b};'
    elif opcode == 0b0011:
        st = f'{r2} = ({W})A.{v2}[i] - {b};'
    elif opcode in (0b0100, 0b0110):  # (R)ADDHN / (R)SUBHN
        rnd = f' + (1ull << {e - 1})' if u else ''
        sgn = '+' if opcode == 0b0100 else '-'
        st = f'R.{VU[e]}[i + {off}] = ({CU[e2]})(A.{VU[e2]}[i] {sgn} B.{VU[e2]}[i]{rnd}) >> {e};'
        written = 16 if q else 8
        return op(emit(rd, srcs, L, st, init_d=bool(q), written=written), 'v_addhn')
    elif opcode in (0b0101, 0b0111):
        d = f'{a} > {b} ? {a} - {b} : {b} - {a}'
        if opcode == 0b0101:
            srcs['D'] = rd
            st = f'{r2} = D.{VU[e2]}[i] + ({d});'
        else:
            st = f'{r2} = {d};'
    elif opcode in (0b1000, 0b1010):
        srcs['D'] = rd
        st = f'{r2} = D.{VU[e2]}[i] {"+" if opcode == 0b1000 else "-"} {a} * {b};'
    elif opcode == 0b1100:
        st = f'{r2} = {a} * {b};'
    if st is None:
        raise Unsupported(f'3diff_{u}_{opcode:04b}')
    return op(emit(rd, srcs, L, st), f'v3d_{u}_{opcode:04b}')


# ---------------------------------------------------------------- two-register misc

FRINT = {(0, 0, 0b11000): 0, (0, 0, 0b11001): 2, (0, 1, 0b11000): 1, (0, 1, 0b11001): 3,
         (1, 0, 0b11000): 4, (1, 0, 0b11001): 0, (1, 1, 0b11001): 0}  # frintn m p z a x i
FCVTI = {(0, 0, 0b11010): 0, (0, 0, 0b11011): 2, (0, 0, 0b11100): 4,
         (0, 1, 0b11010): 1, (0, 1, 0b11011): 3}  # fcvtns ms as ps zs (U=1 for unsigned)


def two_misc(w, scalar):
    q, u, size, opcode, rn, rd = (bits(w, 30, 30), bits(w, 29, 29), bits(w, 23, 22), bits(w, 16, 12),
                                  bits(w, 9, 5), bits(w, 4, 0))
    srcs = {'A': rn}
    nb = 16 if q else 8

    if opcode >= 0b01100 and (size >> 1 or opcode >= 0b10110) and opcode not in (0b10010, 0b10011, 0b10100):
        return two_misc_fp(w, scalar, q, u, size, opcode, rn, rd)

    e = 8 << size
    L = 1 if scalar else nb * 8 // e
    vu, vs, cu = VU[e], VS[e], CU[e]
    a_u, a_s = f'A.{vu}[i]', f'A.{vs}[i]'
    r = f'R.{vu}[i]'
    st = None

    if opcode in (0b10010, 0b10100):  # XTN / SQXTUN / SQXTN / UQXTN (narrow)
        if size == 3:
            raise Unsupported('xtn')
        e2 = e * 2
        Ln = 1 if scalar else 64 // e
        off = 0 if scalar else (Ln if q else 0)
        src = f'A.{VU[e2]}[i]' if (opcode == 0b10100 and u) else f'A.{VS[e2]}[i]'
        if opcode == 0b10010:
            ex = f'A.{VU[e2]}[i]' if not u else f'sat_u({src}, {e}, &q)'
            tag = 'xtn' if not u else 'sqxtun'
            if scalar and not u:
                raise Unsupported('xtn_scalar')
        else:
            ex = f'sat_{"u" if u else "s"}({src}, {e}, &q)'
            tag = 'qxtn'
        st = f'R.{vu}[i + {off}] = ({cu})({ex});'
        if scalar:
            return op(emit(rd, srcs, 1, st), tag)
        return op(emit(rd, srcs, Ln, st, init_d=bool(q), written=16 if q else 8), tag)
    if opcode == 0b10011 and u and not scalar:  # SHLL
        if size == 3:
            raise Unsupported('shll')
        Ln, off, e2 = 64 // e, (64 // e if q else 0), e * 2
        return op(emit(rd, srcs, Ln, f'R.{VU[e2]}[i] = (uint64_t)A.{vu}[i + {off}] << {e};'), 'shll')

    if scalar and opcode not in (0b01000, 0b01001, 0b01010, 0b01011, 0b00011, 0b00111):
        raise Unsupported(f'scalar_2misc_{opcode:05b}')
    if scalar and size != 3 and opcode in (0b01000, 0b01001, 0b01010, 0b01011):
        raise Unsupported('scalar_2misc')

    if opcode <= 0b00001:  # REV64 / REV32 / REV16
        group = {(0, 0): 64, (1, 0): 32, (0, 1): 16}.get((u, opcode))
        if group is None or e >= group:
            raise Unsupported('rev')
        return op(emit(rd, srcs, L, f'{r} = A.{vu}[i ^ {group // e - 1}];'), f'rev{group}')
    if opcode in (0b00010, 0b00110):  # (S|U)ADDLP / ADALP
        if size == 3:
            raise Unsupported('addlp')
        e2 = e * 2
        Lp = nb * 8 // e2
        v = vu if u else vs
        W = 'uint64_t' if u else 'int64_t'
        acc = ''
        if opcode == 0b00110:
            srcs['D'] = rd
            acc = f'D.{VU[e2]}[i] + '
        return op(emit(rd, srcs, Lp, f'R.{VU[e2]}[i] = {acc}({W})A.{v}[2 * i] + A.{v}[2 * i + 1];'), 'addlp')
    if opcode == 0b00100:
        if size == 3:
            raise Unsupported('clz')
        if u:
            ex = f'clz32((uint32_t){a_u}) - {32 - e}'
        else:
            ex = f'cls32((uint32_t)(int32_t){a_s}) - {32 - e}'
        return op(emit(rd, srcs, L, f'{r} = {ex};'), 'clz')
    if opcode == 0b00101:
        if size == 0 and not u:
            return op(emit(rd, srcs, L, f'{r} = __builtin_popcount({a_u});'), 'cnt')
        if size == 0 and u:
            return op(emit(rd, srcs, L, f'{r} = ~{a_u};'), 'not')
        if size == 1 and u:
            return op(emit(rd, {'A': rn}, nb, 'R.b[i] = __builtin_bitreverse8(A.b[i]);'), 'rbit')
        raise Unsupported('cnt')
    if opcode == 0b01000:
        st = f'{r} = {mask_of(f"{a_s} >= 0" if u else f"{a_s} > 0", e)};'
    elif opcode == 0b01001:
        st = f'{r} = {mask_of(f"{a_s} <= 0" if u else f"{a_s} == 0", e)};'
    elif opcode == 0b01010 and not u:
        st = f'{r} = {mask_of(f"{a_s} < 0", e)};'
    elif opcode == 0b01011:
        st = f'{r} = -{a_u};' if u else f'{r} = {a_s} < 0 ? -{a_u} : {a_u};'
    elif opcode == 0b00111:  # SQABS / SQNEG
        v = f'-(__int128){a_s}' if u else f'({a_s} < 0 ? -(__int128){a_s} : (__int128){a_s})'
        st = f'{r} = ({cu})sat_s({v}, {e}, &q);'
    if st is None:
        raise Unsupported(f'2misc_{u}_{opcode:05b}')
    if size == 3 and not q and not scalar:
        raise Unsupported('2misc_1d')
    return op(emit(rd, srcs, L, st), f'v2m_{u}_{opcode:05b}')


def two_misc_fp(w, scalar, q, u, size, opcode, rn, rd):
    sz, o1 = size & 1, size >> 1
    srcs = {'A': rn}
    if opcode in (0b10110, 0b10111):  # FCVTN / FCVTL (and FCVTXN)
        if scalar or o1:
            raise Unsupported('fcvtn')
        if opcode == 0b10110:
            if u:
                raise Unsupported('fcvtxn')
            off = (4 if not sz else 2) if q else 0
            if sz:
                st = f'R.f[i + {off}] = (float)A.fd[i];'
                Ln = 2
            else:
                st = f'R.h[i + {off}] = f2h(A.f[i]);'
                Ln = 4
            return op(emit(rd, srcs, Ln, st, init_d=bool(q), written=16 if q else 8), 'fcvtn')
        if u:
            raise Unsupported('fcvtl')
        if sz:
            return op(emit(rd, srcs, 2, f'R.fd[i] = (double)A.f[i + {2 if q else 0}];'), 'fcvtl')
        return op(emit(rd, srcs, 4, f'R.f[i] = h2f(A.h[i + {4 if q else 0}]);'), 'fcvtl')
    e = fview(sz)
    if not scalar and sz and not q:
        raise Unsupported('fp_1d')
    L = 1 if scalar else (16 if q else 8) * 8 // e
    vf, vu, vs, s = VF[e], VU[e], VS[e], FS[e]
    a = f'A.{vf}[i]'
    k = (u, o1, opcode)
    st = None
    if k in FRINT and not scalar:
        st = f'R.{vf}[i] = round_mode{"32" if e == 32 else ""}({a}, {FRINT[k]});'
    elif (0, o1, opcode) in FCVTI:
        rm = FCVTI[(0, o1, opcode)]
        st = (f'R.{vu}[i] = f2u({a}, {rm}, {e});' if u else f'R.{vs}[i] = f2s({a}, {rm}, {e});')
    elif not o1 and opcode == 0b11101:
        st = f'R.{vf}[i] = ({CF[e]})({"" if u else "(" + CS[e] + ")"}A.{vu}[i]);'
    elif o1 and opcode == 0b01100:
        st = f'R.{vu}[i] = {mask_of(f"{a} >= 0" if u else f"{a} > 0", e)};'
    elif o1 and opcode == 0b01101:
        st = f'R.{vu}[i] = {mask_of(f"{a} <= 0" if u else f"{a} == 0", e)};'
    elif o1 and opcode == 0b01110 and not u:
        st = f'R.{vu}[i] = {mask_of(f"{a} < 0", e)};'
    elif o1 and opcode == 0b01111 and not scalar:
        st = f'R.{vf}[i] = {"-" if u else ("fabsf" if e == 32 else "fabs")}({a});'
    elif o1 and opcode == 0b11101:
        fn = 'frsqrte' if u else 'frecpe'
        st = f'R.{vu}[i] = a64_{fn}{s}(A.{vu}[i]);'
    elif k == (1, 1, 0b11111) and not scalar:
        st = f'R.{vf}[i] = sqrt{"f" if e == 32 else ""}({a});'
    if st is None:
        raise Unsupported(f'2misc_fp_{u}{o1}_{opcode:05b}')
    return op(emit(rd, srcs, L, st), f'v2mf_{u}{o1}_{opcode:05b}')


# ---------------------------------------------------------------- across lanes / scalar pairwise

def across(w):
    q, u, size, opcode, rn, rd = (bits(w, 30, 30), bits(w, 29, 29), bits(w, 23, 22), bits(w, 16, 12),
                                  bits(w, 9, 5), bits(w, 4, 0))
    nb = 16 if q else 8
    if opcode in (0b01100, 0b01111):  # FMAXNMV/FMINNMV/FMAXV/FMINV (4s only)
        if size & 1 or not q or not u:
            raise Unsupported('fp_across')
        nm = 'nm' if opcode == 0b01100 else ''
        f = f'fm{"in" if size >> 1 else "ax"}{nm}32'
        return op(f'{{ V128 A = c->v[{rn}], R = {{0}}; R.f[0] = {f}({f}(A.f[0], A.f[1]), {f}(A.f[2], A.f[3])); '
                  f'c->v[{rd}] = R; }}', 'fp_across')
    e = 8 << size
    L = nb * 8 // e
    if size == 3 or (size == 2 and not q):
        raise Unsupported('across')
    v = VU[e] if u else VS[e]
    if opcode == 0b11011 and not u:  # ADDV
        body = f'uint64_t acc = 0; for (int i = 0; i < {L}; i++) acc += A.{VU[e]}[i]; R.{VU[e]}[0] = acc;'
    elif opcode == 0b00011:  # (S|U)ADDLV
        W = 'uint64_t' if u else 'int64_t'
        body = f'{W} acc = 0; for (int i = 0; i < {L}; i++) acc += A.{v}[i]; R.{VU[2 * e]}[0] = acc;'
    elif opcode in (0b01010, 0b11010):  # MAXV / MINV
        cmp = '>' if opcode == 0b01010 else '<'
        body = f'int64_t acc = A.{v}[0]; for (int i = 1; i < {L}; i++) if (A.{v}[i] {cmp} acc) acc = A.{v}[i]; R.{VU[e]}[0] = acc;'
    else:
        raise Unsupported(f'across_{u}_{opcode:05b}')
    return op(f'{{ V128 A = c->v[{rn}], R = {{0}}; {body} c->v[{rd}] = R; }}', f'across_{u}_{opcode:05b}')


def pairwise_scalar(w):
    u, size, opcode, rn, rd = bits(w, 29, 29), bits(w, 23, 22), bits(w, 16, 12), bits(w, 9, 5), bits(w, 4, 0)
    if not u and opcode == 0b11011 and size == 3:
        return op(f'{{ V128 A = c->v[{rn}], R = {{0}}; R.d[0] = A.d[0] + A.d[1]; c->v[{rd}] = R; }}', 'addp_s')
    if u:
        sz, o1 = size & 1, size >> 1
        e = fview(sz)
        vf, s = VF[e], FS[e]
        f = {(0, 0b01100): f'fmaxnm{s}', (1, 0b01100): f'fminnm{s}', (0, 0b01101): None,
             (0, 0b01111): f'fmax{s}', (1, 0b01111): f'fmin{s}'}.get((o1, opcode))
        if opcode == 0b01101 and not o1:
            ex = f'A.{vf}[0] + A.{vf}[1]'
        elif f:
            ex = f'{f}(A.{vf}[0], A.{vf}[1])'
        else:
            raise Unsupported('pairwise_scalar')
        return op(f'{{ V128 A = c->v[{rn}], R = {{0}}; R.{vf}[0] = {ex}; c->v[{rd}] = R; }}', 'fpairwise_s')
    raise Unsupported('pairwise_scalar')


# ---------------------------------------------------------------- copy / permute / ext / tbl

def copy(w, scalar):
    q, opb, imm5, imm4, rn, rd = (bits(w, 30, 30), bits(w, 29, 29), bits(w, 20, 16), bits(w, 14, 11),
                                  bits(w, 9, 5), bits(w, 4, 0))
    if imm5 & 15 == 0:
        raise Unsupported('copy')
    size = (imm5 & -imm5).bit_length() - 1
    e = 8 << size
    idx = imm5 >> (size + 1)
    vu, vs = VU[e], VS[e]
    if scalar:
        if opb or imm4:
            raise Unsupported('copy_scalar')
        return op(f'{{ V128 A = c->v[{rn}], R = {{0}}; R.{vu}[0] = A.{vu}[{idx}]; c->v[{rd}] = R; }}', 'dup_s')
    if opb:
        if not q:
            raise Unsupported('ins')
        si = imm4 >> size
        return op(f'c->v[{rd}].{vu}[{idx}] = c->v[{rn}].{vu}[{si}];', 'ins_elem')
    L = (16 if q else 8) * 8 // e
    if imm4 == 0:
        if size == 3 and not q:
            raise Unsupported('dup')
        return op(emit(rd, {'A': rn}, L, f'R.{vu}[i] = A.{vu}[{idx}];'), 'dup_elem')
    if imm4 == 1:
        if size == 3 and not q:
            raise Unsupported('dup')
        return op(emit(rd, {}, L, f'R.{vu}[i] = ({CU[e]}){rx(rn)};'), 'dup_gen')
    if imm4 == 3:
        if not q:
            raise Unsupported('ins')
        return op(f'c->v[{rd}].{vu}[{idx}] = ({CU[e]}){rx(rn)};', 'ins_gen')
    if imm4 == 5:
        if size == 3 or (size == 2 and not q):
            raise Unsupported('smov')
        return op(wx(rd, f'(int64_t)c->v[{rn}].{vs}[{idx}]', q), 'smov')
    if imm4 == 7:
        if (size == 3) != bool(q):
            raise Unsupported('umov')
        return op(wx(rd, f'c->v[{rn}].{vu}[{idx}]', q), 'umov')
    raise Unsupported('copy')


def permute(w):
    q, size, rm, opc, rn, rd = (bits(w, 30, 30), bits(w, 23, 22), bits(w, 20, 16), bits(w, 14, 12),
                                bits(w, 9, 5), bits(w, 4, 0))
    if size == 3 and not q:
        raise Unsupported('permute')
    e = 8 << size
    L = (16 if q else 8) * 8 // e
    vu = VU[e]
    h = L // 2
    srcs = {'A': rn, 'B': rm}
    pre = f'{CU[e]} cat[{2 * L}]; for (int k = 0; k < {L}; k++) {{ cat[k] = A.{vu}[k]; cat[{L} + k] = B.{vu}[k]; }} '
    if opc in (1, 5):  # UZP
        return op(emit(rd, srcs, L, f'R.{vu}[i] = cat[2 * i + {opc >> 2}];', pre=pre), 'uzp')
    if opc in (2, 6):  # TRN
        p = opc >> 2
        return op(emit(rd, srcs, h, f'R.{vu}[2 * i] = A.{vu}[2 * i + {p}]; R.{vu}[2 * i + 1] = B.{vu}[2 * i + {p}];'),
                  'trn')
    if opc in (3, 7):  # ZIP
        base = h if opc == 7 else 0
        return op(emit(rd, srcs, h, f'R.{vu}[2 * i] = A.{vu}[i + {base}]; R.{vu}[2 * i + 1] = B.{vu}[i + {base}];'),
                  'zip')
    raise Unsupported('permute')


def ext(w):
    q, rm, imm4, rn, rd = bits(w, 30, 30), bits(w, 20, 16), bits(w, 14, 11), bits(w, 9, 5), bits(w, 4, 0)
    n = 16 if q else 8
    if not q and imm4 >= 8:
        raise Unsupported('ext')
    pre = f'uint8_t cat[{2 * n}]; memcpy(cat, A.b, {n}); memcpy(cat + {n}, B.b, {n}); '
    return op(emit(rd, {'A': rn, 'B': rm}, n, f'R.b[i] = cat[i + {imm4}];', pre=pre), 'ext')


def tbl(w):
    q, rm, ln, opx, rn, rd = (bits(w, 30, 30), bits(w, 20, 16), bits(w, 14, 13), bits(w, 12, 12),
                              bits(w, 9, 5), bits(w, 4, 0))
    n = 16 if q else 8
    regs = [(rn + k) % 32 for k in range(ln + 1)]
    pre = 'uint8_t T[64]; ' + ' '.join(f'memcpy(T + {16 * k}, c->v[{r}].b, 16);' for k, r in enumerate(regs)) + ' '
    other = 'D.b[i]' if opx else '0'
    st = f'uint8_t x = B.b[i]; R.b[i] = x < {16 * (ln + 1)} ? T[x] : {other};'
    return op(emit(rd, {'B': rm, 'D': rd}, n, st, pre=pre), 'tbx' if opx else 'tbl')


# ---------------------------------------------------------------- modified immediate

def vfp_expand(imm8, n):
    a, b, cd, efgh = imm8 >> 7, (imm8 >> 6) & 1, (imm8 >> 4) & 3, imm8 & 15
    if n == 32:
        exp = ((b ^ 1) << 7) | ((0x1f if b else 0) << 2) | cd
        return (a << 31) | (exp << 23) | (efgh << 19)
    if n == 64:
        exp = ((b ^ 1) << 10) | ((0xff if b else 0) << 2) | cd
        return (a << 63) | (exp << 52) | (efgh << 48)
    exp = ((b ^ 1) << 4) | ((3 if b else 0) << 2) | cd
    return (a << 15) | (exp << 10) | (efgh << 6)


def modimm(w):
    q, opb, cmode, rd = bits(w, 30, 30), bits(w, 29, 29), bits(w, 15, 12), bits(w, 4, 0)
    imm8 = (bits(w, 18, 16) << 5) | bits(w, 9, 5)
    c31 = cmode >> 1

    def rep(v, n):
        out = 0
        for i in range(64 // n):
            out |= v << (i * n)
        return out
    if c31 <= 3:
        imm, kind = rep(imm8 << (8 * c31), 32), ('orr' if cmode & 1 else 'movi')
    elif c31 <= 5:
        imm, kind = rep(imm8 << (8 * (c31 & 1)), 16), ('orr' if cmode & 1 else 'movi')
    elif c31 == 6:
        imm = rep((imm8 << 16 | 0xffff) if cmode & 1 else (imm8 << 8 | 0xff), 32)
        kind = 'movi'
    elif cmode == 14:
        if opb:
            imm = 0
            for i in range(8):
                if imm8 >> i & 1:
                    imm |= 0xff << (8 * i)
            kind = 'movi_raw'
        else:
            imm, kind = rep(imm8, 8), 'movi'
    else:
        if opb:
            if not q:
                raise Unsupported('fmov_2d')
            imm, kind = vfp_expand(imm8, 64), 'movi_raw'
        else:
            imm, kind = rep(vfp_expand(imm8, 32), 32), 'movi'
    if kind == 'movi' and opb:
        imm, kind = ~imm & (2**64 - 1), 'movi_raw'
    if kind == 'orr' and opb:
        kind = 'bic'
    hi = hexc(imm) if q else '0'
    if kind.startswith('movi'):
        return op(f'{{ V128 R; R.d[0] = {hexc(imm)}; R.d[1] = {hi}; c->v[{rd}] = R; }}', 'movi')
    if kind == 'orr':
        return op(f'{{ V128 R = c->v[{rd}]; R.d[0] |= {hexc(imm)}; R.d[1] = {"R.d[1] | " + hexc(imm) if q else "0"}; c->v[{rd}] = R; }}', 'orr_imm_v')
    return op(f'{{ V128 R = c->v[{rd}]; R.d[0] &= ~{hexc(imm)}; R.d[1] = {"R.d[1] & ~" + hexc(imm) if q else "0"}; c->v[{rd}] = R; }}', 'bic_imm_v')


# ---------------------------------------------------------------- shift by immediate

def shift_imm(w, scalar):
    q, u, immh, immb, opcode, rn, rd = (bits(w, 30, 30), bits(w, 29, 29), bits(w, 22, 19), bits(w, 18, 16),
                                        bits(w, 15, 11), bits(w, 9, 5), bits(w, 4, 0))
    hb = immh.bit_length() - 1
    e = 8 << hb
    ih = (immh << 3) | immb
    rsh, lsh = 2 * e - ih, ih - e
    srcs = {'A': rn}
    nb = 16 if q else 8
    vu, vs, cu = VU[e], VS[e], CU[e]
    narrow = opcode in (0b10000, 0b10001, 0b10010, 0b10011)
    if narrow or opcode == 0b10100:
        if e == 64:
            raise Unsupported('shift_narrow')
        e2 = 2 * e
        Ln = 1 if scalar else 64 // e
        off = 0 if scalar else (Ln if q else 0)
        if opcode == 0b10100:
            if scalar:
                raise Unsupported('sshll_scalar')
            v = vu if u else vs
            return op(emit(rd, srcs, Ln, f'R.{VU[e2]}[i] = ({CU[e2]})((uint64_t){"" if u else "(int64_t)"}A.{v}[i + {off}] << {lsh});'),
                      'sshll')
        rnd = opcode & 1
        if opcode in (0b10000, 0b10001) and not u:  # SHRN / RSHRN
            if scalar:
                raise Unsupported('shrn_scalar')
            ex = f'shl_u(A.{VU[e2]}[i], -{rsh}, {rnd})'
            tag = 'shrn'
        elif opcode in (0b10000, 0b10001):  # SQSHRUN / SQRSHRUN
            ex = f'sat_u(shl_s(A.{VS[e2]}[i], -{rsh}, {rnd}), {e}, &q)'
            tag = 'sqshrun'
        else:  # (S|U)Q(R)SHRN
            ex = (f'sat_u(shl_u(A.{VU[e2]}[i], -{rsh}, {rnd}), {e}, &q)' if u else
                  f'sat_s(shl_s(A.{VS[e2]}[i], -{rsh}, {rnd}), {e}, &q)')
            tag = 'qshrn'
        st = f'R.{vu}[i + {off}] = ({cu})({ex});'
        if scalar:
            return op(emit(rd, srcs, 1, st), tag)
        return op(emit(rd, srcs, Ln, st, init_d=bool(q), written=16 if q else 8), tag)

    if scalar and e != 64 and opcode not in (0b01110, 0b01100, 0b11100, 0b11111):
        raise Unsupported('shift_scalar')
    if not scalar and e == 64 and not q:
        raise Unsupported('shift_1d')
    L = 1 if scalar else nb * 8 // e
    a_u, a_s = f'A.{vu}[i]', f'A.{vs}[i]'
    r = f'R.{vu}[i]'
    shr = f'shl_u({a_u}, -{rsh}, {{rnd}})' if u else f'shl_s({a_s}, -{rsh}, {{rnd}})'
    st = None
    if opcode in (0b00000, 0b00100):
        st = f'{r} = ({cu}){shr.format(rnd=opcode >> 2)};'
    elif opcode in (0b00010, 0b00110):
        srcs['D'] = rd
        st = f'{r} = ({cu})(D.{vu}[i] + ({cu}){shr.format(rnd=opcode >> 2)});'
    elif opcode == 0b01000 and u:  # SRI
        srcs['D'] = rd
        m = ((1 << e) - 1) >> rsh if rsh < e else 0
        st = f'{r} = ({cu})((D.{vu}[i] & ~({cu}){hexc(m)}) | ({cu})shl_u({a_u}, -{rsh}, 0));'
    elif opcode == 0b01010:
        if u:  # SLI
            srcs['D'] = rd
            m = (((1 << e) - 1) << lsh) & ((1 << e) - 1)
            st = f'{r} = ({cu})((D.{vu}[i] & ~({cu}){hexc(m)}) | ({cu})((uint64_t){a_u} << {lsh}));'
        else:
            st = f'{r} = ({cu})((uint64_t){a_u} << {lsh});'
    elif opcode == 0b01100 and u:  # SQSHLU
        st = f'{r} = ({cu})sat_u((__int128){a_s} << {lsh}, {e}, &q);'
    elif opcode == 0b01110:  # SQSHL / UQSHL imm
        st = (f'{r} = ({cu})sat_u((__int128)((__uint128_t){a_u} << {lsh}), {e}, &q);' if u else
              f'{r} = ({cu})sat_s((__int128){a_s} << {lsh}, {e}, &q);')
    elif opcode == 0b11100:  # (S|U)CVTF fixed
        if e < 32:
            raise Unsupported('cvtf_fixed')
        src = a_u if u else a_s
        st = f'R.{VF[e]}[i] = ({CF[e]})ldexp(({CF[e]}){src}, -{rsh});'
        if e == 32:
            st = f'R.f[i] = ldexpf((float){src}, -{rsh});'
    elif opcode == 0b11111:  # FCVTZ(S|U) fixed
        if e < 32:
            raise Unsupported('fcvtz_fixed')
        x = f'ldexp((double)A.{VF[e]}[i], {rsh})'
        st = f'R.{vu}[i] = f2u({x}, 3, {e});' if u else f'R.{vs}[i] = f2s({x}, 3, {e});'
    if st is None:
        raise Unsupported(f'shift_{u}_{opcode:05b}')
    return op(emit(rd, srcs, L, st), f'shift_{u}_{opcode:05b}')


# ---------------------------------------------------------------- by element

def by_elem(w, scalar):
    q, u, size, l, m, rm, opcode, h, rn, rd = (bits(w, 30, 30), bits(w, 29, 29), bits(w, 23, 22), bits(w, 21, 21),
                                               bits(w, 20, 20), bits(w, 19, 16), bits(w, 15, 12), bits(w, 11, 11),
                                               bits(w, 9, 5), bits(w, 4, 0))
    srcs = {'A': rn}
    fp = opcode in (0b0001, 0b0101, 0b1001) and not (u and opcode != 0b1001)
    if fp:
        if size >> 1 == 0:
            raise Unsupported('fp16_elem')
        sz = size & 1
        e = fview(sz)
        if sz:
            if l:
                raise Unsupported('elem')
            idx = h
        else:
            idx = (h << 1) | l
        rmf = (m << 4) | rm
        if u:
            raise Unsupported('fmulx_elem')
        if not scalar and sz and not q:
            raise Unsupported('elem_1d')
        L = 1 if scalar else (16 if q else 8) * 8 // e
        vf = VF[e]
        fma = 'fmaf' if e == 32 else 'fma'
        pre = f'{CF[e]} el = c->v[{rmf}].{vf}[{idx}]; '
        if opcode == 0b1001:
            st = f'R.{vf}[i] = A.{vf}[i] * el;'
        else:
            srcs['D'] = rd
            st = f'R.{vf}[i] = {fma}({"-" if opcode == 0b0101 else ""}A.{vf}[i], el, D.{vf}[i]);'
        return op(emit(rd, srcs, L, st, pre=pre), f'elem_f_{opcode:04b}')
    if size == 1:
        idx, rmi = (h << 2) | (l << 1) | m, rm
    elif size == 2:
        idx, rmi = (h << 1) | l, (m << 4) | rm
    else:
        raise Unsupported('elem_size')
    e = 8 << size
    vu, vs, cu = VU[e], VS[e], CU[e]
    long_ops = {0b0010: '+', 0b0110: '-', 0b1010: ''}
    if opcode in long_ops:
        if scalar:
            raise Unsupported('elem_long_scalar')
        e2 = 2 * e
        Ln, off = 64 // e, (64 // e if q else 0)
        W = 'uint64_t' if u else 'int64_t'
        v = vu if u else vs
        pre = f'{W} el = c->v[{rmi}].{v}[{idx}]; '
        prod = f'({W})A.{v}[i + {off}] * el'
        if opcode == 0b1010:
            st = f'R.{VU[e2]}[i] = {prod};'
        else:
            srcs['D'] = rd
            st = f'R.{VU[e2]}[i] = D.{VU[e2]}[i] {long_ops[opcode]} {prod};'
        return op(emit(rd, srcs, Ln, st, pre=pre), 'elem_long')
    L = 1 if scalar else (16 if q else 8) * 8 // e
    if opcode in (0b1100, 0b1101) and not u:
        rnd = f' + ((__int128)1 << {e - 1})' if opcode == 0b1101 else ''
        pre = f'int64_t el = c->v[{rmi}].{vs}[{idx}]; '
        st = f'R.{vu}[i] = ({cu})sat_s(((__int128)2 * A.{vs}[i] * el{rnd}) >> {e}, {e}, &q);'
        return op(emit(rd, srcs, L, st, pre=pre), 'elem_sqdmulh')
    if scalar:
        raise Unsupported('elem_scalar')
    pre = f'{cu} el = c->v[{rmi}].{vu}[{idx}]; '
    if opcode == 0b1000 and not u:
        st = f'R.{vu}[i] = ({cu})(A.{vu}[i] * el);'
    elif opcode in (0b0000, 0b0100) and u:
        srcs['D'] = rd
        st = f'R.{vu}[i] = ({cu})(D.{vu}[i] {"+" if opcode == 0 else "-"} ({cu})(A.{vu}[i] * el));'
    else:
        raise Unsupported(f'elem_{u}_{opcode:04b}')
    return op(emit(rd, srcs, L, st, pre=pre), f'elem_{u}_{opcode:04b}')


# ---------------------------------------------------------------- scalar floating point

def fread(n, t):
    return {0: f'c->v[{n}].f[0]', 1: f'c->v[{n}].fd[0]', 3: f'h2f(c->v[{n}].h[0])'}[t]


def fwrite(d, t, e):
    f = {0: 'f', 1: 'fd'}[t]
    return f'{{ V128 R = {{0}}; R.{f}[0] = {e}; c->v[{d}] = R; }}'


def fp_scalar(w):
    sf, s, t, rn, rd = bits(w, 31, 31), bits(w, 29, 29), bits(w, 23, 22), bits(w, 9, 5), bits(w, 4, 0)
    if s:
        raise Unsupported('fp_s')
    if not bits(w, 21, 21):
        return fp_fixed(w)
    if bits(w, 15, 10) == 0:
        return fp_int(w)
    if sf:
        raise Unsupported('fp_m')
    if t == 2:
        raise Unsupported('fp_type')
    fs = 'f' if t == 0 else ''
    S = '32' if t == 0 else '64'
    if bits(w, 14, 10) == 0b10000:  # 1-source
        opc = bits(w, 20, 15)
        if t == 3:
            if opc in (4, 5):
                return op(fwrite(rd, opc - 4, fread(rn, 3)), 'fcvt_h')
            raise Unsupported('fp16')
        a = fread(rn, t)
        if opc == 0:
            return op(fwrite(rd, t, a), 'fmov')
        if opc == 1:
            return op(fwrite(rd, t, f'fabs{fs}({a})'), 'fabs')
        if opc == 2:
            return op(fwrite(rd, t, f'-{a}'), 'fneg')
        if opc == 3:
            return op(fwrite(rd, t, f'sqrt{fs}({a})'), 'fsqrt')
        if opc in (4, 5, 7):
            dt = opc & 3
            if dt == t:
                raise Unsupported('fcvt')
            if dt == 3:
                return op(f'{{ V128 R = {{0}}; R.h[0] = f2h((float){a}); c->v[{rd}] = R; }}', 'fcvt_h')
            return op(fwrite(rd, dt, f'({"float" if dt == 0 else "double"}){a}'), 'fcvt')
        if 8 <= opc <= 15 and opc not in (13,):
            rm = {8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 14: 0, 15: 0}[opc]
            return op(fwrite(rd, t, f'round_mode{"32" if t == 0 else ""}({a}, {rm})'), 'frint')
        raise Unsupported('fp1')
    if t == 3:
        raise Unsupported('fp16')
    rm = bits(w, 20, 16)
    a, b = fread(rn, t), fread(rm, t)
    if bits(w, 13, 10) == 0b1000:  # compare
        if bits(w, 15, 14) or bits(w, 2, 0):
            raise Unsupported('fcmp')
        bb = '0.0' if bits(w, 3, 3) else b
        return op(f'fcmp_flags(&F, {a}, {bb});', 'fcmp')
    if bits(w, 12, 10) == 0b100:  # FMOV immediate
        if bits(w, 9, 5):
            raise Unsupported('fmov_imm')
        imm8 = bits(w, 20, 13)
        v = vfp_expand(imm8, 32 if t == 0 else 64)
        if t == 0:
            return op(f'{{ V128 R = {{0}}; R.s[0] = {v:#x}u; c->v[{rd}] = R; }}', 'fmov_imm')
        return op(f'{{ V128 R = {{0}}; R.d[0] = {hexc(v)}; c->v[{rd}] = R; }}', 'fmov_imm')
    if bits(w, 11, 10) == 1:  # FCCMP
        cond, nzcv = bits(w, 15, 12), bits(w, 3, 0)
        return op(f'if ({COND[cond]}) fcmp_flags(&F, {a}, {b}); else SETNZCV(&F, {nzcv});', 'fccmp')
    if bits(w, 11, 10) == 2:  # 2-source
        opc = bits(w, 15, 12)
        ex = {0: f'{a} * {b}', 1: f'{a} / {b}', 2: f'{a} + {b}', 3: f'{a} - {b}', 4: f'fmax{S}({a}, {b})',
              5: f'fmin{S}({a}, {b})', 6: f'fmaxnm{S}({a}, {b})', 7: f'fminnm{S}({a}, {b})', 8: f'-({a} * {b})'}.get(opc)
        if ex is None:
            raise Unsupported('fp2')
        return op(fwrite(rd, t, ex), f'fp2_{opc}')
    cond = bits(w, 15, 12)  # FCSEL
    f = 'f' if t == 0 else 'd'
    return op(f'{{ V128 R = {{0}}; R.{f}[0] = {COND[cond]} ? c->v[{rn}].{f}[0] : c->v[{rm}].{f}[0]; c->v[{rd}] = R; }}',
              'fcsel')


def fp_int(w):
    sf, t, rmode, opc, rn, rd = (bits(w, 31, 31), bits(w, 23, 22), bits(w, 20, 19), bits(w, 18, 16),
                                 bits(w, 9, 5), bits(w, 4, 0))
    nbits = 64 if sf else 32
    if opc in (6, 7):  # FMOV general
        if rmode == 1:
            if not sf or t != 2:
                raise Unsupported('fmov_top')
            if opc == 6:
                return op(wx(rd, f'c->v[{rn}].d[1]'), 'fmov_top')
            return op(f'c->v[{rd}].d[1] = {rx(rn)};', 'fmov_top')
        if rmode or (sf and t != 1) or (not sf and t != 0 and t != 3):
            raise Unsupported('fmov_gen')
        if t == 3:
            raise Unsupported('fmov_h')
        if opc == 6:
            return op(wx(rd, f'c->v[{rn}].{"d" if sf else "s"}[0]', sf), 'fmov_to_gen')
        return op(f'{{ V128 R = {{0}}; R.{"d" if sf else "s"}[0] = {rx(rn, sf)}; c->v[{rd}] = R; }}', 'fmov_from_gen')
    if t == 2:
        raise Unsupported('fp_int')
    if t == 3:
        raise Unsupported('fp16_int')
    if opc in (2, 3):  # SCVTF / UCVTF
        if rmode:
            raise Unsupported('cvtf')
        ct = 'float' if t == 0 else 'double'
        src = f'({"int64_t" if sf else "int32_t"}){rx(rn, sf)}' if opc == 2 else rx(rn, sf)
        return op(fwrite(rd, t, f'({ct}){src}'), 'cvtf')
    a = fread(rn, t)
    if opc in (0, 1):
        rm = [0, 1, 2, 3][rmode]
    elif opc in (4, 5):
        if rmode:
            raise Unsupported('fcvta')
        rm = 4
    else:
        raise Unsupported('fp_int')
    uns = opc & 1
    ex = f'f2u({a}, {rm}, {nbits})' if uns else f'f2s({a}, {rm}, {nbits})'
    return op(wx(rd, ex, sf), 'fcvt_int')


def fp_fixed(w):
    sf, t, rmode, opc, scale, rn, rd = (bits(w, 31, 31), bits(w, 23, 22), bits(w, 20, 19), bits(w, 18, 16),
                                        bits(w, 15, 10), bits(w, 9, 5), bits(w, 4, 0))
    if t > 1 or (not sf and scale < 32):
        raise Unsupported('fp_fixed')
    fbits = 64 - scale
    nbits = 64 if sf else 32
    if rmode == 0 and opc in (2, 3):
        ct = 'float' if t == 0 else 'double'
        src = f'({"int64_t" if sf else "int32_t"}){rx(rn, sf)}' if opc == 2 else rx(rn, sf)
        return op(fwrite(rd, t, f'({ct})ldexp((double)({ct}){src}, -{fbits})'), 'cvtf_fixed')
    if rmode == 3 and opc in (0, 1):
        x = f'ldexp((double){fread(rn, t)}, {fbits})'
        ex = f'f2u({x}, 3, {nbits})' if opc else f'f2s({x}, 3, {nbits})'
        return op(wx(rd, ex, sf), 'fcvtz_fixed')
    raise Unsupported('fp_fixed')


def fp_3src(w):
    t, o1, rm, o0, ra, rn, rd = (bits(w, 23, 22), bits(w, 21, 21), bits(w, 20, 16), bits(w, 15, 15),
                                 bits(w, 14, 10), bits(w, 9, 5), bits(w, 4, 0))
    if t > 1 or bits(w, 29, 29):
        raise Unsupported('fp3')
    f = 'fmaf' if t == 0 else 'fma'
    a, n, m = fread(ra, t), fread(rn, t), fread(rm, t)
    ex = {(0, 0): f'{f}({n}, {m}, {a})', (0, 1): f'{f}(-{n}, {m}, {a})',
          (1, 0): f'{f}(-{n}, {m}, -{a})', (1, 1): f'{f}({n}, {m}, -{a})'}[(o1, o0)]
    return op(fwrite(rd, t, ex), 'fmadd')


def aes(w):
    opc, rn, rd = bits(w, 16, 12), bits(w, 9, 5), bits(w, 4, 0)
    if opc == 4:
        return op(f'c->v[{rd}] = a64_aese(c->v[{rd}], c->v[{rn}]);', 'aese')
    if opc == 5:
        return op(f'c->v[{rd}] = a64_aesd(c->v[{rd}], c->v[{rn}]);', 'aesd')
    if opc == 6:
        return op(f'c->v[{rd}] = a64_aesmc(c->v[{rn}]);', 'aesmc')
    if opc == 7:
        return op(f'c->v[{rd}] = a64_aesimc(c->v[{rn}]);', 'aesimc')
    raise Unsupported('aes')

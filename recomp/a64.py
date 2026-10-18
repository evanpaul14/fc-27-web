"""A64 instruction decoder that emits C for recompiled code (see rt/cpu.h).

translate(word, pc) returns an Insn whose kind tells the function builder
how control flow continues:
  op     straight-line C in .c
  b      unconditional branch to .target
  bcond  branch to .target if C expression .c is true
  bl     call .target (builder sets x30)
  blr    call through C expression .c
  br     jump through C expression .c
  ret    return through C expression .c (normally x30)
  trap   .c is C that does not return
Unsupported encodings raise Unsupported(tag).
"""


class Unsupported(Exception):
    pass


class Insn:
    __slots__ = ('kind', 'c', 'target', 'tag')

    def __init__(self, kind, c='', target=None, tag=''):
        self.kind, self.c, self.target, self.tag = kind, c, target, tag

    def __repr__(self):
        t = f' -> {self.target:#x}' if self.target is not None else ''
        return f'<{self.tag} {self.kind}{t}: {self.c}>'


def bits(w, hi, lo):
    return (w >> lo) & ((1 << (hi - lo + 1)) - 1)


def sx(v, n):
    return v - (1 << n) if v & (1 << (n - 1)) else v


def hexc(v):
    """C literal for an unsigned 64-bit value."""
    return f'{v & (2**64 - 1):#x}ull'


def op(c, tag):
    return Insn('op', c, tag=tag)


# ---------------------------------------------------------------- registers

def rx(n, sf=1, sp=False):
    if n == 31 and not sp:
        return '0ull' if sf else '0u'
    return f'c->x[{n}]' if sf else f'(uint32_t)c->x[{n}]'


def wx(n, e, sf=1, sp=False):
    if n == 31 and not sp:
        return f'(void)({e});'
    return f'c->x[{n}] = (uint64_t)({e});' if sf else f'c->x[{n}] = (uint32_t)({e});'


UT = {1: 'uint64_t', 0: 'uint32_t'}
ST = {1: 'int64_t', 0: 'int32_t'}

COND = ['F.zf', '!F.zf', 'F.cf', '!F.cf', 'F.nf', '!F.nf', 'F.vf', '!F.vf',
        '(F.cf && !F.zf)', '!(F.cf && !F.zf)', '(F.nf == F.vf)', '(F.nf != F.vf)',
        '(!F.zf && F.nf == F.vf)', '!(!F.zf && F.nf == F.vf)', '1', '1']


def shift_expr(v, typ, amt, sf):
    if amt == 0:
        return v
    if typ == 0:
        return f'(({UT[sf]}){v} << {amt})'
    if typ == 1:
        return f'(({UT[sf]}){v} >> {amt})'
    if typ == 2:
        return f'({UT[sf]})(({ST[sf]}){v} >> {amt})'
    return f'ror{64 if sf else 32}({v}, {amt})'


def ext_expr(n, opt, amt, sf):
    """Extended register operand (64-bit expression), then truncated by caller."""
    v = rx(n)
    e = ['(uint64_t)(uint8_t)', '(uint64_t)(uint16_t)', '(uint64_t)(uint32_t)', '(uint64_t)',
         '(uint64_t)(int64_t)(int8_t)', '(uint64_t)(int64_t)(int16_t)',
         '(uint64_t)(int64_t)(int32_t)', '(uint64_t)'][opt] + f'{v}'
    if amt:
        e = f'({e} << {amt})'
    return e if sf else f'(uint32_t){e}'


def decode_bitmask(n, imms, immr, sf):
    """DecodeBitMasks for logical immediates -> wmask (datasize bits)."""
    size = 64 if sf else 32
    ln = (n << 6) | (~imms & 0x3f)
    length = ln.bit_length() - 1
    if length < 1:
        raise Unsupported('bad_bitmask')
    levels = (1 << length) - 1
    s, r = imms & levels, immr & levels
    esize = 1 << length
    welem = (1 << (s + 1)) - 1
    welem = ((welem >> r) | (welem << (esize - r))) & ((1 << esize) - 1)
    out = 0
    for i in range(size // esize):
        out |= welem << (i * esize)
    return out


# ---------------------------------------------------------------- top level

def translate(w, pc):
    op0 = bits(w, 28, 25)
    if op0 in (8, 9):
        return dp_imm(w, pc)
    if op0 in (10, 11):
        return branch_sys(w, pc)
    if op0 & 5 == 4:
        return ldst(w, pc)
    if op0 & 7 == 5:
        return dp_reg(w, pc)
    if op0 & 7 == 7:
        from a64_simd import simd_fp
        return simd_fp(w, pc)
    if bits(w, 31, 16) == 0:
        return Insn('trap', f'a64_trap(c, {pc:#x}, {w:#x}); return;', tag='udf')
    raise Unsupported('reserved')


# ---------------------------------------------------------------- DP immediate

def dp_imm(w, pc):
    sf, rd, rn = bits(w, 31, 31), bits(w, 4, 0), bits(w, 9, 5)
    k = bits(w, 25, 23)
    if k <= 1:  # ADR / ADRP
        imm = sx((bits(w, 23, 5) << 2) | bits(w, 30, 29), 21)
        if w >> 31:
            return op(wx(rd, hexc((pc & ~0xfff) + (imm << 12))), 'adrp')
        return op(wx(rd, hexc(pc + imm)), 'adr')
    if k == 2:  # add/sub immediate
        opn, s, sh, imm = bits(w, 30, 30), bits(w, 29, 29), bits(w, 22, 22), bits(w, 21, 10)
        imm <<= 12 * sh
        a = rx(rn, sf, sp=True)
        if s:
            f = f'adds{64 if sf else 32}(&F, {a}, {"~" if opn else ""}({UT[sf]}){imm:#x}u, {opn})'
            return op(wx(rd, f, sf), 'subs_imm' if opn else 'adds_imm')
        return op(wx(rd, f'{a} {"-" if opn else "+"} {imm:#x}u', sf, sp=True), 'sub_imm' if opn else 'add_imm')
    if k == 4:  # logical immediate
        opc, n = bits(w, 30, 29), bits(w, 22, 22)
        if not sf and n:
            raise Unsupported('logimm_n')
        m = decode_bitmask(n, bits(w, 15, 10), bits(w, 21, 16), sf)
        a = rx(rn, sf)
        lit = hexc(m) if sf else f'{m:#x}u'
        if opc == 0:
            return op(wx(rd, f'{a} & {lit}', sf, sp=True), 'and_imm')
        if opc == 1:
            return op(wx(rd, f'{a} | {lit}', sf, sp=True), 'orr_imm')
        if opc == 2:
            return op(wx(rd, f'{a} ^ {lit}', sf, sp=True), 'eor_imm')
        return op(wx(rd, f'SETNZ{64 if sf else 32}(&F, {a} & {lit})', sf), 'ands_imm')
    if k == 5:  # move wide
        opc, hw, imm = bits(w, 30, 29), bits(w, 22, 21), bits(w, 20, 5)
        if opc == 1 or (not sf and hw > 1):
            raise Unsupported('movw')
        v = imm << (16 * hw)
        mask = (1 << (64 if sf else 32)) - 1
        if opc == 0:
            return op(wx(rd, hexc((~v) & mask), sf), 'movn')
        if opc == 2:
            return op(wx(rd, hexc(v), sf), 'movz')
        keep = ~(0xffff << (16 * hw)) & mask
        return op(wx(rd, f'({rx(rd, sf)} & {hexc(keep)}) | {hexc(v)}', sf), 'movk')
    if k == 6:  # bitfield
        opc, n, immr, imms = bits(w, 30, 29), bits(w, 22, 22), bits(w, 21, 16), bits(w, 15, 10)
        W = 64 if sf else 32
        if n != sf or opc == 3 or (not sf and (immr > 31 or imms > 31)):
            raise Unsupported('bitfield')
        src = f'(uint64_t){rx(rn, sf)}'

        def mask(wd):
            return hexc((1 << wd) - 1)
        if opc == 2:  # UBFM
            if imms >= immr:
                e = f'(({src} >> {immr}) & {mask(imms - immr + 1)})'
            else:
                e = f'(({src} & {mask(imms + 1)}) << {W - immr})'
            return op(wx(rd, e, sf), 'ubfm')
        if opc == 0:  # SBFM
            if imms >= immr:
                e = f'(uint64_t)((int64_t)({src} << {63 - imms}) >> {63 - imms + immr})'
            else:
                e = f'((uint64_t)((int64_t)({src} << {63 - imms}) >> {63 - imms}) << {W - immr})'
            return op(wx(rd, e, sf), 'sbfm')
        dst = f'(uint64_t){rx(rd, sf)}'  # BFM
        if imms >= immr:
            wd = imms - immr + 1
            e = f'({dst} & ~{mask(wd)}) | (({src} >> {immr}) & {mask(wd)})'
        else:
            wd, pos = imms + 1, W - immr
            e = f'({dst} & ~({mask(wd)} << {pos})) | (({src} & {mask(wd)}) << {pos})'
        return op(wx(rd, e, sf), 'bfm')
    if k == 7:  # EXTR
        rm, lsb = bits(w, 20, 16), bits(w, 15, 10)
        if bits(w, 30, 29) or bits(w, 22, 22) != sf or bits(w, 21, 21) or (not sf and lsb > 31):
            raise Unsupported('extr')
        W = 64 if sf else 32
        if lsb == 0:
            return op(wx(rd, rx(rm, sf), sf), 'extr')
        e = f'(({UT[sf]}){rx(rm, sf)} >> {lsb}) | (({UT[sf]}){rx(rn, sf)} << {W - lsb})'
        return op(wx(rd, e, sf), 'extr')
    raise Unsupported('dp_imm')


# ---------------------------------------------------------------- branches / system

SYSREG = {
    (3, 3, 13, 0, 2): 'c->tpidr',
    (3, 3, 13, 0, 3): 'c->tpidrro',
    (3, 3, 4, 4, 0): 'c->fpcr',
    (3, 3, 4, 4, 1): 'c->fpsr',
}


def branch_sys(w, pc):
    if bits(w, 30, 26) == 0b00101:  # B / BL
        t = pc + 4 * sx(bits(w, 25, 0), 26)
        return Insn('bl' if w >> 31 else 'b', target=t, tag='bl' if w >> 31 else 'b')
    if bits(w, 31, 24) == 0b01010100:  # B.cond
        if bits(w, 4, 4):
            raise Unsupported('bc')
        return Insn('bcond', COND[bits(w, 3, 0)], pc + 4 * sx(bits(w, 23, 5), 19), 'b.cond')
    if bits(w, 30, 25) == 0b011010:  # CBZ / CBNZ
        sf, rt = w >> 31, bits(w, 4, 0)
        c = f'{rx(rt, sf)} {"!=" if bits(w, 24, 24) else "=="} 0'
        return Insn('bcond', c, pc + 4 * sx(bits(w, 23, 5), 19), 'cbz')
    if bits(w, 30, 25) == 0b011011:  # TBZ / TBNZ
        b = (bits(w, 31, 31) << 5) | bits(w, 23, 19)
        c = f'((c->x[{bits(w, 4, 0)}] >> {b}) & 1) {"!=" if bits(w, 24, 24) else "=="} 0'
        if bits(w, 4, 0) == 31:
            c = '1' if not bits(w, 24, 24) else '0'
        return Insn('bcond', c, pc + 4 * sx(bits(w, 18, 5), 14), 'tbz')
    if bits(w, 31, 25) == 0b1101011:  # branch register
        opc, op2, op3, rn, op4 = bits(w, 24, 21), bits(w, 20, 16), bits(w, 15, 10), bits(w, 9, 5), bits(w, 4, 0)
        if op2 != 31 or op3 or op4:
            raise Unsupported('br_pac')
        if opc == 0:
            return Insn('br', rx(rn), tag='br')
        if opc == 1:
            return Insn('blr', rx(rn), tag='blr')
        if opc == 2:
            return Insn('ret', rx(rn), tag='ret')
        raise Unsupported('br_other')
    if bits(w, 31, 24) == 0b11010100:  # exceptions
        opc, ll = bits(w, 23, 21), bits(w, 1, 0)
        imm = bits(w, 20, 5)
        if opc == 0 and ll == 1:
            return op(f'a64_svc(c, {imm:#x}, {pc:#x});', 'svc')
        return Insn('trap', f'a64_trap(c, {pc:#x}, {w:#x}); return;', tag='brk')
    if bits(w, 31, 22) == 0b1101010100:  # system
        l, o0, op1, crn, crm, op2, rt = (bits(w, 21, 21), bits(w, 20, 19), bits(w, 18, 16),
                                         bits(w, 15, 12), bits(w, 11, 8), bits(w, 7, 5), bits(w, 4, 0))
        if not l and o0 == 0 and op1 == 3 and crn == 2 and rt == 31:  # hints
            hint = (crm << 3) | op2
            if hint == 1:
                return op('a64_yield();', 'yield')
            return op('', 'hint')  # nop, xpaclri (no PAC on this target), ...
        if not l and o0 == 0 and op1 == 3 and crn == 3 and rt == 31:  # barriers
            if op2 == 2:
                return op('c->excl_addr = ~0ull;', 'clrex')
            if op2 in (4, 5):
                return op('__atomic_thread_fence(__ATOMIC_SEQ_CST);', 'dmb')
            if op2 == 6:
                return op('', 'isb')
            raise Unsupported('barrier')
        if o0 >= 2:  # MRS / MSR register
            key = (o0, op1, crn, crm, op2)
            if key == (3, 3, 4, 2, 0):  # NZCV
                if l:
                    return op(wx(rt, '(F.nf << 31) | (F.zf << 30) | (F.cf << 29) | (F.vf << 28)'), 'mrs_nzcv')
                return op(f'SETNZCV(&F, ({rx(rt)} >> 28) & 15);', 'msr_nzcv')
            if l and key == (3, 3, 14, 0, 1):
                return op(wx(rt, 'a64_cntpct()'), 'mrs_cntpct')
            if l and key == (3, 3, 14, 0, 0):
                return op(wx(rt, '19200000ull'), 'mrs_cntfrq')
            if l and key == (3, 3, 0, 0, 7):
                return op(wx(rt, '4ull'), 'mrs_dczid')  # 64-byte DC ZVA blocks
            if key in SYSREG:
                if l:
                    return op(wx(rt, SYSREG[key]), 'mrs')
                if key == (3, 3, 13, 0, 3):
                    raise Unsupported('msr_ro')
                return op(f'{SYSREG[key]} = {rx(rt)};', 'msr')
            raise Unsupported(f'sysreg_{o0}_{op1}_{crn}_{crm}_{op2}')
        raise Unsupported('system')
    raise Unsupported('branch_sys')


# ---------------------------------------------------------------- loads / stores

def ld_gpr(size, sign, sf):
    """C expression type conversion for a GPR load of 1<<size bytes."""
    ld = f'LD{8 << size}(a)'
    if not sign:
        return ld
    st = ['int8_t', 'int16_t', 'int32_t'][size]
    return f'(uint64_t)(int64_t)({st}){ld}' if sf else f'(uint32_t)(int32_t)({st}){ld}'


def vld(size, rt):
    """Load FP/SIMD register t with 1<<size bytes at a (zeroing the rest)."""
    if size == 4:
        return f'c->v[{rt}] = LDQ(a);'
    f = ['b', 'h', 's', 'd'][size]
    return f'{{ V128 r = {{0}}; r.{f}[0] = LD{8 << size}(a); c->v[{rt}] = r; }}'


def vst(size, rt):
    if size == 4:
        return f'STQ(a, c->v[{rt}]);'
    f = ['b', 'h', 's', 'd'][size]
    return f'ST{8 << size}(a, c->v[{rt}].{f}[0]);'


def addr_block(rn, off, mode, body):
    """mode: 'off' (base+off), 'pre' (base+off, writeback), 'post' (base, then +off)."""
    base = rx(rn, sp=True)
    if mode == 'off':
        a = f'{base} + {hexc(off)}' if off else base
        return f'{{ uint64_t a = {a}; {body} }}'
    if mode == 'pre':
        return f'{{ uint64_t a = {base} + {hexc(off)}; {body} c->x[{rn}] = a; }}'
    return f'{{ uint64_t a = {base}; {body} c->x[{rn}] = a + {hexc(off)}; }}'


def ldst(w, pc):
    rt, rn = bits(w, 4, 0), bits(w, 9, 5)
    V = bits(w, 26, 26)

    if bits(w, 29, 24) == 0b001000 and not V:  # exclusive / ordered
        size, o2, l, o1, rs, o0, rt2 = (bits(w, 31, 30), bits(w, 23, 23), bits(w, 22, 22),
                                        bits(w, 21, 21), bits(w, 20, 16), bits(w, 15, 15), bits(w, 14, 10))
        nb = 8 << size
        a = rx(rn, sp=True)
        sf = int(size == 3)
        if o2 and not o1:  # LDAR / STLR (o0=0 is LDLAR/STLLR, treat the same)
            if l:
                return op(wx(rt, f'LDA({nb}, {a})', sf), 'ldar')
            return op(f'STL({nb}, {a}, {rx(rt, sf)});', 'stlr')
        if o2:
            raise Unsupported('cas')
        if not o1:
            if l:
                return op(wx(rt, f'LDX({nb}, c, {a}, {o0})', sf), 'ldxr')
            return op(f'{{ uint32_t s = STX({nb}, c, {a}, {rx(rt, sf)}, {o0}); {wx(rs, "s", 0)} }}', 'stxr')
        if size < 2:
            raise Unsupported('xp')
        if size == 2:  # 32-bit pair = one 64-bit exclusive
            if l:
                return op(f'{{ uint64_t v = LDX(64, c, {a}, {o0}); {wx(rt, "v", 0)} {wx(rt2, "v >> 32", 0)} }}', 'ldxp')
            val = f'((uint64_t){rx(rt, 0)} | ((uint64_t){rx(rt2, 0)} << 32))'
            return op(f'{{ uint32_t s = STX(64, c, {a}, {val}, {o0}); {wx(rs, "s", 0)} }}', 'stxp')
        if l:
            return op(f'{{ uint64_t lo, hi; a64_ldxp128(c, {a}, &lo, &hi); {wx(rt, "lo")} {wx(rt2, "hi")} }}', 'ldxp')
        return op(f'{{ uint32_t s = a64_stxp128(c, {a}, {rx(rt)}, {rx(rt2)}); {wx(rs, "s", 0)} }}', 'stxp')

    if bits(w, 29, 27) == 0b011 and bits(w, 25, 24) == 0:  # load literal
        opc = bits(w, 31, 30)
        addr = pc + 4 * sx(bits(w, 23, 5), 19)
        body_a = f'uint64_t a = {hexc(addr)};'
        if V:
            if opc == 3:
                raise Unsupported('ldr_lit_v')
            return op(f'{{ {body_a} {vld(opc + 2, rt)} }}', 'ldr_lit_v')
        if opc == 3:
            return op('', 'prfm')
        if opc == 2:
            return op(f'{{ {body_a} {wx(rt, ld_gpr(2, 1, 1))} }}', 'ldrsw_lit')
        return op(f'{{ {body_a} {wx(rt, ld_gpr(2 + opc, 0, 1), opc)} }}', 'ldr_lit')

    if bits(w, 29, 27) == 0b101:  # load/store pair
        opc, mode, l, imm7, rt2 = bits(w, 31, 30), bits(w, 24, 23), bits(w, 22, 22), bits(w, 21, 15), bits(w, 14, 10)
        if V:
            if opc == 3:
                raise Unsupported('ldp_v')
            size = opc + 2
        else:
            if opc == 3 or (opc == 1 and not l):
                raise Unsupported('ldp')
            size = 3 if opc == 2 else 2
        off = sx(imm7, 7) << size
        m = {0: 'off', 1: 'post', 2: 'off', 3: 'pre'}[mode]
        nb = 1 << size
        if V:
            if l:
                body = f'{{ uint64_t a0 = a; {{ uint64_t a = a0; {vld(size, rt)} }} {{ uint64_t a = a0 + {nb}; {vld(size, rt2)} }} }}'
            else:
                body = f'{{ uint64_t a0 = a; {{ uint64_t a = a0; {vst(size, rt)} }} {{ uint64_t a = a0 + {nb}; {vst(size, rt2)} }} }}'
            return op(addr_block(rn, off, m, body), 'ldp_v' if l else 'stp_v')
        sf = int(size == 3)
        if l:
            sign = opc == 1
            t1 = ld_gpr(size, sign, 1 if sign else sf).replace('(a)', '(a)')
            t2 = ld_gpr(size, sign, 1 if sign else sf).replace('(a)', f'(a + {nb})')
            body = f'{{ uint64_t v1 = {t1}, v2 = {t2}; {wx(rt, "v1", 1 if sign else sf)} {wx(rt2, "v2", 1 if sign else sf)} }}'
            return op(addr_block(rn, off, m, body), 'ldpsw' if sign else 'ldp')
        body = f'ST{8 << size}(a, {rx(rt, sf)}); ST{8 << size}(a + {nb}, {rx(rt2, sf)});'
        return op(addr_block(rn, off, m, body), 'stp')

    if bits(w, 29, 27) == 0b111:  # load/store register
        size, opc = bits(w, 31, 30), bits(w, 23, 22)
        if bits(w, 24, 24):  # unsigned offset
            scale = size + (4 - size if V and opc >= 2 else 0) if V else size
            if V and opc >= 2:
                scale = 4
            off, mode = bits(w, 21, 10) << scale, 'off'
        elif not bits(w, 21, 21):  # 9-bit immediate forms
            k = bits(w, 11, 10)
            off = sx(bits(w, 20, 12), 9)
            mode = {0: 'off', 1: 'post', 2: 'off', 3: 'pre'}[k]  # 2 = LDTR (unprivileged), same at EL0
        elif bits(w, 11, 10) == 2:  # register offset
            mode = 'reg'
        else:
            raise Unsupported('ldst_atomic')

        if mode == 'reg':
            rm, opt, s = bits(w, 20, 16), bits(w, 15, 13), bits(w, 12, 12)
            if not opt & 2:
                raise Unsupported('ldst_regext')
            scale = (4 if V and opc >= 2 else size) if s else 0
            idx = ext_expr(rm, opt, scale, 1)

            def blk(body):
                return f'{{ uint64_t a = {rx(rn, sp=True)} + {idx}; {body} }}'
        else:
            def blk(body):
                return addr_block(rn, off, mode, body)

        if V:
            if opc >= 2:
                if size:
                    raise Unsupported('ldst_v')
                vs = 4
            else:
                vs = size
            return op(blk(vld(vs, rt) if opc & 1 else vst(vs, rt)), 'ldr_v' if opc & 1 else 'str_v')
        if opc == 0:
            sf = int(size == 3)
            return op(blk(f'ST{8 << size}(a, {rx(rt, sf)});'), 'str')
        if opc == 1:
            return op(blk(wx(rt, f'LD{8 << size}(a)', 1)), 'ldr')
        if size == 3:
            if opc == 2:
                return op('', 'prfm')
            raise Unsupported('ldst')
        if opc == 2:  # sign-extend to 64
            if size == 3:
                raise Unsupported('ldst')
            return op(blk(wx(rt, ld_gpr(size, 1, 1))), 'ldrs_x')
        if size >= 2:
            raise Unsupported('ldst')
        return op(blk(wx(rt, ld_gpr(size, 1, 0), 0)), 'ldrs_w')

    if bits(w, 31, 31) == 0 and bits(w, 29, 24) == 0b001100:  # AdvSIMD multiple structures
        return ld_multi(w)
    if bits(w, 31, 31) == 0 and bits(w, 29, 24) == 0b001101:  # AdvSIMD single structure
        return ld_single(w)
    raise Unsupported('ldst')


def simd_post(w, rn, total):
    """Post-index writeback for AdvSIMD structure loads."""
    if not bits(w, 23, 23):
        return ''
    rm = bits(w, 20, 16)
    inc = f'{total}ull' if rm == 31 else rx(rm)
    return f'c->x[{rn}] = c->x[{rn}] + {inc};'


def ld_multi(w):
    q, l, opcode, size = bits(w, 30, 30), bits(w, 22, 22), bits(w, 15, 12), bits(w, 11, 10)
    if not bits(w, 23, 23) and bits(w, 20, 16):
        raise Unsupported('ldst_multi')
    rt, rn = bits(w, 4, 0), bits(w, 9, 5)
    rs = {0: (1, 4), 2: (4, 1), 4: (1, 3), 6: (3, 1), 7: (1, 1), 8: (1, 2), 10: (2, 1)}
    if opcode not in rs or (size == 3 and not q and rs[opcode][1] > 1):
        raise Unsupported('ldst_multi')
    rpt, selem = rs[opcode]
    eb = 1 << size
    elems = (16 if q else 8) // eb
    f = ['b', 'h', 's', 'd'][size]
    lines, off = [], 0
    regs = set()
    for r in range(rpt):
        for e in range(elems):
            tt = (rt + r) % 32
            for s in range(selem):
                regs.add(tt)
                if l:
                    lines.append(f'R[{tt}].{f}[{e}] = LD{8 * eb}(a + {off});')
                else:
                    lines.append(f'ST{8 * eb}(a + {off}, c->v[{tt}].{f}[{e}]);')
                off += eb
                tt = (tt + 1) % 32
    pre = ''
    post = ''
    if l:
        pre = ' '.join(f'R[{t}] = c->v[{t}];' for t in sorted(regs))
        zero = '' if q else ' '.join(f'R[{t}].d[1] = 0;' for t in sorted(regs))
        post = zero + ' ' + ' '.join(f'c->v[{t}] = R[{t}];' for t in sorted(regs))
        pre = f'V128 R[32]; {pre}'
    body = f'{{ uint64_t a = {rx(rn, sp=True)}; {pre} {" ".join(lines)} {post} {simd_post(w, rn, off)} }}'
    return op(body, ('ld' if l else 'st') + f'{selem}_multi')


def ld_single(w):
    q, l, r, opcode, s, size = (bits(w, 30, 30), bits(w, 22, 22), bits(w, 21, 21),
                                bits(w, 15, 13), bits(w, 12, 12), bits(w, 11, 10))
    if not bits(w, 23, 23) and bits(w, 20, 16):
        raise Unsupported('ldst_single')
    rt, rn = bits(w, 4, 0), bits(w, 9, 5)
    scale = opcode >> 1
    selem = ((opcode & 1) << 1 | r) + 1
    replicate = False
    if scale == 3:
        if not l or s:
            raise Unsupported('ldst_single')
        scale, replicate = size, True
    elif scale == 0:
        index = (q << 3) | (s << 2) | size
    elif scale == 1:
        if size & 1:
            raise Unsupported('ldst_single')
        index = (q << 2) | (s << 1) | (size >> 1)
    else:
        if size & 2:
            raise Unsupported('ldst_single')
        if size & 1 == 0:
            index = (q << 1) | s
        else:
            if s:
                raise Unsupported('ldst_single')
            index, scale = q, 3
    eb = 1 << scale
    f = ['b', 'h', 's', 'd'][scale]
    lines = []
    for k in range(selem):
        tt = (rt + k) % 32
        if replicate:
            n = (16 if q else 8) // eb
            lines.append(f'{{ V128 R = {{0}}; uint64_t e = LD{8 * eb}(a + {k * eb}); '
                         f'for (int i = 0; i < {n}; i++) R.{f}[i] = e; c->v[{tt}] = R; }}')
        elif l:
            lines.append(f'c->v[{tt}].{f}[{index}] = LD{8 * eb}(a + {k * eb});')
        else:
            lines.append(f'ST{8 * eb}(a + {k * eb}, c->v[{tt}].{f}[{index}]);')
    body = f'{{ uint64_t a = {rx(rn, sp=True)}; {" ".join(lines)} {simd_post(w, rn, selem * eb)} }}'
    return op(body, ('ld' if l else 'st') + f'{selem}{"r" if replicate else ""}_single')


# ---------------------------------------------------------------- DP register

def dp_reg(w, pc):
    sf, rd, rn, rm = bits(w, 31, 31), bits(w, 4, 0), bits(w, 9, 5), bits(w, 20, 16)
    op1, op2 = bits(w, 28, 28), bits(w, 24, 21)
    W = 64 if sf else 32
    if not op1:
        if not op2 & 8:  # logical shifted register
            opc, sh, n, imm6 = bits(w, 30, 29), bits(w, 23, 22), bits(w, 21, 21), bits(w, 15, 10)
            if not sf and imm6 > 31:
                raise Unsupported('log_reg')
            b = shift_expr(rx(rm, sf), sh, imm6, sf)
            if n:
                b = f'~{b}' if sf else f'(uint32_t)~{b}'
            a = rx(rn, sf)
            if opc == 0:
                return op(wx(rd, f'{a} & {b}', sf), 'and_reg')
            if opc == 1:
                return op(wx(rd, f'{a} | {b}', sf), 'orr_reg')
            if opc == 2:
                return op(wx(rd, f'{a} ^ {b}', sf), 'eor_reg')
            return op(wx(rd, f'SETNZ{W}(&F, {a} & {b})', sf), 'ands_reg')
        opn, s = bits(w, 30, 30), bits(w, 29, 29)
        if not op2 & 1:  # add/sub shifted register
            sh, imm6 = bits(w, 23, 22), bits(w, 15, 10)
            if sh == 3 or (not sf and imm6 > 31):
                raise Unsupported('addsub_reg')
            a, b = rx(rn, sf), shift_expr(rx(rm, sf), sh, imm6, sf)
        else:  # extended register
            opt, imm3 = bits(w, 15, 13), bits(w, 12, 10)
            if bits(w, 23, 22) or imm3 > 4:
                raise Unsupported('addsub_ext')
            a, b = rx(rn, sf, sp=True), ext_expr(rm, opt, imm3, sf)
        if s:
            e = f'adds{W}(&F, {a}, ({UT[sf]}){"~" if opn else ""}({UT[sf]})({b}), {opn})'
            return op(wx(rd, e, sf), 'subs_reg' if opn else 'adds_reg')
        return op(wx(rd, f'{a} {"-" if opn else "+"} ({UT[sf]})({b})', sf, sp=bool(op2 & 1)),
                  'sub_reg' if opn else 'add_reg')

    if op2 == 0:  # ADC / SBC
        if bits(w, 15, 10):
            raise Unsupported('adc')
        opn, s = bits(w, 30, 30), bits(w, 29, 29)
        b = f'({UT[sf]})~{rx(rm, sf)}' if opn else rx(rm, sf)
        if s:
            return op(wx(rd, f'adds{W}(&F, {rx(rn, sf)}, {b}, ({UT[sf]})F.cf)', sf), 'adcs')
        return op(wx(rd, f'{rx(rn, sf)} + {b} + ({UT[sf]})F.cf', sf), 'adc')
    if op2 == 2:  # conditional compare
        opn, s, cond, nzcv = bits(w, 30, 30), bits(w, 29, 29), bits(w, 15, 12), bits(w, 3, 0)
        if not s or bits(w, 10, 10) or bits(w, 4, 4):
            raise Unsupported('ccmp')
        b = f'{rm}u' if bits(w, 11, 11) else rx(rm, sf)
        b = f'({UT[sf]})~({UT[sf]}){b}' if opn else f'({UT[sf]}){b}'
        return op(f'if ({COND[cond]}) adds{W}(&F, {rx(rn, sf)}, {b}, {opn}); else SETNZCV(&F, {nzcv});',
                  'ccmp' if opn else 'ccmn')
    if op2 == 4:  # conditional select
        opn, s, cond, o2 = bits(w, 30, 30), bits(w, 29, 29), bits(w, 15, 12), bits(w, 11, 10)
        if s or o2 > 1:
            raise Unsupported('csel')
        m = rx(rm, sf)
        alt = {(0, 0): m, (0, 1): f'{m} + 1', (1, 0): f'~{m}', (1, 1): f'-{m}'}[(opn, o2)]
        return op(wx(rd, f'{COND[cond]} ? ({UT[sf]}){rx(rn, sf)} : ({UT[sf]})({alt})', sf), 'csel')
    if op2 == 6:
        if bits(w, 30, 30):  # 1-source
            opcode = bits(w, 15, 10)
            if bits(w, 29, 29) or rm:
                raise Unsupported('dp1')
            a = rx(rn, sf)
            if opcode == 0:
                return op(wx(rd, f'__builtin_bitreverse{W}({a})', sf), 'rbit')
            if opcode == 1:
                e = (f'((({a} >> 8) & 0x00ff00ff00ff00ffull) | (({a} & 0x00ff00ff00ff00ffull) << 8))' if sf else
                     f'((({a} >> 8) & 0x00ff00ffu) | (({a} & 0x00ff00ffu) << 8))')
                return op(wx(rd, e, sf), 'rev16')
            if opcode == 2:
                if sf:
                    e = f'((uint64_t)__builtin_bswap32((uint32_t){a}) | ((uint64_t)__builtin_bswap32((uint32_t)({a} >> 32)) << 32))'
                else:
                    e = f'__builtin_bswap32({a})'
                return op(wx(rd, e, sf), 'rev32' if sf else 'rev')
            if opcode == 3:
                if not sf:
                    raise Unsupported('dp1')
                return op(wx(rd, f'__builtin_bswap64({a})', sf), 'rev')
            if opcode == 4:
                return op(wx(rd, f'clz{W}({a})', sf), 'clz')
            if opcode == 5:
                return op(wx(rd, f'cls{W}({a})', sf), 'cls')
            raise Unsupported('dp1')
        opcode = bits(w, 15, 10)  # 2-source
        if bits(w, 29, 29):
            raise Unsupported('dp2')
        a, b = rx(rn, sf), rx(rm, sf)
        if opcode == 2:
            return op(wx(rd, f'udiv{W}({a}, {b})', sf), 'udiv')
        if opcode == 3:
            return op(wx(rd, f'sdiv{W}(({ST[sf]}){a}, ({ST[sf]}){b})', sf), 'sdiv')
        if 8 <= opcode <= 11:
            k = opcode - 8
            amt = f'({b} & {W - 1})'
            e = [f'({UT[sf]}){a} << {amt}', f'({UT[sf]}){a} >> {amt}',
                 f'({UT[sf]})(({ST[sf]}){a} >> {amt})', f'ror{W}({a}, {amt})'][k]
            return op(wx(rd, e, sf), 'shiftv')
        if 16 <= opcode <= 23:
            sz, cpoly = opcode & 3, opcode & 4
            if (sz == 3) != bool(sf):
                raise Unsupported('crc')
            poly = '0x82F63B78u' if cpoly else '0xEDB88320u'
            val = rx(rm, 1) if sz == 3 else f'(uint64_t){rx(rm, 0)}'
            return op(wx(rd, f'crc32_a64({rx(rn, 0)}, {val}, {1 << sz}, {poly})', 0), 'crc32')
        raise Unsupported('dp2')
    if op2 & 8:  # 3-source
        op54, op31, o0, ra = bits(w, 30, 29), bits(w, 23, 21), bits(w, 15, 15), bits(w, 14, 10)
        if op54:
            raise Unsupported('dp3')
        a, b, acc = rx(rn, sf), rx(rm, sf), rx(ra, sf)
        sgn = '-' if o0 else '+'
        if op31 == 0:
            return op(wx(rd, f'{acc} {sgn} ({UT[sf]})({a} * {b})', sf), 'madd')
        if not sf:
            raise Unsupported('dp3')
        if op31 in (1, 5):
            t = 'int64_t)(int32_t' if op31 == 1 else 'uint64_t)(uint32_t'
            return op(wx(rd, f'{acc} {sgn} (uint64_t)(({t}){a} * ({t}){b})'), 'smaddl' if op31 == 1 else 'umaddl')
        if op31 == 2 and not o0:
            return op(wx(rd, f'smulh((int64_t){a}, (int64_t){b})'), 'smulh')
        if op31 == 6 and not o0:
            return op(wx(rd, f'umulh({a}, {b})'), 'umulh')
        raise Unsupported('dp3')
    raise Unsupported('dp_reg')

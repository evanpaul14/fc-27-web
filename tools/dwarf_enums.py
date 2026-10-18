"""Enumerations from `llvm-dwarfdump --debug-info` output, with their enclosing
namespaces/classes: one line per enumerator, `Scope::Enum<TAB>NAME<TAB>value`.

usage: llvm-dwarfdump --debug-info X.nrs > X.dwarf
       python3 -I tools/dwarf_enums.py X.dwarf > enums.tsv
"""
import re, sys

TAG = re.compile(r'^0x[0-9a-f]+:(\s+)(DW_TAG_\w+)')
ATTR = re.compile(r'^\s+(DW_AT_\w+)\s+\((.*)\)$')


def main():
    stack = []  # (indent, tag, name)
    cur = None  # [indent, tag, attrs]
    seen = set()

    def flush():
        if cur is None:
            return
        ind, tag, attrs = cur
        while stack and stack[-1][0] >= ind:
            stack.pop()
        name = attrs.get('DW_AT_name', '').strip('"')
        if tag == 'DW_TAG_enumerator' and stack and stack[-1][1] == 'DW_TAG_enumeration_type':
            scope = '::'.join(s[2] for s in stack if s[2])
            v = attrs.get('DW_AT_const_value', '')
            m = re.match(r'(-?0x[0-9a-f]+|-?\d+)', v)
            key = (scope, name)
            if m and key not in seen:
                seen.add(key)
                print(f'{scope}\t{name}\t{int(m.group(1), 0)}')
        stack.append((ind, tag, name if tag in ('DW_TAG_namespace', 'DW_TAG_structure_type', 'DW_TAG_class_type',
                                                  'DW_TAG_enumeration_type', 'DW_TAG_union_type') else ''))

    for line in open(sys.argv[1], errors='replace'):
        m = TAG.match(line)
        if m:
            flush()
            cur = [len(m.group(1)), m.group(2), {}]
            continue
        m = ATTR.match(line)
        if m and cur is not None:
            cur[2].setdefault(m.group(1), m.group(2))
    flush()


if __name__ == '__main__':
    main()

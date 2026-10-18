"""Decoder coverage over every word of main's text segment.

Counts translated tags and Unsupported reasons, weighted by how often each
word occurs, and writes OUT/coverage.json plus a sample word per failure.

usage: python3 -I recomp/coverage.py extracted/code/main extracted/analysis/main
"""
import collections, json, os, sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from a64 import translate, Unsupported  # noqa: E402


def work(chunk):
    words, counts = chunk
    ok, bad, sample = collections.Counter(), collections.Counter(), {}
    for w, k in zip(words.tolist(), counts.tolist()):
        try:
            ok[translate(w, 0x1000).tag] += k
        except Unsupported as e:
            tag = str(e)
            bad[tag] += k
            sample.setdefault(tag, w)
    return ok, bad, sample


def main():
    prefix, out = sys.argv[1], sys.argv[2]
    info = json.load(open(prefix + '.json'))
    text = next(s for s in info['segments'] if s['name'] == 'text')
    img = np.fromfile(prefix + '.bin', dtype='<u4', count=text['size'] // 4, offset=text['mem_off'])
    words, counts = np.unique(img, return_counts=True)
    print(f'{len(img)} words, {len(words)} unique')
    n = os.cpu_count() * 8
    chunks = [(words[i::n], counts[i::n]) for i in range(n)]
    ok, bad, sample = collections.Counter(), collections.Counter(), {}
    with ProcessPoolExecutor(os.cpu_count()) as ex:
        for o, b, s in ex.map(work, chunks):
            ok.update(o); bad.update(b)
            for k, v in s.items():
                sample.setdefault(k, v)
    total = sum(ok.values()) + sum(bad.values())
    print(f'translated {sum(ok.values())}/{total} ({100 * sum(ok.values()) / total:.4f}%)')
    for tag, k in bad.most_common(40):
        print(f'  {k:9d}  {tag:24s} {sample[tag]:#010x}')
    json.dump({'ok': dict(ok.most_common()), 'unsupported': dict(bad.most_common()),
               'sample': {k: f'{v:#010x}' for k, v in sample.items()}},
              open(os.path.join(out, 'coverage.json'), 'w'), indent=1)


if __name__ == '__main__':
    main()

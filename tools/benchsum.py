#!/usr/bin/env python3
"""Cost per frame for each segment of a tools/bench.py run, to compare runs whatever the clock was.

  python3 -I tools/benchsum.py build/b5 [build/b6 ...] [--skip S]

Per segment (from marks.log), skipping its first S seconds (default 10): fps, giga-instructions per frame (all cores;
the clock-independent CPU cost), CPU ms per frame and the share on P cores, the P clock, GPU ms per frame and the
waits from the renderer's stats line, and the last line's live textures / memory.
"""
import os
import re
import sys

args = [a for a in sys.argv[1:]]
skip = 10.0
if '--skip' in args:
    i = args.index('--skip')
    skip = float(args[i + 1])
    del args[i:i + 2]


def load(d):
    marks = []
    for line in open(os.path.join(d, 'marks.log')):
        t, s = line.split(' ', 1)
        marks.append((float(t), s.strip()))
    segs = [(t, s) for t, s in marks if s.startswith('seg ')]
    end = [t for t, s in marks if s == 'end']
    bounds = [(t, segs[i + 1][0] if i + 1 < len(segs) else (end[0] if end else 1e9), s) for i, (t, s) in enumerate(segs)]
    fps = []  # (t, frame, gpu ms, wait fence, wait flush)
    rx = re.compile(r'^([\d.]+) \[metal\] frame (\d+) [\d.]+ fps .*?gpu (\d+)ms.*?mem (\d+) MB(?:.*?waits fence (\d+)ms/\d+ flush (\d+)ms)?')
    for line in open(os.path.join(d, 'game.log'), errors='replace'):
        m = rx.match(line)
        if m:
            fps.append(tuple(float(x) if x is not None else 0.0 for x in m.groups()))
    cores = []
    cx = re.compile(r'^([\d.]+) cpu ([\d.]+) P ([\d.]+) E ([\d.]+) .*?Ginst ([\d.]+) P ([\d.]+) .*?GHz all [\d.]+ P ([\d.]+)(?:.*thermal (\d+))?')
    path = os.path.join(d, 'cores.log')
    if os.path.exists(path):
        for line in open(path):
            m = cx.match(line)
            if m:
                cores.append(tuple(float(x) if x is not None else -1 for x in m.groups()))
    return bounds, fps, cores


for d in args:
    bounds, fps, cores = load(d)
    print(d)
    print('  %-34s %5s %6s %7s %5s %5s %6s %6s %6s %6s %3s' % ('segment', 'fps', 'Gi/f', 'cpu ms', 'P%', 'PGHz', 'gpu ms',
                                                           'fence', 'flush', 'mem', 'thm'))
    for a, b, name in bounds:
        a += skip
        f = [x for x in fps if a < x[0] <= b]
        c = [x for x in cores if a < x[0] <= b]
        if len(f) < 2:
            continue
        frames = f[-1][1] - f[0][1]
        secs = f[-1][0] - f[0][0]
        gpu = sum(x[2] for x in f[1:])
        fence = sum(x[4] for x in f[1:])
        flush = sum(x[5] for x in f[1:])
        cpu = sum(x[1] for x in c)
        pcpu = sum(x[2] for x in c)
        inst = sum(x[4] for x in c)
        csecs = c[-1][0] - c[0][0] + 2 if c else 0
        cf = frames / secs * csecs if secs else 0  # frames over the cores samples' span
        ghz = sum(x[6] * x[2] for x in c) / pcpu if pcpu else 0
        th = max((x[7] for x in c), default=-1)
        print('  %-34s %5.1f %6.3f %7.1f %5.0f %5.2f %6.1f %6.1f %6.1f %6.0f %3s' % (
            name[:34], frames / secs, inst / cf if cf else 0, cpu * 1e3 / cf if cf else 0, 100 * pcpu / cpu if cpu else 0,
            ghz, gpu / frames, fence / frames, flush / frames, f[-1][3], '%d' % th if th >= 0 else '-'))

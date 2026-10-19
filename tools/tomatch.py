#!/usr/bin/env python3
"""Drive a running game (FIFA_CONTROL=<fifo>) from boot to Kick Off's match (Bournemouth v Brighton).

  python3 -I tools/tomatch.py <fifo> [--shot build/live.png] [--classify PNG...]

Fixed timings alone don't work: the "sign in to the EA Servers" notice turns up at a different point in each run
and swallows presses, and the menu falls back to the "Press Any Button" attract screen when left alone. So each step
takes a screenshot and looks at a few pixels: the main menu has its teal panel on the right, the notice is flat grey.
From the menu, `up 12` reaches FC News (the list doesn't wrap) and `down 4` Kick Off. Prints what it saw at each step
and exits 0 once the match should be loading (the last screenshot is left at --shot).
"""
import os
import struct
import sys
import time
import zlib


def read_png(path):
    """(width, height, channels, rows) of an 8-bit non-interlaced PNG; rows are bytes."""
    data = open(path, 'rb').read()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    pos, idat, w = 8, b'', 0
    while pos < len(data):
        n, kind = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b'IHDR':
            w, h, depth, ctype, _, _, interlace = struct.unpack('>IIBBBBB', body)
            if depth != 8 or interlace:
                raise ValueError('unsupported PNG')
            ch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
        elif kind == b'IDAT':
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * ch
    rows, prev = [], bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + b) & 255
            elif f == 3: line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    return w, h, ch, rows


def pixel(img, fx, fy):
    w, h, ch, rows = img
    x, y = min(int(fx * w), w - 1), min(int(fy * h), h - 1)
    return tuple(rows[y][x * ch:x * ch + 3])


def classify(path):
    """'menu', 'notice' or 'other'."""
    img = read_png(path)
    panel = [pixel(img, 0.93, fy) for fy in (0.3, 0.5)]
    if all(r < 20 and g > r + 12 and b > r + 12 and abs(g - b) < 8 for r, g, b in panel):  # dark teal panel of the menu
        return 'menu'
    # the notice: the corners flat dark grey (~20, not the black of a loading screen)
    flat = [pixel(img, fx, fy) for fx, fy in ((0.05, 0.1), (0.95, 0.1), (0.05, 0.9), (0.95, 0.9))]
    if all(12 <= r <= 40 and max(r, g, b) - min(r, g, b) < 8 for r, g, b in flat):
        return 'notice'
    return 'other'


class Game:
    def __init__(self, fifo, shot):
        self.fifo, self.shot = fifo, shot

    def send(self, cmds):
        with open(self.fifo, 'w') as f:
            f.write(cmds + '\n')

    def look(self, cmds='wait 30'):
        """Run cmds, then take a screenshot and classify it."""
        try:
            os.remove(self.shot)
        except FileNotFoundError:
            pass
        self.send(cmds + '; shot ' + self.shot)
        for _ in range(3000):
            if os.path.exists(self.shot) and os.path.getsize(self.shot) > 0:
                time.sleep(0.5)  # (written in place: let it finish)
                what = classify(self.shot)
                print('[tomatch] %s -> %s' % (cmds, what), flush=True)
                return what
            time.sleep(0.2)
        raise SystemExit('[tomatch] no screenshot (is the game running with FIFA_CONTROL=%s?)' % self.fifo)


def main():
    args = sys.argv[1:]
    if args[:1] == ['--classify']:
        for p in args[1:]:
            print(p, classify(p))
        return
    if not args:
        raise SystemExit(__doc__)
    fifo, shot = args[0], 'build/live.png'
    if '--shot' in args:
        shot = args[args.index('--shot') + 1]
    g = Game(fifo, shot)
    what = g.look('wait 1200')
    for _ in range(40):  # intro, attract screen, notices -> the main menu
        if what == 'menu':
            break
        what = g.look('a; wait 300')
    else:
        raise SystemExit('[tomatch] never reached the main menu')
    for attempt in range(3):
        g.send('up 12; wait 30; down 4; wait 60')  # Kick Off
        what = g.look('a; wait 250')  # Enter Kick Off
        if what == 'notice':  # (it can turn up here: dismiss it and pick Kick Off again)
            g.look('a; wait 300')
            continue
        # welcome pages, gameplay type, Match Preview -> Start Match, difficulty notice
        what = g.look('a; wait 250; a; wait 350; a; wait 250; a; wait 900')
        if what != 'notice':
            print('[tomatch] the match should be loading; screenshot at %s' % shot)
            return
        g.look('a; wait 300')
        g.look('b; wait 120; b; wait 120; b; wait 120')
        g.look('wait 60')
    raise SystemExit('[tomatch] gave up')


if __name__ == '__main__':
    main()

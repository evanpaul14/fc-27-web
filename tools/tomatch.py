#!/usr/bin/env python3
"""Drive a running game (FIFA_CONTROL=<fifo>) from boot to Kick Off's match (Bournemouth v Brighton).

  python3 -I tools/tomatch.py <fifo> [--shot build/live.png] [--from-menu] [--classify PNG...]

The game is started with FIFA_CONTROL=<fifo> FIFA_ROMFS=extracted/update/romfs_program.json ./build/fifa (mkfifo first).

--from-menu: the game is already past boot (skips the initial 1200-frame wait).

Fixed timings alone don't work: the "sign in to the EA Servers" notice turns up at a different point in each run
and swallows presses, and the menu falls back to the "Press Any Button" attract screen when left alone. So each step
takes a screenshot and looks at a few pixels: the main menu has its teal panel on the right, the notice is an Ok
button on neutral dark grey. The menu list scrolls, so the highlighted entry (drawn
white, the others grey) is identified by its label's size, not its position, and it steps one press at a time until
Kick Off is selected.
The highlight lags the presses (it slides, and a press can show up a screenshot later), so after each press it waits
until two screenshots in a row agree.
Prints what it saw at each step (each screenshot is kept as <shot>-NN.png) and exits 0 once the match should be
loading (the last screenshot is left at --shot).
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


KICK_OFF = 4  # main menu entries: FC News, Ultimate Team, Manager Career, Player Career, Kick Off, ...


def classify_img(img):
    """'menu', 'notice' or 'other'."""
    panel = [pixel(img, 0.93, fy) for fy in (0.3, 0.5)]
    if all(r < 20 and g > r + 12 and b > r + 12 and abs(g - b) < 8 for r, g, b in panel):  # dark teal panel of the menu
        return 'menu'
    # the notice: a light Ok button below the text, neutral dark grey either side (other dialogs are tinted)
    button = pixel(img, 0.52, 0.66)
    sides = [pixel(img, fx, 0.66) for fx in (0.40, 0.60)]
    if min(button) > 150 and all(max(c) < 45 and max(c) - min(c) < 10 for c in sides):
        return 'notice'
    return 'other'


def classify(path):
    return classify_img(read_png(path))


# highlighted label at 720p: (width, white pixels), in menu order (measured with `down` from FC News)
MENU_LABELS = [(116, 834), (211, 1403), (244, 1806), (210, 1458), (118, 770), (201, 1323), (123, 934), (206, 1493),
               (216, 1517), (252, 1888), (214, 1514)]


def menu_entry(img):
    """Index in MENU_LABELS of the highlighted main menu entry, or None."""
    w, h, ch, rows = img
    x0, x1 = int(0.074 * w), int(0.28 * w)
    lo, hi, n, seen = x1, x0, 0, False
    for y in range(int(0.14 * h), int(0.75 * h)):
        row = rows[y]
        white = [x for x in range(x0, x1) if min(row[x * ch:x * ch + 3]) > 235]
        if len(white) > 3:
            seen = True
            lo, hi, n = min(lo, white[0]), max(hi, white[-1]), n + len(white)
        elif seen:
            break  # (the first highlighted line only)
    if not seen:
        return None
    s = 720 / h
    size = ((hi - lo) * s, n * s * s)
    d, i = min((abs(size[0] - lw) / lw + abs(size[1] - ln) / ln, i) for i, (lw, ln) in enumerate(MENU_LABELS))
    return i if d < 0.1 else None


class Game:
    def __init__(self, fifo, shot):
        self.fifo, self.shot = fifo, shot
        self.step = 0
        self.img = None

    def send(self, cmds):
        with open(self.fifo, 'w') as f:
            f.write(cmds + '\n')

    def settle(self, what):
        """Screenshots until two in a row read the same (classification, menu entry)."""
        prev = (what, menu_entry(self.img) if what == 'menu' else None)
        for _ in range(10):
            what = self.look('wait 30')
            cur = (what, menu_entry(self.img) if what == 'menu' else None)
            if cur == prev:
                return what
            prev = cur
        return what

    def look(self, cmds='wait 30'):
        """Run cmds, then take a screenshot and classify it."""
        try:
            os.remove(self.shot)
        except FileNotFoundError:
            pass
        self.send(cmds + '; shot ' + self.shot)
        for _ in range(9000):  # (30 minutes: frame waits are slow in the match's menus)
            if os.path.exists(self.shot) and os.path.getsize(self.shot) > 0:
                time.sleep(0.5)  # (written in place: let it finish)
                self.img = read_png(self.shot)
                what = classify_img(self.img)
                self.step += 1
                keep = '%s-%02d.png' % (os.path.splitext(self.shot)[0], self.step)  # each step's screenshot stays
                with open(self.shot, 'rb') as src, open(keep, 'wb') as dst:
                    dst.write(src.read())
                entry = menu_entry(self.img) if what == 'menu' else None
                print('[tomatch] %s -> %s%s (%s)' % (cmds, what, '' if entry is None else ' at entry %d' % entry, keep),
                      flush=True)
                return what
            time.sleep(0.2)
        raise SystemExit('[tomatch] no screenshot (is the game running with FIFA_CONTROL=%s?)' % self.fifo)


def main():
    args = sys.argv[1:]
    if args[:1] == ['--classify']:
        for p in args[1:]:
            img = read_png(p)
            print(p, classify_img(img), menu_entry(img))
        return
    if not args:
        raise SystemExit(__doc__)
    fifo, shot = args[0], 'build/live.png'
    if '--shot' in args:
        shot = args[args.index('--shot') + 1]
    g = Game(fifo, shot)
    what = g.look('wait 30' if '--from-menu' in args else 'wait 1200')
    for _ in range(40):  # intro, attract screen, notices -> the main menu
        if what == 'menu':
            break
        what = g.look('a; wait 300')
    else:
        raise SystemExit('[tomatch] never reached the main menu')
    for attempt in range(3):
        what = g.settle(what)
        for _ in range(30):  # select Kick Off, one press at a time
            entry = menu_entry(g.img) if what == 'menu' else None
            if entry == KICK_OFF:
                break
            if what == 'notice':
                what = g.look('a; wait 300')
            elif entry is None:  # a submenu or the attract screen
                what = g.look('b; wait 90')
            else:
                what = g.settle(g.look(('down' if entry < KICK_OFF else 'up') + '; wait 30'))
        else:
            raise SystemExit('[tomatch] could not select Kick Off')
        what = g.look('a; wait 250')  # Enter Kick Off
        if what == 'notice':  # (it turns up here: dismissing it shows Kick Off's home, or the menu again)
            what = g.look('a; wait 300')
            if what in ('menu', 'notice'):
                continue
        # welcome pages, gameplay type, Match Preview -> Start Match, difficulty notice
        what = g.look('a; wait 250; a; wait 350; a; wait 250; a; wait 900')
        if what != 'notice':
            print('[tomatch] the match should be loading; screenshot at %s' % shot)
            return
        g.look('a; wait 300')
        what = g.look('b; wait 120; b; wait 120; b; wait 120')
    raise SystemExit('[tomatch] gave up')


if __name__ == '__main__':
    main()

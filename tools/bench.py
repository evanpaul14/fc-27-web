#!/usr/bin/env python3
"""Benchmark the match with timed `blits` segments.

  python3 tools/bench.py <outdir> <initial blits 0|1> <plan>     e.g. build/b1 0 0:150,1:150,0:120,1:120

plan: comma-separated <commands>:<seconds>, run after the match has loaded. Boots the game hidden, drives it to the
match with tools/tomatch.py, waits LOAD (env, default 120) s, then runs the plan; <commands> are control commands
separated by ';' (a bare number n means `blits n`), e.g. 'blits 1;prof 0:120'; each segment ends with a screenshot
(<outdir>/seg<i>.png, and seg<i>a.png at its start). Writes <outdir>/game.log (lines prefixed with seconds since start), marks.log, swap.log and
cores.log: every 2 s the game's CPU seconds on P and E cores, retired instructions and clocks (proc_pid_rusage v6, no
root). Compare fps only at the same P clock: the P cores throttle to ~0.83 GHz under thermal pressure; compare runs by
cost per frame instead (tools/benchsum.py: instructions, CPU and GPU time per frame, which don't depend on the clock). SAMPLE=1 also runs
/usr/bin/sample for 10 s near the end of each segment (<outdir>/seg<i>.sample). VISIBLE=1 shows the window.
"""
import ctypes
import os
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out, init, plan = os.path.abspath(sys.argv[1]), sys.argv[2], sys.argv[3]
LOAD = int(os.environ.get('LOAD', '120'))
os.makedirs(out, exist_ok=True)
fifo = os.path.join(out, 'ctl')
if os.path.exists(fifo):
    os.remove(fifo)
os.mkfifo(fifo)
env = dict(os.environ, FIFA_CONTROL=fifo, FIFA_ROMFS='extracted/update/romfs_program.json', FIFA_HIDDEN='1')
if os.environ.get('VISIBLE'):  # VISIBLE=1: show the window
    del env['FIFA_HIDDEN']
t0 = time.time()
game = subprocess.Popen(['./build/fifa'], cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
log = open(os.path.join(out, 'game.log'), 'w')
marks = open(os.path.join(out, 'marks.log'), 'w')
stop = False


def pump():
    for line in game.stdout:
        log.write('%.1f %s' % (time.time() - t0, line.decode('utf-8', 'replace')))
        log.flush()


def swap():
    with open(os.path.join(out, 'swap.log'), 'w') as f:
        while not stop:
            s = subprocess.run(['sysctl', '-n', 'vm.swapusage'], capture_output=True, text=True).stdout.strip()
            f.write('%.1f %s\n' % (time.time() - t0, s))
            f.flush()
            time.sleep(10)


# rusage_info_v6 fields (uint64 index after the 16-byte uuid)
RU = {'user': 0, 'system': 1, 'instructions': 29, 'cycles': 30, 'runnable': 34, 'user_p': 36, 'system_p': 37,
      'pinstr': 38, 'pcycles': 39}


def cores():
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    tb = (ctypes.c_uint32 * 2)()
    libc.mach_timebase_info(tb)
    ns = tb[0] / tb[1]
    buf = (ctypes.c_uint64 * 80)()
    prev = None
    with open(os.path.join(out, 'cores.log'), 'w') as f:
        while not stop:
            libc.proc_pid_rusage(game.pid, 6, buf)
            v = {k: buf[2 + i] for k, i in RU.items()}
            if prev:
                d = {k: v[k] - prev[k] for k in RU}
                cpu = (d['user'] + d['system']) * ns / 1e9
                pt = (d['user_p'] + d['system_p']) * ns / 1e9
                et = cpu - pt
                ecyc = d['cycles'] - d['pcycles']
                th = subprocess.run(['notifyutil', '-g', 'com.apple.system.thermalpressurelevel'], capture_output=True,
                                    text=True).stdout.split()[-1:] or ['?']
                f.write('%.1f cpu %.2f P %.2f E %.2f runnable %.2f  Ginst %.3f P %.3f  GHz all %.2f P %.2f E %.2f  IPC P %.2f E %.2f  thermal %s\n' % (
                    time.time() - t0, cpu, pt, et, d['runnable'] * ns / 1e9, d['instructions'] / 1e9, d['pinstr'] / 1e9,
                    d['cycles'] / cpu / 1e9 if cpu else 0, d['pcycles'] / pt / 1e9 if pt else 0,
                    ecyc / et / 1e9 if et > 0.01 else 0, d['pinstr'] / d['pcycles'] if d['pcycles'] else 0,
                    (d['instructions'] - d['pinstr']) / ecyc if ecyc > 0 else 0, th[0]))
                f.flush()
            prev = v
            time.sleep(2)


def mark(s):
    marks.write('%.1f %s\n' % (time.time() - t0, s))
    marks.flush()
    print('%.1f %s' % (time.time() - t0, s), flush=True)


def send(cmd):
    with open(fifo, 'w') as f:
        f.write(cmd + '\n')


for fn in (pump, swap, cores):
    threading.Thread(target=fn, daemon=True).start()
time.sleep(3)
send('blits %s' % init)
mark('start blits %s' % init)
r = subprocess.run(['python3', '-I', 'tools/tomatch.py', fifo, '--shot', os.path.join(out, 'm.png')], cwd=REPO,
                   capture_output=True, text=True)
open(os.path.join(out, 'tomatch.log'), 'w').write(r.stdout + r.stderr)
if r.returncode:
    mark('tomatch failed')
    game.kill()
    sys.exit(1)
mark('match loading')
time.sleep(LOAD)
for i, seg in enumerate(plan.split(',')):
    cmds, secs = seg.rsplit(':', 1)
    cmds = ['blits ' + c if c.isdigit() else c for c in cmds.split(';')]
    for c in cmds:
        send(c)
    mark('seg %d %s' % (i, '; '.join(cmds)))
    send('shot %s' % os.path.join(out, 'seg%da.png' % i))  # with the end shot: the match clock's progress
    if os.environ.get('SAMPLE') and int(secs) > 30:
        time.sleep(int(secs) - 15)
        subprocess.run(['/usr/bin/sample', str(game.pid), '10', '-file', os.path.join(out, 'seg%d.sample' % i)],
                       capture_output=True)
        time.sleep(5)
    else:
        time.sleep(int(secs))
    send('shot %s' % os.path.join(out, 'seg%d.png' % i))
time.sleep(3)  # (the last shot is written by the next present)
mark('end')
stop = True
game.kill()
game.wait()

#!/usr/bin/env python3
"""Drive the real TUI in a private tmux server and time one scan.

    python tool/tui_time.py PATH --src SRC --xdg SCRATCH [--live on|off]
                            [--workers N] [--size COLSxROWS] [--label NAME]
                            [--pyspy FILE]

`bench_scan.py` does not paint, and the difference is not a rounding error:
the same home directory it walked in 37 s took ~420 s in the explorer before
the live UI was paced. Every thread in the process shares one GIL, so a second
of drawing is a second the walk does not run, and the only honest way to see
that is to run the program.

So this starts a real `python -m disktide` under a *private* tmux server
(`tmux -L`, never the user's), types a path into the welcome screen, and
watches the pane. `--src` goes on `PYTHONPATH`, which is how a frozen copy of
another revision is measured against the working tree.

Prints one JSON line:

    scan_wall_s     first pane showing "Scanning" -> "Scanning" gone again
    enter_to_done_s from the Enter that started it, so the welcome screen's
                    own work is in it
    proc_cpu_s      utime+stime of the whole process
    threads         per-thread CPU, the maximum seen over the whole window,
                    so a worker that exits before the end is still counted
    threads_walking per-thread CPU *spent on the walk*: the last sample that
                    still saw "Scanning", minus the first one. Both
                    subtractions matter. /proc counts from process start, so
                    without the first the UI thread's number is mostly boot
                    and the welcome screen; and everything a finished scan
                    costs -- a tree reload, a full-depth chart, a category
                    index -- runs before the compositor repaints the title
                    that stops saying "Scanning", so without the second it
                    is in there too
    peak_rss_mb     VmHWM
    rescans         with `--rescans N`, one entry per extra scan driven from
                    the keyboard, each with its own wall time and the RSS
                    the process was holding when it finished -- the real-TUI
                    half of `tool/soak_memory.py`, and the only place the
                    cyclic garbage a live app makes while the collector is
                    paused can actually be seen

The thread keys are `main` and `python#<tid>`: CPython 3.12 does not set the
OS thread name, so every `/proc/<pid>/task/*/comm` reads `python` and the tid
is the only thing telling them apart. Sort by CPU and read them by size --
the walk's workers first, then the scheduler (which runs inside Textual's
`asyncio_0` worker thread), then the UI thread.

XDG_{CONFIG,DATA,CACHE,STATE}_HOME are redirected into `--xdg/<label>` and the
config written there, because the explorer saves config on some key presses
and a benchmark must not move the user's real one.

`--live on|off` writes `[ui] live_scan_render` into that config. `--pyspy
FILE` records a py-spy raw profile for the length of the scan; aggregate it
with `tool/spy_agg.py`.
"""
import argparse, json, os, pathlib, shlex, shutil, signal, subprocess, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("path")
ap.add_argument("--workers", type=int, default=1)
ap.add_argument("--live", choices=("on", "off"), default="off")
ap.add_argument("--size", default="307x69")
ap.add_argument("--label", default="run")
ap.add_argument("--src", required=True, help="frozen src dir for PYTHONPATH")
ap.add_argument("--xdg", required=True, help="scratch root for XDG homes")
ap.add_argument("--python", default=os.path.expanduser("~/.local/share/uv/tools/disktide/bin/python"))
ap.add_argument("--aspect", default="2.43")
ap.add_argument("--pyspy", default=None, help="record a py-spy raw profile to this file")
ap.add_argument("--timeout", type=float, default=1200)
ap.add_argument("--rescans", type=int, default=0,
                help="after the first scan, press r,r this many times and "
                     "report RSS after each -- the real-TUI memory soak")
a = ap.parse_args()

cols, rows = a.size.split("x")
xdg = pathlib.Path(a.xdg) / a.label
if xdg.exists():
    shutil.rmtree(xdg)
env = dict(os.environ)
keys = ["XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"]
for k in keys:
    d = xdg / k.lower()
    d.mkdir(parents=True)
    env[k] = str(d)
env["PYTHONPATH"] = a.src
env["DISKTIDE_CELL_ASPECT"] = a.aspect
env.pop("TMUX", None)
cp = subprocess.run(
    [a.python, "-c", "from disktide.config import config_path; print(config_path())"],
    env=env, capture_output=True, text=True, check=True,
).stdout.strip()
pathlib.Path(cp).parent.mkdir(parents=True, exist_ok=True)
pathlib.Path(cp).write_text(f'[ui]\nlive_scan_render = "{a.live}"\n')

sock = f"tt-{a.label}-{os.getpid()}"
def tmux(*args, check=False):
    return subprocess.run(["tmux", "-L", sock, *args], capture_output=True, text=True, check=check)
def pane():
    return tmux("capture-pane", "-p", "-t", "cap").stdout

tmux("kill-server")
tmux("new-session", "-d", "-s", "cap", "-x", cols, "-y", rows, "bash", check=True)
tmux("set", "-g", "window-size", "manual")
tmux("resize-window", "-t", "cap", "-x", cols, "-y", rows)
time.sleep(0.3)
envstr = " ".join(f"{k}={shlex.quote(env[k])}" for k in keys + ["PYTHONPATH", "DISKTIDE_CELL_ASPECT"])
tmux("send-keys", "-t", "cap", f"exec env {envstr} {a.python} -m disktide --workers {a.workers}", "Enter")

CLK = os.sysconf("SC_CLK_TCK")
def read_stat(p):
    try:
        s = pathlib.Path(p).read_text()
    except OSError:
        return None
    rp = s.rfind(")")
    comm = s[s.find("(") + 1:rp]
    f = s[rp + 2:].split()
    return comm, (int(f[11]) + int(f[12])) / CLK

def sample_threads(pid, acc):
    for t in pathlib.Path(f"/proc/{pid}/task").glob("*"):
        r = read_stat(t / "stat")
        if r is None:
            continue
        comm, cpu = r
        # Python 3.12 does not name OS threads: every comm is "python", so key by tid.
        key = "main" if int(t.name) == pid else f"{comm}#{t.name}"
        acc[key] = max(acc.get(key, 0.0), cpu)

deadline = time.monotonic() + a.timeout
while "Explore" not in pane():
    if time.monotonic() > deadline:
        sys.exit("welcome screen never appeared:\n" + pane())
    time.sleep(0.2)
pid = int(tmux("display", "-p", "-t", "cap", "#{pane_pid}").stdout.strip())
tmux("send-keys", "-t", "cap", "C-u")
tmux("send-keys", "-t", "cap", "-l", a.path)
t_enter = time.monotonic()
tmux("send-keys", "-t", "cap", "Enter")

threads = {}
walking = {}
# Per-thread CPU as of the moment the walk started. /proc counts from process
# start, so without this every number below carries the interpreter's boot,
# the imports and the welcome screen's first render at full terminal size --
# which on the UI thread is more than a whole scan of it costs.
at_start = {}
t_start = None
spy = None
quiet = 0
while True:
    txt = pane()
    now = time.monotonic()
    sample_threads(pid, threads)
    if "Scanning" in txt:
        # The last sample that still saw the walk. What a scan costs the UI
        # thread and what *finishing* one costs it are different questions
        # and differ by more than either: the explorer reloads the tree,
        # builds a full-depth chart and a category index when the scan ends,
        # and all of that runs before the compositor repaints the title that
        # stops saying "Scanning". Folding it into the scan's number hides
        # whatever is being measured about the scan itself.
        walking = {
            key: value - at_start.get(key, 0.0)
            for key, value in threads.items()
        }
        quiet = 0
        if t_start is None:
            t_start = now
            at_start = dict(threads)
            walking = {key: 0.0 for key in threads}
            if a.pyspy:
                spy = subprocess.Popen(
                    ["uvx", "py-spy@0.4.0", "record", "--pid", str(pid), "--format", "raw",
                     "--threads", "--nonblocking", "--rate", "200", "-o", a.pyspy, "--duration", "3600"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif "Explore" not in txt:
        quiet += 1
        if quiet >= 3:
            t_end = now
            break
    if now > deadline:
        sys.exit("scan never finished:\n" + txt)
    time.sleep(0.1)

sample_threads(pid, threads)


def _read_rss():
    for line in pathlib.Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS"):
            return int(line.split()[1]) // 1024
    return None


rss_after_first = _read_rss()


def memory_mb():
    """(current, peak) resident MiB. Peak only rises, current can fall."""
    current = peak = None
    for line in pathlib.Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmHWM"):
            peak = int(line.split()[1]) // 1024
        elif line.startswith("VmRSS"):
            current = int(line.split()[1]) // 1024
    return current, peak


def watch_one_scan(limit):
    """Wait for "Scanning" to appear in the pane and then to leave again."""
    started = None
    silent = 0
    while True:
        text = pane()
        moment = time.monotonic()
        if "Scanning" in text:
            silent = 0
            if started is None:
                started = moment
        elif started is not None and "Explore" not in text:
            silent += 1
            if silent >= 3:
                return started, moment
        if moment > limit:
            sys.exit("a rescan never finished:\n" + text)
        time.sleep(0.1)


# The real-TUI half of `tool/soak_memory.py`. Pausing the collector for a
# walk and freezing the finished tree are both bets about a process that
# keeps scanning; a headless loop cannot see what a live Textual app
# allocates while the collector is off, and this can.
rescans = []
for attempt in range(a.rescans):
    # `r` opens a confirm modal and `r` again answers it; a rescan is slow
    # enough that the app gates it behind a keystroke. Re-sent until the
    # scan actually starts: the screen reclaims the last scan's frames a
    # second after it finishes, which holds the GIL for the better part of a
    # second, and a keystroke that lands in there can be dropped.
    began = None
    for _ in range(6):
        tmux("send-keys", "-t", "cap", "r")
        time.sleep(0.6)
        tmux("send-keys", "-t", "cap", "r")
        started_by = time.monotonic() + 5
        while time.monotonic() < started_by:
            if "Scanning" in pane():
                began = True
                break
            time.sleep(0.1)
        if began:
            break
    if not began:
        sys.exit("a rescan never started:\n" + pane())
    began, ended = watch_one_scan(time.monotonic() + a.timeout)
    current, high = memory_mb()
    rescans.append({
        "rescan": attempt + 1,
        "scan_wall_s": round(ended - began, 2),
        "rss_mb": current,
        "peak_rss_mb": high,
    })
    # Long enough for the screen's post-completion recollect to have run and
    # finished before the next rescan is asked for, so the RSS above is the
    # settled number rather than a snapshot taken mid-reclaim.
    time.sleep(3.0)
    rescans[-1]["settled_rss_mb"] = memory_mb()[0]

proc = read_stat(f"/proc/{pid}/stat")
rss = memory_mb()[1]
if spy is not None:
    spy.send_signal(signal.SIGINT)
    try:
        spy.wait(timeout=60)
    except subprocess.TimeoutExpired:
        spy.kill()
final = pane()
tmux("send-keys", "-t", "cap", "q")
time.sleep(0.5)
tmux("kill-server")
print(json.dumps({
    "label": a.label, "path": a.path, "workers": a.workers, "live": a.live, "size": a.size,
    "scan_wall_s": round(t_end - t_start, 2) if t_start else None,
    "enter_to_done_s": round(t_end - t_enter, 2),
    "proc_cpu_s": round(proc[1], 2) if proc else None,
    "peak_rss_mb": rss,
    "threads": {k: round(v, 2) for k, v in sorted(threads.items(), key=lambda kv: -kv[1])},
    "threads_walking": {
        k: round(v, 2) for k, v in sorted(walking.items(), key=lambda kv: -kv[1])
    },
    "rss_after_first_scan_mb": rss_after_first,
    "rescans": rescans,
}))

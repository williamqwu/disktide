#!/usr/bin/env python3
"""Aggregate a `py-spy record --format raw --threads` file, per thread.

    python tool/spy_agg.py FILE [--top N] [--grep SUBSTR ...]

py-spy's own summaries fold every thread together, which is the one thing a
GIL-bound program must not do: what matters is which *thread* spent the time,
because a second on the UI thread and a second on a walk worker are the same
second of wall clock and only one of them is work.

Per thread it prints the sample count and share of the process, then the top
frames by inclusive time (the frame appears anywhere in the stack) and by
self time (the frame is the leaf). `--grep` adds a line per substring with
that frame's inclusive share of the thread and of the process, which is how
a before/after on one function is read.

Note where a cost lands. A `key=` argument to `sorted` is called from C, so
its time is charged to the *caller's* self time, not to a frame of its own.

Sample counts are only comparable at the same `--rate`; 100 Hz is what the
scanner numbers in `docs/contributing.md` use.
"""
import argparse, collections, re, sys

ap = argparse.ArgumentParser()
ap.add_argument("file")
ap.add_argument("--top", type=int, default=12)
ap.add_argument("--grep", nargs="*", default=[], help="frames to report inclusive share for")
a = ap.parse_args()

per_thread = collections.Counter()
incl = collections.defaultdict(collections.Counter)
self_ = collections.defaultdict(collections.Counter)
total = 0
def short(fr):
    # "func (path/to/file.py:123)" -> "func file.py"
    m = re.match(r"(.*?) \((.*?):(\d+)\)$", fr)
    return f"{m.group(1)} [{m.group(2).rsplit('/',1)[-1]}]" if m else fr
for line in open(a.file):
    line = line.rstrip("\n")
    if not line:
        continue
    stack, _, cnt = line.rpartition(" ")
    n = int(cnt)
    frames = stack.split(";")
    thread = "?"
    if frames and (frames[0].startswith("Thread") or frames[0].startswith("thread")):
        thread = frames[0]
        frames = frames[1:]
    total += n
    per_thread[thread] += n
    seen = set()
    for fr in frames:
        s = short(fr)
        if s not in seen:
            seen.add(s)
            incl[thread][s] += n
    if frames:
        self_[thread][short(frames[-1])] += n
print(f"total samples {total}")
for th, n in per_thread.most_common():
    print(f"\n== {th}: {n} samples ({n/total*100:.1f}%)")
    print("  -- inclusive --")
    for fr, c in incl[th].most_common(a.top):
        print(f"  {c/n*100:5.1f}%  {fr}")
    print("  -- self --")
    for fr, c in self_[th].most_common(a.top):
        print(f"  {c/n*100:5.1f}%  {fr}")
    for g in a.grep:
        c = sum(v for k, v in incl[th].items() if g in k)
        print(f"  grep {g!r}: {c/n*100:.1f}% of thread, {c/total*100:.1f}% of process")

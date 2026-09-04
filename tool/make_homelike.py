#!/usr/bin/env python3
"""Build a home-shaped tree on local disk, for benchmarking the scanner.

    python tool/make_homelike.py ROOT [DIRS] [SEED]

Defaults reproduce the fixture every scanner number in `docs/contributing.md`
is measured on:

    88,000 directories, 888,100 empty files (976,099 entries), max depth 10,
    ~122 MB of metadata, 11.5 s to build on local xfs.

Deterministic for a given seed, so two machines comparing numbers are
comparing the same tree. The shape is drawn from what a real cluster home
looks like rather than from a uniform fanout, because the scanner's cost is
per *directory* and a tree of the right size with the wrong shape measures a
different program:

    files per directory   0, 1, 2, 3, 5, 8, 12, 20, 40   (uniform draw)
    subdirs per directory 0, 1, 2, 3, 4, 5, 6            (uniform draw)
    max depth             10

Files are empty. The scanner stats every entry either way, and 890k files
with bytes in them is a fixture nobody can keep on a login node.

The frontier drain at the end is not optional. Directories are created from a
frontier that the loop pops at random, and the loop stops the moment the
directory target is reached -- which leaves roughly a quarter of the
directories never having been popped, and so with no files at all. Without the
drain the same invocation builds 646k files instead of 888,100, and every
per-entry number measured on it is 27 % light.
"""

import os, random, sys, time

root = sys.argv[1]
target_dirs = int(sys.argv[2]) if len(sys.argv) > 2 else 88000
seed = int(sys.argv[3]) if len(sys.argv) > 3 else 42
rng = random.Random(seed)
FILES = [0, 1, 2, 3, 5, 8, 12, 20, 40]
SUBS = [0, 1, 2, 3, 4, 5, 6]
MAXDEPTH = 10

os.makedirs(root, exist_ok=True)
dirs = [(root, 0)]
frontier = [(root, 0)]
ndirs, nfiles = 1, 0
t0 = time.monotonic()
O = os.O_CREAT | os.O_WRONLY
while ndirs < target_dirs:
    if frontier:
        parent, depth = frontier.pop(rng.randrange(len(frontier)))
    else:
        parent, depth = dirs[rng.randrange(len(dirs))]
    nf = rng.choice(FILES)
    for _ in range(nf):
        os.close(os.open(f"{parent}/f{nfiles}.dat", O, 0o644))
        nfiles += 1
    ns = rng.choice(SUBS) if depth < MAXDEPTH else 0
    for _ in range(ns):
        if ndirs >= target_dirs:
            break
        d = f"{parent}/d{ndirs}"
        os.mkdir(d)
        ndirs += 1
        dirs.append((d, depth + 1))
        frontier.append((d, depth + 1))
# Directories still on the frontier never got their files: give them their
# draw now (no subdirectories, the count is already at target).
while frontier:
    parent, depth = frontier.pop()
    for _ in range(rng.choice(FILES)):
        os.close(os.open(f"{parent}/f{nfiles}.dat", O, 0o644))
        nfiles += 1
print(f"{root}: {ndirs} dirs, {nfiles} files, {time.monotonic()-t0:.1f}s, "
      f"max depth {max(d for _, d in dirs)}")

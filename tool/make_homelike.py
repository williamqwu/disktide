#!/usr/bin/env python3
"""Build a home-shaped tree on local disk, for benchmarking the scanner.

    python tool/make_homelike.py TARGET [--dirs N] [--seed N] [--allow-home]

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

`TARGET` may not be under `$HOME` without `--allow-home`, and that guard is
not theoretical: `python tool/make_homelike.py --help` used to read `--help`
as the target -- there was no argument parsing at all, just `sys.argv[1]` --
and build 88,000 directories and 888,100 files into a directory called
`--help` in the working tree. On a quota'd NFS home that is 299,161 inodes
and an account over its file quota, with an `rm -rf` over NFS to get back.

The frontier drain at the end is not optional. Directories are created from a
frontier that the loop pops at random, and the loop stops the moment the
directory target is reached -- which leaves roughly a quarter of the
directories never having been popped, and so with no files at all. Without the
drain the same invocation builds 646k files instead of 888,100, and every
per-entry number measured on it is 27 % light.
"""

import argparse, os, random, sys, time
from pathlib import Path

FILES = [0, 1, 2, 3, 5, 8, 12, 20, 40]
SUBS = [0, 1, 2, 3, 4, 5, 6]
MAXDEPTH = 10


def _under_home(target: Path) -> bool:
    """Whether `target` is inside `$HOME`, as far as that is knowable."""
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):  # no HOME to compare against
        return False
    try:
        target.relative_to(home)
    except ValueError:
        return False
    return True


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="make_homelike.py",
        description=(
            "Build a home-shaped tree on local disk, for benchmarking the "
            "scanner. Deterministic for a given seed."
        ),
    )
    parser.add_argument("target", help="directory to build the tree in")
    parser.add_argument(
        "--dirs", type=int, default=88000,
        help="how many directories to create (default: %(default)s)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="random seed; the same seed builds the same tree "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--allow-home", action="store_true",
        help="permit a target under $HOME, which this otherwise refuses",
    )
    args = parser.parse_args(argv)
    if args.dirs < 1:
        parser.error("--dirs must be at least 1")
    target = Path(args.target).expanduser()
    resolved = target.resolve()
    if _under_home(resolved) and not args.allow_home:
        parser.error(
            f"{resolved} is under {Path.home()}. This writes up to a million "
            "entries and a network home is usually quota'd by inode as well "
            "as by size; point it at local disk (/tmp, scratch), or pass "
            "--allow-home if you are sure."
        )
    args.target = str(resolved)
    return args


args = parse_args()
root = args.target
target_dirs = args.dirs
rng = random.Random(args.seed)

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

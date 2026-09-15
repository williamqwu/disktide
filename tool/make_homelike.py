#!/usr/bin/env python3
"""Build a home-shaped tree on local disk, for benchmarking the scanner.

    python tool/make_homelike.py [TARGET] [--dirs N] [--seed N]
                                 [--allow-home] [--allow-network]

Defaults reproduce the fixture every scanner number in `docs/contributing.md`
is measured on:

    88,000 directories, 888,100 empty files (976,099 new entries), max depth 10,
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

With no `TARGET` the tree goes to the guarded scratch root -- see
`tool/scratchguard.py` -- named after the size and seed, so two shapes do not
land on each other. `TARGET` may not be under `$HOME` without `--allow-home`,
nor on a network filesystem without `--allow-network`, and that guard is not
theoretical: `python tool/make_homelike.py --help` used to read `--help` as
the target -- there was no argument parsing at all, just `sys.argv[1]` -- and
build 88,000 directories and 888,100 files into a directory called `--help`
in the working tree. On a quota'd NFS home that is 299,161 inodes and an
account over its file quota, with an `rm -rf` over NFS to get back. The third
refusal is inode headroom, which no flag lifts: the exact entry count is
planned before anything is created, so the guard is asked the real number
rather than an estimate, and being sure you want to write somewhere has never
made room there.

The frontier drain at the end is not optional. Directories are created from a
frontier that the loop pops at random, and the loop stops the moment the
directory target is reached -- which leaves roughly a quarter of the
directories never having been popped, and so with no files at all. Without the
drain the same invocation builds 646k files instead of 888,100, and every
per-entry number measured on it is 27 % light.
"""

import argparse, os, random, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratchguard  # noqa: E402 - needs tool/ on the path first

FILES = [0, 1, 2, 3, 5, 8, 12, 20, 40]
SUBS = [0, 1, 2, 3, 4, 5, 6]
MAXDEPTH = 10


def plan(root, target_dirs, seed):
    """Yield the operations that build the tree, creating nothing.

    Two op shapes, in the order they must be applied:

        ("d", path, depth)             make this directory
        ("f", parent, first, count)    make `count` empty files, `f<first>`..

    A generator rather than a list because the default fixture is 976,100
    entries and the caller wants to walk the sequence twice -- once to count,
    once to build -- without holding a million paths in memory. Both walks
    re-seed the same RNG, so both see the same tree.
    """
    rng = random.Random(seed)
    dirs = [(root, 0)]
    frontier = [(root, 0)]
    ndirs, nfiles = 1, 0
    while ndirs < target_dirs:
        if frontier:
            parent, depth = frontier.pop(rng.randrange(len(frontier)))
        else:
            parent, depth = dirs[rng.randrange(len(dirs))]
        count = rng.choice(FILES)
        yield ("f", parent, nfiles, count)
        nfiles += count
        subs = rng.choice(SUBS) if depth < MAXDEPTH else 0
        for _ in range(subs):
            if ndirs >= target_dirs:
                break
            child = f"{parent}/d{ndirs}"
            yield ("d", child, depth + 1)
            ndirs += 1
            dirs.append((child, depth + 1))
            frontier.append((child, depth + 1))
    # Directories still on the frontier never got their files: give them their
    # draw now (no subdirectories, the count is already at target).
    while frontier:
        parent, depth = frontier.pop()
        count = rng.choice(FILES)
        yield ("f", parent, nfiles, count)
        nfiles += count


def summarize(target_dirs, seed):
    """`(dirs, files, max depth)` the plan would build. Touches nothing.

    No root: the draws depend on the seed alone, so the counts are the same
    wherever the tree would land, and the caller needs them *before* it has
    a destination to name.
    """
    ndirs, nfiles, deepest = 1, 0, 0
    for op in plan("", target_dirs, seed):
        if op[0] == "d":
            ndirs += 1
            deepest = max(deepest, op[2])
        else:
            nfiles += op[3]
    return ndirs, nfiles, deepest


def build(root, target_dirs, seed):
    """Replay the plan against the filesystem. `(dirs, files, max depth)`."""
    os.makedirs(root, exist_ok=True)
    flags = os.O_CREAT | os.O_WRONLY
    ndirs, nfiles, deepest = 1, 0, 0
    for op in plan(root, target_dirs, seed):
        if op[0] == "d":
            os.mkdir(op[1])
            ndirs += 1
            deepest = max(deepest, op[2])
        else:
            _kind, parent, first, count = op
            for index in range(count):
                os.close(os.open(f"{parent}/f{first + index}.dat", flags, 0o644))
            nfiles += count
    return ndirs, nfiles, deepest


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="make_homelike.py",
        description=(
            "Build a home-shaped tree on local disk, for benchmarking the "
            "scanner. Deterministic for a given seed."
        ),
    )
    parser.add_argument(
        "target", nargs="?", default=None,
        help="directory to build the tree in (default: the guarded scratch "
             "root, named after --dirs and --seed)",
    )
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
    parser.add_argument(
        "--allow-network", action="store_true",
        help="permit a target on a network filesystem, which this "
             "otherwise refuses",
    )
    args = parser.parse_args(argv)
    if args.dirs < 1:
        parser.error("--dirs must be at least 1")
    return args


def main(argv=None):
    args = parse_args(argv)
    label = f"homelike-{args.dirs}-s{args.seed}"
    # The plan is pure RNG, so the exact entry count is knowable before the
    # first mkdir -- and the guard would rather be told than guess.
    planned_dirs, planned_files, _depth = summarize(args.dirs, args.seed)
    entries = planned_dirs + planned_files
    root = str(scratchguard.claim(
        args.target, entries=entries, label=label,
        allow_home=args.allow_home, allow_network=args.allow_network,
    ))

    started = time.monotonic()
    ndirs, nfiles, deepest = build(root, args.dirs, args.seed)
    assert (ndirs, nfiles) == (planned_dirs, planned_files), (
        f"plan said {planned_dirs} dirs / {planned_files} files, "
        f"built {ndirs} / {nfiles}"
    )
    print(f"{root}: {ndirs} dirs, {nfiles} files, "
          f"{time.monotonic() - started:.1f}s, max depth {deepest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

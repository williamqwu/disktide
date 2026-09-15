#!/usr/bin/env python3
"""Dump one scan's whole tree as sorted text, for byte-identity diffs.

A scanner change is only allowed to move the numbers it says it moves.
This prints every node a serial scan produced -- one line per node, sorted
by path -- so two builds can be diffed against each other with `diff` and
the answer is either "empty" or a list of exactly which nodes moved.

Usage:
    python tool/dump_tree.py PATH [-o OUT] [--workers N]

Serial by default (`ScanEngine(workers=1)`): the scheduler is
order-independent by construction, but a diff that has to explain worker
scheduling is not a diff anybody trusts.

The columns are the ones a scanner change can plausibly break -- sizes,
counts, and the four "why is this subtree not here" flags -- and
deliberately not mtime or inode, which move on their own between runs on a
live tree.

The first header line names the directory reader the dump was produced with
-- `--backend native|python|auto` picks it -- so two dumps say what made
them. It is the one line that legitimately differs between a native dump and
a fallback dump of the same tree, so an identity check compares the rest:

    diff <(tail -n +2 native.tsv) <(tail -n +2 python.tsv)
"""

from __future__ import annotations

import argparse
import os
import sys


COLUMNS = (
    "path",
    "is_dir",
    "size",
    "allocated_size",
    "unique_allocated_size",
    "file_count",
    "dir_count",
    "error",
    "vanished",
    "excluded",
    "depth_limited",
)


def format_node(node) -> str:
    return "\t".join(
        (
            node.path,
            str(int(node.is_dir)),
            str(node.size),
            str(node.allocated_size),
            str(node.unique_allocated_size),
            str(node.file_count),
            str(node.dir_count),
            "" if node.error is None else node.error,
            str(int(node.vanished)),
            str(int(node.excluded)),
            str(int(node.depth_limited)),
        )
    )


def dump(path: str, workers: int, stream) -> int:
    # Imported here, not at module scope: `--backend` sets DISKTIDE_ACCEL,
    # and `disktide.scanner.accel` reads it once, at import.
    from disktide.scanner.accel import ACCEL_BACKEND
    from disktide.scanner.engine import ScanEngine

    root = ScanEngine(workers=workers, scan_path=path).scan(path)
    lines = [format_node(node) for node in root.walk()]
    lines.sort()
    # Its own line, not a column: a row and the column header have to keep
    # the same field count for anything that reads this as a TSV.
    stream.write(f"#backend={ACCEL_BACKEND}\n")
    stream.write("#" + "\t".join(COLUMNS) + "\n")
    stream.write("\n".join(lines))
    stream.write("\n")
    return len(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="directory to scan")
    parser.add_argument("-o", "--out", default=None, help="output file")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--backend",
        choices=("auto", "native", "python"),
        default="auto",
        help="directory reader: the extension, the fallback, or whatever "
             "the environment already selects",
    )
    args = parser.parse_args(argv)

    if args.backend == "python":
        os.environ["DISKTIDE_ACCEL"] = "0"
    elif args.backend == "native":
        os.environ.pop("DISKTIDE_ACCEL", None)

    if args.out is None:
        count = dump(args.path, args.workers, sys.stdout)
    else:
        with open(args.out, "w") as handle:
            count = dump(args.path, args.workers, handle)
        print(f"dump_tree: {count:,} nodes -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

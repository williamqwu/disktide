#!/usr/bin/env python3
"""Randomized soak for the scan scheduler: invariants, snapshots, totals.

The suite's scheduler tests use fixed shapes so CI stays deterministic.
This is the counterpart that trades determinism for reach: it generates
adversarial trees with random seeds and checks three things the ordinary
assertions cannot see.

  * structure -- the scheduler's positional invariants on every state
    change (`tests/scheduler_invariants`), so a stale cursor or a
    mis-addressed parent index is caught where it happens rather than as a
    `KeyError` several directories later.
  * snapshots -- every published live frame is fingerprinted at publish
    time and re-checked at the end, so a copy-on-write miss that mutates an
    already-rendered frame is caught.
  * totals -- an independent single-threaded walker recomputes logical,
    allocated, unique, file, and directory totals and compares.

The generated trees deliberately favour the shapes that broke the
scheduler before: directories read ahead of files, file names that sort
before directory names, bounded source queues, symlink loops, dangling
links, unreadable directories, and mid-scan mutation.

Usage:
    python tool/soak_scan.py [--cases N] [--seed N] [--quick]

    --cases    how many randomized cases to run (default: 100)
    --seed     base seed, so a failure can be replayed exactly
    --quick    smaller trees; useful as a pre-push smoke check

Exit status is non-zero if anything failed, so this can be wired into a
nightly job. A failure prints the seed and the exact engine configuration
needed to reproduce it.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import stat
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratchguard  # noqa: E402 - needs tool/ on the path first

from disktide.scanner.accounting import finalize_unique_allocated  # noqa: E402
from disktide.scanner.engine import ScanEngine  # noqa: E402
from tests.scheduler_invariants import (  # noqa: E402
    fingerprint,
    install,
    validate_tree,
)


# Names chosen so sorted order fights read order: every FILE_NAME below
# sorts before at least one DIR_NAME.
DIR_NAMES = ["zdir", "archive_status", "summaries", "sub", "Zeta", "~tmp", "0dir"]
FILE_NAMES = ["000000010000000000000002", "0file", "1file", "a.txt", "!bang", "AAA"]


def entry_budget(quick: bool) -> int:
    """Worst-case entries one case creates, for the scratch guard.

    `_build` draws 0-4 subdirectories and 0-6 files per directory, so a case
    is bounded by the full 4-way tree at its depth cap plus six files and two
    symlinks per node. The wide shape multiplies that by up to 50 branches.
    Cases run one at a time and each is removed after, so this is the peak,
    not the total.
    """
    depth_cap = 3 if quick else 5
    branches = 20 if quick else 50

    def nodes(depth: int) -> int:
        return sum(4 ** level for level in range(depth + 1))

    per_node = 1 + 6 + 2  # the directory, its files, its two symlinks
    wide = branches * nodes(depth_cap - 1) * per_node
    deep = nodes(depth_cap) * per_node
    return max(wide, deep)


class _Monkeypatch:
    """Minimal setattr/undo shim so tests/ helpers work outside pytest."""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        for target, name, original in reversed(self._undo):
            setattr(target, name, original)
        self._undo.clear()


def _build(rng, root, depth, max_depth, features, shapes):
    directories = rng.randint(0, 4) if depth < max_depth else 0
    entries = [("d", f"{rng.choice(DIR_NAMES)}-{i}") for i in range(directories)]
    entries += [("f", f"{rng.choice(FILE_NAMES)}-{i}") for i in range(rng.randint(0, 6))]
    # Create directories first so readdir tends to hand them out before the
    # files that sort ahead of them.
    for kind, name in sorted(entries, key=lambda entry: entry[0]):
        path = os.path.join(root, name)
        if kind == "d":
            os.mkdir(path)
            _build(rng, path, depth + 1, max_depth, features, shapes)
            if "denied" in features and rng.random() < 0.10:
                os.chmod(path, 0)
                shapes["denied_dir"] += 1
        else:
            with open(path, "wb") as handle:
                handle.write(b"x" * rng.randint(0, 40))
    if "symlink" in features and rng.random() < 0.35:
        try:
            os.symlink(root, os.path.join(root, "loop-link"))
            os.symlink("/nonexistent/target", os.path.join(root, "dangling"))
            shapes["symlink"] += 2
        except OSError:
            pass


def _unlock(root):
    for base, directories, _ in os.walk(root, topdown=False):
        for name in directories:
            try:
                os.chmod(os.path.join(base, name), stat.S_IRWXU)
            except OSError:
                pass


def _reference(path, depth=0, max_depth=None):
    """Independent single-threaded walker: never follows symlinks.

    Mirrors the engine's depth policy: a directory at ``max_depth`` is still
    counted by its parent and still contributes its *own* blocks, but is not
    entered, so nothing inside it is counted. Same for a directory that
    cannot be listed.

    ``allocated`` is `st_blocks * 512` of every file, symlink and directory,
    this one included -- what `du` reports and what the engine now reports.
    The directory half is returned separately as ``dir_allocated`` so the
    unique reference can add it back after hardlink dedup: only leaves can
    be a second path to one inode, so directory blocks are never deduped.
    """
    own = own_allocated = 0
    size = allocated = files = directories = denied = 0
    sub_dir_allocated = 0
    leaves = []
    try:
        own_dir_allocated = os.lstat(path).st_blocks * 512
    except OSError:
        own_dir_allocated = 0
    if max_depth is not None and depth >= max_depth:
        return dict(size=0, allocated=own_dir_allocated, files=0, dirs=0,
                    denied=0, leaves=[], dir_allocated=own_dir_allocated)
    try:
        entries = list(os.scandir(path))
    except OSError:
        return dict(size=0, allocated=own_dir_allocated, files=0, dirs=0,
                    denied=1, leaves=[], dir_allocated=own_dir_allocated)
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                sub = _reference(entry.path, depth + 1, max_depth)
                size += sub["size"]
                allocated += sub["allocated"]
                sub_dir_allocated += sub["dir_allocated"]
                files += sub["files"]
                directories += 1 + sub["dirs"]
                denied += sub["denied"]
                leaves.extend(sub["leaves"])
            else:
                info = entry.stat(follow_symlinks=False)
                own += info.st_size
                own_allocated += info.st_blocks * 512
                files += 1
                leaves.append(
                    (entry.path, info.st_dev, info.st_ino, info.st_nlink,
                     info.st_blocks * 512)
                )
        except OSError:
            denied += 1
    return dict(size=size + own,
                allocated=allocated + own_allocated + own_dir_allocated,
                files=files, dirs=directories, denied=denied, leaves=leaves,
                dir_allocated=own_dir_allocated + sub_dir_allocated)


def _reference_unique(leaves):
    owners = set()
    total = 0
    for path, device, inode, links, allocated in sorted(leaves):
        if links > 1:
            if (device, inode) in owners:
                continue
            owners.add((device, inode))
        total += allocated
    return total


def _run_case(seed, *, quick, shapes, scratch):
    rng = random.Random(seed)
    features = set()
    if seed % 2 == 0:
        features.add("symlink")
    if seed % 5 == 0:
        features.add("denied")
    mutate = seed % 7 == 0
    cancel = seed % 11 == 0
    config = dict(
        workers=rng.choice([1, 2, 4, 8]),
        scheduler_queue_capacity=rng.choice([1, 2, 4, 16, None]),
        scheduler_submission_limit=rng.choice([1, 2, 8, None]),
        entry_chunk_size=rng.choice([1, 2, 4, 256]),
        max_depth=rng.choice([None, None, 2, 3]),
        one_file_system=rng.random() < 0.3,
    )
    root_dir = tempfile.mkdtemp(dir=scratch, prefix="case-")
    stop = threading.Event()
    patch = _Monkeypatch()
    try:
        depth_cap = 3 if quick else 5
        if seed % 3 == 0:
            shapes["wide"] += 1
            for index in range(rng.randint(8 if quick else 20, 20 if quick else 50)):
                branch = os.path.join(root_dir, f"w{index:03d}")
                os.mkdir(branch)
                _build(rng, branch, 1, depth_cap - 1, features, shapes)
        else:
            shapes["deep"] += 1
            _build(rng, root_dir, 0, depth_cap, features, shapes)

        install(patch)
        frames = []
        engine = ScanEngine(
            scan_path=root_dir,
            tree_update_callback=lambda update: frames.append(
                (update.root, fingerprint(update.root))
            ),
            tree_callback_interval=0.0,
            **config,
        )

        churn = None
        if mutate:
            shapes["mutated"] += 1

            def _churn():
                local = random.Random(seed)
                while not stop.is_set():
                    for base, directories, files in os.walk(root_dir):
                        if stop.is_set():
                            break
                        try:
                            if files and local.random() < 0.3:
                                os.remove(os.path.join(base, local.choice(files)))
                            if directories and local.random() < 0.15:
                                shutil.rmtree(
                                    os.path.join(base, local.choice(directories)),
                                    ignore_errors=True,
                                )
                        except OSError:
                            pass

            churn = threading.Thread(target=_churn, daemon=True)
            churn.start()

        if cancel:
            shapes["cancelled"] += 1
            threading.Timer(0.02, engine.cancel).start()

        root = engine.scan(root_dir)
        stop.set()
        if churn is not None:
            churn.join(timeout=2)

        problems = []
        for frame_root, published in frames:
            if fingerprint(frame_root) != published:
                problems.append(f"published frame mutated after publish: {frame_root.path}")
                break

        # A cancelled, mutated, or partially denied tree has no stable truth to
        # compare against; structure and frame checks still applied above.
        if not (mutate or cancel or "denied" in features):
            problems.extend(validate_tree(root))
        # `one_file_system` prunes at mount boundaries the reference walker
        # has no cheap way to model, so totals are only compared without it.
        if not (mutate or cancel or "denied" in features
                or config["one_file_system"]):
            finalize_unique_allocated(root)
            reference = _reference(root_dir, 0, config["max_depth"])
            for label, got, want in (
                ("size", root.size, reference["size"]),
                ("file_count", root.file_count, reference["files"]),
                ("dir_count", root.dir_count, reference["dirs"]),
                ("allocated", root.allocated_size, reference["allocated"]),
                ("unique", root.unique_allocated_size,
                 _reference_unique(reference["leaves"])
                 + reference["dir_allocated"]),
            ):
                if got != want:
                    problems.append(f"{label}: engine={got} reference={want}")
        return problems, config
    finally:
        stop.set()
        patch.undo()
        _unlock(root_dir)
        shutil.rmtree(root_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=100)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    shapes: Counter = Counter()
    failures = []
    with scratchguard.temporary_scratch(
        "soak-scan", entries=entry_budget(args.quick)
    ) as scratch:
        for offset in range(args.cases):
            seed = args.seed + offset
            try:
                problems, config = _run_case(
                    seed, quick=args.quick, shapes=shapes, scratch=scratch
                )
            except BaseException as exc:  # noqa: BLE001 - soak reports, never aborts
                failures.append((seed, None, [f"{type(exc).__name__}: {exc}"]))
                continue
            if problems:
                failures.append((seed, config, problems))
    print(f"cases:  {args.cases} (base seed {args.seed})")
    print(f"shapes: {dict(shapes)}")
    if not failures:
        print("result: no invariant, snapshot, or accounting problems")
        return 0
    print(f"result: {len(failures)} FAILING CASE(S)")
    for seed, config, problems in failures[:10]:
        print(f"\n  seed {seed}  config={config}")
        for problem in problems[:5]:
            print(f"    - {problem}")
        print(f"    reproduce: python tool/soak_scan.py --cases 1 --seed {seed}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

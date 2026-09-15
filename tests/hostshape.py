"""Force the host-dependent inputs into a shape other machines have.

Four times now the suite has been green on a developer's machine and red
on the GitHub runners, and each time the cause was the same *kind* of
thing: a test that reads something about the host rather than about
disktide.  The runner is not exotic -- it just has a different filesystem,
a different mount table, and less CPU than a workstation:

  ``readdir``   The order ``os.scandir`` hands back entries is the
                filesystem's, not a promise.  An NFS home returns creation
                order and ext4 returns hash order, so code that leans on
                the former is green here and red there.
  ``medium``    ``/sys/block/<dev>/queue/rotational`` is unreadable for
                the runner's ``/dev/root``, so every mount classifies
                ``unknown`` -- a row a developer whose disks are all
                network or flash never renders once.
  ``cores``     ``os.cpu_count()`` is an *input to product behaviour*,
                not just a performance knob: it picks the scan's worker
                count, and it used to decide the ``live_scan_render``
                auto-gate as well, so a two-core runner sent the explorer
                down a different code path than any developer machine.
                (That clause is gone -- the paint holds the GIL, so cores
                never hid it -- but the worker count remains.)  Note that
                neither ``taskset`` nor a container's ``--cpus``
                reproduces this: both leave ``os.cpu_count()`` reporting
                the host's count.
  ``sleepless`` A ``time.sleep`` that paces a test is a guess about how
                long the rest of the machine takes.  On a loaded two-core
                runner the coroutine it was pacing is parked far longer,
                which is the same thing as the sleep not happening.

Each shape is a deterministic caricature of one of those axes, so a test
that depends on the host fails *here*, by name, instead of on a push.

One thing happens whether or not a shape is named: the core count is
**pinned**.  ``os.cpu_count()`` is not a performance detail here, it is an
input to product behaviour, and leaving it to the machine is what let a
test pass on a 128-core node and fail on a 2-core runner.  Pinned, every
machine agrees; the ``cores`` shape is how you deliberately ask for the
other answer.

Enable the rest with ``DISKTIDE_HOST_SHAPE=readdir,medium,cores`` (or
``all``); unset, nothing but the core pin applies.
``DISKTIDE_HOST_SHAPE_SEED`` reseeds the readdir permutation -- it is fixed
by default so a failure reproduces from the log line alone.

``sleepless`` is a **diagnostic, not a gate**: it is the fastest way to
find a test whose window is built out of a sleep, but it also trips the
handful of tests that use a sleep as an instrument rather than as pacing
(measuring elapsed time, or standing in for a slow scan).  Run it by hand
when hunting a timing flake; do not put it in CI.
"""

from __future__ import annotations

import os
import random
import time
from typing import Iterator

import pytest

ENV_VAR = "DISKTIDE_HOST_SHAPE"
SEED_ENV_VAR = "DISKTIDE_HOST_SHAPE_SEED"

SHAPES = ("readdir", "medium", "cores", "sleepless")


def active_shapes() -> tuple[str, ...]:
    """The shapes named in the environment, validated."""
    raw = (os.environ.get(ENV_VAR) or "").strip()
    if not raw:
        return ()
    if raw == "all":
        return SHAPES
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    unknown = sorted(set(names) - set(SHAPES))
    if unknown:
        raise pytest.UsageError(
            f"{ENV_VAR}={raw!r} names unknown host shape(s) {unknown}; "
            f"choose from {list(SHAPES)} or 'all'"
        )
    return names


def _seed() -> int:
    return int(os.environ.get(SEED_ENV_VAR, "20260831"))


# --- readdir order --------------------------------------------------------


class _ShuffledScandir:
    """``os.scandir``'s full contract over a reordered entry list.

    The real return value is an iterator that is *also* a context manager
    with a ``close()``; ``scan_directory_once`` closes it in a ``finally``,
    so a bare ``iter(list)`` breaks every scanner test with
    ``AttributeError`` and reads like a genuine regression.
    """

    def __init__(self, entries: list, inner) -> None:
        self._entries = iter(entries)
        self._inner = inner

    def __iter__(self) -> Iterator:
        return self

    def __next__(self):
        return next(self._entries)

    def __enter__(self) -> "_ShuffledScandir":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._inner.close()


def _install_readdir(monkeypatch: pytest.MonkeyPatch) -> None:
    real_scandir = os.scandir
    rng = random.Random(_seed())

    def scandir(path=None):
        inner = real_scandir(path) if path is not None else real_scandir()
        try:
            entries = list(inner)
        except Exception:
            inner.close()
            raise

        def is_dir(entry) -> bool:
            # An entry can vanish between the readdir and the stat this
            # may need; ordering is arbitrary by definition here, so a
            # missing entry just sorts with the files rather than turning
            # the shape into an unrelated OSError.
            try:
                return entry.is_dir(follow_symlinks=False)
            except OSError:
                return False

        # Directories last and otherwise shuffled: the inverse of the
        # "directories first, creation order" an NFS home tends to give.
        rng.shuffle(entries)
        entries.sort(key=is_dir)
        return _ShuffledScandir(entries, inner)

    monkeypatch.setattr(os, "scandir", scandir)

    # The scheduler reads a directory through `scheduler.scan_dir`, which is
    # a C extension wherever one was built -- and a `readdir` in C does not
    # pass through `os.scandir` at all. Patching only the Python one left
    # the shape modelling nothing on exactly the configuration a release
    # wheel ships, so the same reordering is applied to the tuples too.
    from disktide.scanner import scheduler
    from disktide.scanner.accel import DT_DIR

    entry_rng = random.Random(_seed())
    real_scan_dir = scheduler.scan_dir

    def scan_dir(fd, stat_dirs=False):
        entries = real_scan_dir(fd, stat_dirs)
        entry_rng.shuffle(entries)
        entries.sort(key=lambda row: row[1] == DT_DIR)
        return entries

    monkeypatch.setattr(scheduler, "scan_dir", scan_dir)


# --- storage medium -------------------------------------------------------


def _install_medium(monkeypatch: pytest.MonkeyPatch) -> None:
    from disktide.collectors.platform import base, linux
    from disktide.collectors.platform.models import ProbeResult

    def storage_medium(self, path: str):
        return ProbeResult.degraded(
            None,
            "host shape 'medium': the rotational bit is unreadable here",
            "Every mount classifies as an unknown medium.",
        )

    # The concrete class wins: patching only the base silently does nothing.
    monkeypatch.setattr(base.PlatformAdapter, "storage_medium", storage_medium)
    monkeypatch.setattr(
        linux.LinuxPlatformAdapter, "storage_medium", storage_medium
    )


# --- core count -----------------------------------------------------------

#: What GitHub's standard runners give a private repository.
RUNNER_CPUS = 2

#: What every test sees unless the ``cores`` shape says otherwise: an
#: unremarkable developer machine, and the same one on every host.
DEFAULT_CPUS = 8


def pin_cpus(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    """Report `count` cores to everything that asks, consistently.

    Both halves have to move together: `detect_cpu_count` reads the total
    from `os.cpu_count` and the available from `os.sched_getaffinity`, and
    it is an invariant of that pair -- asserted in `test_sysinfo` -- that
    the second is not larger than the first.
    """
    monkeypatch.setattr(os, "cpu_count", lambda: count)
    monkeypatch.setattr(
        os,
        "sched_getaffinity",
        lambda _pid: frozenset(range(count)),
        raising=False,
    )


# --- timing ---------------------------------------------------------------


def _install_sleepless(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse every pacing sleep to nothing.

    A test that guards a mid-flight state with ``time.sleep`` is asserting
    that the rest of the machine is slower than the sleep.  Removing the
    sleep is what a loaded runner does to it, and it is deterministic.
    """
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


_INSTALLERS = {
    "readdir": _install_readdir,
    "medium": _install_medium,
    "sleepless": _install_sleepless,
}


def install(monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """Pin the host inputs for one test. Returns the shapes applied."""
    shapes = active_shapes()
    # Unconditional: host-independence is the default, not an opt-in.
    pin_cpus(monkeypatch, RUNNER_CPUS if "cores" in shapes else DEFAULT_CPUS)
    for name in shapes:
        installer = _INSTALLERS.get(name)
        if installer is not None:
            installer(monkeypatch)
    return shapes

#!/usr/bin/env python3
"""One gate every bulk file generator in `tool/` asks before it writes.

    python tool/scratchguard.py [PATH] --entries N [--allow-home] [--allow-network]

Prints `ok <path>` and exits 0, or the refusal on stderr and exits 2. So a
human -- or an agent about to run a generator -- can ask first.

Why this exists: `python tool/make_homelike.py --help` once read `--help` as
its target and built 88,000 directories and 888,100 files into a directory of
that name, inside a git worktree on a quota'd NFS home, and took the account
to 100 % of its inode limit. Argument parsing fixed that one script.
The shape of the mistake is not one script's, though: every generator here
picks its own destination, several of them through `tempfile`, and `tempfile`
follows `TMPDIR` -- which on an HPC account is routinely `~/tmp`, i.e. the
network home the guard exists to protect.

Three refusals, in this order, each naming the path, the reason, and the flag
or environment variable that lifts it:

  home     -- the resolved path is under `$HOME`. Lifted by `--allow-home`.
  network  -- the filesystem under it is one of NETWORK_FSTYPES, decided by
              longest-prefix match against `/proc/mounts`. Lifted by
              `--allow-network`, and by `$DISKTIDE_SCRATCH` naming a root
              the path is under: a site's scratch is a parallel filesystem
              by design and is where large fixtures belong, so refusing the
              one directory provided for the job only taught people to pass
              the flag every time. Skipped where `/proc/mounts` is absent.
  headroom -- there is not room for the entries the caller says it will
              create. **No flag lifts this one**; being sure you want to
              write to your home does not create inodes.

Headroom asks two sources and refuses if either one, when it answers, is
short. `os.statvfs` is the obvious one and is not sufficient: on the quota'd
NFS home this was written for it reports orders of magnitude more free inodes while the
account has far fewer left. Only `quota` knows about the quota, so `quota` is
asked as well -- and never fails closed, because CI runners do not have it.

The default destination, when a caller passes no path, is
`$DISKTIDE_SCRATCH` (or `tempfile.gettempdir()`) `/disktide-<user>/<label>`,
and it goes through the same three checks as an explicit one: a `TMPDIR`
under `~` is refused exactly like a target typed out by hand. Setting
`$DISKTIDE_SCRATCH` buys exactly one thing -- the network refusal, and only
for paths under that root. A scratch root under `$HOME` is still refused
without `--allow-home`, and no amount of configuration creates inodes.

Standard library only, and importable from a script in `tool/` because a
script's own directory is `sys.path[0]`. Tests load it by file path.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

#: Overridden by tests. Read fresh on every call, never cached: a caller that
#: monkeypatches this expects the next question to use it.
PROC_MOUNTS = "/proc/mounts"

#: Names a directory to use instead of `tempfile.gettempdir()` as the root of
#: the default scratch path.
SCRATCH_ENV = "DISKTIDE_SCRATCH"

#: Names a file whose contents stand in for `quota`'s output, so the parser
#: can be tested against the real format on a host with no quota binary.
QUOTA_FIXTURE_ENV = "DISKTIDE_SCRATCHGUARD_QUOTA"

#: `quota` on a hung NFS mount can block forever; a scratch check is not
#: worth waiting on.
QUOTA_TIMEOUT_SECONDS = 5.0

#: Filesystems where a million small files is somebody else's problem: slow
#: to create, slow to delete, usually quota'd by inode, and shared.
NETWORK_FSTYPES = frozenset({
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "gpfs", "lustre", "ceph",
    "fuse.sshfs", "fuse.glusterfs", "glusterfs", "afs", "9p", "beegfs",
    "panfs",
})

#: Slack on top of what the caller says it needs, so a fixture that finishes
#: does not leave the filesystem with nothing left for anyone else.
MINIMUM_MARGIN = 1000


class ScratchRefused(SystemExit):
    """A refusal to create bulk entries at a path.

    A `SystemExit` with status 2, so an unhandled one ends a script the way
    `argparse` does. The message is written to stderr on construction rather
    than by the interpreter's exit handling, because `SystemExit(2)` prints
    nothing and a script that catches this to clean up would otherwise
    swallow the only explanation the user gets. `.message` carries the text
    for anyone who wants to re-render it.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        sys.stderr.write(f"scratchguard: {message}\n")
        super().__init__(2)


# --- filesystem facts ------------------------------------------------------


def _unescape(field: str) -> str:
    """Undo the octal escapes `/proc/mounts` uses for space, tab, newline."""
    if "\\" not in field:
        return field
    for escape, char in (("\\040", " "), ("\\011", "\t"),
                         ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(escape, char)
    return field


def mount_rows() -> list[tuple[str, str, str]]:
    """`(device, mount point, fstype)` for every row of `/proc/mounts`.

    Empty where the file is absent (macOS, Windows), which is how the
    network check is skipped rather than guessed at.
    """
    try:
        text = Path(PROC_MOUNTS).read_text()
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        rows.append((_unescape(fields[0]), _unescape(fields[1]), fields[2]))
    return rows


def _within(path: Path, ancestor: Path) -> bool:
    """Whether `path` is `ancestor` or below it."""
    try:
        Path(path).relative_to(ancestor)
    except ValueError:
        return False
    return True


def mount_for(path, rows: list[tuple[str, str, str]] | None = None):
    """The `/proc/mounts` row whose mount point is the longest prefix of `path`.

    Longest prefix, not first match: `/users` is `autofs` here and
    `/users/PRJ0042` is the `nfs4` under it, and only the second one is the
    truth about a path inside a home. Ties go to the *later* row, because a
    second mount over the same point shadows the first -- which is what
    `/tmp` looks like on this host (`vg0-lv_tmp`, then `vg0-lv_tmp_private`).
    """
    if rows is None:
        rows = mount_rows()
    best = None
    for row in rows:
        mount = row[1]
        if not _within(path, Path(mount)):
            continue
        if best is None or len(mount) >= len(best[1]):
            best = row
    return best


def filesystem_type(path) -> tuple[str, str] | None:
    """`(mount point, fstype)` for `path`, or None when unknowable."""
    row = mount_for(Path(path))
    return (row[1], row[2]) if row is not None else None


def is_network_path(path) -> tuple[str, str] | None:
    """`(mount point, fstype)` when `path` sits on a network filesystem.

    A statement about the filesystem, not a verdict: `network_refusal` is
    the one that decides whether being here is a reason to stop.
    """
    found = filesystem_type(path)
    if found is not None and found[1] in NETWORK_FSTYPES:
        return found
    return None


def _resolve(path) -> Path:
    """`path` with `~` and symlinks resolved, or the closest thing to it."""
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(path)


def trusted_root() -> Path | None:
    """The resolved `$DISKTIDE_SCRATCH`, or None when it names nothing.

    Resolved, because the comparison in `network_refusal` is against a
    resolved target and `DISKTIDE_SCRATCH=~/../scratch` must mean the
    directory, not the spelling.
    """
    named = os.environ.get(SCRATCH_ENV)
    return _resolve(named) if named else None


def network_refusal(path, rows: list[tuple[str, str, str]] | None = None):
    """`(mount point, fstype)` when `path` is a network path to *refuse*.

    None where `is_network_path` would also answer None -- and, on top of
    that, wherever the path lies under `$DISKTIDE_SCRATCH`. Somebody set
    that variable on purpose, at a directory chosen for exactly this: on an
    HPC account the designated place for a large fixture is the site's
    scratch (`/fs/scratch/...` here, gpfs, no inode quota), which is a
    parallel filesystem and so matches NETWORK_FSTYPES. A guard that refuses
    the one directory provided for the job does not protect anything; it
    teaches people to type `--allow-network` on every call, which is the
    flag that would also have let a million files onto the home.

    Network only. The home refusal and the headroom check do not consult
    this, and a scratch root under `$HOME` is refused like any other path.
    """
    resolved = _resolve(path)
    root = trusted_root()
    if root is not None and _within(resolved, root):
        return None
    row = mount_for(resolved, rows)
    if row is not None and row[2] in NETWORK_FSTYPES:
        return (row[1], row[2])
    return None


def _nearest_existing(path: Path) -> Path | None:
    """The closest ancestor of `path` that exists, `path` itself included."""
    current = Path(path)
    while True:
        if current.exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


# --- headroom --------------------------------------------------------------


def required_entries(entries: int) -> int:
    """What a caller wanting `entries` must actually find room for."""
    return entries + max(MINIMUM_MARGIN, entries // 10)


def _quota_output() -> str | None:
    """`quota`'s report, or None for "no answer".

    Never fails closed. A missing binary, a timeout on a wedged mount, or a
    status this does not understand all mean the quota source abstains and
    `statvfs` decides alone; CI runners have no `quota` at all.
    """
    fixture = os.environ.get(QUOTA_FIXTURE_ENV)
    if fixture:
        try:
            return Path(fixture).read_text()
        except OSError:
            return None
    if shutil.which("quota") is None:
        return None
    try:
        done = subprocess.run(
            ["quota", "-w", "-u", "-p"],
            capture_output=True, text=True,
            timeout=QUOTA_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # `quota` exits 1 when a quota is *exceeded*, which is the case this
    # check most wants to hear about; only stranger statuses mean "no answer".
    if done.returncode not in (0, 1):
        return None
    return done.stdout


def parse_quota(text: str) -> list[tuple[str, int, int, int]]:
    """`(device, files, file quota, file limit)` per filesystem row.

    The wide format is `device` then blocks/bquota/blimit/[bgrace] then
    files/fquota/flimit/[fgrace]. The grace columns are *blank* when nothing
    is over quota, so neither left- nor right-anchored positional parsing is
    safe on the plain output -- which is why the caller runs `quota -p`, where
    every grace prints as a number and the row is exactly eight numeric
    fields. Six (no grace columns at all) is still accepted so an older
    `quota` parses too. Values are marked with a trailing `*` when over
    limit; that marker is stripped, not rejected.
    """
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 7:
            continue
        numbers: list[int] | None = []
        for field in fields[1:]:
            field = field.rstrip("*")
            if not field.isdigit():
                numbers = None
                break
            assert numbers is not None
            numbers.append(int(field))
        if numbers is None:
            continue
        if len(numbers) == 8:
            files, quota, limit = numbers[4], numbers[5], numbers[6]
        elif len(numbers) == 6:
            files, quota, limit = numbers[3], numbers[4], numbers[5]
        else:
            continue
        rows.append((fields[0], files, quota, limit))
    return rows


def quota_headroom(path: Path, rows: list[tuple[str, str, str]]):
    """`(inodes left, mount point)` from `quota`, or None for no answer.

    The report names a *device*, which is column one of `/proc/mounts` too,
    so the device maps to a mount point and the mount point decides whether
    it covers `path`. A limit of zero in both columns means unlimited, which
    is an abstention rather than a refusal.
    """
    if not rows:
        return None  # no mount table, so no way to map a device to a path
    text = _quota_output()
    if text is None:
        return None
    best = None
    for device, files, quota, limit in parse_quota(text):
        effective = limit or quota
        if effective <= 0:
            continue
        for row_device, mount, _fstype in rows:
            if row_device != device or not _within(path, Path(mount)):
                continue
            if best is None or len(mount) >= len(best[1]):
                best = (max(0, effective - files), mount)
    return best


def statvfs_headroom(path: Path):
    """`(inodes left, path asked)` from `statvfs`, or None for no answer.

    Zero free inodes is read as "this filesystem does not count inodes"
    (btrfs and several FUSE filesystems report exactly that), not as a full
    disk: refusing every write on a btrfs volume would be the guard making
    itself useless.
    """
    anchor = _nearest_existing(path)
    if anchor is None:
        return None
    try:
        stats = os.statvfs(anchor)
    except OSError:
        return None
    if stats.f_favail <= 0:
        return None
    return (int(stats.f_favail), str(anchor))


# --- the gate --------------------------------------------------------------


def _username() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry, no LOGNAME
        return str(os.getuid()) if hasattr(os, "getuid") else "unknown"


def default_root(label: str = "scratch") -> Path:
    """Where a caller that names no target writes: `<tmp>/disktide-<user>/<label>`."""
    root = os.environ.get(SCRATCH_ENV) or tempfile.gettempdir()
    return Path(root).expanduser() / f"disktide-{_username()}" / label


def claim(
    target=None,
    *,
    entries: int,
    allow_home: bool = False,
    allow_network: bool = False,
    label: str = "scratch",
) -> Path:
    """Approve `target` for `entries` new filesystem entries, and create it.

    `target` may be None, which means `default_root(label)`. Returns the
    resolved path, which exists on return. Raises `ScratchRefused` otherwise.
    """
    if entries < 0:
        raise ValueError(f"entries must not be negative, got {entries}")

    chosen = default_root(label) if target is None else Path(target)
    resolved = chosen.expanduser().resolve()
    source = (
        f"the default scratch root (${SCRATCH_ENV} or $TMPDIR)"
        if target is None else "this target"
    )

    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):  # no HOME to compare against
        home = None
    if home is not None and _within(resolved, home) and not allow_home:
        raise ScratchRefused(
            f"{resolved} is under $HOME ({home}), and {source} would create "
            f"up to {entries:,} entries there. A network home is usually "
            f"quota'd by inode as well as by size; point it at local disk "
            f"(/tmp, or ${SCRATCH_ENV}=/some/local/scratch), or pass "
            f"--allow-home if you are sure."
        )

    rows = mount_rows()
    network = network_refusal(resolved, rows)
    if network is not None and not allow_network:
        raise ScratchRefused(
            f"{resolved} is on {network[0]}, a {network[1]} filesystem, and "
            f"{source} would create up to {entries:,} entries there. Network "
            f"filesystems are slow to fill, slower to empty, and shared; "
            f"point it at local disk (/tmp), or point ${SCRATCH_ENV} at it "
            f"to trust it -- a site's scratch is parallel by design and is "
            f"where fixtures belong -- or pass --allow-network if you are "
            f"sure."
        )

    required = required_entries(entries)
    # Asked in order and lazily: `quota` is a subprocess with a five-second
    # timeout, and a path `statvfs` has already refused should not wait on it.
    for name, ask in (
        ("statvfs", lambda: statvfs_headroom(resolved)),
        ("quota", lambda: quota_headroom(resolved, rows)),
    ):
        answer = ask()
        if answer is None:
            continue
        available, where = answer
        if available < required:
            raise ScratchRefused(
                f"{resolved} needs room for {required:,} entries "
                f"({entries:,} plus a {required - entries:,} margin) but "
                f"{name} reports {available:,} left on {where}. No flag "
                f"lifts this: --allow-home and --allow-network say where to "
                f"write, not how many inodes exist. Delete something, ask "
                f"for fewer entries, or point ${SCRATCH_ENV} at a filesystem "
                f"with room."
            )

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def scratch_dir(
    label: str,
    *,
    entries: int,
    allow_home: bool = False,
    allow_network: bool = False,
) -> Path:
    """The approved, stable scratch directory named `label`.

    Stable so a fixture built once can be reused by name; see
    `temporary_scratch` for the throwaway kind.
    """
    return claim(
        None, entries=entries, allow_home=allow_home,
        allow_network=allow_network, label=label,
    )


@contextmanager
def temporary_scratch(
    label: str,
    *,
    entries: int,
    allow_home: bool = False,
    allow_network: bool = False,
) -> Iterator[Path]:
    """A unique directory under `scratch_dir(label)`, removed on exit.

    The replacement for `tempfile.mkdtemp()` in a generator: same shape, but
    the destination has been through the guard, and two concurrent runs get
    two directories.
    """
    root = scratch_dir(
        label, entries=entries, allow_home=allow_home,
        allow_network=allow_network,
    )
    path = Path(tempfile.mkdtemp(dir=root))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="scratchguard.py",
        description=(
            "Ask whether a path may take N new filesystem entries. Prints "
            "'ok <path>' and exits 0, or the refusal on stderr and exits 2."
        ),
    )
    parser.add_argument(
        "path", nargs="?", default=None,
        help="directory to check (default: the scratch root for --label)",
    )
    parser.add_argument(
        "--entries", type=int, required=True,
        help="how many files and directories the caller intends to create",
    )
    parser.add_argument(
        "--label", default="scratch",
        help="names the default scratch directory (default: %(default)s)",
    )
    parser.add_argument("--allow-home", action="store_true",
                        help="permit a path under $HOME")
    parser.add_argument(
        "--allow-network", action="store_true",
        help=(
            f"permit a path on a network filesystem (a path under "
            f"${SCRATCH_ENV} is trusted already)"
        ),
    )
    args = parser.parse_args(argv)
    if args.entries < 0:
        parser.error("--entries must not be negative")
    path = claim(
        args.path, entries=args.entries, allow_home=args.allow_home,
        allow_network=args.allow_network, label=args.label,
    )
    print(f"ok {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

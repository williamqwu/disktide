"""The scratch guard, and the lint that keeps every generator using it.

Three groups here.

*The guard itself* -- `tool/scratchguard.py`, loaded by file path because
`tool/` is not an importable package. Each test gets its own module object, so
one test's `PROC_MOUNTS` cannot decide the next one's answer. The mount tables
and quota reports have the shape of the real ones from the host this was
written for, with invented names and figures: a `/users autofs` row sitting
above a `/users/PRJ0042 nfs4` row, and the wide `quota -p` layout with its
eight numeric columns.

*The lint* -- an AST check that a `tool/*.py` which writes files inside a loop
imports the guard. The point is not this week's scripts; it is that the next
generator someone adds cannot quietly repeat the incident that started all
this.

*The suite's own temp root* -- `tests/conftest.py` refuses to run when
`tmp_path` would land on a network filesystem, and the message it prints is
worth pinning.

Nothing here creates a fixture under `$HOME`. `$HOME` on the machine this
guard exists for is a quota'd NFS home with an inode limit, which is the
whole reason there is a guard.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_DIR = REPO_ROOT / "tool"
GUARD_PATH = TOOL_DIR / "scratchguard.py"

#: `quota -w -u -p` laid out exactly as on the host the guard was written
#: for; the user, uid and figures are invented. `-p` prints every grace as a
#: number, so the row is the device plus exactly eight numeric fields and
#: positional parsing is finally safe.
QUOTA_WIDE = """Disk quotas for user alice (uid 51234):
     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace
{device} 157286400  1048576000 1048576000       0  {files}  2000000 2000000       0
"""

#: The same report from a `quota` that does not number its graces: the grace
#: columns are simply absent, leaving six numeric fields.
QUOTA_NARROW = """Disk quotas for user alice (uid 51234):
     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace
{device} 157286400  1048576000 1048576000  {files}  2000000 2000000
"""


@pytest.fixture
def guard():
    """A fresh `scratchguard` module, so monkeypatched state cannot leak."""
    spec = importlib.util.spec_from_file_location(
        "scratchguard_under_test", GUARD_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_real_quota(monkeypatch):
    """Keep the host's own quota out of every test that does not want it."""
    monkeypatch.delenv("DISKTIDE_SCRATCHGUARD_QUOTA", raising=False)
    monkeypatch.delenv("DISKTIDE_SCRATCH", raising=False)


def _mounts(tmp_path, rows) -> str:
    """Write a `/proc/mounts` table and return its path."""
    path = tmp_path / "proc-mounts"
    path.write_text(
        "".join(
            f"{device} {mount} {fstype} rw,relatime 0 0\n"
            for device, mount, fstype in rows
        )
    )
    return str(path)


class _Statvfs:
    def __init__(self, favail):
        self.f_favail = favail


def _pin_statvfs(monkeypatch, guard, under: Path, favail: int) -> None:
    """Make `statvfs` report `favail` inodes for anything under `under`."""
    real = os.statvfs

    def fake(path):
        if str(path).startswith(str(under)):
            return _Statvfs(favail)
        return real(path)

    monkeypatch.setattr(guard.os, "statvfs", fake)


# --- home ------------------------------------------------------------------


def test_refuses_a_target_under_home(guard, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = home / "fixture"

    with pytest.raises(guard.ScratchRefused) as excinfo:
        guard.claim(target, entries=10)

    assert excinfo.value.code == 2
    assert "is under $HOME" in excinfo.value.message
    assert "--allow-home" in excinfo.value.message
    assert not target.exists()


def test_allow_home_permits_a_target_under_home(guard, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = home / "fixture"

    assert guard.claim(target, entries=10, allow_home=True) == target
    assert target.is_dir()


def test_the_refusal_reaches_stderr(guard, tmp_path, monkeypatch, capsys):
    """`SystemExit(2)` prints nothing, and a caught refusal would be silent."""
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(guard.ScratchRefused):
        guard.claim(tmp_path / "fixture", entries=10)

    assert "scratchguard: " in capsys.readouterr().err


# --- network ---------------------------------------------------------------


def test_refuses_a_network_filesystem(guard, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(guard, "PROC_MOUNTS", _mounts(tmp_path, [
        ("/dev/root", "/", "xfs"),
        ("192.0.2.12:/PRJ0042", str(tmp_path), "nfs4"),
    ]))
    target = tmp_path / "fixture"

    with pytest.raises(guard.ScratchRefused) as excinfo:
        guard.claim(target, entries=10)

    assert "nfs4" in excinfo.value.message
    assert "--allow-network" in excinfo.value.message
    assert not target.exists()


def test_allow_network_permits_it(guard, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(guard, "PROC_MOUNTS", _mounts(tmp_path, [
        ("192.0.2.12:/PRJ0042", str(tmp_path), "nfs4"),
    ]))

    target = guard.claim(tmp_path / "fixture", entries=10, allow_network=True)
    assert target.is_dir()


def test_longest_prefix_wins_over_the_first_match(guard, tmp_path, monkeypatch):
    """`/users` is autofs; `/users/PRJ0042` is the nfs4 under it.

    Both are rows, and only the second one is the truth about a path inside a
    home. A first-match walk of `/proc/mounts` answers `autofs` and lets a
    million files onto the network home.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    users = tmp_path / "users"
    project = users / "PRJ0042"
    project.mkdir(parents=True)
    monkeypatch.setattr(guard, "PROC_MOUNTS", _mounts(tmp_path, [
        ("/etc/auto.users", str(users), "autofs"),
        ("192.0.2.12:/PRJ0042", str(project), "nfs4"),
    ]))

    assert guard.filesystem_type(project / "alice") == (str(project), "nfs4")
    with pytest.raises(guard.ScratchRefused) as excinfo:
        guard.claim(project / "alice" / "fixture", entries=10)
    assert "nfs4" in excinfo.value.message

    # A sibling still under the autofs row only is not a network path.
    assert guard.is_network_path(users / "other") is None
    assert guard.claim(users / "other", entries=10).is_dir()


def test_a_later_row_shadows_an_equal_length_mount(guard, tmp_path, monkeypatch):
    """`/tmp` is mounted twice on this host; the second mount is what you get."""
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(guard, "PROC_MOUNTS", _mounts(tmp_path, [
        ("/dev/mapper/vg0-lv_tmp", str(tmp_path), "xfs"),
        ("192.0.2.12:/PRJ0042", str(tmp_path), "nfs4"),
    ]))

    assert guard.filesystem_type(tmp_path / "x") == (str(tmp_path), "nfs4")


def test_a_missing_proc_mounts_skips_the_network_check(guard, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(guard, "PROC_MOUNTS", str(tmp_path / "nothing-here"))

    assert guard.mount_rows() == []
    assert guard.filesystem_type(tmp_path) is None
    assert guard.claim(tmp_path / "fixture", entries=10).is_dir()


# --- a configured scratch root ---------------------------------------------


def _gpfs_scratch(guard, tmp_path, monkeypatch) -> Path:
    """A gpfs mount with a scratch root on it, the shape of `/fs/scratch`."""
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(guard, "PROC_MOUNTS", _mounts(tmp_path, [
        ("/dev/root", "/", "xfs"),
        ("scratch", str(tmp_path / "fs"), "gpfs"),
    ]))
    return tmp_path / "fs" / "scratch" / "alice"


def test_disktide_scratch_is_trusted_for_the_network_check(
    guard, tmp_path, monkeypatch
):
    """`/fs/scratch` is gpfs, and is where large fixtures belong."""
    scratch = _gpfs_scratch(guard, tmp_path, monkeypatch)
    monkeypatch.setenv("DISKTIDE_SCRATCH", str(scratch))
    target = scratch / "homelike"

    assert guard.claim(target, entries=10) == target
    assert target.is_dir()
    # The report about the filesystem is unchanged; only the verdict is.
    assert guard.is_network_path(target) == (str(tmp_path / "fs"), "gpfs")
    assert guard.network_refusal(target) is None


def test_the_same_target_is_refused_when_the_variable_is_unset(
    guard, tmp_path, monkeypatch
):
    """Trust comes from the configuration, not from the fstype being gpfs."""
    scratch = _gpfs_scratch(guard, tmp_path, monkeypatch)
    target = scratch / "homelike"

    assert guard.network_refusal(target) == (str(tmp_path / "fs"), "gpfs")
    with pytest.raises(guard.ScratchRefused) as excinfo:
        guard.claim(target, entries=10)

    assert "gpfs" in excinfo.value.message
    assert f"${guard.SCRATCH_ENV} at it to trust it" in excinfo.value.message
    assert not target.exists()


def test_a_sibling_outside_the_trusted_root_is_still_refused(
    guard, tmp_path, monkeypatch
):
    """The trust is a subtree, not the mount the subtree happens to sit on."""
    scratch = _gpfs_scratch(guard, tmp_path, monkeypatch)
    monkeypatch.setenv("DISKTIDE_SCRATCH", str(scratch))
    target = tmp_path / "fs" / "scratch" / "somebody-else" / "homelike"

    with pytest.raises(guard.ScratchRefused) as excinfo:
        guard.claim(target, entries=10)

    assert "gpfs" in excinfo.value.message
    assert not target.exists()


def test_the_default_root_is_trusted_too(guard, tmp_path, monkeypatch):
    """`target is None` lands under the variable, so it qualifies by itself."""
    scratch = _gpfs_scratch(guard, tmp_path, monkeypatch)
    monkeypatch.setenv("DISKTIDE_SCRATCH", str(scratch))

    path = guard.scratch_dir("homelike-42", entries=10)

    assert path.is_dir()
    assert path.parent.parent == scratch


def test_a_trusted_root_under_home_is_still_refused(
    guard, tmp_path, monkeypatch
):
    """`DISKTIDE_SCRATCH=~/scratch` lifts the network check, and no more."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISKTIDE_SCRATCH", str(home / "scratch"))
    monkeypatch.setattr(guard, "PROC_MOUNTS", _mounts(tmp_path, [
        ("192.0.2.12:/PRJ0042", str(tmp_path), "nfs4"),
    ]))
    target = home / "scratch" / "homelike"

    with pytest.raises(guard.ScratchRefused) as excinfo:
        guard.claim(target, entries=10)

    assert "is under $HOME" in excinfo.value.message
    assert "--allow-home" in excinfo.value.message
    assert not target.exists()


def test_a_trusted_root_does_not_lift_headroom(guard, tmp_path, monkeypatch):
    """Naming a filesystem as yours does not create inodes on it."""
    scratch = _gpfs_scratch(guard, tmp_path, monkeypatch)
    monkeypatch.setenv("DISKTIDE_SCRATCH", str(scratch))
    _pin_statvfs(monkeypatch, guard, tmp_path, favail=500)
    target = scratch / "homelike"

    with pytest.raises(guard.ScratchRefused) as excinfo:
        guard.claim(target, entries=5_000)

    message = excinfo.value.message
    assert "statvfs" in message
    assert "No flag lifts this" in message
    assert not target.exists()


def test_the_trusted_root_is_resolved_before_it_is_compared(
    guard, tmp_path, monkeypatch
):
    """`~/...` and a relative path name a directory, not a spelling."""
    scratch = _gpfs_scratch(guard, tmp_path, monkeypatch)
    scratch.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(tmp_path / "fs"))
    monkeypatch.setenv("DISKTIDE_SCRATCH", "~/scratch/alice")
    assert guard.trusted_root() == scratch

    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    monkeypatch.chdir(scratch.parent)
    monkeypatch.setenv("DISKTIDE_SCRATCH", "alice")
    assert guard.trusted_root() == scratch

    target = scratch / "homelike"
    assert guard.claim(target, entries=10) == target


def test_an_empty_variable_names_no_trusted_root(guard, monkeypatch):
    """`DISKTIDE_SCRATCH=` is unset, not "trust the whole filesystem"."""
    monkeypatch.setenv("DISKTIDE_SCRATCH", "")

    assert guard.trusted_root() is None


# --- headroom --------------------------------------------------------------


def test_headroom_short_on_statvfs(guard, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    _pin_statvfs(monkeypatch, guard, tmp_path, favail=500)
    target = tmp_path / "fixture"

    with pytest.raises(guard.ScratchRefused) as excinfo:
        guard.claim(target, entries=5_000)

    message = excinfo.value.message
    assert "statvfs" in message
    assert "6,000" in message and "500" in message
    assert not target.exists()


def test_zero_free_inodes_is_read_as_no_answer(guard, tmp_path, monkeypatch):
    """btrfs and several FUSE filesystems report `f_favail == 0` always."""
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    _pin_statvfs(monkeypatch, guard, tmp_path, favail=0)

    assert guard.statvfs_headroom(tmp_path) is None
    assert guard.claim(tmp_path / "fixture", entries=5_000).is_dir()


def test_headroom_short_on_quota(guard, tmp_path, monkeypatch):
    """statvfs says there is room; only `quota` knows there is not.

    This is the real shape of the incident: `statvfs` on the NFS home
    reported orders of magnitude more free inodes than the account had left.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    report = tmp_path / "quota.txt"
    report.write_text(QUOTA_WIDE.format(device="10.0.0.1:/vol", files=1_999_990))
    monkeypatch.setenv("DISKTIDE_SCRATCHGUARD_QUOTA", str(report))
    monkeypatch.setattr(guard, "PROC_MOUNTS", _mounts(tmp_path, [
        ("10.0.0.1:/vol", str(tmp_path), "xfs"),
    ]))
    target = tmp_path / "fixture"

    with pytest.raises(guard.ScratchRefused) as excinfo:
        guard.claim(target, entries=5_000)

    message = excinfo.value.message
    assert "quota" in message
    assert "10 left" in message
    assert not target.exists()


def test_allow_home_does_not_lift_headroom(guard, tmp_path, monkeypatch):
    """Being sure you want to write to your home does not create inodes."""
    monkeypatch.setenv("HOME", str(tmp_path))
    report = tmp_path / "quota.txt"
    report.write_text(QUOTA_WIDE.format(device="10.0.0.1:/vol", files=1_999_990))
    monkeypatch.setenv("DISKTIDE_SCRATCHGUARD_QUOTA", str(report))
    monkeypatch.setattr(guard, "PROC_MOUNTS", _mounts(tmp_path, [
        ("10.0.0.1:/vol", str(tmp_path), "nfs4"),
    ]))
    target = tmp_path / "fixture"

    with pytest.raises(guard.ScratchRefused) as excinfo:
        guard.claim(target, entries=5_000, allow_home=True, allow_network=True)

    assert "No flag lifts this" in excinfo.value.message
    assert not target.exists()


def test_no_quota_binary_leaves_statvfs_to_decide(guard, tmp_path, monkeypatch):
    """CI runners have no `quota`; a missing tool must never fail closed."""
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(guard.shutil, "which", lambda name: None)

    assert guard._quota_output() is None
    assert guard.claim(tmp_path / "fixture", entries=10).is_dir()


@pytest.mark.parametrize("template", [QUOTA_WIDE, QUOTA_NARROW])
def test_both_grace_layouts_parse(guard, template):
    rows = guard.parse_quota(
        template.format(device="192.0.2.12:/PRJ0042", files=612_000)
    )

    assert rows == [("192.0.2.12:/PRJ0042", 612_000, 2_000_000, 2_000_000)]


def test_over_quota_markers_and_headers_do_not_confuse_the_parser(guard):
    text = (
        "Disk quotas for user alice (uid 51234):\n"
        "     Filesystem  blocks   quota   limit   grace   files   quota"
        "   limit   grace\n"
        "10.0.0.1:/vol 999999* 500 500 0 1999999* 2000000 2000000 0\n"
    )

    assert guard.parse_quota(text) == [
        ("10.0.0.1:/vol", 1_999_999, 2_000_000, 2_000_000)
    ]


def test_an_unlimited_quota_is_an_abstention(guard, tmp_path, monkeypatch):
    report = tmp_path / "quota.txt"
    report.write_text(
        "10.0.0.1:/vol 1 0 0 0 900000 0 0 0\n"
    )
    monkeypatch.setenv("DISKTIDE_SCRATCHGUARD_QUOTA", str(report))
    rows = [("10.0.0.1:/vol", str(tmp_path), "xfs")]

    assert guard.quota_headroom(tmp_path, rows) is None


def test_required_entries_carries_a_margin(guard):
    assert guard.required_entries(0) == 1_000
    assert guard.required_entries(5_000) == 6_000
    assert guard.required_entries(976_100) == 1_073_710


# --- the default destination -----------------------------------------------


def test_disktide_scratch_drives_the_default(guard, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DISKTIDE_SCRATCH", str(tmp_path / "scratch"))

    path = guard.scratch_dir("homelike-42", entries=10)

    assert path.is_dir()
    assert path.parent.parent == (tmp_path / "scratch")
    assert path.name == "homelike-42"


def test_tmpdir_drives_the_default_when_scratch_is_unset(guard, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    # `tempfile` resolves TMPDIR once per process and caches it; a generator
    # is a fresh process, this test is not.
    monkeypatch.setattr(guard.tempfile, "tempdir", None)

    assert guard.default_root("x").parent.parent == (tmp_path / "tmp")


def test_a_tmpdir_under_home_is_refused_like_any_other_target(
    guard, tmp_path, monkeypatch
):
    """The default is not a trusted path. On HPC accounts `TMPDIR=~/tmp`."""
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(home / "tmp"))
    monkeypatch.setattr(guard.tempfile, "tempdir", None)

    with pytest.raises(guard.ScratchRefused) as excinfo:
        guard.scratch_dir("fixture", entries=10)

    assert "is under $HOME" in excinfo.value.message
    assert "the default scratch root" in excinfo.value.message


def test_scratch_dir_returns_a_fresh_directory(guard, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DISKTIDE_SCRATCH", str(tmp_path / "scratch"))

    first = guard.scratch_dir("label", entries=10)
    assert first.is_dir()
    assert list(first.iterdir()) == []
    # Stable by name, so a fixture built once can be reused.
    assert guard.scratch_dir("label", entries=10) == first


def test_temporary_scratch_is_unique_and_removed(guard, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DISKTIDE_SCRATCH", str(tmp_path / "scratch"))

    with guard.temporary_scratch("soak", entries=10) as first:
        (first / "leftover").write_text("x")
        with guard.temporary_scratch("soak", entries=10) as second:
            assert first != second
            assert second.is_dir()
        assert not second.exists()
    assert not first.exists()


def test_negative_entries_is_a_programming_error(guard, tmp_path):
    with pytest.raises(ValueError):
        guard.claim(tmp_path / "x", entries=-1)


# --- the command line ------------------------------------------------------


def _guard_cli(args, env_overrides=None):
    env = dict(os.environ)
    env.pop("DISKTIDE_SCRATCH", None)
    env.pop("DISKTIDE_SCRATCHGUARD_QUOTA", None)
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, str(GUARD_PATH), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_cli_prints_ok_and_exits_zero(tmp_path):
    target = tmp_path / "fixture"
    result = _guard_cli([str(target), "--entries", "10"],
                        {"HOME": str(tmp_path / "home")})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ok {target}"


def test_cli_prints_the_refusal_and_exits_two(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = _guard_cli([str(home / "fixture"), "--entries", "10"],
                        {"HOME": str(home)})

    assert result.returncode == 2
    assert "is under $HOME" in result.stderr
    assert result.stdout == ""


# --- the lint --------------------------------------------------------------


#: `os.<name>` calls that create something.
WRITE_OS_FUNCTIONS = {"mkdir", "makedirs", "open", "symlink", "link"}

#: Method calls that create something, whatever the receiver is. Matched on
#: the attribute name alone: `pathlib` is not the only thing with `.mkdir`,
#: and a false positive costs one allowlist line while a false negative costs
#: an account its inodes.
WRITE_METHODS = {"mkdir", "touch", "write_text", "write_bytes"}

#: Modules that loop over writes without building a tree. The reason is
#: printed when the lint fails, so a future reader can judge it.
LOOPING_WRITES_ALLOWED = {
    "tui_time.py": (
        "four XDG directories and one config file for a single TUI run; "
        "the tree it times is built elsewhere, by make_homelike"
    ),
}


def _is_write_call(node: ast.Call) -> str | None:
    """The name of the bulk-write this call performs, or None."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if (isinstance(func.value, ast.Name) and func.value.id == "os"
                and func.attr in WRITE_OS_FUNCTIONS):
            return f"os.{func.attr}()"
        if func.attr in WRITE_METHODS:
            return f".{func.attr}()"
        return None
    if isinstance(func, ast.Name) and func.id == "open":
        mode = None
        if len(node.args) > 1:
            mode = node.args[1]
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode = keyword.value
        if mode is None:
            return None  # `open(path)` reads
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            if not set(mode.value) & set("wax+"):
                return None
        return "open()"
    return None


def looping_write_calls(source: str) -> list[tuple[int, str]]:
    """`(line, call)` for every bulk-write lexically inside a for/while."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            name = _is_write_call(inner)
            if name is not None:
                found.append((inner.lineno, name))
    return sorted(set(found))


def imports_scratchguard(source: str) -> bool:
    """Whether the module imports the guard, in any form."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[-1] == "scratchguard"
                   for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[-1] == "scratchguard":
                return True
            if any(alias.name == "scratchguard" for alias in node.names):
                return True
    return False


def home_rooted_default_constants(source: str) -> list[tuple[int, str]]:
    """Module-level `DEFAULT_*` constants built from the user's home.

    Textual, and deliberately narrow: it catches the shape that caused this
    -- `DEFAULT_TARGET = os.path.expanduser("~/tests/sysmonitor-cli")` in
    `gen_activity`, which was then handed to `argparse` as the default write
    target. It does not catch an `expanduser` inline in an `add_argument`
    call (`tui_time`'s `--python` is one, and it names an interpreter to
    read, not a directory to fill), nor a home path assembled at runtime.
    Those are the guard's job; this only keeps the *default* honest.
    """
    tree = ast.parse(source)
    found = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not any(name.startswith("DEFAULT_") for name in names):
            continue
        text = ast.get_source_segment(source, node) or ""
        if 'expanduser("~' in text or "expanduser('~" in text \
                or "Path.home()" in text:
            found.append((node.lineno, text.splitlines()[0]))
    return found


TOOL_MODULES = sorted(TOOL_DIR.glob("*.py"))


def test_the_lint_finds_the_tool_modules():
    """A glob that silently matched nothing would pass every check below."""
    assert len(TOOL_MODULES) > 15


@pytest.mark.parametrize("module", TOOL_MODULES, ids=lambda p: p.name)
def test_a_generator_that_loops_over_writes_imports_the_guard(module):
    """The rule that keeps the next new script from repeating the incident.

    "Fixtures on /tmp, never under ~" lived in prose, and prose is not
    enforceable. A module that creates files inside a loop is building
    something in bulk, and bulk creation goes through the guard.
    """
    source = module.read_text()
    calls = looping_write_calls(source)
    if not calls or imports_scratchguard(source):
        return
    excuse = LOOPING_WRITES_ALLOWED.get(module.name)
    detail = ", ".join(f"line {line}: {call}" for line, call in calls[:5])
    assert excuse is not None, (
        f"{module.name} creates files inside a loop ({detail}) but does not "
        f"import scratchguard. Route the destination through "
        f"scratchguard.claim/scratch_dir/temporary_scratch, or add "
        f"{module.name!r} to LOOPING_WRITES_ALLOWED with the reason it is "
        f"not building a tree."
    )


@pytest.mark.parametrize("module", TOOL_MODULES, ids=lambda p: p.name)
def test_no_tool_defaults_its_write_target_to_home(module):
    offenders = home_rooted_default_constants(module.read_text())
    assert not offenders, (
        f"{module.name} builds a DEFAULT_* constant from the user's home "
        f"({offenders}). A default write target under $HOME is how "
        f"gen_activity spent a thousand inodes on a network home every time "
        f"someone ran it with no argument; use scratchguard.scratch_dir()."
    )


def test_the_allowlist_names_only_real_modules():
    for name in LOOPING_WRITES_ALLOWED:
        assert (TOOL_DIR / name).exists(), f"stale allowlist entry: {name}"


def test_the_lint_catches_a_looping_write_without_the_import(tmp_path):
    guilty = tmp_path / "guilty.py"
    guilty.write_text(
        "import os\n"
        "for index in range(10):\n"
        "    os.mkdir(f'/tmp/d{index}')\n"
    )
    innocent = tmp_path / "innocent.py"
    innocent.write_text(
        "import os\n"
        "import scratchguard\n"
        "root = scratchguard.scratch_dir('x', entries=10)\n"
        "for index in range(10):\n"
        "    os.mkdir(root / f'd{index}')\n"
    )
    reader = tmp_path / "reader.py"
    reader.write_text(
        "for line in open('/etc/hostname'):\n"
        "    print(line)\n"
    )

    assert looping_write_calls(guilty.read_text()) == [(3, "os.mkdir()")]
    assert not imports_scratchguard(guilty.read_text())

    assert looping_write_calls(innocent.read_text()) == [(5, "os.mkdir()")]
    assert imports_scratchguard(innocent.read_text())

    # `open()` for reading is not bulk creation.
    assert looping_write_calls(reader.read_text()) == []


def test_the_lint_reads_the_mode_of_an_open_call():
    assert looping_write_calls(
        "for x in y:\n    open(x, 'wb')\n"
    ) == [(2, "open()")]
    assert looping_write_calls(
        "for x in y:\n    open(x, mode='a')\n"
    ) == [(2, "open()")]
    assert looping_write_calls("for x in y:\n    open(x, 'rb')\n") == []


def test_the_default_constant_check_catches_the_old_gen_activity():
    source = 'DEFAULT_TARGET = os.path.expanduser("~/tests/sysmonitor-cli")\n'
    assert home_rooted_default_constants(source) == [(1, source.strip())]
    assert home_rooted_default_constants(
        'DEFAULT_TARGET = "/tmp/sysmonitor-cli"\n'
    ) == []


# --- the suite's own temp root ---------------------------------------------


def test_conftest_refuses_a_network_temp_root(tmp_path, monkeypatch):
    """The check `pytest_configure` runs, without exercising `pytest.exit`."""
    from tests import conftest

    monkeypatch.delenv(conftest.ALLOW_NETWORK_TMP_ENV, raising=False)
    monkeypatch.setattr(
        conftest.scratchguard, "PROC_MOUNTS",
        _mounts(tmp_path, [("192.0.2.12:/PRJ0042", str(tmp_path), "nfs4")]),
    )

    message = conftest.network_tmp_refusal(tmp_path / "pytest-of-alice")

    assert message is not None
    assert "nfs4" in message
    assert "export TMPDIR=/tmp" in message
    assert conftest.ALLOW_NETWORK_TMP_ENV in message


def test_conftest_allows_a_local_temp_root(tmp_path, monkeypatch):
    from tests import conftest

    monkeypatch.delenv(conftest.ALLOW_NETWORK_TMP_ENV, raising=False)
    monkeypatch.setattr(
        conftest.scratchguard, "PROC_MOUNTS",
        _mounts(tmp_path, [("/dev/mapper/vg0-lv_tmp", str(tmp_path), "xfs")]),
    )

    assert conftest.network_tmp_refusal(tmp_path) is None


def test_conftest_trusts_a_temp_root_under_disktide_scratch(
    tmp_path, monkeypatch
):
    """`TMPDIR=/fs/scratch/...` is a choice, not the mistake the check hunts."""
    from tests import conftest

    monkeypatch.delenv(conftest.ALLOW_NETWORK_TMP_ENV, raising=False)
    monkeypatch.setenv("DISKTIDE_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(
        conftest.scratchguard, "PROC_MOUNTS",
        _mounts(tmp_path, [("scratch", str(tmp_path), "gpfs")]),
    )

    assert conftest.network_tmp_refusal(
        tmp_path / "scratch" / "pytest-of-alice"
    ) is None
    # A temp root the variable does not cover is refused exactly as before.
    outside = conftest.network_tmp_refusal(tmp_path / "pytest-of-alice")
    assert outside is not None and "gpfs" in outside


def test_conftest_honours_the_escape_hatch(tmp_path, monkeypatch):
    from tests import conftest

    monkeypatch.setenv(conftest.ALLOW_NETWORK_TMP_ENV, "1")
    monkeypatch.setattr(
        conftest.scratchguard, "PROC_MOUNTS",
        _mounts(tmp_path, [("192.0.2.12:/PRJ0042", str(tmp_path), "nfs4")]),
    )

    assert conftest.network_tmp_refusal(tmp_path) is None


def test_conftest_reads_basetemp_before_the_factory_exists():
    """`config._tmp_path_factory` does not exist at `pytest_configure`."""
    from tests import conftest

    class _Config:
        def __init__(self, basetemp):
            self._basetemp = basetemp

        def getoption(self, name, default=None):
            assert name == "basetemp"
            return self._basetemp

    assert conftest.temp_root(_Config("/tmp/explicit")) == Path("/tmp/explicit")
    import tempfile

    assert conftest.temp_root(_Config(None)) == Path(tempfile.gettempdir())

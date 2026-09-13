"""Low-level cleanup executors used only behind CleanupService safety gates."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import json
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

try:
    import fcntl
except ImportError:
    fcntl = None

from disktide import QUARANTINE_DIRECTORY_NAME
from disktide.domain.cleanup import (
    CleanupAction,
    CleanupActionKind,
    CleanupMutationToken,
    CleanupUndo,
    FileIdentity,
)


class CleanupExecutionError(RuntimeError):
    """Raised when an executor cannot preserve the cleanup safety contract."""


@dataclass(frozen=True, slots=True)
class CleanupMutationCapabilities:
    dir_fd_verification: bool
    recoverable_move: str
    permanent_file: str
    permanent_directory: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "dir_fd_verification": self.dir_fd_verification,
            "recoverable_move": self.recoverable_move,
            "permanent_file": self.permanent_file,
            "permanent_directory": self.permanent_directory,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class QuarantineLedgerStatus:
    root: str
    ledger_bytes: int
    ledger_items: int
    manifest_bytes: int
    manifest_items: int
    pending_items: int
    ledger_matches: bool
    rebuilt: bool = False
    recoveries: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "ledger_bytes": self.ledger_bytes,
            "ledger_items": self.ledger_items,
            "manifest_bytes": self.manifest_bytes,
            "manifest_items": self.manifest_items,
            "pending_items": self.pending_items,
            "ledger_matches": self.ledger_matches,
            "rebuilt": self.rebuilt,
            "recoveries": list(self.recoveries),
            "issues": list(self.issues),
        }


MutationHook = Callable[[CleanupMutationToken], None]
TransitionHook = Callable[[str], None]


def identity_from_path(path: str | Path) -> FileIdentity:
    return _identity_from_stat(os.lstat(path))


def _identity_from_stat(value: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        mode=int(value.st_mode),
        size=int(value.st_size),
        mtime_ns=int(value.st_mtime_ns),
        is_dir=stat.S_ISDIR(value.st_mode),
        is_symlink=stat.S_ISLNK(value.st_mode),
    )


def mutation_capabilities() -> CleanupMutationCapabilities:
    required = (os.open, os.rename, os.stat, os.unlink, os.rmdir)
    dir_fd = (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and all(function in os.supports_dir_fd for function in required)
    )
    if dir_fd:
        return CleanupMutationCapabilities(
            dir_fd_verification=True,
            recoverable_move="dir-fd verified rename with post-move identity check",
            permanent_file="dir-fd staged unlink",
            permanent_directory="quarantine first; purge only inside mode-0700 quarantine",
            reason="POSIX dir-fd primitives are available",
        )
    return CleanupMutationCapabilities(
        dir_fd_verification=False,
        recoverable_move="path-revalidated recoverable rename",
        permanent_file="blocked",
        permanent_directory="blocked",
        reason="required POSIX dir-fd primitives are unavailable",
    )


def create_mutation_token(
    path: str | Path,
    expected_identity: FileIdentity,
    *,
    ttl_seconds: float = 30.0,
) -> CleanupMutationToken:
    value = Path(path)
    if not value.name:
        raise CleanupExecutionError("mutation target must have a parent entry name")
    try:
        current = identity_from_path(value)
        parent = identity_from_path(value.parent)
    except OSError as exc:
        raise CleanupExecutionError(f"mutation token creation failed: {exc}") from exc
    if not expected_identity.matches(current):
        raise CleanupExecutionError(
            "target identity changed before mutation token creation"
        )
    if parent.is_symlink or not parent.is_dir:
        raise CleanupExecutionError("mutation target parent is not a real directory")
    now = datetime.now(timezone.utc)
    return CleanupMutationToken(
        path=str(value),
        parent_path=str(value.parent),
        entry_name=value.name,
        parent_identity=parent,
        target_identity=current,
        created_at=now,
        expires_at=now + timedelta(seconds=max(1.0, ttl_seconds)),
        mode=(
            "dir-fd"
            if mutation_capabilities().dir_fd_verification
            else "path-revalidated"
        ),
    )


class XDGTrashAdapter:
    """Freedesktop Trash implementation for same-filesystem atomic moves."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        mutation_hook: MutationHook | None = None,
    ):
        if root is None:
            data_home = Path(
                os.environ.get(
                    "XDG_DATA_HOME",
                    os.path.expanduser("~/.local/share"),
                )
            )
            root = data_home / "Trash"
        self.root = Path(root)
        self._mutation_hook = mutation_hook

    @property
    def files_dir(self) -> Path:
        return self.root / "files"

    @property
    def info_dir(self) -> Path:
        return self.root / "info"

    @property
    def supported(self) -> bool:
        return os.name == "posix"

    def can_move(self, path: str | Path) -> bool:
        if not self.supported:
            return False
        try:
            target_device = os.lstat(path).st_dev
            anchor = self.root
            while not anchor.exists() and anchor != anchor.parent:
                anchor = anchor.parent
            return anchor.stat().st_dev == target_device
        except OSError:
            return False

    def move(
        self,
        action: CleanupAction,
        token: CleanupMutationToken | None = None,
    ) -> CleanupUndo:
        source = Path(action.path)
        if not self.can_move(source):
            raise CleanupExecutionError(
                "system Trash is unavailable on the target filesystem"
            )
        self._ensure_root()
        destination = _unique_destination(
            self.files_dir,
            f"{action.id[:12]}-{source.name or 'item'}",
        )
        info_path = self.info_dir / f"{destination.name}.trashinfo"
        deletion_date = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S")
        info = (
            "[Trash Info]\n"
            f"Path={quote(str(source), safe='/')}\n"
            f"DeletionDate={deletion_date}\n"
        )
        bound_token = _token_for_action(action, token)
        _move_with_token(
            bound_token,
            destination.parent,
            destination.name,
            mutation_hook=self._mutation_hook,
        )
        try:
            info_path.write_text(info, encoding="utf-8")
            os.chmod(info_path, 0o600)
        except Exception as exc:
            try:
                _move_path(
                    destination,
                    source,
                    bound_token.target_identity,
                )
            finally:
                info_path.unlink(missing_ok=True)
            raise CleanupExecutionError(
                f"Trash metadata write failed: {exc}"
            ) from exc
        return CleanupUndo(
            strategy=CleanupActionKind.TRASH,
            original_path=str(source),
            isolated_path=str(destination),
            metadata_path=str(info_path),
        )

    def restore(
        self,
        undo: CleanupUndo,
        expected_identity: FileIdentity | None = None,
    ) -> None:
        _restore_isolated(undo, expected_identity)

    def _ensure_root(self) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.info_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (self.root, self.files_dir, self.info_dir):
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise CleanupExecutionError(f"unsafe Trash directory: {path}")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise CleanupExecutionError(
                    f"Trash directory is not owned by this user: {path}"
                )


class QuarantineExecutor:
    """Same-filesystem atomic isolation with a local recovery manifest."""

    def __init__(
        self,
        *,
        retention_days: int = 7,
        max_bytes: int = 10 * 1024**3,
        mutation_hook: MutationHook | None = None,
        transition_hook: TransitionHook | None = None,
    ):
        self.retention_days = max(1, retention_days)
        self.max_bytes = max(1, max_bytes)
        self._mutation_hook = mutation_hook
        self._transition_hook = transition_hook

    @staticmethod
    def root_for(path: str | Path) -> Path:
        return Path(path).parent / QUARANTINE_DIRECTORY_NAME

    def move(
        self,
        action: CleanupAction,
        token: CleanupMutationToken | None = None,
    ) -> CleanupUndo:
        source = Path(action.path)
        root = self.root_for(source)
        self._ensure_root(root)
        bound_token = _token_for_action(action, token)
        source_identity = bound_token.target_identity
        if source_identity.device != os.lstat(root).st_dev:
            raise CleanupExecutionError(
                "quarantine must be on the same filesystem as the target"
            )
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=self.retention_days
        )
        estimated_bytes = max(0, action.estimated_reclaimable_bytes)
        with _ledger_lock(root):
            ledger = self._load_ledger_for_move(root)
            destination = _unique_quarantine_destination(
                root,
                f"{action.id[:12]}-{source.name or 'item'}",
            )
            manifest_path = root / f"{destination.name}.manifest.json"
            manifest = {
                "version": 1,
                "action_id": action.id,
                "plan_id": action.plan_id,
                "original_path": str(source),
                "quarantine_path": str(destination),
                "estimated_reclaimable_bytes": action.estimated_reclaimable_bytes,
                "identity": {
                    "device": source_identity.device,
                    "inode": source_identity.inode,
                    "mode": source_identity.mode,
                    "size": source_identity.size,
                    "mtime_ns": source_identity.mtime_ns,
                    "is_dir": source_identity.is_dir,
                    "is_symlink": source_identity.is_symlink,
                },
                "created_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": expires_at.isoformat(),
                "state": "prepared",
            }
            pending = ledger["pending"]
            assert isinstance(pending, dict)
            reserved_bytes = sum(
                max(0, int(item.get("estimated_reclaimable_bytes", 0)))
                for item in pending.values()
                if isinstance(item, dict)
            )
            if (
                int(ledger["isolated_bytes"])
                + reserved_bytes
                + estimated_bytes
                > self.max_bytes
            ):
                raise CleanupExecutionError(
                    "quarantine capacity policy would be exceeded"
                )
            pending[action.id] = {
                "manifest_path": str(manifest_path),
                "quarantine_path": str(destination),
                "original_path": str(source),
                "estimated_reclaimable_bytes": estimated_bytes,
            }
            _write_ledger(root, ledger)
            self._transition("reservation-committed")
            try:
                _atomic_json_write(manifest_path, manifest)
            except Exception:
                pending.pop(action.id, None)
                _write_ledger(root, ledger)
                raise
            self._transition("manifest-prepared")
            try:
                _move_with_token(
                    bound_token,
                    destination.parent,
                    destination.name,
                    mutation_hook=self._mutation_hook,
                )
            except Exception as exc:
                manifest["state"] = "aborted"
                manifest["error"] = str(exc)
                _atomic_json_write(manifest_path, manifest)
                pending.pop(action.id, None)
                _write_ledger(root, ledger)
                if isinstance(exc, CleanupExecutionError):
                    raise
                raise CleanupExecutionError(
                    f"quarantine move failed: {exc}"
                ) from exc
            self._transition("renamed")
            manifest["state"] = "isolated"
            _atomic_json_write(manifest_path, manifest)
            self._transition("manifest-isolated")
            pending.pop(action.id, None)
            ledger["isolated_bytes"] = int(ledger["isolated_bytes"]) + estimated_bytes
            ledger["item_count"] = int(ledger["item_count"]) + 1
            _write_ledger(root, ledger)
            self._transition("ledger-committed")
        return CleanupUndo(
            strategy=CleanupActionKind.QUARANTINE,
            original_path=str(source),
            isolated_path=str(destination),
            metadata_path=str(manifest_path),
            expires_at=expires_at,
        )

    def restore(
        self,
        undo: CleanupUndo,
        expected_identity: FileIdentity | None = None,
    ) -> None:
        manifest_path = _required_manifest_path(undo)
        root = manifest_path.parent
        self._ensure_root(root)
        with _ledger_lock(root):
            ledger = self._load_ledger_for_move(root)
            manifest = _read_manifest(manifest_path)
            identity = expected_identity or _manifest_identity(manifest)
            original = Path(undo.original_path)
            if original.exists() or original.is_symlink():
                raise CleanupExecutionError(
                    f"restore refused because the original path exists: {original}"
                )
            manifest["state"] = "restoring"
            _atomic_json_write(manifest_path, manifest)
            try:
                _move_path(
                    Path(undo.isolated_path),
                    original,
                    identity,
                    mutation_hook=self._mutation_hook,
                )
            except Exception:
                manifest["state"] = "isolated"
                _atomic_json_write(manifest_path, manifest)
                raise
            manifest["state"] = "restored"
            manifest["restored_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_json_write(manifest_path, manifest)
            self._remove_from_ledger(ledger, manifest)
            _write_ledger(root, ledger)

    def purge(
        self,
        undo: CleanupUndo,
        expected_identity: FileIdentity | None = None,
    ) -> None:
        manifest_path = _required_manifest_path(undo)
        root = manifest_path.parent
        self._ensure_root(root)
        with _ledger_lock(root):
            ledger = self._load_ledger_for_move(root)
            manifest = _read_manifest(manifest_path)
            identity = expected_identity or _manifest_identity(manifest)
            token = create_mutation_token(undo.isolated_path, identity)
            manifest["state"] = "purging"
            _atomic_json_write(manifest_path, manifest)
            try:
                _delete_with_token(
                    token,
                    allow_directory=True,
                    mutation_hook=self._mutation_hook,
                )
            except Exception:
                manifest["state"] = "isolated"
                _atomic_json_write(manifest_path, manifest)
                raise
            manifest["state"] = "purged"
            manifest["purged_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_json_write(manifest_path, manifest)
            self._remove_from_ledger(ledger, manifest)
            _write_ledger(root, ledger)

    def audit(
        self,
        root: str | Path,
        *,
        rebuild: bool = False,
    ) -> QuarantineLedgerStatus:
        value = Path(root)
        if not value.exists():
            raise CleanupExecutionError(f"quarantine root does not exist: {value}")
        self._ensure_root(value)
        with _ledger_lock(value):
            return self._audit_locked(value, rebuild=rebuild)

    def _ensure_root(self, root: Path) -> None:
        root.mkdir(mode=0o700, exist_ok=True)
        info = os.lstat(root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CleanupExecutionError(f"unsafe quarantine root: {root}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise CleanupExecutionError(
                f"quarantine root is not owned by this user: {root}"
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise CleanupExecutionError(
                f"quarantine root permissions must be 0700: {root}"
            )

    def _load_ledger_for_move(self, root: Path) -> dict[str, object]:
        try:
            ledger = _read_ledger(root)
        except CleanupExecutionError:
            status = self._audit_locked(root, rebuild=True)
            _require_resolved_ledger(status)
            ledger = _read_ledger(root)
        if ledger is None:
            if next(root.glob("*.manifest.json"), None) is not None:
                status = self._audit_locked(root, rebuild=True)
                _require_resolved_ledger(status)
                ledger = _read_ledger(root)
            else:
                ledger = _empty_ledger()
                _write_ledger(root, ledger)
        pending = ledger.get("pending")
        if not isinstance(pending, dict):
            status = self._audit_locked(root, rebuild=True)
            _require_resolved_ledger(status)
            ledger = _read_ledger(root)
        elif pending:
            status = self._audit_locked(root, rebuild=True)
            _require_resolved_ledger(status)
            ledger = _read_ledger(root)
        if ledger is None:
            raise CleanupExecutionError("quarantine ledger recovery failed")
        return ledger

    def _audit_locked(
        self,
        root: Path,
        *,
        rebuild: bool,
    ) -> QuarantineLedgerStatus:
        recoveries: list[str] = []
        issues: list[str] = []
        manifest_bytes = 0
        manifest_items = 0
        for path in sorted(root.glob("*.manifest.json")):
            try:
                manifest = _read_manifest(path)
                identity = _manifest_identity(manifest)
            except CleanupExecutionError as exc:
                issues.append(f"{path.name}: {exc}")
                continue
            source = Path(str(manifest.get("original_path", "")))
            destination = Path(str(manifest.get("quarantine_path", "")))
            state = str(manifest.get("state", "prepared"))
            source_matches = _path_matches(source, identity)
            destination_matches = _path_matches(destination, identity)
            next_state = state
            if state == "prepared":
                if destination_matches and not source_matches:
                    next_state = "isolated"
                    recoveries.append(f"{path.name}: completed prepared move")
                elif source_matches and not destination_matches:
                    next_state = "aborted"
                    recoveries.append(f"{path.name}: cleared unused reservation")
                else:
                    issues.append(f"{path.name}: ambiguous prepared state")
            elif state == "restoring":
                if source_matches and not destination_matches:
                    next_state = "restored"
                    recoveries.append(f"{path.name}: completed restore accounting")
                elif destination_matches and not source_matches:
                    next_state = "isolated"
                    recoveries.append(f"{path.name}: rolled back incomplete restore")
                else:
                    issues.append(f"{path.name}: ambiguous restoring state")
            elif state == "purging":
                if destination_matches:
                    next_state = "isolated"
                    recoveries.append(f"{path.name}: rolled back incomplete purge")
                elif not _path_exists(destination):
                    next_state = "purged"
                    recoveries.append(f"{path.name}: completed purge accounting")
                else:
                    issues.append(f"{path.name}: purge target identity changed")
            elif state == "isolated" and not destination_matches:
                issues.append(f"{path.name}: isolated target is missing or changed")
            if rebuild and next_state != state:
                manifest["state"] = next_state
                manifest["recovered_at"] = datetime.now(timezone.utc).isoformat()
                _atomic_json_write(path, manifest)
            if next_state == "isolated" and destination_matches:
                manifest_items += 1
                manifest_bytes += max(
                    0,
                    int(manifest.get("estimated_reclaimable_bytes", 0)),
                )
        try:
            ledger = _read_ledger(root)
        except CleanupExecutionError as exc:
            issues.append(str(exc))
            ledger = None
        ledger_bytes = int(ledger["isolated_bytes"]) if ledger else 0
        ledger_items = int(ledger["item_count"]) if ledger else 0
        pending = ledger.get("pending", {}) if ledger else {}
        pending_items = len(pending) if isinstance(pending, dict) else 0
        matches = (
            ledger is not None
            and ledger_bytes == manifest_bytes
            and ledger_items == manifest_items
            and pending_items == 0
        )
        if rebuild:
            ledger = _empty_ledger()
            ledger["isolated_bytes"] = manifest_bytes
            ledger["item_count"] = manifest_items
            _write_ledger(root, ledger)
            ledger_bytes = manifest_bytes
            ledger_items = manifest_items
            pending_items = 0
            matches = True
        return QuarantineLedgerStatus(
            root=str(root),
            ledger_bytes=ledger_bytes,
            ledger_items=ledger_items,
            manifest_bytes=manifest_bytes,
            manifest_items=manifest_items,
            pending_items=pending_items,
            ledger_matches=matches,
            rebuilt=rebuild,
            recoveries=tuple(recoveries),
            issues=tuple(issues),
        )

    @staticmethod
    def _remove_from_ledger(
        ledger: dict[str, object],
        manifest: dict[str, object],
    ) -> None:
        size = max(0, int(manifest.get("estimated_reclaimable_bytes", 0)))
        ledger["isolated_bytes"] = max(0, int(ledger["isolated_bytes"]) - size)
        ledger["item_count"] = max(0, int(ledger["item_count"]) - 1)

    def _transition(self, name: str) -> None:
        if self._transition_hook is not None:
            self._transition_hook(name)


def permanent_delete(
    path: str | Path,
    *,
    expected_identity: FileIdentity | None = None,
    token: CleanupMutationToken | None = None,
    mutation_hook: MutationHook | None = None,
) -> None:
    value = Path(path)
    identity = expected_identity or identity_from_path(value)
    bound_token = token or create_mutation_token(value, identity)
    _delete_with_token(
        bound_token,
        allow_directory=False,
        mutation_hook=mutation_hook,
    )


def available_bytes(path: str | Path) -> int:
    return int(shutil.disk_usage(Path(path).parent).free)


def _restore_isolated(
    undo: CleanupUndo,
    expected_identity: FileIdentity | None = None,
) -> None:
    original = Path(undo.original_path)
    isolated = Path(undo.isolated_path)
    if original.exists() or original.is_symlink():
        raise CleanupExecutionError(
            f"restore refused because the original path exists: {original}"
        )
    if not isolated.exists() and not isolated.is_symlink():
        raise CleanupExecutionError(f"isolated path is missing: {isolated}")
    if not original.parent.exists():
        raise CleanupExecutionError(
            f"restore refused because the original parent is missing: {original.parent}"
        )
    identity = expected_identity or identity_from_path(isolated)
    _move_path(isolated, original, identity)
    if undo.metadata_path:
        Path(undo.metadata_path).unlink(missing_ok=True)


def _unique_destination(directory: Path, preferred: str) -> Path:
    candidate = directory / preferred
    index = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = directory / f"{preferred}.{index}"
        index += 1
    return candidate


def _unique_quarantine_destination(directory: Path, preferred: str) -> Path:
    candidate = directory / preferred
    index = 1
    while (
        candidate.exists()
        or candidate.is_symlink()
        or (directory / f"{candidate.name}.manifest.json").exists()
    ):
        candidate = directory / f"{preferred}.{index}"
        index += 1
    return candidate


def _atomic_json_write(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _token_for_action(
    action: CleanupAction,
    token: CleanupMutationToken | None,
) -> CleanupMutationToken:
    if action.identity is None:
        raise CleanupExecutionError("cleanup action has no target identity")
    if token is None:
        return create_mutation_token(action.path, action.identity)
    if Path(token.path) != Path(action.path):
        raise CleanupExecutionError("mutation token path does not match cleanup action")
    if not action.identity.matches(token.target_identity):
        raise CleanupExecutionError("mutation token identity does not match cleanup action")
    return token


def _move_path(
    source: Path,
    destination: Path,
    expected_identity: FileIdentity,
    *,
    mutation_hook: MutationHook | None = None,
) -> None:
    token = create_mutation_token(source, expected_identity)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _move_with_token(
        token,
        destination.parent,
        destination.name,
        mutation_hook=mutation_hook,
    )


def _move_with_token(
    token: CleanupMutationToken,
    destination_directory: Path,
    destination_name: str,
    *,
    mutation_hook: MutationHook | None = None,
) -> None:
    _validate_token(token)
    if token.mode != "dir-fd" or not mutation_capabilities().dir_fd_verification:
        _move_with_path_revalidation(
            token,
            destination_directory / destination_name,
            mutation_hook=mutation_hook,
        )
        return
    with _open_verified_parent(token) as source_fd:
        destination_fd = _open_directory(destination_directory)
        try:
            if os.fstat(destination_fd).st_dev != token.target_identity.device:
                raise CleanupExecutionError(
                    "recoverable move destination is on a different filesystem"
                )
            if _entry_exists(destination_fd, destination_name):
                raise CleanupExecutionError(
                    f"recoverable move destination already exists: {destination_name}"
                )
            if mutation_hook is not None:
                mutation_hook(token)
            _verify_entry(source_fd, token.entry_name, token.target_identity)
            os.rename(
                token.entry_name,
                destination_name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
            )
            try:
                moved = _identity_at(destination_fd, destination_name)
            except OSError as exc:
                raise CleanupExecutionError(
                    f"moved target could not be reverified: {exc}"
                ) from exc
            if not token.target_identity.matches(moved):
                restored = _rollback_bound_move(
                    source_fd,
                    token.entry_name,
                    destination_fd,
                    destination_name,
                )
                suffix = " and was restored" if restored else " and could not be restored"
                raise CleanupExecutionError(
                    "target identity changed during recoverable move" + suffix
                )
        finally:
            os.close(destination_fd)


def _move_with_path_revalidation(
    token: CleanupMutationToken,
    destination: Path,
    *,
    mutation_hook: MutationHook | None,
) -> None:
    source = Path(token.path)
    if destination.exists() or destination.is_symlink():
        raise CleanupExecutionError(
            f"recoverable move destination already exists: {destination}"
        )
    if mutation_hook is not None:
        mutation_hook(token)
    current = identity_from_path(source)
    if not token.target_identity.matches(current):
        raise CleanupExecutionError("target identity changed immediately before rename")
    os.rename(source, destination)
    moved = identity_from_path(destination)
    if not token.target_identity.matches(moved):
        if not source.exists() and not source.is_symlink():
            os.rename(destination, source)
        raise CleanupExecutionError("target identity changed during recoverable move")


def _delete_with_token(
    token: CleanupMutationToken,
    *,
    allow_directory: bool,
    mutation_hook: MutationHook | None,
) -> None:
    capabilities = mutation_capabilities()
    if not capabilities.dir_fd_verification:
        raise CleanupExecutionError(
            "permanent deletion is blocked because dir-fd verification is unavailable"
        )
    if token.target_identity.is_dir and not allow_directory:
        raise CleanupExecutionError(
            "permanent directory deletion is disabled; quarantine it first, then purge"
        )
    with _open_verified_parent(token) as parent_fd:
        if mutation_hook is not None:
            mutation_hook(token)
        _verify_entry(parent_fd, token.entry_name, token.target_identity)
        if token.target_identity.is_dir:
            directory_fd = os.open(
                token.entry_name,
                _directory_open_flags(),
                dir_fd=parent_fd,
            )
            try:
                opened = _identity_from_stat(os.fstat(directory_fd))
                if not token.target_identity.matches(opened):
                    raise CleanupExecutionError(
                        "directory identity changed immediately before purge"
                    )
                _delete_directory_contents(directory_fd)
            finally:
                os.close(directory_fd)
            os.rmdir(token.entry_name, dir_fd=parent_fd)
            return
        staging_name = _unique_entry_name(
            parent_fd,
            f".disktide-delete-{uuid4().hex}",
        )
        os.rename(
            token.entry_name,
            staging_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        staged = _identity_at(parent_fd, staging_name)
        if not token.target_identity.matches(staged):
            restored = _rollback_bound_move(
                parent_fd,
                token.entry_name,
                parent_fd,
                staging_name,
            )
            suffix = " and was restored" if restored else " and could not be restored"
            raise CleanupExecutionError(
                "target identity changed during permanent deletion" + suffix
            )
        os.unlink(staging_name, dir_fd=parent_fd)


def _delete_directory_contents(directory_fd: int) -> None:
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        identity = _identity_at(directory_fd, name)
        if identity.is_dir and not identity.is_symlink:
            child_fd = os.open(name, _directory_open_flags(), dir_fd=directory_fd)
            try:
                opened = _identity_from_stat(os.fstat(child_fd))
                if not identity.same_object(opened):
                    raise CleanupExecutionError(
                        f"directory entry changed during quarantine purge: {name}"
                    )
                _delete_directory_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


@contextmanager
def _open_verified_parent(token: CleanupMutationToken) -> Iterator[int]:
    _validate_token(token)
    descriptor = _open_directory(Path(token.parent_path))
    try:
        parent = _identity_from_stat(os.fstat(descriptor))
        if not token.parent_identity.same_object(parent):
            raise CleanupExecutionError("target parent identity changed before mutation")
        _verify_entry(descriptor, token.entry_name, token.target_identity)
        yield descriptor
    finally:
        os.close(descriptor)


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, _directory_open_flags())
    except OSError as exc:
        raise CleanupExecutionError(f"directory fd open failed for {path}: {exc}") from exc


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _validate_token(token: CleanupMutationToken) -> None:
    if datetime.now(timezone.utc) > token.expires_at:
        raise CleanupExecutionError("mutation token expired before filesystem action")
    if not token.entry_name or Path(token.entry_name).name != token.entry_name:
        raise CleanupExecutionError("mutation token contains an invalid entry name")


def _identity_at(directory_fd: int, name: str) -> FileIdentity:
    return _identity_from_stat(
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    )


def _verify_entry(
    directory_fd: int,
    name: str,
    expected_identity: FileIdentity,
) -> None:
    try:
        current = _identity_at(directory_fd, name)
    except OSError as exc:
        raise CleanupExecutionError(
            f"target entry could not be verified immediately before mutation: {exc}"
        ) from exc
    if not expected_identity.matches(current):
        raise CleanupExecutionError(
            "target identity changed immediately before filesystem mutation"
        )


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _rollback_bound_move(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> bool:
    if _entry_exists(source_fd, source_name):
        return False
    try:
        os.rename(
            destination_name,
            source_name,
            src_dir_fd=destination_fd,
            dst_dir_fd=source_fd,
        )
        return True
    except OSError:
        return False


def _unique_entry_name(directory_fd: int, preferred: str) -> str:
    candidate = preferred
    index = 1
    while _entry_exists(directory_fd, candidate):
        candidate = f"{preferred}.{index}"
        index += 1
    return candidate


def _required_manifest_path(undo: CleanupUndo) -> Path:
    if not undo.metadata_path:
        raise CleanupExecutionError("quarantine recovery manifest is missing")
    return Path(undo.metadata_path)


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CleanupExecutionError(
            f"quarantine manifest is unreadable: {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise CleanupExecutionError(f"quarantine manifest is not an object: {path}")
    return payload


def _manifest_identity(manifest: dict[str, object]) -> FileIdentity:
    payload = manifest.get("identity")
    if not isinstance(payload, dict):
        raise CleanupExecutionError("quarantine manifest identity is missing")
    try:
        return FileIdentity(
            device=int(payload["device"]),
            inode=int(payload["inode"]),
            mode=int(payload["mode"]),
            size=int(payload["size"]),
            mtime_ns=int(payload["mtime_ns"]),
            is_dir=bool(payload["is_dir"]),
            is_symlink=bool(payload["is_symlink"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CleanupExecutionError(
            f"quarantine manifest identity is invalid: {exc}"
        ) from exc


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _path_matches(path: Path, expected_identity: FileIdentity) -> bool:
    try:
        return expected_identity.matches(identity_from_path(path))
    except OSError:
        return False


def _empty_ledger() -> dict[str, object]:
    return {
        "version": 1,
        "isolated_bytes": 0,
        "item_count": 0,
        "pending": {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _ledger_path(root: Path) -> Path:
    return root / ".ledger.json"


def _read_ledger(root: Path) -> dict[str, object] | None:
    path = _ledger_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or int(payload.get("version", 0)) != 1:
            raise ValueError("unsupported ledger format")
        isolated_bytes = int(payload["isolated_bytes"])
        item_count = int(payload["item_count"])
        pending = payload["pending"]
        if isolated_bytes < 0 or item_count < 0 or not isinstance(pending, dict):
            raise ValueError("invalid ledger counters")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise CleanupExecutionError(f"quarantine ledger is invalid: {exc}") from exc
    return payload


def _write_ledger(root: Path, ledger: dict[str, object]) -> None:
    ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_json_write(_ledger_path(root), ledger)


def _require_resolved_ledger(status: QuarantineLedgerStatus) -> None:
    if status.issues:
        raise CleanupExecutionError(
            "quarantine recovery found unresolved evidence; run "
            f"'disktide cleanup quarantine audit {status.root}'"
        )


@contextmanager
def _ledger_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".ledger.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

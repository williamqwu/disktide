"""Low-level cleanup executors used only behind CleanupService safety gates."""

from __future__ import annotations

import os
import shutil
import json
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fs_monitor.domain.cleanup import (
    CleanupAction,
    CleanupActionKind,
    CleanupUndo,
    FileIdentity,
)
from fs_monitor.models.patterns import CleanupTarget, RiskLevel


class CleanupExecutionError(RuntimeError):
    """Raised when an executor cannot preserve the cleanup safety contract."""


def identity_from_path(path: str | Path) -> FileIdentity:
    value = os.lstat(path)
    return FileIdentity(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        mode=int(value.st_mode),
        size=int(value.st_size),
        mtime_ns=int(value.st_mtime_ns),
        is_dir=stat.S_ISDIR(value.st_mode),
        is_symlink=stat.S_ISLNK(value.st_mode),
    )


class XDGTrashAdapter:
    """Freedesktop Trash implementation for same-filesystem atomic moves."""

    def __init__(self, root: str | Path | None = None):
        if root is None:
            data_home = Path(
                os.environ.get(
                    "XDG_DATA_HOME",
                    os.path.expanduser("~/.local/share"),
                )
            )
            root = data_home / "Trash"
        self.root = Path(root)

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

    def move(self, action: CleanupAction) -> CleanupUndo:
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
        os.rename(source, destination)
        try:
            info_path.write_text(info, encoding="utf-8")
            os.chmod(info_path, 0o600)
        except Exception as exc:
            try:
                os.rename(destination, source)
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

    def restore(self, undo: CleanupUndo) -> None:
        _restore_isolated(undo)

    def _ensure_root(self) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.info_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (self.root, self.files_dir, self.info_dir):
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise CleanupExecutionError(f"unsafe Trash directory: {path}")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise CleanupExecutionError(f"Trash directory is not owned by this user: {path}")


class QuarantineExecutor:
    """Same-filesystem atomic isolation with a local recovery manifest."""

    def __init__(
        self,
        *,
        retention_days: int = 7,
        max_bytes: int = 10 * 1024**3,
    ):
        self.retention_days = max(1, retention_days)
        self.max_bytes = max(1, max_bytes)

    @staticmethod
    def root_for(path: str | Path) -> Path:
        return Path(path).parent / ".fsmonitor-quarantine"

    def move(self, action: CleanupAction) -> CleanupUndo:
        source = Path(action.path)
        root = self.root_for(source)
        self._ensure_root(root)
        source_identity = identity_from_path(source)
        if source_identity.device != root.stat().st_dev:
            raise CleanupExecutionError(
                "quarantine must be on the same filesystem as the target"
            )
        current_bytes = self._manifest_bytes(root)
        if current_bytes + action.estimated_reclaimable_bytes > self.max_bytes:
            raise CleanupExecutionError("quarantine capacity policy would be exceeded")
        destination = _unique_destination(
            root,
            f"{action.id[:12]}-{source.name or 'item'}",
        )
        manifest_path = root / f"{destination.name}.manifest.json"
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=self.retention_days
        )
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
        _atomic_json_write(manifest_path, manifest)
        try:
            os.rename(source, destination)
            manifest["state"] = "isolated"
            _atomic_json_write(manifest_path, manifest)
        except Exception as exc:
            if destination.exists() or destination.is_symlink():
                try:
                    os.rename(destination, source)
                except OSError:
                    pass
            manifest_path.unlink(missing_ok=True)
            raise CleanupExecutionError(f"quarantine move failed: {exc}") from exc
        return CleanupUndo(
            strategy=CleanupActionKind.QUARANTINE,
            original_path=str(source),
            isolated_path=str(destination),
            metadata_path=str(manifest_path),
            expires_at=expires_at,
        )

    def restore(self, undo: CleanupUndo) -> None:
        _restore_isolated(undo)

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

    @staticmethod
    def _manifest_bytes(root: Path) -> int:
        total = 0
        for path in root.glob("*.manifest.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("state") == "isolated":
                    total += int(payload.get("estimated_reclaimable_bytes", 0))
            except (OSError, ValueError, TypeError):
                continue
        return total


def permanent_delete(path: str | Path) -> None:
    """Delete exactly one already-revalidated entry without following symlinks."""
    value = Path(path)
    identity = identity_from_path(value)
    if identity.is_symlink or not identity.is_dir:
        os.unlink(value)
    else:
        shutil.rmtree(value)


def available_bytes(path: str | Path) -> int:
    return int(shutil.disk_usage(Path(path).parent).free)


def _restore_isolated(undo: CleanupUndo) -> None:
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
    os.rename(isolated, original)
    if undo.metadata_path:
        Path(undo.metadata_path).unlink(missing_ok=True)


def _unique_destination(directory: Path, preferred: str) -> Path:
    candidate = directory / preferred
    index = 1
    while candidate.exists() or candidate.is_symlink():
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


@dataclass
class DeletionResult:
    """Result of a deletion operation."""
    path: str
    size: int
    rule_name: str
    success: bool = True
    error: str | None = None
    dry_run: bool = False


@dataclass
class CleanupResult:
    """Aggregated results from a cleanup operation."""
    results: list[DeletionResult] = field(default_factory=list)
    total_freed: int = 0
    total_errors: int = 0

    @property
    def successful(self) -> list[DeletionResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[DeletionResult]:
        return [r for r in self.results if not r.success]


def delete_targets(
    targets: list[CleanupTarget],
    dry_run: bool = False,
    progress_callback=None,
) -> CleanupResult:
    """Delete cleanup targets directly from the filesystem.

    Actual deletions are permanent: this helper provides no trash, undo,
    revalidation, or persistent audit log.

    Args:
        targets: List of targets to delete.
        dry_run: If True, simulate deletion without actually removing files.
        progress_callback: Optional callable(completed, total, current_path).
    """
    result = CleanupResult()
    total = len(targets)

    for i, target in enumerate(targets):
        if progress_callback:
            progress_callback(i, total, target.path)

        dr = DeletionResult(
            path=target.path,
            size=target.size,
            rule_name=target.rule.name,
            dry_run=dry_run,
        )

        if dry_run:
            dr.success = True
            result.results.append(dr)
            result.total_freed += target.size
            continue

        try:
            if os.path.islink(target.path):
                os.unlink(target.path)
            elif os.path.isdir(target.path):
                shutil.rmtree(target.path)
            elif os.path.exists(target.path):
                os.unlink(target.path)
            else:
                dr.success = False
                dr.error = "Path does not exist"
                result.total_errors += 1
                result.results.append(dr)
                continue

            dr.success = True
            result.total_freed += target.size
        except PermissionError:
            dr.success = False
            dr.error = f"Permission denied: {target.path}"
            result.total_errors += 1
        except OSError as e:
            dr.success = False
            dr.error = str(e)
            result.total_errors += 1

        result.results.append(dr)

    if progress_callback:
        progress_callback(total, total, "")

    return result

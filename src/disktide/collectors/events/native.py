"""Linux inotify adapter loaded only when the ``watch`` extra is installed."""

from __future__ import annotations

import errno
import os
import sys
import threading
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from disktide.collectors.events.base import (
    EventBackendInfo,
    EventCallback,
    EventWatch,
    FilesystemEvent,
    FilesystemEventKind,
)
from disktide.domain.monitor import WatchDiagnostics
from disktide.domain.policy import ScanPolicy
from disktide.extensions.capabilities import CapabilityStatus
from disktide.scanner.policy import discover_pseudo_mounts


class NativeEventBackendUnavailable(RuntimeError):
    pass


#: Errnos that describe one directory rather than the inotify instance.
#: `inotify_add_watch(2)` returns EACCES when the directory may not be read,
#: and a directory disktide may not read is one no scan can look inside
#: either -- so skipping it loses nothing, while treating it as a backend
#: failure cost a full rescan of the whole tree on every session pass.
_PER_DIRECTORY_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.EPERM,
        errno.ENOENT,
        errno.ENOTDIR,
        errno.ELOOP,
        errno.ENAMETOOLONG,
    }
)


@dataclass(frozen=True, slots=True)
class _WatchState:
    path: str
    root_path: str
    depth: int
    root_device: int
    excluded_mounts: frozenset[str]
    policy: ScanPolicy


@dataclass(frozen=True, slots=True)
class _PendingMove:
    path: str
    root_path: str
    is_directory: bool
    created_at: float


def _read_linux_limit(name: str) -> int | None:
    try:
        return int(Path("/proc/sys/fs/inotify", name).read_text().strip())
    except (OSError, TypeError, ValueError):
        return None


def probe_native_event_backend() -> EventBackendInfo:
    if not sys.platform.startswith("linux"):
        return EventBackendInfo(
            name="inotify-simple",
            status=CapabilityStatus.UNAVAILABLE,
            reason=f"native event acceleration is not implemented for {sys.platform}",
            suggestion="Use periodic mode; Linux inotify support is available through disktide[watch].",
            system=sys.platform,
        )
    descriptor_limit = _read_linux_limit("max_user_watches")
    instance_limit = _read_linux_limit("max_user_instances")
    queued_event_limit = _read_linux_limit("max_queued_events")
    if find_spec("inotify_simple") is None:
        return EventBackendInfo(
            name="inotify-simple",
            status=CapabilityStatus.UNAVAILABLE,
            reason="the optional inotify backend is not installed",
            suggestion="Install with: uv tool install 'disktide[watch]'",
            system=sys.platform,
            descriptor_limit=descriptor_limit,
            instance_limit=instance_limit,
            queued_event_limit=queued_event_limit,
        )
    try:
        backend_version = version("inotify-simple")
    except PackageNotFoundError:
        backend_version = "unknown"
    return EventBackendInfo(
        name="inotify-simple",
        version=backend_version,
        status=CapabilityStatus.AVAILABLE,
        reason="Linux inotify event acceleration is available",
        suggestion="Periodic full reconciliation remains enabled.",
        system=sys.platform,
        descriptor_limit=descriptor_limit,
        instance_limit=instance_limit,
        queued_event_limit=queued_event_limit,
    )


def create_native_event_backend() -> InotifyEventBackend:
    info = probe_native_event_backend()
    if not info.available:
        raise NativeEventBackendUnavailable(info.reason)
    return InotifyEventBackend()


class InotifyEventBackend:
    """Recursive inotify watcher with explicit move and overflow signals."""

    def __init__(
        self,
        *,
        read_timeout_ms: int = 100,
        move_timeout_seconds: float = 0.25,
    ):
        self._read_timeout_ms = max(10, int(read_timeout_ms))
        self._move_timeout_seconds = max(0.05, float(move_timeout_seconds))
        self._callback: EventCallback | None = None
        self._notifier: Any = None
        self._flags: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._watches_by_wd: dict[int, _WatchState] = {}
        self._wd_by_path: dict[str, int] = {}
        self._pending_moves: dict[int, _PendingMove] = {}
        self._roots: tuple[str, ...] = ()
        self._root_states: dict[str, _WatchState] = {}
        self._registration_started_at: float | None = None
        self._registration_duration_seconds: float | None = None
        self._registration_strategy = "unavailable"
        self._registration_in_progress = False
        self._warning: str | None = None
        self._fallback_reason: str | None = None
        self._unwatchable_count = 0
        self._unwatchable_first: str | None = None
        info = probe_native_event_backend()
        self._descriptor_limit = info.descriptor_limit
        self._instance_limit = info.instance_limit
        self._queued_event_limit = info.queued_event_limit

    @property
    def name(self) -> str:
        return "inotify-simple"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def watched_roots(self) -> tuple[str, ...]:
        return self._roots

    @property
    def diagnostics(self) -> WatchDiagnostics:
        with self._lock:
            duration = self._registration_duration_seconds
            if self._registration_in_progress and self._registration_started_at is not None:
                duration = max(0.0, time.monotonic() - self._registration_started_at)
            return WatchDiagnostics(
                descriptor_count=len(self._watches_by_wd),
                descriptor_limit=self._descriptor_limit,
                instance_limit=self._instance_limit,
                queued_event_limit=self._queued_event_limit,
                registration_duration_seconds=duration,
                registration_strategy=self._registration_strategy,
                registration_in_progress=self._registration_in_progress,
                warning=self._skipped_warning_locked(),
                fallback_reason=self._fallback_reason,
            )

    def start(
        self,
        watches: tuple[EventWatch, ...],
        callback: EventCallback,
    ) -> None:
        self._prepare_start(watches, callback, strategy="recursive-prewalk")
        try:
            for item, root_path in zip(watches, self._roots, strict=True):
                self._add_watch_tree(root_path, item)
        except Exception:
            self.stop()
            raise
        self.finish_registration()
        self._start_thread()

    def start_discovery_handoff(
        self,
        watches: tuple[EventWatch, ...],
        callback: EventCallback,
    ) -> None:
        """Start with root watches; the scanner registers each directory before read."""

        self._prepare_start(watches, callback, strategy="scan-driven-handoff")
        try:
            for item, root_path in zip(watches, self._roots, strict=True):
                state = self._build_root_state(root_path, item)
                self._root_states[root_path] = state
                self._add_one(root_path, state)
        except Exception:
            self.stop()
            raise
        self._start_thread()

    def register_directory(self, path: str) -> None:
        """Register a directory immediately before the scanner enumerates it."""

        normalized = str(Path(path).expanduser().resolve())
        root_path = next(
            (
                root
                for root in sorted(self._roots, key=len, reverse=True)
                if normalized == root
                or normalized.startswith(root.rstrip(os.sep) + os.sep)
            ),
            None,
        )
        if root_path is None:
            return
        root_state = self._root_states.get(root_path)
        if root_state is None:
            return
        try:
            depth = len(Path(normalized).relative_to(root_path).parts)
        except ValueError:
            return
        state = _WatchState(
            path=normalized,
            root_path=root_path,
            depth=depth,
            root_device=root_state.root_device,
            excluded_mounts=root_state.excluded_mounts,
            policy=root_state.policy,
        )
        if not self._path_allowed(normalized, state):
            return
        self._add_one(normalized, state)

    def finish_registration(self) -> None:
        with self._lock:
            if not self._registration_in_progress:
                return
            started = self._registration_started_at
            self._registration_duration_seconds = (
                max(0.0, time.monotonic() - started)
                if started is not None
                else None
            )
            self._registration_in_progress = False

    def _prepare_start(
        self,
        watches: tuple[EventWatch, ...],
        callback: EventCallback,
        *,
        strategy: str,
    ) -> None:
        if self.running:
            raise RuntimeError("event backend is already running")
        if not watches:
            raise ValueError("at least one event watch is required")
        info = probe_native_event_backend()
        if not info.available:
            raise NativeEventBackendUnavailable(info.reason)
        from inotify_simple import INotify, flags

        self._callback = callback
        self._flags = flags
        self._notifier = INotify(nonblocking=False)
        self._stop.clear()
        self._roots = tuple(
            str(Path(item.root_path).expanduser().resolve()) for item in watches
        )
        self._root_states.clear()
        self._registration_started_at = time.monotonic()
        self._registration_duration_seconds = None
        self._registration_strategy = strategy
        self._registration_in_progress = True
        self._warning = None
        self._fallback_reason = None
        self._unwatchable_count = 0
        self._unwatchable_first = None

    def _start_thread(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="disktide-inotify",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        notifier = self._notifier
        if notifier is not None:
            try:
                notifier.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            self._thread = None
            self._notifier = None
            self._watches_by_wd.clear()
            self._wd_by_path.clear()
            self._pending_moves.clear()
            self._roots = ()
            self._root_states.clear()
            self._registration_in_progress = False

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    events = self._notifier.read(timeout=self._read_timeout_ms)
                except (OSError, ValueError) as exc:
                    if self._stop.is_set():
                        return
                    self._emit(
                        FilesystemEventKind.BACKEND_ERROR,
                        self._roots[0] if self._roots else "/",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                    return
                for event in events:
                    self._handle_raw_event(event)
                self._flush_unpaired_moves()
        finally:
            self._flush_unpaired_moves(force=True)

    def _handle_raw_event(self, event: Any) -> None:
        flags = self._flags
        mask = event.mask
        if mask & flags.Q_OVERFLOW:
            for root in self._roots:
                self._emit(
                    FilesystemEventKind.OVERFLOW,
                    root,
                    is_directory=True,
                    detail="inotify queue overflowed",
                )
            return
        with self._lock:
            state = self._watches_by_wd.get(event.wd)
        if state is None:
            return
        path = os.path.normpath(
            os.path.join(state.path, event.name) if event.name else state.path
        )
        is_directory = bool(mask & flags.ISDIR)

        if mask & flags.IGNORED:
            self._forget_watch(event.wd)
            return
        if mask & (flags.UNMOUNT | flags.DELETE_SELF | flags.MOVE_SELF):
            if path == state.root_path:
                self._emit(
                    FilesystemEventKind.ROOT_LOST,
                    path,
                    is_directory=True,
                    detail="watched root was removed, moved, or unmounted",
                )
            else:
                self._emit(
                    FilesystemEventKind.DELETE,
                    path,
                    is_directory=True,
                )
            self._remove_path_prefix(path)
            return
        if mask & flags.MOVED_FROM:
            self._pending_moves[event.cookie] = _PendingMove(
                path=path,
                root_path=state.root_path,
                is_directory=is_directory,
                created_at=time.monotonic(),
            )
            return
        if mask & flags.MOVED_TO:
            source = self._pending_moves.pop(event.cookie, None)
            if source is not None:
                if source.is_directory:
                    self._remap_directory(source.path, path)
                self._emit(
                    FilesystemEventKind.MOVE,
                    source.path,
                    destination_path=path,
                    is_directory=source.is_directory or is_directory,
                )
            else:
                if is_directory:
                    self._add_new_directory(path, state)
                self._emit(
                    FilesystemEventKind.CREATE,
                    path,
                    is_directory=is_directory,
                )
            return
        if mask & flags.CREATE:
            if is_directory:
                self._add_new_directory(path, state)
            self._emit(
                FilesystemEventKind.CREATE,
                path,
                is_directory=is_directory,
            )
            return
        if mask & flags.DELETE:
            if is_directory:
                self._remove_path_prefix(path)
            self._emit(
                FilesystemEventKind.DELETE,
                path,
                is_directory=is_directory,
            )
            return
        if mask & (flags.CLOSE_WRITE | flags.MODIFY | flags.ATTRIB):
            self._emit(
                FilesystemEventKind.MODIFY,
                path,
                is_directory=is_directory,
            )

    def _add_watch_tree(
        self,
        path: str,
        watch: EventWatch,
        *,
        watch_root: str | None = None,
        depth_offset: int = 0,
        root_device: int | None = None,
        excluded_mounts: frozenset[str] | None = None,
    ) -> None:
        monitor_root = watch_root or path
        root_stat = os.stat(monitor_root, follow_symlinks=False)
        if not os.path.isdir(path):
            raise NotADirectoryError(path)
        excluded = (
            excluded_mounts
            if excluded_mounts is not None
            else frozenset(
                discover_pseudo_mounts(monitor_root)
                if watch.policy.exclude_pseudo_filesystems
                else ()
            )
        )
        device = root_device if root_device is not None else root_stat.st_dev
        if monitor_root not in self._root_states:
            self._root_states[monitor_root] = _WatchState(
                path=monitor_root,
                root_path=monitor_root,
                depth=0,
                root_device=device,
                excluded_mounts=excluded,
                policy=watch.policy,
            )
        stack = [(path, depth_offset)]
        while stack:
            current_path, depth = stack.pop()
            state = _WatchState(
                path=current_path,
                root_path=monitor_root,
                depth=depth,
                root_device=device,
                excluded_mounts=excluded,
                policy=watch.policy,
            )
            if not self._path_allowed(current_path, state):
                continue
            self._add_one(current_path, state)
            if watch.policy.max_depth is not None and depth >= watch.policy.max_depth:
                continue
            try:
                with os.scandir(current_path) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append((entry.path, depth + 1))
                        except OSError:
                            continue
            except OSError as exc:
                if exc.errno in _PER_DIRECTORY_ERRNOS:
                    with self._lock:
                        self._note_unwatchable_locked(current_path, exc)
                    continue
                self._emit(
                    FilesystemEventKind.BACKEND_ERROR,
                    current_path,
                    is_directory=True,
                    detail=f"cannot enumerate watch tree: {exc}",
                )

    def _add_new_directory(self, path: str, parent: _WatchState) -> None:
        watch = EventWatch(root_path=parent.root_path, policy=parent.policy)
        try:
            self._add_watch_tree(
                path,
                watch,
                watch_root=parent.root_path,
                depth_offset=parent.depth + 1,
                root_device=parent.root_device,
                excluded_mounts=parent.excluded_mounts,
            )
        except OSError as exc:
            self._emit_watch_error(path, exc)

    def _add_one(self, path: str, state: _WatchState) -> None:
        flags = self._flags
        mask = (
            flags.CREATE
            | flags.MODIFY
            | flags.ATTRIB
            | flags.CLOSE_WRITE
            | flags.DELETE
            | flags.DELETE_SELF
            | flags.MOVED_FROM
            | flags.MOVED_TO
            | flags.MOVE_SELF
            | flags.UNMOUNT
            | flags.ONLYDIR
            | flags.DONT_FOLLOW
        )
        failure: OSError | None = None
        with self._lock:
            if path in self._wd_by_path or self._notifier is None:
                return
            try:
                wd = self._notifier.add_watch(path, mask)
            except OSError as exc:
                if exc.errno in _PER_DIRECTORY_ERRNOS:
                    # `inotify_add_watch` needs read access to the directory,
                    # and so does `os.scandir`: a directory this fails on is
                    # one no scan can see inside either, so there is nothing
                    # for events to miss. Reporting it as a backend failure
                    # made the service stop and restart the backend on every
                    # session pass, and each restart forces a full rescan.
                    self._note_unwatchable_locked(path, exc)
                    return
                self._fallback_reason = (
                    "inotify descriptor capacity was exhausted"
                    if exc.errno in {errno.ENOSPC, errno.EMFILE, errno.ENFILE}
                    else f"cannot register inotify descriptor: {exc}"
                )
                failure = exc
            else:
                self._watches_by_wd[wd] = state
                self._wd_by_path[path] = wd
                self._update_limit_warning_locked()
        if failure is not None:
            # The service handles this by stopping the backend, which joins
            # the reader thread -- and that thread reads `diagnostics`, which
            # takes this lock. Emitting under it deadlocks the pair.
            self._emit_watch_error(path, failure)

    def _build_root_state(self, root_path: str, watch: EventWatch) -> _WatchState:
        root_stat = os.stat(root_path, follow_symlinks=False)
        excluded = frozenset(
            discover_pseudo_mounts(root_path)
            if watch.policy.exclude_pseudo_filesystems
            else ()
        )
        return _WatchState(
            path=root_path,
            root_path=root_path,
            depth=0,
            root_device=root_stat.st_dev,
            excluded_mounts=excluded,
            policy=watch.policy,
        )

    def _update_limit_warning_locked(self) -> None:
        limit = self._descriptor_limit
        if limit is None or limit <= 0:
            return
        count = len(self._watches_by_wd)
        ratio = count / limit
        if ratio >= 0.8:
            self._warning = (
                f"inotify descriptors use {count:,}/{limit:,} "
                f"({ratio:.0%}); periodic fallback may be required"
            )

    def _note_unwatchable_locked(self, path: str, exc: OSError) -> None:
        """Remember a directory that cannot be watched, without failing.

        Only the first path is kept: a tree can hold thousands of these and
        the diagnostics line has room for one example and a count.
        """
        self._unwatchable_count += 1
        if self._unwatchable_first is None:
            self._unwatchable_first = f"{path}: {exc}"

    def _skipped_warning_locked(self) -> str | None:
        if not self._unwatchable_count:
            return self._warning
        plural = "y" if self._unwatchable_count == 1 else "ies"
        note = (
            f"{self._unwatchable_count:,} director{plural} could not be "
            f"watched and are left to the periodic scan "
            f"(first: {self._unwatchable_first})"
        )
        return f"{self._warning}; {note}" if self._warning else note

    def _emit_watch_error(self, path: str, exc: OSError) -> None:
        if exc.errno in _PER_DIRECTORY_ERRNOS:
            with self._lock:
                self._note_unwatchable_locked(path, exc)
            return
        if exc.errno in {errno.ENOSPC, errno.EMFILE, errno.ENFILE}:
            detail = "inotify watch limit reached; full reconciliation required"
        else:
            detail = f"cannot watch path: {exc}"
        self._emit(
            FilesystemEventKind.BACKEND_ERROR,
            path,
            is_directory=True,
            detail=detail,
        )

    def _path_allowed(self, path: str, state: _WatchState) -> bool:
        real = os.path.realpath(path)
        if real in state.excluded_mounts:
            return False
        if state.policy.one_file_system:
            try:
                if os.stat(path, follow_symlinks=False).st_dev != state.root_device:
                    return False
            except OSError:
                return False
        return True

    def _flush_unpaired_moves(self, *, force: bool = False) -> None:
        now = time.monotonic()
        stale = [
            cookie
            for cookie, item in self._pending_moves.items()
            if force or now - item.created_at >= self._move_timeout_seconds
        ]
        for cookie in stale:
            item = self._pending_moves.pop(cookie, None)
            if item is None:
                continue
            if item.is_directory:
                self._remove_path_prefix(item.path)
            self._emit(
                FilesystemEventKind.DELETE,
                item.path,
                is_directory=item.is_directory,
            )

    def _remap_directory(self, source: str, destination: str) -> None:
        prefix = source.rstrip(os.sep) + os.sep
        with self._lock:
            updates: list[tuple[int, _WatchState, str, int]] = []
            for wd, state in self._watches_by_wd.items():
                if state.path == source:
                    new_path = destination
                elif state.path.startswith(prefix):
                    new_path = destination + state.path[len(source):]
                else:
                    continue
                try:
                    new_depth = len(Path(new_path).relative_to(state.root_path).parts)
                except ValueError:
                    new_depth = state.depth
                updates.append((wd, state, new_path, new_depth))
            remove: list[int] = []
            for wd, state, new_path, new_depth in updates:
                if (
                    state.policy.max_depth is not None
                    and new_depth > state.policy.max_depth
                ):
                    remove.append(wd)
                    continue
                self._wd_by_path.pop(state.path, None)
                replacement = _WatchState(
                    path=new_path,
                    root_path=state.root_path,
                    depth=new_depth,
                    root_device=state.root_device,
                    excluded_mounts=state.excluded_mounts,
                    policy=state.policy,
                )
                self._watches_by_wd[wd] = replacement
                self._wd_by_path[new_path] = wd
        for wd in remove:
            self._forget_watch(wd)

    def _remove_path_prefix(self, path: str) -> None:
        prefix = path.rstrip(os.sep) + os.sep
        with self._lock:
            descriptors = [
                wd
                for wd, state in self._watches_by_wd.items()
                if state.path == path or state.path.startswith(prefix)
            ]
        for wd in descriptors:
            self._forget_watch(wd)

    def _forget_watch(self, wd: int) -> None:
        with self._lock:
            state = self._watches_by_wd.pop(wd, None)
            if state is not None:
                self._wd_by_path.pop(state.path, None)
        notifier = self._notifier
        if state is not None and notifier is not None:
            try:
                notifier.rm_watch(wd)
            except OSError:
                pass

    def _emit(
        self,
        kind: FilesystemEventKind,
        path: str,
        *,
        destination_path: str | None = None,
        is_directory: bool = False,
        detail: str | None = None,
    ) -> None:
        callback = self._callback
        if callback is None:
            return
        try:
            callback(
                FilesystemEvent(
                    kind=kind,
                    path=os.path.abspath(path),
                    destination_path=(
                        os.path.abspath(destination_path)
                        if destination_path is not None
                        else None
                    ),
                    is_directory=is_directory,
                    backend=self.name,
                    detail=detail,
                )
            )
        except Exception:
            return

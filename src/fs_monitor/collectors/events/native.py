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

from fs_monitor.collectors.events.base import (
    EventBackendInfo,
    EventCallback,
    EventWatch,
    FilesystemEvent,
    FilesystemEventKind,
)
from fs_monitor.domain.policy import ScanPolicy
from fs_monitor.extensions.capabilities import CapabilityStatus
from fs_monitor.scanner.policy import discover_pseudo_mounts


class NativeEventBackendUnavailable(RuntimeError):
    pass


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


def probe_native_event_backend() -> EventBackendInfo:
    if not sys.platform.startswith("linux"):
        return EventBackendInfo(
            name="inotify-simple",
            status=CapabilityStatus.UNAVAILABLE,
            reason=f"native event acceleration is not implemented for {sys.platform}",
            suggestion="Use periodic mode; Linux inotify support is available through fsmonitor-cli[watch].",
            system=sys.platform,
        )
    if find_spec("inotify_simple") is None:
        return EventBackendInfo(
            name="inotify-simple",
            status=CapabilityStatus.UNAVAILABLE,
            reason="the optional inotify backend is not installed",
            suggestion="Install with: uv tool install 'fsmonitor-cli[watch]'",
            system=sys.platform,
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

    @property
    def name(self) -> str:
        return "inotify-simple"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def watched_roots(self) -> tuple[str, ...]:
        return self._roots

    def start(
        self,
        watches: tuple[EventWatch, ...],
        callback: EventCallback,
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
        try:
            for item, root_path in zip(watches, self._roots, strict=True):
                self._add_watch_tree(root_path, item)
        except Exception:
            self.stop()
            raise
        self._thread = threading.Thread(
            target=self._run,
            name="fsmonitor-inotify",
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
        with self._lock:
            if path in self._wd_by_path:
                return
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
        try:
            wd = self._notifier.add_watch(path, mask)
        except OSError as exc:
            self._emit_watch_error(path, exc)
            return
        with self._lock:
            self._watches_by_wd[wd] = state
            self._wd_by_path[path] = wd

    def _emit_watch_error(self, path: str, exc: OSError) -> None:
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

"""CLI entry point (click-based)."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import click

from disktide import __version__


def _stdio_is_interactive() -> bool:
    """Whether both halves of the terminal the TUI needs are actually there.

    A closed or replaced stream can raise instead of answering, so a
    failed check counts as not-a-terminal.
    """
    for stream in (sys.stdin, sys.stdout):
        try:
            if stream is None or not stream.isatty():
                return False
        except (AttributeError, ValueError, OSError):
            return False
    return True


@click.group(invoke_without_command=True)
@click.option("--max-depth", "-d", type=int, default=None, help="Maximum scan depth")
@click.option("--workers", "-w", type=int, default=None, help="Number of scan threads")
@click.option(
    "--one-file-system/--cross-filesystems",
    default=None,
    help="Stay on the scan root filesystem",
)
@click.option(
    "--exclude-pseudo/--include-pseudo",
    default=None,
    help="Exclude pseudo-filesystem mountpoints below the scan root",
)
@click.option(
    "--no-mouse",
    is_flag=True,
    default=False,
    help="Launch the TUI without mouse support (this session only)",
)
@click.version_option(version=__version__)
@click.pass_context
def cli(
    ctx,
    max_depth: int | None,
    workers: int | None,
    one_file_system: bool | None,
    exclude_pseudo: bool | None,
    no_mouse: bool,
):
    """Interactive terminal disk usage explorer.

    \b
    Launch TUI:  disktide
    Subcommands: scan, watch, cleanup, compare, monitor, alerts, doctor
    """
    ctx.ensure_object(dict)
    ctx.obj["max_depth"] = max_depth
    ctx.obj["workers"] = workers
    ctx.obj["one_file_system"] = one_file_system
    ctx.obj["exclude_pseudo"] = exclude_pseudo
    ctx.obj["no_mouse"] = no_mouse

    if ctx.invoked_subcommand is None:
        if not _stdio_is_interactive():
            # Textual would otherwise sit forever waiting on a keypress
            # that a pipe or a redirect can never deliver, which reads as
            # a hang in CI and in `disktide > file`.
            raise click.ClickException(
                "the TUI needs an interactive terminal, but stdin and stdout "
                "are not both a TTY. Use a subcommand for non-interactive "
                "runs, e.g. 'disktide scan PATH' (add --json for "
                "machine-readable output)."
            )

        from disktide.app import DiskTideApp
        from disktide.config import load_config

        config = load_config()
        if max_depth is not None:
            config.scan.max_depth = max_depth
        if workers is not None:
            config.scan.workers = workers
        if one_file_system is not None:
            config.scan.one_file_system = one_file_system
        if exclude_pseudo is not None:
            config.scan.exclude_pseudo_filesystems = exclude_pseudo

        app = DiskTideApp(show_welcome=True, config=config)
        # --no-mouse is a session override, not a preference: it never
        # writes back to the config the settings screen owns.
        app.run(mouse=config.ui.mouse and not no_mouse)

        # Quitting is silent: the TUI restores the terminal and the
        # shell prompt is the only acknowledgement a user needs. Run
        # the cleanup that matters (cancel scan + close SQLite), then
        # hard-exit -- see `_force_teardown` for why we skip Python's
        # natural shutdown sequence on the way out.
        _force_teardown(app)
        sys.stdout.flush()
        sys.stderr.flush()
        # Bypass Python's interpreter teardown: gc of the in-memory
        # FSNode tree + atexit + module cleanup adds tens of seconds on
        # a multi-million-file scan, all spent freeing memory the kernel
        # is about to reclaim anyway. All cleanup that matters for
        # correctness (cancel + DB close) has already run above, and
        # both streams were flushed right before this block.
        import os as _os
        _os._exit(0)


def _force_teardown(app) -> None:
    """Run the minimum cleanup that must happen before exit.

    Only two things matter for correctness:

      1. Cancel any in-flight scan. The walker checks the engine's
         cancel event at every entry boundary (~10ms bail in practice),
         so a brief in-flight scan won't keep doing work between here
         and the os._exit below. We do NOT wait for the worker thread
         to actually return: it does read-only filesystem scans, so
         killing it mid-syscall via os._exit is safe.

      2. Close the SQLite handle. The TUI only ever writes to the DB
         from the main thread, so closing here is race-free, and a
         clean close keeps the WAL file from being left in a state
         that costs the next session a recovery pass.

    Everything else (joining worker threads, dropping the FSNode tree
    so it can be gc'd, running gc.collect() to dissolve Textual's
    screen/widget cycles) used to live here and was pure throwaway
    work: it ran for tens of seconds on a multi-million-file scan and
    accomplished nothing the kernel doesn't do for free on exit.
    """
    try:
        monitor_service = getattr(app, "_monitor_service", None)
        if monitor_service is not None:
            monitor_service.shutdown(wait=False)
        service = getattr(app, "_scan_service", None)
        if service is not None:
            service.cancel_all()
        elif getattr(app, "_explorer", None) is not None:
            app._explorer.cancel_active_scan()
    except Exception:
        pass
    repository = getattr(app, "_snapshot_repository", None)
    if repository is not None:
        try:
            repository.close()
        except Exception:
            pass


@cli.command()
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit a stable machine-readable report",
)
@click.option(
    "--show-paths",
    is_flag=True,
    help="Include raw application paths instead of redacted XDG paths",
)
def doctor(json_output: bool, show_paths: bool) -> None:
    """Report installation, database, and platform capabilities."""
    from disktide.services.doctor import (
        build_doctor_report,
        render_doctor_report,
    )

    report = build_doctor_report(show_paths=show_paths)
    if json_output:
        click.echo(report.to_json())
    else:
        click.echo(render_doctor_report(report))


@cli.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--snapshot", "-s", is_flag=True, help="Save snapshot to database")
@click.option("--max-depth", "-d", type=int, default=None, help="Maximum scan depth")
@click.option("--workers", "-w", type=int, default=None, help="Number of scan threads")
@click.option(
    "--metric",
    type=click.Choice(["logical", "allocated", "unique", "files"]),
    default="logical",
    show_default=True,
    help="Measurement used for totals, sorting, and bars",
)
@click.option(
    "--one-file-system/--cross-filesystems",
    default=False,
    show_default=True,
    help="Stay on the scan root filesystem",
)
@click.option(
    "--exclude-pseudo/--include-pseudo",
    default=True,
    show_default=True,
    help="Exclude pseudo-filesystem mountpoints below the scan root",
)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
@click.pass_context
def scan(
    ctx,
    path: str,
    snapshot: bool,
    max_depth: int | None,
    workers: int | None,
    metric: str,
    one_file_system: bool,
    exclude_pseudo: bool,
    json_output: bool,
):
    """Scan a directory and display results.

    Run status goes to stderr; stdout carries the report alone, so
    `disktide scan PATH > report.txt` captures the result and nothing else.
    """
    import json as json_module
    from disktide.domain.metrics import MetricId
    from disktide.domain.policy import ScanPolicy
    from disktide.domain.scan import (
        ScanCancelled,
        ScanCompleted,
        ScanFailed,
        ScanPhaseChanged,
        ScanProgressUpdated,
        ScanQueued,
        ScanRequest,
        ScanRequestError,
        ScanStarted,
        ScanStatus,
    )
    from disktide.metrics import (
        METRIC_EXPLANATIONS,
        METRIC_NAMES,
        metric_text,
        metric_value,
        metric_value_or_zero,
    )
    from disktide.services.scan import ScanService
    import humanize

    path = str(Path(path).resolve())
    policy = ScanPolicy(
        one_file_system=one_file_system,
        exclude_pseudo_filesystems=exclude_pseudo,
        max_depth=max_depth,
    )
    request = ScanRequest(
        path=path,
        metric=MetricId(metric),
        policy=policy,
        workers=workers,
        source="cli",
    )
    service = ScanService()
    try:
        run = service.create_run(request)
    except ScanRequestError as exc:
        raise click.BadParameter(str(exc), param_hint="path") from exc

    class Reporter:
        """Narrate the run on stderr, leaving stdout for the report.

        Everything here is transient status about a scan in flight --
        queue position, phase changes, the live counter. None of it is
        the result the caller asked for, so redirecting stdout must not
        capture it.
        """

        def __init__(self, *, quiet: bool) -> None:
            self.quiet = quiet
            # The counter is a carriage-return redraw. Against a pipe or
            # a file every refresh would survive as another \r-separated
            # copy, so it is emitted only when stderr is a terminal that
            # can overwrite the line.
            self.live = not quiet and sys.stderr.isatty()
            self.progress_written = False

        def _status(self, message: str) -> None:
            if not self.quiet:
                click.echo(message, err=True)

        def _finish_progress_line(self) -> None:
            if self.progress_written:
                click.echo(err=True)
                self.progress_written = False

        def __call__(self, event) -> None:
            if isinstance(event, ScanQueued):
                self._finish_progress_line()
                self._status(
                    f"Scan {event.run_id[:8]} queued at position "
                    f"{event.position}: {event.reason}"
                )
            elif isinstance(event, ScanStarted):
                self._status(f"Scan {event.run_id[:8]} started")
                self._status(f"  Path: {event.request.path}")
                self._status(f"  Phase: {event.phase.value}")
                self._status(f"  Policy: {event.policy.summary()}")
                if event.worker_selection is not None:
                    selection = event.worker_selection
                    requested = (
                        "auto"
                        if selection.requested_workers is None
                        else str(selection.requested_workers)
                    )
                    self._status(
                        f"  Workers: requested={requested}, "
                        f"effective={selection.effective_workers}"
                    )
                    self._status(f"  Worker reason: {selection.reason}")
                if event.resource_slot is not None:
                    self._status(f"  Resource slot: {event.resource_slot}")
            elif isinstance(event, ScanProgressUpdated):
                if not self.live:
                    return
                progress = event.progress
                click.echo(
                    f"\r  Run {event.run_id[:8]} [{event.phase.value}] "
                    f"{progress.dirs_scanned:,} dirs, "
                    f"{progress.files_scanned:,} files, logical "
                    f"{humanize.naturalsize(progress.logical_bytes, binary=True)}",
                    nl=False,
                    err=True,
                )
                self.progress_written = True
            elif isinstance(event, ScanPhaseChanged):
                if event.phase.value == "finalizing":
                    self._finish_progress_line()
                    self._status(f"  Phase: {event.phase.value}")
            elif isinstance(event, ScanCancelled):
                self._finish_progress_line()
                click.echo(
                    f"Scan {event.run_id[:8]} cancelled: {event.reason}",
                    err=True,
                )
            elif isinstance(event, ScanFailed):
                self._finish_progress_line()
                click.echo(
                    f"Scan {event.run_id[:8]} failed: "
                    f"{event.error_type}: {event.message}",
                    err=True,
                )
            elif isinstance(event, ScanCompleted):
                self._finish_progress_line()

    reporter = Reporter(quiet=json_output)
    run = service.execute(run, consumers=(reporter,))
    if run.status is ScanStatus.CANCELLED or run.status is ScanStatus.FAILED or run.root is None:
        # A JSON caller still gets a parseable document describing the
        # outcome; the exit code carries the same verdict either way.
        if json_output:
            click.echo(
                json_module.dumps(
                    {
                        "schema_version": 1,
                        "run_id": run.run_id,
                        "status": run.status.value,
                        "path": run.request.path,
                        "metric": run.request.metric.value,
                        "error_type": run.error_type,
                        "error_message": run.error_message,
                        "cancellation_reason": run.cancellation_reason,
                    },
                    sort_keys=True,
                )
            )
        ctx.exit(130 if run.status is ScanStatus.CANCELLED else 1)

    root = run.root
    metric = run.request.metric.value
    status_label = "partial" if run.partial else "complete"
    duplicate_links = sum(1 for node in root.walk() if node.is_hardlink_duplicate)
    children = sorted(
        (child for child in root.children if child.is_dir),
        key=lambda child: (-metric_value_or_zero(child, metric), child.name),
    )
    total_value = metric_value(root, metric)

    if json_output:
        payload = {
            "schema_version": 1,
            "run_id": run.run_id,
            "status": run.status.value,
            "partial": run.partial,
            "path": run.request.path,
            "metric": metric,
            "duration_seconds": round(run.duration_seconds, 3),
            "policy": (
                root.scan_policy.summary() if root.scan_policy is not None else None
            ),
            "resource_wait_seconds": round(run.resource_wait_seconds, 3),
            "capability_warnings": list(run.capability_warnings),
            "totals": {
                "logical_bytes": metric_value(root, "logical"),
                "allocated_bytes": metric_value(root, "allocated"),
                "unique_bytes": metric_value(root, "unique"),
                "file_count": root.file_count,
                "dir_count": root.dir_count,
            },
            "coverage": {
                "inaccessible_subtrees": root.inaccessible_subtree_count,
                "excluded_subtrees": root.excluded_subtree_count,
                "depth_limited_subtrees": root.depth_limited_subtree_count,
                "hardlink_duplicates": duplicate_links,
            },
            "workers": (
                None
                if run.worker_selection is None
                else {
                    "requested": run.worker_selection.requested_workers,
                    "effective": run.worker_selection.effective_workers,
                    "mode": run.worker_selection.mode,
                    "reason": run.worker_selection.reason,
                }
            ),
            # Direct child directories only. That is the "where did the
            # space go" answer, and its length is bounded by the root's
            # subdirectory count rather than by its file count.
            "children": [
                {
                    "name": child.name,
                    "path": child.path,
                    "logical_bytes": metric_value(child, "logical"),
                    "allocated_bytes": metric_value(child, "allocated"),
                    "unique_bytes": metric_value(child, "unique"),
                    "file_count": child.file_count,
                    "dir_count": child.dir_count,
                }
                for child in children
            ],
        }
        if snapshot:
            payload["snapshot"] = _save_scan_snapshot(run)
        click.echo(json_module.dumps(payload, sort_keys=True))
        return

    # No leading blank line: the report is the first thing on stdout now
    # that the run narration goes to stderr.
    click.echo(f"Scan {run.run_id[:8]} {status_label} in {run.duration_seconds:.1f}s")
    click.echo(f"  Status: {run.status.value}")
    metric_name = METRIC_NAMES[metric]
    click.echo(f"  Metric: {metric_name} — {METRIC_EXPLANATIONS[metric]}")
    click.echo(f"  Total ({metric_name}): {metric_text(root, metric)}")
    click.echo(f"  Logical: {metric_text(root, 'logical')}")
    click.echo(f"  Allocated: {metric_text(root, 'allocated')}")
    click.echo(f"  Unique on disk: {metric_text(root, 'unique')}")
    click.echo(f"  Files: {root.file_count:,}")
    click.echo(f"  Directories: {root.dir_count:,}")
    if run.worker_selection is not None:
        click.echo(
            f"  Workers: {run.worker_selection.effective_workers} "
            f"({run.worker_selection.mode})"
        )
    if run.resource_wait_seconds > 0:
        click.echo(f"  Resource wait: {run.resource_wait_seconds:.3f}s")
    if root.scan_policy is not None:
        click.echo(f"  Policy: {root.scan_policy.summary()}")
    for warning in run.capability_warnings:
        click.echo(f"  Capability warning: {warning}")
    if root.inaccessible_subtree_count:
        click.echo(
            f"  Coverage: partial ({root.inaccessible_subtree_count:,} "
            "unreadable entries/subtrees)"
        )
    if root.excluded_subtree_count or root.depth_limited_subtree_count:
        click.echo(
            f"  Scoped out: {root.excluded_subtree_count:,} policy-excluded, "
            f"{root.depth_limited_subtree_count:,} depth-limited"
        )
    if duplicate_links:
        click.echo(f"  Hardlinks deduplicated in Unique: {duplicate_links:,}")

    # Show top directories
    click.echo(f"\nTop directories by {metric_name.lower()}:")
    for child in children[:15]:
        child_value = metric_value(child, metric)
        pct = (
            child_value / total_value * 100
            if child_value is not None and total_value not in (None, 0)
            else 0.0
        )
        filled = min(50, int(pct / 2))
        bar = "█" * filled + "░" * (50 - filled)
        click.echo(
            f"  {bar} {pct:5.1f}% {metric_text(child, metric):>12s}  "
            f"{child.name}/"
        )

    if snapshot:
        outcome = _save_scan_snapshot(run)
        if outcome["saved"]:
            click.echo(f"\nSnapshot saved (id={outcome['id']})")
        elif outcome["kind"] == "unwritable":
            click.echo(
                "\nCould not save snapshot: the storage database is "
                f"unavailable or read-only. {outcome['error']}".rstrip(),
                err=True,
            )
        else:
            click.echo(
                "\nCould not save snapshot; the scan result is "
                f"still valid: {outcome['error']}",
                err=True,
            )


def _save_scan_snapshot(run) -> dict:
    """Persist a scan run and report the outcome instead of printing it.

    The human report and the JSON document both need this result but
    place it differently, so saving is kept separate from rendering.
    """
    from disktide.repositories import default_snapshot_repository
    from disktide.services.snapshots import SnapshotService

    repository = default_snapshot_repository()
    repository.connect()
    try:
        if not repository.status.writable:
            return {
                "saved": False,
                "id": None,
                "kind": "unwritable",
                "error": (repository.status.reason or "").strip(),
            }
        try:
            saved = SnapshotService(repository).save_run(run)
        except Exception as exc:
            return {
                "saved": False,
                "id": None,
                "kind": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {"saved": True, "id": saved.id, "kind": "saved", "error": None}
    finally:
        repository.close()


@cli.command()
@click.argument("selectors", nargs=-1)
@click.option(
    "--since",
    metavar="DURATION",
    help="Compare latest with the newest snapshot at least this old (e.g. 7d)",
)
@click.option(
    "--raw",
    "raw_diff",
    is_flag=True,
    help="Show an explicitly untrusted diff when policies are incompatible",
)
@click.option("--limit", default=10, show_default=True, type=click.IntRange(1, 100))
def compare(
    selectors: tuple[str, ...],
    since: str | None,
    raw_diff: bool,
    limit: int,
) -> None:
    """Compare snapshots: TARGET BASELINE [PATH].

    Examples: "compare latest previous /data" or "compare 42 41".
    """
    from datetime import timedelta

    from disktide.config import parse_duration
    from disktide.repositories import default_snapshot_repository
    from disktide.services.compare import (
        CompareService,
        SnapshotRepositoryUnavailable,
        SnapshotSelectionError,
        render_compare_result,
    )

    if since is not None:
        if len(selectors) != 1:
            raise click.UsageError("--since requires exactly one PATH argument")
        target_selector = "latest"
        baseline_selector = "previous"
        root_path = str(Path(selectors[0]).expanduser().resolve())
    else:
        if len(selectors) not in {0, 2, 3}:
            raise click.UsageError(
                "use: disktide compare TARGET BASELINE [PATH]"
            )
        target_selector = selectors[0] if selectors else "latest"
        baseline_selector = selectors[1] if selectors else "previous"
        root_path = (
            str(Path(selectors[2]).expanduser().resolve())
            if len(selectors) == 3
            else None
        )

    repository = default_snapshot_repository()
    repository.connect()
    service = CompareService(repository)
    try:
        if since is not None:
            try:
                seconds = parse_duration(since)
            except (TypeError, ValueError) as exc:
                raise click.BadParameter(
                    "expected a duration such as 7d, 12h, or 30m",
                    param_hint="--since",
                ) from exc
            result = service.compare_since(
                timedelta(seconds=seconds),
                root_path=root_path,
                raw=raw_diff,
            )
        else:
            result = service.compare(
                target_selector,
                baseline_selector,
                root_path=root_path,
                raw=raw_diff,
            )
    except (SnapshotSelectionError, SnapshotRepositoryUnavailable) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        repository.close()

    click.echo(render_compare_result(result, limit=limit))
    if result.blocked:
        raise click.exceptions.Exit(2)


def _monitor_service(*, event_mode: str | None = None):
    from disktide.config import load_config
    from disktide.repositories import default_snapshot_repository
    from disktide.services.monitor import MonitorService

    config = load_config()
    repository = default_snapshot_repository()
    repository.connect()
    service = MonitorService(
        repository,
        host_type="cli",
        soft_budget_bytes=config.monitor.database_soft_budget,
        hard_budget_bytes=config.monitor.database_hard_budget,
        event_mode=event_mode or config.monitor.event_mode,
    )
    return config, repository, service


def _duration_value(value: str, *, param_hint: str) -> int:
    from disktide.config import parse_duration

    try:
        seconds = parse_duration(value)
    except (TypeError, ValueError) as exc:
        raise click.BadParameter(str(exc), param_hint=param_hint) from exc
    if seconds <= 0:
        raise click.BadParameter(
            "duration must be greater than zero", param_hint=param_hint
        )
    return seconds


def _size_value(value: str, *, param_hint: str) -> int:
    from disktide.config import parse_size

    try:
        size = parse_size(value)
    except (TypeError, ValueError) as exc:
        raise click.BadParameter(str(exc), param_hint=param_hint) from exc
    if size < 0:
        raise click.BadParameter(
            "size must be zero or greater", param_hint=param_hint
        )
    return size


@cli.group("monitor")
def monitor_group() -> None:
    """Create and manage persistent monitor definitions."""


@monitor_group.command("list")
@click.option("--all", "include_archived", is_flag=True, help="Include archived monitors")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON")
def monitor_list(include_archived: bool, json_output: bool) -> None:
    """List monitor definitions and runtime state."""
    import json

    from disktide.config import format_duration

    _, repository, service = _monitor_service()
    try:
        dashboard = service.dashboard(include_archived=include_archived)
        if json_output:
            payload = {
                "repository_state": dashboard.repository_state,
                "session_running": dashboard.session_running,
                "monitors": [
                    {
                        "id": item.definition.id,
                        "label": item.definition.label,
                        "path": item.definition.root_path,
                        "revision": item.definition.revision,
                        "desired": item.definition.desired_state.value,
                        "activity": item.status.activity.value,
                        "health": item.status.health.value,
                        "interval_seconds": item.definition.interval_seconds,
                        "next_due_at": (
                            item.status.next_due_at.isoformat()
                            if item.status.next_due_at
                            else None
                        ),
                        "snapshot_count": item.snapshot_count,
                        "active_alert_count": item.alert_count,
                    }
                    for item in dashboard.monitors
                ],
            }
            click.echo(json.dumps(payload, sort_keys=True))
            return
        if not dashboard.monitors:
            click.echo("No monitor definitions. Use: disktide monitor add PATH")
            return
        click.echo(
            "ID  Desired   Activity   Health    Interval  Snaps  Label · Path"
        )
        for item in dashboard.monitors:
            definition = item.definition
            status = item.status
            click.echo(
                f"{definition.id:<3} {definition.desired_state.value:<9} "
                f"{status.activity.value:<10} {status.health.value:<9} "
                f"{format_duration(definition.interval_seconds):<9} "
                f"{item.snapshot_count:<6} "
                f"{definition.label} · {definition.root_path}"
            )
        if dashboard.repository_state != "writable":
            click.echo(f"Repository: {dashboard.repository_state}", err=True)
    finally:
        service.shutdown(wait=False)
        repository.close()


@monitor_group.command("add")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--label", default="", help="Display label")
@click.option("--interval", default=None, help="Start-to-start cadence (e.g. 6h)")
@click.option(
    "--metric",
    type=click.Choice(["logical", "allocated", "unique", "files"]),
    default="logical",
    show_default=True,
)
@click.option("--workers", type=int, default=None)
@click.option("--one-file-system/--cross-filesystems", default=None)
@click.option("--exclude-pseudo/--include-pseudo", default=None)
@click.option("--max-depth", type=int, default=None)
@click.option("--capture-now", is_flag=True, help="Capture the first snapshot now")
def monitor_add(
    path: Path,
    label: str,
    interval: str | None,
    metric: str,
    workers: int | None,
    one_file_system: bool | None,
    exclude_pseudo: bool | None,
    max_depth: int | None,
    capture_now: bool,
) -> None:
    """Create a persistent monitor definition."""
    from disktide.domain.metrics import MetricId
    from disktide.domain.monitor import MonitorDefinition
    from disktide.domain.policy import ScanPolicy

    config, repository, service = _monitor_service()
    try:
        seconds = (
            _duration_value(interval, param_hint="--interval")
            if interval is not None
            else config.monitor.default_interval
        )
        policy = ScanPolicy(
            one_file_system=(
                config.scan.one_file_system
                if one_file_system is None
                else one_file_system
            ),
            exclude_pseudo_filesystems=(
                config.scan.exclude_pseudo_filesystems
                if exclude_pseudo is None
                else exclude_pseudo
            ),
            max_depth=(config.scan.max_depth if max_depth is None else max_depth),
        )
        definition = MonitorDefinition(
            label=label,
            root_path=str(path),
            interval_seconds=seconds,
            metric=MetricId.parse(metric),
            policy=policy,
            workers=workers if workers is not None else config.scan.workers,
        )
        warnings = service.definition_warnings(definition)
        created = service.create_monitor(definition)
        click.echo(
            f"Created monitor {created.id}: {created.label} · {created.root_path} "
            f"(revision {created.revision})"
        )
        for warning in warnings:
            click.echo(f"Warning: {warning}", err=True)
        if capture_now:
            result = service.run_monitor_now(created.id)
            if result is not None:
                if result.snapshot is not None:
                    click.echo(f"Captured snapshot #{result.snapshot.id}")
                elif result.run is not None:
                    raise click.ClickException(
                        result.run.error_message or result.run.status.value
                    )
    except Exception as exc:
        if isinstance(exc, click.ClickException):
            raise
        raise click.ClickException(str(exc)) from exc
    finally:
        service.shutdown(wait=False)
        repository.close()


@monitor_group.command("edit")
@click.argument("identifier")
@click.option("--label", default=None)
@click.option("--path", "new_path", type=click.Path(path_type=Path), default=None)
@click.option("--interval", default=None)
@click.option(
    "--metric",
    type=click.Choice(["logical", "allocated", "unique", "files"]),
    default=None,
)
@click.option("--workers", type=int, default=None)
@click.option("--one-file-system/--cross-filesystems", default=None)
@click.option("--exclude-pseudo/--include-pseudo", default=None)
@click.option("--max-depth", type=int, default=None)
def monitor_edit(
    identifier: str,
    label: str | None,
    new_path: Path | None,
    interval: str | None,
    metric: str | None,
    workers: int | None,
    one_file_system: bool | None,
    exclude_pseudo: bool | None,
    max_depth: int | None,
) -> None:
    """Edit a definition using optimistic revision control."""
    from dataclasses import replace

    from disktide.domain.metrics import MetricId
    from disktide.domain.policy import ScanPolicy

    _, repository, service = _monitor_service()
    try:
        current = service.get_monitor(identifier)
        if current is None:
            raise click.ClickException(f"monitor '{identifier}' does not exist")
        policy = ScanPolicy(
            one_file_system=(
                current.policy.one_file_system
                if one_file_system is None
                else one_file_system
            ),
            exclude_pseudo_filesystems=(
                current.policy.exclude_pseudo_filesystems
                if exclude_pseudo is None
                else exclude_pseudo
            ),
            max_depth=(
                current.policy.max_depth if max_depth is None else max_depth
            ),
            symlink_policy=current.policy.symlink_policy,
            hardlink_policy=current.policy.hardlink_policy,
        )
        edited = replace(
            current,
            label=current.label if label is None else label,
            root_path=current.root_path if new_path is None else str(new_path),
            interval_seconds=(
                current.interval_seconds
                if interval is None
                else _duration_value(interval, param_hint="--interval")
            ),
            metric=current.metric if metric is None else MetricId.parse(metric),
            workers=current.workers if workers is None else workers,
            policy=policy,
        )
        warnings = service.definition_warnings(edited)
        updated = service.update_monitor(
            edited, expected_revision=current.revision
        )
        click.echo(
            f"Updated monitor {updated.id}: revision {current.revision} → "
            f"{updated.revision}"
        )
        for warning in warnings:
            click.echo(f"Warning: {warning}", err=True)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        service.shutdown(wait=False)
        repository.close()


def _monitor_state_command(identifier: str, state: str) -> None:
    _, repository, service = _monitor_service()
    try:
        monitor = (
            service.pause_monitor(identifier)
            if state == "paused"
            else service.resume_monitor(identifier)
        )
        click.echo(f"Monitor {monitor.id} is {monitor.desired_state.value}")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        service.shutdown(wait=False)
        repository.close()


@monitor_group.command("pause")
@click.argument("identifier")
def monitor_pause(identifier: str) -> None:
    """Pause scheduled runs without deleting history."""
    _monitor_state_command(identifier, "paused")


@monitor_group.command("resume")
@click.argument("identifier")
def monitor_resume(identifier: str) -> None:
    """Resume an enabled definition."""
    _monitor_state_command(identifier, "enabled")


@monitor_group.command("run")
@click.argument("identifier")
def monitor_run(identifier: str) -> None:
    """Run one saved monitor immediately in the foreground."""
    from disktide.metrics import METRIC_NAMES, metric_text

    _, repository, service = _monitor_service()
    try:
        result = service.run_monitor_now(identifier)
        if result is None:
            click.echo("Run queued in the active host session.")
            return
        if result.run is None or not result.run.succeeded:
            raise click.ClickException(
                result.run.error_message if result.run else "run did not start"
            )
        root = result.run.root
        metric = result.definition.metric
        metric_name = METRIC_NAMES[metric.value]
        value = metric_text(root, metric) if root else "unknown"
        snapshot = (
            f"snapshot #{result.snapshot.id}"
            if result.snapshot is not None
            else "snapshot not persisted"
        )
        click.echo(
            f"Run {result.run.run_id[:8]} {result.run.status.value}: "
            f"{metric_name}: {value}, {snapshot}, "
            f"{len(result.alerts)} alert event(s)"
        )
        if result.persistence_error:
            click.echo(f"Warning: {result.persistence_error}", err=True)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        service.shutdown(wait=False)
        repository.close()


@monitor_group.command("reconcile")
@click.argument("identifier")
def monitor_reconcile(identifier: str) -> None:
    """Run a trusted full reconciliation for one monitor."""
    _, repository, service = _monitor_service()
    try:
        result = service.reconcile_monitor(identifier)
        if result is None:
            click.echo("Full reconciliation queued in the active host session.")
            return
        if result.run is None or not result.run.succeeded:
            raise click.ClickException(
                result.run.error_message if result.run else "reconciliation did not start"
            )
        snapshot = (
            f"snapshot #{result.snapshot.id}"
            if result.snapshot is not None
            else "snapshot not persisted"
        )
        click.echo(
            f"Reconciliation {result.run.run_id[:8]} "
            f"{result.run.status.value}: {snapshot}"
        )
        if result.persistence_error:
            click.echo(f"Warning: {result.persistence_error}", err=True)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        service.shutdown(wait=False)
        repository.close()


@monitor_group.command("status")
@click.argument("identifier", required=False)
@click.option("--json", "json_output", is_flag=True)
def monitor_status(identifier: str | None, json_output: bool) -> None:
    """Show detailed status for one or all monitors."""
    import json

    import humanize

    from disktide.config import format_duration

    _, repository, service = _monitor_service()
    try:
        dashboard = service.dashboard(include_archived=True)
        items = list(dashboard.monitors)
        if identifier is not None:
            monitor = service.get_monitor(identifier)
            if monitor is None:
                raise click.ClickException(
                    f"monitor '{identifier}' does not exist"
                )
            items = [item for item in items if item.definition.id == monitor.id]
        if json_output:
            click.echo(
                json.dumps(
                    [
                        {
                            "id": item.definition.id,
                            "label": item.definition.label,
                            "path": item.definition.root_path,
                            "revision": item.definition.revision,
                            "desired": item.definition.desired_state.value,
                            "activity": item.status.activity.value,
                            "health": item.status.health.value,
                            "resource_queue_position": (
                                item.status.resource_queue_position
                            ),
                            "resource_queue_reason": (
                                item.status.resource_queue_reason
                            ),
                            "resource_active_slot": (
                                item.status.resource_active_slot
                            ),
                            "effective_workers": item.status.effective_workers,
                            "worker_policy_reason": (
                                item.status.worker_policy_reason
                            ),
                            "host_id": item.status.host_id,
                            "host_type": item.status.host_type,
                            "next_due_at": (
                                item.status.next_due_at.isoformat()
                                if item.status.next_due_at
                                else None
                            ),
                            "last_success_at": (
                                item.status.last_success_at.isoformat()
                                if item.status.last_success_at
                                else None
                            ),
                            "last_error": item.status.last_error,
                            "watch_mode": item.status.watch_mode.value,
                            "event_backend": item.status.event_backend,
                            "event_backend_status": item.status.event_backend_status,
                            "watched_root_count": item.status.watched_root_count,
                            "watch_diagnostics": (
                                item.status.watch_diagnostics.to_dict()
                            ),
                            "pending_dirty_paths": item.status.pending_dirty_paths,
                            "dirty_paths": list(item.status.dirty_paths),
                            "last_event_at": (
                                item.status.last_event_at.isoformat()
                                if item.status.last_event_at
                                else None
                            ),
                            "last_local_reconciliation_at": (
                                item.status.last_local_reconciliation_at.isoformat()
                                if item.status.last_local_reconciliation_at
                                else None
                            ),
                            "last_full_reconciliation_at": (
                                item.status.last_full_reconciliation_at.isoformat()
                                if item.status.last_full_reconciliation_at
                                else None
                            ),
                            "last_reconciliation_path": (
                                item.status.last_reconciliation_path
                            ),
                            "last_local_size": item.status.last_local_size,
                            "last_local_file_count": (
                                item.status.last_local_file_count
                            ),
                            "overflow_count": item.status.overflow_count,
                            "recovery_count": item.status.recovery_count,
                            "degraded_reason": item.status.degraded_reason,
                            "reconciliation_required": (
                                item.status.reconciliation_required
                            ),
                            "reconciliation_state": (
                                item.status.reconciliation_state.value
                            ),
                            "current_state": item.status.provisional.to_dict(),
                            "next_full_scan_at": (
                                item.status.next_due_at.isoformat()
                                if item.status.next_due_at
                                else None
                            ),
                            "snapshot_count": item.snapshot_count,
                            "database_bytes": item.database_bytes,
                        }
                        for item in items
                    ],
                    sort_keys=True,
                )
            )
            return
        if not items:
            click.echo("No monitor definitions.")
            return
        for item in items:
            definition = item.definition
            status = item.status
            click.echo(f"Monitor {definition.id}: {definition.label}")
            click.echo(f"  Path: {definition.root_path}")
            click.echo(
                f"  State: {definition.desired_state.value} · "
                f"{status.activity.value} · {status.health.value}"
            )
            click.echo(
                f"  Schedule: every {format_duration(definition.interval_seconds)}; "
                f"next {status.next_due_at.isoformat() if status.next_due_at else 'not scheduled'}"
            )
            click.echo(
                f"  Policy: {definition.metric.value}; {definition.policy.summary()}; "
                f"revision {definition.revision}"
            )
            if status.resource_queue_position:
                click.echo(
                    f"  Scan resource: queued #{status.resource_queue_position}; "
                    f"{status.resource_queue_reason or 'policy constrained'}"
                )
            else:
                click.echo(
                    f"  Scan resource: slot {status.resource_active_slot or 'none'}; "
                    f"workers {status.effective_workers or 'unknown'}"
                )
            if status.worker_policy_reason:
                click.echo(f"  Worker policy: {status.worker_policy_reason}")
            click.echo(
                f"  History: {item.snapshot_count} snapshot(s), "
                f"~{humanize.naturalsize(item.database_bytes, binary=True)}"
            )
            if status.host_id:
                click.echo(f"  Host: {status.host_type} · {status.host_id}")
            else:
                click.echo("  Host: none (enabled does not install a daemon)")
            click.echo(
                f"  Watch: {status.watch_mode.value}; "
                f"{status.event_backend or 'none'} · {status.event_backend_status}; "
                f"{status.watched_root_count} root(s)"
            )
            diagnostics = status.watch_diagnostics
            click.echo(
                f"  Watch resources: {diagnostics.descriptor_count} descriptor(s) / "
                f"{diagnostics.descriptor_limit or 'unknown'}; "
                f"instances {diagnostics.instance_limit or 'unknown'}; "
                f"queue {diagnostics.queued_event_limit or 'unknown'}"
            )
            click.echo(
                f"  Registration: {diagnostics.registration_strategy}; "
                f"{diagnostics.registration_duration_seconds or 0:.4f}s; "
                f"warning {diagnostics.warning or 'none'}; "
                f"fallback {diagnostics.fallback_reason or 'none'}"
            )
            click.echo(
                f"  Reconciliation: {status.reconciliation_state.value}; "
                f"pending {status.pending_dirty_paths}; "
                f"last event {status.last_event_at.isoformat() if status.last_event_at else 'never'}"
            )
            click.echo(
                f"  Last local/full: "
                f"{status.last_local_reconciliation_at.isoformat() if status.last_local_reconciliation_at else 'never'} / "
                f"{status.last_full_reconciliation_at.isoformat() if status.last_full_reconciliation_at else 'never'}"
            )
            current = status.provisional
            click.echo(
                f"  Current state: "
                f"{'provisional' if current.active else 'canonical'}; "
                f"base {current.base_snapshot_id or 'none'}; "
                f"confidence {current.confidence.value}; "
                f"canonical/current {current.canonical_value} / {current.current_value}"
            )
            if current.invalidation_reason:
                click.echo(
                    f"  Current invalidation: {current.invalidation_reason}"
                )
            click.echo(
                f"  Recovery: overflow {status.overflow_count}; "
                f"recovered {status.recovery_count}; "
                f"next full {status.next_due_at.isoformat() if status.next_due_at else 'not scheduled'}"
            )
            if status.degraded_reason:
                click.echo(f"  Degraded: {status.degraded_reason}", err=True)
            if status.last_error or status.blocked_reason:
                click.echo(
                    f"  Problem: {status.blocked_reason or status.last_error}",
                    err=True,
                )
    finally:
        service.shutdown(wait=False)
        repository.close()


@monitor_group.command("remove")
@click.argument("identifier")
@click.option("--yes", is_flag=True, help="Archive without prompting")
def monitor_remove(identifier: str, yes: bool) -> None:
    """Archive a definition while retaining history, pins, and events."""
    _, repository, service = _monitor_service()
    try:
        monitor = service.get_monitor(identifier)
        if monitor is None:
            raise click.ClickException(f"monitor '{identifier}' does not exist")
        if not yes and not click.confirm(
            f"Archive monitor {monitor.id} and keep its history?"
        ):
            return
        archived = service.archive_monitor(monitor.id)
        click.echo(f"Archived monitor {archived.id}; history retained.")
    finally:
        service.shutdown(wait=False)
        repository.close()


@monitor_group.command("pin")
@click.argument("snapshot_id", type=int)
@click.option("--label", default="")
def monitor_pin(snapshot_id: int, label: str) -> None:
    """Protect a snapshot from retention."""
    _, repository, service = _monitor_service()
    try:
        service.pin_snapshot(snapshot_id, label=label)
        click.echo(f"Pinned snapshot #{snapshot_id}")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        repository.close()


@monitor_group.command("unpin")
@click.argument("snapshot_id", type=int)
def monitor_unpin(snapshot_id: int) -> None:
    """Allow a snapshot to be pruned again."""
    _, repository, service = _monitor_service()
    try:
        service.unpin_snapshot(snapshot_id)
        click.echo(f"Unpinned snapshot #{snapshot_id}")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        repository.close()


@monitor_group.command("retention")
@click.argument("identifier")
@click.option("--apply", "apply_changes", is_flag=True, help="Run maintenance")
def monitor_retention(identifier: str, apply_changes: bool) -> None:
    """Preview or run retention maintenance."""
    _, repository, service = _monitor_service()
    try:
        if apply_changes:
            result = service.run_retention_now(identifier)
            click.echo(
                f"Retention {result.status}: kept {result.kept}, "
                f"pruned {result.pruned}, rollups {result.rolled_up}"
            )
        else:
            preview = service.retention_preview(identifier)
            click.echo(
                f"Keep {len(preview.keep_ids)}, prune {len(preview.prune_ids)}, "
                f"rollups {len(preview.rollups)}, pinned {len(preview.pinned_ids)}"
            )
            if preview.prune_ids:
                click.echo("  Prune IDs: " + ", ".join(map(str, preview.prune_ids)))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        repository.close()


@cli.group("alerts")
def alerts_group() -> None:
    """Manage monitor alert rules and inspect events."""


@alerts_group.command("list")
@click.argument("monitor_id", required=False, type=int)
@click.option("--events", "show_events", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def alerts_list(
    monitor_id: int | None, show_events: bool, json_output: bool
) -> None:
    """List alert rules or recent events."""
    import json

    _, repository, service = _monitor_service()
    try:
        if show_events:
            events = service.list_alert_events(monitor_id)
            if json_output:
                click.echo(
                    json.dumps(
                        [
                            {
                                "id": event.id,
                                "rule_id": event.rule_id,
                                "monitor_id": event.monitor_id,
                                "kind": event.kind.value,
                                "severity": event.severity.value,
                                "message": event.message,
                                "suppressed": event.suppressed,
                                "confidence": event.confidence,
                                "triggered_at": event.triggered_at.isoformat(),
                                "old_snapshot_id": event.old_snapshot_id,
                                "new_snapshot_id": event.new_snapshot_id,
                            }
                            for event in events
                        ],
                        sort_keys=True,
                    )
                )
                return
            for event in events:
                suffix = (
                    f" · suppressed: {event.suppression_reason}"
                    if event.suppressed
                    else ""
                )
                click.echo(
                    f"{event.id}: {event.severity.value} {event.kind.value} · "
                    f"{event.message}{suffix}"
                )
            if not events:
                click.echo("No alert events.")
            return
        rules = service.list_alert_rules(monitor_id)
        if json_output:
            click.echo(
                json.dumps(
                    [
                        {
                            "id": rule.id,
                            "monitor_id": rule.monitor_id,
                            "path": rule.path,
                            "kind": rule.kind.value,
                            "metric": rule.metric.value,
                            "threshold": rule.threshold,
                            "window_seconds": rule.window_seconds,
                            "severity": rule.severity.value,
                            "cooldown_seconds": rule.cooldown_seconds,
                            "enabled": rule.enabled,
                        }
                        for rule in rules
                    ],
                    sort_keys=True,
                )
            )
            return
        for rule in rules:
            state = "enabled" if rule.enabled else "disabled"
            click.echo(
                f"{rule.id}: monitor {rule.monitor_id} · {rule.kind.value} · "
                f"{rule.path} · threshold {rule.threshold:g} · {state}"
            )
        if not rules:
            click.echo("No alert rules.")
    finally:
        repository.close()


@alerts_group.command("add")
@click.argument("monitor_id", type=int)
@click.argument("path", required=False)
@click.option("--size", "absolute_size", default=None, help="Absolute size threshold")
@click.option("--growth", default=None, help="Absolute growth threshold")
@click.option("--percent", type=float, default=None, help="Percentage growth threshold")
@click.option("--free-space", default=None, help="Trigger at or below free bytes")
@click.option("--inode-free", type=float, default=None, help="Trigger at or below free inodes")
@click.option("--new-large", default=None, help="New item threshold")
@click.option("--window", default=None, help="Growth comparison window")
@click.option(
    "--metric",
    type=click.Choice(["logical", "allocated", "unique", "files"]),
    default="logical",
)
@click.option(
    "--severity",
    type=click.Choice(["info", "warning", "critical"]),
    default="warning",
)
@click.option("--cooldown", default="0s")
def alerts_add(
    monitor_id: int,
    path: str | None,
    absolute_size: str | None,
    growth: str | None,
    percent: float | None,
    free_space: str | None,
    inode_free: float | None,
    new_large: str | None,
    window: str | None,
    metric: str,
    severity: str,
    cooldown: str,
) -> None:
    """Add one threshold rule to a monitor."""
    from disktide.domain.alerts import AlertKind, AlertRule, AlertSeverity
    from disktide.domain.metrics import MetricId

    _, repository, service = _monitor_service()
    try:
        monitor = service.get_monitor(monitor_id)
        if monitor is None:
            raise click.ClickException(f"monitor {monitor_id} does not exist")
        choices = [
            absolute_size is not None,
            growth is not None,
            percent is not None,
            free_space is not None,
            inode_free is not None,
            new_large is not None,
        ]
        if sum(choices) != 1:
            raise click.UsageError(
                "choose exactly one of --size/--growth/--percent/"
                "--free-space/--inode-free/--new-large"
            )
        if absolute_size is not None:
            kind = AlertKind.ABSOLUTE_SIZE
            threshold = _size_value(absolute_size, param_hint="--size")
        elif growth is not None:
            kind = AlertKind.ABSOLUTE_GROWTH
            threshold = _size_value(growth, param_hint="--growth")
        elif percent is not None:
            kind = AlertKind.PERCENTAGE_GROWTH
            threshold = percent
        elif free_space is not None:
            kind = AlertKind.FREE_SPACE
            threshold = _size_value(free_space, param_hint="--free-space")
        elif inode_free is not None:
            kind = AlertKind.INODE_FREE
            threshold = inode_free
        else:
            kind = AlertKind.NEW_LARGE_ITEM
            threshold = _size_value(new_large or "0", param_hint="--new-large")
        rule = service.create_alert_rule(
            AlertRule(
                monitor_id=monitor_id,
                path=path or monitor.root_path,
                kind=kind,
                metric=MetricId.parse(metric),
                threshold=float(threshold),
                window_seconds=(
                    _duration_value(window, param_hint="--window")
                    if window
                    else None
                ),
                severity=AlertSeverity(severity),
                cooldown_seconds=(
                    0
                    if cooldown == "0s"
                    else _duration_value(cooldown, param_hint="--cooldown")
                ),
            )
        )
        click.echo(f"Created alert rule {rule.id} for monitor {monitor_id}")
    except (click.ClickException, click.UsageError):
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        repository.close()


@alerts_group.command("edit")
@click.argument("rule_id", type=int)
@click.option("--path", default=None)
@click.option("--threshold", default=None)
@click.option("--window", default=None)
@click.option(
    "--metric",
    type=click.Choice(["logical", "allocated", "unique", "files"]),
    default=None,
)
@click.option(
    "--severity",
    type=click.Choice(["info", "warning", "critical"]),
    default=None,
)
@click.option("--cooldown", default=None)
def alerts_edit(
    rule_id: int,
    path: str | None,
    threshold: str | None,
    window: str | None,
    metric: str | None,
    severity: str | None,
    cooldown: str | None,
) -> None:
    """Edit common alert rule fields."""
    from dataclasses import replace

    from disktide.domain.alerts import AlertKind, AlertSeverity
    from disktide.domain.metrics import MetricId

    _, repository, service = _monitor_service()
    try:
        rule = repository.get_alert_rule(rule_id)
        if rule is None:
            raise click.ClickException(f"alert rule {rule_id} does not exist")
        parsed_threshold = rule.threshold
        if threshold is not None:
            parsed_threshold = (
                float(threshold)
                if rule.kind
                in {AlertKind.PERCENTAGE_GROWTH, AlertKind.INODE_FREE}
                else float(_size_value(threshold, param_hint="--threshold"))
            )
        updated = service.update_alert_rule(
            replace(
                rule,
                path=rule.path if path is None else path,
                threshold=parsed_threshold,
                window_seconds=(
                    rule.window_seconds
                    if window is None
                    else _duration_value(window, param_hint="--window")
                ),
                metric=rule.metric if metric is None else MetricId.parse(metric),
                severity=(
                    rule.severity
                    if severity is None
                    else AlertSeverity(severity)
                ),
                cooldown_seconds=(
                    rule.cooldown_seconds
                    if cooldown is None
                    else 0
                    if cooldown == "0s"
                    else _duration_value(cooldown, param_hint="--cooldown")
                ),
            )
        )
        click.echo(f"Updated alert rule {updated.id}")
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        repository.close()


def _alerts_enabled_command(rule_id: int, enabled: bool) -> None:
    _, repository, service = _monitor_service()
    try:
        service.set_alert_rule_enabled(rule_id, enabled)
        click.echo(f"Alert rule {rule_id} {'enabled' if enabled else 'disabled'}")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        repository.close()


@alerts_group.command("enable")
@click.argument("rule_id", type=int)
def alerts_enable(rule_id: int) -> None:
    """Enable an alert rule."""
    _alerts_enabled_command(rule_id, True)


@alerts_group.command("disable")
@click.argument("rule_id", type=int)
def alerts_disable(rule_id: int) -> None:
    """Disable an alert rule."""
    _alerts_enabled_command(rule_id, False)


@alerts_group.command("remove")
@click.argument("rule_id", type=int)
def alerts_remove(rule_id: int) -> None:
    """Delete an alert rule while retaining prior events."""
    _, repository, service = _monitor_service()
    try:
        service.remove_alert_rule(rule_id)
        click.echo(f"Removed alert rule {rule_id}")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        repository.close()


@alerts_group.command("check")
@click.argument("monitor_id", required=False, type=int)
@click.option("--json", "json_output", is_flag=True)
def alerts_check(monitor_id: int | None, json_output: bool) -> None:
    """Evaluate rules against latest history without persisting duplicates.

    Exit 0 means no trigger, 2 means an active trigger, and 3 means only
    suppressed/low-confidence triggers.
    """
    import json

    _, repository, service = _monitor_service()
    try:
        monitors = (
            [service.get_monitor(monitor_id)]
            if monitor_id is not None
            else service.list_monitors()
        )
        if any(item is None for item in monitors):
            raise click.ClickException(f"monitor {monitor_id} does not exist")
        events = []
        for monitor in monitors:
            assert monitor is not None and monitor.id is not None
            events.extend(service.check_alerts(monitor.id))
        if json_output:
            click.echo(
                json.dumps(
                    [
                        {
                            "rule_id": event.rule_id,
                            "monitor_id": event.monitor_id,
                            "message": event.message,
                            "suppressed": event.suppressed,
                            "suppression_reason": event.suppression_reason,
                            "confidence": event.confidence,
                            "old_snapshot_id": event.old_snapshot_id,
                            "new_snapshot_id": event.new_snapshot_id,
                        }
                        for event in events
                    ],
                    sort_keys=True,
                )
            )
        else:
            for event in events:
                prefix = "SUPPRESSED" if event.suppressed else "TRIGGERED"
                click.echo(f"{prefix}: rule {event.rule_id}: {event.message}")
            if not events:
                click.echo("No alerts triggered.")
        if any(not event.suppressed for event in events):
            raise click.exceptions.Exit(2)
        if events:
            raise click.exceptions.Exit(3)
    finally:
        repository.close()


@cli.command()
@click.argument("path", required=False, type=click.Path(path_type=Path))
@click.option("--monitor", "monitor_identifier", default=None, help="Host one saved monitor")
@click.option("--all", "watch_all", is_flag=True, help="Host all enabled monitors")
@click.option("--interval", "-i", default=None, help="Transient scan interval")
@click.option("--max-time", "-t", default=None, help="Maximum foreground host time")
@click.option("--workers", "-w", type=int, default=None, help="Transient scan workers")
@click.option(
    "--events",
    is_flag=True,
    help="Require optional native filesystem-event acceleration",
)
@click.option(
    "--periodic-only",
    is_flag=True,
    help="Disable filesystem events for this foreground host",
)
@click.pass_context
def watch(
    ctx,
    path: Path | None,
    monitor_identifier: str | None,
    watch_all: bool,
    interval: str | None,
    max_time: str | None,
    workers: int | None,
    events: bool,
    periodic_only: bool,
) -> None:
    """Run the shared monitor host in the foreground.

    "watch PATH" is transient and does not create a saved definition.
    "watch --monitor ID" and "watch --all" host persisted definitions.
    """
    import humanize

    from disktide.config import format_duration
    from disktide.domain.metrics import MetricId
    from disktide.domain.monitor import MonitorDefinition
    from disktide.domain.policy import ScanPolicy
    from disktide.services.monitor import MonitorEventKind

    if watch_all and monitor_identifier is not None:
        raise click.UsageError("--all and --monitor are mutually exclusive")
    if events and periodic_only:
        raise click.UsageError("--events and --periodic-only are mutually exclusive")
    if (watch_all or monitor_identifier is not None) and path is not None:
        raise click.UsageError("PATH cannot be combined with --all or --monitor")
    if (watch_all or monitor_identifier is not None) and (
        interval is not None or workers is not None
    ):
        raise click.UsageError(
            "saved monitors use their persisted interval/workers; edit the definition instead"
        )

    requested_mode = "events" if events else "periodic" if periodic_only else None
    config, repository, service = _monitor_service(event_mode=requested_mode)
    max_seconds = (
        _duration_value(max_time, param_hint="--max-time")
        if max_time is not None
        else config.monitor.max_watch_time
    )

    def report(event) -> None:
        stamp = event.timestamp.astimezone().strftime("%H:%M:%S")
        if event.kind is MonitorEventKind.RUN_STARTED:
            click.echo(f"[{stamp}] {event.message}")
        elif event.kind is MonitorEventKind.SNAPSHOT_SAVED:
            click.echo(f"[{stamp}] {event.message}")
        elif event.kind is MonitorEventKind.ALERT_TRIGGERED:
            click.echo(f"[{stamp}] ALERT: {event.message}", err=True)
        elif event.kind is MonitorEventKind.RETENTION_COMPLETED:
            click.echo(f"[{stamp}] {event.message}")
        elif event.kind is MonitorEventKind.RUN_FINISHED:
            click.echo(f"[{stamp}] {event.message}")
        elif event.kind is MonitorEventKind.WATCH_CHANGED:
            click.echo(f"[{stamp}] watch: {event.message}")
        elif event.kind is MonitorEventKind.RECONCILIATION_FINISHED:
            click.echo(f"[{stamp}] reconcile: {event.message}")

    service.subscribe(report)
    try:
        if events:
            service.set_event_mode("events")
        backend = service.event_backend_info()
        if service.event_mode.value == "periodic":
            click.echo("Watch mode: periodic-only")
        elif backend.available:
            click.echo(
                f"Watch mode: event-assisted ({backend.name} {backend.version or 'unknown'})"
            )
        else:
            click.echo(
                f"Watch mode: periodic fallback ({backend.reason})",
                err=True,
            )
        if watch_all or monitor_identifier is not None:
            monitor_ids: set[int] | None = None
            if monitor_identifier is not None:
                monitor = service.get_monitor(monitor_identifier)
                if monitor is None or monitor.id is None:
                    raise click.ClickException(
                        f"monitor '{monitor_identifier}' does not exist"
                    )
                monitor_ids = {monitor.id}
                click.echo(
                    f"Hosting monitor {monitor.id} ({monitor.root_path}) in the foreground"
                )
            else:
                enabled = [
                    item
                    for item in service.list_monitors()
                    if item.desired_state.value == "enabled"
                ]
                if not enabled:
                    raise click.ClickException("no enabled monitors to host")
                click.echo(f"Hosting {len(enabled)} enabled monitor(s) in the foreground")
            click.echo("No daemon is installed; Ctrl+C stops this host.")
            service.start_session(monitor_ids, host_type="cli")
            started = time.monotonic()
            while service.session_running:
                if max_seconds is not None and time.monotonic() - started >= max_seconds:
                    click.echo("Max watch time reached. Stopping.")
                    break
                time.sleep(0.2)
            return

        transient_path = str((path or Path(".")).expanduser().resolve())
        seconds = (
            _duration_value(interval, param_hint="--interval")
            if interval is not None
            else config.monitor.default_interval
        )
        definition = MonitorDefinition(
            label=Path(transient_path).name or transient_path,
            root_path=transient_path,
            interval_seconds=seconds,
            metric=MetricId.LOGICAL,
            policy=ScanPolicy(
                one_file_system=config.scan.one_file_system,
                exclude_pseudo_filesystems=(
                    config.scan.exclude_pseudo_filesystems
                ),
                max_depth=config.scan.max_depth,
            ),
            workers=workers if workers is not None else config.scan.workers,
        )
        click.echo(
            f"Watching {transient_path} every {format_duration(seconds)} "
            "(transient; Ctrl+C to stop)"
        )
        if max_seconds is not None:
            click.echo(f"  Max watch time: {format_duration(max_seconds)}")
        results = service.watch_transient(
            definition,
            max_seconds=max_seconds,
        )
        for result in results[-1:]:
            if result.run and result.run.root:
                click.echo(
                    f"Last run: {humanize.naturalsize(result.run.root.size, binary=True)}, "
                    f"{result.run.root.file_count:,} files"
                )
    except KeyboardInterrupt:
        # 128 + SIGINT. A caller has to be able to tell "the user pressed
        # Ctrl-C" apart from "the host reached --max-time and returned".
        click.echo("\nStopped watching.", err=True)
        ctx.exit(130)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        service.shutdown(wait=True)
        repository.close()


_HELP_FLAGS = frozenset({"--help", "-h"})


class _DefaultCommandGroup(click.Group):
    """A group whose bare arguments fall through to one default subcommand.

    ``disktide cleanup /srv`` has to keep working now that ``cleanup`` is
    a group, so a leading token that is not a subcommand name is routed
    to the default subcommand instead. Flag options written ahead of a
    subcommand -- ``cleanup --json history`` -- are hoisted along with
    it.

    The scan stops at the first bare token, so an option that takes a
    value has to follow its subcommand (``cleanup history --by
    category``, not ``cleanup --by category history``). Arity is a
    property of the subcommand's own parameters, which are not known
    until the subcommand has been picked, and guessing wrong would route
    an option's value as a command name.
    """

    def __init__(self, *args, default_command: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.default_command = default_command

    def parse_args(self, ctx, args):
        return super().parse_args(ctx, self._route(list(args)))

    def _route(self, args: list[str]) -> list[str]:
        for index, argument in enumerate(args):
            if argument == "--":
                break
            if argument in _HELP_FLAGS:
                # Group-level help, not the default subcommand's help.
                return args
            if argument.startswith("-"):
                continue
            if argument in self.commands:
                return [argument, *args[:index], *args[index + 1 :]]
            break
        return [self.default_command, *args]


def _cleanup_catalog():
    """Load the config and rule catalog every cleanup subcommand starts from."""
    from disktide.cleanup.rules import get_rule_catalog
    from disktide.config import cleanup_rule_directory, load_config

    config = load_config()
    rule_directory = cleanup_rule_directory()
    catalog = get_rule_catalog(
        disabled_packs=config.cleanup.disabled_rule_packs,
        user_directory=rule_directory,
    )
    return config, rule_directory, catalog


@contextmanager
def _cleanup_session(config, rule_directory):
    """Open a repository-backed CleanupService and always close the handle."""
    from disktide.cleanup.actions import QuarantineExecutor
    from disktide.cleanup.rules import get_rule_by_name
    from disktide.repositories import default_snapshot_repository
    from disktide.services.cleanup import (
        CleanupConfirmationRequired,
        CleanupError,
        CleanupService,
    )

    repository = default_snapshot_repository()
    repository.connect()
    service = CleanupService(
        repository,
        quarantine=QuarantineExecutor(
            retention_days=config.cleanup.quarantine_retention_days,
            max_bytes=config.cleanup.quarantine_max_bytes,
        ),
        rule_provider=lambda name: get_rule_by_name(
            name,
            disabled_packs=config.cleanup.disabled_rule_packs,
            user_directory=rule_directory,
        ),
    )
    try:
        yield repository, service
    except (CleanupConfirmationRequired, CleanupError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        repository.close()


@cli.group("cleanup", cls=_DefaultCommandGroup, default_command="plan")
def cleanup() -> None:
    """Create, apply, inspect, or undo a persistent CleanupPlan.

    \b
    "disktide cleanup PATH" is shorthand for "disktide cleanup plan PATH",
    and PATH itself defaults to the current directory.
    """


@cleanup.command("plan")
@click.argument("path", required=False)
@click.option("--workers", "-w", type=int, default=None, help="Number of scan threads")
@click.option(
    "--apply", "apply_safe", is_flag=True, help="Move targets to Trash/quarantine"
)
@click.option("--plan", "plan_id", help="Load an existing cleanup plan")
@click.option("--permanent", is_flag=True, help="Use the separate permanent-delete flow")
@click.option("--confirm", help="Exact permanent-delete confirmation token")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def cleanup_plan(
    path: str | None,
    workers: int | None,
    apply_safe: bool,
    plan_id: str | None,
    permanent: bool,
    confirm: str | None,
    json_output: bool,
) -> None:
    """Scan PATH for cleanup targets, then preview or apply the plan.

    Without --apply or --permanent this only previews; nothing on disk
    is touched.
    """
    import json

    import humanize

    from disktide.cleanup.detector import detect_targets
    from disktide.domain.cleanup import CleanupActionKind, cleanup_plan_to_dict
    from disktide.domain.metrics import MetricId
    from disktide.domain.policy import ScanPolicy
    from disktide.domain.scan import ScanRequest, ScanRequestError, ScanStatus
    from disktide.services.scan import ScanService

    if apply_safe and permanent:
        raise click.UsageError("--apply and --permanent are mutually exclusive")

    config, rule_directory, catalog = _cleanup_catalog()
    with _cleanup_session(config, rule_directory) as (_, service):
        if plan_id:
            plan = service.get_plan(plan_id)
            if path is not None:
                requested_root = str(Path(path).expanduser().resolve())
                if requested_root != plan.scan_root:
                    raise click.UsageError(
                        f"PATH does not match plan root {plan.scan_root}"
                    )
        else:
            path_object = Path(path or ".").expanduser()
            if not path_object.exists():
                raise click.BadParameter("Path does not exist", param_hint="PATH")
            if not path_object.is_dir():
                raise click.BadParameter("Not a directory", param_hint="PATH")
            scan_root = str(path_object.resolve())
            # Status, not result: a --json caller must get JSON alone on
            # stdout, and a human redirecting the report does not want it
            # either.
            click.echo(f"Scanning {scan_root} for cleanup targets...", err=True)
            request = ScanRequest(
                path=scan_root,
                metric=MetricId.LOGICAL,
                policy=ScanPolicy(
                    one_file_system=config.scan.one_file_system,
                    exclude_pseudo_filesystems=(
                        config.scan.exclude_pseudo_filesystems
                    ),
                    max_depth=config.scan.max_depth,
                ),
                workers=workers,
                source="cleanup",
            )
            try:
                scan_run = ScanService().scan(request)
            except ScanRequestError as exc:
                raise click.BadParameter(str(exc), param_hint="PATH") from exc
            if scan_run.status is ScanStatus.CANCELLED:
                raise click.ClickException("Cleanup scan was cancelled")
            if scan_run.status is ScanStatus.FAILED or scan_run.root is None:
                detail = scan_run.error_message or "unknown scan failure"
                raise click.ClickException(f"Cleanup scan failed: {detail}")
            targets = detect_targets(scan_run.root, rules=list(catalog.rules))
            for issue in catalog.issues:
                click.echo(
                    f"Rule pack isolated ({issue.path}): {issue.error}",
                    err=True,
                )
            if not targets:
                if json_output:
                    click.echo(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "scan_root": scan_root,
                                "targets": 0,
                                "plan": None,
                            },
                            sort_keys=True,
                        )
                    )
                else:
                    click.echo("No cleanup targets found.")
                return
            requested = (
                CleanupActionKind.PERMANENT
                if permanent
                else (
                    CleanupActionKind.TRASH
                    if config.cleanup.prefer_trash
                    else CleanupActionKind.QUARANTINE
                )
                if apply_safe
                else CleanupActionKind.PREVIEW
            )
            plan = service.create_plan(
                scan_root,
                targets,
                requested_action=requested,
                scan_run_id=scan_run.run_id,
                provenance="cli-scan",
            )

        if json_output and not apply_safe and not permanent:
            click.echo(json.dumps(cleanup_plan_to_dict(plan)))
            return
        if not json_output:
            _render_cleanup_plan(plan, humanize)
        if not apply_safe and not permanent:
            if not json_output:
                click.echo(
                    f"Preview only; no filesystem changes made. Apply with: "
                    f"disktide cleanup --plan {plan.id} --apply"
                )
            return

        if permanent:
            token = service.permanent_confirmation(plan.id)
            if confirm is None:
                click.echo(
                    "PERMANENT DELETE bypasses Trash and cannot be undone.", err=True
                )
                confirm = click.prompt(
                    f'Type "{token}" to continue',
                    default="",
                    show_default=False,
                )
            action_kind = CleanupActionKind.PERMANENT
        else:
            action_kind = (
                CleanupActionKind.TRASH
                if config.cleanup.prefer_trash
                else CleanupActionKind.QUARANTINE
            )
        result = service.execute(plan, action=action_kind, confirmation=confirm)
        if json_output:
            click.echo(json.dumps(cleanup_plan_to_dict(result.plan)))
            return
        click.echo(
            f"Plan {result.plan.id}: {result.plan.status.value}; "
            f"{result.plan.succeeded_count} succeeded, "
            f"{result.plan.skipped_count} skipped, "
            f"{result.plan.failed_count} failed."
        )
        click.echo(
            "  Estimated: "
            f"{humanize.naturalsize(result.plan.estimated_reclaimable_bytes, binary=True)}"
        )
        click.echo(
            "  Validated: "
            f"{humanize.naturalsize(result.plan.validated_reclaimable_bytes, binary=True)}"
        )
        click.echo(
            "  Actual reclaimed: "
            f"{humanize.naturalsize(result.plan.actual_reclaimed_bytes, binary=True)}"
        )
        if any(action.undo_available for action in result.plan.actions):
            click.echo(f"  Undo: disktide cleanup undo {result.plan.id}")
        for item in result.plan.actions:
            if item.error:
                click.echo(f"  {item.path}: {item.error}", err=True)


@cleanup.command("history")
@click.option(
    "--by",
    "history_group",
    type=click.Choice(["category", "pack", "path"]),
    help="Group cleanup savings history",
)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def cleanup_history(history_group: str | None, json_output: bool) -> None:
    """List recorded cleanup plans, or grouped savings with --by."""
    import json

    import humanize

    from disktide.domain.cleanup import cleanup_plan_to_dict

    config, rule_directory, _ = _cleanup_catalog()
    with _cleanup_session(config, rule_directory) as (_, service):
        if history_group is not None:
            summaries = service.savings_history(group_by=history_group)
            if json_output:
                click.echo(
                    json.dumps([item.to_dict() for item in summaries], sort_keys=True)
                )
            elif not summaries:
                click.echo("No cleanup savings history recorded.")
            else:
                click.echo(f"Cleanup savings history grouped by {history_group}:")
                for item in summaries:
                    click.echo(
                        f"{item.key:<28} {item.action_count:>3} actions  "
                        f"estimated {humanize.naturalsize(item.estimated_bytes, binary=True):>9}  "
                        f"isolated {humanize.naturalsize(item.isolated_bytes, binary=True):>9}  "
                        f"purged {humanize.naturalsize(item.purged_bytes, binary=True):>9}  "
                        f"actual {humanize.naturalsize(item.actual_reclaimed_bytes, binary=True):>9}  "
                        f"undone {humanize.naturalsize(item.undone_bytes, binary=True):>9}"
                    )
            return

        plans = service.history(limit=100)
        if json_output:
            click.echo(json.dumps([cleanup_plan_to_dict(item) for item in plans]))
        elif not plans:
            click.echo("No cleanup plans recorded.")
        else:
            for item in plans:
                click.echo(
                    f"{item.id}  {item.status.value:<9}  "
                    f"{len(item.active_actions):>3} targets  "
                    f"estimated {humanize.naturalsize(item.estimated_reclaimable_bytes, binary=True)}  "
                    f"actual {humanize.naturalsize(item.actual_reclaimed_bytes, binary=True)}  "
                    f"{item.scan_root}"
                )


@cleanup.command("undo")
@click.argument("plan_or_action_id")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def cleanup_undo(plan_or_action_id: str, json_output: bool) -> None:
    """Restore quarantined content from a plan or a single action."""
    import json

    from disktide.domain.cleanup import CleanupExecutionStatus, cleanup_plan_to_dict

    config, rule_directory, _ = _cleanup_catalog()
    with _cleanup_session(config, rule_directory) as (_, service):
        result = service.undo(plan_or_action_id)
        if json_output:
            click.echo(json.dumps(cleanup_plan_to_dict(result.plan)))
            return
        restored = sum(
            action.execution_status is CleanupExecutionStatus.UNDONE
            for action in result.plan.actions
        )
        click.echo(
            f"Undo {result.plan.id}: restored {restored} item(s); "
            f"status {result.plan.status.value}."
        )
        for action in result.plan.actions:
            if action.error:
                click.echo(f"  skipped {action.path}: {action.error}", err=True)


@cleanup.command("purge")
@click.argument("plan_or_action_id")
@click.option("--confirm", help="Exact permanent-delete confirmation token")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def cleanup_purge(
    plan_or_action_id: str, confirm: str | None, json_output: bool
) -> None:
    """Permanently remove quarantined content. This cannot be undone."""
    import json

    import humanize

    from disktide.domain.cleanup import cleanup_plan_to_dict

    config, rule_directory, _ = _cleanup_catalog()
    with _cleanup_session(config, rule_directory) as (repository, service):
        plan = repository.get_cleanup_plan(plan_or_action_id)
        if plan is None:
            plan = repository.get_cleanup_plan_for_action(plan_or_action_id)
        if plan is None:
            raise click.ClickException(
                f"cleanup plan or action '{plan_or_action_id}' does not exist"
            )
        token = service.purge_confirmation(plan.id)
        if confirm is None:
            click.echo(
                "PURGE permanently removes quarantined content and cannot be undone.",
                err=True,
            )
            confirm = click.prompt(
                f'Type "{token}" to continue',
                default="",
                show_default=False,
            )
        result = service.purge(plan_or_action_id, confirmation=confirm)
        if json_output:
            click.echo(json.dumps(cleanup_plan_to_dict(result.plan)))
        else:
            click.echo(
                f"Purge {result.plan.id}: {result.plan.purged_count} purged; "
                f"actual reclaimed "
                f"{humanize.naturalsize(result.plan.actual_reclaimed_bytes, binary=True)}."
            )


@cleanup.group("rules")
def cleanup_rules() -> None:
    """Inspect, validate, and toggle cleanup rule packs."""


@cleanup_rules.command("list")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def cleanup_rules_list(json_output: bool) -> None:
    """List rule packs and any packs isolated by a load error."""
    import json

    _, _, catalog = _cleanup_catalog()
    payload = {
        "schema_version": 1,
        "packs": [
            {
                "name": pack.name,
                "version": pack.version,
                "schema_version": pack.schema_version,
                "description": pack.description,
                "source": pack.source,
                "enabled": pack.enabled,
                "rule_count": len(pack.rules),
                "rules": [rule.name for rule in pack.rules],
            }
            for pack in catalog.packs
        ],
        "issues": [
            {
                "pack": issue.pack_name,
                "source": issue.source,
                "path": issue.path,
                "error": issue.error,
            }
            for issue in catalog.issues
        ],
    }
    if json_output:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    for pack in catalog.packs:
        state = "enabled" if pack.enabled else "disabled"
        click.echo(
            f"{pack.name:<12} {state:<8} v{pack.version:<8} "
            f"{pack.source:<7} {len(pack.rules):>2} rule(s)"
        )
    for issue in catalog.issues:
        click.echo(f"INVALID      isolated {issue.path}: {issue.error}", err=True)


@cleanup_rules.command("validate")
@click.argument("path")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def cleanup_rules_validate(path: str, json_output: bool) -> None:
    """Check that the rule pack at PATH loads and conforms to the schema."""
    import json

    from disktide.extensions.cleanup_rules import (
        RulePackValidationError,
        validate_rule_pack,
    )

    try:
        pack = validate_rule_pack(path)
    except RulePackValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {
        "name": pack.name,
        "version": pack.version,
        "schema_version": pack.schema_version,
        "source": pack.source,
        "enabled": pack.enabled,
        "rule_count": len(pack.rules),
    }
    if json_output:
        click.echo(json.dumps(payload, sort_keys=True))
    else:
        click.echo(
            f"VALID {pack.name} v{pack.version} · schema "
            f"{pack.schema_version} · {len(pack.rules)} rule(s)"
        )


def _toggle_cleanup_rule_pack(pack_name: str, *, enable: bool) -> None:
    """Flip a pack's enabled state in the persisted config."""
    from disktide.config import save_config

    config, _, catalog = _cleanup_catalog()
    known_pack = catalog.get_pack(pack_name)
    if known_pack is None and not any(
        issue.pack_name == pack_name for issue in catalog.issues
    ):
        raise click.ClickException(f"unknown cleanup rule pack: {pack_name}")
    disabled = set(config.cleanup.disabled_rule_packs)
    if enable:
        disabled.discard(pack_name)
    else:
        disabled.add(pack_name)
    config.cleanup.disabled_rule_packs = sorted(disabled)
    save_config(config)
    click.echo(
        f"Cleanup rule pack '{pack_name}' {'enabled' if enable else 'disabled'}."
    )


@cleanup_rules.command("enable")
@click.argument("pack")
def cleanup_rules_enable(pack: str) -> None:
    """Re-enable a previously disabled rule pack."""
    _toggle_cleanup_rule_pack(pack, enable=True)


@cleanup_rules.command("disable")
@click.argument("pack")
def cleanup_rules_disable(pack: str) -> None:
    """Stop a rule pack from contributing cleanup targets."""
    _toggle_cleanup_rule_pack(pack, enable=False)


@cleanup.group("quarantine")
def cleanup_quarantine() -> None:
    """Inspect the quarantine ledger that backs undo and purge."""


def _run_quarantine_audit(root: str, *, rebuild: bool, json_output: bool) -> None:
    """Audit a quarantine root, optionally rebuilding its ledger first."""
    import json

    import humanize

    from disktide.cleanup.actions import CleanupExecutionError, QuarantineExecutor

    config, _, _ = _cleanup_catalog()
    executor = QuarantineExecutor(
        retention_days=config.cleanup.quarantine_retention_days,
        max_bytes=config.cleanup.quarantine_max_bytes,
    )
    try:
        status = executor.audit(root, rebuild=rebuild)
    except CleanupExecutionError as exc:
        raise click.ClickException(str(exc)) from exc
    if json_output:
        click.echo(json.dumps(status.to_dict(), sort_keys=True))
        return
    verdict = "MATCH" if status.ledger_matches else "MISMATCH"
    click.echo(f"Quarantine ledger {verdict}: {status.root}")
    click.echo(
        f"  Ledger: {status.ledger_items} items / "
        f"{humanize.naturalsize(status.ledger_bytes, binary=True)}"
    )
    click.echo(
        f"  Manifests: {status.manifest_items} items / "
        f"{humanize.naturalsize(status.manifest_bytes, binary=True)}"
    )
    click.echo(f"  Pending reservations: {status.pending_items}")
    if status.rebuilt:
        click.echo("  Ledger rebuilt from recovery manifests.")
    for recovery in status.recoveries:
        click.echo(f"  RECOVERED: {recovery}")
    for issue in status.issues:
        click.echo(f"  ISSUE: {issue}")


@cleanup_quarantine.command("audit")
@click.argument("root")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def cleanup_quarantine_audit(root: str, json_output: bool) -> None:
    """Compare the ledger at ROOT against the on-disk recovery manifests."""
    _run_quarantine_audit(root, rebuild=False, json_output=json_output)


@cleanup_quarantine.command("rebuild")
@click.argument("root")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def cleanup_quarantine_rebuild(root: str, json_output: bool) -> None:
    """Rebuild the ledger at ROOT from its recovery manifests, then audit."""
    _run_quarantine_audit(root, rebuild=True, json_output=json_output)


def _render_cleanup_plan(plan, humanize_module) -> None:
    """Render a stable human-readable CleanupPlan summary."""
    click.echo(f"\nCleanup plan {plan.id} v{plan.version}")
    click.echo(f"  Root: {plan.scan_root}")
    click.echo(f"  Status: {plan.status.value}")
    click.echo(f"  Planned action: {plan.requested_action.value}")
    click.echo(
        "  Estimated reclaimable: "
        f"{humanize_module.naturalsize(plan.estimated_reclaimable_bytes, binary=True)}"
    )
    click.echo(
        f"  Targets: {len(plan.active_actions)} active, "
        f"{len(plan.actions) - len(plan.active_actions)} subsumed"
    )
    click.echo(f"  Confidence: {plan.confidence:.0%}")
    for item in plan.actions:
        marker = "subsumed" if item.subsumed_by else item.validation_status.value
        action_policy = (
            "detection-only"
            if item.detection_only
            else item.rule_action_policy.value
        )
        click.echo(
            f"    [{item.risk.value}/{marker}] {item.path}\n"
            f"      {item.rule_pack}@{item.rule_pack_version} · "
            f"{item.category} · age {item.age_days:.1f}d · score {item.score:.1f} · "
            f"confidence {item.confidence:.0%} · {action_policy}\n"
            f"      {item.reason}; "
            f"{humanize_module.naturalsize(item.estimated_reclaimable_bytes, binary=True)}"
            + (f"; rebuild: {item.rebuild_hint}" if item.rebuild_hint else "")
        )


if __name__ == "__main__":
    cli()

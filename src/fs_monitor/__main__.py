"""CLI entry point (click-based)."""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import click

from fs_monitor import APP_NAME, __version__


@click.group(invoke_without_command=True)
@click.option("--max-depth", "-d", type=int, default=None, help="Maximum scan depth")
@click.option("--workers", "-w", type=int, default=None, help="Number of scan threads")
@click.version_option(version=__version__)
@click.pass_context
def cli(ctx, max_depth: int | None, workers: int | None):
    """Interactive terminal disk usage explorer.

    Launch TUI: fsmonitor
    Subcommands: scan, watch, cleanup
    """
    ctx.ensure_object(dict)
    ctx.obj["max_depth"] = max_depth
    ctx.obj["workers"] = workers

    if ctx.invoked_subcommand is None:
        from fs_monitor.app import FSMonitorApp
        from fs_monitor.config import load_config

        config = load_config()
        if max_depth is not None:
            config.scan.max_depth = max_depth
        if workers is not None:
            config.scan.workers = workers

        app = FSMonitorApp(show_welcome=True, config=config)
        app.run(mouse=False)

        # Print "Exiting..." first so the user sees feedback, then run
        # the necessary cleanup (cancel scan + close SQLite), print the
        # goodbye, and hard-exit. See `_force_teardown` for why we skip
        # Python's natural shutdown sequence on the way out.
        click.echo("Exiting...", nl=True)
        sys.stdout.flush()
        _force_teardown(app)
        click.echo(f"{APP_NAME} closed. Goodbye!")
        sys.stdout.flush()
        # Bypass Python's interpreter teardown: gc of the in-memory
        # FSNode tree + atexit + module cleanup adds tens of seconds on
        # a multi-million-file scan, all spent freeing memory the kernel
        # is about to reclaim anyway. All cleanup that matters for
        # correctness (cancel + DB close) has already run above, and
        # the goodbye text was flushed via sys.stdout.flush() right
        # before this block.
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
        if getattr(app, "_explorer", None) is not None:
            app._explorer.cancel_active_scan()
    except Exception:
        pass
    db = getattr(app, "_db", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


@cli.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--snapshot", "-s", is_flag=True, help="Save snapshot to database")
@click.option("--max-depth", "-d", type=int, default=None, help="Maximum scan depth")
@click.option("--workers", "-w", type=int, default=None, help="Number of scan threads")
def scan(path: str, snapshot: bool, max_depth: int | None, workers: int | None):
    """Scan a directory and display results."""
    from fs_monitor.scanner.engine import ScanEngine
    from fs_monitor.scanner.progress import ScanProgress
    import humanize

    path = str(Path(path).resolve())
    click.echo(f"Scanning {path}...")

    start = time.monotonic()

    def on_progress(p: ScanProgress):
        click.echo(
            f"\r  {p.dirs_scanned:,} dirs, {p.files_scanned:,} files, "
            f"{humanize.naturalsize(p.total_size, binary=True)}",
            nl=False,
        )

    engine = ScanEngine(
        workers=workers,
        progress_callback=on_progress,
        max_depth=max_depth,
    )
    root = engine.scan(path)
    elapsed = time.monotonic() - start

    click.echo()
    click.echo(f"\nScan complete in {elapsed:.1f}s")
    click.echo(f"  Total size: {humanize.naturalsize(root.size, binary=True)}")
    click.echo(f"  Files: {root.file_count:,}")
    click.echo(f"  Directories: {root.dir_count:,}")

    # Show top directories
    click.echo("\nTop directories:")
    for child in root.sorted_children[:15]:
        if child.is_dir:
            pct = child.size_percent(root.size)
            size_str = humanize.naturalsize(child.size, binary=True)
            bar = "█" * int(pct / 2) + "░" * (50 - int(pct / 2))
            click.echo(f"  {bar} {pct:5.1f}% {size_str:>10s}  {child.name}/")

    if snapshot:
        from fs_monitor.models.snapshot import Snapshot
        from fs_monitor.storage.database import Database

        db = Database()
        db.connect()
        if db.degraded:
            db.close()
            click.echo(
                "\nCould not save snapshot: the storage database is "
                "unavailable or not writable.",
                err=True,
            )
        else:
            snap = Snapshot(
                root_path=path,
                timestamp=datetime.now(),
                total_size=root.size,
                file_count=root.file_count,
                dir_count=root.dir_count,
                scan_duration=elapsed,
            )
            snap_id = db.save_snapshot(snap, root)
            db.close()
            click.echo(f"\nSnapshot saved (id={snap_id})")


@cli.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--interval", "-i", default=None, help="Scan interval (e.g., 1h, 6h, 1d)")
@click.option("--max-time", "-t", default=None, help="Max watch time (e.g., 2h, 1d)")
@click.option("--workers", "-w", type=int, default=None, help="Number of scan threads")
def watch(path: str, interval: str | None, max_time: str | None, workers: int | None):
    """Watch a directory for changes with periodic scanning."""
    import asyncio
    from fs_monitor.config import load_config, parse_duration, format_duration

    path = str(Path(path).resolve())
    config = load_config()

    # Parse interval (CLI flag > config > default 6h)
    if interval is not None:
        seconds = parse_duration(interval)
    else:
        seconds = config.monitor.default_interval

    # Parse max watch time (CLI flag > config > unlimited)
    max_seconds: int | None = None
    if max_time is not None:
        max_seconds = parse_duration(max_time)
    elif config.monitor.max_watch_time is not None:
        max_seconds = config.monitor.max_watch_time

    click.echo(f"Watching {path} every {format_duration(seconds)} (Ctrl+C to stop)")
    if max_seconds is not None:
        click.echo(f"  Max watch time: {format_duration(max_seconds)}")

    async def run():
        from fs_monitor.scanner.engine import ScanEngine
        from fs_monitor.models.snapshot import Snapshot
        from fs_monitor.storage.database import Database
        import humanize

        db = Database()
        db.connect()
        if db.degraded:
            click.echo(
                "Warning: the storage database is unavailable or not "
                "writable; snapshots will not be persisted.",
                err=True,
            )
        watch_start = time.monotonic()

        try:
            while True:
                start = time.monotonic()
                engine = ScanEngine(workers=workers, scan_path=path)
                root = engine.scan(path)
                elapsed = time.monotonic() - start

                if db.degraded:
                    snapshot_status = "not persisted"
                    pruned = 0
                else:
                    snap = Snapshot(
                        root_path=path,
                        timestamp=datetime.now(),
                        total_size=root.size,
                        file_count=root.file_count,
                        dir_count=root.dir_count,
                        scan_duration=elapsed,
                    )
                    snap_id = db.save_snapshot(snap, root)
                    snapshot_status = f"snapshot #{snap_id}"
                    pruned = db.prune_snapshots(
                        path, config.monitor.snapshot_retention
                    )

                status = (
                    f"[{datetime.now():%H:%M:%S}] Scan complete: "
                    f"{humanize.naturalsize(root.size, binary=True)}, "
                    f"{root.file_count:,} files "
                    f"({snapshot_status}, {elapsed:.1f}s)"
                )
                if pruned:
                    status += f", pruned {pruned} old snapshot(s)"
                click.echo(status)

                if max_seconds is not None:
                    total_elapsed = time.monotonic() - watch_start
                    if total_elapsed + seconds >= max_seconds:
                        click.echo("Max watch time reached. Stopping.")
                        return

                await asyncio.sleep(seconds)
        finally:
            db.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        click.echo("\nStopped watching.")


@cli.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--workers", "-w", type=int, default=None, help="Number of scan threads")
def cleanup(path: str, workers: int | None):
    """Detect candidates and permanently delete them after confirmation."""
    from fs_monitor.scanner.engine import ScanEngine
    from fs_monitor.cleanup.detector import detect_targets, group_by_category, total_savings
    from fs_monitor.cleanup.actions import delete_targets
    import humanize

    path = str(Path(path).resolve())
    click.echo(f"Scanning {path} for cleanup targets...")

    engine = ScanEngine(workers=workers, scan_path=path)
    root = engine.scan(path)

    targets = detect_targets(root)
    if not targets:
        click.echo("No cleanup targets found.")
        return

    groups = group_by_category(targets)
    total = total_savings(targets)

    click.echo(f"\nFound {len(targets)} targets ({humanize.naturalsize(total, binary=True)}):\n")

    for category, items in sorted(groups.items()):
        cat_size = sum(t.size for t in items)
        click.echo(f"  {category} ({len(items)} items, {humanize.naturalsize(cat_size, binary=True)}):")
        for item in items[:5]:
            click.echo(f"    {item.path} ({humanize.naturalsize(item.size, binary=True)})")
        if len(items) > 5:
            click.echo(f"    ... and {len(items) - 5} more")
        click.echo()

    if click.confirm("Permanently delete all targets?"):
        result = delete_targets(targets)
        click.echo(
            f"\nDeleted {len(result.successful)} items, "
            f"freed {humanize.naturalsize(result.total_freed, binary=True)}"
        )
        if result.failed:
            click.echo(f"Failed: {len(result.failed)} items")
            for f in result.failed:
                click.echo(f"  {f.path}: {f.error}")


if __name__ == "__main__":
    cli()

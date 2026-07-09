# Changelog

## Unreleased

Since `e174a6a` (v0.1.6 release).

**Explorer**

- **fix** The tree's quantitative sort now follows the active bar metric. With the bar toggled to file count (`t`), directories were still ordered by bytes; the default sort now orders by the active metric (count or size), matching what the treemap and sunburst already do, and toggling the metric re-sorts the tree. Name and modified-time sorts remain metric-independent. The sort indicator reflects this (`Sort: Files` when sorting by the count metric).
- **fix** The tree cursor is no longer frozen after a rescan. The tree is `display: none` during a scan, so it loses focus; nothing restored it afterward, leaving arrow keys controlling whatever grabbed focus while it was hidden. The explorer now refocuses the tree once the scan completes, and a reload pins the cursor to the root so it's always live and visible.

**FS Overview**

- **feat** The single **Speed** column is replaced by a **Storage** column of facet badges that break the conflated speed heuristic into orthogonal physical properties. The first badge is the *medium* (`Flash` / `HDD` / `RAM` / `Network` / `?`); it is followed by any detected *transforms* — `RAID` (md device), `Encrypted` (dm-crypt, via a `CRYPT-*` sysfs uuid), `CoW` (btrfs/zfs), and `Compressed` (a `compress=` mount option) — each materially shifting the performance profile yet invisible to the flash-vs-HDD axis. Transforms are detected once at load with no extra I/O. The detail modal gains a matching **Attributes** row. The medium badge already encodes locality, so a network mount shows `Network` with no redundant `Local` badge. The classifier (`sysinfo.storage_class`) is now the single source of truth shared by every view, and newly recognises RAM-backed filesystems (`tmpfs`/`ramfs`), which previously fell through to "Unknown".
- **feat** New opt-in **Benchmark mount** action (`b`): an on-demand throughput probe of the highlighted mount, since the storage-class badges are only heuristics. Because it writes a temporary file, it is gated behind a confirm prompt (press `b` again, muscle-memory friendly) so a stray keystroke never starts disk I/O. The probe writes ≤256 MiB (capped at 25% of free space, time-bounded), `fsync`s, drops the page cache via `posix_fadvise(DONTNEED)`, and reads back cold — reporting buffered-write and approximate cold-read bandwidth without root. The result is recorded per mount for the life of the screen and shown as a **Measured** row when that row is reopened. New module: `scanner/benchmark.py`.
- **fix** "Used" and "Usage %" no longer over-report by the root-reserved block count. The screen computed `used = total - f_bavail`, which folds the unprivileged-user reservation (the default ~5% on ext4) into used space — on a typical root filesystem this nearly doubled the reported usage versus `df` (e.g. 11.4% shown vs 7% real, a 48 GB overstatement). It now follows `df` semantics exactly: `used = (f_blocks - f_bfree) * f_frsize`, `free = f_bavail * f_frsize`, and `usage_pct = used / (used + free)`. The reclaimed gap is exposed as a new **Reserved** row in the per-filesystem detail modal.
- **fix** The aggregate summary (Total / Used / proportional bar) no longer double-counts capacity for devices mounted at more than one path. Bind mounts and btrfs subvolumes report the full pool size from each `statvfs`, so summing raw mount entries inflated the totals; the summary now de-duplicates by backing device first. The per-mount table still lists every mountpoint.
- **fix** A stale NFS/CIFS mount can no longer wedge the loader on the spinner forever. `statvfs` on network filesystems now runs under a 3 s watchdog (local filesystems, which never block, are stat'd directly) and a timed-out mount is skipped.
- **fix** Non-ASCII mountpoints are no longer mojibaked. `/proc/mounts` octal escapes (`\040` etc.) were decoded with `encode('utf-8').decode('unicode_escape')`, which reinterpreted UTF-8 bytes as Latin-1 (`/mnt/café` → `/mnt/cafÃ©`). A targeted octal-only unescape (`unescape_mount_path`) replaces it, shared across `sysinfo` and the overview screen.
- **fix** The usage bar clamps over-100% values (the `*` over-quota case) instead of emitting an over-length bar with a negative empty count.
- **fix** Refreshing (`r`) is now `exclusive`, so mashing the key can't stack overlapping loader threads.

**Block Devices**

- **feat** The FS Overview gains a second **Block Devices** panel that enumerates the whole block layer via `lsblk -J -b`, surfacing storage that `/proc/mounts` + `statvfs` fundamentally cannot see: unmounted-but-formatted filesystems, unformatted partitions, and raw disks with no partition table. Each disk is shown with its partition tree and a colour-coded status (`● mounted`, `○ not mounted`, `○ unformatted`, `○ raw — no partition table`); the panel header reports idle-disk count and unused capacity. Row-select opens a per-device detail modal (model, media type, partition list). The panel hides itself when `lsblk` is unavailable. New module: `scanner/blockdev.py`.

**Scanner**

- **fix** `sysinfo._find_block_device` now strips the partition suffix correctly for `mmcblk0p1` → `mmcblk0` (previously `mmcblk0p`) and keeps `loop0` / `dm-0` whole, so the Speed (HDD/SSD) column resolves on eMMC/SD and loop devices.
- **fix** `_find_block_device` no longer mangles software-RAID devices. The trailing-digit strip turned `md0` into `md`, so the sysfs rotational lookup missed and the medium showed "Unknown"; the md number is part of the device identity and is now kept (`md0`, `md127`, partitionable `md_d0`), while md partitions (`md0p1`) strip to the parent like nvme.
- **feat** New facet helpers in `sysinfo`: `classify_medium` (flash/hdd/ram/network/unknown), `detect_transforms` (cheap, no-I/O detection of RAID / dm-crypt / CoW / compression), and `facet_labels` (ordered medium-then-transform badge pairs). These back the FS-Overview Storage column and are consumable individually by downstream tuning.

**Settings**

- **fix** The System Information **Storage** line now uses the shared `storage_class` classifier, so it agrees with the FS-Overview screen. It previously appended only `(HDD)`/`(SSD)` from the rotational bit, ignoring network and RAM-backed mounts entirely.

**Storage**

- **fix** A full disk no longer crashes launch. The SQLite database is set up eagerly at startup, and its setup requires writes — `os.makedirs` for the data dir (first launch), then `PRAGMA journal_mode=WAL` (the `-wal`/`-shm` sidecars) and the schema migrations. On a full disk any of these raised `OSError`/`sqlite3.OperationalError`, killing the TUI at the exact moment a user needs it to find what's filling the disk. `Database.connect()` now degrades to an in-memory database when the on-disk location is unwritable: the app launches and the explorer/cleanup stay fully usable, only snapshots and history aren't persisted for that session. The post-migration `VACUUM` is now non-fatal for the same reason (it needs temporary space the migration itself already committed without).
- **fix** The degraded (full-disk) state is now surfaced to the user rather than failing silently. The TUI shows a one-time `Running without persistence` warning on any launch path (welcome screen or straight into the explorer), the Settings screen warns when it can't save (`Settings not saved`), and `scan --snapshot` / `watch` report that snapshots can't be persisted instead of silently writing to a throwaway in-memory database.

## v0.1.6

Since `407136d` (v0.1.5 release).

**Explorer**

- **feat** The active visualization tab (Sunburst or Treemap) now renders live as the scan runs, instead of waiting until the end and popping in at once. The engine builds a fresh shallow-copy `FSNode` snapshot after the top-level scandir and again after each top-level subdir worker finishes; the explorer applies that snapshot to whichever viz tab is currently visible. The progress overlay sits in the left tree-panel (where the empty file tree would be) so the entire right side is free for the chart to form, with the dirs / files / size counters ticking next to it.
- **fix** The progress overlay no longer floats in the middle of the screen during a scan; it now occupies the tree-panel (left 40%), which is empty until the scan finishes anyway. This gives the live viz the full right 60% with no occlusion AND gives the progress info a much roomier display area. On scan completion the panel swaps back to the file tree. (Earlier v0.1.6 iterations first wrapped the overlay in a full-screen Container with `background: transparent` — Textual treats transparent-bg widgets as owning their cells, so the live viz rendered correctly into its strip and was immediately painted over — then floated it with `position: absolute` to claim only its own 60x12 footprint, which fixed the occlusion but still ate the middle of the viz panel.)
- **fix** Worker-thread exceptions no longer brick the UI. If `engine.scan()` raised for any reason (e.g. the scan dir was deleted between welcome-screen validation and the worker starting), `_run_scan` never reached `_on_scan_complete` and `_scan_in_progress` stayed True forever, silently gating every subsequent `r` / `u` / `i` / drill-into. The worker now wraps `engine.scan` in try/except and routes failures through a new `_on_scan_failed` handler that resets the same state and surfaces the error via `app.notify`.
- **fix** Previous scan's chart no longer bleeds into a new scan. The viz tabs were only being cleared (`set_node(None)`) when `live_render` was active; with the position-absolute overlay fix above, leftover charts were plainly visible around the centered overlay for the full duration of any non-live rescan. Now every `_start_scan` clears both viz views and the Details panel regardless of mode.
- **fix** `_apply_tree_snapshot` ignores snapshots that arrive when no scan is in progress. Textual's `call_from_thread` preserves FIFO ordering so this can't happen in practice today, but only the runtime contract guarantees it; the defensive guard prevents a stale partial snapshot from overwriting the final tree if that ordering ever changes.
- **style** The viz-switch key hints (`1` / `2` / `3`) now live on the tab labels themselves (`Sunburst [1]` / `Treemap [2]` / `Details [3]`) instead of eating a wide cell in the footer. The bindings still work, they just aren't surfaced in the footer summary anymore.
- **fix** The progress overlay's bar now fills the panel width instead of stopping at ~60%. Textual's `ProgressBar` defaults to `width: auto` and its inner `Bar` defaults to `width: 32`, so the bar was using 32 of the overlay's 54 content cells. Both are now `width: 1fr` inside the overlay.
- **fix** Live frames render at reduced depth (`max_depth=2` instead of 4 for the sunburst and 3 for the treemap), restored on completion. The outer rings of a sunburst are exactly the ones that re-tile every time a subtree's size lands, so dropping them mid-scan removes both the visual jitter and most of the per-frame braille-fill cost.
- **fix** Drill-into is gated while a scan is in flight (`u` go-up, `i` go-into, `r` rescan, and click-to-drill on the tree all return early). The live snapshot's per-subtree aggregates are honest, but the root totals visible on a partial render are not, and we don't want the Details panel showing numbers that contradict themselves a second later.

**Settings**

- **feat** New **Live scan rendering** setting (`ui.live_scan_render`): `auto` (default), `on`, or `off`. `auto` enables live rendering on roomy terminals (>= 80x24) with enough cores (>= 4), and silently stays out of the way on cramped web shells or small VMs where the per-frame redraw cost would compete with the scan. Persisted to `~/.config/fsmonitor-cli/config.toml` only when the user picks `on` or `off`; the default does not pollute the file.

**Scanner**

- **feat** `ScanEngine` gains an optional `tree_callback: Callable[[FSNode], None]` and a `tree_callback_interval` (default 0.25 s). When set, the callback fires once with a top-level-only snapshot before workers start, then at most once per interval as top-level subdir futures resolve, and unconditionally one final time at the end. The first non-forced emit after a force bypasses the throttle so the first ring slice appears the moment a top-level subdir lands, instead of after a full interval of blank viz. Aggregate math is factored into a single `_roll_up` helper used by both the live snapshot path and the final root assembly, so they cannot drift.

**Shutdown**

- **fix** Quit no longer hangs the shell for tens of seconds after a large scan. The previous `_force_teardown` joined worker threads (up to 8s), then `del app` and `gc.collect()` triggered a refcount cascade through the held FSNode tree (5-15s on a 4M-node tree), then `Goodbye!` printed, then Python's natural interpreter shutdown ran atexit handlers + module cleanup + a final cycle GC pass (another 20-30s on the same tree). On a 12 TB / 4M file scan that added up to ~40 seconds of nothing-visible-happening between pressing `q` and getting the shell prompt back. The teardown now does only what matters for correctness — cancel the scan and close the SQLite handle — then `os._exit(0)`. Worker threads are doing read-only FS scans, so killing them mid-syscall is safe; the kernel reclaims the entire FSNode tree's memory in microseconds vs the seconds Python takes to do it with refcounts and finalizers. Measured on a tiny tree: from second-`q` press to process exit is now 214 ms.

**Docs**

- **docs** Architecture guide gains a short subsection on live-scan rendering (the snapshot path, the depth damping, the auto-gate) under Visualization.
- **docs** New blog post [docs/blogs/2026-05-24-reading-the-sunburst.md](blogs/2026-05-24-reading-the-sunburst.md): a short field guide to the chart in the right panel, with the academic lineage of sunburst / radial space-filling visualizations (Shneiderman 1991 treemap, Stasko and Zhang 2000 sunburst), the disk-usage tool lineage that brought it to desktops (Filelight, DaisyDisk, Baobab), how the radial form maps to a Linux filesystem, and a key-by-key reading guide for our implementation.

## v0.1.5

Since `6cb0459` (v0.1.4 release).

**Scanner**

- **fix** Symlink scanning is now one syscall per entry. The walker previously did three (`entry.stat` for the link, `os.readlink` for the target text, `os.stat` to follow the link); on slow shared storage with many symlinks each extra syscall is a server round-trip, blowing up scan times by an order of magnitude. `os.readlink` and the target-following `os.stat` are now deferred to `classify_symlink`, called on demand by the Details panel and the `i` action; the result is cached on the node. Measured on one cluster home (6.1M files, 100 GB on NFS): 2386 s on the pre-fix build, 195 s after, a ~12x recovery and ~20% under the v0.1.3 baseline.
- **feat** On top of that, the engine eagerly classifies the first 100 symlinks it sees at the scan root so a typical `fsmon ~` still shows `→ /target` arrows for the handful of links at home root. The cap is hard, so it cannot regress the case where the scan root itself contains hundreds of thousands of symlinks.
- **fix** Directory scans no longer recurse forever on a filesystem cycle. A bind mount or other setup that makes a directory reappear inside itself used to send the scan into an infinite loop; the walker now tracks each directory's `(st_dev, st_ino)` identity along the path from the scan root and stops when a directory is its own ancestor, marking it `(loop)` in the tree and the Details panel. Symlink loops were already prevented; this closes the non-symlink case.

**Progress**

- **fix** Scan progress now updates continuously during a deep scan instead of freezing while a single big subtree is being walked. The walker calls a per-directory tick callback as each directory finishes; the engine folds those into shared live counters and forwards them through the existing `ProgressThrottle` (100 ms coalesce). The earlier behavior only updated when an entire top-level subtree completed, so a scan with one dominant child sat with frozen `Dirs / Files / Size` counters for minutes.
- **fix** The scanning overlay's progress bar is now indeterminate (a pulsing animation with no percentage). The earlier percentage was `completed_top_level_dirs / total_top_level_dirs`, unrelated to actual work, and routinely parked at 97% while one large subtree finished. Without a pre-count pass there is no honest progress fraction; an indeterminate bar plus the now-continuously-moving counters is the honest feedback.

**Tooling**

- **feat** Two diagnostic scripts under `tool/`. `tool/bench_scan.py` is a version-agnostic one-shot that prints `wall-time / dirs / files / size / rate` for any scan path and any prior release (uses only `ScanEngine().scan()`). `tool/diag_scan.py` adds a 1-second heartbeat (with stall detection), a per-directory hotspot table, and an optional `--profile` pass that wraps the scan in `cProfile` and dumps the top callees. Both accept `--workers N` to isolate threading from the picture. These are how the symlink regression above was diagnosed; see [docs/blogs/2026-05-23-symlink-scan-nfs-speedup.md](blogs/2026-05-23-symlink-scan-nfs-speedup.md).
- **add** `tests/test_tools.py` smoke-tests both scripts as subprocesses against a real `tmp_path` and asserts the documented output shape. The scripts also degrade gracefully if the scanner's internal API changes (defensive `getattr` on every field, the diag's monkey-patch wrapped in `try/except` with a stderr notice on failure), so they keep working across releases.

**Docs**

- **docs** Architecture guide, filesystem-call reference, and contributing guide updated for the deferred-symlink-classification behavior and the two new diagnostic scripts.
- **docs** New blog post under `docs/blogs/` walking through the symlink-scan regression hunt end to end: cProfile-driven diagnosis, the fix, measured 12x recovery, and the small UX trade-offs the lazy scheme accepts.

## v0.1.4

Since `96dc76b` (v0.1.3 release).

**Explorer**

- **add** `y` copies the highlighted item's absolute path to the system clipboard. It uses the terminal's OSC 52 escape, so it also works over SSH and in web-based shells that have no local clipboard tool.
- **add** `t` toggles whether proportions are measured by total size (default) or file count. The size-tree bar, sunburst arc angles, treemap rectangle areas, and the Details panel "Top Items" list all follow the active metric. Both `size` and `file_count` are aggregated bottom-up during the scan, so toggling is a re-layout of in-memory data with no extra filesystem access.
- **add** The indicator line above the tree now shows the active sort order and bar metric (e.g. `Sort: Size  Bar: Files`).
- **add** New `fs_monitor/metrics.py` centralises the size/file-count metric vocabulary (`metric_value`, `metric_text`); `compute_layout` and `compute_sunburst` take a `metric` argument so the tree, visualizations, and Details panel stay in sync.

**Symlinks**

- **add** Symbolic links are now first-class in the explorer: the tree shows them as `name → target`, the Details panel reports the target and its type, and broken or file targets are marked. They are never recursed into, so a symlinked directory's bytes never inflate the parent (only the link's own size counts).
- **add** `i` on a symlink whose target is a directory resolves the real path and rescans from there, so linked folders are navigable without the scan traversing the link.
- **fix** A symlink directly under the scan root is now counted as one file, consistent with symlinks deeper in the tree (the engine and walker had diverged).
- **fix** Removed the `follow_symlinks` config option and its Settings toggle, which were never wired into the scanner.

**Docs**

- **docs** User guide, README, and architecture document the `y` / `t` hotkeys and the size/file-count metric.
- **docs** Corrected the stale clone directory (`util_fs_monitor`) in the contributing guide and the viz-tab order in the README key-binding table.

## v0.1.3

Since `1163b24` (Update changelog for quota, viz reorder, and config save fixes).

**Partial Inaccessibility (#14)**

- **add** Walker/engine now track per-node `inaccessible_count` (direct unreadable entries) and `inaccessible_subtree_count` (bottom-up aggregate); previously, per-entry `OSError`s in `os.scandir` and `entry.stat()` were silently dropped, hiding the fact that reported sizes under-counted.
- **add** Bottom-up aggregates `denied_dir_subtree_count` / `partial_dir_subtree_count` on `FSNode` so subtree-wide UI summaries are O(1) instead of re-walking on every status update.
- **add** Size tree marks affected directories with a yellow `◐ N hidden` glyph (full denial keeps its red `⚠`); ancestors with hidden state below get a dim `◐`.
- **add** Info panel gains an **Access** row (Full / Partial / Unreadable) and prefixes the size with `≥` when the subtree is partially scanned.
- **add** Sunburst arc labels and treemap rect labels append `◐` (partial) or `⚠` (unreadable) when there is room.
- **add** Breadcrumb shows the access state of the current directory (`◐` partial, `⚠` unreadable) so it's visible in every viz tab without crowding the sunburst.
- **add** Explorer header subtitle summarises subtree counts: `⚠ N unreadable, ◐ M partial`.

**Web-shell Compatibility**

- **add** UI setting **Safe rendering (web shells)** — when enabled, swaps the block-drawing proportional bar (`█`/`░`) for ASCII (`#`/space) and `⚠`/`◐` accessibility glyphs for `[!]`/`[~]`. Default off; web-based shells (OSC OnDemand, JupyterHub terminals) often lack the Unicode glyphs and render them as runaway horizontal lines.
- **add** All accessibility glyphs (`⚠`, `◐`) now carry a U+FE0E (variation selector-15) suffix forcing *text* presentation, so terminals that would otherwise render them as 2-cell emoji keep them at width 1.

**Navigation / Safety**

- **add** Rescan (`r`) and quit (`q`) now prompt y/n via a confirmation modal to prevent accidental keystrokes from kicking off a long scan or exiting the app. Pressing the originating key (`r` or `q`) twice also confirms — muscle-memory friendly.
- **add** Quarter-screen jumps in the explorer tree: Ctrl+U / Ctrl+D move the cursor by `max(1, height // 4)` rows.
- **add** Settings: Up/Down arrows now move focus between fields (mirrors Tab / Shift+Tab); guarded so an open `Select` dropdown still gets its own arrow-key handling.

**Quit / Shutdown**

- **fix** Pressing `q` on a large scan no longer hangs the shell for many seconds. The walker now propagates the engine's cancel event through every recursive call, so worker threads bail out within ~10 ms instead of running the in-flight subtree to completion. Measured on a 2500-file synthetic tree: cancel-to-finish dropped from "until done" to ~11 ms.
- **fix** Shutdown ordering: cancel active scan → join non-daemon threads (8s cap, warning on expiry) → close the SQLite handle → drop refs → `gc.collect()` → print final goodbye. Previously "Goodbye!" printed before interpreter teardown, so the shell hung on a silent pause; now the goodbye genuinely lands last.
- **fix** Removed the in-TUI "Finishing background tasks..." toast — the screen tore down within the same tick, so the toast was never visible. The terminal-side `Exiting...` / `Goodbye!` messages do the job.

**UI Fixes**

- **fix** Scan progress overlay top border was clipped by the `TabbedContent` panel above it. `ExplorerScreen` now declares an `overlay` layer and centers the overlay in a full-screen invisible container so it floats cleanly above the panels.
- **fix** Explorer subtitle text "denied" replaced with "unreadable" — `node.error` catches any `OSError` (EACCES, EIO, ESTALE, …), not only permission denials.
- **fix** The default `color_theme` is now unified at `warm`. The `UIConfig` dataclass default was already `warm`, but `load_config()` fell back to `default` when a config file omitted the key, so fresh installs and configs predating the `color_theme` setting diverged.

**Code Quality**

- **add** `ConfirmModal(ModalScreen[bool])` in `widgets/confirm_modal.py` — generic y/n dialog with optional `confirm_keys` for caller-supplied muscle-memory keys. Reusable for any future "are you sure?" gate.
- **add** `Database.path` public property; `ExplorerScreen.cancel_active_scan()` public method. Replaces several `self._db._path` / `self._explorer._engine` private-attr reaches.
- **add** New `fs_monitor.glyphs` (`DENIED`, `PARTIAL`, `VS15`, `visible_width()`) and `fs_monitor.rendering` (runtime safe-rendering toggle + glyph/bar getters) modules; every render site consults the helpers so the safe-rendering switch takes effect on the next paint.
- **add** Test pytest-asyncio-free pattern: `asyncio.run(go())` wrapping `app.run_test()`, avoiding the missing-plugin issue that breaks `test_welcome.py`.

**Docs**

- **docs** User guide: documented `q` y/n prompt, `r` y/n prompt, Ctrl+U / Ctrl+D quarter-screen jumps, Settings Up/Down field nav.

## v0.1.2

Since `42fb096` (Update docs for welcome screen and hostname-aware paths).

**Welcome Screen**

- **add** Redesigned welcome screen: single path input with suggestion cycling via Up/Down arrows.
- **add** Suggestion sources: current directory, saved default, last visited, and recent snapshot paths from the database.
- **add** Suggestions are deduplicated; active suggestion highlighted in yellow with an arrow indicator.
- **fix** "Save as default path" was not persisted when the chosen path matched the last-visited path.
- **fix (qol)** "Save as default path" checkbox is now unchecked by default.
- **fix (qol)** Improved welcome screen wording: TUI keys and CLI subcommands are clearly separated.

**FS Overview**

- **add** FS Overview screen (`f`): all real mounted filesystems with mount point, FS type, speed tier (Fast SSD / Medium HDD / Slow Network), size columns, and a visual usage bar.
- **add** FS Overview detail popup: press Enter on any row to see device path, inode stats, block size, and full mount options.
- **add** Proportional summary bar at the top of FS Overview, coloured by speed tier.
- **add** User quota column: displays personal quota usage and limit when the `quota` command is available (supports local and NFS quotas). Detail popup shows soft/hard limits with usage bar.
- **fix** Detail popup no longer resets the table cursor to the first row on close.

**Cleanup**

- **add** `show_cleanup` config flag (`[ui]`); toggle in Settings -> Cleanup Settings.
- **fix (qol)** Cleanup mode is hidden by default; pressing `c` shows a notification instead of silently switching to a destructive screen.

**Quit / Shutdown**

- **add** Exit notification: pressing `q` shows "Finishing background tasks..." toast inside the TUI while background threads wind down; "fsmonitor-cli closed. Goodbye!" is printed to the terminal once fully exited.
- **fix (qol)** Pressing `q` now cancels any in-progress scan, reducing shutdown delay.
- **fix** Config changes (e.g. default visualization) were not persisted when quitting with `q`; config is now saved on quit.

**Fixes**

- **fix** `watch` and `cleanup` CLI commands were passing `/` to `ScanEngine` for FS-type detection instead of the actual scan path -- worker count now adapts correctly per path (e.g. NFS cap applies).
- **fix** Settings screen always showed storage/FS type for `/`; now uses the active scan path.

**Visualization**

- **add** Two new file-type categories: "model" (ML checkpoints: pt, pth, ckpt, onnx, safetensors, pb, tflite, savedmodel) and "log" (log, out, err).
- **add** Expanded extension coverage across all existing categories (60+ new extensions including ML data formats, modern web frameworks, and additional media/archive types).
- **add** Sunburst is now the default visualization and first tab (`1`); treemap moved to `2`.
- **add** `default_viz` config is now applied on explorer mount.

**Docs**

- **docs** User guide: updated welcome screen section, added FS Overview section, added File-Type Categories reference, Cleanup experimental note, updated key binding table, documented `show_cleanup` and per-path worker detection.

## v0.1.1

Since `29b6eaf` (Add file locations section to user guide).

- **add** Database schema v3: path interning (each unique path stored once) and delta storage (only changed directories between snapshots). ~97% size reduction for typical workloads.
- **add** Full baselines every 50 snapshots; deltas in between.
- **add** `strict_path` config option (`[monitor]`) to restrict snapshot matching to exact paths only; toggle in Settings.
- **add** Welcome screen with three path options: current directory, saved default, and last visited. Tab to switch.
- **add** Hostname-aware path storage: paths stored per hostname by default (`hostname_aware_paths` config); toggle in Settings.
- **add** Settings shows database file size and path under System Information.
- **fix** Empty Changes table — `min_delta` threshold was 1 MB, now 0.
- **fix** Trend chart time axis — adaptive format: HH:MM intraday, MM-DD HH:MM multi-day, YYYY-MM-DD for weeks+.
- **fix** Snapshot path matching is now bidirectional: exploring `/data` surfaces watches at `/data/logs` and vice versa.
- **fix** Monitor data loading moved to background thread; added loading indicator to prevent UI freeze.
- **fix (qol)** Snapshot table capped at 200 most recent entries.
- **fix (qol)** Changes title shows the timestamps of the two compared snapshots.
- **fix (qol)** Path completion capped at 200 entries to avoid blocking on large directories (e.g. `/home`).
- **fix (breaking)** Migration v3 clears existing snapshot history. Delete the old database before launching: `rm ~/.local/share/fsmonitor-cli/data.db`
- **fix (breaking)** `VACUUM` run after destructive migration to reclaim disk space.
- **docs** Restructured README: install/uninstall, stored data table, TUI/CLI split, screenshot gallery.
- **docs** Restructured user guide into TUI and CLI sections; added `enabled_rules`/`disabled_rules` config fields.
- **docs** Added `monitor/` module to architecture.md and contributing.md.

## v0.1.0

Initial release (`29b6eaf` and earlier).

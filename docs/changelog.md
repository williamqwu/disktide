# Changelog

## v0.1.5

Since `6cb0459` (v0.1.4 release).

**Scanner**

- **fix** Directory scans no longer recurse forever on a filesystem cycle. A bind mount (or container rootfs) that makes a directory reappear inside itself sent the scan into an infinite loop, which is common under devcontainer and Docker data directories. The walker now tracks each directory's `(st_dev, st_ino)` identity along the path from the scan root and stops when a directory is its own ancestor, marking it `(loop)` in the tree and the Details panel. Symlink loops were already prevented; this closes the non-symlink case.
- **fix** Symlink target classification is now deferred. The walker previously did one `entry.stat(follow_symlinks=True)` per symlink to record whether the target was a directory, a file, or broken; on slow storage with many symlinks (cluster home with `miniconda3`, `node_modules`, `.vscode-server`, etc.) this added minutes to a scan because each follow-stat is a remote round-trip. The walker now classifies only symlinks at the scan-root level eagerly (so the initial view stays informative); deeper symlinks are classified on demand when the Details panel renders them or the `i` action navigates one. The tree label's `/` suffix for dir-targets and `(broken)` marker move to the Details panel for deeper links; selecting the link surfaces the same info on demand.

**Progress**

- **fix** Scan progress now updates continuously during a deep scan instead of freezing while a single big subtree is being walked. The walker calls a per-directory tick callback as each directory finishes; the engine folds those into shared live counters (using the existing engine lock) and forwards them through the existing `ProgressThrottle` (100 ms coalesce). The earlier behavior only updated when an entire top-level subtree completed, so a scan of a directory with one dominant child (typical under devcontainer or Docker data directories) sat with frozen `Dirs / Files / Size` counters for minutes.
- **fix** The scanning overlay's progress bar is now indeterminate (a pulsing animation with no percentage). The earlier percentage was `completed_top_level_dirs / total_top_level_dirs`, unrelated to actual work, and routinely parked at 97% while one large subtree finished. Without a pre-count pass there is no honest progress fraction; an indeterminate bar plus the now-continuously-moving counters is the honest feedback.

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

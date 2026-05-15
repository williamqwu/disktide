# Changelog

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
- **add** UI setting **Safe rendering (web shells)** — when enabled, swaps the block-drawing proportional bar (`█`/`░`) for ASCII (`#`/space) and `⚠`/`◐` accessibility glyphs for `[!]`/`[~]`. Default off; web-based shells (OSC OnDemand, JupyterHub terminals) often lack the Unicode glyphs and render them as runaway horizontal lines.

**Partial Inaccessibility (#14)**

- **add** Walker/engine now track per-node `inaccessible_count` (direct unreadable entries) and `inaccessible_subtree_count` (bottom-up aggregate); previously, per-entry `OSError`s were silently dropped, hiding the fact that reported sizes under-counted.
- **add** Size tree marks affected directories with a yellow `◐ N hidden` glyph (full denial keeps its red `⚠`); ancestors with hidden state below get a dim `◐`.
- **add** Info panel gains an **Access** row (Full / Partial / Denied) and prefixes the size with `≥` when the subtree is partially scanned.
- **add** Sunburst arc labels and treemap rect labels append `◐` (partial) or `⚠` (denied) when there is room.
- **add** Explorer header subtitle summarises subtree counts: `⚠ N denied, ◐ M partial`.

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

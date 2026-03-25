# Changelog

## v0.1.1

Since `29b6eaf` (Add file locations section to user guide).

**Storage**

- Redesigned database schema: path interning (each unique path stored once) and delta storage (only changed directories between snapshots). ~97% size reduction for typical workloads.
- Full baselines every 50 snapshots; deltas in between.
- `VACUUM` after destructive migration to reclaim disk space.
- Breaking: migration v3 clears existing snapshot history. Delete the old database before launching: `rm ~/.local/share/fsmonitor-cli/data.db`

**Monitor**

- Fixed empty Changes table (`min_delta` was 1 MB, now 0).
- Changes title shows compared snapshot timestamps.
- Trend chart x-axis shows real timestamps instead of integer indices.
- Data loading moved to background thread with loading indicator.
- Snapshot table capped at 200 most recent entries.

**Settings**

- Shows database file size and path under System Information.

**Documentation**

- Restructured README: install/uninstall, stored data table, TUI/CLI split, screenshot gallery.
- Restructured user guide into TUI and CLI sections; added `enabled_rules`/`disabled_rules` config fields.
- Added `monitor/` module to architecture.md and contributing.md.

## v0.1.0

Initial release (`29b6eaf` and earlier).

# User Guide

fsmonitor-cli is an interactive terminal tool for exploring disk usage, detecting cleanup opportunities, and tracking how directory sizes change over time.

## Getting Started

Install and launch:

```bash
pip install -e .
fsmonitor-cli
```

The welcome screen appears with a path input pre-filled with your home directory. Type or edit the path, then press Enter or click Explore to begin scanning.

To accept the ghost-text suggestion that appears as you type, press the right arrow key. The completions list below the input updates live as you type, showing available files and directories.

Check "Save as default path" to remember your choice for next time.

## Modes

The TUI has three modes, switched with the `E`, `C`, and `M` keys:

### Explorer (E)

The main view. A file tree on the left shows directories sorted by size, with inline size bars. The right panel shows one of three visualizations:

- **Treemap** (`1`) -- Rectangles sized proportionally to disk usage. Drill into directories by clicking or selecting them.
- **Sunburst** (`2`) -- Concentric rings radiating outward by depth. Each arc's angle represents its share of the parent.
- **Details** (`3`) -- Text panel with metadata about the selected file or directory.

Navigation:

| Key | Action |
|-----|--------|
| Up/Down | Move through the tree |
| Left/Right | Collapse/expand tree nodes |
| `u` | Go up to the parent directory |
| `i` | Drill into the selected directory (rescans from there) |
| `s` | Cycle sort order: size, name, modified |
| `r` | Rescan the current directory |

### Cleanup (C)

Detects files and directories that can safely be removed: dependency caches (`node_modules`), build outputs, bytecode files, old logs, OS junk files, and IDE caches.

Switch to cleanup mode after scanning in explorer. A table lists all targets grouped by category, with size and risk level (safe, moderate, dangerous).

| Key | Action |
|-----|--------|
| Space | Toggle selection on the current row |
| `a` | Select all targets |
| `d` | Delete selected targets (with confirmation) |
| `r` | Rescan for targets |

### Monitor (M)

Shows historical snapshots and size trends. The monitor displays data from the `watch` command (see below) or any scans saved with `--snapshot`.

The top half shows a snapshot table and a trend chart. The bottom half shows the largest changes between the two most recent snapshots.

Press `r` to refresh data. The monitor also refreshes automatically each time you switch to it.

If you are watching directory `/data` and then explore `/data/logs` in the TUI, the monitor will show the snapshots from the parent `/data` watch.

## CLI Commands

These commands run outside the TUI and print results to stdout.

### scan

One-shot scan with a text summary:

```bash
fsmonitor-cli scan /path
fsmonitor-cli scan /path --snapshot      # save results to the database
fsmonitor-cli scan /path -d 5 -w 4       # limit depth to 5, use 4 threads
```

### watch

Periodic scanning with automatic snapshots. Runs until interrupted or `--max-time` is reached:

```bash
fsmonitor-cli watch /path                   # default interval from config (6h)
fsmonitor-cli watch /path --interval 1h     # scan every hour
fsmonitor-cli watch /path -i 30m -t 12h    # every 30 min, stop after 12 hours
```

Snapshots are saved to the database and visible in the Monitor tab. Old snapshots are pruned automatically based on the `snapshot_retention` setting (default: 30 days).

Since `watch` runs in the foreground, use tmux or nohup for persistent monitoring:

```bash
# tmux (recommended -- reattach later with `tmux attach -t fsmon`)
tmux new -s fsmon
fsmonitor-cli watch /path --interval 6h

# nohup (background, no reattach)
nohup fsmonitor-cli watch /path --interval 6h > /dev/null 2>&1 &
```

For a systemd user service that survives reboots:

```ini
# ~/.config/systemd/user/fsmonitor.service
[Service]
ExecStart=%h/.local/bin/fsmonitor-cli watch /path --interval 6h

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now fsmonitor.service
loginctl enable-linger $USER
```

### cleanup

Non-interactive cleanup with confirmation prompt:

```bash
fsmonitor-cli cleanup /path
```

## Configuration

Settings are stored in `~/.config/fsmonitor-cli/config.toml` (respects `XDG_CONFIG_HOME`). Edit the file directly or use the settings screen (`?` in the TUI).

```toml
[scan]
max_depth = 10                           # omit for unlimited
workers = 4                              # omit for auto-detect
follow_symlinks = false
exclude_patterns = [".git", "node_modules"]

[cleanup]
require_confirm_dangerous = true

[monitor]
default_interval = 21600                 # 6 hours, in seconds
snapshot_retention = 30                  # days
# max_watch_time = 86400                # optional cap in seconds

[ui]
color_theme = "warm"                     # default, cold, warm, vivid, mono
default_sort = "size"                    # size, name, mtime
default_viz = "treemap"                  # treemap, sunburst, details
show_hidden = false
# default_scan_path = "/home/user/data" # pre-fill welcome screen
```

All fields are optional. Missing values use sensible defaults. When `workers` is omitted, the scanner picks a thread count based on CPU count, system load, filesystem type, and available memory.

## Quick Reference

| Key | Scope | Action |
|-----|-------|--------|
| `e` / `c` / `m` | Global | Switch to Explorer / Cleanup / Monitor |
| `?` | Global | Open settings |
| `q` | Global | Quit |
| `1` / `2` / `3` | Explorer | Treemap / Sunburst / Details |
| `u` / `i` | Explorer | Navigate up / drill into directory |
| `s` | Explorer | Cycle sort order |
| `r` | Explorer, Cleanup, Monitor | Rescan / refresh |
| `d` | Cleanup | Delete selected |
| `a` | Cleanup | Select all |
| Space | Cleanup | Toggle row selection |

# fsmonitor-cli

Interactive terminal disk usage explorer built with Python and Textual.

## Installation

```bash
pip install -e .
```

## Usage

```bash
# Launch interactive TUI
fsmonitor-cli /path/to/explore

# CLI scan
fsmonitor-cli scan /path --snapshot

# Watch mode
fsmonitor-cli watch /path --interval 6h

# Cleanup mode
fsmonitor-cli cleanup /path
```

## Configuration

Settings are stored in `~/.config/fsmonitor-cli/config.toml` (respects `XDG_CONFIG_HOME`). You can edit the file directly or use the settings screen (`?` key in the TUI).

```toml
[scan]
max_depth = 10
workers = 4
follow_symlinks = false
exclude_patterns = [".git", "node_modules"]

[cleanup]
require_confirm_dangerous = true

[monitor]
default_interval = 21600
snapshot_retention = 30

[ui]
color_theme = "default"
default_sort = "size"
default_viz = "treemap"
show_hidden = false
```

All fields are optional — missing values use defaults. Workers defaults to an adaptive value based on CPU count, system load, filesystem type, and available memory.

## Key Bindings

| Key | Action |
|-----|--------|
| e/c/m | Switch mode (Explorer/Cleanup/Monitor) |
| 1/2/3 | Switch visualization (Treemap/Sunburst/Details) |
| u/i | Navigate up / drill into directory |
| s | Cycle sort (Size/Name/Modified) |
| r | Rescan |
| ? | Settings |
| q | Quit |

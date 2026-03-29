# fsmonitor-cli

Interactive terminal disk usage explorer built with Python and [Textual](https://github.com/Textualize/textual).

<p align="center">
  <img src="docs/images/sunburst.png" width="49%" alt="Explorer with sunburst visualization" />
  <img src="docs/images/monitor.png" width="49%" alt="Monitor with snapshot history and size trends" />
</p>

## Installation

```bash
pip install -e .
```

To uninstall:

```bash
pip uninstall fsmonitor-cli
```

**Stored data.** The app follows XDG conventions and writes to three locations (all safe to delete):

| Location | Contents | Typical size |
|----------|----------|--------------|
| `~/.config/fsmonitor-cli/config.toml` | User settings | < 1 KB |
| `~/.local/share/fsmonitor-cli/data.db` | SQLite database (directory-level snapshots) | 1 -- 200 MB depending on tree size and snapshot count |
| `~/.cache/fsmonitor-cli/` | Scan result cache (JSON) | < 10 MB |

The database is only written by the CLI commands `scan --snapshot` and `watch`. The TUI reads from it (Monitor mode) but never writes. Old snapshots are pruned automatically based on retention settings (default: 30 days).

Paths respect `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_CACHE_HOME` if set. Settings can also be edited by pressing `?` inside the TUI. See the [User Guide](docs/user-guide.md) for the full configuration reference.

## Quick Start

`fsmonitor-cli` has two modes of operation: an **interactive TUI** for visual exploration (the main interface), and three **CLI commands** (`scan`, `watch`, `cleanup`) for scripting and one-shot tasks.

### TUI

```bash
# Launch interactive TUI (opens welcome screen)
fsmonitor-cli
```

#### Key Bindings

| Key | Action |
|-----|--------|
| `e` / `c` / `m` | Switch mode (Explorer / Cleanup / Monitor) |
| `1` / `2` / `3` | Switch visualization (Treemap / Sunburst / Details) |
| `u` / `i` | Navigate up / drill into directory |
| `s` | Cycle sort (Size / Name / Modified) |
| `r` | Rescan / refresh |
| `?` | Settings |
| `q` | Quit |

### CLI Commands

```bash
# Scan a directory directly
fsmonitor-cli scan /path --snapshot

# Watch for changes over time
fsmonitor-cli watch /path --interval 6h

# Find and clean up unnecessary files
fsmonitor-cli cleanup /path
```

## Documentation

- **[User Guide](docs/user-guide.md)** -- Usage, configuration, CLI commands, all key bindings
- **[Architecture](docs/architecture.md)** -- Internals: scanner threading, database schema, visualization algorithms, screen management
- **[Filesystem Compatibility](docs/fs.md)** -- Supported filesystems, every syscall the tool makes, platform-specific behavior
- **[Contributing](docs/contributing.md)** -- Dev setup, testing, how to add rules/screens/visualizations

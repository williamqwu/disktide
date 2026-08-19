# fsmonitor

Interactive terminal disk usage explorer built with Python and [Textual](https://github.com/Textualize/textual).

<p align="center">
  <img src="docs/images/sunburst.png" width="49%" alt="Explorer with sunburst visualization" />
  <img src="docs/images/monitor.png" width="49%" alt="Monitor with snapshot history and size trends" />
</p>

## Installation

```bash
# Install a snapshot from the current checkout
uv tool install .

# Or keep the installed command linked to this checkout during development
uv tool install --force --editable .
```

To uninstall:

```bash
uv tool uninstall fsmonitor-cli
```

The Python distribution is still named `fsmonitor-cli` for package-index compatibility. The canonical command is `fsmonitor`; the previous `fsmonitor-cli` command remains available as a compatibility alias.

**Stored data.** The app follows XDG conventions and writes to two persistent locations. Deleting either is safe for the filesystem, but resets the corresponding settings or snapshot history. The existing `fsmonitor-cli` directory names are intentionally retained so upgrades keep current data:

| Location | Contents | Typical size |
|----------|----------|--------------|
| `~/.config/fsmonitor-cli/config.toml` | User settings | < 1 KB |
| `~/.local/share/fsmonitor-cli/data.db` | SQLite database (directory-level snapshots) | 1 -- 200 MB depending on tree size and snapshot count |

Snapshot data is written by the CLI commands `scan --snapshot` and `watch`. The TUI opens the database for recent paths and Monitor mode but does not save snapshots. Old snapshots are pruned automatically based on retention settings (default: 30 days).

Paths respect `XDG_CONFIG_HOME` and `XDG_DATA_HOME` if set. Settings can also be edited by pressing `?` inside the TUI. See the [User Guide](docs/user-guide.md) for the full configuration reference.

## Quick Start

`fsmonitor` has two modes of operation: an **interactive TUI** for visual exploration (the main interface), and three **CLI commands** (`scan`, `watch`, `cleanup`) for scripting and one-shot tasks.

### TUI

```bash
# Launch interactive TUI (opens welcome screen)
fsmonitor
```

#### Key Bindings

| Key | Action |
|-----|--------|
| `e` / `m` / `f` | Switch mode (Explorer / Monitor / FS Overview) |
| `c` | Switch to experimental Cleanup mode (enable it in Settings first) |
| `1` / `2` / `3` | Switch visualization (Sunburst / Treemap / Details) |
| `u` / `i` | Navigate up / drill into directory |
| `s` | Cycle sort (Size / Name / Modified) |
| `y` / `t` | Copy highlighted path / toggle size vs. file count |
| `r` | Rescan / refresh |
| `b` | Benchmark the highlighted mount in FS Overview (after confirmation) |
| `?` | Settings |
| `q` | Quit |

### CLI Commands

```bash
# Scan a directory directly
fsmonitor scan /path --snapshot

# Watch for changes over time
fsmonitor watch /path --interval 6h

# Find candidates and permanently delete all of them after confirmation
fsmonitor cleanup /path
```

## Documentation

- **[User Guide](docs/user-guide.md)** -- Usage, configuration, CLI commands, all key bindings
- **[Architecture](docs/architecture.md)** -- Internals: scanner threading, database schema, visualization algorithms, screen management
- **[Filesystem Compatibility](docs/fs.md)** -- Supported filesystems, every syscall the tool makes, platform-specific behavior
- **[Contributing](docs/contributing.md)** -- Dev setup, testing, how to add rules/screens/visualizations

# fsmonitor-cli

Interactive terminal disk usage explorer built with Python and Textual.

## Installation

```bash
pip install -e .
```

## Quick Start

```bash
# Launch interactive TUI (opens welcome screen)
fsmonitor-cli

# Scan a directory directly
fsmonitor-cli scan /path --snapshot

# Watch for changes over time
fsmonitor-cli watch /path --interval 6h

# Find and clean up unnecessary files
fsmonitor-cli cleanup /path
```

## Key Bindings

| Key | Action |
|-----|--------|
| `e` / `c` / `m` | Switch mode (Explorer / Cleanup / Monitor) |
| `1` / `2` / `3` | Switch visualization (Treemap / Sunburst / Details) |
| `u` / `i` | Navigate up / drill into directory |
| `s` | Cycle sort (Size / Name / Modified) |
| `r` | Rescan / refresh |
| `?` | Settings |
| `q` | Quit |

## Configuration

Settings live in `~/.config/fsmonitor-cli/config.toml` (respects `XDG_CONFIG_HOME`), or press `?` in the TUI. All fields are optional with sensible defaults. See the [User Guide](docs/user-guide.md) for the full reference.

## Documentation

- **[User Guide](docs/user-guide.md)** -- Usage, configuration, CLI commands, all key bindings
- **[Architecture](docs/architecture.md)** -- Internals: scanner threading, database schema, visualization algorithms, screen management
- **[Filesystem Compatibility](docs/fs.md)** -- Supported filesystems, every syscall the tool makes, platform-specific behavior
- **[Contributing](docs/contributing.md)** -- Dev setup, testing, how to add rules/screens/visualizations

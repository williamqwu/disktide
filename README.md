<div align="center">

<h1>DiskTide</h1>

<h3><code>&nbsp;ebb,&nbsp;flow,&nbsp;reclaim&nbsp;</code></h3>

<p>
  <b>CLI-native disk-usage visualization.</b><br>
  Explorer, monitor, and cleanup in one terminal tool.<br>
  Built with Python and <a href="https://github.com/Textualize/textual">Textual</a>.
</p>

<p>
  <a href="https://github.com/williamqwu/disktide/actions/workflows/ci.yml">
    <img alt="CI" src="https://github.com/williamqwu/disktide/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.10 to 3.14" src="https://img.shields.io/badge/python-3.10%20to%203.14-3776AB?logo=python&logoColor=white">
  <a href="LICENSE">
    <img alt="License Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-green"></a>
  <img alt="Textual TUI" src="https://img.shields.io/badge/TUI-Textual-5A2CA0">
</p>

<img alt="Explorer: the file tree of a disktide working checkout on the left, led by .venv/ at 47.7% of 8.1 MiB, its sunburst on the right in the default tiles shape, concentric rectangular rings cut by straight lines with the virtualenv a rust frame enclosing every inner ring, legend reading ephemeral 52%, code 30%, media 10%, docs 8%"
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/readme/sunburst.png" width="49%">
<img alt="Monitor Center: the Space-Time Trend of the disktide codebase growing from March to August 2026, 184 snapshots, one per commit"
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/readme/monitor.png" width="49%">

<p>
  <a href="#installation"><b>Install</b></a> &nbsp;·&nbsp;
  <a href="docs/user-guide.md">User Guide</a> &nbsp;·&nbsp;
  <a href="docs/user-guide.md#cli-commands">CLI</a> &nbsp;·&nbsp;
  <a href="docs/architecture.md">Architecture</a> &nbsp;·&nbsp;
  <a href="docs/contributing.md">Contributing</a>
</p>

</div>

## What it does

- **General** — measures every tree four ways (logical bytes, allocated blocks, unique on-disk, file count) so sparse files, hardlinks, and many-tiny-file directories each show up for what they are.
  - Filesystem boundaries, pseudo-mounts, policy exclusions, and unreadable subtrees are reported as coverage, never silently dropped.
  - Five validated color themes, mouse support, and XDG-compliant stored state.

- **Explorer** — sorted file tree with three synchronized visualizations: sunburst, treemap, and details panel.
  - `t` cycles the size metric across every view at once; `g` cycles the sunburst shape (tiles / disc / fill).

- **Monitor** — snapshot-based growth tracking with versioned retention, pinned snapshots, and audited alerts.
  - Four history views share one baseline/target pair: Trend, Diff Treemap, growth-overlay Sunburst, and persistent-growth Heatmap.
  - No daemon: scans run only while a TUI session or `disktide watch` foreground host is alive.

- **Cleanup** — versioned rule packs (Python, Node, Rust, general, IDE, container) with a review-before-apply workflow.
  - Every apply goes through a persisted plan, moves to Trash or quarantine, and stays undoable. Permanent deletion is a separate typed-confirmation path.

## Installation

Python 3.10–3.14, five runtime dependencies, nothing to configure. The
scanner carries one small optional C extension, prebuilt in the Linux and
macOS wheels; where there is no wheel for your platform the source
distribution builds it if a compiler is present and runs a pure-Python
fallback if not, so the install never fails for the want of one.
`disktide doctor` says which is live.

```bash
git clone https://github.com/williamqwu/disktide && cd disktide
```

**With uv** (recommended):

```bash
uv sync --locked                 # editable venv with dev tools
uv run disktide                  # or: . .venv/bin/activate && disktide
```

**With pip:**

```bash
python -m venv .venv && . .venv/bin/activate
pip install .                    # -e for editable
disktide
```

### Optional extras

| Extra | Install | Effect |
|---|---|---|
| `watch` | `pip install '.[watch]'` / `uv sync --locked --extra watch` | Linux only. `inotify-simple` for filesystem-event acceleration; without it, monitors work in periodic mode. |

`disktide doctor` reports the active watch backend and its status.

## Quick start

```bash
disktide                         # interactive TUI
disktide scan ~/projects         # one-shot scan
disktide monitor                 # list monitors
disktide compare <monitor>       # growth report
disktide cleanup ~/projects      # review cleanup candidates
```

Press `?` for settings and the full keymap. `--help` works at every level.
See the [User Guide](docs/user-guide.md) for details.

## Stored data

| Location | Contents |
|----------|----------|
| `~/.config/disktide/config.toml` | User settings |
| `~/.config/disktide/cleanup-rules/*.toml` | Declarative cleanup rule packs |
| `~/.local/share/disktide/data.db` | Monitors, snapshots, retention/alert history, cleanup plans |

Paths respect `XDG_CONFIG_HOME` and `XDG_DATA_HOME`. Legacy `sizetrail` and
`fsmonitor-cli` directories are picked up automatically on first run.

## Documentation

- **[User Guide](docs/user-guide.md)** — usage, configuration, key bindings
- **[CLI Reference](docs/user-guide.md#cli-commands)** — subcommands, flags, JSON output, exit codes
- **[Architecture](docs/architecture.md)** — scanner threading, database schema, visualization algorithms
- **[Filesystem Compatibility](docs/fs.md)** — supported filesystems and platform behavior
- **[Release Process](docs/release-process.md)** — locked builds, checksums, SBOM, provenance
- **[Contributing](docs/contributing.md)** — dev setup, testing, extending

## License

[Apache License 2.0](LICENSE).

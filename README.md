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
  <a href="https://github.com/williamqwu/disktide/blob/main/LICENSE">
    <img alt="License Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-green"></a>
  <img alt="Textual TUI" src="https://img.shields.io/badge/TUI-Textual-5A2CA0">
</p>

<img alt="Explorer: the file tree of a disktide working checkout on the left, led by .venv/ at 45.2% of 8.5 MiB, every row's share bar drawn in one column, its sunburst on the right in the default tiles shape, concentric rectangular rings cut by straight lines with the virtualenv a rust frame enclosing every inner ring, legend reading ephemeral 49%, code 42%, docs 8%"
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/readme/sunburst.png" width="49%">
<img alt="Monitor Center: the Space-Time Trend of the disktide codebase growing from March to September 2026, 342 snapshots, one per commit"
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/readme/monitor.png" width="49%">

<p><a href="https://github.com/williamqwu/disktide/blob/main/docs/blogs/2026-09-12-reading-the-tui.md">(How to read these charts?)</a></p>

<p>
  <a href="#installation"><b>Install</b></a> &nbsp;·&nbsp;
  <a href="https://github.com/williamqwu/disktide/blob/main/docs/user-guide.md">User Guide</a> &nbsp;·&nbsp;
  <a href="https://github.com/williamqwu/disktide/blob/main/docs/user-guide.md#cli-commands">CLI</a> &nbsp;·&nbsp;
  <a href="https://github.com/williamqwu/disktide/blob/main/docs/architecture.md">Architecture</a> &nbsp;·&nbsp;
  <a href="https://github.com/williamqwu/disktide/blob/main/docs/contributing.md">Contributing</a>
</p>

</div>

## What it does

- **General.** DiskTide measures every directory four ways: logical bytes, allocated blocks, unique on-disk bytes after hardlink dedup, and file count. Sparse files, hardlinks and piles of tiny files all show up for what they are. Anything the scan could not cover (other filesystems, pseudo mounts, excluded paths, unreadable directories) is reported as coverage instead of being silently dropped.
  - Six color themes, one of which follows your terminal's own 16 colors. The mouse works. Config and data live in standard XDG paths.

- **Explorer.** A sorted file tree next to a sunburst, a treemap, or a details panel. `t` switches the size metric everywhere at once, `g` switches the sunburst shape (tiles, disc, fill), and `d` shows what changed between two saved snapshots (the newest two by default).

- **Monitor.** Takes a snapshot of a directory on a schedule (every 6 hours by default) and shows you how it grew: a trend chart, a diff treemap, a growth sunburst, and a heatmap of what keeps growing. Snapshots can be pinned, retention thins out the old ones, and alerts fire on thresholds you set.
  - There is no daemon. Scans run only while a TUI session is sampling (`S` in the Monitor Center, or Auto-start) or `disktide watch` is running.

- **Cleanup.** Finds reclaimable build and cache artifacts using six rule packs (Python, Node, Rust, general, IDE, containers). You review a plan before anything moves. Files go to the system Trash or to a quarantine directory, and the plan can be undone. Permanent deletion is a separate step that asks you to type a confirmation.

Scanning is metadata only: DiskTide never opens or reads file contents. A small optional C extension makes multi-worker scans about twice as fast.

## Installation

Python 3.10 to 3.14, Linux or macOS, five runtime dependencies.

**With uv** (recommended) [(Why uv?)](https://github.com/williamqwu/disktide/blob/main/docs/why-uv.md):

```bash
uv tool install disktide   # isolated environment, `disktide` on your PATH
disktide
```

`uvx disktide` runs it once without installing anything. `uv tool upgrade disktide` picks up a new release and `uv tool uninstall disktide` removes it. If your shell cannot find `disktide` afterwards, run `uv tool update-shell` (it adds uv's bin directory, normally `~/.local/bin`, to your PATH) and open a new shell.

**With pip:**

```bash
python -m venv .venv && . .venv/bin/activate
pip install disktide
disktide
```

`pipx install disktide` is a one-line alternative that also gives you a global `disktide` command.

The scanner has a small optional C extension. The wheels on PyPI include it for CPython 3.10 to 3.14 on Linux (x86_64 and aarch64, glibc and musl) and macOS (Intel and Apple silicon). Anywhere else the install compiles it when a C compiler is present. Without one the install still succeeds and DiskTide uses a slower pure-Python reader instead. `disktide doctor` shows which one is active.

**From source:**

```bash
git clone https://github.com/williamqwu/disktide && cd disktide
uv tool install .      # or: pip install .
```

After a `git pull`, run `uv tool install --reinstall .` to pick up the new version. See [contributing.md](https://github.com/williamqwu/disktide/blob/main/docs/contributing.md#scanner-extension-in-a-development-checkout) for rebuilding the C extension after editing it.

### Optional extras

| Extra | Install | Effect |
|---|---|---|
| `watch` | `uv tool install 'disktide[watch]'` / `pip install 'disktide[watch]'` | Linux only. Installs `inotify-simple` so monitors react to filesystem events. Without it, monitors run on a timer. |

`disktide doctor` reports the active watch backend and its status.

## Quick start

```bash
disktide                                # interactive TUI, welcome screen
disktide ~/projects                     # interactive TUI, straight into that path
disktide scan ~/projects                # one-shot scan
disktide monitor list                   # list monitors
disktide compare --since 7d ~/projects  # what grew in the last week
disktide cleanup ~/projects             # review cleanup candidates
```

`?` opens the key map for the current screen, `,` opens Settings, and Ctrl+P searches every command by name. `--help` works at every level. See the [User Guide](https://github.com/williamqwu/disktide/blob/main/docs/user-guide.md) for details.

## Stored data

| Location | Contents | Size |
|---|---|---|
| `~/.config/disktide/config.toml` | Your settings and recent paths. | Under 1 KiB (a few hundred bytes). |
| `~/.config/disktide/cleanup-rules/*.toml` | Your own cleanup rule packs. The six built-in packs ship inside the package and are not copied here. | Nothing unless you add one. A pack is a few KiB. |
| `~/.local/share/disktide/data.db` | Monitors, snapshots, retention and alert history, cleanup plans (SQLite). | About 210 KiB empty. The first snapshot of a tree costs roughly 200 to 500 bytes per file or directory, mostly the path text: 11 MiB for a tree of 23,000 entries with long paths, and a few hundred MiB for a million entries. Later snapshots of the same tree store only what changed, about 100 bytes per changed entry, plus a full re-baseline of about 55 bytes per entry every 50th snapshot. |
| `~/.local/share/disktide/data.db.pre-vN.bak` | A one-time backup taken before a schema migration. `disktide doctor` names it. | Whatever the database weighed at that moment. Safe to delete once the new version has run. |
| `.disktide-quarantine/` next to a cleaned path | Where cleanup moves files when the system Trash is not available on that filesystem. Normal cleanups go to the standard XDG Trash at `~/.local/share/Trash`. | Whatever was moved. Kept 7 days and capped at 10 GiB by default (`quarantine_retention_days` and `quarantine_max_bytes` under `[cleanup]`). |

Paths respect `XDG_CONFIG_HOME` and `XDG_DATA_HOME`. Legacy `sizetrail` and
`fsmonitor-cli` directories are picked up automatically on first run.

## Documentation

- **[User Guide](https://github.com/williamqwu/disktide/blob/main/docs/user-guide.md)**: usage, configuration, key bindings
- **[CLI Reference](https://github.com/williamqwu/disktide/blob/main/docs/user-guide.md#cli-commands)**: subcommands, flags, JSON output, exit codes
- **[Architecture](https://github.com/williamqwu/disktide/blob/main/docs/architecture.md)**: scanner threading, database schema, visualization algorithms
- **[Filesystem Compatibility](https://github.com/williamqwu/disktide/blob/main/docs/fs.md)**: supported filesystems and platform behavior
- **[Release Process](https://github.com/williamqwu/disktide/blob/main/docs/release-process.md)**: locked builds, checksums, SBOM, provenance
- **[Contributing](https://github.com/williamqwu/disktide/blob/main/docs/contributing.md)**: dev setup, testing, extending

## License

[Apache License 2.0](https://github.com/williamqwu/disktide/blob/main/LICENSE).

# Contributing

Guide for developers working on fsmonitor-cli.

## Setup

```bash
git clone <repo-url> && cd util_fs_monitor
pip install -e ".[dev]"
```

This installs the project in editable mode with dev dependencies (pytest, pytest-asyncio, textual-dev).

## Running Tests

```bash
# full suite
python -m pytest tests/ -v

# specific module
python -m pytest tests/test_scanner.py -v

# with coverage (if pytest-cov installed)
python -m pytest tests/ --cov=fs_monitor
```

The test suite covers: scanner engine, tree model, cache, config, storage/database, cleanup rules and detection, visualization, braille canvas, progress reporting, system info detection, snapshot comparison, welcome screen, and migrations.

Async tests use `pytest-asyncio` with strict mode (configured in `pyproject.toml`).

## Project Structure

See [architecture.md](architecture.md) for the full layout. In brief:

- `src/fs_monitor/` -- all source code
- `tests/` -- test suite (no subdirectories, flat layout)
- `assets/` -- TCSS stylesheets
- `tool/` -- development utilities (e.g., `gen_activity` for generating test data)
- `docs/` -- documentation

## Key Conventions

### Code Style

- Python >= 3.11 features are used freely: `X | Y` union syntax, `tomllib`, `slots=True` dataclasses.
- Type annotations on all public APIs. `from __future__ import annotations` at the top of every module.
- Dataclasses with `slots=True` for models (`FSNode`, `Snapshot`, `SizeDelta`).

### Module Boundaries

- **models/** -- pure data structures, no I/O. Can be imported anywhere.
- **scanner/** -- filesystem I/O only. No Textual imports.
- **storage/** -- SQLite I/O only. No Textual imports.
- **cleanup/** -- operates on FSNode trees. No Textual imports.
- **monitor/** -- alerting, tree diffs, scan scheduling. No Textual imports.
- **viz/** -- rendering logic. Produces Rich Segments, no direct Textual widget deps.
- **screens/** and **widgets/** -- Textual UI layer. Can import everything above.

This layering means the scanner, storage, cleanup, and viz modules are testable without a running Textual app.

### Configuration

`config.py` uses plain dataclasses (not Pydantic). TOML serialization is manual (line-by-line string building in `save_config`). When adding a new config field:

1. Add the field to the appropriate dataclass (`ScanConfig`, `CleanupConfig`, `MonitorConfig`, `UIConfig`)
2. Add serialization in `save_config()` -- skip `None` values for optional fields
3. Add deserialization in `load_config()` with a sensible default
4. Add a roundtrip test in `tests/test_config.py`

### Database Migrations

`storage/migrations.py` uses a `schema_version` table. To add a migration:

1. Increment the version number
2. Add a migration function that takes a `sqlite3.Connection`
3. Register it in the migration list
4. Add a test in `tests/test_migrations.py`

## Adding a New Cleanup Rule

1. Add a `CleanupRule` entry in `cleanup/rules.py`:

```python
CleanupRule(
    name="my_rule",
    description="What this cleans up",
    patterns=["pattern1", "pattern2"],
    risk=RiskLevel.SAFE,           # SAFE, MODERATE, or DANGEROUS
    parent_indicators=["marker"],  # optional: parent must contain this file
    min_age_days=0,                # optional: minimum age in days
    category="my_category",
)
```

2. Add test cases in `tests/test_rules.py` and `tests/test_cleanup.py`.

## Adding a New Screen

1. Create `screens/my_screen.py` subclassing `textual.screen.Screen`.
2. Define `BINDINGS` and `compose()`.
3. Install the screen in `app.py` `_launch_explorer()`:
   ```python
   self.install_screen(MyScreen(...), name="myscreen")
   ```
4. Add a key binding in `FSMonitorApp.BINDINGS` and a case in `action_switch_mode()`.
5. Add styles in `assets/default.tcss`.

## Adding a New Visualization

1. Create the layout/rendering logic in `viz/my_viz.py`. It should produce Rich `Segment` objects or use the braille canvas.
2. Create a Textual widget in `widgets/my_viz_view.py` that calls the renderer.
3. Add a `TabPane` in `screens/explorer.py` `compose()`.
4. Add a key binding (e.g., `4`) in `ExplorerScreen.BINDINGS` and handle it in `action_switch_viz()`.

## Testing Tips

- **Scanner tests**: Use `tmp_path` fixtures to create real directory trees. The scanner operates on real filesystems, not mocks.
- **Database tests**: Use in-memory SQLite (`:memory:`) or `tmp_path` for the db file. The `db` fixture in `tests/test_storage.py` provides a connected, migrated database.
- **Async tests**: Mark with `@pytest.mark.asyncio`. The `PathSuggester` tests are good examples.
- **Visualization tests**: Test layout computation separately from rendering. Verify rectangle coordinates, arc angles, etc. numerically.

## Dev Utilities

The `tool/` directory contains helper scripts:

- `gen_activity` -- generates filesystem activity (creates/modifies/deletes files) for testing the watch/monitor features. Supports `--max-files` and `--max-size` caps.

## Running the TUI in Dev Mode

```bash
# normal launch
fsmonitor-cli

# with Textual dev tools (live CSS reloading, DOM inspector)
textual run --dev -c fsmonitor-cli
```

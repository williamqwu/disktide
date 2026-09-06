"""Regressions for config values the loader and the writer have to survive."""

from __future__ import annotations

import pytest


# --- an unknown default_viz lands on the picker default -----------------


def test_an_unknown_default_viz_falls_back_to_the_picker_default(tmp_path):
    from disktide.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('[ui]\ndefault_viz = "foo"\n')

    assert load_config(path).ui.default_viz == "sunburst"


# --- a quoted path survives the config round trip ------------------------


@pytest.mark.parametrize("name", ['it"s', "back\\slash", "café"])
def test_a_quoted_path_survives_a_config_round_trip(tmp_path, name):
    from disktide.config import (
        AppConfig,
        HostPaths,
        get_effective_paths,
        load_config,
        save_config,
    )

    visited = str(tmp_path / name)
    config = AppConfig()
    config.host_paths['host"x'] = HostPaths(last_visited_path=visited)
    path = tmp_path / "config.toml"
    save_config(config, path)

    reloaded = load_config(path)

    assert reloaded.host_paths['host"x'].last_visited_path == visited


# --- a value of the wrong type says which line to fix --------------------


@pytest.mark.parametrize(
    "text, section, key",
    [
        ('[monitor]\ndatabase_soft_budget = "2GB"\n', "monitor",
         "database_soft_budget"),
        ('[cleanup]\nquarantine_retention_days = "x"\n', "cleanup",
         "quarantine_retention_days"),
        ('[scan]\nworkers = "4"\n', "scan", "workers"),
        ('[scan]\nmax_depth = "3"\n', "scan", "max_depth"),
        ('[monitor]\ndefault_interval = "6h"\n', "monitor",
         "default_interval"),
        ('[cleanup]\nmap_max_points = "80"\n', "cleanup", "map_max_points"),
        ('[scan]\none_file_system = "yes"\n', "scan", "one_file_system"),
        ('[scan]\nexclude_snapshot_dirs = "no"\n', "scan",
         "exclude_snapshot_dirs"),
        ('[ui]\nshow_cleanup = 1\n', "ui", "show_cleanup"),
        ('[ui]\ndefault_scan_path = 7\n', "ui", "default_scan_path"),
    ],
)
def test_a_wrong_type_names_its_section_and_key(tmp_path, text, section, key):
    """These reached the first place that did arithmetic on them.

    `[monitor] database_soft_budget = "2GB"` came out as
    `TypeError: '<=' not supported between instances of 'str' and 'int'`
    with a traceback, and `[scan] max_depth = "3"` did not even fail: the
    string was copied into a monitor's stored policy, where it outlived
    the config edit that made it.
    """
    from disktide.config import ConfigError, load_config

    path = tmp_path / "config.toml"
    path.write_text(text)

    with pytest.raises(ConfigError) as caught:
        load_config(path)

    message = str(caught.value)
    assert section in message and key in message


def test_a_section_that_is_not_a_table_is_named(tmp_path):
    from disktide.config import ConfigError, load_config

    path = tmp_path / "config.toml"
    path.write_text("scan = 1\n")

    with pytest.raises(ConfigError) as caught:
        load_config(path)

    assert "[scan]" in str(caught.value)


def test_malformed_toml_names_the_file(tmp_path):
    from disktide.config import ConfigError, load_config

    path = tmp_path / "config.toml"
    path.write_text("[scan\n")

    with pytest.raises(ConfigError) as caught:
        load_config(path)

    assert str(path) in str(caught.value)


def test_a_stray_entry_in_paths_is_skipped_not_refused(tmp_path):
    """[paths] is written by disktide; a hand-edit costs only itself."""
    from disktide.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('[paths]\nhost = "x"\n\n[paths.box]\nlast_visited_path = "/t"\n')

    config = load_config(path)

    assert "host" not in config.host_paths
    assert config.host_paths["box"].last_visited_path == "/t"


def test_a_good_config_still_loads_every_typed_key(tmp_path):
    """The readers must not have changed what a correct file means."""
    from disktide.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        "[scan]\nmax_depth = 3\nworkers = 2\none_file_system = true\n"
        "exclude_pseudo_filesystems = false\nexclude_snapshot_dirs = false\n"
        "[monitor]\ndefault_interval = 600\nmax_watch_time = 60\n"
        "database_soft_budget = 5\ndatabase_hard_budget = 6\n"
        "auto_start_in_tui = true\n"
        "[cleanup]\nprefer_trash = false\nquarantine_retention_days = 4\n"
        "quarantine_max_bytes = 99\nmap_max_points = 120\n"
        '[ui]\nshow_cleanup = true\nsafe_rendering = true\nmouse = false\n'
        'default_scan_path = "/somewhere"\nhostname_aware_paths = false\n'
    )

    config = load_config(path)

    assert (config.scan.max_depth, config.scan.workers) == (3, 2)
    assert config.scan.one_file_system is True
    assert config.scan.exclude_pseudo_filesystems is False
    assert config.scan.exclude_snapshot_dirs is False
    assert config.monitor.default_interval == 600
    assert config.monitor.max_watch_time == 60
    assert config.monitor.database_soft_budget == 5
    assert config.monitor.database_hard_budget == 6
    assert config.monitor.auto_start_in_tui is True
    assert config.cleanup.prefer_trash is False
    assert config.cleanup.quarantine_retention_days == 4
    assert config.cleanup.quarantine_max_bytes == 99
    assert config.cleanup.map_max_points == 120
    assert config.ui.show_cleanup is True
    assert config.ui.safe_rendering is True
    assert config.ui.mouse is False
    assert config.ui.default_scan_path == "/somewhere"
    assert config.ui.hostname_aware_paths is False


def test_a_command_exits_2_and_stores_nothing_for_an_unreadable_config(
    tmp_path, monkeypatch
):
    """It used to traceback with exit 1, or store the bad value."""
    from click.testing import CliRunner

    from disktide.__main__ import cli
    from disktide.repositories.sqlite import SQLiteSnapshotRepository

    for variable, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(variable, str(tmp_path / name))
    settings = tmp_path / "config" / "disktide"
    settings.mkdir(parents=True)
    (settings / "config.toml").write_text('[scan]\nworkers = "4"\n')
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("hello")

    result = CliRunner().invoke(cli, ["monitor", "add", str(tree), "--label", "t1"])

    assert result.exit_code == 2, result.output
    assert "workers" in result.output
    assert "Traceback" not in result.output
    database = tmp_path / "data" / "disktide" / "data.db"
    if database.exists():
        repository = SQLiteSnapshotRepository(path=str(database))
        repository.connect()
        try:
            assert repository.list_monitors() == []
        finally:
            repository.close()

"""Tests for configuration loading."""

from unittest.mock import patch

from fs_monitor.config import (
    load_config, save_config, AppConfig, HostPaths,
    get_effective_paths, parse_size, set_effective_paths,
)


class TestConfig:
    def test_default_config(self):
        config = load_config("/nonexistent/path/config.toml")
        assert isinstance(config, AppConfig)
        assert config.ui.default_viz == "sunburst"
        assert config.scan.max_depth is None
        assert config.scan.one_file_system is False
        assert config.scan.exclude_pseudo_filesystems is True
        assert config.monitor.default_interval == 21600
        assert config.monitor.database_soft_budget == 2 * 1024**3
        assert config.monitor.database_hard_budget == 3 * 1024**3
        assert config.monitor.auto_start_in_tui is False

    def test_parse_size(self):
        assert parse_size("4MiB") == 4 * 1024**2
        assert parse_size("1.5 GB") == 1_500_000_000
        assert parse_size("42") == 42

    def test_load_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[scan]
max_depth = 10
workers = 2
one_file_system = true
exclude_pseudo_filesystems = false

[monitor]
default_interval = 3600

[ui]
color_theme = "dark"
default_viz = "sunburst"
safe_rendering = true
""")
        config = load_config(str(config_file))
        assert config.scan.max_depth == 10
        assert config.scan.workers == 2
        assert config.scan.one_file_system is True
        assert config.scan.exclude_pseudo_filesystems is False
        assert config.monitor.default_interval == 3600
        assert config.ui.color_theme == "dark"
        assert config.ui.default_viz == "sunburst"
        assert config.ui.safe_rendering is True

    def test_partial_config(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[ui]
show_cleanup = true
""")
        config = load_config(str(config_file))
        # Specified value
        assert config.ui.show_cleanup is True
        # Defaults preserved
        assert config.scan.max_depth is None
        assert config.ui.default_viz == "sunburst"

    def test_retired_noop_keys_are_ignored(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[scan]
exclude_patterns = [".git"]

[cleanup]
enabled_rules = ["old_logs"]
disabled_rules = ["ide_caches"]
require_confirm_dangerous = false

[monitor]
snapshot_retention = 30
strict_path = true

[ui]
default_sort = "name"
show_hidden = true
""")

        config = load_config(config_file)

        assert not hasattr(config.scan, "exclude_patterns")
        assert config.cleanup.prefer_trash is True
        assert config.cleanup.quarantine_retention_days == 7
        assert config.cleanup.quarantine_max_bytes == 10 * 1024**3
        assert not hasattr(config.ui, "default_sort")
        assert not hasattr(config.ui, "show_hidden")

        save_config(config, config_file)
        saved = config_file.read_text()
        for retired_key in (
            "exclude_patterns",
            "enabled_rules",
            "disabled_rules",
            "require_confirm_dangerous",
            "snapshot_retention",
            "strict_path",
            "default_sort",
            "show_hidden",
        ):
            assert retired_key not in saved

    def test_cleanup_policy_roundtrip(self, tmp_path):
        config = AppConfig()
        config.cleanup.prefer_trash = False
        config.cleanup.quarantine_retention_days = 14
        config.cleanup.quarantine_max_bytes = 512 * 1024**2
        config.cleanup.disabled_rule_packs = ["node", "containers"]
        config.cleanup.map_max_points = 120

        config_file = tmp_path / "cleanup.toml"
        save_config(config, config_file)
        loaded = load_config(config_file)

        assert loaded.cleanup.prefer_trash is False
        assert loaded.cleanup.quarantine_retention_days == 14
        assert loaded.cleanup.quarantine_max_bytes == 512 * 1024**2
        assert loaded.cleanup.disabled_rule_packs == ["containers", "node"]
        assert loaded.cleanup.map_max_points == 120

    def test_save_and_reload(self, tmp_path):
        config = AppConfig()
        config.scan.workers = 4
        config.scan.max_depth = 5
        config.scan.one_file_system = True
        config.scan.exclude_pseudo_filesystems = False
        config.monitor.default_interval = 7200
        config.ui.color_theme = "dark"
        config.ui.default_viz = "sunburst"
        config.ui.safe_rendering = True

        config_file = tmp_path / "saved.toml"
        save_config(config, config_file)

        loaded = load_config(config_file)
        assert loaded.scan.workers == 4
        assert loaded.scan.max_depth == 5
        assert loaded.scan.one_file_system is True
        assert loaded.scan.exclude_pseudo_filesystems is False
        assert loaded.monitor.default_interval == 7200
        assert loaded.ui.color_theme == "dark"
        assert loaded.ui.default_viz == "sunburst"
        assert loaded.ui.safe_rendering is True

    def test_monitor_budget_roundtrip(self, tmp_path):
        config = AppConfig()
        config.monitor.database_soft_budget = 512 * 1024**2
        config.monitor.database_hard_budget = None
        config.monitor.auto_start_in_tui = True

        config_file = tmp_path / "monitor.toml"
        save_config(config, config_file)

        loaded = load_config(config_file)
        assert loaded.monitor.database_soft_budget == 512 * 1024**2
        assert loaded.monitor.database_hard_budget is None
        assert loaded.monitor.auto_start_in_tui is True
        assert "database_hard_budget = 0" in config_file.read_text()

    def test_save_creates_parent_dirs(self, tmp_path):
        config = AppConfig()
        config_file = tmp_path / "sub" / "dir" / "config.toml"
        save_config(config, config_file)
        assert config_file.exists()

    def test_save_with_none_optionals(self, tmp_path):
        config = AppConfig()
        config.scan.workers = None
        config.scan.max_depth = None
        config_file = tmp_path / "defaults.toml"
        save_config(config, config_file)

        loaded = load_config(config_file)
        assert loaded.scan.workers is None
        assert loaded.scan.max_depth is None

    def test_default_scan_path_roundtrip(self, tmp_path):
        config = AppConfig()
        config.ui.default_scan_path = "/home/user/projects"
        config_file = tmp_path / "config.toml"
        save_config(config, config_file)

        loaded = load_config(config_file)
        assert loaded.ui.default_scan_path == "/home/user/projects"

    def test_default_scan_path_none(self, tmp_path):
        config = AppConfig()
        assert config.ui.default_scan_path is None

        config_file = tmp_path / "config.toml"
        save_config(config, config_file)

        loaded = load_config(config_file)
        assert loaded.ui.default_scan_path is None

    def test_host_paths_roundtrip(self, tmp_path):
        config = AppConfig()
        config.host_paths["server1"] = HostPaths(
            default_scan_path="/data/projects",
            last_visited_path="/data/projects/frontend",
        )
        config.host_paths["server2"] = HostPaths(
            default_scan_path="/home/user",
        )

        config_file = tmp_path / "config.toml"
        save_config(config, config_file)

        loaded = load_config(config_file)
        assert "server1" in loaded.host_paths
        assert loaded.host_paths["server1"].default_scan_path == "/data/projects"
        assert loaded.host_paths["server1"].last_visited_path == "/data/projects/frontend"
        assert "server2" in loaded.host_paths
        assert loaded.host_paths["server2"].default_scan_path == "/home/user"
        assert loaded.host_paths["server2"].last_visited_path is None

    def test_hostname_aware_paths_default_true(self):
        config = AppConfig()
        assert config.ui.hostname_aware_paths is True

    @patch("fs_monitor.config.current_hostname", return_value="myhost")
    def test_get_effective_paths_hostname_aware(self, mock_host):
        config = AppConfig()
        config.host_paths["myhost"] = HostPaths(
            default_scan_path="/data",
            last_visited_path="/data/logs",
        )
        config.host_paths["otherhost"] = HostPaths(
            default_scan_path="/other",
        )

        paths = get_effective_paths(config)
        assert paths.default_scan_path == "/data"
        assert paths.last_visited_path == "/data/logs"

    @patch("fs_monitor.config.current_hostname", return_value="myhost")
    def test_get_effective_paths_legacy_fallback(self, mock_host):
        """No per-host entry but legacy default_scan_path exists."""
        config = AppConfig()
        config.ui.default_scan_path = "/legacy/path"

        paths = get_effective_paths(config)
        assert paths.default_scan_path == "/legacy/path"
        assert paths.last_visited_path is None

    @patch("fs_monitor.config.current_hostname", return_value="myhost")
    def test_get_effective_paths_legacy_mode(self, mock_host):
        """hostname_aware_paths=False uses _default key."""
        config = AppConfig()
        config.ui.hostname_aware_paths = False
        config.host_paths["_default"] = HostPaths(
            default_scan_path="/shared",
        )

        paths = get_effective_paths(config)
        assert paths.default_scan_path == "/shared"

    @patch("fs_monitor.config.current_hostname", return_value="myhost")
    def test_set_effective_paths(self, mock_host):
        config = AppConfig()
        hp = HostPaths(default_scan_path="/new", last_visited_path="/new/sub")
        set_effective_paths(config, hp)

        assert config.host_paths["myhost"] is hp
        assert config.ui.default_scan_path == "/new"

    def test_hostname_aware_paths_roundtrip(self, tmp_path):
        config = AppConfig()
        config.ui.hostname_aware_paths = False

        config_file = tmp_path / "config.toml"
        save_config(config, config_file)

        loaded = load_config(config_file)
        assert loaded.ui.hostname_aware_paths is False

    def test_show_cleanup_default_false(self):
        config = AppConfig()
        assert config.ui.show_cleanup is False

    def test_show_cleanup_roundtrip(self, tmp_path):
        config = AppConfig()
        config.ui.show_cleanup = True

        config_file = tmp_path / "config.toml"
        save_config(config, config_file)

        loaded = load_config(config_file)
        assert loaded.ui.show_cleanup is True

    def test_show_cleanup_false_not_written(self, tmp_path):
        """show_cleanup=False is omitted from TOML (only written when True)."""
        config = AppConfig()
        config.ui.show_cleanup = False

        config_file = tmp_path / "config.toml"
        save_config(config, config_file)

        content = config_file.read_text()
        assert "show_cleanup" not in content

        loaded = load_config(config_file)
        assert loaded.ui.show_cleanup is False


class TestLiveScanRender:
    """Round-trip + auto-gate for `ui.live_scan_render`."""

    def test_default_is_auto(self):
        config = AppConfig()
        assert config.ui.live_scan_render == "auto"

    def test_auto_omitted_from_toml(self, tmp_path):
        """The default value stays out of the file so older configs
        keep round-tripping cleanly."""
        config = AppConfig()
        config_file = tmp_path / "config.toml"
        save_config(config, config_file)
        assert "live_scan_render" not in config_file.read_text()

    def test_explicit_value_persists(self, tmp_path):
        """on/off survives a save/load cycle."""
        for value in ("on", "off"):
            config = AppConfig()
            config.ui.live_scan_render = value
            config_file = tmp_path / f"cfg_{value}.toml"
            save_config(config, config_file)
            assert f'live_scan_render = "{value}"' in config_file.read_text()
            loaded = load_config(config_file)
            assert loaded.ui.live_scan_render == value

    def test_invalid_value_in_toml_falls_back_to_auto(self, tmp_path):
        """A garbage value never leaves the user unable to use the
        feature, since the dropdown only writes auto/on/off."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[ui]
live_scan_render = "always-and-forever"
""")
        loaded = load_config(config_file)
        assert loaded.ui.live_scan_render == "auto"


class TestResolveLiveScanRender:
    """The auto-gate: terminal-size + cpu_count thresholds."""

    def test_on_overrides_gate(self):
        from fs_monitor.config import resolve_live_scan_render
        assert resolve_live_scan_render(
            "on", terminal_width=40, terminal_height=10, cpu_count=1
        ) is True

    def test_off_overrides_gate(self):
        from fs_monitor.config import resolve_live_scan_render
        assert resolve_live_scan_render(
            "off", terminal_width=200, terminal_height=60, cpu_count=16
        ) is False

    def test_auto_enabled_on_roomy_terminal(self):
        from fs_monitor.config import resolve_live_scan_render
        assert resolve_live_scan_render(
            "auto", terminal_width=100, terminal_height=40, cpu_count=8
        ) is True

    def test_auto_disabled_when_terminal_too_narrow(self):
        from fs_monitor.config import resolve_live_scan_render
        assert resolve_live_scan_render(
            "auto", terminal_width=60, terminal_height=40, cpu_count=8
        ) is False

    def test_auto_disabled_when_terminal_too_short(self):
        from fs_monitor.config import resolve_live_scan_render
        assert resolve_live_scan_render(
            "auto", terminal_width=120, terminal_height=15, cpu_count=8
        ) is False

    def test_auto_disabled_when_too_few_cpus(self):
        from fs_monitor.config import resolve_live_scan_render
        assert resolve_live_scan_render(
            "auto", terminal_width=120, terminal_height=40, cpu_count=2
        ) is False

    def test_unknown_value_treated_as_auto(self):
        """Any non-on/off value uses the gate, so a future bad config
        value can't permanently disable the feature."""
        from fs_monitor.config import resolve_live_scan_render
        assert resolve_live_scan_render(
            "yes-please", terminal_width=120, terminal_height=40, cpu_count=8
        ) is True
        assert resolve_live_scan_render(
            "", terminal_width=40, terminal_height=10, cpu_count=1
        ) is False

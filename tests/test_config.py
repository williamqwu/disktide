"""Tests for configuration loading."""

import os
import tempfile
from unittest.mock import patch

import pytest
from fs_monitor.config import (
    load_config, save_config, AppConfig, HostPaths,
    get_effective_paths, set_effective_paths,
)


class TestConfig:
    def test_default_config(self):
        config = load_config("/nonexistent/path/config.toml")
        assert isinstance(config, AppConfig)
        assert config.ui.default_sort == "size"
        assert config.ui.default_viz == "treemap"
        assert config.ui.show_hidden is False
        assert config.scan.follow_symlinks is False
        assert config.scan.max_depth is None
        assert config.cleanup.require_confirm_dangerous is True
        assert config.monitor.default_interval == 21600

    def test_load_from_toml(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[scan]
max_depth = 10
workers = 2
follow_symlinks = true
exclude_patterns = [".git", ".svn"]

[cleanup]
require_confirm_dangerous = false

[monitor]
default_interval = 3600

[ui]
color_theme = "dark"
default_sort = "name"
default_viz = "sunburst"
show_hidden = true
""")
        config = load_config(str(config_file))
        assert config.scan.max_depth == 10
        assert config.scan.workers == 2
        assert config.scan.follow_symlinks is True
        assert config.scan.exclude_patterns == [".git", ".svn"]
        assert config.cleanup.require_confirm_dangerous is False
        assert config.monitor.default_interval == 3600
        assert config.ui.color_theme == "dark"
        assert config.ui.default_sort == "name"
        assert config.ui.default_viz == "sunburst"
        assert config.ui.show_hidden is True

    def test_partial_config(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[ui]
show_hidden = true
""")
        config = load_config(str(config_file))
        # Specified value
        assert config.ui.show_hidden is True
        # Defaults preserved
        assert config.scan.max_depth is None
        assert config.ui.default_sort == "size"

    def test_save_and_reload(self, tmp_path):
        config = AppConfig()
        config.scan.workers = 4
        config.scan.max_depth = 5
        config.scan.follow_symlinks = True
        config.scan.exclude_patterns = [".git", "node_modules"]
        config.cleanup.require_confirm_dangerous = False
        config.monitor.default_interval = 7200
        config.ui.color_theme = "dark"
        config.ui.default_sort = "name"
        config.ui.default_viz = "sunburst"
        config.ui.show_hidden = True

        config_file = tmp_path / "saved.toml"
        save_config(config, config_file)

        loaded = load_config(config_file)
        assert loaded.scan.workers == 4
        assert loaded.scan.max_depth == 5
        assert loaded.scan.follow_symlinks is True
        assert loaded.scan.exclude_patterns == [".git", "node_modules"]
        assert loaded.cleanup.require_confirm_dangerous is False
        assert loaded.monitor.default_interval == 7200
        assert loaded.ui.color_theme == "dark"
        assert loaded.ui.default_sort == "name"
        assert loaded.ui.default_viz == "sunburst"
        assert loaded.ui.show_hidden is True

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

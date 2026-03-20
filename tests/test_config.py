"""Tests for configuration loading."""

import os
import tempfile

import pytest
from fs_monitor.config import load_config, save_config, AppConfig


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

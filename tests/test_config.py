"""Tests for configuration loading."""

import os
import tempfile

import pytest
from fs_monitor.config import load_config, AppConfig


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

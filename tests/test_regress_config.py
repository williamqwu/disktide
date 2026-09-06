"""Regressions for config values the loader and the writer have to survive."""

from __future__ import annotations

import pytest


# --- an unknown default_viz lands on the picker default -----------------


def test_an_unknown_default_viz_falls_back_to_the_picker_default(tmp_path):
    from disktide.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('[ui]\ndefault_viz = "foo"\n')

    assert load_config(path).ui.default_viz == "sunburst"

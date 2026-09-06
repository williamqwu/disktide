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

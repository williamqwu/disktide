from pathlib import Path

from disktide.config import cleanup_rule_directory, config_path
from disktide.paths import database_file, state_log_file


def _set_xdg_roots(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def test_new_install_uses_disktide_namespace(tmp_path, monkeypatch):
    _set_xdg_roots(monkeypatch, tmp_path)

    assert config_path() == tmp_path / "config" / "disktide" / "config.toml"
    assert cleanup_rule_directory() == (
        tmp_path / "config" / "disktide" / "cleanup-rules"
    )
    assert database_file() == tmp_path / "data" / "disktide" / "data.db"
    assert state_log_file() == (
        tmp_path / "state" / "disktide" / "disktide.log"
    )


def test_existing_sizetrail_paths_remain_active(tmp_path, monkeypatch):
    _set_xdg_roots(monkeypatch, tmp_path)
    previous_config = tmp_path / "config" / "sizetrail"
    previous_data = tmp_path / "data" / "sizetrail"
    previous_state = tmp_path / "state" / "sizetrail"
    previous_config.mkdir(parents=True)
    previous_data.mkdir(parents=True)
    previous_state.mkdir(parents=True)
    (previous_config / "config.toml").write_text("[ui]\n")
    (previous_data / "data.db").touch()
    (previous_state / "sizetrail.log").touch()

    assert config_path() == previous_config / "config.toml"
    assert cleanup_rule_directory() == previous_config / "cleanup-rules"
    assert database_file() == previous_data / "data.db"
    assert state_log_file() == previous_state / "sizetrail.log"


def test_existing_fsmonitor_paths_remain_active(tmp_path, monkeypatch):
    _set_xdg_roots(monkeypatch, tmp_path)
    legacy_config = tmp_path / "config" / "fsmonitor-cli"
    legacy_data = tmp_path / "data" / "fsmonitor-cli"
    legacy_state = tmp_path / "state" / "fsmonitor-cli"
    legacy_config.mkdir(parents=True)
    legacy_data.mkdir(parents=True)
    legacy_state.mkdir(parents=True)
    (legacy_config / "config.toml").write_text("[ui]\n")
    (legacy_data / "data.db").touch()
    (legacy_state / "fsmonitor.log").touch()

    assert config_path() == legacy_config / "config.toml"
    assert cleanup_rule_directory() == legacy_config / "cleanup-rules"
    assert database_file() == legacy_data / "data.db"
    assert state_log_file() == legacy_state / "fsmonitor.log"


def test_namespace_priority_prefers_disktide_then_sizetrail(tmp_path, monkeypatch):
    _set_xdg_roots(monkeypatch, tmp_path)
    for namespace in ("disktide", "sizetrail", "fsmonitor-cli"):
        config_dir = tmp_path / "config" / namespace
        data_dir = tmp_path / "data" / namespace
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text("[ui]\n")
        (data_dir / "data.db").touch()

    assert config_path() == tmp_path / "config" / "disktide" / "config.toml"
    assert database_file() == tmp_path / "data" / "disktide" / "data.db"

    (tmp_path / "config" / "disktide" / "config.toml").unlink()
    (tmp_path / "data" / "disktide" / "data.db").unlink()

    assert config_path() == tmp_path / "config" / "sizetrail" / "config.toml"
    assert database_file() == tmp_path / "data" / "sizetrail" / "data.db"

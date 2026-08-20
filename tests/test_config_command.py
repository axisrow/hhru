from __future__ import annotations

import argparse

import pytest
import yaml

from hhru_bot.commands.config_cmd import _lookup, _set, _unset, run

pytestmark = pytest.mark.unit


def _args(path, **values):
    defaults = {
        "config": str(path),
        "path": False,
        "edit": False,
        "key": None,
        "set": None,
        "unset": None,
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


def _config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "account:\n  storage_state_file: storage.json\nthrottle:\n  daily_apply_limit: 2\nresumes: []\n",
        encoding="utf-8",
    )
    return path


def test_config_get_set_and_unset(tmp_path, capsys):
    path = _config(tmp_path)
    run(_args(path, key="throttle.daily_apply_limit"))
    assert yaml.safe_load(capsys.readouterr().out) == 2
    run(_args(path, set=["throttle.daily_apply_limit", "7"]))
    assert yaml.safe_load(path.read_text())["throttle"]["daily_apply_limit"] == 7
    run(_args(path, unset="throttle.daily_apply_limit"))
    assert "daily_apply_limit" not in yaml.safe_load(path.read_text())["throttle"]


def test_config_write_uses_loader_validation_and_preserves_file(tmp_path, capsys):
    path = _config(tmp_path)
    before = path.read_text()
    assert run(_args(path, set=["account.storage_state_file", "null"])) is True
    assert "[FAIL]" in capsys.readouterr().out
    assert path.read_text() == before


def test_config_missing_key_is_concise_failure(tmp_path, capsys):
    assert run(_args(_config(tmp_path), key="missing.key")) is True
    output = capsys.readouterr().out
    assert output.startswith("[FAIL] Ключ не найден")
    assert "Traceback" not in output


def test_dotted_helpers():
    raw = {"a": {"b": 1}}
    assert _lookup(raw, "a.b") == 1
    _set(raw, "a.c", True)
    _unset(raw, "a.b")
    assert raw == {"a": {"c": True}}

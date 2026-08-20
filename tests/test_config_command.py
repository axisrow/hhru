from __future__ import annotations

import argparse
import os
from unittest.mock import patch

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
    assert capsys.readouterr().out == "2\n"
    run(_args(path, set=["throttle.daily_apply_limit", "7"]))
    assert yaml.safe_load(path.read_text())["throttle"]["daily_apply_limit"] == 7
    run(_args(path, unset="throttle.daily_apply_limit"))
    assert "daily_apply_limit" not in yaml.safe_load(path.read_text())["throttle"]


def test_config_get_scalar_has_no_yaml_document_marker(tmp_path, capsys):
    """A scalar value must print plainly, without yaml.safe_dump's trailing '...'."""
    run(_args(_config(tmp_path), key="throttle.daily_apply_limit"))
    assert capsys.readouterr().out == "2\n"


def test_config_get_mapping_still_dumps_as_yaml(tmp_path, capsys):
    run(_args(_config(tmp_path), key="account"))
    assert yaml.safe_load(capsys.readouterr().out) == {"storage_state_file": "storage.json"}


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


def test_config_edit_invalid_result_preserves_temp_buffer(tmp_path, capsys):
    """A rejected -e edit must not silently discard the user's hand-typed changes (#401 review)."""
    path = _config(tmp_path)
    before = path.read_text()

    def _break_the_config(argv, **_kwargs):
        # Simulate an editor that leaves the buffer with an invalid value.
        candidate = argv[-1]
        content = yaml.safe_load(open(candidate, encoding="utf-8"))
        content["account"]["storage_state_file"] = None
        with open(candidate, "w", encoding="utf-8") as stream:
            yaml.safe_dump(content, stream)

        class _Result:
            returncode = 0

        return _Result()

    with (
        patch.dict(os.environ, {"EDITOR": "true"}),
        patch("hhru_bot.commands.config_cmd.subprocess.run", side_effect=_break_the_config),
    ):
        assert run(_args(path, edit=True)) is True

    output = capsys.readouterr().out
    assert "[FAIL]" in output
    assert "Правки сохранены в" in output
    assert path.read_text() == before

    # The salvaged buffer path is quoted in the message; verify it actually survived.
    salvaged_path = output.split("Правки сохранены в", 1)[1].strip()
    assert os.path.exists(salvaged_path)
    salvaged = yaml.safe_load(open(salvaged_path, encoding="utf-8"))
    assert salvaged["account"]["storage_state_file"] is None


def test_config_edit_success_replaces_file_and_cleans_up_temp(tmp_path, capsys):
    path = _config(tmp_path)

    def _apply_edit(argv, **_kwargs):
        candidate = argv[-1]
        content = yaml.safe_load(open(candidate, encoding="utf-8"))
        content["throttle"]["daily_apply_limit"] = 9
        with open(candidate, "w", encoding="utf-8") as stream:
            yaml.safe_dump(content, stream)

        class _Result:
            returncode = 0

        return _Result()

    with (
        patch.dict(os.environ, {"EDITOR": "true"}),
        patch("hhru_bot.commands.config_cmd.subprocess.run", side_effect=_apply_edit),
    ):
        assert run(_args(path, edit=True)) is None

    assert "[OK]" in capsys.readouterr().out
    assert yaml.safe_load(path.read_text())["throttle"]["daily_apply_limit"] == 9
    assert list(path.parent.glob(".config.yaml.*")) == []


def test_dotted_helpers():
    raw = {"a": {"b": 1}}
    assert _lookup(raw, "a.b") == 1
    _set(raw, "a.c", True)
    _unset(raw, "a.b")
    assert raw == {"a": {"c": True}}

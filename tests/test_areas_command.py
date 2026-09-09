"""Тесты команды ``areas``: вывод и fail-closed классификация (без браузера).

Моки: launch_context/config подменяются monkeypatch (образец — tests/
test_call_api.py). Каталог — мини-копия живого /areas (id — строки, дети
в ключе "areas", parent_id из вложенности не используется).
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from hhru_bot.commands import areas as areas_cmd

pytestmark = pytest.mark.unit


class _Response:
    def __init__(self, *, ok: bool, status: int, text: str) -> None:
        self.ok = ok
        self.status = status
        self._text = text

    def text(self) -> str:
        return self._text


class _Request:
    def __init__(self, response: _Response) -> None:
        self._response = response
        self.calls: list[str] = []

    def get(self, url: str) -> _Response:
        self.calls.append(url)
        return self._response


class _Context:
    def __init__(self, response: _Response) -> None:
        self.request = _Request(response)

    def __enter__(self) -> _Context:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _tree_json() -> str:
    return json.dumps(
        [
            {
                "id": "113",
                "name": "Россия",
                "parent_id": None,
                "areas": [
                    {
                        "id": "1624",
                        "name": "Республика Татарстан",
                        "parent_id": "113",
                        "areas": [
                            {
                                "id": "1641",
                                "name": "Набережные Челны",
                                "parent_id": "1624",
                                "areas": [],
                            },
                            {"id": "88", "name": "Казань", "parent_id": "1624", "areas": []},
                            {
                                "id": "7242",
                                "name": "Верхние Челны",
                                "parent_id": "1624",
                                "areas": [],
                            },
                            {
                                "id": "7247",
                                "name": "Старые Челны",
                                "parent_id": "1624",
                                "areas": [],
                            },
                        ],
                    },
                    {
                        "id": "1586",
                        "name": "Самарская область",
                        "parent_id": "113",
                        "areas": [
                            {
                                "id": "3032",
                                "name": "Челно-Вершины",
                                "parent_id": "1586",
                                "areas": [],
                            },
                        ],
                    },
                    {
                        "id": "1898",
                        "name": "Орловская область",
                        "parent_id": "113",
                        "areas": [
                            {
                                "id": "11602",
                                "name": "Набережный",
                                "parent_id": "1898",
                                "areas": [],
                            },
                        ],
                    },
                    {
                        "id": "5001",
                        "name": "Подставная область",
                        "parent_id": "113",
                        "areas": [
                            {
                                "id": "90001",
                                "name": "Набережный",
                                "parent_id": "5001",
                                "areas": [],
                            },
                        ],
                    },
                    {
                        "id": "1317",
                        "name": "Пермский край",
                        "parent_id": "113",
                        "areas": [
                            {
                                "id": "13485",
                                "name": "Набережный (Пермский край)",
                                "parent_id": "1317",
                                "areas": [],
                            },
                        ],
                    },
                ],
            }
        ]
    )


def _patch_browser(
    monkeypatch, *, ok: bool = True, status: int = 200, text: str | None = None
) -> _Context:
    body = _tree_json() if text is None else text
    context = _Context(_Response(ok=ok, status=status, text=body))
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: context)
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: SimpleNamespace(storage_state_file="state.json", user_agent=None),
    )
    return context


def _args(name: str) -> argparse.Namespace:
    return argparse.Namespace(name=name, headless=True, config="config.yaml")


def test_register_wires_required_name_and_run() -> None:
    parser = argparse.ArgumentParser()
    areas_cmd.register(parser.add_subparsers())
    parsed = parser.parse_args(["areas", "--name", "Казань"])
    assert parsed.name == "Казань"
    assert parsed.func is areas_cmd.run


def test_run_fetches_public_areas_endpoint_via_context_request(monkeypatch, capsys) -> None:
    context = _patch_browser(monkeypatch)
    assert areas_cmd.run(_args("Казань")) is None
    assert context.request.calls == ["https://api.hh.ru/areas"]


def test_run_exact_unique_prints_ok_with_id_and_parent_chain(monkeypatch, capsys) -> None:
    _patch_browser(monkeypatch)
    assert areas_cmd.run(_args("Набережные Челны")) is None
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "1641" in out
    assert "Россия -> Республика Татарстан" in out


def test_run_exact_multiple_prints_candidate_table_and_fail(monkeypatch, capsys) -> None:
    _patch_browser(monkeypatch)
    assert areas_cmd.run(_args("набережный")) is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "[OK]" not in out
    assert "+---" in out  # ASCII-таблица кандидатов
    assert "11602" in out
    assert "90001" in out
    # n3: «Набережный (Пермский край)» — частичный кандидат (имя с суффиксом):
    # точное совпадение «набережный» его не матчит, в таблицу он не попадает
    assert "13485" not in out
    # m2: подсказка при exact_multiple — id из таблицы идёт в search, а не в --name
    assert "search --area" in out
    assert "уточните --name" not in out


def test_run_partial_only_lists_candidates_and_fails(monkeypatch, capsys) -> None:
    _patch_browser(monkeypatch)
    assert areas_cmd.run(_args("челны")) is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "[OK]" not in out
    for area_id in ("1641", "7242", "7247"):
        assert area_id in out
    assert "3032" not in out  # «Челно-Вершины» не содержит подстроку «челны»
    assert "уточните --name" in out  # m2: подсказка partial_only — уточнить имя


def test_run_no_match_prints_fail(monkeypatch, capsys) -> None:
    _patch_browser(monkeypatch)
    assert areas_cmd.run(_args("несуществующий-город-xyz")) is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "[OK]" not in out


def test_run_non_2xx_prints_fail_with_status(monkeypatch, capsys) -> None:
    _patch_browser(monkeypatch, ok=False, status=503, text="service down")
    assert areas_cmd.run(_args("Казань")) is True
    out, err = capsys.readouterr()
    assert "[FAIL]" in out
    assert "503" in out
    assert err == ""


def test_run_non_2xx_body_is_single_line_and_truncated(monkeypatch, capsys) -> None:
    """n2: тело [FAIL] — одна строка с head-усечением _FAIL_BODY_LIMIT
    (зеркало test_run_non_2xx_body_is_single_line_and_truncated у call-api)."""
    _patch_browser(monkeypatch, ok=False, status=500, text="x" * 400 + "\nline2")
    assert areas_cmd.run(_args("Казань")) is True
    out, err = capsys.readouterr()
    fail_lines = [line for line in out.splitlines() if line.startswith("[FAIL]")]
    assert len(fail_lines) == 1
    assert "x" * 300 in fail_lines[0]
    assert "x" * 301 not in fail_lines[0]
    assert err == ""


def test_run_invalid_json_prints_fail(monkeypatch, capsys) -> None:
    _patch_browser(monkeypatch, text="<<не json>>")
    assert areas_cmd.run(_args("Казань")) is True
    out, err = capsys.readouterr()
    assert "[FAIL]" in out
    assert "JSON" in out
    assert err == ""


def test_run_malformed_tree_prints_fail(monkeypatch, capsys) -> None:
    _patch_browser(monkeypatch, text=json.dumps([{"name": "Без id", "areas": []}]))
    assert areas_cmd.run(_args("Казань")) is True
    out, err = capsys.readouterr()
    assert "[FAIL]" in out
    assert err == ""

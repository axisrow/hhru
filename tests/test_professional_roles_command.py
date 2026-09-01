from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import hhru_bot.commands.professional_roles as command
from hhru_bot.professional_roles import (
    ProfessionalRole,
    VacancySearchRoleCatalog,
    write_professional_role_cache,
)

pytestmark = pytest.mark.unit


def _catalog(*, stale: bool = False) -> VacancySearchRoleCatalog:
    fetched_at = datetime.now(UTC) - (timedelta(days=8) if stale else timedelta())
    return VacancySearchRoleCatalog(
        fetched_at=fetched_at,
        categories=("Информационные технологии",),
        roles=(ProfessionalRole("96", "Программист, разработчик", "Информационные технологии"),),
    )


def _args(**overrides) -> argparse.Namespace:
    values = {
        "refresh": False,
        "query": ["разработчик"],
        "limit": 20,
        "config": "config.yaml",
        "headless": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_cached_search_never_opens_browser(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "professional_roles.json"
    write_professional_role_cache(_catalog(), cache)
    monkeypatch.setattr(command, "DEFAULT_CACHE_PATH", cache)
    monkeypatch.setattr(
        "hhru_bot.browser.launch_context",
        lambda *_a, **_kw: pytest.fail("локальный поиск не должен открывать браузер"),
    )

    assert command.run(_args()) is False

    out = capsys.readouterr().out
    assert "Программист, разработчик" in out
    assert "не автоматическая классификация" in out


def test_stale_cache_is_used_with_refresh_warning(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "professional_roles.json"
    write_professional_role_cache(_catalog(stale=True), cache)
    monkeypatch.setattr(command, "DEFAULT_CACHE_PATH", cache)

    assert command.run(_args()) is False

    out = capsys.readouterr().out
    assert "[WARN]" in out
    assert "professional-roles --refresh" in out
    assert "Программист, разработчик" in out


def test_missing_cache_prints_refresh_instruction(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(command, "DEFAULT_CACHE_PATH", tmp_path / "missing.json")

    assert command.run(_args()) is True

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "professional-roles --refresh" in out


def test_refresh_writes_complete_snapshot_then_searches(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "professional_roles.json"
    monkeypatch.setattr(command, "DEFAULT_CACHE_PATH", cache)
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.professional_roles.collect_vacancy_search_role_catalog",
        lambda _page: _catalog(),
    )

    assert command.run(_args(refresh=True)) is False

    assert cache.is_file()
    out = capsys.readouterr().out
    assert "Кэш каталога поиска вакансий обновлён" in out
    assert "Программист, разработчик" in out


def test_failed_refresh_preserves_previous_cache(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "professional_roles.json"
    write_professional_role_cache(_catalog(), cache)
    before = cache.read_bytes()
    monkeypatch.setattr(command, "DEFAULT_CACHE_PATH", cache)
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.professional_roles.collect_vacancy_search_role_catalog",
        lambda _page: (_ for _ in ()).throw(RuntimeError("неполный DOM")),
    )

    assert command.run(_args(refresh=True, query=None)) is True

    assert cache.read_bytes() == before
    out = capsys.readouterr().out
    assert "[FAIL] Кэш каталога поиска вакансий не обновлён" in out
    assert "Предыдущий валидный снимок сохранён" in out


def test_no_query_or_refresh_is_actionable(capsys):
    assert command.run(_args(query=None)) is True
    assert "--query" in capsys.readouterr().out

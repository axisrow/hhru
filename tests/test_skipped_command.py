"""Тесты read-команды skipped (#392).

Карточки вакансий команда берёт из общей market.db (#1109) — дефолтный путь
уводим в tmp, чтобы команда не трогала data/market.db cwd.
"""

from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import skipped as skipped_cmd
from hhru_bot.history import SKIP_REASONS, History
from hhru_bot.market_store import MarketStore

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolated_market(tmp_path, monkeypatch):
    monkeypatch.setattr("hhru_bot.market_store.DEFAULT_MARKET_PATH", tmp_path / "market.db")


def _args(history_path, **overrides):
    values = {"config": None, "history": str(history_path), "reason": None}
    values.update(overrides)
    return argparse.Namespace(**values)


def test_skipped_run_prints_joined_rows_and_filter(capsys, tmp_path):
    h = History(tmp_path / "h.db")
    MarketStore().upsert_vacancy_seen("v1", "python", "Python developer", "Acme")
    h.record_skip("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)
    h.record_skip("r1", "v2", SKIP_REASONS.HAS_QUESTIONS)

    skipped_cmd.run(_args(tmp_path / "h.db", reason=SKIP_REASONS.HAS_QUESTIONS))

    out = capsys.readouterr().out
    assert "Вакансия" in out
    assert "https://hh.ru/vacancy/v2" in out
    assert "v2" in out
    assert "v1" not in out
    assert "has_questions" in out


def test_skipped_run_empty_prints_header(capsys, tmp_path):
    skipped_cmd.run(_args(tmp_path / "h.db"))
    assert "Вакансия" in capsys.readouterr().out

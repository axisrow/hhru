"""Интеграционные тесты команды funnel (#13): funnel.run целиком.

Команда без браузера — только SQLite + форматтер воронки. Проверяем сам run()
с минимальным конфигом и seeded историей, через захват stdout.
"""

from __future__ import annotations

import argparse
import textwrap
from datetime import datetime, timedelta

import pytest

from hhru_bot.commands import funnel as funnel_cmd
from hhru_bot.history import History
from hhru_bot.market_store import MarketStore

pytestmark = pytest.mark.integration


def _iso_days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


@pytest.fixture(autouse=True)
def _isolated_market(tmp_path, monkeypatch):
    """funnel читает карточки из общей market.db (#1109) — уводим дефолтный
    путь в tmp, чтобы команда не открывала реальный data/market.db cwd."""
    monkeypatch.setattr("hhru_bot.market_store.DEFAULT_MARKET_PATH", tmp_path / "market.db")


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _minimal_config() -> str:
    # id (slug) ≠ resume_id (число из URL). apply пишет под resume.resume_id.
    return """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: python
            resume_url: "https://hh.ru/resume/12345"
            search:
              text: "python developer"
    """


def _args(config_path, history_path, **overrides) -> argparse.Namespace:
    base = {
        "config": str(config_path),
        "history": str(history_path),
        "resume": None,
        "search_query": False,
        "format": "table",
        "period": 30,
        "dead_days": 14,
        "dead": False,
        "rejections": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _seed(h: History):
    h.record_action("12345", "v1", "apply", "success")  # просмотрен
    h.record_action("12345", "v2", "apply", "success")  # приглашение
    h.record_action("12345", "v3", "apply", "success")  # без ответа
    h.upsert_response("v1", "Acme", "read", "/chat/v1")
    h.upsert_response("v2", "Acme", "invitation", "/chat/v2")


def test_funnel_run_table_seeded(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    _seed(h)

    funnel_cmd.run(_args(config, tmp_path / "h.db"))
    out = capsys.readouterr().out

    # шаги воронки видны + значения
    assert "Отправлено" in out
    assert "Просмотрено" in out
    assert "Приглашение" in out
    assert "Оффер" in out
    assert "12345" in out
    assert "3" in out  # sent


def test_funnel_run_empty_does_not_crash(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    funnel_cmd.run(_args(config, tmp_path / "h.db"))
    out = capsys.readouterr().out
    # даже на пустой истории не падает — шапка стабильна
    assert "Отправлено" in out


def test_funnel_run_md_format(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    _seed(h)

    funnel_cmd.run(_args(config, tmp_path / "h.db", format="md"))
    out = capsys.readouterr().out
    assert "|" in out
    assert "---" in out


def test_funnel_run_resume_filter(capsys, tmp_path):
    """--resume фильтрует по resume.resume_id (как stats)."""
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("12345", "v1", "apply", "success")
    h.record_action("99999", "v9", "apply", "success")  # другое резюме

    funnel_cmd.run(_args(config, tmp_path / "h.db", resume="python"))
    out = capsys.readouterr().out
    assert "12345" in out
    assert "99999" not in out


def test_funnel_run_unknown_resume_exits(capsys, tmp_path):

    config = _write_config(tmp_path, _minimal_config())
    with pytest.raises(SystemExit) as exc:
        funnel_cmd.run(_args(config, tmp_path / "h.db", resume="nope"))
    assert exc.value.code == 1


def test_funnel_run_dead_zone_flag(capsys, tmp_path):
    """--dead печатает «мёртвую зону» вместо/вместе с воронкой."""
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    _seed(h)

    funnel_cmd.run(_args(config, tmp_path / "h.db", dead=True))
    out = capsys.readouterr().out
    # подпись «мёртвой зоны»
    assert "мёртв" in out.lower() or "без ответ" in out.lower()


def test_funnel_run_dead_zone_respects_period(capsys, tmp_path):
    """#1273: --dead ограничен --period, как и обычная воронка.

    Отклик 45-дневной давности без ответа: в срезе за 1 день счётчики нулевые,
    без ограничения периода (period=0) — считается мёртвым.
    """
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("12345", "v702", "apply", "success")
    with h._connect() as conn:
        conn.execute(
            "UPDATE actions SET created_at = ? WHERE vacancy_id = ?",
            (_iso_days_ago(45), "v702"),
        )

    funnel_cmd.run(_args(config, tmp_path / "h.db", dead=True, period=1, format="md"))
    out = capsys.readouterr().out
    assert "| Всего откликов (старше порога) | 0 |" in out
    assert "| Без ответа («мёртвые») | 0 |" in out

    funnel_cmd.run(_args(config, tmp_path / "h.db", dead=True, period=0, format="md"))
    out = capsys.readouterr().out
    assert "| Всего откликов (старше порога) | 1 |" in out
    assert "| Без ответа («мёртвые») | 1 |" in out


def test_funnel_run_search_query(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("12345", "v1", "apply", "success")
    MarketStore().upsert_vacancy_seen(vacancy_id="v1", search_query="python")

    funnel_cmd.run(_args(config, tmp_path / "h.db", search_query=True, period=0))
    out = capsys.readouterr().out
    assert "Поисковый запрос" in out
    assert "python" in out
    assert "[INFO]" not in out  # все отклики атрибутированы — предупреждать не о чем


def test_funnel_run_search_query_warns_about_unattributed_applies(capsys, tmp_path):
    """apply/run не пишут карточки (#411 code review) — отклик без карточки в
    market.db не имеет атрибуции по запросу; funnel обязан предупредить.
    """
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("12345", "v1", "apply", "success")
    MarketStore().upsert_vacancy_seen(vacancy_id="v1", search_query="python")
    h.record_action("12345", "v2", "apply", "success")  # без карточки в market.db

    funnel_cmd.run(_args(config, tmp_path / "h.db", search_query=True, period=0))
    out = capsys.readouterr().out
    assert "[INFO]" in out
    assert "1" in out.split("[INFO]")[1]


def test_funnel_run_rejections(capsys, tmp_path):
    config = _write_config(tmp_path, _minimal_config())
    h = History(tmp_path / "h.db")
    h.record_action("12345", "v1", "apply", "success")
    MarketStore().upsert_vacancy_seen("v1", search_query="python", salary_from=100000)
    h.upsert_response("v1", "Acme", "discard", None, topic="1")

    funnel_cmd.run(_args(config, tmp_path / "h.db", rejections=True, period=0))
    out = capsys.readouterr().out

    assert "Работодатель" in out
    assert "Поисковый запрос" in out
    assert "Acme" in out
    assert "Отказов" in out


def test_funnel_run_huge_period_exits_with_clear_error(capsys, tmp_path):
    """#1279: --period больше представимого календарём datetime — [FAIL] на
    stderr + exit 1, а не OverflowError traceback из timedelta-вычитания.

    10**9 заодно превышает и диапазон самого timedelta (999999999 дней) —
    гард покрывает оба класса переполнения до вычисления.
    """
    config = _write_config(tmp_path, _minimal_config())

    with pytest.raises(SystemExit) as exc:
        funnel_cmd.run(_args(config, tmp_path / "h.db", period=10**9))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("[FAIL]")
    assert "--period" in err


def test_funnel_run_period_max_representable_boundary_works(capsys, tmp_path):
    """#1279: максимум представимого периода остаётся допустимым.

    Граница = дни от datetime.min до сегодня (минус 1 — она ползёт с датой,
    точное равенство флакует на полуночной смене дня между расчётом в тесте
    и в команде).
    """
    config = _write_config(tmp_path, _minimal_config())
    max_days = (datetime.now() - datetime.min).days

    funnel_cmd.run(_args(config, tmp_path / "h.db", period=max_days - 1))
    out = capsys.readouterr().out

    assert "Отправлено" in out  # таблица отрисована, а не отказ


def test_funnel_run_huge_dead_days_exits_with_clear_error(capsys, tmp_path):
    """#1279: --dead-days — та же арифметика timedelta в dead_responses,
    тот же понятный отказ вместо traceback."""
    config = _write_config(tmp_path, _minimal_config())

    with pytest.raises(SystemExit) as exc:
        funnel_cmd.run(_args(config, tmp_path / "h.db", dead=True, dead_days=10**9))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("[FAIL]")
    assert "--dead-days" in err

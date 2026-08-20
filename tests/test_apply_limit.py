"""Execution-level ``apply --limit`` semantics."""

from __future__ import annotations

import argparse

import pytest

from hhru_bot.apply import ApplyResult
from hhru_bot.commands import _common
from hhru_bot.commands import apply as apply_command
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.history import History
from hhru_bot.search import VacancyCard
from hhru_bot.throttle import Throttle

pytestmark = pytest.mark.integration


def _cards(count: int) -> list[VacancyCard]:
    return [
        VacancyCard(str(i), f"Python {i}", "Acme", f"https://hh.ru/vacancy/{i}")
        for i in range(count)
    ]


def _setup(tmp_path):
    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python"),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="hello",
        resumes=[resume],
    )
    history = History(tmp_path / "history.db")
    return config, resume, history, Throttle(config.throttle, history)


def _args(limit: int, *, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(dry_run=dry_run, limit=limit, max_pages=1, headless=True)


def _run(monkeypatch, tmp_path, results, limit, *, dry_run=False, cards=None):
    config, resume, history, throttle = _setup(tmp_path)
    cards = cards or _cards(len(results))
    calls = []

    monkeypatch.setattr(_common, "search_vacancies", lambda *a, **k: cards)
    monkeypatch.setattr(_common, "resolve_numeric_resume_ids", lambda _page: None)
    monkeypatch.setattr(Throttle, "wait", lambda *a, **k: None)

    def apply(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(args[1].vacancy_id)
        return results[len(calls) - 1]

    monkeypatch.setattr(_common, "apply_to_vacancy", apply)
    _common.run_apply_for_resume(
        object(), config, resume, history, throttle, _args(limit, dry_run=dry_run)
    )
    return calls


def test_limit_counts_success_after_skips(tmp_path, monkeypatch):
    cards = _cards(3)
    results = [
        ApplyResult(cards[0], False, "анкета", skipped=True),
        ApplyResult(cards[1], True, "success"),
        ApplyResult(cards[2], True, "success"),
    ]

    calls = _run(monkeypatch, tmp_path, results, 2, cards=cards)

    assert calls == ["0", "1", "2"]


def test_limit_counts_success_after_pre_submit_failure(tmp_path, monkeypatch):
    cards = _cards(3)
    results = [
        ApplyResult(cards[0], False, "кнопка не найдена"),
        ApplyResult(cards[1], True, "success"),
        ApplyResult(cards[2], True, "success"),
    ]

    calls = _run(monkeypatch, tmp_path, results, 2, cards=cards)

    assert calls == ["0", "1", "2"]


def test_limit_zero_processes_all_candidates(tmp_path, monkeypatch):
    cards = _cards(3)
    results = [ApplyResult(card, True, "success") for card in cards]

    calls = _run(monkeypatch, tmp_path, results, 0, cards=cards)

    assert calls == ["0", "1", "2"]


def test_limit_reports_fewer_when_candidates_run_out(tmp_path, monkeypatch, capsys):
    cards = _cards(2)
    results = [ApplyResult(card, True, "success") for card in cards]

    calls = _run(monkeypatch, tmp_path, results, 3, cards=cards)

    assert calls == ["0", "1"]
    # #441 review: недобор цели из-за реального исчерпания выдачи (has_next
    # ложный, а не потолок страниц) не должен предлагать поднять --max-pages.
    assert "--max-pages" not in capsys.readouterr().out


def test_uncertain_stops_batch_and_does_not_count(tmp_path, monkeypatch):
    cards = _cards(3)
    results = [
        ApplyResult(cards[0], False, "неопределённо", acted=True, uncertain=True),
        ApplyResult(cards[1], True, "success"),
    ]

    with pytest.raises(_common.ApplyRunStopped):
        _run(monkeypatch, tmp_path, results, 2, cards=cards)


def test_limit_is_applied_separately_per_resume(tmp_path, monkeypatch):
    resumes = [
        ResumeConfig(
            id="python-a",
            resume_url="https://hh.ru/resume/AAA111",
            search=SearchFilters(text="python-a"),
        ),
        ResumeConfig(
            id="python-b",
            resume_url="https://hh.ru/resume/BBB222",
            search=SearchFilters(text="python-b"),
        ),
    ]
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="hello",
        resumes=resumes,
    )
    calls: list[str] = []

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def new_page(self):
            return object()

    def search(_page, search, max_pages):  # noqa: ARG001
        return [
            VacancyCard(
                f"{search.text}-{i}",
                f"Python {i}",
                "Acme",
                f"https://hh.ru/vacancy/{search.text}-{i}",
            )
            for i in range(2)
        ]

    def apply(_page, card, *_args, **_kwargs):
        calls.append(card.vacancy_id)
        return ApplyResult(card, True, "dry-run")

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **k: _Context())
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.search.search_vacancies", search)
    monkeypatch.setattr(_common, "search_vacancies", search)
    monkeypatch.setattr(_common, "apply_to_vacancy", apply)
    monkeypatch.setattr(Throttle, "wait", lambda *a, **k: None)

    args = argparse.Namespace(
        config=None,
        history=str(tmp_path / "history.db"),
        account=None,
        resume=None,
        dry_run=True,
        headless=True,
        max_pages=1,
        limit=2,
        approved=None,
        permit=None,
        force=False,
    )

    assert apply_command.run(args) is False
    assert calls == ["python-a-0", "python-a-1", "python-b-0", "python-b-1"]


def test_lazy_search_stops_after_first_page_when_target_is_reached(tmp_path, monkeypatch):
    config, resume, history, throttle = _setup(tmp_path)
    cards = _cards(5)
    loaded: list[int] = []
    calls: list[str] = []

    def load(_page, _filters, page_num):  # noqa: ANN001
        loaded.append(page_num)
        return cards, True

    def apply(_page, card, *_args, **_kwargs):  # noqa: ANN001
        calls.append(card.vacancy_id)
        return ApplyResult(card, True, "success")

    monkeypatch.setattr(_common, "_load_apply_page", load)
    monkeypatch.setattr(_common, "resolve_numeric_resume_ids", lambda _page: None)
    monkeypatch.setattr(_common, "apply_to_vacancy", apply)
    monkeypatch.setattr(Throttle, "wait", lambda *a, **k: None)

    args = _args(5)
    args.max_pages = None
    assert _common.run_apply_for_resume(object(), config, resume, history, throttle, args) is False
    assert loaded == [0]
    assert calls == ["0", "1", "2", "3", "4"]


def test_lazy_search_opens_next_page_only_after_shortfall(tmp_path, monkeypatch):
    config, resume, history, throttle = _setup(tmp_path)
    first = _cards(2)
    second = _cards(3)
    loaded: list[int] = []

    def load(_page, _filters, page_num):  # noqa: ANN001
        loaded.append(page_num)
        return (first, True) if page_num == 0 else (second, False)

    monkeypatch.setattr(_common, "_load_apply_page", load)
    monkeypatch.setattr(_common, "resolve_numeric_resume_ids", lambda _page: None)
    monkeypatch.setattr(
        _common,
        "apply_to_vacancy",
        lambda _page, card, *_args, **_kwargs: ApplyResult(card, True, "success"),
    )
    monkeypatch.setattr(Throttle, "wait", lambda *a, **k: None)

    args = _args(5)
    args.max_pages = None
    assert _common.run_apply_for_resume(object(), config, resume, history, throttle, args) is False
    assert loaded == [0, 1]


def test_apply_auto_page_cap_uses_target_and_reserve():
    assert _common.apply_search_page_limit(argparse.Namespace(limit=0, max_pages=None)) == 1
    assert _common.apply_search_page_limit(argparse.Namespace(limit=5, max_pages=None)) == 2
    assert _common.apply_search_page_limit(argparse.Namespace(limit=10, max_pages=None)) == 3
    assert _common.apply_search_page_limit(argparse.Namespace(limit=10, max_pages=1)) == 1


def test_warns_when_page_cap_hit_short_of_target_with_more_pages_available(
    tmp_path, monkeypatch, capsys
):
    """#441 review: auto page-cap heuristic (ceil(limit/5)+1) can end the run
    short of --limit while has_next=True still holds — the user only sees the
    final count, with nothing pointing at "raise --max-pages" as the fix.
    """
    config, resume, history, throttle = _setup(tmp_path)
    # limit=10 -> auto cap is 3 pages (ceil(10/5)+1); each page yields only
    # 1 success (low per-page success rate) and still reports has_next=True,
    # so the run ends 7 short of the target while more results exist.
    loaded: list[int] = []

    def load(_page, _filters, page_num):  # noqa: ANN001
        loaded.append(page_num)
        return _cards(1), True

    monkeypatch.setattr(_common, "_load_apply_page", load)
    monkeypatch.setattr(_common, "resolve_numeric_resume_ids", lambda _page: None)
    monkeypatch.setattr(
        _common,
        "apply_to_vacancy",
        lambda _page, card, *_args, **_kwargs: ApplyResult(card, True, "success"),
    )
    monkeypatch.setattr(Throttle, "wait", lambda *a, **k: None)

    args = _args(10)
    args.max_pages = None
    _common.run_apply_for_resume(object(), config, resume, history, throttle, args)

    assert loaded == [0, 1, 2]
    out = capsys.readouterr().out
    assert "--max-pages" in out


class _PageWithoutLocator:
    """A plain command-level test double: no Playwright ``.locator`` at all."""


class _PageThatRaisesAttributeErrorFromDom:
    """Has ``.locator`` (looks like a real Page) but a downstream DOM bug in
    it raises AttributeError for a reason unrelated to test-double shape —
    #441 review: the old bare ``except AttributeError`` swallowed this and
    silently guessed has_next from card count instead of surfacing the bug.
    """

    def locator(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AttributeError("'NoneType' object has no attribute 'count'")


def test_load_apply_page_fallback_only_for_missing_locator(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "search_vacancies", lambda *a, **k: _cards(21))

    cards, has_next = _common._load_apply_page(
        _PageWithoutLocator(), SearchFilters(text="python"), 0
    )
    assert has_next is True  # 21 >= 20 cards -> fallback heuristic kicks in
    assert len(cards) == 21


def test_load_apply_page_does_not_mask_real_attribute_error(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "search_vacancies", lambda *a, **k: _cards(3))

    with pytest.raises(AttributeError):
        _common._load_apply_page(
            _PageThatRaisesAttributeErrorFromDom(), SearchFilters(text="python"), 0
        )

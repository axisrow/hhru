"""Tests for the read-only bulk questionnaire detector."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hhru_bot.ai.questions import Question
from hhru_bot.apply import questionnaire
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit


def _card(vacancy_id: str) -> VacancyCard:
    return VacancyCard(
        vacancy_id, f"Python {vacancy_id}", "ACME", f"https://hh.ru/vacancy/{vacancy_id}"
    )


def test_scan_extracts_task_body_without_artifacts_or_submit(monkeypatch):
    calls = []

    class Page:
        def set_default_navigation_timeout(self, timeout):
            calls.append(("timeout", timeout))

    page = Page()
    card = _card("101")
    monkeypatch.setattr(questionnaire, "goto_hh", lambda page, url: calls.append(("goto", url)))
    monkeypatch.setattr(questionnaire, "has_login_form", lambda page: False)
    monkeypatch.setattr(questionnaire, "wait_apply_button", lambda page, **kwargs: True)

    def navigate(page, vacancy_id, **kwargs):
        assert vacancy_id == "101"
        assert kwargs["dump_diagnostics"] is False
        calls.append(("form", kwargs))
        return True

    monkeypatch.setattr(questionnaire, "navigate_to_response_form", navigate)
    monkeypatch.setattr(
        questionnaire,
        "detect_questions",
        lambda page: SimpleNamespace(indeterminate=False, has_questions=True, reason="task-body"),
    )
    question = Question(0, "В каком городе вы работаете?", "text")
    monkeypatch.setattr(questionnaire, "extract_questions", lambda page: ([question], 1))
    monkeypatch.setattr(
        "hhru_bot.apply.probe.dump_probe_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("artifact dump")),
    )

    result = questionnaire.scan_questionnaire(page, card)

    assert result.status == questionnaire.QUESTIONNAIRE
    assert result.questions == (question,)
    assert result.total_bodies == 1
    assert not result.retryable
    assert not any(call[0] == "submit" for call in calls)


def test_timeout_is_unknown_and_retryable_not_no_questionnaire(monkeypatch):
    class Page:
        def set_default_navigation_timeout(self, timeout):
            pass

    monkeypatch.setattr(questionnaire, "goto_hh", lambda page, url: None)
    monkeypatch.setattr(questionnaire, "has_login_form", lambda page: False)
    monkeypatch.setattr(questionnaire, "wait_apply_button", lambda page, **kwargs: False)

    result = questionnaire.scan_questionnaire(Page(), _card("102"))

    assert result.status == questionnaire.UNKNOWN
    assert result.retryable is True
    assert result.status != questionnaire.NO_QUESTIONNAIRE


def _config(resumes):
    from hhru_bot.config import ThrottleConfig

    return SimpleNamespace(
        storage_state_file="state",
        resumes=resumes,
        get_resume=lambda key: resumes[0],
        throttle=ThrottleConfig(min_delay_seconds=1, max_delay_seconds=2),
    )


def test_bulk_uses_one_page_dedupes_and_retries_without_history(monkeypatch, capsys):
    card1 = _card("201")
    card2 = _card("202")
    resume = SimpleNamespace(id="python", search=object())
    config = _config([resume])
    page = object()
    pages = []
    scan_calls = []

    @contextmanager
    def context_manager(*args, **kwargs):
        class Context:
            def new_page(self):
                pages.append(page)
                return page

        yield Context()

    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: config)
    monkeypatch.setattr("hhru_bot.browser.launch_context", context_manager)
    monkeypatch.setattr(
        "hhru_bot.search.search_vacancies", lambda page, search, max_pages: [card1, card1, card2]
    )

    def fake_scan(page_arg, vacancy, *, timeout_ms, form_timeout_ms):
        scan_calls.append((page_arg, vacancy.vacancy_id, timeout_ms, form_timeout_ms))
        if vacancy.vacancy_id == "201" and timeout_ms == questionnaire.FAST_TIMEOUT_MS:
            return questionnaire.QuestionnaireScanResult(
                vacancy, questionnaire.UNKNOWN, "timeout", retryable=True
            )
        return questionnaire.QuestionnaireScanResult(vacancy, questionnaire.NO_QUESTIONNAIRE)

    monkeypatch.setattr("hhru_bot.apply.questionnaire.scan_questionnaire", fake_scan)
    sleep_calls = []
    monkeypatch.setattr(
        "hhru_bot.commands.probe.time.sleep", lambda seconds: sleep_calls.append(seconds)
    )
    monkeypatch.setattr("hhru_bot.commands.probe.random.uniform", lambda lo, hi: 1.5)
    from hhru_bot.commands import probe

    args = SimpleNamespace(
        config="config.yaml",
        resume="python",
        max_pages=10,
        headless=True,
        vacancy_id=None,
        vacancy_url=None,
    )
    assert probe.run_questionnaires(args) is False

    assert pages == [page]
    assert [call[1] for call in scan_calls] == ["201", "202", "201"]
    assert scan_calls[0][2:] == (questionnaire.FAST_TIMEOUT_MS, questionnaire.FAST_FORM_TIMEOUT_MS)
    assert scan_calls[-1][2:] == (90_000, 10_000)
    output = capsys.readouterr().out
    assert "no_questionnaire" in output
    # #433 cycle-review: клик по кнопке отклика на каждой вакансии подряд без
    # паузы выглядит для анти-фрод системы hh.ru как автоматизация (нарушает
    # базовый принцип CLAUDE.md). Пауза должна быть между каждым сканом.
    assert len(sleep_calls) == len(scan_calls)
    assert all(delay == 1.5 for delay in sleep_calls)


def test_bulk_counts_unauthenticated_as_failure(monkeypatch, capsys):
    card = _card("301")
    resume = SimpleNamespace(id="python", search=object())
    config = _config([resume])
    page = object()

    @contextmanager
    def context_manager(*args, **kwargs):
        class Context:
            def new_page(self):
                return page

        yield Context()

    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: config)
    monkeypatch.setattr("hhru_bot.browser.launch_context", context_manager)
    monkeypatch.setattr("hhru_bot.search.search_vacancies", lambda page, search, max_pages: [card])
    monkeypatch.setattr(
        "hhru_bot.apply.questionnaire.scan_questionnaire",
        lambda page_arg, vacancy, **kwargs: questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.UNAUTHENTICATED, "требуется авторизация"
        ),
    )
    monkeypatch.setattr("hhru_bot.commands.probe.time.sleep", lambda seconds: None)
    from hhru_bot.commands import probe

    args = SimpleNamespace(
        config="config.yaml",
        resume="python",
        max_pages=10,
        headless=True,
        vacancy_id=None,
        vacancy_url=None,
    )
    # #433 cycle-review: потеря аутентификации посреди прогона должна давать
    # [FAIL] и ненулевой exit-статус, а не тихий success с неполным сканом.
    assert probe.run_questionnaires(args) is True
    output = capsys.readouterr().out
    assert "unauthenticated" in output

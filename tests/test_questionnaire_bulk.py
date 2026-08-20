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
    monkeypatch.setattr(questionnaire, "require_authenticated_page", lambda page: None)
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
    monkeypatch.setattr(questionnaire, "require_authenticated_page", lambda page: None)
    monkeypatch.setattr(questionnaire, "wait_apply_button", lambda page, **kwargs: False)
    monkeypatch.setattr(questionnaire, "check_already_responded", lambda page, vacancy: None)

    result = questionnaire.scan_questionnaire(Page(), _card("102"))

    assert result.status == questionnaire.UNKNOWN
    assert result.retryable is True
    assert result.status != questionnaire.NO_QUESTIONNAIRE


def test_scan_uses_two_signal_auth_check_not_bare_login_form(monkeypatch):
    from hhru_bot.browser import NotAuthenticated

    class Page:
        def set_default_navigation_timeout(self, timeout):
            pass

    monkeypatch.setattr(questionnaire, "goto_hh", lambda page, url: None)

    def fail_auth(page):
        # #433 cycle-review: has_login_form в одиночку не доказывает валидную
        # сессию (см. её собственный докстринг) — контракт require_authenticated_page
        # (cookie + отсутствие login-формы) обязателен здесь, как и в остальном проекте.
        raise NotAuthenticated("cookie hhtoken не найден")

    monkeypatch.setattr(questionnaire, "require_authenticated_page", fail_auth)

    result = questionnaire.scan_questionnaire(Page(), _card("103"))

    assert result.status == questionnaire.UNAUTHENTICATED


def test_scan_distinguishes_already_responded_from_timeout(monkeypatch):
    class Page:
        def set_default_navigation_timeout(self, timeout):
            pass

    monkeypatch.setattr(questionnaire, "goto_hh", lambda page, url: None)
    monkeypatch.setattr(questionnaire, "require_authenticated_page", lambda page: None)
    monkeypatch.setattr(questionnaire, "wait_apply_button", lambda page, **kwargs: False)
    # #433 cycle-review round 3: wait_apply_button() возвращает False для
    # реального timeout И для штатного «уже откликались» (общий локатор) —
    # без отдельной проверки обычная выдача с прежними откликами валила бы
    # весь bulk-скан как неподтверждённую.
    monkeypatch.setattr(
        questionnaire, "check_already_responded", lambda page, vacancy: "уже откликались"
    )

    result = questionnaire.scan_questionnaire(Page(), _card("104"))

    assert result.status == questionnaire.ALREADY_RESPONDED
    assert result.retryable is False


def test_scan_treats_partial_question_extraction_as_unknown(monkeypatch):
    class Page:
        def set_default_navigation_timeout(self, timeout):
            pass

    monkeypatch.setattr(questionnaire, "goto_hh", lambda page, url: None)
    monkeypatch.setattr(questionnaire, "require_authenticated_page", lambda page: None)
    monkeypatch.setattr(questionnaire, "wait_apply_button", lambda page, **kwargs: True)
    monkeypatch.setattr(
        questionnaire, "navigate_to_response_form", lambda page, vacancy_id, **kwargs: True
    )
    monkeypatch.setattr(
        questionnaire,
        "detect_questions",
        lambda page: SimpleNamespace(indeterminate=False, has_questions=True, reason="task-body"),
    )
    question = Question(0, "В каком городе вы работаете?", "text")
    # #433 cycle-review round 3: extract_questions() может тихо отбросить тело
    # вопроса (2 body в DOM, 1 распознан) — сравнение len(questions) !=
    # total_bodies обязательно, иначе урезанный список репортится как полная
    # анкета (тот же инвариант, что и в apply/pipeline.py).
    monkeypatch.setattr(questionnaire, "extract_questions", lambda page: ([question], 2))

    result = questionnaire.scan_questionnaire(Page(), _card("105"))

    assert result.status == questionnaire.UNKNOWN
    assert result.questions == ()


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


def test_bulk_counts_unknown_as_failure(monkeypatch, capsys):
    card = _card("401")
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
            vacancy, questionnaire.UNKNOWN, "ошибка проверки", retryable=False
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
    # #433 cycle-review: массово неподтверждённые (unknown) результаты — это
    # неполный скан, не «анкет нет»; молчаливый success замаскировал бы это
    # (как и у unauthenticated), в отличие от run_healthcheck, где unreachable
    # тоже считается провалом.
    assert probe.run_questionnaires(args) is True
    output = capsys.readouterr().out
    assert "unknown" in output


def test_bulk_already_responded_does_not_fail_the_scan(monkeypatch, capsys):
    card = _card("501")
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
            vacancy, questionnaire.ALREADY_RESPONDED, "уже откликались"
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
    # #433 cycle-review round 3: already_responded — подтверждённое состояние
    # (не timeout/drift), обычная выдача с прежними откликами не должна
    # проваливать весь bulk-скан.
    assert probe.run_questionnaires(args) is False
    output = capsys.readouterr().out
    assert "already_responded" in output


# --- #443 Этап 2: группировка одинаковых вопросов между вакансиями ---


def test_group_questions_merges_identical_text_and_options_across_vacancies():
    q = Question(0, "Готовы к переезду?", "choice", ("Да", "Нет"), is_radio=True)
    results = [
        questionnaire.QuestionnaireScanResult(_card("1"), questionnaire.QUESTIONNAIRE, "", (q,), 1),
        questionnaire.QuestionnaireScanResult(_card("2"), questionnaire.QUESTIONNAIRE, "", (q,), 1),
    ]
    groups = questionnaire.group_questions(results)
    assert len(groups) == 1
    assert groups[0].vacancy_ids == ("1", "2")
    assert groups[0].text == "Готовы к переезду?"
    assert groups[0].options == ("Да", "Нет")


def test_group_questions_normalizes_whitespace_and_case():
    q1 = Question(0, "  Готовы к переезду?  ", "text")
    q2 = Question(0, "готовы к переезду?", "text")
    results = [
        questionnaire.QuestionnaireScanResult(
            _card("1"), questionnaire.QUESTIONNAIRE, "", (q1,), 1
        ),
        questionnaire.QuestionnaireScanResult(
            _card("2"), questionnaire.QUESTIONNAIRE, "", (q2,), 1
        ),
    ]
    groups = questionnaire.group_questions(results)
    assert len(groups) == 1
    assert groups[0].vacancy_ids == ("1", "2")


def test_group_questions_keeps_same_text_with_different_options_separate():
    # Same wording, different answer choices for two different employers —
    # merging would falsely claim both vacancies accept the same options.
    q1 = Question(0, "Готовы к переезду?", "choice", ("Да", "Нет"), is_radio=True)
    q2 = Question(0, "Готовы к переезду?", "choice", ("Да", "Нет", "Обсудим"), is_radio=True)
    results = [
        questionnaire.QuestionnaireScanResult(
            _card("1"), questionnaire.QUESTIONNAIRE, "", (q1,), 1
        ),
        questionnaire.QuestionnaireScanResult(
            _card("2"), questionnaire.QUESTIONNAIRE, "", (q2,), 1
        ),
    ]
    groups = questionnaire.group_questions(results)
    assert len(groups) == 2
    assert {g.vacancy_ids for g in groups} == {("1",), ("2",)}


def test_group_questions_ignores_duplicate_vacancy_id_and_non_questionnaire_status():
    q = Question(0, "Готовы к переезду?", "text")
    results = [
        questionnaire.QuestionnaireScanResult(_card("1"), questionnaire.QUESTIONNAIRE, "", (q,), 1),
        # Same vacancy re-scanned (retry path) must not double-count.
        questionnaire.QuestionnaireScanResult(_card("1"), questionnaire.QUESTIONNAIRE, "", (q,), 1),
        questionnaire.QuestionnaireScanResult(_card("2"), questionnaire.NO_QUESTIONNAIRE),
        questionnaire.QuestionnaireScanResult(_card("3"), questionnaire.UNKNOWN, "timeout"),
    ]
    groups = questionnaire.group_questions(results)
    assert len(groups) == 1
    assert groups[0].vacancy_ids == ("1",)


def test_group_questions_empty_input_returns_empty_list():
    assert questionnaire.group_questions([]) == []

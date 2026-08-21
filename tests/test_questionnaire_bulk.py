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
        "hhru_bot.search.search_vacancies",
        lambda page, search, max_pages, **kwargs: [card1, card1, card2],
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
        start_page=0,
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
    monkeypatch.setattr(
        "hhru_bot.search.search_vacancies", lambda page, search, max_pages, **kwargs: [card]
    )
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
    monkeypatch.setattr(
        "hhru_bot.search.search_vacancies", lambda page, search, max_pages, **kwargs: [card]
    )
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
    monkeypatch.setattr(
        "hhru_bot.search.search_vacancies", lambda page, search, max_pages, **kwargs: [card]
    )
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


def test_group_questions_merges_same_options_in_different_order():
    # cycle-review PR #444: same text, same OPTION SET, but different order —
    # the key must canonicalize (sort) options, otherwise identical questions
    # split into separate groups whenever hh.ru renders options in a
    # different order across vacancies (undercounting duplicate questions).
    q1 = Question(0, "Готовы к переезду?", "choice", ("Да", "Нет"), is_radio=True)
    q2 = Question(0, "Готовы к переезду?", "choice", ("Нет", "Да"), is_radio=True)
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


# --- #448: потоковый прогресс, счётчики, лимит, прерывание ---


def _bulk_args(**overrides):
    args = SimpleNamespace(
        config="config.yaml",
        resume="python",
        max_pages=10,
        headless=True,
        vacancy_id=None,
        vacancy_url=None,
        limit_questionnaires=0,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _bulk_env(monkeypatch, cards, scan, *, events=None):
    """Wire run_questionnaires to fakes; optionally record a print/scan event log."""
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
    monkeypatch.setattr(
        "hhru_bot.search.search_vacancies", lambda page, search, max_pages, **kwargs: cards
    )
    monkeypatch.setattr("hhru_bot.apply.questionnaire.scan_questionnaire", scan)
    monkeypatch.setattr("hhru_bot.commands.probe.time.sleep", lambda seconds: None)
    if events is not None:
        import builtins

        real_print = builtins.print

        def recording_print(*args, **kwargs):
            events.append(("print", " ".join(str(a) for a in args)))
            real_print(*args, **kwargs)

        monkeypatch.setattr(builtins, "print", recording_print)
    from hhru_bot.commands import probe

    return probe


def test_bulk_streams_each_result_before_scanning_the_next_vacancy(monkeypatch, capsys):
    # #448 headline requirement: результат первой вакансии должен быть напечатан
    # ДО того, как начнётся проверка второй. Проверка итогового текста этого не
    # ловит — старая буферизованная реализация печатала бы то же самое в конце,
    # поэтому тест смотрит на ПОРЯДОК событий, а не на содержимое вывода.
    cards = [_card("601"), _card("602")]
    events = []
    question = Question(0, "Готовы к переезду?", "text")

    def scan(page_arg, vacancy, **kwargs):
        events.append(("scan", vacancy.vacancy_id))
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.QUESTIONNAIRE, "task-body", (question,), 1
        )

    probe = _bulk_env(monkeypatch, cards, scan, events=events)
    probe.run_questionnaires(_bulk_args())
    capsys.readouterr()

    scan_602 = events.index(("scan", "602"))
    printed_601 = [
        index
        for index, (kind, text) in enumerate(events)
        if kind == "print" and "601" in text and "[OK] анкета" in text
    ]
    assert printed_601, "подтверждённая анкета первой вакансии не напечатана"
    assert printed_601[0] < scan_602
    # Текст вопроса и ссылка на вакансию печатаются сразу, а не только в итоге.
    streamed = [text for kind, text in events[:scan_602] if kind == "print"]
    assert any("Готовы к переезду?" in text for text in streamed)
    assert any("https://hh.ru/vacancy/601" in text for text in streamed)


def test_bulk_prints_retry_progress_with_the_real_vacancy_position(monkeypatch, capsys):
    # Долгий retry должен быть виден в прогрессе (#448), и номер вакансии в
    # строке прогресса — её собственная позиция: retry вакансии 1 из 3 не может
    # печататься как «проверено 3/3».
    cards = [_card("701"), _card("702"), _card("703")]
    seen = []

    def scan(page_arg, vacancy, *, timeout_ms, form_timeout_ms):
        seen.append((vacancy.vacancy_id, timeout_ms))
        if vacancy.vacancy_id == "701" and timeout_ms == questionnaire.FAST_TIMEOUT_MS:
            return questionnaire.QuestionnaireScanResult(
                vacancy, questionnaire.UNKNOWN, "timeout", retryable=True
            )
        return questionnaire.QuestionnaireScanResult(vacancy, questionnaire.NO_QUESTIONNAIRE)

    probe = _bulk_env(monkeypatch, cards, scan)
    probe.run_questionnaires(_bulk_args())
    output = capsys.readouterr().out

    assert "[INFO] retry вакансии 701" in output
    lines = output.splitlines()
    retry_index = next(i for i, line in enumerate(lines) if "retry вакансии 701" in line)
    progress_after_retry = next(
        line for line in lines[retry_index + 1 :] if line.startswith("[INFO] проверено")
    )
    # Позиция самой перепроверенной вакансии (1 из 3), а не длина списка
    # результатов: len(resume_results) напечатал бы «проверено 3/3».
    assert progress_after_retry.startswith("[INFO] проверено 1/3: ")
    assert "проверено 3/3: no_questionnaire" in output


def test_bulk_final_counters_report_every_status(monkeypatch, capsys):
    # #448: отдельный счётчик проверено/анкеты/без анкеты/уже откликались/unknown.
    statuses = {
        "801": questionnaire.QUESTIONNAIRE,
        "802": questionnaire.NO_QUESTIONNAIRE,
        "803": questionnaire.ALREADY_RESPONDED,
        "804": questionnaire.UNKNOWN,
    }
    cards = [_card(vacancy_id) for vacancy_id in statuses]

    def scan(page_arg, vacancy, **kwargs):
        return questionnaire.QuestionnaireScanResult(
            vacancy, statuses[vacancy.vacancy_id], "", retryable=False
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    probe.run_questionnaires(_bulk_args())
    output = capsys.readouterr().out

    assert "вакансий 4" in output
    assert "анкет 1" in output
    assert "без анкеты 1" in output
    assert "уже откликались 1" in output
    assert "unknown 1" in output
    assert "требует авторизации 0" in output


def test_limit_questionnaires_stops_scanning_after_n_confirmed(monkeypatch, capsys):
    # #448: --limit-questionnaires N завершает скан. Проверяем число ВЫЗОВОВ
    # scan_questionnaire — только оно доказывает, что скан остановился, а не
    # просто напечатал результат раньше.
    cards = [_card("901"), _card("902"), _card("903")]
    scanned = []

    def scan(page_arg, vacancy, **kwargs):
        scanned.append(vacancy.vacancy_id)
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.QUESTIONNAIRE, "task-body", (), 0
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    assert probe.run_questionnaires(_bulk_args(limit_questionnaires=1)) is False
    capsys.readouterr()

    assert scanned == ["901"]


def test_limit_zero_scans_every_vacancy(monkeypatch, capsys):
    cards = [_card("911"), _card("912"), _card("913")]
    scanned = []

    def scan(page_arg, vacancy, **kwargs):
        scanned.append(vacancy.vacancy_id)
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.QUESTIONNAIRE, "task-body", (), 0
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    probe.run_questionnaires(_bulk_args(limit_questionnaires=0))
    capsys.readouterr()

    assert scanned == ["911", "912", "913"]


def test_negative_limit_is_rejected_before_launching_a_browser(monkeypatch, capsys):
    resume = SimpleNamespace(id="python", search=object())
    config = _config([resume])
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: config)

    @contextmanager
    def forbidden_context(*args, **kwargs):
        raise AssertionError("браузер не должен запускаться при неверном лимите")
        yield  # pragma: no cover

    monkeypatch.setattr("hhru_bot.browser.launch_context", forbidden_context)
    from hhru_bot.commands import probe

    assert probe.run_questionnaires(_bulk_args(limit_questionnaires=-1)) is True
    assert "--limit-questionnaires" in capsys.readouterr().err


def test_keyboard_interrupt_prints_partial_report_without_traceback(monkeypatch, capsys):
    # #448: прерывание печатает итог уже обработанной части, без traceback.
    cards = [_card("921"), _card("922"), _card("923")]

    def scan(page_arg, vacancy, **kwargs):
        if vacancy.vacancy_id == "922":
            raise KeyboardInterrupt
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.QUESTIONNAIRE, "task-body", (), 0
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    from hhru_bot.exit_codes import CommandExitCode

    assert probe.run_questionnaires(_bulk_args()) is CommandExitCode.SIGINT
    output = capsys.readouterr().out

    assert "прерван пользователем" in output
    assert "вакансий 1" in output
    assert "анкет 1" in output


def test_interrupt_does_not_mask_lost_authentication(monkeypatch, capsys):
    # Ctrl-C имеет приоритет над fail-closed причиной: владелец #452 закрепил
    # единый exit 130 для любого пользовательского прерывания, даже если до
    # него уже обнаружена потеря сессии. [FAIL] и частичный отчёт сохраняются.
    cards = [_card("931"), _card("932")]

    def scan(page_arg, vacancy, **kwargs):
        if vacancy.vacancy_id == "932":
            raise KeyboardInterrupt
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.UNAUTHENTICATED, "требуется авторизация"
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    from hhru_bot.exit_codes import CommandExitCode

    assert probe.run_questionnaires(_bulk_args()) is CommandExitCode.SIGINT
    output = capsys.readouterr().out

    assert "прерван пользователем" in output
    assert "[FAIL] сессия истекла во время прогона" in output


def test_limit_still_drains_pending_retries_before_stopping(monkeypatch, capsys):
    # cycle-review PR #450: лимит — условие «не начинать новые вакансии», а не
    # повод бросить уже накопленную неопределённость. Без слива retry_ids
    # вакансия с транзиентным unknown навсегда репортится как unknown, хотя
    # перепроверка подтвердила бы анкету (#448: unknown не выдавать за
    # отсутствие анкеты).
    cards = [_card("941"), _card("942")]
    scanned = []

    def scan(page_arg, vacancy, *, timeout_ms, form_timeout_ms):
        scanned.append((vacancy.vacancy_id, timeout_ms))
        if vacancy.vacancy_id == "941":
            if timeout_ms == questionnaire.FAST_TIMEOUT_MS:
                return questionnaire.QuestionnaireScanResult(
                    vacancy, questionnaire.UNKNOWN, "timeout", retryable=True
                )
            return questionnaire.QuestionnaireScanResult(
                vacancy, questionnaire.QUESTIONNAIRE, "task-body", (), 0
            )
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.QUESTIONNAIRE, "task-body", (), 0
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    probe.run_questionnaires(_bulk_args(limit_questionnaires=1))
    output = capsys.readouterr().out

    assert ("941", 90_000) in scanned, "накопленный retry не выполнен из-за лимита"
    assert "unknown 0" in output


def test_interrupt_after_unresolved_unknown_is_a_failure(monkeypatch, capsys):
    # cycle-review PR #450 (Codex): прерывание не должно давать exit 0, если в
    # обработанной части остались неразрешённые unknown — иначе неполный скан
    # неотличим от полного. Fail-closed тот же, что и для потери авторизации.
    cards = [_card("951"), _card("952")]

    def scan(page_arg, vacancy, **kwargs):
        if vacancy.vacancy_id == "952":
            raise KeyboardInterrupt
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.UNKNOWN, "timeout", retryable=True
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    from hhru_bot.exit_codes import CommandExitCode

    assert probe.run_questionnaires(_bulk_args()) is CommandExitCode.SIGINT
    output = capsys.readouterr().out

    assert "прерван пользователем" in output
    assert "[FAIL]" in output


def test_clean_interrupt_without_uncertainty_returns_sigint_code(monkeypatch, capsys):
    # Даже чистая намеренная остановка возвращает стандартный POSIX-код SIGINT;
    # частичный отчёт при этом по-прежнему печатается без traceback.
    cards = [_card("961"), _card("962")]

    def scan(page_arg, vacancy, **kwargs):
        if vacancy.vacancy_id == "962":
            raise KeyboardInterrupt
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.QUESTIONNAIRE, "task-body", (), 0
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    from hhru_bot.exit_codes import CommandExitCode

    assert probe.run_questionnaires(_bulk_args()) is CommandExitCode.SIGINT
    assert "[FAIL]" not in capsys.readouterr().out


def test_interrupt_with_lost_auth_and_unknown_prints_both_fail_lines(monkeypatch, capsys):
    # Обе причины неполноты независимы: потерянная сессия не объясняет
    # unresolved unknown у другой вакансии. Обе строки [FAIL] должны печататься.
    cards = [_card("991"), _card("992"), _card("993")]

    def scan(page_arg, vacancy, **kwargs):
        if vacancy.vacancy_id == "992":
            return questionnaire.QuestionnaireScanResult(
                vacancy, questionnaire.UNKNOWN, "timeout", retryable=True
            )
        if vacancy.vacancy_id == "993":
            raise KeyboardInterrupt
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.UNAUTHENTICATED, "требуется авторизация"
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    from hhru_bot.exit_codes import CommandExitCode

    assert probe.run_questionnaires(_bulk_args()) is CommandExitCode.SIGINT
    output = capsys.readouterr().out

    assert "[FAIL] сессия истекла во время прогона" in output
    assert "[FAIL] скан прерван с неподтверждёнными вакансиями" in output


def test_limit_reached_exits_cleanly_without_interrupt(monkeypatch, capsys):
    # --limit-questionnaires останавливает скан штатно, не через SIGINT.
    cards = [_card("981"), _card("982")]

    def scan(page_arg, vacancy, **kwargs):
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.QUESTIONNAIRE, "task-body", (), 0
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    assert probe.run_questionnaires(_bulk_args(limit_questionnaires=1)) is False
    assert "прерван пользователем" not in capsys.readouterr().out


def test_throttle_pause_precedes_every_scan_including_retry_after_limit(monkeypatch, capsys):
    # cycle-review PR #450 round 2 (Codex): выход по лимиту происходит ДО
    # time.sleep(), а следом сразу стартует накопленный retry — два клика по
    # hh.ru подряд без паузы. Базовый принцип CLAUDE.md: троттлинг между
    # реальными действиями не ослабляется, в том числе на границе лимита.
    cards = [_card("971"), _card("972")]
    events = []

    def scan(page_arg, vacancy, *, timeout_ms, form_timeout_ms):
        events.append(("scan", vacancy.vacancy_id, timeout_ms))
        if vacancy.vacancy_id == "971":
            return questionnaire.QuestionnaireScanResult(
                vacancy, questionnaire.UNKNOWN, "timeout", retryable=True
            )
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.QUESTIONNAIRE, "task-body", (), 0
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    monkeypatch.setattr(
        "hhru_bot.commands.probe.time.sleep", lambda seconds: events.append(("sleep", seconds))
    )
    probe.run_questionnaires(_bulk_args(limit_questionnaires=1))
    capsys.readouterr()

    scans = [index for index, event in enumerate(events) if event[0] == "scan"]
    for previous, current in zip(scans, scans[1:], strict=False):
        assert any(events[index][0] == "sleep" for index in range(previous + 1, current)), (
            f"нет паузы между сканами {events[previous]} и {events[current]}"
        )


def test_retry_pass_also_honours_the_limit(monkeypatch, capsys):
    # cycle-review PR #450 round 3: слив retry не должен игнорировать лимит.
    # Если перепроверка сама подтверждает анкеты, цикл обязан остановиться на
    # N-й, иначе --limit-questionnaires 1 прокликает все накопленные retry и
    # вернёт N анкет вместо одной.
    cards = [_card("981"), _card("982"), _card("983")]
    calls = []

    def scan(page_arg, vacancy, *, timeout_ms, form_timeout_ms):
        calls.append((vacancy.vacancy_id, timeout_ms))
        if timeout_ms == questionnaire.FAST_TIMEOUT_MS:
            return questionnaire.QuestionnaireScanResult(
                vacancy, questionnaire.UNKNOWN, "timeout", retryable=True
            )
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.QUESTIONNAIRE, "task-body", (), 0
        )

    probe = _bulk_env(monkeypatch, cards, scan)
    probe.run_questionnaires(_bulk_args(limit_questionnaires=1))
    output = capsys.readouterr().out

    retries = [call for call in calls if call[1] != questionnaire.FAST_TIMEOUT_MS]
    assert len(retries) == 1, f"retry продолжился после достижения лимита: {retries}"
    assert "анкет 1" in output


class _FakeHistory:
    """Records record_questionnaire() calls without touching real SQLite."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def record_questionnaire(self, resume_id, vacancy_id, vacancy_url, title, company, questions):
        self.calls.append(vacancy_id)


def test_retry_confirmed_questionnaire_is_persisted_to_history(monkeypatch, capsys):
    # cycle-review PR #456 (Codex): вакансия с transient UNKNOWN на быстром
    # проходе и подтверждённой анкетой на retry (731-743) должна попасть в
    # SQLite так же, как подтверждённая на первом проходе (682-699) — иначе
    # итоговый отчёт (печать) расходится с исследовательской базой.
    card = _card("991")

    def scan(page_arg, vacancy, *, timeout_ms, form_timeout_ms):
        if timeout_ms == questionnaire.FAST_TIMEOUT_MS:
            return questionnaire.QuestionnaireScanResult(
                vacancy, questionnaire.UNKNOWN, "timeout", retryable=True
            )
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.QUESTIONNAIRE, "task-body", (), 0
        )

    probe = _bulk_env(monkeypatch, [card], scan)
    fake_history = _FakeHistory()
    monkeypatch.setattr("hhru_bot.history.History", lambda path: fake_history)
    probe.run_questionnaires(_bulk_args(history="history.db"))
    capsys.readouterr()

    assert fake_history.calls == ["991"], (
        "подтверждённая на retry анкета не записана в history.record_questionnaire"
    )


def test_history_write_failure_does_not_abort_the_rest_of_the_scan(monkeypatch, capsys):
    # cycle-review PR #456 (Claude /review): history.record_questionnaire()
    # внутри цикла (682-699) не обёрнут в try/except, в отличие от
    # search_vacancies чуть выше — упавшая запись (locked DB, диск полон)
    # прервала бы весь bulk-скан для всех оставшихся вакансий/резюме, что
    # противоречит fail-tolerant дизайну команды (--start-page, retry, обработка
    # прерывания). cycle-review round 2 (Codex): проглатывание sqlite3.Error
    # не должно превращаться в молчаливый success — иначе подтверждённая
    # анкета тихо теряется без [FAIL]/ненулевого результата, и повторный
    # запуск может уже не найти тот же transient questionnaire.
    import sqlite3

    card1 = _card("992")
    card2 = _card("993")

    def scan(page_arg, vacancy, **kwargs):
        return questionnaire.QuestionnaireScanResult(
            vacancy, questionnaire.QUESTIONNAIRE, "task-body", (), 0
        )

    probe = _bulk_env(monkeypatch, [card1, card2], scan)

    class FailingHistory(_FakeHistory):
        def record_questionnaire(self, *args, **kwargs):
            raise sqlite3.Error("database is locked")

    monkeypatch.setattr("hhru_bot.history.History", lambda path: FailingHistory())
    result = probe.run_questionnaires(_bulk_args(history="history.db"))
    output = capsys.readouterr().out

    # Скан должен дойти до второй вакансии, а не оборваться на первой записи...
    assert "993" in output
    # ...но потеря подтверждённой анкеты должна быть видна как провал, а не
    # молчаливый success (иначе исследовательская база расходится с отчётом
    # без единого машинно-обнаружимого сигнала).
    assert result is True, "падение записи в history должно давать [FAIL], а не молчаливый success"
    assert "[FAIL]" in output
    assert "history" in output.lower() or "истори" in output.lower()

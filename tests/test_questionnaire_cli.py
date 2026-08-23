"""CLI-команда questionnaire и её классификация для write-lock (#482)."""

from __future__ import annotations

import argparse

import pytest

from hhru_bot.cli import _is_write_command, build_parser
from hhru_bot.commands import questionnaire as cmd
from hhru_bot.history import SKIP_REASONS, History

pytestmark = pytest.mark.integration


def _args(tmp_path, **overrides) -> argparse.Namespace:
    values = {
        "config": str(tmp_path / "config.yaml"),
        "history": str(tmp_path / "h.db"),
        "resume": None,
        "limit": 50,
        "template": "salary",
        "mode": "static",
        "answer": None,
        "instruction": None,
        "example": [],
        "cluster": None,
        "low_confidence": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _audit_args(tmp_path, **overrides) -> argparse.Namespace:
    """``_args`` держит template='salary' — это дефолт для set/unset, а для audit
    он означал бы фильтр по шаблону, молча прячущий все остальные строки."""
    overrides.setdefault("template", None)
    return _args(tmp_path, **overrides)


def _record_audit(tmp_path, **overrides) -> None:
    resume_id = overrides.pop("resume_id", "r1")
    question = {
        "body_index": 0,
        "text": "Настоящим подтверждаю достоверность сведений",
        "kind": "text",
        "is_radio": False,
        "options": [],
        "answer": "Да",
        "answer_source": "profile",
        "confidence": 1.0,
        "filled": True,
        "template": "data_accuracy",
        "cluster": "compliance",
        "resolver_source": "static",
    }
    question.update(overrides)
    History(tmp_path / "h.db").record_questionnaire(
        resume_id,
        "132855712",
        "https://hh.ru/vacancy/132855712",
        "Разработчик",
        "Acme",
        [question],
        source="apply",
        run_id="run-488",
    )


# --- set --------------------------------------------------------------------


def test_set_static_stores_the_answer(capsys, tmp_path):
    cmd.run_set(_args(tmp_path, answer="от 250000"))

    out = capsys.readouterr().out
    assert "[OK]" in out
    stored = History(tmp_path / "h.db").get_questionnaire_templates()["salary"]
    assert stored["answer"] == "от 250000"


def test_set_static_without_answer_fails_and_writes_nothing(capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        cmd.run_set(_args(tmp_path))

    assert exc.value.code == 1
    assert "[FAIL]" in capsys.readouterr().err
    assert History(tmp_path / "h.db").get_questionnaire_templates() == {}


def test_set_contextual_without_instruction_fails(capsys, tmp_path):
    with pytest.raises(SystemExit):
        cmd.run_set(_args(tmp_path, mode="contextual"))

    assert "[FAIL]" in capsys.readouterr().err


def test_set_contextual_stores_instruction_and_examples(tmp_path):
    cmd.run_set(
        _args(
            tmp_path,
            mode="contextual",
            instruction="назови вилку",
            example=["Ваш желаемый доход?", "Сколько хотите зарабатывать?"],
        )
    )

    history = History(tmp_path / "h.db")
    assert history.get_questionnaire_templates()["salary"]["instruction"] == "назови вилку"
    assert len(history.get_confirmed_phrases()) == 2


def test_example_is_rejected_for_static_mode(capsys, tmp_path):
    with pytest.raises(SystemExit):
        cmd.run_set(_args(tmp_path, answer="от 250000", example=["Доход?"]))

    assert "--example" in capsys.readouterr().err


def test_set_uses_the_seed_cluster_by_default(tmp_path):
    cmd.run_set(_args(tmp_path, answer="от 250000"))

    assert (
        History(tmp_path / "h.db").get_questionnaire_templates()["salary"]["cluster"]
        == "conditions"
    )


def test_set_accepts_an_explicit_cluster(tmp_path):
    cmd.run_set(
        _args(tmp_path, template="work_permit", answer="Гражданство РФ", cluster="compliance")
    )

    stored = History(tmp_path / "h.db").get_questionnaire_templates()["work_permit"]
    assert stored["cluster"] == "compliance"


def test_set_scopes_to_a_resume(tmp_path):
    cmd.run_set(_args(tmp_path, answer="резюме", resume="r1"))

    history = History(tmp_path / "h.db")
    assert history.get_questionnaire_templates("r1")["salary"]["answer"] == "резюме"
    assert history.get_questionnaire_templates() == {}


def test_set_unblocks_vacancies_skipped_for_the_queue(capsys, tmp_path):
    history = History(tmp_path / "h.db")
    history.record_questionnaire_pending(
        "r1",
        [
            {
                "text": "Зарплатные ожидания?",
                "kind": "text",
                "reason": "нет ответа",
                "template": "salary",
            }
        ],
        vacancy_id="v1",
    )
    history.record_skip("r1", "v1", SKIP_REASONS.QUESTIONNAIRE_PENDING)
    history.record_skip("r1", "v2", SKIP_REASONS.STOPWORD_TITLE)

    cmd.run_set(_args(tmp_path, answer="от 250000"))

    assert "Возвращено в поиск" in capsys.readouterr().out
    assert history.is_skipped("r1", "v1") is False
    assert history.is_skipped("r1", "v2") is True


def test_set_does_not_unblock_a_vacancy_with_other_open_questions(capsys, tmp_path):
    """Один ответ не делает анкету из двух неизвестных вопросов проходимой."""
    history = History(tmp_path / "h.db")
    history.record_questionnaire_pending(
        "r1",
        [
            {
                "text": "Зарплатные ожидания?",
                "kind": "text",
                "reason": "нет ответа",
                "template": "salary",
            },
            {"text": "Опишите сложный проект", "kind": "text", "reason": "нет шаблона"},
        ],
        vacancy_id="v1",
    )
    history.record_skip("r1", "v1", SKIP_REASONS.QUESTIONNAIRE_PENDING)

    cmd.run_set(_args(tmp_path, answer="от 250000"))

    assert "Возвращено в поиск" not in capsys.readouterr().out
    assert history.is_skipped("r1", "v1") is True


# --- unset ------------------------------------------------------------------


def test_unset_removes_an_existing_template(capsys, tmp_path):
    cmd.run_set(_args(tmp_path, answer="от 250000"))

    cmd.run_unset(_args(tmp_path))

    assert "[OK]" in capsys.readouterr().out
    assert History(tmp_path / "h.db").get_questionnaire_templates() == {}


def test_unset_missing_template_reports_info(capsys, tmp_path):
    cmd.run_unset(_args(tmp_path))

    assert "[INFO]" in capsys.readouterr().out


# --- templates / pending ----------------------------------------------------


def test_templates_prints_an_ascii_table(capsys, tmp_path):
    cmd.run_set(_args(tmp_path, answer="от 250000"))
    capsys.readouterr()

    cmd.run_templates(_args(tmp_path))

    out = capsys.readouterr().out
    assert "шаблон" in out and "+---" in out.replace("-", "-")
    assert "account" in out
    assert "от 250000" in out


def test_templates_empty_prints_info(capsys, tmp_path):
    cmd.run_templates(_args(tmp_path))

    assert "[INFO]" in capsys.readouterr().out


def test_pending_lists_queued_questions(capsys, tmp_path):
    History(tmp_path / "h.db").record_questionnaire_pending(
        "r1", [{"text": "Ваш опыт?", "kind": "text", "reason": "нет шаблона"}]
    )

    cmd.run_pending(_args(tmp_path))

    out = capsys.readouterr().out
    assert "Ваш опыт?" in out
    assert "нет шаблона" in out
    assert "Ожидает решения: 1" in out


def test_pending_empty_prints_info(capsys, tmp_path):
    cmd.run_pending(_args(tmp_path))

    assert "[INFO]" in capsys.readouterr().out


def test_output_has_no_emoji(capsys, tmp_path):
    cmd.run_set(_args(tmp_path, answer="от 250000"))
    cmd.run_templates(_args(tmp_path))

    out = capsys.readouterr().out
    assert all(ord(char) < 0x2190 for char in out), "вывод CLI должен быть текст/ASCII-таблицы"


# --- learn ------------------------------------------------------------------


def test_learn_is_a_noop_without_a_tty(capsys, tmp_path, monkeypatch):
    History(tmp_path / "h.db").record_questionnaire_pending(
        "r1", [{"text": "Ваш опыт?", "kind": "text", "reason": "нет шаблона"}]
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert cmd.run_learn(_args(tmp_path, limit=20)) is None
    assert "Неинтерактивный" in capsys.readouterr().out


def test_learn_stores_answer_and_resolves_the_queue(capsys, tmp_path, monkeypatch):
    history = History(tmp_path / "h.db")
    history.record_questionnaire_pending(
        "r1",
        [{"text": "Ваш желаемый доход?", "kind": "text", "reason": "нет шаблона"}],
        vacancy_id="v1",
    )
    history.record_skip("r1", "v1", SKIP_REASONS.QUESTIONNAIRE_PENDING)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _answers = iter(["salary", "от 250000"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(_answers))

    cmd.run_learn(_args(tmp_path, limit=20))

    assert history.get_questionnaire_templates()["salary"]["answer"] == "от 250000"
    assert history.get_confirmed_phrases()["ваш желаемый доход?"] == "salary"
    assert history.list_questionnaire_pending("r1") == []
    assert history.is_skipped("r1", "v1") is False
    assert "Разобрано вопросов: 1" in capsys.readouterr().out


def test_learn_keeps_the_question_when_the_answer_is_empty(capsys, tmp_path, monkeypatch):
    history = History(tmp_path / "h.db")
    history.record_questionnaire_pending(
        "r1", [{"text": "Ваш опыт?", "kind": "text", "reason": "нет шаблона"}]
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _answers = iter(["experience", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(_answers))

    cmd.run_learn(_args(tmp_path, limit=20))

    assert len(history.list_questionnaire_pending("r1")) == 1
    assert "[skip]" in capsys.readouterr().out


def test_learn_returns_sigint_on_interrupt(capsys, tmp_path, monkeypatch):
    from hhru_bot.exit_codes import CommandExitCode

    History(tmp_path / "h.db").record_questionnaire_pending(
        "r1", [{"text": "Ваш опыт?", "kind": "text", "reason": "нет шаблона"}]
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _interrupt(_prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _interrupt)

    assert cmd.run_learn(_args(tmp_path, limit=20)) is CommandExitCode.SIGINT
    assert "Прервано" in capsys.readouterr().out


# --- audit (#488) -----------------------------------------------------------


def test_audit_prints_answer_confidence_and_template(capsys, tmp_path):
    _record_audit(tmp_path)

    cmd.run_audit(_audit_args(tmp_path))

    out = capsys.readouterr().out
    assert "132855712" in out
    assert "1.00" in out
    assert "data_accuracy" in out
    assert "static" in out
    assert "Заполнено ответов: 1, без ответа: 0, форма не заполнялась: 0." in out


def test_audit_marks_a_refused_answer_instead_of_printing_an_empty_cell(capsys, tmp_path):
    """Пустой answer — осознанный отказ отвечать, а не «ответили пустотой».

    ``filled=True``: форму бот заполнил и отклик отправил, но по ЭТОМУ вопросу
    отвечать отказался. Именно так отказ отличается от отсеянного батча,
    у которого не заполнялось вообще ничего.
    """
    _record_audit(tmp_path, answer="", answer_source="llm", confidence=0.2, filled=True)

    cmd.run_audit(_audit_args(tmp_path))

    out = capsys.readouterr().out
    assert "[без ответа]" in out
    assert "0.20" in out
    assert "без ответа: 1" in out


def test_audit_low_confidence_flag_keeps_only_unanswered_questions(capsys, tmp_path):
    _record_audit(tmp_path)
    _record_audit(tmp_path, text="Какие редакторы?", answer="", confidence=0.2, filled=False)

    cmd.run_audit(_audit_args(tmp_path, low_confidence=True))

    out = capsys.readouterr().out
    assert "Какие редакторы?" in out
    assert "Настоящим подтверждаю" not in out


def test_audit_clips_a_long_question_so_the_table_stays_readable(capsys, tmp_path):
    _record_audit(tmp_path, text="Опишите ваш опыт " * 40)

    cmd.run_audit(_audit_args(tmp_path))

    out = capsys.readouterr().out
    assert "..." in out
    assert max(len(line) for line in out.splitlines()) < 160


def test_audit_template_flag_filters_by_template(capsys, tmp_path):
    _record_audit(tmp_path)
    _record_audit(tmp_path, text="Зарплатные ожидания?", template="salary")

    cmd.run_audit(_audit_args(tmp_path, template="salary"))

    out = capsys.readouterr().out
    assert "Зарплатные ожидания?" in out
    assert "Настоящим подтверждаю" not in out


def test_audit_renders_legacy_rows_without_resolver_columns(capsys, tmp_path):
    """template/cluster/resolver_source добавлены ALTER'ом задним числом (#482),
    поэтому в базе есть apply-строки, где они NULL. Таблица не должна падать и
    обязана показать прочерк, а не пустую ячейку или None."""
    _record_audit(tmp_path, template=None, cluster=None, resolver_source=None)

    cmd.run_audit(_audit_args(tmp_path))

    out = capsys.readouterr().out
    assert "None" not in out
    assert "| -" in out
    assert "Заполнено ответов: 1" in out


def test_audit_empty_prints_info(capsys, tmp_path):
    cmd.run_audit(_audit_args(tmp_path))

    assert "[INFO]" in capsys.readouterr().out


def test_audit_distinguishes_an_empty_audit_from_an_empty_filter(capsys, tmp_path):
    """«Ответов нет» и «под фильтр ничего не попало» — разные факты: первый
    отправил бы оператора в прямой SQL проверять, правда ли база пуста."""
    _record_audit(tmp_path)

    cmd.run_audit(_audit_args(tmp_path, template="salary"))

    assert "Под заданный фильтр" in capsys.readouterr().out


def test_audit_rejects_a_negative_last(capsys, tmp_path):
    """--last -5 в SQLite означало бы «без ограничения» — противоположность намерению."""
    with pytest.raises(SystemExit) as exc:
        cmd.run_audit(_audit_args(tmp_path, limit=-5))

    assert exc.value.code == 1
    assert "[FAIL]" in capsys.readouterr().err


def test_audit_last_flag_stores_into_limit():
    """--last переиспользует dest=limit, иначе _limit() не увидел бы значение.

    Здесь же сверяются дефолты, на которые опирается ``_audit_args``: без этого
    контур незамкнут — появись у ``--template`` не-None дефолт, CLI-тесты audit
    продолжили бы проходить против фикстуры, разошедшейся с парсером.
    """
    args = build_parser().parse_args(["questionnaire", "audit", "--last", "5"])

    assert args.limit == 5
    assert args.low_confidence is False
    assert args.template is None
    assert args.resume is None


def test_audit_empty_template_is_a_real_filter_not_an_empty_audit(capsys, tmp_path):
    """``--template ""`` — фильтр реальный (SQL ставит его по is not None),
    совпадений у него просто нет. Сообщение «ответов нет» было бы ложью про базу."""
    _record_audit(tmp_path)

    cmd.run_audit(_audit_args(tmp_path, template=""))

    assert "Под заданный фильтр" in capsys.readouterr().out


def test_audit_last_n_applies_after_the_filter(capsys, tmp_path):
    """LIMIT + WHERE + reversed() вместе: срез берётся от отфильтрованных строк."""
    for index in range(4):
        _record_audit(tmp_path, text=f"Зарплата {index}?", template="salary")
        _record_audit(tmp_path, text=f"Комплаенс {index}?", template="data_accuracy")

    cmd.run_audit(_audit_args(tmp_path, template="salary", limit=2))

    out = capsys.readouterr().out
    assert "Зарплата 2?" in out and "Зарплата 3?" in out
    assert "Зарплата 1?" not in out
    assert "Комплаенс" not in out
    assert "Заполнено ответов: 2" in out


def test_audit_output_has_no_emoji(capsys, tmp_path):
    _record_audit(tmp_path)

    cmd.run_audit(_audit_args(tmp_path))

    out = capsys.readouterr().out
    assert all(ord(char) < 0x2190 for char in out), "вывод CLI должен быть текст/ASCII-таблицы"


def test_audit_does_not_present_a_never_filled_proposal_as_an_answer(capsys, tmp_path):
    """Отсеянный батч — это НЕ ответы: форма не заполнялась вовсе.

    ``pipeline.py:589`` при единственном неуверенном вопросе пишет ``filled=0``
    ВСЕМУ скану и на ``:615`` отсеивает вакансию. Уверенный сосед сохраняется с
    непустым ``answer``, хотя в форму не попал ни один символ. Печатать его
    неотличимо от заполненного — та же ложь про базу, ради которой писались
    ``95e7b5d`` и ``c375eb2``, только этажом выше.
    """
    _record_audit(tmp_path, text="Готовы к переезду?", answer="Да", confidence=0.98, filled=False)
    _record_audit(tmp_path, text="Какие редакторы?", answer="", confidence=0.2, filled=False)

    cmd.run_audit(_audit_args(tmp_path))

    out = capsys.readouterr().out
    assert "Готовы к переезду?" in out, "строку не прячем — её всё ещё оценивают"
    assert "[форма не заполнялась]" in out, "но её нельзя показывать как заполненный ответ"
    assert "Заполнено ответов: 0" in out, "ни один ответ в форму не попал"


def test_audit_counts_only_filled_rows_as_answers(capsys, tmp_path):
    """Футер считает заполненное, а не сохранённое: иначе счётчик лжёт так же,
    как лгала бы ячейка."""
    _record_audit(tmp_path, text="Зарплата?", answer="200000", filled=True)
    _record_audit(tmp_path, text="Переезд?", answer="Да", confidence=0.98, filled=False)

    cmd.run_audit(_audit_args(tmp_path))

    out = capsys.readouterr().out
    assert "Заполнено ответов: 1" in out
    assert "форма не заполнялась: 1" in out


# --- write-lock классификация (критерий приёмки #482) ----------------------


@pytest.mark.parametrize("subcommand", ["set", "unset", "learn"])
def test_mutating_subcommands_take_the_write_lock(subcommand):
    args = argparse.Namespace(command="questionnaire", questionnaire_command=subcommand)

    assert _is_write_command(args) is True


@pytest.mark.parametrize("subcommand", ["pending", "templates", "audit"])
def test_read_subcommands_do_not_take_the_write_lock(subcommand):
    args = argparse.Namespace(command="questionnaire", questionnaire_command=subcommand)

    assert _is_write_command(args) is False


def test_account_create_still_takes_the_write_lock():
    """Обобщение dest-ов не должно сломать уже классифицированную команду."""
    args = argparse.Namespace(command="account", account_command="create")

    assert _is_write_command(args) is True


def test_account_list_still_reads():
    args = argparse.Namespace(command="account", account_command="list")

    assert _is_write_command(args) is False


# --- регистрация в парсере --------------------------------------------------


def test_parser_registers_every_subcommand():
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    questionnaire = subparsers.choices["questionnaire"]
    nested = next(
        action
        for action in questionnaire._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    assert set(nested.choices) == {"pending", "templates", "audit", "learn", "set", "unset"}


def test_set_requires_a_mode():
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["questionnaire", "set", "salary"])


# --- bootstrap очереди из ранее собранных сканов ---------------------------


def _scan(history, resume_id: str, vacancy_id: str, *texts: str) -> None:
    history.record_questionnaire(
        resume_id,
        vacancy_id,
        f"https://hh.ru/vacancy/{vacancy_id}",
        "Разработчик",
        "Acme",
        [
            {"body_index": i, "text": t, "kind": "text", "is_radio": False, "options": []}
            for i, t in enumerate(texts)
        ],
    )


def _learn_with(tmp_path, monkeypatch, answers=()):
    """Запустить learn интерактивно с заготовленными ответами."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(replies, ""))
    return cmd.run_learn(_args(tmp_path, limit=20))


def test_learn_seeds_the_queue_from_earlier_scans(capsys, tmp_path, monkeypatch):
    """probe --questionnaires-only уже собрал вопросы: обучать бота можно сразу,
    не дожидаясь первого боевого apply."""
    history = History(tmp_path / "h.db")
    _scan(history, "RID", "v1", "Опишите самый сложный проект", "Есть ли судимость?")

    _learn_with(tmp_path, monkeypatch)

    assert "из ранее собранных анкет: 2" in capsys.readouterr().out
    queued = {row["question_text"] for row in history.list_questionnaire_pending("RID")}
    assert queued == {"Опишите самый сложный проект", "Есть ли судимость?"}


def test_pending_reports_scanned_questions_without_writing_them(capsys, tmp_path):
    """pending классифицирована READ, не берёт общий write-lock и потому не
    имеет права писать в историю — она только сообщает, что материал есть."""
    history = History(tmp_path / "h.db")
    _scan(history, "RID", "v1", "Опишите самый сложный проект")

    cmd.run_pending(_args(tmp_path))

    assert "неразобранных вопросов: 1" in capsys.readouterr().out
    assert history.list_questionnaire_pending("RID") == []


def test_seeding_deduplicates_a_question_seen_in_many_vacancies(tmp_path, monkeypatch):
    history = History(tmp_path / "h.db")
    _scan(history, "RID", "v1", "Опишите самый сложный проект")
    _scan(history, "RID", "v2", "Опишите самый сложный проект")

    _learn_with(tmp_path, monkeypatch)

    assert len(history.list_questionnaire_pending("RID")) == 1


def test_seeding_skips_questions_the_resolver_already_answers(tmp_path, monkeypatch):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")
    _scan(history, "RID", "v1", "Ваши зарплатные ожидания?", "Опишите самый сложный проект")

    _learn_with(tmp_path, monkeypatch)

    queued = {row["question_text"] for row in history.list_questionnaire_pending("RID")}
    assert queued == {"Опишите самый сложный проект"}


def test_answering_a_template_clears_its_question_from_the_queue(capsys, tmp_path, monkeypatch):
    """Шаблон мог появиться позже, чем вопрос попал в очередь."""
    history = History(tmp_path / "h.db")
    _scan(history, "RID", "v1", "Ваши зарплатные ожидания?")
    _learn_with(tmp_path, monkeypatch)
    capsys.readouterr()

    cmd.run_set(_args(tmp_path, answer="от 250000"))
    cmd.run_pending(_args(tmp_path))

    assert "зарплатные ожидания" not in capsys.readouterr().out


def test_contextual_template_without_llm_keeps_its_question_queued(tmp_path, monkeypatch):
    """Contextual-шаблон без LLM неисполним — снимать вопрос с очереди рано."""
    history = History(tmp_path / "h.db")
    _scan(history, "RID", "v1", "Ваши зарплатные ожидания?")
    _learn_with(tmp_path, monkeypatch)

    cmd.run_set(_args(tmp_path, mode="contextual", instruction="назови вилку"))

    assert len(history.list_questionnaire_pending("RID")) == 1


def test_seeding_is_scoped_to_the_requested_resume(tmp_path, monkeypatch):
    history = History(tmp_path / "h.db")
    _scan(history, "RID1", "v1", "Опишите проект")
    _scan(history, "RID2", "v2", "Опишите проект")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    cmd.run_learn(_args(tmp_path, resume="RID1", limit=20))

    assert len(history.list_questionnaire_pending("RID1")) == 1
    assert history.list_questionnaire_pending("RID2") == []


# --- валидация аргументов ---------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, -50])
def test_non_positive_limit_is_rejected(capsys, tmp_path, limit):
    """LIMIT -1 в SQLite означает «без ограничения» — молча противоположный смысл."""
    with pytest.raises(SystemExit) as exc:
        cmd.run_pending(_args(tmp_path, limit=limit))

    assert exc.value.code == 1
    assert "--limit" in capsys.readouterr().err


def test_compliance_cluster_rejects_a_contextual_template(capsys, tmp_path):
    """Отказ на входе, а не при ответе: такой шаблон заведомо неисполним."""
    with pytest.raises(SystemExit) as exc:
        cmd.run_set(
            _args(
                tmp_path,
                template="work_permit",
                mode="contextual",
                instruction="ответь по документам",
                cluster="compliance",
            )
        )

    assert exc.value.code == 1
    assert "только --mode static" in capsys.readouterr().err
    assert History(tmp_path / "h.db").get_questionnaire_templates() == {}


def test_compliance_cluster_accepts_a_static_template(tmp_path):
    cmd.run_set(
        _args(tmp_path, template="work_permit", answer="Гражданство РФ", cluster="compliance")
    )

    assert History(tmp_path / "h.db").get_questionnaire_templates()["work_permit"]["answer"]

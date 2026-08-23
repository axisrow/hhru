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
        "last": 50,
        "low_confidence": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


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


# --- audit --------------------------------------------------------------


def _record_apply_answer(tmp_path, **overrides):
    question = {
        "body_index": 0,
        "text": "Настоящим подтверждаю...",
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
        "marketing",
        "132855712",
        "https://hh.ru/vacancy/132855712",
        "Маркетолог",
        "Acme",
        [question],
        source="apply",
    )


def test_audit_prints_an_ascii_table(capsys, tmp_path):
    _record_apply_answer(tmp_path)

    cmd.run_audit(_args(tmp_path, template=None))

    out = capsys.readouterr().out
    assert "132855712" in out
    assert "data_accuracy" in out
    assert "Настоящим подтверждаю" in out
    assert "Да" in out
    assert "Показано строк: 1" in out


def test_audit_truncates_long_question_and_answer_text(capsys, tmp_path):
    """Реальные вопросы анкет — по 150-300 символов; без обрезки одна такая
    строка растягивает всю ASCII-таблицу до нечитаемой ширины."""
    long_question = "Расскажите подробно о своём опыте работы с CRM-системами. " * 5
    long_answer = "У меня большой опыт работы с несколькими CRM-системами. " * 5
    _record_apply_answer(tmp_path, text=long_question, answer=long_answer)

    cmd.run_audit(_args(tmp_path, template=None))

    out = capsys.readouterr().out
    assert long_question not in out
    assert long_answer not in out
    assert "..." in out
    assert max(len(line) for line in out.splitlines()) < len(long_question)


def test_audit_shows_not_filled_marker_for_unanswered_questions(capsys, tmp_path):
    _record_apply_answer(
        tmp_path,
        text="Какие редакторы вы используете?",
        answer="",
        answer_source=None,
        confidence=0.0,
        filled=False,
        template=None,
        cluster=None,
        resolver_source=None,
    )

    cmd.run_audit(_args(tmp_path, template=None))

    out = capsys.readouterr().out
    assert "[не заполнено]" in out


def test_audit_empty_prints_info(capsys, tmp_path):
    cmd.run_audit(_args(tmp_path, template=None))

    assert "[INFO]" in capsys.readouterr().out


def test_audit_filters_by_resume(capsys, tmp_path):
    _record_apply_answer(tmp_path)

    cmd.run_audit(_args(tmp_path, template=None, resume="backend"))

    assert "[INFO]" in capsys.readouterr().out


def test_audit_filters_by_template(capsys, tmp_path):
    _record_apply_answer(tmp_path)

    cmd.run_audit(_args(tmp_path, template="other_template"))

    assert "[INFO]" in capsys.readouterr().out


def test_audit_filters_by_low_confidence(capsys, tmp_path):
    _record_apply_answer(tmp_path)  # confidence=1.0 — не должен попасть

    cmd.run_audit(_args(tmp_path, template=None, low_confidence=True))

    assert "[INFO]" in capsys.readouterr().out


def test_audit_rejects_non_positive_last(capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        cmd.run_audit(_args(tmp_path, template=None, last=0))

    assert exc.value.code == 1
    assert "[FAIL]" in capsys.readouterr().err


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

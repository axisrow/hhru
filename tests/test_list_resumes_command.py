"""Тесты команды list-resumes (#57): список резюме из конфига (READ, #21).

READ-команда: читает config.resumes, опционально --status добавляет колонки
«можно bump» (throttle.can_bump_now) и «последний bump» (history.last_action_at).
Браузер НЕ нужен — только config.yaml + SQLite-история. hh.ru НЕ дёргается.

Конвенция (как test_mark_command.py): интеграционный прогон run() целиком на
реальном History (tmp SQLite) + реальном Throttle поверх него. Время кулдауна
(BUMP_COOLDOWN = 4ч) контролируется через созданные записи истории:
  - resume без bump в истории     → can_bump_now() = (True, None)
  - resume с недавним success-bump → can_bump_now() = (False, wait_left)
"""

from __future__ import annotations

import argparse
import textwrap

from hhru_bot.commands import list_resumes as list_resumes_cmd
from hhru_bot.history import History


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _two_resumes_config() -> str:
    """Два резюме с разными resume_id (числовой хвост URL)."""
    return """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: backend
            resume_url: "https://hh.ru/resume/11111111"
            search:
              text: "python developer"
          - id: analyst
            resume_url: "https://hh.ru/resume/22222222"
            search:
              text: "data analyst"
        """


def _args(config_path, history_path, **overrides) -> argparse.Namespace:
    base = {
        "config": str(config_path),
        "history": str(history_path),
        "status": False,
        "remote": False,
        "headless": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --- базовый READ (без --status) --------------------------------------------


def test_list_resumes_prints_table_from_config(capsys, tmp_path):
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    # обе строки резюме видны: slug (id) + числовой resume_id
    assert "backend" in out
    assert "11111111" in out
    assert "analyst" in out
    assert "22222222" in out
    # рамка ASCII-таблицы (+---+)
    assert "+" in out and "-" in out and "|" in out


def test_list_resumes_no_status_omits_status_columns(capsys, tmp_path):
    """Без --status колонки «можно bump»/«последний bump» не появляются."""
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "можно bump" not in out
    assert "последний bump" not in out


def test_list_resumes_headers(capsys, tmp_path):
    """Базовый режим: ровно колонки id | resume_id."""
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "id" in out
    assert "resume_id" in out


def test_list_resumes_no_emoji(capsys, tmp_path):
    """Контракт проекта: вывод только текст/ASCII, НИ ОДНОЙ эмодзи."""
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    # эмодзи лежат в высоких плоскостях Unicode — проверяем их отсутствие.
    for ch in out:
        assert not ("\U0001f000" <= ch <= "\U0001faff"), f"найдена эмодзи: {ch!r}"


def test_list_resumes_shows_all_resume_fields(capsys, tmp_path):
    """Каждое резюме из конфига: и slug (id), и числовой resume_id (хвост URL)."""
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    # slug и resume_id разделены (не склеены) — оба видны как отдельные значения
    assert "backend" in out and "11111111" in out
    assert "analyst" in out and "22222222" in out


# --- --status ----------------------------------------------------------------


def test_status_adds_status_columns(capsys, tmp_path):
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", status=True))

    out = capsys.readouterr().out
    assert "можно bump" in out
    assert "последний bump" in out


def test_status_never_bumped_shows_yes_and_dash(capsys, tmp_path):
    """Резюме без bump в истории: «можно bump» = да, «последний bump» = —."""
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", status=True))

    out = capsys.readouterr().out
    assert "да" in out
    # строки backend/analyst обе без истории → обе «да», дата отсутствует (—)
    assert "—" in out or "-" in out


def test_status_recent_bump_shows_no_and_timestamp(capsys, tmp_path):
    """Резюме с недавним success-bump: «можно bump» = нет, «последний bump» = дата."""
    config = _write_config(tmp_path, _two_resumes_config())
    h = History(tmp_path / "h.db")
    # success-bump для backend (vacancy_id = resume_id как sentinel, см. bump.py)
    h.record_action("11111111", "11111111", "bump", "success")

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", status=True))

    out = capsys.readouterr().out
    assert "нет" in out  # кулдаунд 4ч ещё не прошёл
    # analyst без bump — «да»; backend с bump — «нет»: обе ветки can_bump_now проверены
    assert "да" in out


def test_status_dry_run_bump_not_treated_as_success(capsys, tmp_path):
    """last_action_at/can_bump_now считают только status='success'
    (как count_today/time_since_last). dry_run-bump НЕ делает «последний bump»."""
    config = _write_config(tmp_path, _two_resumes_config())
    h = History(tmp_path / "h.db")
    h.record_action("11111111", "11111111", "bump", "dry_run")

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", status=True))

    out = capsys.readouterr().out
    # dry_run не считается успехом → оба резюме «можно bump»: да
    # и строк «последний bump» (дата) быть не должно, только прочерк
    assert "да" in out
    assert "нет" not in out


def test_status_failed_bump_not_treated_as_success(capsys, tmp_path):
    """failed-bump тоже не считается (last_action_at/can_bump_now фильтруют
    status='success'). Неудачное поднятие не делает резюме «кулдаунным»."""
    config = _write_config(tmp_path, _two_resumes_config())
    h = History(tmp_path / "h.db")
    h.record_action("11111111", "11111111", "bump", "failed")

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", status=True))

    out = capsys.readouterr().out
    assert "да" in out
    assert "нет" not in out


def test_status_does_not_touch_hhru(tmp_path, monkeypatch):
    """READ-контракт: команда не открывает браузер/не ходит на hh.ru.
    Любой сетевой/браузерный вызов должен упасть, если команда попытается его сделать."""
    config = _write_config(tmp_path, _two_resumes_config())

    def _boom(*a, **kw):
        raise AssertionError("list-resumes не должен открывать браузер/ходить в сеть")

    monkeypatch.setattr("hhru_bot.browser.launch_context", _boom)
    # команда должна выполниться без исключений
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", status=True))


# --- register (авторегистрация через pkgutil) -------------------------------


def test_registers_list_resumes_subparser():
    """register(subparsers) добавляет subparser 'list-resumes'."""
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    list_resumes_cmd.register(sub)

    assert "list-resumes" in sub.choices
    subparser = sub.choices["list-resumes"]
    flags = {a.option_strings[0] for a in subparser._actions if a.option_strings}
    assert "--status" in flags
    assert "--remote" in flags


# --- --remote (#135) ---------------------------------------------------------


class _FakeCard:
    def __init__(self, resume_id, title):
        self.resume_id = resume_id
        self.title = title


def test_remote_rows_marks_configured_and_unconfigured():
    cards = [_FakeCard("11111111", "Backend developer"), _FakeCard("99999999", "")]
    rows = list_resumes_cmd._remote_rows(cards, configured_ids={"11111111"})

    assert rows == [
        ["11111111", "Backend developer", "да"],
        ["99999999", "—", "—"],
    ]


def test_remote_invalid_session_prints_fail_and_does_not_launch_browser(
    capsys, tmp_path, monkeypatch
):
    config = _write_config(tmp_path, _two_resumes_config())

    def _boom(*a, **kw):
        raise AssertionError("не должен открывать браузер без валидной сессии")

    monkeypatch.setattr("hhru_bot.browser.launch_context", _boom)
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", remote=True))

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "login" in out


class _FakeLocator:
    def __init__(self, count: int = 0):
        self._count = count

    def count(self):
        return self._count


class _FakePage:
    """Заглушка Page: только .locator(), нужный has_login_form (#147)."""

    def locator(self, selector):
        return _FakeLocator(0)


class _FakeContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def new_page(self):
        return _FakePage()


def test_remote_valid_session_prints_remote_table(capsys, tmp_path, monkeypatch):
    config = _write_config(tmp_path, _two_resumes_config())
    storage_state = tmp_path / "session.json"
    storage_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: _config_with_storage_state(path, storage_state),
    )

    fake_cards = [_FakeCard("11111111", "Backend developer"), _FakeCard("99999999", "Analyst")]

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _FakeContext())
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda page: False)
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", lambda page: fake_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", remote=True))

    out = capsys.readouterr().out
    assert "resume_id" in out
    assert "название" in out
    assert "в конфиге" in out
    assert "11111111" in out and "Backend developer" in out
    assert "99999999" in out and "Analyst" in out
    # 99999999 не в конфиге -> готовый YAML-фрагмент для вставки
    assert "resume_url" in out
    assert "99999999" in out


def test_remote_expired_session_detected_via_auth_cookie(capsys, tmp_path, monkeypatch):
    """_check_session проверяет только формат файла — реальную авторизацию на
    hh.ru подтверждает cookie hhtoken (has_auth_cookie), НЕ browser.is_logged_in()
    (та проверяет "account/login" в URL — приём, отвергнутый в auth.py как
    ненадёжный). Истёкшие cookies не должны маскироваться под «резюме не найдено»."""
    config = _write_config(tmp_path, _two_resumes_config())
    storage_state = tmp_path / "session.json"
    storage_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: _config_with_storage_state(path, storage_state),
    )

    def _boom_list_cards(page):
        raise AssertionError("list_resume_cards не должен вызываться после провала has_auth_cookie")

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _FakeContext())
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda page: False)
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", _boom_list_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", remote=True))

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "login" in out
    assert "не найдено" not in out


def test_remote_unconfirmed_title_selector_warns_when_all_titles_empty(
    capsys, tmp_path, monkeypatch
):
    """Если RESUME_LIST_CARD_TITLE не совпал ни для одной карточки — предупредить,
    а не молча выдать прочерки за подтверждённые данные."""
    config = _write_config(tmp_path, _two_resumes_config())
    storage_state = tmp_path / "session.json"
    storage_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: _config_with_storage_state(path, storage_state),
    )

    fake_cards = [_FakeCard("11111111", ""), _FakeCard("99999999", "")]

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _FakeContext())
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda page: False)
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", lambda page: fake_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", remote=True))

    out = capsys.readouterr().out
    assert "не подтверждён" in out


def test_remote_indeterminate_state_prints_fail_not_empty(capsys, tmp_path, monkeypatch):
    """Codex adversarial review (PR #136, round 2): list_resume_cards может поднять
    ResumeListIndeterminate (timeout/интерстишл/дрейф селектора — состояние страницы
    не подтверждено). Команда обязана сообщить [FAIL], а не «резюме не найдено»."""
    from hhru_bot.copy_resume import ResumeListIndeterminate

    config = _write_config(tmp_path, _two_resumes_config())
    storage_state = tmp_path / "session.json"
    storage_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: _config_with_storage_state(path, storage_state),
    )

    def _raise_indeterminate(page):
        raise ResumeListIndeterminate("карточки резюме не появились за отведённое время")

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _FakeContext())
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda page: False)
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", _raise_indeterminate)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", remote=True))

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "не найдено" not in out


def test_remote_stale_cookie_rejects_login_form(capsys, tmp_path, monkeypatch):
    """#147: hhtoken присутствует в jar, но сервер отдал форму входа — сессия
    отвергнута сервером, а не подтверждена одной лишь cookie."""
    config = _write_config(tmp_path, _two_resumes_config())
    storage_state = tmp_path / "session.json"
    storage_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: _config_with_storage_state(path, storage_state),
    )

    def _boom_list_cards(page):
        raise AssertionError("list_resume_cards не должен вызываться после провала has_login_form")

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _FakeContext())
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda page: True)
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", _boom_list_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", remote=True))

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "форму входа" in out
    assert "не найдено" not in out


def _config_with_storage_state(config_path, storage_state_path):
    from hhru_bot.config import load_config

    config = load_config(config_path)
    config.storage_state_file = storage_state_path
    return config

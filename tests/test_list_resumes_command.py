"""Тесты команды list-resumes: live-дефолт #320, --local, READ-контракт #21.

Запускают run() end-to-end на реальной History (tmp SQLite) и реальном Throttle;
кулдауны управляются засеянной историей, а не моками. Браузер/live-чтение
замоканы по строковым путям hhru_bot.browser.* / hhru_bot.copy_resume.*.

Модель #320: дефолт — канонический live-список HH.ru (единая таблица
resume_id | alias | название | статус); ``--local`` — офлайн-просмотр overlay
из конфига; сбой live-чтения даёт [FAIL] БЕЗ молчаливого fallback на конфиг.
"""

from __future__ import annotations

import argparse
import textwrap

import pytest

from hhru_bot.commands import list_resumes as list_resumes_cmd
from hhru_bot.history import History

pytestmark = pytest.mark.integration


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


def _empty_overlay_config() -> str:
    """#320: пустой overlay — раздел resumes отсутствует, конфиг валиден."""
    return """
        account:
          storage_state_file: data/storage_state/hh_session.json
        """


def _args(config_path, history_path, **overrides) -> argparse.Namespace:
    base = {
        "config": str(config_path),
        "history": str(history_path),
        "status": False,
        "local": False,
        "headless": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --- --local (офлайн-просмотр overlay из конфига) -----------------------------


def test_local_prints_table_from_config(capsys, tmp_path):
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", local=True))

    out = capsys.readouterr().out
    # обе строки резюме видны: slug (id) + числовой resume_id
    assert "backend" in out
    assert "11111111" in out
    assert "analyst" in out
    assert "22222222" in out
    # рамка ASCII-таблицы (+---+)
    assert "+" in out and "-" in out and "|" in out


def test_local_no_status_omits_status_columns(capsys, tmp_path):
    """Без --status колонки «можно bump»/«последний bump» не появляются."""
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", local=True))

    out = capsys.readouterr().out
    assert "можно bump" not in out
    assert "последний bump" not in out


def test_local_headers(capsys, tmp_path):
    """Локальный режим: ровно колонки id | resume_id."""
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", local=True))

    out = capsys.readouterr().out
    assert "id" in out
    assert "resume_id" in out


def test_no_emoji_in_local_output(capsys, tmp_path):
    """Контракт проекта: вывод только текст/ASCII, НИ ОДНОЙ эмодзи."""
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", local=True))

    out = capsys.readouterr().out
    # эмодзи лежат в высоких плоскостях Unicode — проверяем их отсутствие.
    for ch in out:
        assert not ("\U0001f000" <= ch <= "\U0001faff"), f"найдена эмодзи: {ch!r}"


def test_local_shows_all_resume_fields(capsys, tmp_path):
    """Каждое резюме из конфига: и slug (id), и числовой resume_id (хвост URL)."""
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", local=True))

    out = capsys.readouterr().out
    # slug и resume_id разделены (не склеены) — оба видны как отдельные значения
    assert "backend" in out and "11111111" in out
    assert "analyst" in out and "22222222" in out


def test_local_does_not_touch_hhru(tmp_path, monkeypatch):
    """READ-контракт --local: команда не открывает браузер/не ходит на hh.ru.
    Любой сетевой/браузерный вызов должен упасть, если команда попытается его сделать."""
    config = _write_config(tmp_path, _two_resumes_config())

    def _boom(*a, **kw):
        raise AssertionError("list-resumes --local не должен открывать браузер/ходить в сеть")

    monkeypatch.setattr("hhru_bot.browser.launch_context", _boom)
    # команда должна выполниться без исключений
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", local=True, status=True))


# --- --status (bump-колонки из локальной истории; в --local и в live) ---------


def test_local_status_adds_status_columns(capsys, tmp_path):
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", local=True, status=True))

    out = capsys.readouterr().out
    assert "можно bump" in out
    assert "последний bump" in out


def test_local_status_never_bumped_shows_yes_and_dash(capsys, tmp_path):
    """Резюме без bump в истории: «можно bump» = да, «последний bump» = —."""
    config = _write_config(tmp_path, _two_resumes_config())
    list_resumes_cmd.run(_args(config, tmp_path / "h.db", local=True, status=True))

    out = capsys.readouterr().out
    assert "да" in out
    # строки backend/analyst обе без истории → обе «да», дата отсутствует (—)
    assert "—" in out or "-" in out


def test_local_status_recent_bump_shows_no_and_timestamp(capsys, tmp_path):
    """Резюме с недавним success-bump: «можно bump» = нет, «последний bump» = дата."""
    config = _write_config(tmp_path, _two_resumes_config())
    h = History(tmp_path / "h.db")
    # success-bump для backend (vacancy_id = resume_id как sentinel, см. bump.py)
    h.record_action("11111111", "11111111", "bump", "success")

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", local=True, status=True))

    out = capsys.readouterr().out
    assert "нет" in out  # кулдаун 4ч ещё не прошёл
    # analyst без bump — «да»; backend с bump — «нет»: обе ветки can_bump_now проверены
    assert "да" in out


def test_local_status_dry_run_bump_not_treated_as_success(capsys, tmp_path):
    """last_action_at/can_bump_now считают только status='success'
    (как count_today/time_since_last). dry_run-bump НЕ делает «последний bump»."""
    config = _write_config(tmp_path, _two_resumes_config())
    h = History(tmp_path / "h.db")
    h.record_action("11111111", "11111111", "bump", "dry_run")

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", local=True, status=True))

    out = capsys.readouterr().out
    # dry_run не считается успехом → оба резюме «можно bump»: да
    # и строк «последний bump» (дата) быть не должно, только прочерк
    assert "да" in out
    assert "нет" not in out


def test_local_status_failed_bump_not_treated_as_success(capsys, tmp_path):
    """failed-bump тоже не считается (last_action_at/can_bump_now фильтруют
    status='success'). Неудачное поднятие не делает резюме «кулдаунным»."""
    config = _write_config(tmp_path, _two_resumes_config())
    h = History(tmp_path / "h.db")
    h.record_action("11111111", "11111111", "bump", "failed")

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", local=True, status=True))

    out = capsys.readouterr().out
    assert "да" in out
    assert "нет" not in out


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
    assert "--local" in flags
    # #320: флаг --remote удалён — live-список стал поведением по умолчанию.
    assert "--remote" not in flags


# --- live-дефолт (#320) ------------------------------------------------------


class _FakeCard:
    def __init__(self, resume_id, title, status=None, ssr_unavailable=False):
        self.resume_id = resume_id
        self.title = title
        self.status = status
        self.ssr_unavailable = ssr_unavailable


class _StubThrottle:
    def can_bump_now(self, resume_id):
        return True, None


class _StubHistory:
    def last_action_at(self, resume_id, action):
        return None


def _patch_live(monkeypatch, fake_cards, *, page=None, auth_cookie=True, login_form=False):
    """Стандартная обвязка live-чтения: сессия-файл, браузер, карточки."""
    storage_state = page  # не используется; страница отдельным аргументом ниже
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _FakeContext(page))
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda p: auth_cookie)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda p, url, **kw: None)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda p: login_form)
    monkeypatch.setattr(
        "hhru_bot.copy_resume.list_resume_cards", lambda p, navigate=True: fake_cards
    )
    return storage_state


class _FakeLocator:
    def __init__(self, count: int = 0):
        self._count = count

    def count(self):
        return self._count


class _FakePage:
    """Заглушка Page: только .locator(), нужный has_login_form (#147).

    ``navigated`` фиксирует факт goto (см. test_login_form_check_reads_navigated_page):
    login-форма «появляется» только после навигации, воспроизводя реальную
    последовательность DOM hh.ru."""

    def __init__(self, login_form_count_before: int = 0, login_form_count_after: int = 0):
        self.navigated = False
        self._login_form_count_before = login_form_count_before
        self._login_form_count_after = login_form_count_after

    def locator(self, selector):
        count = self._login_form_count_after if self.navigated else self._login_form_count_before
        return _FakeLocator(count)


class _FakeContext:
    def __init__(self, page: object | None = None):
        self._page = page if page is not None else _FakePage()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def new_page(self):
        return self._page


def _live_env(tmp_path, monkeypatch, config_body=None):
    """Валидная сессия + реальный конфиг с подменённым storage_state-путём."""
    config = _write_config(tmp_path, config_body or _two_resumes_config())
    storage_state = tmp_path / "session.json"
    storage_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: _config_with_storage_state(path, storage_state),
    )
    return config


def _live_rows_pure():
    """Чистый тест строк live-таблицы без браузера/конфига."""
    cards = [
        _FakeCard("11111111", "Backend developer", "modified"),
        _FakeCard("99999999", "", "not_finished"),
    ]
    return cards, {"11111111": "backend"}


def test_live_rows_alias_and_status():
    cards, alias_by_hash = _live_rows_pure()
    rows = list_resumes_cmd._live_rows(
        cards, alias_by_hash, with_status=False, throttle=None, history=None
    )

    assert rows == [
        ["11111111", "backend", "Backend developer", "опубликовано"],
        ["99999999", "—", "—", "черновик"],
    ]


def test_live_rows_status_columns():
    cards, alias_by_hash = _live_rows_pure()
    rows = list_resumes_cmd._live_rows(
        cards,
        alias_by_hash,
        with_status=True,
        throttle=_StubThrottle(),
        history=_StubHistory(),
    )

    assert rows[0] == ["11111111", "backend", "Backend developer", "опубликовано", "да", "—"]
    assert rows[1] == ["99999999", "—", "—", "черновик", "да", "—"]


def test_live_rows_visibility_column_when_available():
    cards = [_FakeCard("11111111", "Backend", "modified")]
    cards[0].is_searchable = False
    rows = list_resumes_cmd._live_rows(
        cards, {}, with_status=True, throttle=_StubThrottle(), history=_StubHistory()
    )
    assert rows == [["11111111", "—", "Backend", "опубликовано", "не видно в поиске", "да", "—"]]


def test_default_live_prints_unified_table(capsys, tmp_path, monkeypatch):
    """#320: без флагов — live-таблица HH.ru с alias из конфига; remote-only
    резюме видно с alias «—» и YAML-подсказкой опциональных настроек."""
    config = _live_env(tmp_path, monkeypatch)
    fake_cards = [
        _FakeCard("11111111", "Backend developer", "modified"),
        _FakeCard("99999999", "Analyst", "approved"),
    ]
    _patch_live(monkeypatch, fake_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "resume_id" in out
    assert "alias" in out
    assert "название" in out
    assert "статус" in out
    assert "11111111" in out and "backend" in out and "Backend developer" in out
    assert "опубликовано" in out
    # 99999999 не в конфиге → alias «—» и YAML-фрагмент опциональных настроек
    assert "99999999" in out
    assert "resume_url" in out
    assert "опциональны" in out
    # локальная таблица конфига (заголовок «| id |») в live-режиме не печатается;
    # slug analyst встречается только в [WARN]-сироте, не в таблице
    assert "\n| id " not in out


def test_default_live_shows_draft_without_position(capsys, tmp_path, monkeypatch):
    """#315: черновик (not_finished) без должности показывается со статусом
    «черновик» и не вызывает падения."""
    config = _live_env(tmp_path, monkeypatch)
    fake_cards = [
        _FakeCard("11111111", "Backend developer", "modified"),
        _FakeCard("99999999", "", "not_finished"),
    ]
    _patch_live(monkeypatch, fake_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "99999999" in out
    assert "черновик" in out
    assert "Должность не указана" not in out  # у нас прочерк, не падение
    assert "—" in out


def test_default_live_empty_overlay_all_dashes(capsys, tmp_path, monkeypatch):
    """#320: пустой overlay (resumes отсутствует) — live-список работает,
    все alias «—», ничего не «не найдено»."""
    config = _live_env(tmp_path, monkeypatch, config_body=_empty_overlay_config())
    fake_cards = [_FakeCard("11111111", "Backend developer", "modified")]
    _patch_live(monkeypatch, fake_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "11111111" in out
    assert "— |" in out.replace("+", "") or "| —" in out
    assert "не найдено" not in out
    assert "опциональны" in out  # подсказка настроек всё ещё полезна


def test_default_live_orphan_config_entries_warn(capsys, tmp_path, monkeypatch):
    """Сироты overlay: запись конфига без резюме на hh.ru — [WARN], не молча."""
    config = _live_env(tmp_path, monkeypatch)
    # жива только карточка 11111111; analyst/22222222 на hh.ru нет
    fake_cards = [_FakeCard("11111111", "Backend developer", "modified")]
    _patch_live(monkeypatch, fake_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "[WARN]" in out
    assert "analyst = 22222222" in out
    assert "настройки не применяются" in out


def test_default_live_status_adds_bump_columns(capsys, tmp_path, monkeypatch):
    """--status в live-режиме: bump-колонки keyed by resume_id работают для
    любых карточек, включая remote-only."""
    config = _live_env(tmp_path, monkeypatch)
    h = History(tmp_path / "h.db")
    h.record_action("11111111", "11111111", "bump", "success")
    fake_cards = [
        _FakeCard("11111111", "Backend developer", "modified"),
        _FakeCard("99999999", "Draft", "not_finished"),
    ]
    _patch_live(monkeypatch, fake_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", status=True))

    out = capsys.readouterr().out
    assert "можно bump" in out
    assert "последний bump" in out
    # 11111111 с недавним bump — «нет (кулдаун)»; 99999999 без истории — «да»
    assert "нет" in out
    assert "да" in out


def test_default_live_status_warns_about_published_hidden_resume(capsys, tmp_path, monkeypatch):
    config = _live_env(tmp_path, monkeypatch)
    card = _FakeCard("11111111", "Backend developer", "modified")
    card.is_searchable = False
    _patch_live(monkeypatch, [card])

    list_resumes_cmd.run(_args(config, tmp_path / "h.db", status=True))

    out = capsys.readouterr().out
    assert "видимость" in out
    assert "не видно в поиске" in out
    assert "Опубликованные резюме не видны" in out


def test_default_live_invalid_session_fail_without_fallback(capsys, tmp_path, monkeypatch):
    """#320: сбой live-чтения — явный [FAIL]; локальная таблица конфига НЕ
    печатается (запрет молчаливого fallback на неполный список)."""
    config = _write_config(tmp_path, _two_resumes_config())

    def _boom(*a, **kw):
        raise AssertionError("не должен открывать браузер без валидной сессии")

    monkeypatch.setattr("hhru_bot.browser.launch_context", _boom)
    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "login" in out
    # никакой локальной таблицы поверх/после FAIL
    assert "backend" not in out
    assert "analyst" not in out


def test_default_live_expired_session_detected_via_auth_cookie(capsys, tmp_path, monkeypatch):
    """_check_session проверяет только формат файла — реальную авторизацию на
    hh.ru подтверждает cookie hhtoken (has_auth_cookie), НЕ browser.is_logged_in()
    (та проверяет "account/login" в URL — приём, отвергнутый в auth.py как
    ненадёжный). Истёкшие cookies не должны маскироваться под «резюме не найдено»."""
    config = _live_env(tmp_path, monkeypatch)

    def _boom_list_cards(page):
        raise AssertionError("list_resume_cards не должен вызываться после провала has_auth_cookie")

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _FakeContext())
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda page: False)
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", _boom_list_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "login" in out
    assert "не найдено" not in out


def test_default_live_unconfirmed_title_selector_warns(capsys, tmp_path, monkeypatch):
    """Если RESUME_LIST_CARD_TITLE не совпал ни для одной карточки — предупредить,
    а не молча выдать прочерки за подтверждённые данные."""
    config = _live_env(tmp_path, monkeypatch)
    fake_cards = [_FakeCard("11111111", ""), _FakeCard("99999999", "")]
    _patch_live(monkeypatch, fake_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "не подтверждён" in out


def test_default_live_indeterminate_state_prints_fail_not_empty(capsys, tmp_path, monkeypatch):
    """Codex adversarial review (PR #136, round 2): list_resume_cards может поднять
    ResumeListIndeterminate (timeout/интерстишл/дрейф селектора — состояние страницы
    не подтверждено). Команда обязана сообщить [FAIL], а не «резюме не найдено»;
    локального fallback тоже нет (#320)."""
    from hhru_bot.copy_resume import ResumeListIndeterminate

    config = _live_env(tmp_path, monkeypatch)

    def _raise_indeterminate(page, navigate=True):
        raise ResumeListIndeterminate("карточки резюме не появились за отведённое время")

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _FakeContext())
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda page: True)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda page, url, **kw: None)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda page: False)
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", _raise_indeterminate)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "не найдено" not in out
    # fallback на локальную таблицу не происходит
    assert "backend" not in out


def test_default_live_stale_cookie_rejects_login_form(capsys, tmp_path, monkeypatch):
    """#147: hhtoken присутствует в jar, но сервер отдал форму входа — сессия
    отвергнута сервером, а не подтверждена одной лишь cookie."""
    config = _live_env(tmp_path, monkeypatch)

    def _boom_list_cards(page, navigate=True):
        raise AssertionError("list_resume_cards не должен вызываться после провала has_login_form")

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _FakeContext())
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda page: True)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda page, url, **kw: None)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda page: True)
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", _boom_list_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "форму входа" in out
    assert "не найдено" not in out


def test_login_form_check_reads_navigated_page(capsys, tmp_path, monkeypatch):
    """Codex adversarial review (PR #152, cycle 1): has_login_form(page) читает
    DOM ТЕКУЩЕЙ страницы. context.new_page() создаёт ещё не навигированную
    страницу — на ней проверка всегда вернула бы 0 совпадений независимо от
    реального состояния сессии, если бы её позвали ДО goto. Этот тест использует
    РЕАЛЬНУЮ browser.has_login_form (не замокана) на _FakePage, где форма входа
    «появляется» только после навигации, — чтобы поймать именно порядок
    goto/has_login_form, а не просто замоканный результат."""
    from hhru_bot.browser import has_login_form as real_has_login_form

    config = _live_env(tmp_path, monkeypatch)

    # Форма входа отсутствует на бланке страницы (count_before=0) и появляется
    # после goto (count_after=1) — так реально ведёт себя hh.ru: отозванная
    # сессия отдаёт форму входа только на РЕАЛЬНО загруженной странице.
    page = _FakePage(login_form_count_before=0, login_form_count_after=1)

    def _boom_list_cards(p, navigate=True):
        raise AssertionError("list_resume_cards не должен вызываться после провала has_login_form")

    def _fake_goto(p, url, **kw):
        p.navigated = True

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: _FakeContext(page))
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda p: True)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", _fake_goto)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", real_has_login_form)
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", _boom_list_cards)

    list_resumes_cmd.run(_args(config, tmp_path / "h.db"))

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "форму входа" in out


def _config_with_storage_state(config_path, storage_state_path):
    from hhru_bot.config import load_config

    config = load_config(config_path)
    config.storage_state_file = storage_state_path
    return config

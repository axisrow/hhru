"""Единый auth-гейт CLI при невалидной сессии (#1129).

Невалидная сессия имитируется в тестах (live-прогонов нет): fake-страница
с формой входа и/или без cookie hhtoken. Проверяются три слоя:
модуль delete_resume (гейт до доменных проверок), census (пометка формы
входа) и граница CLI (единое сообщение + exit 1 для утёкшего
NotAuthenticated).
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.census as census_cmd
import hhru_bot.commands.delete_resume as delete_cmd
import hhru_bot.delete_resume
from hhru_bot.browser import LOGIN_FORM, NotAuthenticated
from hhru_bot.cli import main
from hhru_bot.exit_codes import CommandExitCode

pytestmark = pytest.mark.integration

RESUME_ID = "b" * 38


class _FakeLocator:
    def __init__(self, selectors_with_count):
        self._counts = selectors_with_count

    def count(self) -> int:
        return self._counts


class _LoginPage:
    """Страница входа: cookie hhtoken ещё в jar, но сервер отдал форму входа."""

    def __init__(self, *, cookie: bool = True, login_form: bool = True):
        self.context = SimpleNamespace(cookies=lambda: [{"name": "hhtoken"}] if cookie else [])

        def locator(selector: str) -> _FakeLocator:
            return _FakeLocator(1 if (login_form and selector == LOGIN_FORM) else 0)

        self.locator = locator


CONFIG_BODY = """
    account:
      storage_state_file: data/storage_state/hh_session.json
"""


def _config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_BODY, encoding="utf-8")
    return path


# --- delete-resume: гейт до доменных проверок -------------------------------


def test_delete_resume_invalid_session_raises_not_authenticated(monkeypatch):
    """Истёкшая сессия — NotAuthenticated, а не «карточка не появилась» (#1129)."""
    monkeypatch.setattr(hhru_bot.delete_resume, "goto_hh", lambda page, url, **kw: None)
    monkeypatch.setattr(hhru_bot.browser, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(hhru_bot.browser, "has_login_form", lambda page: True)
    resume = SimpleNamespace(id="training", resume_id=RESUME_ID)
    with pytest.raises(NotAuthenticated):
        hhru_bot.delete_resume.delete_resume_on_hh(_LoginPage(), resume, dry_run=False)


def test_delete_resume_missing_cookie_raises_not_authenticated(monkeypatch):
    monkeypatch.setattr(hhru_bot.delete_resume, "goto_hh", lambda page, url, **kw: None)
    monkeypatch.setattr(hhru_bot.browser, "has_auth_cookie", lambda page: False)
    monkeypatch.setattr(hhru_bot.browser, "has_login_form", lambda page: False)
    resume = SimpleNamespace(id="training", resume_id=RESUME_ID)
    with pytest.raises(NotAuthenticated):
        hhru_bot.delete_resume.delete_resume_on_hh(_LoginPage(cookie=False), resume, True)


def test_delete_resume_command_prints_auth_failure_and_no_uncertain(monkeypatch, tmp_path, capsys):
    """Команда печатает понятную auth-ошибку; pre-click отказ НЕ пишет uncertain."""
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _: SimpleNamespace(
            get_resume=lambda value: SimpleNamespace(id="training", resume_id=RESUME_ID),
            storage_state_file=tmp_path / "session.json",
            user_agent=None,
        ),
    )

    @contextmanager
    def launch(*args, **kwargs):
        yield SimpleNamespace(new_page=lambda: _LoginPage())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", launch)
    monkeypatch.setattr(hhru_bot.delete_resume, "goto_hh", lambda page, url, **kw: None)

    args = argparse.Namespace(
        config="unused",
        history=str(tmp_path / "history.db"),
        headless=True,
        resume="training",
        dry_run=False,
        force=True,
    )
    assert delete_cmd.run(args) is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "Сессия недействительна" in out
    assert "login" in out
    assert "не появилась после загрузки списка" not in out


# --- census: не маскировать страницу входа ----------------------------------


def _census_args(tmp_path, *, as_json: bool) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(_config(tmp_path)),
        headless=True,
        user_agent=None,
        url="https://hh.ru/applicant/my_resumes",
        json=as_json,
        wait_ms=0,
        fill_text=None,
    )


def _patch_census(monkeypatch, *, login_form: bool):
    @contextmanager
    def launch(*args, **kwargs):
        yield SimpleNamespace(new_page=lambda: SimpleNamespace())

    monkeypatch.setattr(census_cmd, "launch_context", launch)
    monkeypatch.setattr(census_cmd, "goto_hh", lambda page, url, **kw: None)
    monkeypatch.setattr(census_cmd, "rendered_controls_census", lambda page: [])
    monkeypatch.setattr(census_cmd, "has_login_form", lambda page: login_form)


def test_census_annotates_login_form(monkeypatch, tmp_path, capsys):
    _patch_census(monkeypatch, login_form=True)
    # #1141: вывод остаётся данными (read-only, «ничего не фейлит»), но
    # подтверждённая страница входа — typed SESSION_EXPIRED для вызывальщика.
    assert census_cmd.run(_census_args(tmp_path, as_json=False)) is (
        CommandExitCode.SESSION_EXPIRED
    )
    out = capsys.readouterr().out
    assert "форма входа" in out
    assert "login" in out


def test_census_json_reports_login_form(monkeypatch, tmp_path, capsys):
    import json

    _patch_census(monkeypatch, login_form=True)
    assert census_cmd.run(_census_args(tmp_path, as_json=True)) is (CommandExitCode.SESSION_EXPIRED)
    out = capsys.readouterr().out
    payload = json.loads(out.split("MACHINE_READABLE_JSON:\n", 1)[1])
    assert payload["login_form"] is True


def test_census_no_annotation_without_login_form(monkeypatch, tmp_path, capsys):
    _patch_census(monkeypatch, login_form=False)
    assert census_cmd.run(_census_args(tmp_path, as_json=False)) is False
    assert "ВНИМАНИЕ" not in capsys.readouterr().out


# --- подсказка агентскому воркеру в тексте отказа (#1194) ---------------------


def test_gate_messages_carry_agent_seed_hint():
    """Обе ветки гейта направляют воркера к storage_state-рецепту, а не к циклу
    «login» (#1194): интерактивный вход воркеру недоступен, сессия живёт в
    файле. Человекная ремедиация `login` в тексте сохранена; ветка с
    отвергнутым hhtoken честно говорит, что пересев того же файла не поможет."""
    from hhru_bot.browser import require_authenticated_page

    with pytest.raises(NotAuthenticated) as no_cookie:
        require_authenticated_page(_LoginPage(cookie=False, login_form=False))
    message = str(no_cookie.value)
    assert "login" in message
    assert "storage_state/hh_session.json" in message
    assert "add_cookies" in message

    with pytest.raises(NotAuthenticated) as rejected:
        require_authenticated_page(_LoginPage(cookie=True, login_form=True))
    message = str(rejected.value)
    assert "login" in message
    assert "пересев того же storage_state не поможет" in message
    assert "import-cookies" in message


# --- граница CLI: единое сообщение и exit-код --------------------------------


def test_cli_converts_leaked_not_authenticated_to_unified_message(monkeypatch, tmp_path, capsys):
    def _boom(args):
        raise NotAuthenticated("страница содержит форму входа при наличии hhtoken")

    monkeypatch.setattr(census_cmd, "run", _boom)
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--config",
                str(_config(tmp_path)),
                "census",
                "--url",
                "https://hh.ru/x",
            ]
        )
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "[FAIL] Вы не авторизованы. Выполните login." in err

"""Контракт delete-photo: dry-run по умолчанию, подтверждение, аудит (#966).

Ключевой инвариант (ревью PR #973): dry_run = not --force, паттерн
delete-resume — голый запуск в TTY не спрашивает «[y/N]» и не выполняет
боевую мутацию, а печатает read-only инвентарь; единственный выход в
боевой режим — явный --force (+ --photo-id).
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.delete_photo as cmd
import hhru_bot.delete_photo
from hhru_bot.delete_photo import DeletePhotoResult, LibraryPhoto
from hhru_bot.history import History

pytestmark = pytest.mark.integration

RESUME_ID = "a" * 38


def _config(tmp_path):
    resume = SimpleNamespace(id="training", resume_id=RESUME_ID, resume_url="https://hh.ru/r")

    def get_resume(value):
        if value != "training":
            from hhru_bot.config import ConfigError

            raise ConfigError("не найдено")
        return resume

    return SimpleNamespace(
        get_resume=get_resume, storage_state_file=tmp_path / "session.json", user_agent=None
    )


def _args(tmp_path, **overrides):
    values = dict(
        config="unused",
        history=str(tmp_path / "history.db"),
        headless=True,
        resume="training",
        photo_id=None,
        from_library=False,
        dry_run=False,
        force=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = SimpleNamespace(result=DeletePhotoResult(success=True, reason="выполнено"))
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _: _config(tmp_path))

    @contextmanager
    def launch(*args, **kwargs):  # noqa: ANN002, ANN003
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", launch)

    def delete(page, resume, photo_id, from_library, dry_run, *, before_click=None):  # noqa: ANN001
        if not dry_run:
            before_click()
        return state.result

    monkeypatch.setattr(hhru_bot.delete_photo, "delete_photo_on_hh", delete)
    return state


def test_no_flags_is_dry_run(env, tmp_path, capsys):
    # Находка ревью PR #973: голый запуск обязан быть dry-run, а не [y/N]
    # сразу в боевую (необратимую при --from-library) мутацию.
    cmd.run(_args(tmp_path, photo_id="100"))
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "[FAIL]" not in out


def test_bare_run_without_photo_id_is_inventory_not_fail(env, tmp_path, capsys):
    env.result = DeletePhotoResult(
        success=True,
        reason="план",
        photos=(LibraryPhoto(photo_id="100", src="https://img.hhcdn.ru/photo/100.jpeg"),),
    )
    cmd.run(_args(tmp_path))
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "photo 100" in out
    assert "[FAIL]" not in out


def test_battle_requires_photo_id(env, tmp_path, capsys):
    assert cmd.run(_args(tmp_path, force=True)) is True
    out = capsys.readouterr().out
    assert "Боевой режим требует --photo-id" in out
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute(
            "SELECT action FROM actions WHERE resume_id = ?", (RESUME_ID,)
        ).fetchone()
    assert row is None  # гейт до браузера и аудита


def test_uncertain_is_audited_and_fails(env, tmp_path, capsys):
    env.result = DeletePhotoResult(success=False, reason="ошибка после клика", uncertain=True)
    assert cmd.run(_args(tmp_path, force=True, photo_id="100")) is True
    assert "uncertain" in capsys.readouterr().out
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute(
            "SELECT status FROM actions WHERE resume_id = ?", (RESUME_ID,)
        ).fetchone()
    assert row["status"] == "uncertain"


def test_live_success_is_recorded_with_hide_action(env, tmp_path):
    assert cmd.run(_args(tmp_path, force=True, photo_id="100")) is False
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute(
            "SELECT action, status FROM actions WHERE resume_id = ?", (RESUME_ID,)
        ).fetchone()
    assert (row["action"], row["status"]) == ("hide_photo", "success")


def test_from_library_audits_delete_action(env, tmp_path):
    assert cmd.run(_args(tmp_path, force=True, photo_id="100", from_library=True)) is False
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute(
            "SELECT action, status FROM actions WHERE resume_id = ?", (RESUME_ID,)
        ).fetchone()
    assert (row["action"], row["status"]) == ("delete_photo", "success")


def test_unresolved_uncertain_blocks_retry(env, tmp_path, capsys):
    history = History(tmp_path / "history.db")
    history.record_action(RESUME_ID, RESUME_ID, "hide_photo", "uncertain", "клик мог уйти")
    assert cmd.run(_args(tmp_path, force=True, photo_id="100")) is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "uncertain" in out

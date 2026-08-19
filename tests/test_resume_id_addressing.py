"""Тесты адресации резюме по реальному resume_id HH.ru (#319).

Контракт: slug из конфига по-прежнему работает (обратная совместимость),
зарегистрированное резюме адресуется и по числовому хэшу, а resume_id без
записи в config.yaml даёт bare-резюме без настроек. Командам, которым нужна
конкретная секция, отсутствие настройки должно давать точечную ошибку
«требуется настройка …», а не вводящее в заблуждение «не найдено в конфиге».
"""

from __future__ import annotations

import argparse
import textwrap
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hhru_bot.commands import publish_resume as publish_cmd
from hhru_bot.commands._common import resolve_resume, resolve_resumes
from hhru_bot.config import ConfigError, bare_resume, load_config

pytestmark = pytest.mark.integration

# Хэш-вид реального resume_id HH.ru (в тестах укороченный): hex без символов
# slug'ов. Не совпадает ни с одним resume_url в тестовом конфиге.
REMOTE_ONLY_HASH = "35661ef3ff10f971a70039ed1f57656d684c54ab"


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _config_body() -> str:
    """Один slug backend → resume_url с числовым хвостом 11111111."""
    return """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: backend
            resume_url: "https://hh.ru/resume/11111111"
            search:
              text: "python developer"
        """


def _args(config_path, history_path, **overrides) -> argparse.Namespace:
    base = {"config": str(config_path), "history": str(history_path), "headless": True}
    base.update(overrides)
    return argparse.Namespace(**base)


# --- конфиг-слой: get_resume по slug и по хэшу -------------------------------


def test_get_resume_by_slug_backward_compatible(tmp_path):
    config = load_config(_write_config(tmp_path, _config_body()))
    assert config.get_resume("backend").resume_id == "11111111"


def test_get_resume_by_real_hash_returns_same_entry(tmp_path):
    config = load_config(_write_config(tmp_path, _config_body()))
    by_hash = config.get_resume("11111111")
    assert by_hash is config.get_resume("backend")


def test_get_resume_unknown_slug_still_raises(tmp_path):
    config = load_config(_write_config(tmp_path, _config_body()))
    with pytest.raises(ConfigError, match="не найдено в конфиге"):
        config.get_resume("nosuchslug")


def test_bare_resume_has_no_settings(tmp_path):
    bare = bare_resume(REMOTE_ONLY_HASH)
    assert bare.id == REMOTE_ONLY_HASH
    assert bare.resume_id == REMOTE_ONLY_HASH
    assert bare.resume_url == f"https://hh.ru/resume/{REMOTE_ONLY_HASH}"
    assert bare.ai_profile is None
    assert bare.education is None
    assert bare.resume_sections is None


# --- резолвер _common.resolve_resume ------------------------------------------


def test_resolve_resume_bare_for_remote_only_hash(tmp_path):
    config = load_config(_write_config(tmp_path, _config_body()))
    resume = resolve_resume(config, REMOTE_ONLY_HASH)
    assert resume.resume_id == REMOTE_ONLY_HASH
    assert resume.ai_profile is None


def test_resolve_resume_unknown_slug_does_not_become_bare(tmp_path):
    config = load_config(_write_config(tmp_path, _config_body()))
    with pytest.raises(ConfigError, match="не найдено в конфиге"):
        resolve_resume(config, "nosuchslug")


def test_resolve_resume_needs_missing_section_for_bare(tmp_path):
    config = load_config(_write_config(tmp_path, _config_body()))
    with pytest.raises(ConfigError, match="требуется настройка 'ai_profile'"):
        resolve_resume(config, REMOTE_ONLY_HASH, needs=("ai_profile",))


def test_resolve_resume_needs_missing_section_for_config_entry(tmp_path):
    # Зарегистрированное резюме без секции — та же точечная ошибка, не «не найдено».
    config = load_config(_write_config(tmp_path, _config_body()))
    with pytest.raises(ConfigError, match="требуется настройка 'education'"):
        resolve_resume(config, "backend", needs=("education",))


def test_resolve_resumes_by_registered_hash(tmp_path):
    config = load_config(_write_config(tmp_path, _config_body()))
    resumes = resolve_resumes(config, ["11111111"])
    assert len(resumes) == 1
    assert resumes[0].id == "backend"


# --- команды: publish-resume работает по resume_id без config.yaml-записи -----


class _PublishResult(SimpleNamespace):
    pass


def _patch_publish(monkeypatch, captured: list):
    import hhru_bot.publish_resume as publish_lib

    @contextmanager
    def fake_launch(*a, **kw):
        yield SimpleNamespace(new_page=lambda: SimpleNamespace())

    def fake_publish(page, resume, dry_run):
        captured.append((resume.id, resume.resume_id, dry_run))
        return _PublishResult(
            success=True, uncertain=False, reason="Черновик готов к публикации", is_searchable=None
        )

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch)
    monkeypatch.setattr(publish_lib, "publish_resume_on_hh", fake_publish)


def test_publish_resume_remote_hash_dry_run_without_config_entry(capsys, tmp_path, monkeypatch):
    """Критерий #319: publish-resume --resume <remote_hash> --dry-run работает
    без записи в config.yaml — никакого «Резюме не найдено в конфиге»."""
    config = _write_config(tmp_path, _config_body())
    captured: list = []
    _patch_publish(monkeypatch, captured)

    publish_cmd.run(
        _args(
            config,
            tmp_path / "h.db",
            resume=REMOTE_ONLY_HASH,
            dry_run=True,
            force=False,
        )
    )

    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "не найдено в конфиге" not in out
    assert captured == [(REMOTE_ONLY_HASH, REMOTE_ONLY_HASH, True)]


def test_publish_resume_registered_hash_uses_config_entry(capsys, tmp_path, monkeypatch):
    """Хэш зарегистрированного резюме резолвится в конфиг-запись (slug backend)."""
    config = _write_config(tmp_path, _config_body())
    captured: list = []
    _patch_publish(monkeypatch, captured)

    publish_cmd.run(
        _args(
            config,
            tmp_path / "h.db",
            resume="11111111",
            dry_run=True,
            force=False,
        )
    )

    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert captured == [("backend", "11111111", True)]


def test_publish_resume_slug_backward_compatible(capsys, tmp_path, monkeypatch):
    config = _write_config(tmp_path, _config_body())
    captured: list = []
    _patch_publish(monkeypatch, captured)

    publish_cmd.run(_args(config, tmp_path / "h.db", resume="backend", dry_run=True, force=False))

    assert captured == [("backend", "11111111", True)]


# --- команды с обязательными секциями: точечная ошибка, не «резюме не найдено» --


def test_edit_education_remote_hash_requires_education_section(capsys, tmp_path):
    from hhru_bot.commands import edit_education as edit_cmd

    config = _write_config(tmp_path, _config_body())
    with pytest.raises(SystemExit) as exc:
        edit_cmd.run(
            _args(
                config,
                tmp_path / "h.db",
                resume=REMOTE_ONLY_HASH,
                section="both",
                source=None,
                mode=None,
                dry_run=True,
                force=False,
            )
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "требуется настройка 'education'" in out
    assert "не найдено в конфиге" not in out


def test_about_remote_hash_requires_ai_profile_section(capsys, tmp_path):
    from hhru_bot.commands import about as about_cmd

    config = _write_config(tmp_path, _config_body())
    with pytest.raises(SystemExit) as exc:
        about_cmd.run(
            _args(config, tmp_path / "h.db", resume=REMOTE_ONLY_HASH, dry_run=True, force=False)
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "требуется настройка 'ai_profile'" in out


# --- локальные WRITE/READ-команды принимают remote-хэш ------------------------


def test_mark_accepts_remote_hash(capsys, tmp_path):
    from hhru_bot.commands import mark as mark_cmd

    config = _write_config(tmp_path, _config_body())
    mark_cmd.run(
        _args(config, tmp_path / "h.db", resume=REMOTE_ONLY_HASH, vacancy="123", status="offer")
    )

    out = capsys.readouterr().out
    assert "[OK]" in out
    assert REMOTE_ONLY_HASH in out


def test_stats_accepts_remote_hash(capsys, tmp_path):
    from hhru_bot.commands import stats as stats_cmd

    config = _write_config(tmp_path, _config_body())
    stats_cmd.run(
        _args(
            config,
            tmp_path / "h.db",
            resume=REMOTE_ONLY_HASH,
            period="all",
            format="table",
            list=False,
            limit=50,
        )
    )

    out = capsys.readouterr().out
    assert "Ошибка конфигурации" not in out
    assert "не найдено" not in out

"""Characterization-тесты config.py: load_config и дата-классы.

Поведение парсинга не должно измениться после ввода config_sections/.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from hhru_bot.config import ConfigError, ResumeConfig, SearchFilters, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _minimal_config() -> str:
    return """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python developer"
    """


def test_load_config_minimal(tmp_path):
    path = _write_config(tmp_path, _minimal_config())
    config = load_config(path)
    assert len(config.resumes) == 1
    resume = config.resumes[0]
    assert resume.id == "r1"
    assert resume.search.text == "python developer"
    # resume_id вычисляется из хвоста resume_url
    assert resume.resume_id == "AAA111"
    # дефолты throttle
    assert config.throttle.daily_apply_limit == 40
    assert config.throttle.min_delay_seconds == 8


def test_load_config_full_search_filters(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/BBB222"
            search:
              text: "data analyst"
              area: 1
              salary_from: 200000
              experience: "between3And6"
              schedule: "remote"
              exclude_employers: ["BadCorp"]
              exclude_keywords: ["1С"]
    """,
    )
    config = load_config(path)
    search: SearchFilters = config.resumes[0].search
    assert search.area == 1
    assert search.salary_from == 200000
    assert search.experience == "between3And6"
    assert search.schedule == "remote"
    assert search.exclude_employers == ["BadCorp"]
    assert search.exclude_keywords == ["1С"]


def test_load_config_cover_letter_default_fallback(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        cover_letter_default: "Default letter for {vacancy_title}"
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/CCC333"
            search:
              text: "x"
    """,
    )
    config = load_config(path)
    resume: ResumeConfig = config.resumes[0]
    assert config.cover_letter_for(resume) == "Default letter for {vacancy_title}"


def test_load_config_cover_letter_override(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/DDD444"
            cover_letter: "Custom for {company_name}"
            search:
              text: "x"
    """,
    )
    config = load_config(path)
    resume = config.resumes[0]
    assert config.cover_letter_for(resume) == "Custom for {company_name}"


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="не найден"):
        load_config(tmp_path / "nope.yaml")


def test_load_config_missing_required_field(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/EEE555"
            # search отсутствует — обязательно
    """,
    )
    with pytest.raises(ConfigError, match="search"):
        load_config(path)


def test_load_config_duplicate_resume_id(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: dup
            resume_url: "https://hh.ru/resume/X1"
            search:
              text: "a"
          - id: dup
            resume_url: "https://hh.ru/resume/X2"
            search:
              text: "b"
    """,
    )
    with pytest.raises(ConfigError, match="Дублирующийся"):
        load_config(path)


def test_get_resume_not_found(tmp_path):
    config = load_config(_write_config(tmp_path, _minimal_config()))
    with pytest.raises(ConfigError, match="не найдено"):
        config.get_resume("nope")


def test_load_config_account_user_agent_default_none(tmp_path):
    # user_agent не задан → None → браузер использует родной UA Playwright (#9).
    config = load_config(_write_config(tmp_path, _minimal_config()))
    assert config.user_agent is None


def test_load_config_account_user_agent_explicit(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
          user_agent: "Mozilla/5.0 (X11; Linux x86_64) Chrome/999.0 Safari/537.36"
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/FFF666"
            search:
              text: "x"
        """,
    )
    config = load_config(path)
    assert config.user_agent == ("Mozilla/5.0 (X11; Linux x86_64) Chrome/999.0 Safari/537.36")


def test_load_config_account_user_agent_wrong_type(tmp_path):
    # user_agent не-строка → ConfigError (контракт валидации типа, #9).
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
          user_agent: 123
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/GGG777"
            search:
              text: "x"
        """,
    )
    with pytest.raises(ConfigError, match="user_agent"):
        load_config(path)


def test_session_secret_path_is_gitignored(tmp_path, monkeypatch):
    # Инвариант безопасности (#23 review): shipped storage_state_file из
    # config.example.yaml должен резолвиться в путь, покрытый .gitignore.
    # Иначе login (auth.py) запишет cookies/localStorage сессии hh.ru в
    # НЕ-ignored файл → account takeover при случайном коммите.
    #
    # Реальный пользовательский кейс: конфиг лежит в config/config.yaml,
    # запуск из корня репо. storage_state_file резолвится относительно cwd
    # (как --config/--history/logs), а НЕ относительно директории конфига —
    # иначе data/... сместится в config/data/... и выйдет из-под .gitignore.
    monkeypatch.chdir(REPO_ROOT)
    config_in_subdir = tmp_path / "config" / "config.yaml"
    config_in_subdir.parent.mkdir(parents=True)
    config_in_subdir.write_text(
        textwrap.dedent(
            """
            account:
              storage_state_file: data/storage_state/hh_session.json
            resumes:
              - id: r1
                resume_url: "https://hh.ru/resume/SEC1"
                search:
                  text: x
            """
        ),
        encoding="utf-8",
    )
    config = load_config(config_in_subdir)

    # Путь резолвится относительно cwd (корень репо), а не config/.
    resolved = Path.cwd() / config.storage_state_file
    ignored = (
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", str(resolved)],
            check=False,
        ).returncode
        == 0
    )
    assert ignored, (
        f"storage_state_file резолвится в НЕ-ignored путь: {resolved}. "
        "Секрет сессии hh.ru может попасть в git-коммит."
    )

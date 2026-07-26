"""Characterization-тесты config.py: load_config и дата-классы.

Поведение парсинга не должно измениться после ввода config_sections/.
"""

from __future__ import annotations

import textwrap

import pytest

from hhru_bot.config import ConfigError, ResumeConfig, SearchFilters, load_config


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

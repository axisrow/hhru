"""Тесты парсинга TOP-LEVEL секции ai через load_config (issue #16, Этап 5).

Секция ai — корневая (как account/throttle), не resume-подсекция. Опциональна:
при отсутствии AppConfig.ai = None. API-ключ НЕ парсится из yaml (только env) —
это проверяется явно: ключ в yaml не должен попасть в AiConfig.
"""

from __future__ import annotations

import textwrap
from dataclasses import fields

import pytest

from hhru_bot.config import ConfigError, load_config
from hhru_bot.config_sections.ai import AiConfig

pytestmark = pytest.mark.unit


def _write(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    # dedent по всему телу, чтобы можно было писать блоки с общим отступом.
    path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return path


def test_ai_section_parsed(tmp_path):
    path = _write(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python"
        ai:
          provider: openai
          model: gpt-4o
          base_url: https://api.openai.com/v1
        """,
    )
    config = load_config(path)
    assert isinstance(config.ai, AiConfig)
    assert config.ai.provider == "openai"
    assert config.ai.model == "gpt-4o"
    assert config.ai.base_url == "https://api.openai.com/v1"


def test_ai_section_optional_defaults_to_none(tmp_path):
    """Без секции ai — AppConfig.ai = None (обратная совместимость)."""
    path = _write(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python"
        """,
    )
    config = load_config(path)
    assert config.ai is None


def test_ai_section_missing_model_raises(tmp_path):
    path = _write(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python"
        ai:
          provider: openai
          base_url: https://api.openai.com/v1
        """,
    )
    with pytest.raises(ConfigError, match="model"):
        load_config(path)


def test_ai_section_non_mapping_raises(tmp_path):
    path = _write(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python"
        ai: "just-a-string"
        """,
    )
    with pytest.raises(ConfigError, match="должна быть отображением"):
        load_config(path)


def test_ai_api_key_not_parsed_from_yaml(tmp_path):
    """api_key в yaml намеренно НЕ читается (только env) — поля api_key в AiConfig нет."""
    path = _write(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python"
        ai:
          provider: openai
          model: gpt-4o
          base_url: https://api.openai.com/v1
          api_key: sk-should-be-ignored
        """,
    )
    config = load_config(path)
    assert config.ai is not None
    # AiConfig — frozen-датакласс ровно с тремя полями; api_key там нет.
    assert {f.name for f in fields(config.ai)} == {"provider", "model", "base_url"}


def test_ai_section_empty_value_raises(tmp_path):
    """Пустая строка в обязательном поле — ConfigError (а не тихая пустота)."""
    path = _write(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python"
        ai:
          provider: ""
          model: gpt-4o
          base_url: https://api.openai.com/v1
        """,
    )
    with pytest.raises(ConfigError, match="непустой строкой"):
        load_config(path)

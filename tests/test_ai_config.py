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
    """Issue #230: пустая секция ai включает AI (AiConfig без полей)."""
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
        ai: {}
        """,
    )
    config = load_config(path)
    assert isinstance(config.ai, AiConfig)
    assert config.ai.provider is None
    assert config.ai.model is None
    assert config.ai.base_url is None


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


def test_ai_section_legacy_routing_fields_fail_closed(tmp_path):
    """Issue #230: legacy provider/model/base_url не констрейнят маршрут → ошибка.

    Маршрутизацию ведёт hermes-agent-axisrow; оператор обязан явно мигрировать.
    """
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
    with pytest.raises(ConfigError, match="устарели"):
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
    """api_key в yaml не читается — и не триггерит fail-closed (это не legacy routing)."""
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
          api_key: sk-should-be-ignored
        """,
    )
    config = load_config(path)
    assert config.ai is not None
    # AiConfig — frozen-датакласс ровно с тремя полями; api_key там нет.
    assert {f.name for f in fields(config.ai)} == {"provider", "model", "base_url"}


def test_ai_section_empty_value_is_legacy_fail_closed(tmp_path):
    """Пустая строка в legacy-поле — fail-closed (это задание устаревшего поля)."""
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
        """,
    )
    with pytest.raises(ConfigError, match="устарели"):
        load_config(path)

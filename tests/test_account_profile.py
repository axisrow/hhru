"""Тесты account_profile: SQL-контракт и приоритет источников."""

from __future__ import annotations

import pytest

from hhru_bot.history import History

pytestmark = pytest.mark.unit


def test_profile_upsert_replaces_only_same_source_and_normalizes_key(tmp_path):
    history = History(tmp_path / "history.db")

    history.upsert_profile_field("  Телефон\n", "+7 900", source="hh_ru")
    history.upsert_profile_field("телефон", "+7 901", source="hh_ru")
    history.upsert_profile_field("телефон", "@user", source="manual")

    assert history.list_profile_fields() == [
        {
            "question_key": "телефон",
            "value": "+7 901",
            "source": "hh_ru",
            "updated_at": history.list_profile_fields()[0]["updated_at"],
        },
        {
            "question_key": "телефон",
            "value": "@user",
            "source": "manual",
            "updated_at": history.list_profile_fields()[1]["updated_at"],
        },
    ]


def test_get_profile_answers_manual_overrides_hh_ru(tmp_path):
    history = History(tmp_path / "history.db")
    history.upsert_profile_field("Имя", "Анна", source="hh_ru")
    history.upsert_profile_field("Телефон", "+7", source="hh_ru")
    history.upsert_profile_field("Имя", "Alice", source="manual")
    history.upsert_profile_field("Telegram", "@alice", source="manual")

    assert history.get_profile_answers() == {
        "имя": "Alice",
        "телефон": "+7",
        "telegram": "@alice",
    }


def test_list_profile_fields_returns_raw_rows(tmp_path):
    history = History(tmp_path / "history.db")
    history.upsert_profile_field("Имя", "Анна", source="hh_ru")
    history.upsert_profile_field("Имя", "Alice", source="manual")

    fields = history.list_profile_fields()

    assert len(fields) == 2
    assert {field["source"] for field in fields} == {"hh_ru", "manual"}
    assert all(set(field) == {"question_key", "value", "source", "updated_at"} for field in fields)

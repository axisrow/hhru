"""Тесты команды probe (#8): построение VacancyCard из URL вакансии.

Характеризация: vacancy_id должен извлекаться канонически (срез query, валидация),
а не наивным split('/')[-1] — иначе ?query-параметр попадает в vacancy_id и в имя
файла дампа. Без браузера — тестируется только чистая функция _vacancy_from_url.
"""

from __future__ import annotations

import pytest

from hhru_bot.commands.probe import _vacancy_from_url

pytestmark = pytest.mark.integration


def test_vacancy_from_url_plain():
    v = _vacancy_from_url("https://hh.ru/vacancy/12345")
    assert v.vacancy_id == "12345"
    assert v.url == "https://hh.ru/vacancy/12345"


def test_vacancy_from_url_strips_query():
    # ?query не должен попадать в vacancy_id и в имя файла дампа
    v = _vacancy_from_url("https://hh.ru/vacancy/12345?from=cl&query=x")
    assert v.vacancy_id == "12345"
    assert "?" not in v.vacancy_id


def test_vacancy_from_url_strips_trailing_slash():
    v = _vacancy_from_url("https://hh.ru/vacancy/12345/")
    assert v.vacancy_id == "12345"


def test_vacancy_from_url_invalid_id_raises():
    with pytest.raises(ValueError):
        _vacancy_from_url("https://hh.ru/vacancy/not-a-number")

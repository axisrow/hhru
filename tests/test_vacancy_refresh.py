from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from hhru_bot.vacancy_refresh import (
    VacancyBody,
    VacancyBodyCache,
    looks_parsed_ok,
    refresh_vacancy_body,
)

pytestmark = pytest.mark.unit

GOOD = (
    "Python backend developer. "
    + "Разработка сервисов и интеграций, тестирование и поддержка production систем. " * 3
)


def test_looks_parsed_ok_rejects_interstitials_and_short_text():
    assert looks_parsed_ok(GOOD)
    assert not looks_parsed_ok("Войдите в аккаунт, чтобы продолжить " + GOOD)
    assert not looks_parsed_ok("короткий текст")


def test_cache_is_bound_to_id_and_url_and_expires():
    cache = VacancyBodyCache(ttl_seconds=10)
    body = VacancyBody("123", "https://hh.ru/vacancy/123", GOOD, datetime.fromtimestamp(100, UTC))
    assert cache.put(body) is body
    assert cache.get("123", body.url, now=105) is body
    assert cache.get("124", body.url, now=105) is None
    assert cache.get("123", body.url, now=111) is None


def test_refresh_does_not_accept_redirect_or_wrong_vacancy():
    page = Mock(url="https://hh.ru/account/login")
    assert refresh_vacancy_body(page, "123", "https://hh.ru/vacancy/123") is None
    page.locator.return_value.inner_text.return_value = GOOD
    page.url = "https://hh.ru/vacancy/124"
    assert refresh_vacancy_body(page, "123", "https://hh.ru/vacancy/123") is None

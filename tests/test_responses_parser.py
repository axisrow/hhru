"""Тесты парсера ответов работодателей (#12): responses.normalize_status + parse_response_card.

normalize_status — чистая функция, тестируется напрямую. parse_response_card
тестируется на HTML-фикстуре /applicant/negotiations через фейковый Playwright
Page (selectsoup-style: locator'ы по data-qa). Без запуска браузера — реально
Playwright используется только для импорта типов, парсинг идёт по разобранному DOM.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from _fakes import NegotiationsPage
from hhru_bot.responses import (
    ResponseItem,
    ResponseStatus,
    _extract_topic,
    _extract_vacancy_id,
    normalize_status,
    parse_response_card,
)


def test_fetch_responses_uses_auth_cookie_not_login_url(monkeypatch):
    from unittest.mock import MagicMock

    from hhru_bot import responses

    page = MagicMock()
    page.url = "https://hh.ru/account/login?successful_redirect=1"
    page.context.cookies.return_value = [{"name": "hhtoken", "value": "abc"}]
    monkeypatch.setattr(responses, "goto_hh", MagicMock())
    page.locator.return_value.count.return_value = 0
    page.locator.return_value.first.wait_for.side_effect = PlaywrightTimeoutError("empty")

    # A valid cookie must not be rejected solely because the URL contains the marker.
    assert responses.fetch_responses(page, max_pages=1) == []


def test_fetch_responses_rejects_missing_auth_cookie_without_url_check(monkeypatch):
    from unittest.mock import MagicMock

    from hhru_bot import responses

    page = MagicMock()
    page.url = "https://hh.ru/applicant/negotiations"
    page.context.cookies.return_value = []
    monkeypatch.setattr(responses, "goto_hh", MagicMock())

    with pytest.raises(responses.NotAuthenticated):
        responses.fetch_responses(page, max_pages=1)


def test_fetch_responses_rejects_login_form_with_stale_auth_cookie(monkeypatch):
    """A server-rendered login form beats a cookie left in the browser jar."""
    from unittest.mock import MagicMock

    from hhru_bot import responses

    page = MagicMock()
    page.context.cookies.return_value = [{"name": "hhtoken", "value": "stale"}]
    monkeypatch.setattr(responses, "goto_hh", MagicMock())

    from hhru_bot.browser import LOGIN_FORM

    def locator(selector):
        result = MagicMock()
        result.count.return_value = int(selector == LOGIN_FORM)
        return result

    page.locator.side_effect = locator

    with pytest.raises(responses.NotAuthenticated, match="форму входа"):
        responses.fetch_responses(page, max_pages=1)


# --- normalize_status (чистая функция) --------------------------------------


def test_normalize_status_invitation_variants():
    assert normalize_status("Приглашение") == ResponseStatus.INVITATION
    assert normalize_status("Приглашен(а) на собеседование") == ResponseStatus.INVITATION
    assert normalize_status("  приглашение  ") == ResponseStatus.INVITATION


def test_normalize_status_discard_variants():
    assert normalize_status("Отказ") == ResponseStatus.DISCARD
    assert normalize_status("Вакансия закрыта") == ResponseStatus.DISCARD
    assert normalize_status("Отклонено") == ResponseStatus.DISCARD


def test_normalize_status_response_messages():
    assert normalize_status("Новое сообщение") == ResponseStatus.RESPONSE
    assert normalize_status("Ответ от работодателя") == ResponseStatus.RESPONSE
    assert normalize_status("Непрочитанные сообщения") == ResponseStatus.RESPONSE


def test_normalize_status_read_and_empty():
    assert normalize_status("Прочитано") == ResponseStatus.READ
    assert normalize_status("Просмотрено") == ResponseStatus.READ
    # None/пусто — нейтральный read (свежий отклик без бейджа).
    assert normalize_status(None) == ResponseStatus.READ
    assert normalize_status("") == ResponseStatus.READ
    assert normalize_status("   ") == ResponseStatus.READ


def test_normalize_status_unknown_preserved():
    assert normalize_status("Какой-то новый бейдж") == ResponseStatus.UNKNOWN


# --- _extract_vacancy_id ----------------------------------------------------


def test_extract_vacancy_id_path_tail():
    assert _extract_vacancy_id("/vacancy/12345?from=serp") == "12345"
    assert _extract_vacancy_id("https://hh.ru/applicant/vacancy/98765") == "98765"


def test_extract_vacancy_id_from_chat_query():
    # Ссылка чата: vacancyId в query-параметре.
    assert _extract_vacancy_id("/applicant/negotiations?topic=77&vacancyId=4242") == "4242"


def test_extract_vacancy_id_empty_and_garbage():
    assert _extract_vacancy_id("") is None
    assert _extract_vacancy_id("/vacancy/abc") is None


# --- _extract_topic (идентификатор переписки из chat_url) -------------------


def test_extract_topic_from_chat_url():
    assert _extract_topic("/applicant/negotiations?topic=77&vacancyId=12345") == "77"
    assert _extract_topic("https://hh.ru/applicant/negotiations?topic=42") == "42"


def test_extract_topic_none_when_absent():
    # Без topic (ответ без чата, fallback на карточку вакансии).
    assert _extract_topic("/vacancy/12345") is None
    assert _extract_topic("") is None
    assert _extract_topic(None) is None


# --- parse_response_card на HTML-фикстуре -----------------------------------
#
# Фикстура повторяет структуру карточки /applicant/negotiations по data-qa из
# selector_groups/negotiations.py. Реальный DOM hh.ru сверяется вручную (см.
# модуль селекторов — НЕ подтверждено), но парсер детерминирован на этой разметке:
# data-qa вакансия/работодатель/статус/чат-ссылка → ResponseItem.

_NEGOTIATIONS_HTML = """
<div data-qa="negotiations-item">
  <a data-qa="negotiations-item__vacancy-link" href="/vacancy/111111?from=responses">Python Developer</a>
  <span data-qa="negotiations-item__employer">ACME Corp</span>
  <span data-qa="negotiations-item__state">Приглашение</span>
  <span data-qa="negotiations-item__date">сегодня, 14:05</span>
  <a data-qa="negotiations-item__messages-link" href="/applicant/negotiations?topic=1">Чат</a>
</div>
<div data-qa="negotiations-item">
  <a data-qa="negotiations-item__vacancy-link" href="/applicant/vacancy/222222">Backend Engineer</a>
  <span data-qa="negotiations-item__employer">Beta LLC</span>
  <span data-qa="negotiations-item__state">Отказ</span>
</div>
<div data-qa="negotiations-item">
  <a data-qa="negotiations-item__vacancy-link" href="/vacancy/333333">Data Analyst</a>
  <!-- работодатель скрыт hh.ru, статуса-бейджа нет (свежий отклик без ответа) -->
</div>
<div data-qa="negotiations-item">
  <!-- битая карточка: нет ссылки вакансии -->
  <span data-qa="negotiations-item__state">Прочитано</span>
</div>
"""


def test_parse_response_card_invitation():
    page = NegotiationsPage(_NEGOTIATIONS_HTML)
    card = page.items[0]
    item = parse_response_card(card)
    assert item is not None
    assert item.vacancy_id == "111111"
    assert item.employer == "ACME Corp"
    assert item.status == ResponseStatus.INVITATION
    assert item.raw_status == "Приглашение"
    # chat_url СОХРАНЯЕТ query (topic=1): без него ссылка ведёт в общий список,
    # а не в конкретную переписку (регрессия: раньше _absolute_url срезал query).
    assert item.chat_url == "https://hh.ru/applicant/negotiations?topic=1"
    assert item.topic == "1"  # идентификатор переписки извлечён из chat_url
    assert item.date == "сегодня, 14:05"


def test_parse_response_card_discard_no_chat_link_falls_back_to_vacancy():
    """Отдельной чат-ссылки нет (отказ) — chat_url fallback на карточку вакансии."""
    page = NegotiationsPage(_NEGOTIATIONS_HTML)
    item = parse_response_card(page.items[1])
    assert item is not None
    assert item.vacancy_id == "222222"
    assert item.status == ResponseStatus.DISCARD
    assert item.chat_url == "https://hh.ru/applicant/vacancy/222222"
    # блока даты нет во второй карточке → пустая строка.
    assert item.date == ""
    # chat_url без topic (fallback на вакансию) → topic None.
    assert item.topic is None


def test_parse_response_card_fresh_apply_read_empty_fields():
    """Свежий отклик: работодатель скрыт, бейджа/даты нет → read, поля пустые."""
    page = NegotiationsPage(_NEGOTIATIONS_HTML)
    item = parse_response_card(page.items[2])
    assert item is not None
    assert item.vacancy_id == "333333"
    assert item.status == ResponseStatus.READ
    assert item.employer == ""
    assert item.raw_status == ""
    assert item.date == ""


def test_parse_response_card_missing_vacancy_link_returns_none():
    """Нет ссылки на вакансию → не из чего достать vacancy_id → None (пропуск)."""
    page = NegotiationsPage(_NEGOTIATIONS_HTML)
    assert parse_response_card(page.items[3]) is None


_LIVE_STATUS_ONLY_HTML = """
<div data-qa="negotiations-item">
  <a data-qa="negotiations-item-vacancy" href="/vacancy/444444">Data Engineer</a>
  <span data-qa="negotiations-tag negotiations-item-not-viewed">Приглашение</span>
</div>
"""


def test_parse_response_card_falls_back_to_live_status_selector():
    """Нет legacy-разметки (только текущий data-qa) — NEGOTIATION_STATUS (prefix
    selector) должен подхватить бейдж, а не оставить статус пустым."""
    page = NegotiationsPage(_LIVE_STATUS_ONLY_HTML)
    item = parse_response_card(page.items[0])
    assert item is not None
    assert item.status == ResponseStatus.INVITATION


_UNRECOGNIZED_LEGACY_STATUS_HTML = """
<div data-qa="negotiations-item">
  <a data-qa="negotiations-item__vacancy-link" href="/vacancy/555555">QA Engineer</a>
  <span data-qa="negotiations-item__state">Незнакомый статус</span>
  <span data-qa="negotiations-tag negotiations-item-not-viewed">Приглашение</span>
</div>
"""


def test_parse_response_card_unrecognized_legacy_status_falls_through_to_live_selector():
    """Legacy-селектор нашёл непустой, но нераспознанный текст — статус не должен
    молча обнуляться: живой (prefix) селектор всё ещё может дать известный статус."""
    page = NegotiationsPage(_UNRECOGNIZED_LEGACY_STATUS_HTML)
    item = parse_response_card(page.items[0])
    assert item is not None
    assert item.status == ResponseStatus.INVITATION


_LIVE_STATUS_HTML = """
<div data-qa="negotiations-item">
  <a data-qa="negotiations-item-vacancy" href="/vacancy/444444">Data Engineer</a>
  <span data-qa="negotiations-tag_negotiations-item-not-viewed">Новое сообщение</span>
</div>
<div data-qa="negotiations-item">
  <a data-qa="negotiations-item-vacancy" href="/vacancy/555555">ML Engineer</a>
  <span data-qa="negotiations-tag_negotiations-item-viewed">Прочитано</span>
</div>
"""


def test_parse_response_card_reads_live_not_viewed_status_without_legacy_markup():
    """Регрессия #186: NEGOTIATION_STATUS был exact-match и НИКОГДА не совпадал
    с реальной разметкой hh.ru (см. selector_groups/negotiations.py) — карточка
    без legacy data-qa молча теряла статус (падала в UNKNOWN/READ). Живая
    разметка (не legacy __state) должна распознаваться prefix-селектором.
    """
    page = NegotiationsPage(_LIVE_STATUS_HTML)
    item = parse_response_card(page.items[0])
    assert item is not None
    assert item.status == ResponseStatus.RESPONSE
    assert item.raw_status == "Новое сообщение"


def test_parse_response_card_reads_live_viewed_status_without_legacy_markup():
    page = NegotiationsPage(_LIVE_STATUS_HTML)
    item = parse_response_card(page.items[1])
    assert item is not None
    assert item.status == ResponseStatus.READ
    assert item.raw_status == "Прочитано"


def test_parse_response_card_prefers_live_fields_when_both_markups_exist():
    """Confirmed selectors win consistently over compatibility selectors."""
    page = NegotiationsPage(
        """
        <div data-qa="negotiations-item">
          <a data-qa="negotiations-item-vacancy" href="/vacancy/666666">Live</a>
          <a data-qa="negotiations-item__vacancy-link" href="/vacancy/777777">Legacy</a>
          <span data-qa="negotiations-tag">Новое сообщение</span>
          <span data-qa="negotiations-item__state">Отказ</span>
          <span data-qa="negotiations-item-company">Live Corp</span>
          <span data-qa="negotiations-item__employer">Legacy Corp</span>
          <span data-qa="negotiations-item-date">сегодня</span>
          <span data-qa="negotiations-item__date">вчера</span>
        </div>
        """
    )
    item = parse_response_card(page.items[0])
    assert item is not None
    assert item.vacancy_id == "666666"
    assert item.status == ResponseStatus.RESPONSE
    assert item.employer == "Live Corp"
    assert item.date == "сегодня"


def test_parse_response_card_uses_legacy_text_when_live_fields_are_empty():
    page = NegotiationsPage(
        """
        <div data-qa="negotiations-item">
          <a data-qa="negotiations-item-vacancy" href="/vacancy/888888">Live</a>
          <span data-qa="negotiations-item__state">Приглашение</span>
          <span data-qa="negotiations-item-company"></span>
          <span data-qa="negotiations-item__employer">Legacy Corp</span>
          <span data-qa="negotiations-item-date"> </span>
          <span data-qa="negotiations-item__date">вчера</span>
        </div>
        """
    )
    item = parse_response_card(page.items[0])
    assert item is not None
    assert item.status == ResponseStatus.INVITATION
    assert item.raw_status == "Приглашение"
    assert item.employer == "Legacy Corp"
    assert item.date == "вчера"


def test_parse_response_card_keeps_unrecognized_live_status_over_recognized_legacy():
    """Присутствующий live-статус авторитетен даже если normalize_status его не знает —
    легаси-статус не должен маскировать реальный (пусть и нераспознанный) live-статус."""
    page = NegotiationsPage(
        """
        <div data-qa="negotiations-item">
          <a data-qa="negotiations-item-vacancy" href="/vacancy/888888">Live</a>
          <span data-qa="negotiations-tag">Unknown live status</span>
          <span data-qa="negotiations-item__state">Приглашение</span>
        </div>
        """
    )
    item = parse_response_card(page.items[0])
    assert item is not None
    assert item.status == ResponseStatus.UNKNOWN
    assert item.raw_status == "Unknown live status"


def test_response_item_dataclass_fields():
    """Контракт dataclass: status обязателен, остальное имеет дефолты."""
    item = ResponseItem(vacancy_id="42", status=ResponseStatus.READ)
    assert item.employer == ""
    assert item.chat_url is None
    assert item.topic is None
    assert item.date == ""
    assert item.raw_status == ""

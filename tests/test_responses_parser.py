"""Тесты парсера ответов работодателей (#12): responses.normalize_status + parse_response_card.

normalize_status — чистая функция, тестируется напрямую. parse_response_card
тестируется на HTML-фикстуре /applicant/negotiations через фейковый Playwright
Page (selectsoup-style: locator'ы по data-qa). Без запуска браузера — реально
Playwright используется только для импорта типов, парсинг идёт по разобранному DOM.
"""

from __future__ import annotations

from _fakes import NegotiationsPage
from hhru_bot.responses import (
    ResponseItem,
    ResponseStatus,
    _extract_vacancy_id,
    normalize_status,
    parse_response_card,
)

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


def test_response_item_dataclass_fields():
    """Контракт dataclass: status обязателен, остальное имеет дефолты."""
    item = ResponseItem(vacancy_id="42", status=ResponseStatus.READ)
    assert item.employer == ""
    assert item.chat_url is None
    assert item.date == ""
    assert item.raw_status == ""

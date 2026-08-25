"""Тесты apply/verify.py (#207): внешняя проверка вердикта по negotiations.

Без браузера: фейковый Playwright Page поверх HTML-страниц /applicant/negotiations
(тот же стиль, что test_responses_parser.py — DOM через html.parser из _fakes).
Проверяются три исхода (found/not_found/indeterminate), polling-поведение,
DOM-fallback при недоступности SSR и терпимость к неподтверждённой пагинации.
"""

from __future__ import annotations

import json
from html import escape
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

import hhru_bot.apply.verify as verify_module
from _fakes import FakeLocator, _CardLocator, _parse_root, _parse_selector
from hhru_bot.apply.antibot import AntiBotChallengeDetected, AntiBotDetection
from hhru_bot.apply.verdict import (
    Completeness,
    PageRead,
    PageSource,
    Partial,
    ResumeAttribution,
    TopicRead,
    compose,
)
from hhru_bot.apply.verify import verify_response_in_negotiations
from hhru_bot.responses import NEGOTIATIONS_URL
from hhru_bot.selector_groups import negotiations as ns

pytestmark = pytest.mark.integration

_V1, _V2 = "111111", "222222"


@pytest.fixture(autouse=True)
def _mock_navigation_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retry semantics while skipping production backoff sleeps.

    The fake pages deliberately exhaust ``goto_hh`` retries in several
    fail-closed cases.  Sleeping for the real 2s + 4s backoff per attempt adds
    no coverage here: the navigation/retry assertions still execute with a
    no-op clock.
    """
    monkeypatch.setattr("hhru_bot.browser.time.sleep", lambda _seconds: None)


@pytest.mark.parametrize(
    ("reads", "expected"),
    [
        ([PageRead(PageSource.SSR, (TopicRead(_V1),), Completeness.LAST_CONFIRMED)], "found"),
        ([PageRead(PageSource.DOM, (), Completeness.LAST_CONFIRMED)], "not_found"),
        ([PageRead(PageSource.SSR, (), Partial("pagination"))], "indeterminate"),
        (
            [
                PageRead(
                    PageSource.SSR,
                    (TopicRead(_V1, ResumeAttribution.INCOMPARABLE),),
                    Completeness.LAST_CONFIRMED,
                )
            ],
            "indeterminate",
        ),
        (
            [
                PageRead(PageSource.SSR, (), Completeness.LAST_CONFIRMED),
                PageRead(PageSource.DOM, (), Partial("render")),
            ],
            "indeterminate",
        ),
    ],
)
def test_verdict_compose_matrix(reads, expected):
    assert compose(reads, _V1) == expected


class _PageLocator:
    """Страничный локатор: count/nth/first по всему документу (как page.locator).

    nth(i) возвращает _CardLocator — parse_response_card вызывает
    item.locator(...) с data-qa-селекторами, которые умеет только _CardLocator.
    """

    def __init__(self, root, qa_match):  # noqa: ANN001
        self._root = root
        self._qa_match = qa_match
        self._matches: list | None = None

    def _resolved(self) -> list:
        if self._matches is None:
            self._matches = self._root.find_all(tag=None, qa_match=self._qa_match)
        return self._matches

    def count(self) -> int:
        return len(self._resolved())

    @property
    def first(self) -> FakeLocator:
        matches = self._resolved()
        if matches:
            return _CardLocator(matches[0], lambda _v: True, matches=[matches[0]])
        # Пустая коллекция: wait_for обязан кинуть PlaywrightTimeoutError,
        # как настоящий локатор на не появившемся элементе.
        return FakeLocator(self._root, self._qa_match, matches=[])

    def nth(self, i: int) -> FakeLocator:
        node = self._resolved()[i]
        return _CardLocator(node, lambda _v: True, matches=[node])


class FakeNegotiationsPage:
    """Имитация Playwright Page: страницы по URL, cookies, content, locator."""

    def __init__(
        self,
        pages: dict[str, str] | None = None,
        *,
        authed: bool = True,
        goto_error: Exception | None = None,
    ):
        self._pages = {} if pages is None else dict(pages)
        self._authed = authed
        self._goto_error = goto_error
        self._html = ""
        self.goto_calls: list[str] = []
        self.wait_for_timeout_calls: list[int] = []

    def goto(self, url: str, *, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)
        if self._goto_error is not None:
            raise self._goto_error
        self._html = self._pages.get(url, "")

    def content(self) -> str:
        return self._html

    @property
    def context(self) -> SimpleNamespace:
        cookies = [{"name": "hhtoken", "value": "x"}] if self._authed else []
        return SimpleNamespace(cookies=lambda: cookies)

    def locator(self, selector: str) -> _PageLocator:
        return _PageLocator(_parse_root(self._html), _parse_selector(selector))

    def wait_for_timeout(self, ms: int) -> None:
        self.wait_for_timeout_calls.append(ms)

    def screenshot(self, *, full_page: bool = True) -> bytes:  # noqa: ARG002
        return b"<png>"


def _topic(topic_id: int, vacancy_id: str, resume_id: str | None = None) -> dict:
    topic = {"id": topic_id, "chatId": topic_id, "vacancyId": vacancy_id}
    if resume_id is not None:
        topic["resumeId"] = resume_id
    return topic


def _ssr_html(topics: list[dict], extra: str = "") -> str:
    state = json.dumps({"applicantNegotiations": {"topicList": topics}}, ensure_ascii=False)
    return (
        "<html><body>"
        f"<template id='HH-Lux-InitialState'>{escape(state)}</template>"
        f"{extra}"
        "</body></html>"
    )


def test_verifier_challenge_stops_before_retry(monkeypatch):
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: "<html></html>"})
    detection = AntiBotDetection("url_path", "URL содержит /captcha")

    def _challenge(_page):
        raise AntiBotChallengeDetected(detection)

    monkeypatch.setattr(verify_module, "raise_for_antibot", _challenge)

    with pytest.raises(AntiBotChallengeDetected):
        verify_response_in_negotiations(page, _V1)

    assert page.goto_calls == [NEGOTIATIONS_URL]
    assert page.wait_for_timeout_calls == []


def test_verifier_checks_challenge_after_failed_navigation(monkeypatch):
    page = FakeNegotiationsPage(goto_error=PlaywrightError("navigation timed out"))
    detection = AntiBotDetection("url_path", "URL содержит /captcha")
    monkeypatch.setattr(
        verify_module,
        "raise_for_antibot",
        lambda _page: (_ for _ in ()).throw(AntiBotChallengeDetected(detection)),
    )

    with pytest.raises(AntiBotChallengeDetected):
        verify_response_in_negotiations(page, _V1)

    assert page.wait_for_timeout_calls == []


# DOM-разметка без SSR-состояния: карточка с span-вакансией внутри <a> (#44).
_DOM_HTML = """
<div data-qa="negotiations-item">
  <a href="/vacancy/111111"><span data-qa="negotiations-item-vacancy">Python</span></a>
  <span data-qa="negotiations-tag negotiations-item-not-viewed">Не просмотрен</span>
</div>
<div data-qa="negotiations-item">
  <a href="/vacancy/999999"><span data-qa="negotiations-item-vacancy">Go</span></a>
</div>
"""


# --- found -------------------------------------------------------------------


def test_found_via_ssr_topic():
    page = FakeNegotiationsPage(
        {NEGOTIATIONS_URL: _ssr_html([_topic(7, _V1), _topic(8, _V2, "R1")])}
    )
    result = verify_response_in_negotiations(page, _V2)
    assert result.found
    assert "topic=8" in result.detail
    assert "resumeId=R1" in result.detail
    assert page.goto_calls == [NEGOTIATIONS_URL]  # found — без второй попытки


def test_incomparable_resume_topic_without_account_ids():
    # #212: тема с ДРУГИМ resumeId при неизвестном перечне резюме аккаунта —
    # НЕ not_found (как было до #212 — «чужое» доказывалось сравнением строк
    # из несовместимых доменов id). Доказать нельзя ни matched, ни foreign →
    # fail-closed indeterminate: вердикт за uncertain-логикой pipeline.
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _ssr_html([_topic(8, _V2, "R1")])})
    result = verify_response_in_negotiations(page, _V2, resume_id="R2")
    assert result.status == "indeterminate"
    assert "атрибуция" in result.detail


def test_foreign_resume_topic_with_account_ids_does_not_confirm_apply():
    # Перечень резюме аккаунта известен: id темы вне перечня — доказуемо
    # чужое → скан продолжается → чистый not_found (исходная семантика
    # #207 сохранена для доказуемого случая).
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _ssr_html([_topic(8, _V2, "R1")])})
    result = verify_response_in_negotiations(
        page, _V2, resume_id="R2", account_resume_ids={"R2", "R3"}
    )
    assert result.status == "not_found"


# --- #212: реальные домены id (хэш конфига vs числовой SSR) ------------------
#
# Урок #212: тесты с «R1» против «R1» не ловят прод-баг — обе стороны в одном
# домене. Дальше — формы живого аккаунта 2026-08-16 (probe212): хэш конфига,
# числовой id конфиг-резюме и числовой id default-резюме (форма отклика не
# предлагает not_finished-резюме, и тема подписывается default-резюме).
_HASH = "b3236ebbff10f60ff30039ed1f6d5876645331"
_NUM_CONFIG = "284561395"  # python (not_finished)
_NUM_DEFAULT = "96223331"  # marketing — резюме по умолчанию формы отклика
_ACCOUNT = {_NUM_CONFIG, _NUM_DEFAULT}


def test_regression_212_config_hash_vs_numeric_topic_is_indeterminate():
    """Регрессия #212 ровно как в проде: верификатору передали хэш конфига
    (резолвер не отработал), SSR-тема несёт числовой resumeId. До фикса —
    «чужое» → чистый not_found → false negative (135170581); после —
    fail-closed indeterminate."""
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _ssr_html([_topic(8, _V2, 96223331)])})
    result = verify_response_in_negotiations(page, _V2, resume_id=_HASH)
    assert result.status == "indeterminate"
    assert "несовместимые домены id" in result.detail
    assert "96223331" in result.detail and _HASH in result.detail


def test_other_own_resume_topic_is_indeterminate():
    """Тема с ДРУГИМ собственным резюме аккаунта (форма приложила default
    marketing 96223331, конфиг — python 284561395) НЕ подтверждает текущий
    apply: тема могла быть создана ПРЕДЫДУЩИМ откликом, а не этим кликом
    (Codex-ревью цикла 2). Подтверждать по ней нельзя — иначе ложный success
    под конфиг-резюме и перманентная дедупликация подавят повторную попытку.
    Fail-closed indeterminate (как incomparable), а не found."""
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _ssr_html([_topic(8, _V2, 96223331)])})
    result = verify_response_in_negotiations(
        page, _V2, resume_id=_NUM_CONFIG, account_resume_ids=_ACCOUNT
    )
    assert result.status == "indeterminate"
    assert "ДРУГИМ собственным резюме" in result.detail
    assert "96223331" in result.detail and _NUM_CONFIG in result.detail


def test_exact_match_wins_over_other_own_resume_topic():
    # Среди тем вакансии есть и с конфиг-резюме (exact match) — подтверждение,
    # даже если рядом тема с другим собственным резюме (не атрибутируемая).
    page = FakeNegotiationsPage(
        {NEGOTIATIONS_URL: _ssr_html([_topic(7, _V2, 96223331), _topic(8, _V2, _NUM_CONFIG)])}
    )
    result = verify_response_in_negotiations(
        page, _V2, resume_id=_NUM_CONFIG, account_resume_ids=_ACCOUNT
    )
    assert result.found
    assert f"resumeId={_NUM_CONFIG}" in result.detail


def test_found_by_direct_numeric_match_without_account_ids():
    # Равенство строк надёжно в любом домене: числовой против числового.
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _ssr_html([_topic(8, _V2, 96223331)])})
    result = verify_response_in_negotiations(page, _V2, resume_id=_NUM_DEFAULT)
    assert result.found
    assert "другое резюме" not in result.detail


def test_not_found_when_topic_resume_outside_account():
    # id темы вне перечня аккаунта (аномалия данных) — доказуемо чужое.
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _ssr_html([_topic(8, _V2, 999999999)])})
    result = verify_response_in_negotiations(
        page, _V2, resume_id=_NUM_CONFIG, account_resume_ids=_ACCOUNT
    )
    assert result.status == "not_found"


def test_found_matching_resume_has_no_mismatch_note():
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _ssr_html([_topic(8, _V2, "R1")])})
    result = verify_response_in_negotiations(page, _V2, resume_id="R1")
    assert result.found
    assert "ДРУГОГО" not in result.detail


def test_found_when_matching_resume_among_foreign():
    # Среди откликов на вакансию есть и с текущего резюме (R2) — подтверждение.
    page = FakeNegotiationsPage(
        {NEGOTIATIONS_URL: _ssr_html([_topic(7, _V2, "R1"), _topic(8, _V2, "R2")])}
    )
    result = verify_response_in_negotiations(page, _V2, resume_id="R2")
    assert result.found
    assert "resumeId=R2" in result.detail


def test_found_via_dom_fallback_without_ssr():
    # SSR-шаблон не нашёлся — DOM-карточки остаются источником (selector #44:
    # vacancy_id из href родительского <a>).
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _DOM_HTML})
    result = verify_response_in_negotiations(page, _V1)
    assert result.found
    assert "DOM" in result.detail


def test_found_on_second_page():
    # Свежий отклик обычно на странице 0, но скан следует и за пагинатором.
    page0 = _ssr_html([_topic(7, "999999")], extra="<a data-qa='pager-next'>далее</a>")
    page1 = _ssr_html([_topic(9, _V2)])
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: page0, f"{NEGOTIATIONS_URL}?page=1": page1})
    result = verify_response_in_negotiations(page, _V2)
    assert result.found
    assert f"{NEGOTIATIONS_URL}?page=1" in page.goto_calls


# --- not_found: подтверждённое отсутствие ------------------------------------


def test_not_found_on_clean_ssr_read():
    page = FakeNegotiationsPage(
        {NEGOTIATIONS_URL: _ssr_html([_topic(7, "999999"), _topic(8, "888888")])}
    )
    result = verify_response_in_negotiations(page, _V2)
    assert result.status == "not_found"
    # Polling: обе попытки с интервалом (отклик мог появиться с задержкой).
    assert page.goto_calls == [NEGOTIATIONS_URL, NEGOTIATIONS_URL]
    assert page.wait_for_timeout_calls == [10_000]


def test_not_found_on_server_rendered_empty_list():
    # topicList=[] — сервер честно отрисовал пустой список: это чистое чтение,
    # а не «не отрендерилось».
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _ssr_html([])})
    result = verify_response_in_negotiations(page, _V2)
    assert result.status == "not_found"


def test_not_found_when_dom_cards_render_without_ssr():
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _DOM_HTML})
    result = verify_response_in_negotiations(page, _V2)
    assert result.status == "not_found"


def test_unconfirmed_pagination_does_not_break_absence_verdict():
    # pager-block есть, но ни pager-next, ни pager-page не появились —
    # ResponsesIndeterminate не должен превращать отсутствие в indeterminate:
    # свежий отклик был бы на странице 0 (сортировка по свежести).
    html = _ssr_html([_topic(7, "999999")], extra="<div data-qa='pager-block'></div>")
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: html})
    result = verify_response_in_negotiations(page, _V2)
    assert result.status == "not_found"


# --- indeterminate: список не прочитан достоверно -----------------------------


def test_indeterminate_when_goto_fails():
    page = FakeNegotiationsPage(goto_error=PlaywrightError("net::ERR_TIMED_OUT"))
    result = verify_response_in_negotiations(page, _V2)
    assert result.indeterminate
    assert "goto" in result.detail


def test_indeterminate_when_session_lost():
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _ssr_html([_topic(7, _V2)])}, authed=False)
    result = verify_response_in_negotiations(page, _V2)
    assert result.indeterminate
    assert "не авторизована" in result.detail


def test_indeterminate_when_neither_ssr_nor_cards_rendered():
    # Пустой HTML без SSR-состояния и без карточек — «пустой inbox» отличить
    # от сломанного рендера нельзя, честный ответ — indeterminate.
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: "<html><body></body></html>"})
    result = verify_response_in_negotiations(page, _V2)
    assert result.indeterminate
    assert "не отрендерился" in result.detail


def test_indeterminate_when_ssr_lacks_negotiations_section():
    # SSR-состояние распарсилось, но секции applicantNegotiations нет — это
    # «не отрендерилось», а не «пустой список»: иначе ложный not_found
    # (false negative, который #207 и предотвращает).
    state = json.dumps({"someOtherSection": {}}, ensure_ascii=False)
    html = (
        f"<html><body><template id='HH-Lux-InitialState'>{escape(state)}</template></body></html>"
    )
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: html})
    result = verify_response_in_negotiations(page, _V2)
    assert result.indeterminate


def test_indeterminate_when_ssr_state_is_non_dict():
    # parse_initial_state возвращает любой валидный JSON, не только объект:
    # null/массив/строка (schema-drift) не должны ронять верификатор
    # AttributeError'ом вне try — нормализуются как «состояние недоступно»
    # (fail-closed indeterminate, а не ложный not_found).
    for raw in ("null", "[1,2]", '"строка"'):
        html = f"<html><body><template id='HH-Lux-InitialState'>{raw}</template></body></html>"
        page = FakeNegotiationsPage({NEGOTIATIONS_URL: html})
        result = verify_response_in_negotiations(page, _V2)
        assert result.indeterminate


def test_indeterminate_when_vacancy_id_unknown():
    result = verify_response_in_negotiations(FakeNegotiationsPage(), None)
    assert result.indeterminate
    assert "vacancy_id" in result.detail


def test_dom_fallback_with_resume_id_is_indeterminate():
    # DOM-карточка не несёт resumeId — при заданном resume_id не можем
    # атрибутировать отклик к текущему резюме: fail-closed indeterminate,
    # а не ложный success (как SSR-путь, #207).
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _DOM_HTML})
    result = verify_response_in_negotiations(page, _V1, resume_id="R2")
    assert result.indeterminate
    assert "атрибуци" in result.detail


def test_indeterminate_when_dom_card_unparseable():
    # Одна карточка не прочиталась (нет vacancy-ссылки) — отсутствие целевой
    # вакансии не подтверждаем: она могла быть в непрочитанной карточке
    # (иначе ложный not_found, #207).
    html = """
    <div data-qa="negotiations-item">
      <a href="/vacancy/999999"><span data-qa="negotiations-item-vacancy">Go</span></a>
    </div>
    <div data-qa="negotiations-item">
      <span data-qa="negotiations-item-vacancy">No link</span>
    </div>
    """
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: html})
    result = verify_response_in_negotiations(page, _V2)
    assert result.indeterminate


class _Page1FailsPage(FakeNegotiationsPage):
    """Страница 0 грузится, переход на страницу 1 падает (goto_hh ретраит и
    пробрасывает PlaywrightError на последней попытке)."""

    def goto(self, url: str, *, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)
        if "?page=1" in url:
            raise PlaywrightError("net::ERR_TIMED_OUT")
        self._html = self._pages.get(url, "")


def test_indeterminate_when_page1_goto_fails():
    # Страница 0 прочитана чисто (вакансии нет), но переход на страницу 1 упал —
    # отсутствие не подтверждаем: свежий отклик мог быть на непрочитанной
    # странице 1 (предположение «свежий отклик на странице 0» не гарантировано).
    page0 = _ssr_html([_topic(7, "999999")], extra="<a data-qa='pager-next'>далее</a>")
    page = _Page1FailsPage({NEGOTIATIONS_URL: page0})
    result = verify_response_in_negotiations(page, _V2)
    assert result.indeterminate
    assert "goto" in result.detail


def test_challenge_after_failed_pagination_navigation_is_terminal(monkeypatch):
    page0 = _ssr_html([_topic(7, "999999")], extra="<a data-qa='pager-next'>далее</a>")
    page = _Page1FailsPage({NEGOTIATIONS_URL: page0})
    detection = AntiBotDetection("url_path", "URL содержит /captcha")
    checks = 0

    def _check(_page):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise AntiBotChallengeDetected(detection)

    monkeypatch.setattr(verify_module, "raise_for_antibot", _check)

    with pytest.raises(AntiBotChallengeDetected):
        verify_response_in_negotiations(page, _V2)

    assert checks == 2


def test_challenge_after_successful_pagination_navigation_is_terminal(monkeypatch):
    page0 = _ssr_html([_topic(7, "999999")], extra="<a data-qa='pager-next'>далее</a>")
    page1 = _ssr_html([_topic(8, "888888")])
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: page0, f"{NEGOTIATIONS_URL}?page=1": page1})
    detection = AntiBotDetection("url_path", "URL содержит /captcha")
    checks = 0

    def _check(_page):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise AntiBotChallengeDetected(detection)

    monkeypatch.setattr(verify_module, "raise_for_antibot", _check)

    with pytest.raises(AntiBotChallengeDetected):
        verify_response_in_negotiations(page, _V2)

    assert checks == 2


def test_indeterminate_when_pagination_cap_reached_with_next_page():
    # Страницы 0 и 1 прочитаны чисто (вакансии нет), но страница 1 подтверждает
    # продолжение — целевая вакансия могла быть на странице 2+. Fail-closed:
    # indeterminate, а не ложный not_found (#207).
    page0 = _ssr_html([_topic(7, "999999")], extra="<a data-qa='pager-next'>далее</a>")
    page1 = _ssr_html([_topic(8, "888888")], extra="<a data-qa='pager-next'>далее</a>")
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: page0, f"{NEGOTIATIONS_URL}?page=1": page1})
    result = verify_response_in_negotiations(page, _V2)
    assert result.indeterminate
    assert "потолок" in result.detail


class _GrowingCardsPage(FakeNegotiationsPage):
    """DOM-список, который догружается на КАЖДОЙ попытке: после каждого
    wait_for_timeout появляется ещё одна карточка (отложенный/виртуализированный
    рендер не стабилизируется)."""

    def __init__(self, html: str):
        super().__init__({NEGOTIATIONS_URL: html})
        self._extra_cards = 0

    def wait_for_timeout(self, ms: int) -> None:
        self.wait_for_timeout_calls.append(ms)
        self._extra_cards += 1

    def locator(self, selector: str) -> _PageLocator:
        html = self._html
        if selector == ns.NEGOTIATION_ITEM:
            for i in range(self._extra_cards):
                html += (
                    f"<div data-qa='negotiations-item'>"
                    f"<a href='/vacancy/{700000 + i}'>"
                    f"<span data-qa='negotiations-item-vacancy'>Late{i}</span></a></div>"
                )
        return _PageLocator(_parse_root(html), _parse_selector(selector))


def test_indeterminate_when_dom_list_still_loading():
    # DOM-fallback: карточки догружаются на каждой попытке — список не
    # завершён, отсутствие целевой вакансии не подтверждаем (иначе ложный
    # not_found, #207).
    page = _GrowingCardsPage(_DOM_HTML)
    result = verify_response_in_negotiations(page, _V2)
    assert result.indeterminate


class _ReplacingCardsPage(FakeNegotiationsPage):
    """DOM-список, который на КАЖДОЙ паузе ПОДМЕНЯЕТ карточки при том же count
    (виртуализация/перерисовка): набор vacancy_id меняется, count — нет. Сравнение
    только count (старая эвристика) не поймало бы подмену и дало бы ложный
    not_found; сравнение набора — indeterminate (#207)."""

    def __init__(self, html: str):
        super().__init__({NEGOTIATIONS_URL: html})
        self._replacements = 0

    def wait_for_timeout(self, ms: int) -> None:
        self.wait_for_timeout_calls.append(ms)
        self._replacements += 1

    def locator(self, selector: str) -> _PageLocator:
        html = self._html
        if selector == ns.NEGOTIATION_ITEM and self._replacements:
            # Тот же count (2 карточки), но vacancy_id меняются на каждой паузе.
            base = 500000 + self._replacements * 100
            html = (
                f"<div data-qa='negotiations-item'>"
                f"<a href='/vacancy/{base + 1}'>"
                f"<span data-qa='negotiations-item-vacancy'>R{self._replacements}</span></a></div>"
                f"<div data-qa='negotiations-item'>"
                f"<a href='/vacancy/{base + 2}'>"
                f"<span data-qa='negotiations-item-vacancy'>R{self._replacements}b</span></a></div>"
            )
        return _PageLocator(_parse_root(html), _parse_selector(selector))


def test_indeterminate_when_dom_cards_replaced_same_count():
    # DOM-fallback: карточки ПОДМЕНЕНЫ при том же count (виртуализация) —
    # сравнение только count не поймало бы подмену и дало бы ложный not_found;
    # сравнение набора vacancy_id — indeterminate (#207).
    page = _ReplacingCardsPage(_DOM_HTML)
    result = verify_response_in_negotiations(page, _V2)
    assert result.indeterminate


class _AlternatingPaginationPage(FakeNegotiationsPage):
    """Попытка 1: пагинация подтверждена (pager-next), страница 1 не грузится.
    Попытка 2: пагинация не подтверждена (pager-block без pager-next) — страница
    0 чистая. Проверяет, что confirmed-incomplete из попытки 1 перевешивает
    чистое чтение попытки 2 (иначе OR-агрегация clean дала бы ложный not_found,
    #207)."""

    def __init__(self, page0_confirmed: str, page0_unconfirmed: str):
        super().__init__({NEGOTIATIONS_URL: page0_confirmed})
        self._confirmed = page0_confirmed
        self._unconfirmed = page0_unconfirmed
        self._page0_gotos = 0

    def goto(self, url: str, *, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)
        if "?page=1" in url:
            raise PlaywrightError("net::ERR_TIMED_OUT")
        if url == NEGOTIATIONS_URL:
            self._page0_gotos += 1
            self._html = self._confirmed if self._page0_gotos == 1 else self._unconfirmed
        else:
            self._html = self._pages.get(url, "")


def test_confirmed_incomplete_attempt_not_masked_by_clean_retry():
    # Попытка 1 подтвердила пагинацию, но страница 1 не загрузилась; попытка 2
    # прочитала страницу 0 чисто, но пагинация не подтвердилась. OR-агрегация
    # clean не должна замаскировать confirmed-incomplete из попытки 1: целевая
    # вакансия могла быть на непрочитанной странице 1 (иначе ложный not_found,
    # #207).
    page0_confirmed = _ssr_html([_topic(7, "999999")], extra="<a data-qa='pager-next'>далее</a>")
    page0_unconfirmed = _ssr_html([_topic(7, "999999")], extra="<div data-qa='pager-block'></div>")
    page = _AlternatingPaginationPage(page0_confirmed, page0_unconfirmed)
    result = verify_response_in_negotiations(page, _V2)
    assert result.indeterminate


class _IncomparableSecondAttemptPage(FakeNegotiationsPage):
    """Попытка 1: чистое чтение без целевой вакансии (отклик ещё не долетел).
    Попытка 2: целевая вакансия появилась, но атрибуция incomparable (хэш
    конфига vs числовой SSR id, маппинга нет). OR-агрегация clean из попытки 1
    не должна замаскировать incomparable из попытки 2 (иначе ложный not_found —
    ровно тот false-negative, что #212 призван устранить)."""

    def __init__(self, page0_clean: str, page0_incomparable: str):
        super().__init__({NEGOTIATIONS_URL: page0_clean})
        self._clean = page0_clean
        self._incomparable = page0_incomparable
        self._page0_gotos = 0

    def goto(self, url: str, *, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)
        if url == NEGOTIATIONS_URL:
            self._page0_gotos += 1
            self._html = self._clean if self._page0_gotos == 1 else self._incomparable
        else:
            self._html = self._pages.get(url, "")


def test_incomparable_second_attempt_not_masked_by_clean_first():
    # Попытка 1: чистое чтение, целевой вакансии ещё нет. Попытка 2: целевая
    # вакансия найдена, но атрибуция incomparable (хэш конфига vs числовой SSR
    # id, маппинга нет). Чистое чтение попытки 1 не должно замаскировать
    # incomparable из попытки 2: иначе ложный not_found вместо fail-closed
    # indeterminate (#212).
    page0_clean = _ssr_html([_topic(7, "999999")])
    page0_incomparable = _ssr_html([_topic(8, _V2, 96223331)])
    page = _IncomparableSecondAttemptPage(page0_clean, page0_incomparable)
    result = verify_response_in_negotiations(page, _V2, resume_id=_HASH)
    assert result.indeterminate
    assert "атрибуция" in result.detail


def test_dom_unattributeable_second_attempt_not_masked_by_clean_first():
    # Попытка 1: чистое чтение SSR без целевой вакансии. Попытка 2: SSR
    # недоступен, DOM-карточка с целевой вакансией, но атрибуция резюме
    # невозможна (DOM не несёт resumeId). Чистое чтение попытки 1 не должно
    # замаскировать неатрибутируемую находку попытки 2: иначе ложный not_found
    # (вакансия есть, но отклик не атрибутируется) — тот же класс false
    # negative, что #212 устраняет (Codex-ревью цикла 2).
    page0_clean = _ssr_html([_topic(7, "999999")])
    page = _IncomparableSecondAttemptPage(page0_clean, _DOM_HTML)
    result = verify_response_in_negotiations(page, _V1, resume_id="R2")
    assert result.indeterminate
    assert "атрибуци" in result.detail


def test_topic_without_resume_id_does_not_claim_other_resume():
    # Тема с совпадающей вакансией, но БЕЗ resumeId — matched (атрибутировать
    # нечем, ронять подтверждение нельзя, #210 напряжение 2). Деталь не должна
    # утверждать «другое резюме аккаунта» — у темы вообще нет resumeId, и
    # str(None)="None" ≠ resume_id лгал бы в логах аудита #212.
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _ssr_html([_topic(8, _V2)])})
    result = verify_response_in_negotiations(page, _V2, resume_id="R2")
    assert result.found
    assert "другое резюме" not in result.detail


# --- проводка: run_apply_for_resume передаёт реальный верификатор ------------


class _SimpleLocator:
    def __init__(self, present: bool):
        self._present = present

    @property
    def first(self) -> _SimpleLocator:
        return self

    def count(self) -> int:
        return 1 if self._present else 0

    def wait_for(self, *, timeout: float = 0, state: str = "attached") -> None:  # noqa: ARG002
        if not self._present:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError("not present")

    def click(
        self,
        *,
        timeout: float | None = None,
        force: bool | None = None,
        no_wait_after: bool | None = None,
    ) -> None:  # noqa: ARG002
        return None

    def get_attribute(self, _name: str) -> str | None:
        return None

    def locator(self, _selector: str) -> _SimpleLocator:
        return _SimpleLocator(False)

    def or_(self, other: _SimpleLocator) -> _SimpleLocator:
        # #226 cycle-review: wait_apply_button() комбинирует apply-button и
        # already-responded-маркеры одним локатором.
        return _SimpleLocator(self._present or other._present)

    def filter(self, *, visible: bool | None = None) -> _SimpleLocator:  # noqa: ARG002
        # #248 cycle-review round 2: dedup.check_already_responded() narrows the
        # union to visible matches before .first — the fake has no hidden-vs-
        # visible distinction, so filtering is a no-op here.
        return self


class _ApplyPipelineFakePage:
    """Не-dry-run прогон до questions-indeterminate: кнопка отклика есть,
    форма отклика не появляется (dump-ы требуют screenshot/content)."""

    def __init__(self):
        self.goto_calls: list[str] = []

    def goto(self, url: str, *, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)

    def locator(self, selector: str) -> _SimpleLocator:  # noqa: ARG002
        from hhru_bot.selector_groups import vacancy_page

        if selector == vacancy_page.VACANCY_APPLY_BUTTON:
            return _SimpleLocator(True)
        return _SimpleLocator(False)

    def wait_for_url(
        self, _url_pattern, *, wait_until: str | None = None, timeout: float | None = None
    ) -> None:  # noqa: ARG002
        return None

    def content(self) -> str:
        return "<html></html>"

    def screenshot(self, *, full_page: bool = True) -> bytes:  # noqa: ARG002
        return b""


def test_run_apply_for_resume_wires_verifier(tmp_path, monkeypatch):
    """#207/#212: продакшн-проводка — run_apply_for_resume передаёт реальный
    верификатор в pipeline; подтверждённый внешним источником отклик доходит
    до history как status='success' (не failed/без записи). Верификатор
    получает ЧИСЛОВОЙ id резюме из маппинга (#212: SSR несёт числовой
    resumeId, а не хэш конфига), запись в history остаётся под хэшем."""
    import argparse
    import sqlite3

    from hhru_bot.apply.verify import NegotiationsVerifyResult
    from hhru_bot.commands import _common
    from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
    from hhru_bot.copy_resume import ResumeIdMapping
    from hhru_bot.history import History
    from hhru_bot.search import VacancyCard
    from hhru_bot.throttle import Throttle

    monkeypatch.setattr(
        "hhru_bot.commands._common.search_vacancies",
        lambda page, search, max_pages=5: [  # noqa: ARG005
            VacancyCard(
                vacancy_id="42", title="Dev", company="Acme", url="https://hh.ru/vacancy/42"
            )
        ],
    )
    seen: list[tuple] = []

    def _fake_verify(page, vacancy_id, resume_id=None, account_resume_ids=None, run_id=None):  # noqa: ANN001
        seen.append((vacancy_id, resume_id, sorted(account_resume_ids or ())))
        return NegotiationsVerifyResult("found", "topic=1")

    monkeypatch.setattr("hhru_bot.commands._common.verify_response_in_negotiations", _fake_verify)
    # #212: маппинг «хэш → числовой id» — как от /applicant/resumes (два резюме
    # аккаунта; конфиг — первое). Без подмены резолвер ходил бы в сеть.
    # #216: статус конфиг-резюме должен быть подтверждён (не not_finished,
    # не отсутствовать) — иначе run_apply_for_resume фейлится до поиска.
    monkeypatch.setattr(
        "hhru_bot.commands._common.resolve_numeric_resume_ids",
        lambda page: ResumeIdMapping(
            {"AAA111": "284561395", "BBB222": "96223331"},
            statuses={"AAA111": "modified", "BBB222": "modified"},
        ),
    )

    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python developer"),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="Здравствуйте!",
        resumes=[resume],
    )
    history_db = tmp_path / "history.db"
    history = History(history_db)
    throttle = Throttle(config.throttle, history)
    args = argparse.Namespace(dry_run=False, limit=1, max_pages=5, headless=True)

    _common.run_apply_for_resume(_ApplyPipelineFakePage(), config, resume, history, throttle, args)

    # Верификатор — в числовом домене с перечнем резюме аккаунта (#212)…
    assert seen == [("42", "284561395", ["284561395", "96223331"])]
    conn = sqlite3.connect(history_db)
    try:
        rows = conn.execute("SELECT resume_id, status, reason FROM actions").fetchall()
    finally:
        conn.close()
    # …а history — по-прежнему под хэшем конфига: домены не смешиваются.
    assert rows == [("AAA111", "success", rows[0][2])]
    assert "negotiations" in rows[0][2]


def test_run_apply_for_resume_fails_before_search_for_unfinished_resume(tmp_path, monkeypatch):
    """#216: an unfinished configured resume must never reach the apply plan."""
    import argparse

    from hhru_bot.commands import _common
    from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
    from hhru_bot.copy_resume import ResumeIdMapping
    from hhru_bot.history import History
    from hhru_bot.throttle import Throttle

    def _unexpected_search(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("unfinished resume must fail before vacancy search")

    monkeypatch.setattr("hhru_bot.commands._common.search_vacancies", _unexpected_search)
    monkeypatch.setattr(
        "hhru_bot.commands._common.resolve_numeric_resume_ids",
        lambda page: ResumeIdMapping({"AAA111": "284561395"}, statuses={"AAA111": "not_finished"}),
    )
    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python developer"),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="Здравствуйте!",
        resumes=[resume],
    )
    history = History(tmp_path / "history.db")
    throttle = Throttle(config.throttle, history)
    args = argparse.Namespace(dry_run=False, limit=1, max_pages=5, headless=True)

    assert (
        _common.run_apply_for_resume(
            _ApplyPipelineFakePage(), config, resume, history, throttle, args
        )
        is True
    )


def test_run_apply_for_resume_fails_before_search_when_status_unknown(tmp_path, monkeypatch):
    """#216: конфиг-хэш присутствует в маппинге (SSR прочитан успешно для
    этого резюме), но поле status для него отсутствует (schema drift) — мы
    объективно не можем подтвердить готовность резюме к откликам, поэтому
    фейлимся так же, как при not_finished, а не молча продолжаем."""
    import argparse

    from hhru_bot.commands import _common
    from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
    from hhru_bot.copy_resume import ResumeIdMapping
    from hhru_bot.history import History
    from hhru_bot.throttle import Throttle

    def _unexpected_search(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("resume with unknown status must fail before vacancy search")

    monkeypatch.setattr("hhru_bot.commands._common.search_vacancies", _unexpected_search)
    monkeypatch.setattr(
        "hhru_bot.commands._common.resolve_numeric_resume_ids",
        lambda page: ResumeIdMapping({"AAA111": "284561395"}, statuses={}),
    )
    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python developer"),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="Здравствуйте!",
        resumes=[resume],
    )
    history = History(tmp_path / "history.db")
    throttle = Throttle(config.throttle, history)
    args = argparse.Namespace(dry_run=False, limit=1, max_pages=5, headless=True)

    assert (
        _common.run_apply_for_resume(
            _ApplyPipelineFakePage(), config, resume, history, throttle, args
        )
        is True
    )


def test_run_apply_for_resume_verifier_falls_back_to_hash(tmp_path, monkeypatch):
    """#212: сбой маппинга (None) не роняет apply — верификатор получает хэш
    конфига без перечня резюме; атрибуция деградирует до fail-closed
    indeterminate в самом верификаторе, а не молчаливого not_found."""
    import argparse

    from hhru_bot.apply.verify import NegotiationsVerifyResult
    from hhru_bot.commands import _common
    from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
    from hhru_bot.history import History
    from hhru_bot.search import VacancyCard
    from hhru_bot.throttle import Throttle

    monkeypatch.setattr(
        "hhru_bot.commands._common.search_vacancies",
        lambda page, search, max_pages=5: [  # noqa: ARG005
            VacancyCard(
                vacancy_id="42", title="Dev", company="Acme", url="https://hh.ru/vacancy/42"
            )
        ],
    )
    seen: list[tuple] = []

    def _fake_verify(page, vacancy_id, resume_id=None, account_resume_ids=None, run_id=None):  # noqa: ANN001
        seen.append((vacancy_id, resume_id, account_resume_ids))
        return NegotiationsVerifyResult("found", "topic=1")

    monkeypatch.setattr("hhru_bot.commands._common.verify_response_in_negotiations", _fake_verify)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_numeric_resume_ids", lambda page: None)

    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python developer"),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="Здравствуйте!",
        resumes=[resume],
    )
    history = History(tmp_path / "history.db")
    throttle = Throttle(config.throttle, history)
    args = argparse.Namespace(dry_run=False, limit=1, max_pages=5, headless=True)

    _common.run_apply_for_resume(_ApplyPipelineFakePage(), config, resume, history, throttle, args)

    assert seen == [("42", "AAA111", None)]


def test_run_apply_for_resume_fail_closed_when_config_resume_not_in_mapping(tmp_path, monkeypatch):
    """#212: маппинг получен, но конфиг-резюме в нём ОТСУТСТВУЕТ (устаревший/
    неверный хэш). Перечень резюме аккаунта НЕ должен заполняться — иначе любая
    тема с id резюме аккаунта подтвердила бы отклик (success под хэшем, которого
    нет в аккаунте), вопреки предупреждению о fail-closed. Без перечня
    атрибуция уходит в incomparable → indeterminate в самом верификаторе."""
    import argparse

    from hhru_bot.apply.verify import NegotiationsVerifyResult
    from hhru_bot.commands import _common
    from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
    from hhru_bot.history import History
    from hhru_bot.search import VacancyCard
    from hhru_bot.throttle import Throttle

    monkeypatch.setattr(
        "hhru_bot.commands._common.search_vacancies",
        lambda page, search, max_pages=5: [  # noqa: ARG005
            VacancyCard(
                vacancy_id="42", title="Dev", company="Acme", url="https://hh.ru/vacancy/42"
            )
        ],
    )
    seen: list[tuple] = []

    def _fake_verify(page, vacancy_id, resume_id=None, account_resume_ids=None, run_id=None):  # noqa: ANN001
        seen.append((vacancy_id, resume_id, account_resume_ids))
        return NegotiationsVerifyResult("found", "topic=1")

    monkeypatch.setattr("hhru_bot.commands._common.verify_response_in_negotiations", _fake_verify)
    # Маппинг есть, но конфиг-резюме AAA111 в нём отсутствует.
    monkeypatch.setattr(
        "hhru_bot.commands._common.resolve_numeric_resume_ids",
        lambda page: {"BBB222": "96223331"},
    )

    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python developer"),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="Здравствуйте!",
        resumes=[resume],
    )
    history = History(tmp_path / "history.db")
    throttle = Throttle(config.throttle, history)
    args = argparse.Namespace(dry_run=False, limit=1, max_pages=5, headless=True)

    _common.run_apply_for_resume(_ApplyPipelineFakePage(), config, resume, history, throttle, args)

    # account_resume_ids=None → атрибуция fail-closed (incomparable), не matched.
    assert seen == [("42", "AAA111", None)]

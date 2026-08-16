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

from playwright.sync_api import Error as PlaywrightError

import hhru_bot.apply.verify as verify_module
from _fakes import FakeLocator, _CardLocator, _parse_root, _parse_selector
from hhru_bot.apply.verify import verify_response_in_negotiations
from hhru_bot.responses import NEGOTIATIONS_URL
from hhru_bot.selector_groups import negotiations as ns

_V1, _V2 = "111111", "222222"


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

    def goto(self, url: str, wait_until: str = "") -> None:  # noqa: ARG002
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

    def screenshot(self, full_page: bool = True) -> bytes:  # noqa: ARG002
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


def test_foreign_resume_topic_does_not_confirm_apply():
    # Отклик с ДРУГОГО резюме (R1) на ту же вакансию — НЕ подтверждение apply
    # с текущего (R2): иначе ложный success для операции, которая не ушла.
    page = FakeNegotiationsPage({NEGOTIATIONS_URL: _ssr_html([_topic(8, _V2, "R1")])})
    result = verify_response_in_negotiations(page, _V2, resume_id="R2")
    assert result.status == "not_found"


def test_incomparable_numeric_ssr_and_hashed_config_resume_ids_are_indeterminate():
    """Live format pair from #212 must not become a false not_found."""
    page = FakeNegotiationsPage(
        {NEGOTIATIONS_URL: _ssr_html([_topic(5503922709, "135170581", "96223331")])}
    )
    result = verify_response_in_negotiations(
        page,
        "135170581",
        resume_id="b3236ebbff10f60ff30039ed1f6d5876645331",
    )
    assert result.indeterminate
    assert "несравнимые форматы" in result.detail


def test_not_found_dumps_read_topic_vacancy_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_module, "LOG_DIR", tmp_path)
    page = FakeNegotiationsPage(
        {NEGOTIATIONS_URL: _ssr_html([_topic(7, "999999"), _topic(8, "888888")])}
    )
    result = verify_response_in_negotiations(page, _V2)
    assert result.status == "not_found"
    artifact = json.loads(
        (tmp_path / f"apply_{_V2}_verify_not_found.json").read_text(encoding="utf-8")
    )
    assert artifact["topic_vacancy_ids"] == ["999999", "888888"]


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

    def goto(self, url: str, wait_until: str = "") -> None:  # noqa: ARG002
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

    def goto(self, url: str, wait_until: str = "") -> None:  # noqa: ARG002
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


# --- проводка: run_apply_for_resume передаёт реальный верификатор ------------


class _SimpleLocator:
    def __init__(self, present: bool):
        self._present = present

    @property
    def first(self) -> _SimpleLocator:
        return self

    def count(self) -> int:
        return 1 if self._present else 0

    def wait_for(self, timeout: float = 0, state: str = "attached") -> None:  # noqa: ARG002
        if not self._present:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError("not present")

    def click(self, **_kwargs) -> None:
        return None

    def get_attribute(self, _name: str) -> str | None:
        return None

    def locator(self, _selector: str) -> _SimpleLocator:
        return _SimpleLocator(False)


class _ApplyPipelineFakePage:
    """Не-dry-run прогон до questions-indeterminate: кнопка отклика есть,
    форма отклика не появляется (dump-ы требуют screenshot/content)."""

    def __init__(self):
        self.goto_calls: list[str] = []

    def goto(self, url: str, wait_until: str = "") -> None:  # noqa: ARG002
        self.goto_calls.append(url)

    def locator(self, selector: str) -> _SimpleLocator:  # noqa: ARG002
        from hhru_bot.selector_groups import vacancy_page

        if selector == vacancy_page.VACANCY_APPLY_BUTTON:
            return _SimpleLocator(True)
        return _SimpleLocator(False)

    def wait_for_url(self, _url_pattern, **_kwargs) -> None:
        return None

    def content(self) -> str:
        return "<html></html>"

    def screenshot(self, full_page: bool = True) -> bytes:  # noqa: ARG002
        return b""


def test_run_apply_for_resume_wires_verifier(tmp_path, monkeypatch):
    """#207: продакшн-проводка — run_apply_for_resume передаёт реальный
    верификатор в pipeline; подтверждённый внешним источником отклик
    доходит до history как status='success' (не failed/без записи)."""
    import argparse
    import sqlite3

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

    def _fake_verify(page, vacancy_id, resume_id=None):  # noqa: ANN001
        seen.append((vacancy_id, resume_id))
        return NegotiationsVerifyResult("found", "topic=1")

    monkeypatch.setattr("hhru_bot.commands._common.verify_response_in_negotiations", _fake_verify)

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

    assert seen == [("42", "AAA111")]
    conn = sqlite3.connect(history_db)
    try:
        rows = conn.execute("SELECT status, reason FROM actions").fetchall()
    finally:
        conn.close()
    assert rows == [("success", rows[0][1])]
    assert "negotiations" in rows[0][1]

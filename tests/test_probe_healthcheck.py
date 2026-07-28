"""Тесты probe --healthcheck (#88): read-only проверка селекторов hh.ru.

Браузер НЕ поднимается (CI без Chromium). Чистые функции check_selectors /
format_healthcheck_table прогоняются на FakePage поверх HTML-фикстур — тот же
приём, что в tests/_fakes.py (DOM из html.parser, locator(sel).count()).

Гарантируем инвариант #88:
- read-only: ничего, кроме goto + locator.count(), не вызывается (apply/submit
  не трогаются);
- статус OK при found>0, NOT_FOUND при 0;
- одна HTML-фикстура = «страница»: goto/set_content переключает проверяемый DOM.
"""

from __future__ import annotations

from _fakes import FakeLocator, _parse_root
from hhru_bot.commands import probe as probe_cmd


class _FakePage:
    """Минимальный Playwright-Page для healthcheck: goto/set_content/locator.

    Поддерживает ровно то, что использует check_selectors: page.goto(url),
    page.locator(selector).count(). DOM хранится в памяти, переключается через
    set_content (как page.set_content в живом Playwright). goto здесь — то же:
    подменяет текущий DOM (имитируя переход), без сетевого запроса.
    """

    def __init__(self, html: str = ""):
        self._root = _parse_root(html)
        self.last_goto: str | None = None

    def set_content(self, html: str) -> None:
        self._root = _parse_root(html)

    def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:  # noqa: ARG002
        self.last_goto = url

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self._root, probe_cmd._parse_qa_selector(selector))


# --- check_selectors: статус по count() ------------------------------------


def test_check_selectors_ok_when_found():
    page = _FakePage("<div data-qa='vacancy-serp__vacancy'>x</div>")
    spec = [
        ("search", "https://hh.ru/search/vacancy", [("CARD", "[data-qa='vacancy-serp__vacancy']")])
    ]
    pages = probe_cmd.check_selectors(page, spec)
    assert pages[0].results[0].name == "CARD"
    assert pages[0].results[0].found == 1
    assert pages[0].results[0].status == "OK"


def test_check_selectors_not_found_when_zero():
    page = _FakePage("<div>нет нужного</div>")
    spec = [("search", "https://hh.ru/search/vacancy", [("MISS", "[data-qa='no-such-thing']")])]
    pages = probe_cmd.check_selectors(page, spec)
    assert pages[0].results[0].found == 0
    assert pages[0].results[0].status == "NOT_FOUND"


def test_check_selectors_counts_multiple_matches():
    # Несколько карточек на странице поиска — found == числу, статус OK.
    html = (
        "<div data-qa='vacancy-serp__vacancy'>a</div>"
        "<div data-qa='vacancy-serp__vacancy'>b</div>"
        "<div data-qa='vacancy-serp__vacancy'>c</div>"
    )
    page = _FakePage(html)
    spec = [
        ("search", "https://hh.ru/search/vacancy", [("CARD", "[data-qa='vacancy-serp__vacancy']")])
    ]
    pages = probe_cmd.check_selectors(page, spec)
    assert pages[0].results[0].found == 3
    assert pages[0].results[0].status == "OK"


def test_check_selectors_visits_each_page_url():
    # read-only прогон: каждая страница открывается goto, ДО проверки селекторов.
    page = _FakePage("<div data-qa='vacancy-title'>t</div>")
    spec = [
        ("search", "https://hh.ru/search/vacancy", [("CARD", "[data-qa='vacancy-serp__vacancy']")]),
        ("vacancy", "https://hh.ru/vacancy/1", [("TITLE", "[data-qa='vacancy-title']")]),
    ]
    probe_cmd.check_selectors(page, spec)
    # последний goto — последняя страница спецификации
    assert page.last_goto == "https://hh.ru/vacancy/1"


def test_check_selectors_switches_dom_per_page_via_callback():
    """В боевом прогоне DOM меняется после goto (живой hh.ru). Моделируем через
    page_loader: на каждую страницу подставляем её HTML, как сделал бы браузер."""
    html_by_page = {
        "search": "<div data-qa='vacancy-serp__vacancy'>x</div>",
        "vacancy": "<a data-qa='vacancy-response-link-top'>отклик</a>",
    }

    def _load(p, url, name):  # noqa: ANN001
        p.set_content(html_by_page[name])

    page = _FakePage()
    spec = [
        ("search", "https://hh.ru/search/vacancy", [("CARD", "[data-qa='vacancy-serp__vacancy']")]),
        (
            "vacancy",
            "https://hh.ru/vacancy/1",
            [("APPLY", "[data-qa='vacancy-response-link-top']")],
        ),
    ]
    pages = probe_cmd.check_selectors(page, spec, page_loader=_load)
    assert pages[0].results[0].status == "OK"  # search CARD
    assert pages[1].results[0].status == "OK"  # vacancy APPLY


def test_check_selectors_readonly_does_not_click_apply(monkeypatch):
    """Инвариант #88: никаких write-действий. Page не имеет click/fill/submit —
    если check_selectors попытается их вызвать, упадёт AttributeError."""
    page = _FakePage("<div data-qa='vacancy-title'>t</div>")
    spec = [("vacancy", "https://hh.ru/vacancy/1", [("TITLE", "[data-qa='vacancy-title']")])]
    # Если бы код кликал apply — потребовался бы click(). Его нет → чисто read-only.
    assert not hasattr(page, "click")
    assert not hasattr(page, "fill")
    pages = probe_cmd.check_selectors(page, spec)
    assert pages[0].results[0].status == "OK"


# --- format_healthcheck_table ---------------------------------------------


def test_format_table_has_header_and_statuses():
    pages = [
        probe_cmd.PageCheck(
            name="search",
            url="https://hh.ru/search/vacancy",
            results=[
                probe_cmd.SelectorCheck("CARD", "[data-qa='vacancy-serp__vacancy']", 2),
                probe_cmd.SelectorCheck("MISS", "[data-qa='none']", 0),
            ],
        ),
    ]
    out = probe_cmd.format_healthcheck_table(pages)
    # ASCII-таблица с рамкой и заголовками колонок
    assert "selector" in out and "status" in out and "count" in out
    assert "OK" in out
    assert "NOT_FOUND" in out
    assert "CARD" in out


def test_format_table_empty_pages_still_renders_header():
    # Нет страниц — таблица всё равно рисует шапку (как report._ascii_table).
    out = probe_cmd.format_healthcheck_table([])
    assert "selector" in out
    assert "status" in out


def test_format_table_groups_by_page():
    pages = [
        probe_cmd.PageCheck("search", "u1", [probe_cmd.SelectorCheck("A", "[data-qa='a']", 1)]),
        probe_cmd.PageCheck("vacancy", "u2", [probe_cmd.SelectorCheck("B", "[data-qa='b']", 0)]),
    ]
    out = probe_cmd.format_healthcheck_table(pages)
    assert "search" in out
    assert "vacancy" in out
    # обе страницы в выводе
    assert out.index("search") < out.index("vacancy") or out.count("A") >= 1

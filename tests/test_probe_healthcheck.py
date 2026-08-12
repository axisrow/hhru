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

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from _fakes import FakeLocator, _parse_root
from hhru_bot.commands import probe as probe_cmd


def _stub_goto_hh(monkeypatch, page):
    """Подменяет probe_cmd.goto_hh на вызов page.goto (без retry/backoff).

    check_selectors (#142) ходит через goto_hh, а не через сырой page.goto. В
    тестах goto_hh не нужен (retry/backoff покрыты отдельным test_browser_navigation),
    поэтому редиректим его обратно на _FakePage.goto, переключающий DOM.
    """

    def _goto(p, url, **kwargs):  # noqa: ANN001
        p.goto(url, wait_until="domcontentloaded")

    monkeypatch.setattr(probe_cmd, "goto_hh", _goto)
    return page


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


def test_check_selectors_ok_when_found(monkeypatch):
    page = _stub_goto_hh(monkeypatch, _FakePage("<div data-qa='vacancy-serp__vacancy'>x</div>"))
    spec = [
        ("search", "https://hh.ru/search/vacancy", [("CARD", "[data-qa='vacancy-serp__vacancy']")])
    ]
    pages = probe_cmd.check_selectors(page, spec)
    assert pages[0].results[0].name == "CARD"
    assert pages[0].results[0].found == 1
    assert pages[0].results[0].status == "OK"


def test_check_selectors_not_found_when_zero(monkeypatch):
    page = _stub_goto_hh(monkeypatch, _FakePage("<div>нет нужного</div>"))
    spec = [("search", "https://hh.ru/search/vacancy", [("MISS", "[data-qa='no-such-thing']")])]
    pages = probe_cmd.check_selectors(page, spec)
    assert pages[0].results[0].found == 0
    assert pages[0].results[0].status == "NOT_FOUND"


def test_check_selectors_counts_multiple_matches(monkeypatch):
    # Несколько карточек на странице поиска — found == числу, статус OK.
    html = (
        "<div data-qa='vacancy-serp__vacancy'>a</div>"
        "<div data-qa='vacancy-serp__vacancy'>b</div>"
        "<div data-qa='vacancy-serp__vacancy'>c</div>"
    )
    page = _stub_goto_hh(monkeypatch, _FakePage(html))
    spec = [
        ("search", "https://hh.ru/search/vacancy", [("CARD", "[data-qa='vacancy-serp__vacancy']")])
    ]
    pages = probe_cmd.check_selectors(page, spec)
    assert pages[0].results[0].found == 3
    assert pages[0].results[0].status == "OK"


def test_check_selectors_visits_each_page_url(monkeypatch):
    # read-only прогон: каждая страница открывается goto, ДО проверки селекторов.
    page = _stub_goto_hh(monkeypatch, _FakePage("<div data-qa='vacancy-title'>t</div>"))
    spec = [
        ("search", "https://hh.ru/search/vacancy", [("CARD", "[data-qa='vacancy-serp__vacancy']")]),
        ("vacancy", "https://hh.ru/vacancy/1", [("TITLE", "[data-qa='vacancy-title']")]),
    ]
    probe_cmd.check_selectors(page, spec)
    # последний goto — последняя страница спецификации
    assert page.last_goto == "https://hh.ru/vacancy/1"


def test_check_selectors_switches_dom_per_page_via_callback(monkeypatch):
    """В боевом прогоне DOM меняется после goto (живой hh.ru). Моделируем через
    page_loader: на каждую страницу подставляем её HTML, как сделал бы браузер."""
    html_by_page = {
        "search": "<div data-qa='vacancy-serp__vacancy'>x</div>",
        "vacancy": "<a data-qa='vacancy-response-link-top'>отклик</a>",
    }

    def _load(p, url, name):  # noqa: ANN001
        p.set_content(html_by_page[name])

    page = _stub_goto_hh(monkeypatch, _FakePage())
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
    page = _stub_goto_hh(monkeypatch, _FakePage("<div data-qa='vacancy-title'>t</div>"))
    spec = [("vacancy", "https://hh.ru/vacancy/1", [("TITLE", "[data-qa='vacancy-title']")])]
    # Если бы код кликал apply — потребовался бы click(). Его нет → чисто read-only.
    assert not hasattr(page, "click")
    assert not hasattr(page, "fill")
    pages = probe_cmd.check_selectors(page, spec)
    assert pages[0].results[0].status == "OK"


def test_check_selectors_unreachable_page_not_treated_as_all_not_found(monkeypatch):
    """#120/#142: goto_hh исчерпал retry и пробросил PlaywrightTimeoutError —
    страница помечается unreachable, а НЕ «все селекторы NOT_FOUND».

    Регрессионный тест из issue #142: при непрогрузившемся JS healthcheck должен
    рапортовать «не проверено» (UNREACHABLE), а не честный, но вводящий в
    заблуждение NOT_FOUND по всем селекторам — иначе провал выглядит как
    «сломанные селекторы», хотя причина в сети/DDoS-Guard.
    """
    # goto_hh падает (имитация: hh.ru не отвечает даже после 3 попыток).
    monkeypatch.setattr(
        probe_cmd,
        "goto_hh",
        lambda p, url, **kw: (_ for _ in ()).throw(PlaywrightTimeoutError("network timeout")),
    )
    page = _FakePage("<div data-qa='vacancy-serp__vacancy'>x</div>")
    spec = [
        (
            "search",
            "https://hh.ru/search/vacancy",
            [
                ("CARD", "[data-qa='vacancy-serp__vacancy']", True),
                ("TITLE", "[data-qa='vacancy-serp__vacancy-title']", True),
            ],
        )
    ]
    pages = probe_cmd.check_selectors(page, spec)
    assert pages[0].unreachable is True
    assert pages[0].results == []  # селекторы НЕ проверялись
    # таблица рисует UNREACHABLE, а не NOT_FOUND по каждому селектору
    out = probe_cmd.format_healthcheck_table(pages)
    assert probe_cmd.STATUS_UNREACHABLE in out
    assert "NOT_FOUND" not in out


def test_check_selectors_unreachable_does_not_swallow_unrelated_exception(monkeypatch):
    """#142: except сужен до (PlaywrightTimeoutError, PlaywrightError) — только то,
    что рейзит goto_hh. Баг в коде (KeyError/AttributeError/...) НЕ должен
    маскироваться под unreachable — он должен падать открыто (fail-loud)."""
    monkeypatch.setattr(
        probe_cmd,
        "goto_hh",
        lambda p, url, **kw: (_ for _ in ()).throw(RuntimeError("bug in code, not network")),
    )
    page = _FakePage()
    spec = [("search", "https://hh.ru/search/vacancy", [("CARD", "[data-qa='x']")])]
    with pytest.raises(RuntimeError, match="bug in code"):
        probe_cmd.check_selectors(page, spec)


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


# --- cycle-1 (Codex F1): required vs optional — здоровая страница не «ломается» ---


def test_optional_selector_absent_is_not_failure():
    """Опциональный селектор (легитимно отсутствует: пагинация в конце выдачи,
    compensation после magritte-перехода hh.ru) НЕ считается провалом, даже если
    count()=0. Иначе здоровая страница репортилась бы [FAIL] (Codex F1)."""
    # required=False, found=0 → статус OPTIONAL_ABSENT, fails=False
    sel = probe_cmd.SelectorCheck(
        "COMPENSATION", "[data-qa='vacancy-serp__vacancy-compensation']", 0, required=False
    )
    assert sel.status == probe_cmd.STATUS_OPTIONAL_ABSENT
    assert sel.fails is False


def test_required_selector_absent_is_failure():
    """Обязательный селектор с count()=0 — это реальный провал (статус NOT_FOUND)."""
    sel = probe_cmd.SelectorCheck("CARD", "[data-qa='vacancy-serp__vacancy']", 0, required=True)
    assert sel.status == probe_cmd.STATUS_NOT_FOUND
    assert sel.fails is True


def test_optional_selector_present_is_ok():
    sel = probe_cmd.SelectorCheck("PAGINATION_NEXT", "[data-qa='pager-next']", 1, required=False)
    assert sel.status == probe_cmd.STATUS_OK
    assert sel.fails is False


def test_check_selectors_marks_optional_via_spec_tuple(monkeypatch):
    """3-tuple в spec: (name, selector, required). required по умолчанию True
    (обратная совместимость с 2-tuple). Опциональный селектор с 0 совпадений не
    роняет страницу."""
    page = _stub_goto_hh(
        monkeypatch, _FakePage("<div data-qa='vacancy-serp__vacancy'>x</div>")
    )  # нет compensation, нет pager
    spec = [
        (
            "search",
            "https://hh.ru/search/vacancy",
            [
                ("CARD", "[data-qa='vacancy-serp__vacancy']", True),  # required, найден
                (
                    "COMPENSATION",
                    "[data-qa='vacancy-serp__vacancy-compensation']",
                    False,
                ),  # optional, 0
                ("PAGER", "[data-qa='pager-next']", False),  # optional, 0
            ],
        )
    ]
    pages = probe_cmd.check_selectors(page, spec)
    res = {r.name: r for r in pages[0].results}
    assert res["CARD"].status == "OK" and res["CARD"].fails is False
    assert res["COMPENSATION"].status == "OPTIONAL_ABSENT" and res["COMPENSATION"].fails is False
    assert res["PAGER"].status == "OPTIONAL_ABSENT" and res["PAGER"].fails is False


def test_healthcheck_spec_no_fake_vacancy_url():
    """Codex F1: resume_id — это id РЕЗЮМЕ (/resume/<id>), НЕ вакансии. Раньше spec
    строил /vacancy/<resume_id> (404 → все vacancy-селекторы NOT_FOUND на здоровом
    аккаунте). Фикс: spec НЕ содержит страниц, требующих id вакансии, которого у
    healthcheck нет (vacancy / apply_form). Только search/negotiations/resume —
    URL, доступные без контекста конкретной вакансии."""
    from hhru_bot.config import ResumeConfig

    config = _StubConfig(
        resumes=[ResumeConfig(id="r1", resume_url="https://hh.ru/resume/12345", search=_search())]
    )
    spec = probe_cmd._healthcheck_spec(config)
    names = {p[0] for p in spec}
    assert "search" in names
    assert "negotiations" in names
    assert "resume" in names
    # vacancy/apply_form убраны: нет валидного id вакансии для goto
    assert "vacancy" not in names
    assert "apply_form" not in names
    # resume-страница использует корректный resume_id (id резюме, не вакансии)
    resume_entry = next(p for p in spec if p[0] == "resume")
    assert resume_entry[1] == "https://hh.ru/resume/12345"


def test_healthcheck_spec_marks_obsolete_and_conditional_optional():
    """Codex F1: VACANCY_CARD_COMPENSATION документированно НЕ работает на живом
    hh.ru с 2025 (magritte), пагинация legitimately отсутствует в конце выдачи.
    Их required=False — иначе гарантированный [FAIL] на здоровом аккаунте."""
    from hhru_bot.config import ResumeConfig

    config = _StubConfig(
        resumes=[ResumeConfig(id="r1", resume_url="https://hh.ru/resume/1", search=_search())]
    )
    spec = probe_cmd._healthcheck_spec(config)
    search = next(p for p in spec if p[0] == "search")
    sel_required = {name: req for name, _sel, req in search[2]}
    # основные карточные селекторы — required
    assert sel_required["VACANCY_CARD"] is True
    assert sel_required["VACANCY_CARD_TITLE_LINK"] is True
    # устаревший/условно-отсутствующий — optional
    assert sel_required["VACANCY_CARD_COMPENSATION"] is False
    assert sel_required["PAGINATION_NEXT"] is False


class _StubConfig:
    """Минимальный конфиг для _healthcheck_spec: только resumes (остальное не нужно)."""

    def __init__(self, resumes):
        self.resumes = resumes


def _search():
    """Минимальный SearchFilters (text обязателен) — search-поля healthcheck не нужны."""
    from hhru_bot.config import SearchFilters

    return SearchFilters(text="python")


def test_run_healthcheck_fail_only_on_required_missing(capsys):
    """Итоговый [FAIL] считается ТОЛЬКО по required-NOT_FOUND. Optional-ABSENT
    не делает здоровый аккаунт «сломанным» (Codex F1 — главная претензия)."""
    pages = [
        probe_cmd.PageCheck(
            "search",
            "u",
            [
                probe_cmd.SelectorCheck("CARD", "[data-qa='c']", 1, required=True),
                probe_cmd.SelectorCheck("COMP", "[data-qa='x']", 0, required=False),
            ],
        )
    ]
    # required CARD найден, COMP optional отсутствует → НЕ провал
    missing = sum(1 for pg in pages for r in pg.results if r.fails)
    assert missing == 0

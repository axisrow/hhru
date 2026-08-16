"""TDD для #95: детекция вопросов в форме отклика (detect-only, NO submit).

Локальный HTML-fake: tests/_fakes.py не парсит input[type=...], bare textarea и
substring data-qa — а detect_questions именно их использует. Чтобы не трогать
общий _fakes.py (конфликт с #96), здесь свой минимальный парсер под нужные селекторы.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hhru_bot.apply.questions import QuestionDetection, detect_questions

pytestmark = pytest.mark.integration

_VOID = {"input", "br", "hr", "img", "meta", "link", "area", "col", "embed", "source"}


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#doc", {})
        self.stack = [self.root]

    def feed(self, data):
        super().feed(data)
        return self.root

    def handle_starttag(self, tag, attrs):
        n = _Node(tag, attrs)
        self.stack[-1].children.append(n)
        if tag not in _VOID:
            self.stack.append(n)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break


def _all(root):
    for c in root.children:
        yield c
        yield from _all(c)


# selector forms: 'textarea' | 'input[type="radio"]' | 'input[type=\'checkbox\']'
#   | '[data-qa=\'task-body\']' | 'textarea[data-qa=\'...\']'
_TAG_RE = re.compile(r"^([a-zA-Z]+)$")
_TYPE_RE = re.compile(r"^input\[type=[\"'](\w+)[\"']\]$")
_QA_RE = re.compile(r"^\[data-qa=[\"']([\w\-]+)[\"']\]$")
_TAG_QA_RE = re.compile(r"^([a-zA-Z]+)\[data-qa=[\"']([\w\-]+)[\"']\]$")
_FORM_ID_RE = re.compile(r"^form#([\w\-]+)$")


def _match(node, sel):
    if m := _TAG_RE.match(sel):
        return node.tag == m.group(1)
    if m := _TYPE_RE.match(sel):
        return node.tag == "input" and node.attrs.get("type") == m.group(1)
    if m := _QA_RE.match(sel):
        return node.attrs.get("data-qa") == m.group(1)
    if m := _TAG_QA_RE.match(sel):
        return node.tag == m.group(1) and node.attrs.get("data-qa") == m.group(2)
    return False


def _find_parent(root, target):
    for c in root.children:
        if c is target:
            return root
        found = _find_parent(c, target)
        if found is not None:
            return found
    return None


def _ancestor_form(root, node):
    """Эмулирует Playwright xpath=ancestor::form[1]: ближайший предок-<form>."""
    cur = node
    while True:
        parent = _find_parent(root, cur)
        if parent is None:
            return None
        if parent.tag == "form":
            return parent
        cur = parent


class _Loc:
    """Локатор фейка. ``wait_for`` — bounded-ожидание (#139): при
    ``render_delayed`` элемент «появляется» только у wait_for, а немедленный
    ``count()`` (без предварительного wait_for) видит пустой DOM — моделирует
    гонку рендера. ``wait_error`` — генерирует не-timeout PlaywrightError
    (аномалия, НЕ легитимное отсутствие) для indeterminate-веток.

    cycle-review #139 (finding #1): режим render_delayed/wait_error раньше
    наследовался от РОДИТЕЛЬСКОГО локатора при вложенном ``scope.locator(sel)``,
    а не смотрел на сам ``sel`` — из-за чего пометка селектора вида
    ``input[type='radio']`` (вложенный внутрь form-scope) никогда не срабатывала,
    и соответствующие regression-тесты проходили даже на не пофикшенном коде
    (тавтология). Теперь ``_Loc`` хранит ссылку на владеющую ``_Page`` и любой
    ``locator(sel)`` — что page-level, что вложенный — определяет режим ИМЕННО
    по ``sel`` через ``_Page``, единообразно.

    ``_waited`` хранится в общем мьютабельном ``_state`` (не поле экземпляра):
    ``.first`` возвращает НОВЫЙ объект ``_Loc`` (как в реальном Playwright —
    каждый вызов ``.first``/``.locator`` даёт новый handle), но production-код
    вызывает ``wait_for`` на ``.first``, а решающий ``count()`` — на исходном
    локаторе (см. questions.py ``_wait_present`` + повторное использование
    ``textarea_loc``). Если бы «дождались» отслеживалось per-instance, а не
    per-selector-состояние, тест не отличил бы «дождались через .first» от
    «не дождались вовсе» — тот же класс ошибки, что и сам finding #1.
    """

    def __init__(self, page: _Page, nodes, state: dict | None = None):
        self._page = page
        self._n = nodes
        self._render_delayed = False
        self._wait_error = False
        # Разделяемое состояние «дождались ли уже этот селектор» — общее для
        # локатора и всех его .first-производных.
        self._state = state if state is not None else {"waited": False}

    @property
    def first(self):
        loc = _Loc(self._page, self._n[:1], state=self._state)
        loc._render_delayed = self._render_delayed
        loc._wait_error = self._wait_error
        return loc

    def wait_for(self, state="visible", timeout=0):  # noqa: ARG002
        if self._wait_error:
            raise PlaywrightError("runtime error waiting for locator")
        self._state["waited"] = True
        if not self._n:
            raise PlaywrightTimeoutError("not attached")

    def count(self):
        if self._render_delayed and not self._state["waited"]:
            # Немедленное чтение без ожидания — застаёт непрогрузившийся DOM.
            return 0
        return len(self._n)

    def locator(self, sel):
        # Вложенный locator() внутри scope (напр. scope.locator(_RADIO)) решает
        # режим по СВОЕМУ ``sel``, спрашивая владеющую _Page — а не наследует
        # режим родителя (cycle-review #139 finding #1: наследование маскировало
        # гонку рендера в heuristic-путях под тавтологичным тестом).
        scope = self._n[0] if self._n else None
        nodes = [] if scope is None else [n for n in _all(scope) if _match(n, sel)]
        return self._page._make_locator(sel, nodes)

    def get_attribute(self, name):
        if name != "form" or not self._n:
            return None
        return self._n[0].attrs.get("form")


class _Page:
    def __init__(self, html, *, render_delayed_selectors=(), wait_error_selectors=()):
        self._root = _Builder()
        self._tree = self._root.feed(html)
        # Множество селекторов (как переданы в .locator(...), page-level ИЛИ
        # вложенный) для которых эмулируем гонку рендера/ошибку ожидания —
        # используется regression-тестами #139.
        self._render_delayed_selectors = set(render_delayed_selectors)
        self._wait_error_selectors = set(wait_error_selectors)

    def _make_locator(self, sel, nodes) -> _Loc:
        loc = _Loc(self, nodes)
        loc._render_delayed = sel in self._render_delayed_selectors
        loc._wait_error = sel in self._wait_error_selectors
        return loc

    def locator(self, sel):
        if m := _FORM_ID_RE.match(sel):
            return self._make_locator(
                sel,
                [
                    n
                    for n in _all(self._tree)
                    if n.tag == "form" and n.attrs.get("id") == m.group(1)
                ],
            )
        if ">> xpath=ancestor::form" in sel:
            base_sel = sel.split(" >> xpath=ancestor::form")[0]
            submit = next((n for n in _all(self._tree) if _match(n, base_sel)), None)
            if submit is None:
                return self._make_locator(sel, [])
            form = _ancestor_form(self._tree, submit)
            return self._make_locator(sel, [form] if form is not None else [])
        return self._make_locator(sel, [n for n in _all(self._tree) if _match(n, sel)])


def test_detect_uses_form_attribute_when_submit_is_outside_form():
    """HH.ru's modal keeps submit outside its form and links it with form=ID."""
    html = """
        <form id='RESPONSE_MODAL_FORM_ID'>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
        </form>
        <div><input type='checkbox'><button form='RESPONSE_MODAL_FORM_ID'
            data-qa='vacancy-response-submit-popup'>Откликнуться</button></div>
    """
    result = detect_questions(_Page(html))
    assert result == QuestionDetection.no()


def test_detect_no_questions_clean_form():
    """Форма без вопросов (только cover-letter textarea) → has_questions False."""
    html = """
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is False
    assert result.reason == ""


def test_detect_task_body_present():
    """Форма с task-body → True, reason упоминает task-body."""
    html = "<div data-qa='task-body'>...</div>"
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is True
    assert "task-body" in result.reason


def test_detect_radio_heuristic():
    """Форма с radio + cover-letter → True (heuristic)."""
    html = """
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <input type='radio' name='q1' value='a'>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is True
    assert "radio/checkbox" in result.reason


def test_detect_checkbox_heuristic():
    """Форма с checkbox + cover-letter → True (heuristic)."""
    html = """
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <input type='checkbox' name='q1' value='a'>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is True
    assert "radio/checkbox" in result.reason


def test_detect_textarea_outside_cover_letter():
    """Cover-letter popup + голый textarea (вопрос) → True (total−cover_letter=1)."""
    html = """
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <textarea></textarea>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is True
    assert "textarea вне cover-letter" in result.reason


def test_detect_full_page_cover_letter_only():
    """Только full-page cover-letter variant → False (не false-positive)."""
    html = """
        <form>
            <textarea data-qa='vacancy-response-form-letter-input'></textarea>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is False


def test_question_detection_no_yes_classmethods():
    """QuestionDetection.no()/yes() работают."""
    assert QuestionDetection.no().has_questions is False
    assert QuestionDetection.no().reason == ""
    assert QuestionDetection.yes("test").has_questions is True
    assert QuestionDetection.yes("test").reason == "test"


def test_detect_ignores_checkbox_outside_form():
    """Посторонний checkbox ВНЕ формы (напр. cookie-баннер) не должен давать
    ложный has_questions=True — форма отклика сама по себе чистая (#95 regression:
    detect_questions обязан скоупить heuristic-поиск внутри <form>, а не по всей
    странице)."""
    html = """
        <div class='cookie-banner'>
            <input type='checkbox' name='consent' value='1'>
        </div>
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is False
    assert result.reason == ""


def test_detect_ignores_radio_outside_form():
    """Посторонний radio ВНЕ формы (напр. чат-виджет) не должен давать ложный
    has_questions=True."""
    html = """
        <div class='chat-widget'>
            <input type='radio' name='rating' value='5'>
        </div>
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is False
    assert result.reason == ""


def test_detect_ignores_textarea_outside_form():
    """Посторонняя textarea ВНЕ формы (напр. форма подписки в футере) не должна
    давать ложный has_questions=True."""
    html = """
        <footer>
            <textarea></textarea>
        </footer>
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is False
    assert result.reason == ""


def test_detect_radio_inside_form_still_detected():
    """Radio ВНУТРИ формы (реальный вопрос) по-прежнему детектится после
    добавления скоупинга — регрессия не должна убить основной сценарий."""
    html = """
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <input type='radio' name='q1' value='a'>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is True
    assert "radio/checkbox" in result.reason


def test_detect_indeterminate_when_submit_not_in_form():
    """Round-2 regression: submit НЕ обёрнут в <form> (SPA без семантического
    form-тега) → indeterminate=True, has_questions=True (fail-closed на submit),
    НО НЕ обычный подтверждённый heuristic-skip — pipeline должен трактовать
    это как fail (не persistent skip), см. apply/pipeline.py::_run."""
    html = """
        <div class='cookie-banner'>
            <input type='checkbox' name='consent' value='1'>
        </div>
        <div>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </div>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is True
    assert result.indeterminate is True


def test_detect_indeterminate_when_submit_missing():
    """Submit-кнопка вообще отсутствует на странице → indeterminate (не должно
    штатно происходить — wait_apply_button гарантирует наличие ранее, но
    detect_questions обязан вести себя fail-closed-без-persist и в этом случае)."""
    html = "<textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>"
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is True
    assert result.indeterminate is True


def test_detect_not_indeterminate_for_confirmed_and_normal_paths():
    """Confirmed task-body путь и чистая форма НЕ помечаются indeterminate —
    только неопределившийся form-scope должен его выставлять."""
    clean = _Page("""
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """)
    assert detect_questions(clean).indeterminate is False

    task_body = _Page("<div data-qa='task-body'>...</div>")
    result = detect_questions(task_body)
    assert result.has_questions is True
    assert result.indeterminate is False

    scoped_radio = _Page("""
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <input type='radio' name='q1' value='a'>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """)
    result = detect_questions(scoped_radio)
    assert result.has_questions is True
    assert result.indeterminate is False


# --- #139: гонка рендера — анкета отрисовывается с задержкой ---


def test_detect_delayed_task_body_still_blocks_submit():
    """РЕГРЕССИЯ #139: task-body появляется в DOM не мгновенно (гонка рендера).
    Немедленный ``count()`` без ожидания видит 0 — старый код шёл дальше и
    submit уходил с пропущенной анкетой. detect_questions обязан ЖДАТЬ и
    увидеть task-body."""
    html = """
        <div data-qa='task-body'>Вопрос теста</div>
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    from hhru_bot.selector_groups import apply_form

    page = _Page(html, render_delayed_selectors={apply_form.APPLY_QUESTION_BODY})

    result = detect_questions(page)

    assert result.has_questions is True
    assert result.indeterminate is False
    assert "task-body" in result.reason


def test_detect_delayed_radio_still_blocks_submit():
    """РЕГРЕССИЯ #139: radio-вопрос внутри формы рендерится с задержкой —
    heuristic обязан дождаться, а не читать count() сразу."""
    html = """
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <input type='radio' name='q1' value='a'>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html, render_delayed_selectors={"input[type='radio']"})

    result = detect_questions(page)

    assert result.has_questions is True
    assert result.indeterminate is False
    assert "radio/checkbox" in result.reason


def test_detect_delayed_textarea_still_blocks_submit():
    """РЕГРЕССИЯ #139: textarea-вопрос вне cover-letter рендерится с задержкой."""
    html = """
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <textarea></textarea>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html, render_delayed_selectors={"textarea"})

    result = detect_questions(page)

    assert result.has_questions is True
    assert result.indeterminate is False
    assert "textarea вне cover-letter" in result.reason


def test_detect_delayed_form_scope_still_resolves():
    """РЕГРЕССИЯ #139: <form>-предок submit'а появляется с задержкой (сама
    форма ещё дорисовывается) — _form_scope обязан дождаться, а не сразу
    решить, что form-scope не найден (что дало бы ложный indeterminate)."""
    from hhru_bot.selector_groups import apply_form

    html = """
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <input type='radio' name='q1' value='a'>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    scope_sel = f"{apply_form.APPLY_SUBMIT_BUTTON} >> xpath=ancestor::form[1]"
    page = _Page(html, render_delayed_selectors={scope_sel})

    result = detect_questions(page)

    assert result.has_questions is True
    assert result.indeterminate is False
    assert "radio/checkbox" in result.reason


def test_detect_runtime_error_is_indeterminate_not_no_questions():
    """Не-timeout PlaywrightError при ожидании task-body (аномалия страницы) —
    fail-closed indeterminate, а НЕ молчаливое «вопросов нет»."""
    from hhru_bot.selector_groups import apply_form

    html = """
        <form>
            <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
            <button data-qa='vacancy-response-submit-popup'>Откликнуться</button>
        </form>
    """
    page = _Page(html, wait_error_selectors={apply_form.APPLY_QUESTION_BODY})

    result = detect_questions(page)

    assert result.has_questions is True
    assert result.indeterminate is True

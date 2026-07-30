"""TDD для #95: детекция вопросов в форме отклика (detect-only, NO submit).

Локальный HTML-fake: tests/_fakes.py не парсит input[type=...], bare textarea и
substring data-qa — а detect_questions именно их использует. Чтобы не трогать
общий _fakes.py (конфликт с #96), здесь свой минимальный парсер под нужные селекторы.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from hhru_bot.apply.questions import QuestionDetection, detect_questions

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
    def __init__(self, nodes):
        self._n = nodes

    def count(self):
        return len(self._n)

    def locator(self, sel):
        scope = self._n[0] if self._n else None
        if scope is None:
            return _Loc([])
        return _Loc([n for n in _all(scope) if _match(n, sel)])


class _Page:
    def __init__(self, html):
        self._root = _Builder()
        self._tree = self._root.feed(html)

    def locator(self, sel):
        if ">> xpath=ancestor::form" in sel:
            base_sel = sel.split(" >> xpath=ancestor::form")[0]
            submit = next((n for n in _all(self._tree) if _match(n, base_sel)), None)
            if submit is None:
                return _Loc([])
            form = _ancestor_form(self._tree, submit)
            return _Loc([form] if form is not None else [])
        return _Loc([n for n in _all(self._tree) if _match(n, sel)])


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

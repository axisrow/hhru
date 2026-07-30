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


class _Loc:
    def __init__(self, nodes):
        self._n = nodes

    def count(self):
        return len(self._n)


class _Page:
    def __init__(self, html):
        self._root = _Builder()
        self._tree = self._root.feed(html)

    def locator(self, sel):
        return _Loc([n for n in _all(self._tree) if _match(n, sel)])


def test_detect_no_questions_clean_form():
    """Форма без вопросов (только cover-letter textarea) → has_questions False."""
    html = "<textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>"
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
        <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
        <input type='radio' name='q1' value='a'>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is True
    assert "radio/checkbox" in result.reason


def test_detect_checkbox_heuristic():
    """Форма с checkbox + cover-letter → True (heuristic)."""
    html = """
        <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
        <input type='checkbox' name='q1' value='a'>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is True
    assert "radio/checkbox" in result.reason


def test_detect_textarea_outside_cover_letter():
    """Cover-letter popup + голый textarea (вопрос) → True (total−cover_letter=1)."""
    html = """
        <textarea data-qa='vacancy-response-popup-form-letter-input'></textarea>
        <textarea></textarea>
    """
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is True
    assert "textarea вне cover-letter" in result.reason


def test_detect_full_page_cover_letter_only():
    """Только full-page cover-letter variant → False (не false-positive)."""
    html = "<textarea data-qa='vacancy-response-form-letter-input'></textarea>"
    page = _Page(html)
    result = detect_questions(page)
    assert result.has_questions is False


def test_question_detection_no_yes_classmethods():
    """QuestionDetection.no()/yes() работают."""
    assert QuestionDetection.no().has_questions is False
    assert QuestionDetection.no().reason == ""
    assert QuestionDetection.yes("test").has_questions is True
    assert QuestionDetection.yes("test").reason == "test"

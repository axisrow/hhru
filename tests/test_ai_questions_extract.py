"""TDD для #97: extract_questions()/AIQuestionAnswerer.apply() на HTML-фикстуре.

Отдельный минимальный HTML-фейк (а не переиспользование tests/_fakes.py или
tests/test_apply_questions.py::_Page) — оба парсят только data-qa/tag-селекторы,
а ai/questions.py дополнительно использует ``form[name='vacancy_response'] ...``
(APPLY_QUESTION_FORM_BODY) и ``control.evaluate(js)`` для label-текста
(``_control_text``); ни один существующий фейк это не поддерживает.

cycle-review #373 (после мерджа CI): выявил, что до этого теста ни
extract_questions(), ни AIQuestionAnswerer.apply() не были покрыты вовсе —
единственный тест на реальный DOM-путь этого PR.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from hhru_bot.ai.questions import AIQuestionAnswerer, AnswerProposal, Question, extract_questions

pytestmark = pytest.mark.unit

_VOID = {"input", "br", "hr", "img", "meta", "link", "area", "col", "embed", "source"}


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text", "checked")

    def __init__(self, tag, attrs, parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []
        self.parent = parent
        self.text = ""
        self.checked = False


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#doc", {})
        self.stack = [self.root]

    def feed(self, data):
        super().feed(data)
        return self.root

    def handle_starttag(self, tag, attrs):
        n = _Node(tag, attrs, parent=self.stack[-1])
        self.stack[-1].children.append(n)
        if tag not in _VOID:
            self.stack.append(n)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].text += data


def _all(root):
    for c in root.children:
        yield c
        yield from _all(c)


def _inner_text(node) -> str:
    parts = [node.text] if node.text else []
    for c in node.children:
        parts.append(_inner_text(c))
    return "".join(parts)


# Selector forms used by ai/questions.py:
#   "form[name='vacancy_response'] [data-qa='task-body']"
#   "[data-qa='task-question']"
#   "input[type='radio']" / "input[type='checkbox']" / "textarea"
_QA_LEAF_RE = re.compile(r"^\[data-qa=[\"']([\w\-]+)[\"']\]$")
_TYPE_RE = re.compile(r"^input\[type=[\"'](\w+)[\"']\]$")
_TAG_RE = re.compile(r"^([a-zA-Z]+)$")
_FORM_SCOPED_QA_RE = re.compile(r"^form\[name=[\"'](\w+)[\"']\]\s+\[data-qa=[\"']([\w\-]+)[\"']\]$")


def _match_leaf(node, sel):
    if m := _QA_LEAF_RE.match(sel):
        return node.attrs.get("data-qa") == m.group(1)
    if m := _TYPE_RE.match(sel):
        return node.tag == "input" and node.attrs.get("type") == m.group(1)
    if m := _TAG_RE.match(sel):
        return node.tag == m.group(1)
    return False


class _Loc:
    def __init__(self, nodes):
        self._n = nodes

    @property
    def first(self):
        return _Loc(self._n[:1])

    def nth(self, i):
        return _Loc([self._n[i]])

    def count(self):
        return len(self._n)

    def inner_text(self):
        return _inner_text(self._n[0]) if self._n else ""

    def fill(self, value):
        if self._n:
            self._n[0].attrs["value"] = value

    def check(self):
        if self._n:
            self._n[0].checked = True

    def locator(self, sel):
        scope = self._n[0] if self._n else None
        nodes = [] if scope is None else [n for n in _all(scope) if _match_leaf(n, sel)]
        return _Loc(nodes)

    def evaluate(self, _js):
        """Emulates _control_text's closest('label')/parentElement.innerText fallback.

        M7 round 2 (#373): the real JS's ``|| el.value`` fallback was removed
        (opaque control ids are not readable labels) — this fake mirrors that,
        returning '' when there is no <label> ancestor and no text in the
        immediate parent, same as production.
        """
        if not self._n:
            return ""
        node = self._n[0]
        cur = node.parent
        while cur is not None:
            if cur.tag == "label":
                return _inner_text(cur).strip()
            cur = cur.parent
        if node.parent is not None:
            return _inner_text(node.parent).strip()
        return ""


class _Page:
    def __init__(self, html):
        self._tree = _Builder().feed(html)

    def locator(self, sel):
        if m := _FORM_SCOPED_QA_RE.match(sel):
            form_name, qa = m.group(1), m.group(2)
            forms = [
                n for n in _all(self._tree) if n.tag == "form" and n.attrs.get("name") == form_name
            ]
            nodes = []
            for form in forms:
                nodes.extend(n for n in _all(form) if n.attrs.get("data-qa") == qa)
            return _Loc(nodes)
        return _Loc([n for n in _all(self._tree) if _match_leaf(n, sel)])


_FORM_OPEN = "<form name='vacancy_response'>"


def test_extract_choice_question_with_labelled_radios():
    html = f"""
        {_FORM_OPEN}
            <div data-qa='task-body'>
                <div data-qa='task-question'>Готовы к переезду?</div>
                <label><input type='radio' value='0'>Да</label>
                <label><input type='radio' value='1'>Нет</label>
            </div>
        </form>
    """
    questions, total_bodies = extract_questions(_Page(html))

    assert len(questions) == 1
    assert total_bodies == 1
    q = questions[0]
    assert q.kind == "choice"
    assert q.options == ("Да", "Нет")
    assert q.is_radio is True


def test_extract_text_question_from_textarea():
    html = f"""
        {_FORM_OPEN}
            <div data-qa='task-body'>
                <div data-qa='task-question'>Расскажите об опыте</div>
                <textarea></textarea>
            </div>
        </form>
    """
    questions, total_bodies = extract_questions(_Page(html))

    assert len(questions) == 1
    assert total_bodies == 1
    assert questions[0].kind == "text"
    assert questions[0].options == ()


def test_extract_drops_question_with_duplicate_option_labels():
    """M7 cycle-review #373: radio без <label>-обёртки (неподтверждённая Bloko
    разметка) — _control_text() падает на ``parentElement.innerText``, который
    для radio-предка равен всему task-body (включая текст вопроса), поэтому у
    ВСЕХ опций получается ОДИНАКОВЫЙ непустой текст (проверено на живом
    Chromium: playwright evaluate этого html даёт ('Q', 'Q'), не ('', '')).
    extract_questions обязан отбросить такой вопрос: LLM не может различить
    опции, которые выглядят идентично."""
    html = f"""
        {_FORM_OPEN}
            <div data-qa='task-body'>
                <div data-qa='task-question'>Вопрос без подписей</div>
                <input type='radio' value='0'>
                <input type='radio' value='1'>
            </div>
        </form>
    """
    questions, total_bodies = extract_questions(_Page(html))

    assert questions == []
    # codex review #373 (P1): total_bodies stays 1 (a task-body WAS detected)
    # even though it produced zero recognisable Questions — pipeline.py's
    # mismatch check (len(extracted) != total_bodies) relies on this to catch
    # a dropped body even when it's the only one in the form.
    assert total_bodies == 1


def test_extract_drops_question_when_option_has_no_readable_text():
    """M7 cycle-review round 2 (#373): _control_text() previously fell back to
    ``el.value`` when no <label>/parent text existed. hh.ru's control ``value``
    attributes are opaque ids (verified on live Chromium: a radio with no text
    in its ancestry returns its raw value, e.g. '42'), which are non-blank AND
    distinct per option — so they used to pass the blank/duplicate guard and
    let the LLM answer against ids it cannot read. The ``el.value`` fallback
    was removed; this radio (wrapped only in an empty <span>, no <label>, no
    text anywhere in its parent chain) must now resolve to '' and be dropped
    by the existing blank-option guard — no separate value-specific check
    needed."""
    html = f"""
        {_FORM_OPEN}
            <div data-qa='task-body'>
                <div data-qa='task-question'>Вопрос без текстовых меток</div>
                <span><input type='radio' value='42'></span>
                <span><input type='radio' value='77'></span>
            </div>
        </form>
    """
    questions, total_bodies = extract_questions(_Page(html))

    assert questions == []
    assert total_bodies == 1


def test_extract_outside_form_name_vacancy_response_is_ignored():
    """APPLY_QUESTION_FORM_BODY скоуплен на form[name='vacancy_response'] —
    task-body в другой форме (или вне формы) не должен попасть в extracted."""
    html = """
        <form name='other'>
            <div data-qa='task-body'>
                <div data-qa='task-question'>Чужая форма</div>
                <textarea></textarea>
            </div>
        </form>
    """
    questions, total_bodies = extract_questions(_Page(html))

    assert questions == []
    assert total_bodies == 0


def test_apply_fills_textarea_and_checks_radio_by_index():
    html = f"""
        {_FORM_OPEN}
            <div data-qa='task-body'>
                <div data-qa='task-question'>Опыт</div>
                <textarea></textarea>
            </div>
            <div data-qa='task-body'>
                <div data-qa='task-question'>Готовы к переезду?</div>
                <label><input type='radio' value='0'>Да</label>
                <label><input type='radio' value='1'>Нет</label>
            </div>
        </form>
    """
    page = _Page(html)
    text_q = Question(0, "Опыт", "text")
    choice_q = Question(1, "Готовы к переезду?", "choice", ("Да", "Нет"), is_radio=True)
    proposals = [
        AnswerProposal(text_q, "5 лет с Python", 0.9),
        AnswerProposal(choice_q, "Да", 0.95, option_indices=(0,)),
    ]

    leftover = AIQuestionAnswerer.apply(page, proposals)

    bodies = page.locator("form[name='vacancy_response'] [data-qa='task-body']")
    assert bodies.nth(0).locator("textarea").first._n[0].attrs["value"] == "5 лет с Python"
    assert bodies.nth(1).locator("input[type='radio']").nth(0)._n[0].checked is True
    assert bodies.nth(1).locator("input[type='radio']").nth(1)._n[0].checked is False
    assert leftover == []


def test_apply_skips_low_confidence_proposal():
    html = f"""
        {_FORM_OPEN}
            <div data-qa='task-body'>
                <div data-qa='task-question'>Опыт</div>
                <textarea></textarea>
            </div>
        </form>
    """
    page = _Page(html)
    question = Question(0, "Опыт", "text")
    proposal = AnswerProposal(question, "", 0.1)

    leftover = AIQuestionAnswerer.apply(page, [proposal])

    bodies = page.locator("form[name='vacancy_response'] [data-qa='task-body']")
    assert bodies.nth(0).locator("textarea").first._n[0].attrs.get("value") is None
    assert leftover == [proposal]

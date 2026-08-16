"""Лёгкие фейки Playwright для тестов парсеров (responses и будущих).

Браузер не запускается — эти объекты имитируют минимальный Playwright Locator API,
который используют парсеры: ``locator(selector).first`` / ``.count()`` /
``.inner_text()`` / ``.get_attribute(name)`` / ``.nth(i)``. Селекторы — только
``[data-qa='<name>']`` (стиль проекта: все селекторы hh.ru через data-qa).

DOM строится из HTML через html.parser (stdlib, без зависимостей). Поддерево
ограничено контейнером карточки (FakeElement), чтобы ``item.locator(...)`` искал
только в пределах одной карточки, как настоящий Playwright ``locator.locator``.

Эти фейки — НЕ полная эмуляция Playwright: ровно столько, чтобы детерминированно
прогнать чистую логику парсера на HTML-фикстуре.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# ``[data-qa='name']`` → name. Селекторы hh.ru в этом проекте — только этот вид.
_QA_RE = re.compile(r"^\[data-qa=(?:['\"])([A-Za-z0-9_\-]+)(?:['\"])\]$")
# ``[data-qa^='prefix']`` → prefix (префиксное совпадение, напр. NEGOTIATION_STATUS
# = "[data-qa^='negotiations-tag']" — без этой ветки _parse_selector вернул бы None,
# и find_all(qa=None) молча матчил бы ЛЮБОЙ узел вместо ничего).
_QA_PREFIX_RE = re.compile(r"^\[data-qa\^=(?:['\"])([A-Za-z0-9_\-]+)(?:['\"])\]$")
# ``xpath=ancestor::a[1]`` → nearest <a> ancestor (used by responses.py's
# _href_or_ancestor_href, #44 live fix: negotiations-item-vacancy is a <span>
# wrapped by the actual <a href=...>).
_XPATH_ANCESTOR_RE = re.compile(r"^xpath=ancestor::([a-z]+)\[1\]$")

# void-теги без закрывающего.
_VOID = {"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}


class _DOMNode:
    """Минимальный узел DOM: tag, attrs, дочерние узлы, накопленный текст."""

    __slots__ = ("tag", "attrs", "children", "text", "parent")

    def __init__(self, tag: str, attrs: dict[str, str]):
        self.tag = tag
        self.attrs = attrs
        self.children: list[_DOMNode] = []
        self.text = ""
        self.parent: _DOMNode | None = None

    def find_all(self, tag: str | None, qa_match) -> list[_DOMNode]:  # noqa: ANN001
        out: list[_DOMNode] = []
        for child in self.children:
            if (tag is None or child.tag == tag) and qa_match(child.attrs.get("data-qa")):
                out.append(child)
            out.extend(child.find_all(tag, qa_match))
        return out

    def inner_text(self) -> str:
        """Текст узла и всех потомков, склеенный как делает inner_text Playwright."""
        parts: list[str] = []
        if self.text:
            parts.append(self.text)
        for child in self.children:
            parts.append(child.inner_text())
        return "".join(parts)


class _DOMBuilder(HTMLParser):
    """Строит дерево _DOMNode из HTML (один корень-«документ»)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _DOMNode("#document", {})
        self._stack: list[_DOMNode] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _DOMNode(tag, {k: (v or "") for k, v in attrs})
        node.parent = self._stack[-1]
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        # Снимаем со стека до совпадающего тега (терпим к незакрытым/пустым).
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].text += data


def _parse_root(html: str) -> _DOMNode:
    b = _DOMBuilder()
    b.feed(html)
    return b.root


def _nearest_ancestor(node: _DOMNode, tag: str) -> _DOMNode | None:
    current = node.parent
    while current is not None:
        if current.tag == tag:
            return current
        current = current.parent
    return None


class FakeLocator:
    """Playwright-совместимый локатор поверх среза DOM (поддерева root).

    ``.first`` и ``.nth(i)`` возвращают локатор, привязанный к конкретному узлу;
    ``.count()``/``.inner_text()``/``.get_attribute()`` работают на этом узле или
    на списке совпадений (до выбора first/nth).
    """

    def __init__(self, root: _DOMNode, qa_match, *, matches: list[_DOMNode] | None = None):  # noqa: ANN001
        self._root = root
        self._qa_match = qa_match
        # matches кешируется лениво: до first/nth локатор — «коллекция».
        self._matches = matches

    def _resolved(self) -> list[_DOMNode]:
        if self._matches is None:
            self._matches = self._root.find_all(tag=None, qa_match=self._qa_match)
        return self._matches

    @property
    def first(self) -> FakeLocator:
        matches = self._resolved()
        return FakeLocator(self._root, self._qa_match, matches=[matches[0]] if matches else [])

    def nth(self, i: int) -> FakeLocator:
        matches = self._resolved()
        return FakeLocator(self._root, self._qa_match, matches=[matches[i]])

    def count(self) -> int:
        return len(self._resolved())

    def inner_text(self) -> str:
        # first/nth зафиксировал один узел (self._matches длина 1); иначе — коллекция,
        # у Playwright inner_text на коллекции в strict mode кидает Error. Для тестов
        # берём первый, но парсеры всегда вызывают inner_text после .first.
        matches = self._resolved()
        return matches[0].inner_text() if matches else ""

    def get_attribute(self, name: str) -> str | None:
        matches = self._resolved()
        if not matches:
            return None
        return matches[0].attrs.get(name)

    def wait_for(self, *, state: str = "attached", timeout: int = 0) -> None:  # noqa: ARG002
        if self._resolved():
            return
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        raise PlaywrightTimeoutError("fake locator did not attach")

    def locator(self, selector: str) -> FakeLocator:
        """Only supports ``xpath=ancestor::<tag>[1]`` (see #44 live fix)."""
        node = self._resolved()[0] if self._resolved() else _DOMNode("#empty", {})
        ancestor_match = _XPATH_ANCESTOR_RE.match(selector.strip())
        if ancestor_match:
            found = _nearest_ancestor(node, ancestor_match.group(1))
            return FakeLocator(node, lambda _v: False, matches=[found] if found else [])
        raise AssertionError(f"FakeLocator.locator unsupported selector: {selector}")


class _CardLocator(FakeLocator):
    """Локатор карточки: ``.locator(selector)`` ищет ВНУТРИ этой карточки.

    Наследует FakeLocator (matches уже = [self-узел] от nth()), но locator()
    делает новый поиск по поддереву узла, а не по всему документу.
    """

    def locator(self, selector: str) -> FakeLocator:
        node = self._resolved()[0] if self._resolved() else _DOMNode("#empty", {})
        ancestor_match = _XPATH_ANCESTOR_RE.match(selector.strip())
        if ancestor_match:
            tag = ancestor_match.group(1)
            found = _nearest_ancestor(node, tag)
            return FakeLocator(node, lambda _v: False, matches=[found] if found else [])
        qa_match = _parse_selector(selector)
        return FakeLocator(node, qa_match)


def _parse_selector(selector: str):  # noqa: ANN201
    """``[data-qa='name']`` / ``[data-qa^='prefix']`` → предикат по data-qa.

    Ровно два вида, используемых парсерами, тестируемыми через эти фейки
    (``responses.parse_response_card``): точное совпадение и префиксное
    (``NEGOTIATION_STATUS``). Незнакомая форма — предикат ``value is not None``,
    НЕ ``True`` безусловно: пустой data-qa/чужой узел не должен молча матчиться
    (это и был баг: до этой правки find_all(qa=None) матчил вообще любой узел).
    """
    selector = selector.strip()
    exact = _QA_RE.match(selector)
    if exact:
        qa = exact.group(1)
        return lambda value: value == qa
    prefix = _QA_PREFIX_RE.match(selector)
    if prefix:
        pre = prefix.group(1)
        return lambda value: value is not None and value.startswith(pre)
    return lambda value: value is not None


class _ItemsContainer:
    """Список карточек верхнего уровня (``[data-qa='negotiations-item']``)."""

    def __init__(self, html: str, item_qa: str):
        root = _parse_root(html)
        self._nodes = root.find_all(tag=None, qa_match=lambda value: value == item_qa)

    @property
    def items(self) -> list[_CardLocator]:
        return [_CardLocator(n, lambda _v: True, matches=[n]) for n in self._nodes]


class NegotiationsPage(_ItemsContainer):
    """Фикстура /applicant/negotiations: список карточек переписки.

    ``page.items`` — локаторы ``[data-qa='negotiations-item']``; парсер вызывает
    ``item.locator(negotiations.NEGOTIATION_...)`` для дочерних полей карточки.
    """

    def __init__(self, html: str):
        super().__init__(html, item_qa="negotiations-item")

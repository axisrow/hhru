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

# void-теги без закрывающего.
_VOID = {"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}


class _DOMNode:
    """Минимальный узел DOM: tag, attrs, дочерние узлы, накопленный текст."""

    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(self, tag: str, attrs: dict[str, str]):
        self.tag = tag
        self.attrs = attrs
        self.children: list[_DOMNode] = []
        self.text = ""

    def find_all(self, tag: str | None, qa: str | None) -> list[_DOMNode]:
        out: list[_DOMNode] = []
        for child in self.children:
            if (tag is None or child.tag == tag) and (
                qa is None or child.attrs.get("data-qa") == qa
            ):
                out.append(child)
            out.extend(child.find_all(tag, qa))
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


class FakeLocator:
    """Playwright-совместимый локатор поверх среза DOM (поддерева root).

    ``.first`` и ``.nth(i)`` возвращают локатор, привязанный к конкретному узлу;
    ``.count()``/``.inner_text()``/``.get_attribute()`` работают на этом узле или
    на списке совпадений (до выбора first/nth).
    """

    def __init__(self, root: _DOMNode, qa: str | None, *, matches: list[_DOMNode] | None = None):
        self._root = root
        self._qa = qa
        # matches кешируется лениво: до first/nth локатор — «коллекция».
        self._matches = matches

    def _resolved(self) -> list[_DOMNode]:
        if self._matches is None:
            self._matches = self._root.find_all(tag=None, qa=self._qa)
        return self._matches

    @property
    def first(self) -> FakeLocator:
        matches = self._resolved()
        return FakeLocator(self._root, self._qa, matches=[matches[0]] if matches else [])

    def nth(self, i: int) -> FakeLocator:
        matches = self._resolved()
        return FakeLocator(self._root, self._qa, matches=[matches[i]])

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


class _CardLocator(FakeLocator):
    """Локатор карточки: ``.locator(selector)`` ищет ВНУТРИ этой карточки.

    Наследует FakeLocator (matches уже = [self-узел] от nth()), но locator()
    делает новый поиск по поддереву узла, а не по всему документу.
    """

    def locator(self, selector: str) -> FakeLocator:
        qa = _parse_selector(selector)
        node = self._resolved()[0] if self._resolved() else _DOMNode("#empty", {})
        return FakeLocator(node, qa)


def _parse_selector(selector: str) -> str | None:
    m = _QA_RE.match(selector.strip())
    return m.group(1) if m else None


class _ItemsContainer:
    """Список карточек верхнего уровня (``[data-qa='negotiations-item']``)."""

    def __init__(self, html: str, item_qa: str):
        root = _parse_root(html)
        self._nodes = root.find_all(tag=None, qa=item_qa)

    @property
    def items(self) -> list[_CardLocator]:
        return [_CardLocator(n, None, matches=[n]) for n in self._nodes]


class NegotiationsPage(_ItemsContainer):
    """Фикстура /applicant/negotiations: список карточек переписки.

    ``page.items`` — локаторы ``[data-qa='negotiations-item']``; парсер вызывает
    ``item.locator(negotiations.NEGOTIATION_...)`` для дочерних полей карточки.
    """

    def __init__(self, html: str):
        super().__init__(html, item_qa="negotiations-item")

"""Regression tests for mixed external-form controls (#330)."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from hhru_bot.external_forms.detect import apply_answers, scan_form

pytestmark = pytest.mark.integration


class _Node:
    def __init__(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag = tag
        self.attrs = dict(attrs)
        self.children: list[_Node] = []
        self.text = ""
        self.value: str | None = None


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", [])
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in {"input", "br", "hr", "img", "meta", "link"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].text += data


def _descendants(node: _Node):
    for child in node.children:
        yield child
        yield from _descendants(child)


def _text(node: _Node) -> str:
    return node.text + "".join(_text(child) for child in node.children)


class _Locator:
    def __init__(self, page: _Page, nodes: list[_Node]) -> None:
        self.page = page
        self.nodes = nodes

    def count(self):
        return len(self.nodes)

    @property
    def first(self):
        return _Locator(self.page, self.nodes[:1])

    def nth(self, index):
        return _Locator(self.page, self.nodes[index : index + 1])

    def get_attribute(self, name):
        return self.nodes[0].attrs.get(name) if self.nodes else None

    def all_inner_texts(self):
        return [_text(node) for node in self.nodes]

    def evaluate(self, _script):
        return self.nodes[0].tag.upper()

    def locator(self, selector):
        if selector == "option":
            return _Locator(
                self.page,
                [child for node in self.nodes for child in node.children if child.tag == "option"],
            )
        if selector.startswith("xpath=ancestor::label"):
            return _Locator(self.page, [])
        if selector == "input, textarea, select":
            return _Locator(
                self.page,
                [
                    node
                    for owner in self.nodes
                    for node in _descendants(owner)
                    if node.tag in {"input", "textarea", "select"}
                ],
            )
        return _Locator(self.page, [])

    def fill(self, value):
        self.nodes[0].value = value

    def select_option(self, *, label):
        self.nodes[0].value = label

    def all(self):
        return [_Locator(self.page, [node]) for node in self.nodes]


class _Page:
    def __init__(self, html: str) -> None:
        parser = _Parser()
        parser.feed(html)
        self.root = parser.root

    def locator(self, selector):
        nodes = list(_descendants(self.root))
        if selector == "form":
            return _Locator(self, [node for node in nodes if node.tag == "form"])
        if selector == "label[for='name']":
            return _Locator(
                self,
                [node for node in nodes if node.tag == "label" and node.attrs.get("for") == "name"],
            )
        if selector == "label[for='details']":
            return _Locator(
                self,
                [
                    node
                    for node in nodes
                    if node.tag == "label" and node.attrs.get("for") == "details"
                ],
            )
        if selector == "label[for='role']":
            return _Locator(
                self,
                [node for node in nodes if node.tag == "label" and node.attrs.get("for") == "role"],
            )
        if selector.startswith("#"):
            return _Locator(self, [node for node in nodes if node.attrs.get("id") == selector[1:]])
        if selector.startswith("form "):
            selector = selector[5:]
        if " >> nth=" in selector:
            selector, raw_index = selector.rsplit(" >> nth=", 1)
            candidates = self._match_nodes(nodes, selector)
            return _Locator(self, candidates[int(raw_index) : int(raw_index) + 1])
        return _Locator(self, self._match_nodes(nodes, selector))

    @staticmethod
    def _match_nodes(nodes, selector):
        if selector in {"input, textarea, select", "input,textarea,select"}:
            return [node for node in nodes if node.tag in {"input", "textarea", "select"}]
        if selector == "textarea":
            return [node for node in nodes if node.tag == "textarea"]
        if selector == "select":
            return [node for node in nodes if node.tag == "select"]
        if selector == "input:not([type]), input[type='text']":
            return [
                node
                for node in nodes
                if node.tag == "input" and node.attrs.get("type") in {None, "text"}
            ]
        if selector.startswith("input[type='"):
            kind = selector.removeprefix("input[type='").removesuffix("']")
            return [
                node for node in nodes if node.tag == "input" and node.attrs.get("type") == kind
            ]
        return []


def test_mixed_controls_keep_type_specific_indexes_and_selects():
    page = _Page(
        """
        <form>
          <label for="name">Name</label><input id="name" type="text">
          <label for="details">Details</label><textarea id="details"></textarea>
          <label for="role">Role</label><select id="role"><option>Developer</option><option>Manager</option></select>
          <input type="email" aria-label="Email">
        </form>
        """
    )

    scan = scan_form(page)

    assert [field.label for field in scan.fields] == ["name", "details", "role", "email"]
    assert scan.fields[2].selector == "#role"
    assert scan.fields[2].options == ("developer", "manager")
    assert scan.fields[3].selector == "form input[type='email'] >> nth=0"
    assert apply_answers(
        page,
        scan,
        {"name": "Ada", "details": "Experience", "role": "Developer", "email": "ada@example.test"},
    ) == (True, [])

"""Live-замер #972: DOM баннера ошибки на /resume/{несуществующий id}.

Read-only по построению: единственное действие — навигация на URL резюме,
которого нет в аккаунте. Никакие кнопки не нажимаются, формы не открываются,
сохранение невозможно — страница сбойного экрана не содержит мутирующих
контролов вообще.

Опциональный прогон (маркер live_read исключён из обычной сюиты):

    HHRU_LIVE_CONFIG=/путь/к/config.yaml \
        pytest -m live_read tests/test_resume_error_banner_live.py -q -s

Что снимает (issue #972):
1. Полный HTML сбойного экрана — дамп в data/logs/, селектор извлекается
   чтением дампа, не угадыванием. Скриншот владельца (2026-09-05) — первичное
   доказательство текста баннера «Произошла ошибка. Возникли неполадки, но
   мы уже работаем над их устранением.».
2. Структуру окружения баннера: тег/id/class/data-qa/role всех элементов,
   содержащих текст баннера, их предков и соседей — источник стабильного
   селектора (data-qa если есть; иначе текстовый/ARIA-паттерн, как у
   ``_ENTRY_NOT_FOUND_TEXT`` в resume_education.py).

Целевой resume_id по умолчанию — заведомо несуществующий (38 нулей:
очевидно поддельный, правило #828). Опционально переопределяется env
``HHRU_LIVE_RESUME_ID`` — для повторения замера на конкретном id
(например, удалённом резюме) без хардкода реальных значений в репо.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hhru_bot.browser import HH_BASE_URL, goto_hh, launch_context
from hhru_bot.config import load_config

_LIVE_CONFIG = os.environ.get("HHRU_LIVE_CONFIG")

pytestmark = [
    pytest.mark.live_read,
    pytest.mark.skipif(
        not _LIVE_CONFIG,
        reason="требуется явный opt-in HHRU_LIVE_CONFIG=<config.yaml с сессией>",
    ),
]

LOG_DIR = Path(__file__).resolve().parents[1] / "data" / "logs"
# 38 нулей: правильная длина hex-id резюме hh.ru, очевидно поддельное значение.
NONEXISTENT_RESUME_ID = "0" * 38
BANNER_TEXT = "Произошла ошибка"

# JS-снимок структуры вокруг баннера: сам элемент с текстом, его предки и
# прямые дети — с тегом, атрибутами и обрезанным outerHTML. Чтение DOM, не
# внутренний API hh.ru.
_BANNER_STRUCTURE_JS = """(needle) => {
  const out = [];
  const seen = new Set();
  const describe = (el, depth) => {
    if (!el || el.nodeType !== 1 || seen.has(el)) return;
    seen.add(el);
    const attrs = {};
    for (const attr of el.attributes || []) attrs[attr.name] = attr.value;
    out.push({
      depth,
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      dataQa: el.getAttribute('data-qa'),
      role: el.getAttribute('role'),
      ariaLive: el.getAttribute('aria-live'),
      className: (el.className && el.className.toString
        ? el.className.toString().slice(0, 160) : null),
      text: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
      outerHTML: el.outerHTML.replace(/\\s+/g, ' ').slice(0, 400),
      attrs,
    });
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const roots = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if ((node.textContent || '').includes(needle)) {
      roots.push(node.parentElement);
    }
  }
  for (const root of roots || []) {
    describe(root, 0);
    let depth = 1;
    for (let el = root && root.parentElement; el && depth <= 4; el = el.parentElement) {
      describe(el, -depth);
      depth += 1;
    }
    for (const child of (root ? root.querySelectorAll('*') : [])) {
      describe(child, 1);
    }
  }
  return {url: location.href, title: document.title, elements: out};
}"""


def test_capture_resume_error_banner_dom():
    assert _LIVE_CONFIG is not None  # skipif выше гарантирует явный opt-in
    config = load_config(_LIVE_CONFIG)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    resume_id = os.environ.get("HHRU_LIVE_RESUME_ID", NONEXISTENT_RESUME_ID)

    with launch_context(
        config.storage_state_file, headless=True, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        goto_hh(page, f"{HH_BASE_URL}/resume/{resume_id}")
        # Сбойный экран — серверная страница; даём возможной клиентской
        # догрузке устояться, чтобы дамп не был преждевременным срезом.
        page.wait_for_timeout(2_000)

        html = page.content()
        (LOG_DIR / f"972_resume_error_banner_{resume_id[:6]}.html").write_text(
            html, encoding="utf-8"
        )
        structure = page.evaluate(_BANNER_STRUCTURE_JS, BANNER_TEXT)

        print(f"[MEASURE #972] url={structure['url']} title={structure['title']!r}")
        print(f"[MEASURE #972] body text: {page.locator('body').inner_text()[:400]!r}")
        print(f"[MEASURE #972] banner elements found: {len(structure['elements'])}")
        for element in structure["elements"]:
            print(f"[MEASURE #972]   {element}")
        print(f"[MEASURE #972] дамп: {LOG_DIR / f'972_resume_error_banner_{resume_id[:6]}.html'}")
        # Одна read-only навигация; см. докстринг — мутирующих действий нет.

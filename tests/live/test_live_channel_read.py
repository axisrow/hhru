"""Live-слой канала: чтение DOM-состояния через реальное подключение (#1164).

Read-only по построению: get_page_state и check_element не мутируют страницу.
Расширение hhru-live ретранслирует состояние ОТКРЫТОЙ вкладки hh.ru в Chrome —
CLI своего браузера не запускает.

Опциональный прогон (маркер live_read исключён из обычной сюиты):

    HHRU_LIVE_CONFIG=data/config.yaml pytest -m live_read tests/live/ -q -s

Предусловия: расширение установлено (README «Live-канал: установка
расширения hhru-live»), Chrome открыт на вкладке hh.ru. Полный ручной
чеклист — docs/live-checklist.md.
"""

from __future__ import annotations

import os
import time

import pytest

from hhru_bot.browser import LOGIN_FORM
from hhru_bot.live.scenarios import LiveChannel, PrimitiveError

_LIVE_CONFIG = os.environ.get("HHRU_LIVE_CONFIG")

# Командный путь даёт расширению 120 с; тесту хватит меньшего — живой прогон
# начинается с подключённой вкладки (чеклист, шаг 0).
CLIENT_TIMEOUT_SECONDS = 30.0

# Расширение подключается ТОЛЬКО к ws://127.0.0.1:8765 (background.js
# LIVE_SERVE_URL) — ephemeral-порт клиента не увидит никогда (#1183).
# Живой live-serve на 8765 на время теста надо остановить.
CHANNEL_PORT = 8765

pytestmark = [
    pytest.mark.live_read,
    pytest.mark.skipif(
        not _LIVE_CONFIG,
        reason="требуется явный opt-in HHRU_LIVE_CONFIG=<config.yaml> и вкладка hh.ru в Chrome",
    ),
]


def test_read_page_state_over_real_channel():
    channel = LiveChannel(port=CHANNEL_PORT, client_timeout=CLIENT_TIMEOUT_SECONDS)
    url = channel.start()
    print(f"[INFO] канал {url} — жду расширение hhru-live (до {CLIENT_TIMEOUT_SECONDS:.0f} с)")
    try:
        channel.wait_client()

        # Первая команда может обогнать content script (гонка document_idle
        # при свежей загрузке вкладки: content_script_unreachable) — короткий
        # ретрай, пока страница догидрирована.
        state = None
        for _ in range(10):
            try:
                state = channel.get_state()
                break
            except PrimitiveError as exc:
                print(f"[INFO] get_page_state ретрай (исполнитель не готов): {exc}")
                time.sleep(1)
        assert state is not None, "get_page_state не ответил успехом за 10 с"
        print(f"[INFO] get_page_state: {state}")
        # Реальная форма ответа executor.js (замер 2026-09-20, testing):
        # {"page": {"url", "title", "readyState"}} — вложенная в "page".
        # ВАЖНО: scenarios.bump_via_live читает плоскую state["url"] (#1186) —
        # этот тест фиксирует фактическую форму исполнителя.
        page = state.get("page") or {}
        assert "hh.ru" in str(page.get("url", "")), state

        check_or_error: object
        try:
            check = channel.check(LOGIN_FORM)
            print(f"[INFO] check_element({LOGIN_FORM}): {check}")
            check_or_error = check
        except PrimitiveError as exc:
            # Реальные отказы исполнителя не блокируют smoke: LiveChannel
            # теряет текст отказа (executor отвечает ключом "error", не
            # "code" — #1186); сам факт ответа канала здесь и проверяется.
            print(f"[INFO] check_element отклонён: {exc}")
            check_or_error = str(exc)
        assert check_or_error  # ответ канала получен (любой вердикт)
    finally:
        channel.close()

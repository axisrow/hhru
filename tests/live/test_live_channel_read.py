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

import pytest

from hhru_bot.browser import LOGIN_FORM
from hhru_bot.live.scenarios import LiveChannel

_LIVE_CONFIG = os.environ.get("HHRU_LIVE_CONFIG")

# Командный путь даёт расширению 120 с; тесту хватит меньшего — живой прогон
# начинается с подключённой вкладки (чеклист, шаг 0).
CLIENT_TIMEOUT_SECONDS = 30.0

pytestmark = [
    pytest.mark.live_read,
    pytest.mark.skipif(
        not _LIVE_CONFIG,
        reason="требуется явный opt-in HHRU_LIVE_CONFIG=<config.yaml> и вкладка hh.ru в Chrome",
    ),
]


def test_read_page_state_over_real_channel():
    channel = LiveChannel(client_timeout=CLIENT_TIMEOUT_SECONDS)
    url = channel.start()
    print(f"[INFO] канал {url} — жду расширение hhru-live (до {CLIENT_TIMEOUT_SECONDS:.0f} с)")
    try:
        channel.wait_client()

        state = channel.get_state()
        print(f"[INFO] get_page_state: {state}")
        # Вкладка обязана быть на hh.ru: расширение работает только там
        # (README), URL — первичный признак живого контура.
        assert "hh.ru" in str(state.get("url", "")), state

        check = channel.check(LOGIN_FORM)
        print(f"[INFO] check_element({LOGIN_FORM}): {check}")
        # Форма ответа расширения: found/visible — договорённость S2
        # (executor.js checkElement), ключи не зависят от состояния страницы.
        assert "found" in check and "visible" in check
    finally:
        channel.close()

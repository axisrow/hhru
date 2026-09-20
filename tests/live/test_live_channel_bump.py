"""Live-слой канала: боевой bump через живую вкладку (#1164).

Мутирующий тест (маркер live_write): реально поднимает резюме кликом
расширения в открытой вкладке /applicant/resumes. Кулдаун 4ч и дневной лимит
НЕ обходятся — те же гейты Throttle, что у команды bump-live; если поднимать
рано, тест пропускается, а не мутит в обход ограничителей.

Запуск (только через live_test_safe.sh, как все live_write):

    HHRU_LIVE_CONFIG=data/config.yaml ./scripts/live_test_safe.sh tests/live/ -q -s

Предусловия: расширение hhru-live в Chrome, вкладка на /applicant/resumes,
валидный resume_url в конфиге. Полный ручной чеклист — docs/live-checklist.md.
"""

from __future__ import annotations

import os

import pytest

from hhru_bot.cli import DEFAULT_HISTORY_PATH
from hhru_bot.commands._audit import action_status, record_resume_action
from hhru_bot.config import is_resume_url_placeholder, load_config
from hhru_bot.history import History
from hhru_bot.live.scenarios import LiveChannel, bump_via_live
from hhru_bot.throttle import LimitReached, Throttle

_LIVE_CONFIG = os.environ.get("HHRU_LIVE_CONFIG")

CLIENT_TIMEOUT_SECONDS = 30.0

# Расширение подключается ТОЛЬКО к ws://127.0.0.1:8765 (background.js
# LIVE_SERVE_URL) — ephemeral-порт клиента не увидит никогда (#1183).
# Живой live-serve на 8765 на время теста надо остановить.
CHANNEL_PORT = 8765

pytestmark = [
    pytest.mark.live_write,
    pytest.mark.skipif(
        not _LIVE_CONFIG,
        reason="требуется явный opt-in HHRU_LIVE_CONFIG=<config.yaml> и вкладка hh.ru в Chrome",
    ),
]


def test_bump_via_live_tab():
    assert _LIVE_CONFIG is not None  # skipif выше гарантирует явный opt-in
    config = load_config(_LIVE_CONFIG)
    name = os.environ.get("HHRU_LIVE_RESUME")
    if name is None:
        resume = config.resumes[0]
    else:
        # Мутационный тест: опечатка в имени не имеет права молча поднять
        # другое резюме — отказ со списком доступных, как у команды bump-live.
        resume = next((r for r in config.resumes if r.id == name), None)
        if resume is None:
            pytest.fail(
                f"HHRU_LIVE_RESUME={name!r} нет в конфиге; доступные: "
                + ", ".join(r.id for r in config.resumes)
            )
    if is_resume_url_placeholder(resume.resume_url):
        pytest.skip(f"в конфиге {resume.id} плейсхолдер resume_url — укажите реальное резюме")

    # Гейты боевого пути (bump-live): без них тест мутил бы в обход лимитов.
    history = History(DEFAULT_HISTORY_PATH)
    throttle = Throttle(config.throttle, history)
    try:
        throttle.check_bump_limit(resume.resume_id, dry_run=False)
    except LimitReached as exc:
        pytest.skip(f"дневной лимит: {exc}")
    can_bump, wait_left = throttle.can_bump_now(resume.resume_id)
    if not can_bump:
        pytest.skip(f"рано поднимать, кулдаун {wait_left}")

    channel = LiveChannel(port=CHANNEL_PORT, client_timeout=CLIENT_TIMEOUT_SECONDS)
    url = channel.start()
    print(f"[INFO] канал {url} — жду расширение hhru-live (до {CLIENT_TIMEOUT_SECONDS:.0f} с)")
    try:
        channel.wait_client()
        result = bump_via_live(channel, resume, dry_run=False)
    finally:
        channel.close()

    print(f"[INFO] bump_via_live: {result.resume_id} -> {result.reason}")
    # Ledger честный с обеих сторон: действие пишется тем же путём, что у
    # команды bump-live (commands/bump_live.py), — иначе live-тестовый подъём
    # невидим локальному кулдауну 4ч и окну 24ч боевых прогонов. Гейт тот же:
    # исходы без реального клика (acted=False) в actions не пишутся.
    if result.acted:
        record_resume_action(
            history,
            resume.resume_id,
            "bump",
            action_status(dry_run=False, success=result.success, uncertain=result.uncertain),
            result.reason,
        )
    # Инварианты вердикта (#176/#1161), не конкретный исход: боевой вердикт
    # зависит от состояния hh.ru (кулдаун, лимит) и проверяется человеком
    # по чеклисту; выдумывать успех тест не имеет права.
    assert not (result.success and result.uncertain)
    if result.success:
        assert result.acted
    if result.acted:
        # Анти-бан-пауза после реального действия — инвариант боевого пути.
        throttle.wait(f"после поднятия резюме '{resume.id}' (live-тест)")

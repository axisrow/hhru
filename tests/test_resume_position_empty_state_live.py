"""Live-замер #954: empty-state фильтра дерева специализаций editor-модалки.

Read-only по построению: открытие редактора позиции, открытие модалки
специализаций и ввод в её поиск ничего не сохраняют на hh.ru — submit
модалки и SAVE редактора не нажимаются вовсе, браузер закрывается с
несохранённой формой (клиентские состояния умирают вместе с контекстом).

Опциональный прогон (маркер live_read исключён из обычной сюиты):

    HHRU_LIVE_CONFIG=/путь/к/config.yaml \
        pytest -m live_read tests/test_resume_position_empty_state_live.py -q -s

Что снимает (issue #954, «Что разведать»):
1. Полный HTML модалки ДО фильтра и ПОСЛЕ запроса без совпадений — источник
   селектора empty-state и признака готовности дерева; дампы кладутся в
   data/logs/, селектор извлекается чтением дампа, не угадыванием.
2. Распределение времени до стабилизации результата фильтра (p50/p95) для
   пустого и непустого запроса — калибровка _CONTROL_WAIT_TIMEOUT_MS.

Стабилизация определяется БЕЗ предположения о пустом селекторе: результат
считается готовым, когда число отрисованных опций и длина HTML модалки не
меняются 3 подряд замера (75 мс) и прошло не менее 150 мс с fill().
"""

from __future__ import annotations

import os
import statistics
from pathlib import Path

import pytest

from hhru_bot.browser import launch_context
from hhru_bot.config import load_config
from hhru_bot.resume_position import (
    SPECIALIZATION_ADD,
    SPECIALIZATION_MODAL,
    SPECIALIZATION_OPTION,
    SPECIALIZATION_SEARCH,
    open_position_form,
)

_LIVE_CONFIG = os.environ.get("HHRU_LIVE_CONFIG")

pytestmark = [
    pytest.mark.live_read,
    pytest.mark.skipif(
        not _LIVE_CONFIG,
        reason="требуется явный opt-in HHRU_LIVE_CONFIG=<config.yaml с сессией>",
    ),
]

LOG_DIR = Path("data/logs")
# Запросы. Пустой — заведомо без совпадений в дереве профессий; непустой —
# префикс распространённого листа (живое имя не критично: важен сам факт
# непустого результата фильтра).
EMPTY_QUERY = "zzq9jx несуществующая специализация 954"
MATCH_QUERY = "Бухгалтер"
PROBES = 8
SETTLE_POLLS = 3
POLL_MS = 25
MIN_ELAPSED_MS = 150


def _wait_filter_settled(page, modal) -> tuple[int, int]:
    """Poll until the filtered tree stops mutating; return (ms, option_count)."""
    elapsed = 0
    option_count = -1
    html_len = -1
    stable = 0
    while elapsed < 5000:
        page.wait_for_timeout(POLL_MS)
        elapsed += POLL_MS
        current_count = page.locator(SPECIALIZATION_OPTION).count()
        current_len = len(modal.inner_html())
        if current_count == option_count and current_len == html_len and elapsed >= MIN_ELAPSED_MS:
            stable += 1
            if stable >= SETTLE_POLLS:
                return elapsed, current_count
        else:
            stable = 0
        option_count = current_count
        html_len = current_len
    return elapsed, option_count


def test_measure_specialization_filter_empty_state():
    config = load_config(_LIVE_CONFIG)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    with launch_context(
        config.storage_state_file, headless=True, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        flow = None
        for resume in config.resumes:
            candidate = open_position_form(page, resume, enter_wizard=False)
            if candidate.kind == "editor":
                flow = candidate
                break
        assert flow is not None, "ни одно резюме конфига не открылось в editor-режиме"

        page.locator(SPECIALIZATION_ADD).click()
        modal = page.locator(SPECIALIZATION_MODAL)
        assert modal.count() == 1, "модалка специализаций не открылась"
        search = page.locator(SPECIALIZATION_SEARCH)
        assert search.count() == 1, "селектор поиска модалки не подтверждён"

        (LOG_DIR / "954_specialization_modal_before_filter.html").write_text(
            modal.inner_html(), encoding="utf-8"
        )

        timings: dict[str, list[int]] = {}
        counts: dict[str, list[int]] = {}
        for label, query in (("empty", EMPTY_QUERY), ("match", MATCH_QUERY)):
            timings[label] = []
            counts[label] = []
            for _probe in range(PROBES):
                search.fill(query)
                elapsed_ms, option_count = _wait_filter_settled(page, modal)
                timings[label].append(elapsed_ms)
                counts[label].append(option_count)
            if label == "empty":
                # Деньги вопроса: DOM пустого результата фильтра.
                (LOG_DIR / "954_specialization_modal_empty_state.html").write_text(
                    modal.inner_html(), encoding="utf-8"
                )

        for label in ("empty", "match"):
            series = sorted(timings[label])
            p50 = statistics.median(series)
            p95 = series[-1] if len(series) < 20 else series[int(len(series) * 0.95) - 1]
            print(
                f"[MEASURE #954] {label}: probes={PROBES} "
                f"p50={p50}ms p95={p95}ms max={series[-1]}ms "
                f"option_counts={counts[label]}"
            )
        print(
            "[MEASURE #954] дампы: data/logs/954_specialization_modal_before_filter.html, "
            "data/logs/954_specialization_modal_empty_state.html"
        )
        # Ни submit модалки, ни SAVE редактора не нажимались: см. докстринг.

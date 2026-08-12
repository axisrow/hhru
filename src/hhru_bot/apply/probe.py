"""Probe-режим (#8): диагностический дамп формы отклика БЕЗ отправки.

Владелец: #8.

Зачем: непроверенные селекторы формы отклика (`/applicant/vacancy_response`,
рендерится только залогиненному через JS) нельзя сверить вслепую. Probe безопасно
доходит до формы, заполняет письмо и сдампит `page.screenshot()` + `page.content()`
в `data/logs/`, после чего останавливается — `submit_button.click()` НИКОГДА не
вызывается. По дампу #10 сверяет селекторы на живой сессии.

Атомарность «дойти до формы и не отправить» — главный инвариант этого модуля.
Поэтому шаги НЕ переиспользуют `apply/steps.fill_response_form` (тот кликает
submit); заполнение письма здесь своё, без блока отправки. pipeline/steps/success/
dedup не трогаются — probe живёт отдельным оркестратором.

`ProbeHook`/`NOOP_PROBE` ниже — нейтральный хук для pipeline.py (точки вызова
`ctx.probe(...)` там пред-добавлены). Это отдельный механизм от `dump_probe_snapshot`,
который вызывается напрямую из probe-команды.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from ..browser import PAGE_STATE, goto_hh
from ..logging_setup import LOG_DIR
from ..search import VacancyCard
from ..selector_groups import apply_form
from . import steps as apply_steps
from .dedup import check_already_responded
from .letter import CoverLetterProvider, render_cover_letter
from .questions import detect_questions
from .steps import OPTIONAL_FIELD_TIMEOUT_MS, _is_visible

logger = logging.getLogger("hhru_bot.apply.probe")

# Дампы probe пишем туда же, куда и остальные логи — relative-to-cwd (см.
# logging_setup.LOG_DIR — data/logs, #133). Раньше было PROJECT_ROOT / "logs",
# но PROJECT_ROOT убран из config.py (ломался после pip install).
PROBE_LOG_DIR = LOG_DIR


@dataclass
class ProbeContext:
    """Контекст одного дампа probe. Лёгкий — не несёт состояния формы, только метаданные."""

    vacancy_id: str
    vacancy_url: str
    stage: str
    logs_dir: Path = field(default_factory=lambda: PROBE_LOG_DIR)


@dataclass
class ProbeResult:
    """Результат probe-прогона одной вакансии."""

    vacancy: VacancyCard
    success: bool
    reason: str = ""
    dump_paths: dict[str, Path] = field(default_factory=dict)
    # #95: форма требует анкеты — dump не делается, submit не кликается.
    skipped: bool = False

    def fail(self, reason: str) -> ProbeResult:
        return ProbeResult(self.vacancy, False, reason)

    def ok(self, reason: str, dump_paths: dict[str, Path]) -> ProbeResult:
        return ProbeResult(self.vacancy, True, reason, dump_paths)


def dump_probe_snapshot(page: Page, ctx: ProbeContext) -> dict[str, Path]:
    """Снимает screenshot + HTML текущего состояния страницы в data/logs/.

    Идемпотентно по имени (vacancy_id + stage): повторный вызов перезаписывает
    файлы той же вакансии. Возвращает пути к записанным файлам.
    """
    ctx.logs_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{ctx.vacancy_id}_{ctx.stage}"

    png_path = ctx.logs_dir / f"probe_{slug}.png"
    html_path = ctx.logs_dir / f"probe_{slug}.html"

    png_path.write_bytes(page.screenshot(full_page=True))
    html_path.write_text(page.content(), encoding="utf-8")

    logger.info("probe[%s]: дамп сохранён — %s, %s", ctx.stage, png_path.name, html_path.name)
    return {"screenshot": png_path, "html": html_path}


def _fill_cover_letter_only(page: Page, resume_id: str, letter: str) -> None:
    """Заполняет письмо в форме отклика, БЕЗ submit.

    Аналог блока заполнения `apply/steps.fill_response_form`, но намеренно без
    блока `submit_button.click()` — атомарность probe. Селекторы те же (shared,
    владеет apply_form), логика выбора резюме делегирована steps._select_resume_in_form.

    #139: раньше опциональные поля определялись голым ``count() > 0`` сразу
    после навигации плюс фиксированная пауза-заглушка (полсекунды сна между
    полями) — гонка рендера (поле ещё не отрисовалось) молча пропускала
    заполнение письма, и дамп выглядел валидным, хотя письмо не заполнено
    (ложная уверенность перед боевым запуском). Переиспользуем
    ``steps._is_visible`` — тот же приём (bounded wait_for + fail-closed на
    «поля нет»), что и в fill_response_form. Фиксированных пауз-заглушек
    (запрещены докстрингом steps.py) здесь больше нет.
    """
    if _is_visible(page, apply_form.APPLY_RESUME_SELECT, timeout_ms=OPTIONAL_FIELD_TIMEOUT_MS):
        apply_steps._select_resume_in_form(page, resume_id)

    if _is_visible(
        page, apply_form.APPLY_COVER_LETTER_TOGGLE, timeout_ms=OPTIONAL_FIELD_TIMEOUT_MS
    ):
        page.locator(apply_form.APPLY_COVER_LETTER_TOGGLE).click()
        # Клик раскрывает textarea — её готовность ждёт следующий _is_visible ниже.

    if _is_visible(
        page, apply_form.APPLY_COVER_LETTER_TEXTAREA, timeout_ms=OPTIONAL_FIELD_TIMEOUT_MS
    ):
        page.locator(apply_form.APPLY_COVER_LETTER_TEXTAREA).fill(letter)


def probe_vacancy(
    page: Page,
    vacancy: VacancyCard,
    resume_id: str,
    cover_letter_template: str,
    logs_dir: Path | None = None,
    letter_provider: CoverLetterProvider | None = None,
) -> ProbeResult:
    """Probe одной вакансии: дойти до формы, заполнить письмо, сдампить, НЕ отправлять.

    Шаги: открыть вакансию → проверка «уже откликались» → ждать кнопку отклика →
    навигация на форму → заполнить письмо → дамп screenshot+HTML → стоп.
    submit никогда не кликается.

    #17 (follow-up #54): если передан letter_provider — письмо рендерится им
    (AI под вакансию, виден в дампе формы), иначе — статичный .format (обратная
    совместимость). Атомарность probe при этом не меняется: провайдер только
    генерирует текст письма, submit не трогается. Аналогично pipeline._run.
    """
    ctx_dir = ProbeContext(
        vacancy_id=vacancy.vacancy_id,
        vacancy_url=vacancy.url,
        stage="form",
        logs_dir=logs_dir or PROBE_LOG_DIR,
    )

    logger.info("[PROBE] Открываю вакансию: %s (%s)", vacancy.title, vacancy.url)
    goto_hh(page, vacancy.url)

    if reason := check_already_responded(page, vacancy):
        logger.info("[PROBE] Вакансия '%s' уже откликнута — пропускаю без дампа", vacancy.title)
        return ProbeResult(vacancy, False, reason)

    if not apply_steps.wait_apply_button(page):
        return ProbeResult(vacancy, False, "кнопка отклика не найдена на странице")

    apply_steps.navigate_to_response_form(page)
    logger.info("[PROBE] Дошёл до формы отклика, заполняю письмо (без отправки)")

    # #95: detect-only. Если форма требует анкеты — НЕ заполняем и НЕ дампим вопросы,
    # возвращаем skip (record_skip делает apply-цикл, у которого есть history).
    # round-2 fix: indeterminate (границы формы не резолвились) — тоже без дампа,
    # но помечаем отдельно в логе для диагностики (не подтверждённый has_questions).
    questions = detect_questions(page)
    if questions.has_questions:
        marker = f"[WARN {PAGE_STATE['indeterminate']}]" if questions.indeterminate else "[INFO]"
        logger.info("%s %s — %s", marker, vacancy.title, questions.reason)
        return ProbeResult(vacancy, success=False, reason=questions.reason, skipped=True)

    # #17 (follow-up #54): письмо через провайдер, если он задан (AI под вакансию),
    # иначе статичный .format. Провайдер сам падает на шаблон при сбое LLM —
    # исключений не ждём (см. ai/letters.py). Атомарность probe не нарушается:
    # провайдер только генерирует текст, submit не кликается.
    if letter_provider is not None:
        letter = letter_provider.render(vacancy).text
    else:
        letter = render_cover_letter(cover_letter_template, vacancy)
    _fill_cover_letter_only(page, resume_id, letter)

    dump_paths = dump_probe_snapshot(page, ctx_dir)
    logger.info("[PROBE] Дамп формы готов для вакансии '%s'. Отправка НЕ выполнена.", vacancy.title)
    return ProbeResult(vacancy, True, "probe dump saved", dump_paths)


class ProbeHook:
    """Нейтральный хук (no-op) для pipeline.py по умолчанию.

    Точки `ctx.probe(stage, ...)` в pipeline пред-добавлены нейтрально; в боевом
    apply-пути они не должны ничего отправлять/дампить (иначе нарушится
    двухшаговая навигация). Этот хук остаётся no-op. probe-команда использует
    отдельный прямой путь через probe_vacancy/dump_probe_snapshot выше.
    """

    def __call__(self, stage: str, **kwargs: Any) -> None:
        logger.debug("probe[%s] (no-op): %s", stage, ", ".join(kwargs))


# Синглтон-заглушка для pipeline.py (не трогать его точки вызова).
NOOP_PROBE = ProbeHook()

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

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..browser import PAGE_STATE, goto_hh, has_login_form
from ..logging_setup import LOG_DIR
from ..search import VacancyCard
from ..vacancy_refresh import VacancyBodyCache, refresh_card
from . import steps as apply_steps
from .dedup import check_already_responded
from .letter import CoverLetterProvider, render_cover_letter
from .questions import detect_questions

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


def dump_probe_snapshot(
    page: Page, ctx: ProbeContext, *, best_effort: bool = False
) -> dict[str, Path]:
    """Снимает screenshot + HTML текущего состояния страницы в data/logs/.

    Идемпотентно по имени (vacancy_id + stage): повторный вызов перезаписывает
    файлы той же вакансии. Возвращает пути к записанным файлам.
    """
    ctx.logs_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{ctx.vacancy_id}_{ctx.stage}"

    png_path = ctx.logs_dir / f"probe_{slug}.png"
    html_path = ctx.logs_dir / f"probe_{slug}.html"

    paths: dict[str, Path] = {}
    try:
        png_path.write_bytes(page.screenshot(full_page=True))
        paths["screenshot"] = png_path
    except PlaywrightError as exc:
        if not best_effort:
            raise
        logger.warning("probe[%s]: screenshot недоступен: %s", ctx.stage, exc)
    try:
        html_path.write_text(page.content(), encoding="utf-8")
        paths["html"] = html_path
    except PlaywrightError as exc:
        if not best_effort:
            raise
        logger.warning("probe[%s]: HTML недоступен: %s", ctx.stage, exc)

    if paths:
        logger.info(
            "probe[%s]: дамп сохранён — %s",
            ctx.stage,
            ", ".join(p.name for p in paths.values()),
        )
    return paths


def _fill_cover_letter_only(page: Page, resume_id: str, letter: str) -> str | None:
    """Заполняет письмо в форме отклика, БЕЗ submit. None — заполнено.

    Аналог блока заполнения `apply/steps.fill_response_form`, но намеренно без
    блока `submit_button.click()` — атомарность probe. Селекторы те же (shared,
    владеет apply_form), выбор резюме делегирован steps.ensure_resume_selected —
    той же функции, которую зовёт боевой fill_response_form.

    #139: раньше опциональные поля определялись голым ``count() > 0`` сразу
    после навигации плюс фиксированная пауза-заглушка (полсекунды сна между
    полями) — гонка рендера (поле ещё не отрисовалось) молча пропускала
    заполнение письма, и дамп выглядел валидным, хотя письмо не заполнено
    (ложная уверенность перед боевым запуском). Фиксированных пауз-заглушек
    (запрещены докстрингом steps.py) здесь больше нет.

    Round-2: проверка резюме тоже не своя. Прежний гейт (``_is_visible`` с
    коротким ``OPTIONAL_FIELD_TIMEOUT_MS``) при неотрисовавшемся селекторе
    молча ПРОПУСКАЛ выбор и probe печатал [OK], тогда как боевой
    ``fill_response_form`` на том же DOM отказывает (``attached`` за
    ``RESUME_SELECT_TIMEOUT_MS``). Теперь обе ветки зовут один
    ``steps.ensure_resume_selected`` — семантика ожидания и текст вердикта
    совпадают по построению, а не по договорённости.
    """
    if reason := apply_steps.ensure_resume_selected(page, resume_id):
        # Боевой apply на этом же отказе отменяет отправку. probe обязан
        # сообщить тот же вердикт: молчаливое продолжение печатало бы [OK]
        # прямо перед боевым прогоном (ложное подтверждение, #139).
        return reason

    # Переиспользуем ту же функцию, что и боевой fill_response_form, вместо
    # собственной копии: копия отстала от изменений (в ней остался только
    # тоггл полной формы, которого в модалке нет), и probe молча не заполнял
    # письмо — то есть не воспроизводил боевой путь, ради чего и существует.
    # Причину отказа возвращаем наверх: probe всё равно сдампит DOM (артефакт
    # диагностики), но success=False — иначе отказ немой (fill_cover_letter
    # ничего не логирует, только возвращает строку).
    return apply_steps.fill_cover_letter(page, letter)


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
    vacancy = refresh_card(page, vacancy, cache=VacancyBodyCache())
    if has_login_form(page):
        return ProbeResult(
            vacancy,
            False,
            "Сессия недействительна: страница содержит форму входа. Выполните login.",
        )

    if not apply_steps.wait_apply_button(page):
        if reason := check_already_responded(page, vacancy):
            logger.info("[PROBE] Вакансия '%s' уже откликнута — пропускаю без дампа", vacancy.title)
            return ProbeResult(vacancy, False, reason, skipped=True)
        return ProbeResult(vacancy, False, "кнопка отклика не найдена на странице")

    navigation_result = apply_steps.navigate_to_response_form(page, vacancy.vacancy_id)
    if isinstance(navigation_result, str):
        # #350: развёрнутое предупреждение о видимости резюме — недвусмысленный
        # пропуск, без дампа (hh.ru дал определённый ответ прямо на странице).
        return ProbeResult(vacancy, False, navigation_result, skipped=True)
    if not navigation_result:
        reason = "форма отклика не отрисовалась — состояние формы не подтверждено"
        partial_ctx = ProbeContext(
            vacancy_id=vacancy.vacancy_id,
            vacancy_url=vacancy.url,
            stage="form_indeterminate",
            logs_dir=ctx_dir.logs_dir,
        )
        dump_probe_snapshot(page, partial_ctx, best_effort=True)
        logger.warning("[WARN %s] %s — %s", PAGE_STATE["indeterminate"], vacancy.title, reason)
        return ProbeResult(vacancy, success=False, reason=reason, skipped=True)
    logger.info("[PROBE] Дошёл до формы отклика, сохраняю исходный DOM")

    # Сохраняем форму до любых взаимодействий с её полями. В частности, не
    # пропускаем DOM через _fill_cover_letter_only(): его проверка resume-select
    # использует тот же якорь, который probe диагностирует в #340. Этот снимок
    # является источником истины для подтверждения актуального multi-resume
    # селектора; последующий form-дамп сохраняет прежний контракт с письмом.
    initial_ctx = ProbeContext(
        vacancy_id=vacancy.vacancy_id,
        vacancy_url=vacancy.url,
        stage="form_initial",
        logs_dir=ctx_dir.logs_dir,
    )
    initial_dump_paths = dump_probe_snapshot(page, initial_ctx)
    logger.info(
        "[PROBE] Исходный дамп формы сохранён: %s",
        ", ".join(str(path) for path in initial_dump_paths.values()),
    )
    logger.info("[PROBE] Заполняю письмо локально (без отправки)")

    # #95: detect-only. Если форма требует анкеты — НЕ заполняем и НЕ дампим вопросы,
    # возвращаем skip (record_skip делает apply-цикл, у которого есть history).
    # round-2 fix: indeterminate (границы формы не резолвились) — тоже без дампа,
    # но помечаем отдельно в логе для диагностики (не подтверждённый has_questions).
    questions = detect_questions(page)
    if questions.has_questions:
        marker = f"[WARN {PAGE_STATE['indeterminate']}]" if questions.indeterminate else "[INFO]"
        if questions.indeterminate:
            # Keep the DOM that survived a navigation/render timeout.  This is
            # diagnostic-only: a closed context may yield no artifact, while a
            # partially rendered page can still provide the evidence needed to
            # correct selectors without guessing.
            partial_ctx = ProbeContext(
                vacancy_id=vacancy.vacancy_id,
                vacancy_url=vacancy.url,
                stage="form_indeterminate",
                logs_dir=ctx_dir.logs_dir,
            )
            dump_probe_snapshot(page, partial_ctx, best_effort=True)
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
    fill_reason = _fill_cover_letter_only(page, resume_id, letter)

    # Дамп снимаем в любом случае — он и есть диагностический артефакт probe,
    # в том числе (особенно) при отказе.
    #
    # best_effort ровно на fail-пути (как на прочих failure-путях этого файла,
    # см. form_indeterminate выше): вердикт отказа уже известен, и сбой съёмки
    # артефакта (target closed/detached после неудачного взаимодействия с
    # формой) не должен подменять [FAIL] пробросом исключения — CLI тогда не
    # напечатает вердикт, а уже записанный частичный артефакт потеряет свой путь.
    # На успешном пути режим остаётся СТРОГИМ намеренно: probe возвращает
    # "probe dump saved" — заявление о готовом артефакте для сверки селекторов,
    # и без файлов это было бы ложью того же сорта, ради устранения которой
    # probe существует.
    dump_paths = dump_probe_snapshot(page, ctx_dir, best_effort=fill_reason is not None)
    if fill_reason is not None:
        # Не skipped: skipped печатается как [INFO] (вердикт hh.ru, не проблема),
        # а здесь боевой apply отказался бы отправлять — это [FAIL].
        logger.warning("[PROBE] Форма не заполнена как в боевом пути: %s", fill_reason)
        return ProbeResult(vacancy, False, fill_reason, dump_paths)
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

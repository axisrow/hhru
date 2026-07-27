"""Шаги навигации по форме отклика: ожидание кнопки, переход на форму, заполнение.

Владелец: #6. #6 правит wait'ы (таймауты, sleep, явные ожидания) здесь — изолированно
от остальных шагов. Sequence шагов в pipeline.py при этом не меняется.

Принцип ожиданий (см. #6): вместо фиксированных time.sleep и проверок count()>0
используются явные ожидания Playwright — locator.wait_for(state=..., timeout=...),
а наличие опционального элемента определяется ловом PlaywrightTimeoutError с коротким
таймаутом. Троттлинг-паузы (анти-бан) сюда не относятся — они в throttle.wait и их
трогать нельзя.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..selector_groups import vacancy_page

logger = logging.getLogger("hhru_bot.apply.steps")

APPLY_TIMEOUT_MS = 10_000
# Короткий таймаут для проверки опциональных полей формы (резюме/письмо могут
# отсутствовать — это нормально, а не ошибка). Ждать полной APPLY_TIMEOUT_MS тут
# бессмысленно: отсутствие поля детерминировано почти сразу.
OPTIONAL_FIELD_TIMEOUT_MS = 1_500
# Таймаут ожидания селектора выбора резюме. Это НЕ опциональное поле вроде
# cover-letter: если на multi-resume аккаунте селектор резюме не успел отрисоваться
# за короткий OPTIONAL_FIELD_TIMEOUT_MS (медленный JS-рендер залогиненной формы),
# выбор молча пропускается и submit отправляет резюме по умолчанию (fail-open,
# см. cycle-2 review #33). Поэтому селектор резюме ждём как обязательный элемент:
# появится — выберем нужное, детерминированно отсутствует после долгого ожидания —
# на этой странице выбора нет (одно резюме), submit разрешён.
RESUME_SELECT_TIMEOUT_MS = APPLY_TIMEOUT_MS


def wait_apply_button(page: Page) -> bool:
    """Ждёт появления кнопки отклика на странице вакансии. False — не дождались."""
    try:
        page.locator(vacancy_page.VACANCY_APPLY_BUTTON).first.wait_for(timeout=APPLY_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return False
    return True


def navigate_to_response_form(page: Page) -> None:
    """Кликает кнопку отклика и дожидается навигации на форму отклика.

    VACANCY_APPLY_BUTTON — это <a href="/applicant/vacancy_response?..."> (подтверждено
    curl-дампом реальной страницы вакансии), а не триггер модалки на этой же странице.
    Клик вызывает обычную навигацию — дожидаемся её перед поиском полей формы.

    Фиксированный sleep после навигации заменён на явное ожидание готовности DOM:
    ждём любого индикатора формы (кнопка отправки), максимум APPLY_TIMEOUT_MS.
    """
    from ..selector_groups import apply_form

    apply_button = page.locator(vacancy_page.VACANCY_APPLY_BUTTON).first
    with page.expect_navigation(wait_until="domcontentloaded", timeout=APPLY_TIMEOUT_MS):
        apply_button.click()
    # Форма рендерится после навигации — ждём её индикатор, а не слепую паузу.
    try:
        page.locator(apply_form.APPLY_SUBMIT_BUTTON).wait_for(
            state="visible", timeout=APPLY_TIMEOUT_MS
        )
    except PlaywrightTimeoutError:
        # Форма не загрузилась — fill_response_form всё равно вернёт причину отказа
        # (submit не найден), логируем для диагностики устаревшего селектора.
        logger.warning("Форма отклика не отрисовалась за %d мс", APPLY_TIMEOUT_MS)


def _is_visible(page: Page, selector: str, *, timeout_ms: int) -> bool:
    """Явное ожидание видимости опционального элемента.

    True — элемент появился и видим; False — не дождались или селектор неоднозначен,
    что для опциональных полей формы означает «на этой странице поля нет / не одно».
    Заменяет идиому ``locator.count() > 0``, которая проверяет наличие в DOM без
    гарантии видимости/готовности к взаимодействию.

    Ловим базовый Playwright Error, а не только PlaywrightTimeoutError: ``wait_for``
    в strict mode при нескольких совпадениях (например, ``APPLY_RESUME_SELECT`` —
    коллекция резюме) кидает обычный Error, и для опционального поля это не фатал —
    логика выбора конкретного резюме (``_select_resume_in_form``) разберётся с
    множественностью сама через count()/nth(). PlaywrightTimeoutError — подкласс Error,
    поэтому одна ветка ловит оба случая.
    """
    try:
        page.locator(selector).wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightError:
        return False
    return True


def fill_response_form(page: Page, resume_id: str, letter: str) -> str | None:
    """Заполняет форму отклика. Возвращает причину отказа или None, если заполнение OK."""
    from ..selector_groups import apply_form

    # Выбор резюме — особый случай: APPLY_RESUME_SELECT это коллекция (несколько резюме),
    # и wait_for в strict mode при >1 совпадении кидает Error. Поэтому «есть ли выбор
    # резюме» проверяем через .first.wait_for(state='visible') (.first снимает strict
    # mode — см. инвариант из PR #29), а затем _select_resume_in_form работает по
    # count()/nth(). Это закрывает гонку рендера: коллекция может появиться позже
    # submit-кнопки, и мгновенный count() > 0 пропустил бы выбор резюме.
    #
    # fail-closed (#33): если нужное резюме не найдено или совпадение неоднозначно —
    # НЕ отправляем (submit не кликается), возвращаем причину. Иначе отправка ушла бы
    # с резюме по умолчанию вместо запрошенного resume_id — необратимая отправка
    # неверного резюме/персональных данных работодателю.
    try:
        page.locator(apply_form.APPLY_RESUME_SELECT).first.wait_for(
            state="visible", timeout=RESUME_SELECT_TIMEOUT_MS
        )
    except PlaywrightError:
        resume_select_present = False
    else:
        resume_select_present = True
    if resume_select_present:
        # TOCTOU (cycle-2 review): селектор был видим в wait_for, но исчез к count()
        # (transient re-render/drift). count()==0 после detect — НЕ «одно резюме»
        # (там wait_for таймаутит → resume_select_present=False), а нестабильный
        # селектор на multi-resume форме. Fail-closed: отказ, не submit дефолтного.
        if page.locator(apply_form.APPLY_RESUME_SELECT).count() > 0:
            if not _select_resume_in_form(page, resume_id):
                return f"не удалось однозначно выбрать резюме '{resume_id}' в форме отклика"
        else:
            return (
                "селектор выбора резюме исчез после обнаружения — "
                "отправка отменена (нестабильная форма отклика)"
            )

    if _is_visible(
        page, apply_form.APPLY_COVER_LETTER_TOGGLE, timeout_ms=OPTIONAL_FIELD_TIMEOUT_MS
    ):
        page.locator(apply_form.APPLY_COVER_LETTER_TOGGLE).click()
        # Клик раскрывает textarea — ждём её готовности явно, а не слепую паузу.

    if _is_visible(
        page, apply_form.APPLY_COVER_LETTER_TEXTAREA, timeout_ms=OPTIONAL_FIELD_TIMEOUT_MS
    ):
        page.locator(apply_form.APPLY_COVER_LETTER_TEXTAREA).fill(letter)
        # fill() синхронно выставляет значение — дополнительное ожидание не нужно.

    # Кнопка отправки — обязательный элемент формы. Не optional: отсутствие = отказ.
    if not _is_visible(page, apply_form.APPLY_SUBMIT_BUTTON, timeout_ms=APPLY_TIMEOUT_MS):
        return "кнопка отправки отклика не найдена в форме"

    page.locator(apply_form.APPLY_SUBMIT_BUTTON).click()
    return None


def _select_resume_in_form(page: Page, resume_id: str) -> bool:
    """Выбирает резюме ``resume_id`` в форме отклика. True — выбрано, False — нет.

    Если у пользователя несколько резюме, hh.ru показывает выбор резюме в форме
    отклика. Селектор APPLY_RESUME_SELECT — приблизительный (не подтверждён
    curl-дампом, рендерится только залогиненному через JS) и наверняка потребует
    уточнения при первом реальном запуске.

    Совпадение resume_id требует **точного равенства** сегмента/значения (см.
    ``_href_matches_resume_id``): последний сегмент пути (``/resume/{id}``) или
    значение query-параметра ``resume_id``. Голая подстрока отвергается — она давала
    лжесовпадения (``RID`` внутри ``/resume/RID2``, ``?other_resume_id=RID``).

    fail-closed: ровно одна подходящая опция → кликаем, возвращаем True. Ноль или
    больше одной (неоднозначно) → возвращаем False, отправка не состоится.
    """
    from ..selector_groups import apply_form

    options = page.locator(apply_form.APPLY_RESUME_SELECT)
    count = options.count()
    matched: list[int] = []
    for i in range(count):
        href = options.nth(i).get_attribute("href") or ""
        if _href_matches_resume_id(href, resume_id):
            matched.append(i)
    if len(matched) != 1:
        # 0 — нужного резюме нет среди опций; >1 — неоднозначно. И то и другое = отказ.
        logger.warning(
            "Не удалось однозначно выбрать резюме '%s' в форме отклика "
            "(совпадений: %d) — отправка отменена",
            resume_id,
            len(matched),
        )
        return False
    options.nth(matched[0]).click()
    return True


def _href_matches_resume_id(href: str, resume_id: str) -> bool:
    """Совпадает ли ``resume_id`` с ``href`` как **полный** сегмент/значение.

    Принимает стандартные формы hh.ru: ``/resume/{id}`` (последний сегмент пути) и
    ``resume_id={id}`` (значение query-параметра). Требуется **точное равенство**
    сегмента/значения, а не вхождение подстроки — иначе ``resume_id="RID"`` ложно
    совпадает с ``/resume/RID2`` (суффикс) или ``?other_resume_id=RID``
    (похожий параметр), и кликается чужое резюме (cycle-2 review #33).
    """
    parsed = urlparse(href)
    # Путь: /resume/{id} — последний сегмент должен совпадать ровно.
    last_segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if last_segment == resume_id:
        return True
    # Query: resume_id={id} — точное значение параметра (не похоже названного).
    query = parse_qs(parsed.query)
    return query.get("resume_id", [None])[0] == resume_id

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

from ..browser import GOTO_TIMEOUT_MS
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
    # #80: потолок навигации на форму отклика — GOTO_TIMEOUT_MS (как у всех goto).
    # Двухшаговая навигация (CLAUDE.md п.4) — это сетевой запрос hh.ru, который под
    # DDoS-Guard грузится 33с+; APPLY_TIMEOUT_MS (10с) тут падал, context-wide
    # set_default_navigation_timeout перебивается явным timeout.
    with page.expect_navigation(wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS):
        # no_wait_after=True: клик НЕ ждёт навигацию своим внутренним action-timeout
        # шагом (дефолт set_default_timeout 30с, а не navigation-timeout) — иначе на
        # навигации 33с+ он упал бы через 30с раньше 90с expect_navigation. Ожидание
        # навигации полностью владеет внешний 90с waiter expect_navigation.
        apply_button.click(no_wait_after=True)
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
    # и wait_for в strict mode при >1 совпадении кидает Error. Поэтому ждём появление
    # коллекции через .first.wait_for(state='attached') (.first снимает strict mode —
    # см. инвариант из PR #29; state='attached' = наличие в DOM, НЕ видимость), а
    # решающую проверку «есть ли выбор» делаем по count(). Это закрывает и гонку
    # рендера (коллекция может появиться позже submit-кнопки), и баг cycle-4: раньше
    # .first.wait_for(state='visible') видел только первый DOM-матч — если первый
    # скрыт, а другой видим, таймаут ошибочно трактовался как «выбора нет» → submit
    # дефолтного резюме.
    #
    # fail-closed (#33): count() > 0 → выбор ОБЯЗАТЕЛЕН (_select_resume_in_form сам
    # fail-closed). Не найдено/неоднозначно — НЕ отправляем, возвращаем причину.
    # count() == 0 (детерминированное отсутствие после долгого ожидания) — выбора нет
    # (одно резюме), submit разрешён.
    try:
        page.locator(apply_form.APPLY_RESUME_SELECT).first.wait_for(
            state="attached", timeout=RESUME_SELECT_TIMEOUT_MS
        )
    except PlaywrightTimeoutError:
        # Легитимное «селектор не появился в DOM за долгий таймаут» — выбора на этой
        # странице нет (одно резюме, happy path). submit разрешён ниже.
        options_count = 0
    except PlaywrightError:
        # Cycle-5 review: НЕ маскировать не-timeout ошибки (runtime/selector failure)
        # под «выбора нет» — это пропустило бы count() и выбор → submit дефолтного
        # резюме. Только PlaywrightTimeoutError = легитимное отсутствие; прочие
        # PlaywrightError — аномалия, fail-closed отказ.
        return (
            "ошибка при проверке выбора резюме в форме отклика — "
            "отправка отменена (нестабильная страница)"
        )
    else:
        options_count = page.locator(apply_form.APPLY_RESUME_SELECT).count()
        if options_count == 0:
            # TOCTOU (cycle-2): был прикреплён в wait_for, но исчез к count()
            # (transient re-render/drift). Это НЕ «одно резюме» (там wait_for таймаутит
            # и мы в ветке except выше), а нестабильный селектор. Fail-closed: отказ.
            return (
                "селектор выбора резюме исчез после обнаружения — "
                "отправка отменена (нестабильная форма отклика)"
            )
    if options_count > 0:
        # Выбор есть (multi-resume) — выбираем нужное; _select_resume_in_form fail-closed.
        if not _select_resume_in_form(page, resume_id):
            return f"не удалось однозначно выбрать резюме '{resume_id}' в форме отклика"

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

    # Identity-bound выбор (cycle-3 review): НЕ кликаем по сохранённому индексу —
    # Playwright резолвит nth(i) против текущего DOM в момент действия, и если JS
    # переупорядочил/вставил опции между сканом и кликом, тот же индекс = другое
    # резюме. Вместо этого: находим единственный совпадающий href, а перед кликом
    # пере-сканируем опции по этому href и требуем строгую уникальность. Любой
    # drift (переупорядочивание, исчезновение, раздвоение) между scan и click → отказ.
    options = page.locator(apply_form.APPLY_RESUME_SELECT)
    count = options.count()
    matched_hrefs: list[str] = []
    for i in range(count):
        href = options.nth(i).get_attribute("href") or ""
        if _href_matches_resume_id(href, resume_id):
            matched_hrefs.append(href)
    if len(matched_hrefs) != 1:
        # 0 — нужного резюме нет среди опций; >1 — неоднозначно. И то и другое = отказ.
        logger.warning(
            "Не удалось однозначно выбрать резюме '%s' в форме отклика "
            "(совпадений: %d) — отправка отменена",
            resume_id,
            len(matched_hrefs),
        )
        return False
    target_href = matched_hrefs[0]
    # Клик по strict href-локатору, БЕЗ конвертации обратно в индекс (cycle-3 review):
    # nth(i).click() re-resolves индекс в момент действия — любой scan→click gap даёт
    # TOCTOU (JS reorder/insert → тот же индекс = другое резюме). Вместо этого кликаем
    # строгий локатор, привязанный к точному href: Playwright сам резолвит его в момент
    # click() и кидает Error (strict mode) при != 1 совпадении — дубль/исчезновение/
    # переупорядочивание между scan и click ловятся как отказ. href — стабильная
    # identity резюме на hh.ru, не позиция в коллекции.
    try:
        page.locator(_resume_option_by_href_selector(target_href)).click()
    except PlaywrightError as exc:
        # strict-mode violation (>1 совпадение), element not found (исчез) или detach —
        # любое из этого = не удалось гарантированно кликнуть нужное резюме → отказ.
        logger.warning(
            "Резюме '%s' (href=%s) не подтверждено при клике (%s) — отправка отменена",
            resume_id,
            target_href,
            exc,
        )
        return False
    return True


def _resume_option_by_href_selector(href: str) -> str:
    """CSS-селектор опции резюме по точному href, для strict-клика в момент действия.

    Совмещает APPLY_RESUME_SELECT с фильтром по атрибуту href. Quote обратными кавычками
    для CSS-безопасности (значение берётся из DOM страницы, не из ввода пользователя).
    """
    from ..selector_groups import apply_form

    escaped = href.replace("\\", "\\\\").replace("'", "\\'")
    return f"{apply_form.APPLY_RESUME_SELECT}[href='{escaped}']"


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

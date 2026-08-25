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

import json
import logging
import random

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..logging_setup import LOG_DIR
from ..selector_groups import vacancy_page
from .blockers import PostClickBlocker, handle_post_click_blockers, raise_if_post_submit_limit

logger = logging.getLogger("hhru_bot.apply.steps")

APPLY_TIMEOUT_MS = 10_000
# The response page may be rendered as a SPA modal, so waiting for its URL can
# block for the global 90s navigation timeout even when the form is ready.
RESPONSE_READY_MIN_TIMEOUT_MS = 5_000
RESPONSE_READY_MAX_TIMEOUT_MS = 15_000
# Короткий таймаут для проверки опциональных полей формы (резюме/письмо могут
# отсутствовать — это нормально, а не ошибка). Ждать полной APPLY_TIMEOUT_MS тут
# бессмысленно: отсутствие поля детерминировано почти сразу.
OPTIONAL_FIELD_TIMEOUT_MS = 1_500
# The response modal is rendered on the vacancy page in this failure mode, so
# do not spend the full navigation timeout waiting for a URL that will never
# change.
RESUME_WARNING_TIMEOUT_MS = 1_500
# Таймаут ожидания селектора выбора резюме. Это НЕ опциональное поле вроде
# cover-letter: если на multi-resume аккаунте селектор резюме не успел отрисоваться
# за короткий OPTIONAL_FIELD_TIMEOUT_MS (медленный JS-рендер залогиненной формы),
# выбор молча пропускается и submit отправляет резюме по умолчанию (fail-open,
# см. cycle-2 review #33). Поэтому селектор резюме ждём как обязательный элемент.
RESUME_SELECT_TIMEOUT_MS = APPLY_TIMEOUT_MS
# Закрытие раскрытого списка резюме после выбора — локальная CSS/React-анимация
# без сетевого запроса, поэтому таймаут заметно короче RESUME_SELECT_TIMEOUT_MS
# (там ждём рендер опций). Отдельная константа, а не переиспользование: иначе
# каждая вакансия с незакрывшимся dropdown стоила бы лишние 10с.
RESUME_DROPDOWN_CLOSE_TIMEOUT_MS = 3_000


class SubmitClickUncertain(Exception):
    """Submit-клик отправлен/отправляется, но Playwright бросил исключение (#176).

    Кнопка отправки — последний шаг fill_response_form; исключение в момент
    клика (navigation timeout после POST, target closed при редиректе) не
    означает, что отклик НЕ ушёл. steps сознательно НЕ решает, выполнено ли
    действие (это ответственность pipeline: acted/uncertain/запись в history) —
    он лишь честно маркирует точку «клик мог уйти», отличая её от обычных
    отказов до submit (те возвращаются причиной, не исключением).
    """

    def __init__(self, cause: PlaywrightError):
        super().__init__(f"submit-клик упал с исключением ({cause})")
        self.playwright_error = cause


def _dump_navigation_diagnostics(
    page: Page, stage: str, vacancy_id: str | None = None, run_id: str | None = None
) -> None:
    """Best-effort screenshot and HTML dump after an indeterminate form wait.

    This is deliberately diagnostic-only: a failure to capture either artifact
    must not change the apply result or interrupt the remaining vacancies.
    Same idempotent-by-name convention as ``probe.dump_probe_snapshot``:
    the filename is keyed by ``stage`` (plus ``vacancy_id`` when the caller has
    one) so a repeated failure on the SAME vacancy overwrites its own previous
    dump instead of accumulating a new pair of files per retry, while distinct
    vacancies in one apply run keep separate artifacts (cycle-review round 2:
    the pure by-stage key from round 1 fixed unbounded growth but lost the
    per-vacancy context #192 needs to tell a slow DDoS-Guard load apart from a
    drifted selector on a specific vacancy).
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        name = f"apply_{stage}" if vacancy_id is None else f"apply_{vacancy_id}_{stage}"
        prefix = LOG_DIR / name
        screenshot_path = prefix.with_suffix(".png")
        html_path = prefix.with_suffix(".html")
        html_metadata_path = html_path.with_suffix(".json")
        html_metadata_path.unlink(missing_ok=True)
        screenshot_path.write_bytes(page.screenshot(full_page=True))
        html_path.write_text(page.content(), encoding="utf-8")
    except (OSError, PlaywrightError) as exc:
        logger.warning("Диагностический дамп после %s недоступен: %s", stage, exc)
        return
    logger.info(
        "Диагностический дамп после %s сохранён: %s, %s",
        stage,
        screenshot_path,
        html_path,
    )
    if html_path.exists():
        try:
            html_metadata_path.write_text(
                json.dumps(
                    {
                        "producer": "hhru_bot.apply.steps",
                        "run_id": run_id,
                        "artifact": html_path.name,
                        "stage": stage,
                        "vacancy_id": vacancy_id,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Метаданные диагностического дампа недоступны: %s", exc)


def wait_apply_button(page: Page, *, timeout_ms: int = APPLY_TIMEOUT_MS) -> bool:
    """Ждёт появления кнопки отклика на странице вакансии. False — не дождались.

    #226 cycle-review: ждём кнопку ИЛИ already-responded-маркеры одним локатором
    (Locator.or_), чтобы вакансия с уже существующим откликом распознавалась сразу,
    а не после полного APPLY_TIMEOUT_MS — иначе батч из многих таких вакансий
    последовательно копит по 10с задержки на каждую. Возвращаем True только когда
    появилась именно кнопка отклика; already-responded случай — False, дальше его
    различает check_already_responded() в pipeline/probe.
    """
    apply_button = page.locator(vacancy_page.VACANCY_APPLY_BUTTON)
    already_responded = page.locator(vacancy_page.VACANCY_ALREADY_RESPONDED_AGAIN).or_(
        page.locator(vacancy_page.VACANCY_ALREADY_RESPONDED_CHAT)
    )
    try:
        apply_button.or_(already_responded).first.wait_for(timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return False
    return apply_button.count() > 0


def _hidden_resume_warning_is_expanded(page: Page) -> bool:
    """Return whether hh.ru has expanded the resume-visibility warning."""
    try:
        page.wait_for_function(
            """selector => [...document.querySelectorAll(selector)].some(el => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && style.maxHeight !== '0px';
            })""",
            arg=vacancy_page.VACANCY_HIDDEN_RESUME_WARNING,
            timeout=RESUME_WARNING_TIMEOUT_MS,
        )
    except (PlaywrightError, AttributeError):
        return False
    return True


def navigate_to_response_form(
    page: Page,
    vacancy_id: str | None = None,
    *,
    form_timeout_ms: int | None = None,
    dump_diagnostics: bool = True,
    allow_relocation: bool = False,
    run_id: str | None = None,
) -> str | bool | PostClickBlocker:
    """Кликает кнопку отклика и дожидается навигации на форму отклика.

    Возвращает:
    - ``str`` — причина недвусмысленного, неисполнимого пропуска (#350: развёрнутое
      предупреждение о видимости резюме прямо на странице вакансии вместо перехода
      на форму отклика);
    - ``True`` — submit-кнопка формы стала видимой;
    - ``False`` — рендер формы не подтверждён (навигация/рендер не удались).

    Это различие важно: pipeline не должен запускать детекцию вопросов для формы,
    рендер которой не подтверждён, и не должен ждать полный таймаут навигации,
    если hh.ru уже дал определённый ответ прямо на странице вакансии.

    ``vacancy_id`` (опционален — probe.py его тоже передаёт, но не обязан) даёт
    диагностическим дампам таймаутов отдельное имя на вакансию, чтобы не терять
    контекст при массовом apply-прогоне (см. ``_dump_navigation_diagnostics``).

    VACANCY_APPLY_BUTTON — это <a href="/applicant/vacancy_response?..."> (подтверждено
    curl-дампом реальной страницы вакансии), а не триггер модалки на этой же странице.
    Клик вызывает обычную навигацию — дожидаемся её перед поиском полей формы.

    Фиксированный sleep после навигации заменён на явное ожидание готовности DOM:
    ждём любого индикатора формы (кнопка отправки). ``form_timeout_ms`` управляет
    этим ожиданием напрямую — если не задан явно (``None``), таймаут выбирается
    случайно в диапазоне ``RESPONSE_READY_MIN_TIMEOUT_MS``..``RESPONSE_READY_MAX_TIMEOUT_MS``
    для обычного apply-цикла; probe/questionnaire передают явное значение для
    своих быстрых режимов (см. ``FAST_FORM_TIMEOUT_MS`` в questionnaire.py).

    #179: раньше ожидание было ``page.expect_navigation(wait_until="domcontentloaded")``.
    Живая диагностика (боевой аккаунт, vacancy_id 136221532) показала: кнопка
    отклика сменилась на "уже откликнулись" (submit реально ушёл на hh.ru), но
    ``expect_navigation`` всё равно упал по TimeoutError 90с — гипотеза: переход
    на ``/applicant/vacancy_response`` у залогиненного пользователя рендерится
    как SPA same-document навигация (``history.pushState``), а не полноценная
    перезагрузка документа, и ``domcontentloaded`` в этом случае не наступает,
    хотя ``page.url`` меняется.

    ``page.wait_for_url()`` реализован через тот же ``expect_navigation`` внутри
    (``playwright._impl._frame.Frame.wait_for_url``) и БЕЗ явного ``wait_until``
    дефолтится на ``"load"`` — это строже, а не мягче ``domcontentloaded``, и не
    решило бы проблему. ``wait_until="commit"`` — единственное значение, которое
    не ждёт lifecycle-событие документа вообще (срабатывает по первому байту
    ответа/навигационному событию), поэтому здесь передан явно.

    #179 (code-review): и клик, и ожидание URL оборачиваются в try/except
    ``PlaywrightError`` (не только ``PlaywrightTimeoutError`` — non-timeout
    ошибки вроде «page/context closed» тоже возможны и раньше пробрасывались
    бы наверх необработанными). Это НЕ то же самое, что #176
    ``SubmitClickUncertain``: там ошибка происходит вокруг submit-клика,
    последнего необратимого шага формы, и pipeline обязан трактовать её как
    «действие могло уйти» (``acted=True``). Здесь клик по кнопке ОТКЛИКА
    (переход на форму) — до заполнения и до submit; аналогично прочим ранним
    отказам (#163) он не означает следа на hh.ru, поэтому исключение просто
    логируется и разбор продолжается через return, а не пробрасывается —
    fill_response_form (через отсутствие APPLY_SUBMIT_BUTTON) и
    detect_questions сами вернут корректный fail/skip для этой вакансии, не
    обрывая цикл apply по остальным вакансиям/резюме (иначе один сбойный клик
    на первой же вакансии останавливает весь прогон — регрессия #163/#176).
    """
    from ..selector_groups import apply_form

    apply_button = page.locator(vacancy_page.VACANCY_APPLY_BUTTON).first
    # #80/#179: потолок навигации на форму отклика — GOTO_TIMEOUT_MS (как у всех
    # goto). Двухшаговая навигация (CLAUDE.md п.4) — это сетевой запрос hh.ru,
    # который под DDoS-Guard грузится 33с+; APPLY_TIMEOUT_MS (10с) тут падал.
    try:
        apply_button.click(no_wait_after=True)
    except PlaywrightError as exc:
        # Клик по apply-кнопке — до submit, ранний отказ (#163): на hh.ru следа
        # нет, не крашим цикл откликов необработанным исключением.
        logger.warning("Клик по кнопке отклика упал с ошибкой (%s) — вакансия пропущена", exc)
        return False
    blocker = handle_post_click_blockers(page, allow_relocation=allow_relocation)
    if blocker is not None:
        return blocker
    # #350: some accounts receive a modal on the vacancy URL instead of a form
    # navigation.  Its expanded warning is a definitive, non-actionable skip.
    if _hidden_resume_warning_is_expanded(page):
        reason = "видимость резюме недостаточна для отклика"
        logger.info("Вакансия пропущена: %s", reason)
        return reason
    # URL не является обязательным: hh.ru может оставить нас на vacancy URL и
    # открыть форму модалкой. В обычном apply-цикле используем bounded jitter;
    # probe/questionnaire могут передать явный form_timeout_ms для своих
    # быстрых режимов — `is not None`, а не truthy-проверка: 0 валидное
    # значение таймаута в этом файле (см. render_timeout_ms=0 ниже).
    ready_timeout_ms = (
        form_timeout_ms
        if form_timeout_ms is not None
        else random.randint(RESPONSE_READY_MIN_TIMEOUT_MS, RESPONSE_READY_MAX_TIMEOUT_MS)
    )
    logger.debug("Ожидание формы отклика: %d мс", ready_timeout_ms)
    blocker = handle_post_click_blockers(
        page,
        allow_relocation=allow_relocation,
        render_timeout_ms=0,
        post_navigation=True,
    )
    if blocker is not None:
        return blocker
    # Форма рендерится после клика — ждём её индикатор, а не URL и не sleep.
    try:
        page.locator(apply_form.APPLY_SUBMIT_BUTTON).wait_for(
            state="visible", timeout=ready_timeout_ms
        )
    except PlaywrightError as exc:
        if dump_diagnostics:
            _dump_navigation_diagnostics(page, "form_timeout", vacancy_id, run_id)
        # Форма не загрузилась — сообщаем pipeline отдельно от детекции вопросов,
        # чтобы таймаут рендера не выглядел как неверная граница <form>.
        logger.warning("Форма отклика не отрисовалась (%s)", exc)
        return False
    return True


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


def fill_cover_letter(page: Page, letter: str) -> str | None:
    """Заполняет сопроводительное письмо в форме отклика. None — заполнено.

    Возвращает причину отказа, если поля письма нет: письмо — смысл этого
    инструмента, а не опциональная деталь, поэтому его отсутствие fail-closed
    останавливает отправку (до submit, следа на hh.ru нет).

    Отдельная функция, а не инлайн в fill_response_form: probe (#8) заполняет
    письмо тем же способом, но без submit, и раньше держал СВОЮ копию этой
    логики. Копия отстала — в ней остался только тоггл полной формы, которого
    в модалке не существует, поэтому probe молча не заполнял письмо и не
    воспроизводил боевой путь. Переиспользование вместо дубля — принцип проекта.
    """
    from ..selector_groups import apply_form

    # Письмо адресуется в ОБОИХ shape формы (см. apply_form.py): модалка
    # (add-cover-letter + …-popup-form-letter-input) и полная страница
    # (vacancy-response-letter-toggle + vacancy-response-form-letter-input).
    # В каждом shape совпадает ровно один операнд, поэтому порядок or_ не важен.
    letter_toggle = page.locator(apply_form.APPLY_COVER_LETTER_TOGGLE).or_(
        page.locator(apply_form.APPLY_COVER_LETTER_TOGGLE_POPUP)
    )
    try:
        letter_toggle.first.wait_for(state="visible", timeout=OPTIONAL_FIELD_TIMEOUT_MS)
        letter_toggle.first.click()
        # Клик раскрывает textarea — её готовности ждём явно ниже, а не слепой паузой.
    except PlaywrightError:
        # Отсутствие тоггла легитимно: hh.ru может отрендерить textarea уже
        # развёрнутой (боевой случай 136417846). Решает не тоггл, а наличие
        # самой textarea — проверка ниже.
        pass

    letter_input = page.locator(apply_form.APPLY_COVER_LETTER_TEXTAREA).or_(
        page.locator(apply_form.APPLY_COVER_LETTER_TEXTAREA_FORM)
    )
    try:
        # CLAUDE.md: клик по тогглу запускает React-рендер — явное ожидание
        # видимости перед fill, а не count()/слепая пауза.
        letter_input.first.wait_for(state="visible", timeout=APPLY_TIMEOUT_MS)
    except PlaywrightError:
        # По SSR topicList[].hasResponseLetter из 18 откликов аккаунта 16 ушли
        # пустыми именно потому, что этот отказ раньше был молчаливым пропуском.
        return (
            "поле сопроводительного письма не найдено в форме отклика — "
            "отправка отменена (отклик без письма не отправляем)"
        )
    letter_input.first.fill(letter)
    # fill() синхронно выставляет значение — дополнительное ожидание не нужно.
    return None


def ensure_resume_selected(page: Page, resume_id: str) -> str | None:
    """Подтверждает выбор резюме в форме отклика. None — подтверждено, иначе причина отказа.

    Единственная реализация этого шага для ОБОИХ путей — боевого
    (``fill_response_form``) и диагностического (``probe._fill_cover_letter_only``).
    Отдельная функция, а не инлайн: probe держал свою копию проверки с другой
    семантикой (``visible`` за ``OPTIONAL_FIELD_TIMEOUT_MS`` вместо ``attached``
    за ``RESUME_SELECT_TIMEOUT_MS``) и при неотрисовавшемся селекторе МОЛЧА
    пропускал выбор → печатал [OK] там, где боевой apply отказался бы отправлять.
    Копия логики — та самая причина, по которой probe уже дважды расходился с
    боевым путём; переиспользование вместо дубля — принцип проекта.
    """
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
    # fail-closed (#33): отсутствие подтверждённого контрола выбора нельзя принять
    # за «на аккаунте одно резюме». Таймаут означает, что якорь не найден, поэтому
    # отправка запрещена до подтверждения актуального DOM-селектора на живой форме.
    #
    # живой DOM (F12) показал, что APPLY_RESUME_SELECT
    # (`resume-title`) присутствует ВСЕГДА — и на single-, и на multi-resume
    # аккаунте (это текст текущего выбора, не признак множественности) —
    # поэтому прежнее ветвление «count()>0 → multi-resume» было в принципе
    # неверным допущением, отдельным от самого факта смены селектора.
    # _select_resume_in_form() теперь сама решает по своему результату:
    # найдена опция с нужным resume_id → выбрана; не найдена (в том числе на
    # single-resume аккаунте, где после клика опций вообще нет) → fail-closed
    # отказ, тот же принцип #33 — отсутствие подтверждённого выбора нельзя
    # тихо принять за «резюме по умолчанию верное».
    try:
        page.locator(apply_form.APPLY_RESUME_SELECT).first.wait_for(
            state="attached", timeout=RESUME_SELECT_TIMEOUT_MS
        )
    except PlaywrightTimeoutError:
        return (
            "селектор выбора резюме не найден в форме отклика — "
            "отправка отменена (резюме не подтверждено)"
        )
    except PlaywrightError:
        # Cycle-5 review: НЕ маскировать не-timeout ошибки (runtime/selector failure)
        # под «выбора нет» — это пропустило бы выбор → submit дефолтного резюме.
        # Только PlaywrightTimeoutError = легитимное отсутствие; прочие
        # PlaywrightError — аномалия, fail-closed отказ.
        return (
            "ошибка при проверке выбора резюме в форме отклика — "
            "отправка отменена (нестабильная страница)"
        )
    if not _select_resume_in_form(page, resume_id):
        return f"не удалось однозначно выбрать резюме '{resume_id}' в форме отклика"
    return None


def fill_response_form(page: Page, resume_id: str, letter: str) -> str | None:
    """Заполняет форму отклика. Возвращает причину отказа или None, если заполнение OK."""
    from ..selector_groups import apply_form

    if reason := ensure_resume_selected(page, resume_id):
        return reason

    if reason := fill_cover_letter(page, letter):
        return reason

    # Кнопка отправки — обязательный элемент формы. Не optional: отсутствие = отказ.
    if not _is_visible(page, apply_form.APPLY_SUBMIT_BUTTON, timeout_ms=APPLY_TIMEOUT_MS):
        return "кнопка отправки отклика не найдена в форме"

    # #176: submit — единственное необратимое действие формы. Playwright может
    # бросить исключение уже после того, как POST отклика ушёл (navigation
    # timeout, target closed при редиректе) — глотать его как обычный отказ
    # нельзя (это «отправки не было», а мы не знаем), пробрасывать сырым тоже
    # (pipeline упадёт до записи в history). Маркируем SubmitClickUncertain —
    # pipeline трактует его fail-closed как acted+uncertain.
    try:
        page.locator(apply_form.APPLY_SUBMIT_BUTTON).click()
    except PlaywrightError as exc:
        logger.warning("Submit-клик упал с исключением (%s) — отправка могла уйти", exc)
        raise SubmitClickUncertain(exc) from exc
    raise_if_post_submit_limit(page)
    return None


def _select_resume_in_form(page: Page, resume_id: str) -> bool:
    """Выбирает резюме ``resume_id`` в форме отклика. True — выбрано, False — нет.

    сверено живым DOM (F12, /applicant/vacancy_response,
    multi-resume аккаунт). APPLY_RESUME_SELECT (`resume-title`) — это
    СВЁРНУТЫЙ ТРИГГЕР (текст текущего выбора), а не коллекция опций и не
    ссылка — на форме нет атрибута ``href`` вообще, прежнее сопоставление
    по href было в принципе нерабочим (не просто неточным). Клик по
    триггеру раскрывает опции; каждая опция — ``<label
    data-qa="magritte-select-option-{resume_id}" role="option">``, где
    ``resume_id`` уже есть готовым идентификатором в самом ``data-qa``
    (тот же хвост, что в ``resume_url`` конфига) — искать/сопоставлять
    ничего не нужно, локатор адресует нужную опцию напрямую.

    Identity-bound выбор (cycle-3 review принцип сохранён): локатор строится
    по ``resume_id`` в data-qa, а не по сохранённому индексу — Playwright
    резолвит его строго в момент click(), поэтому переупорядочивание/
    исчезновение опции между открытием и кликом ловится как отказ (strict
    mode / not found), а не молча кликает не то резюме.

    fail-closed (#33 инвариант сохранён): ровно одна опция с этим resume_id
    после раскрытия → кликаем, True. Ноль (резюме не в списке форм для этой
    вакансии/аккаунта) или больше одной (дубль/аномалия разметки) → False,
    отправка не состоится.
    """
    from ..selector_groups import apply_form

    # Клик по триггеру раскрывает список — событие всплывает от resume-title
    # до обработчика на предке (сверено live: dispatchEvent на самом
    # data-qa-элементе открывает dropdown), явный xpath-обход предка не нужен.
    try:
        page.locator(apply_form.APPLY_RESUME_SELECT).first.click()
    except PlaywrightError as exc:
        logger.warning(
            "Не удалось открыть выбор резюме в форме отклика (%s) — отправка отменена", exc
        )
        return False

    option = page.locator(f"[data-qa='{apply_form.APPLY_RESUME_OPTION_PREFIX}{resume_id}']")
    # CLAUDE.md паттерн: клик, запускающий React-рендер (раскрытие dropdown),
    # требует явного wait_for(state="visible") ПЕРЕД первой строгой проверкой —
    # иначе count() может увидеть пустой список в момент между click() и
    # рендером опций и ошибочно списать это на «резюме нет среди опций».
    try:
        option.wait_for(state="visible", timeout=RESUME_SELECT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        # Легитимный fail-closed отказ (#33): опции с этим resume_id нет —
        # либо аккаунт single-resume (dropdown вообще не содержит эту форму
        # выбора), либо resume_id не входит в список резюме для этой вакансии.
        logger.warning(
            "Резюме '%s' не найдено среди опций формы отклика — отправка отменена", resume_id
        )
        return False
    except PlaywrightError as exc:
        logger.warning(
            "Ошибка при ожидании опции резюме '%s' (%s) — отправка отменена", resume_id, exc
        )
        return False

    if option.count() != 1:
        # >1 совпадение после подтверждённой видимости — аномалия разметки
        # (дубль data-qa), не «резюме не найдено». И то, и другое — отказ.
        logger.warning(
            "Резюме '%s' неоднозначно в форме отклика (совпадений: %d) — отправка отменена",
            resume_id,
            option.count(),
        )
        return False

    try:
        option.click()
    except PlaywrightError as exc:
        # strict-mode violation, element not found (исчез) или detach между
        # wait_for и click() — не удалось гарантированно кликнуть нужное
        # резюме → отказ, а не тихий submit дефолтного резюме.
        logger.warning(
            "Резюме '%s' не подтверждено при клике (%s) — отправка отменена", resume_id, exc
        )
        return False

    # Live DOM 2026-08-25: штатный option-click сам закрывает drop-base. Поэтому
    # безусловный клик по toggle после выбора снова ОТКРЫВАЕТ список и блокирует
    # submit. Сначала принимаем штатное автозакрытие; toggle нужен только как
    # fallback для наблюдавшегося ранее варианта, где React оставлял панель
    # открытой. Escape не используем: в модалке он может закрыть всю форму.
    dropdown = page.locator(apply_form.APPLY_RESUME_DROPDOWN)
    try:
        dropdown.wait_for(state="hidden", timeout=RESUME_DROPDOWN_CLOSE_TIMEOUT_MS)
        return True
    except PlaywrightTimeoutError:
        # Панель не закрылась штатно — пробуем подтверждённый live DOM toggle.
        pass
    except PlaywrightError as exc:
        logger.warning(
            "Не удалось проверить закрытие списка выбора резюме после выбора '%s' (%s) — "
            "отправка отменена",
            resume_id,
            exc,
        )
        return False

    try:
        page.locator(apply_form.APPLY_RESUME_TOGGLE).click()
    except PlaywrightError as exc:
        logger.warning(
            "Не удалось закрыть список выбора резюме после выбора '%s' (%s) — отправка отменена",
            resume_id,
            exc,
        )
        return False
    try:
        dropdown.wait_for(state="hidden", timeout=RESUME_DROPDOWN_CLOSE_TIMEOUT_MS)
    except PlaywrightError as exc:
        # fail-closed: submit при открытой панели гарантированно упрётся в
        # оверлей. Честный отказ ДО клика лучше ложного uncertain после него.
        logger.warning(
            "Список выбора резюме не закрылся после выбора '%s' (%s) — отправка отменена "
            "(открытая панель перекрыла бы кнопку отправки)",
            resume_id,
            exc,
        )
        return False
    return True

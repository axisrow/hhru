"""Загрузка фото в резюме через file-input микрофроненда resumePhotoViewer.

Механизм подтверждён живым боевым прогоном 2026-09-02 (резюме «Столяр,
плотник», фото загружено и назначено; дампы ``photo_flow_*`` /
``photo_assign_final_*`` в data/logs):

1. **Гидратация обязательна.** Скрытые ``input[type=file]`` присутствуют в
   SSR-разметке сразу, но БЕЗ React-привязки (нет ``__reactProps$``) — их
   change-событие никто не обрабатывает: файл принимается, upload не
   начинается (живая диагностика explore_photo_upload_net). Микрофронтенд
   гидратируется лениво: контейнер нужно проскроллить в вьюпорт, после чего
   ``__reactProps$`` появляется за ~2-4с. Ждём именно появление React-привязки,
   не «видимость» инпута (инвариант «visible != гидратирован»).
2. **Поток после передачи файла трёхшаговый:** crop-редактор
   (``photo-editor-apply`` — мутирующий клик, запускает upload) -> модалка
   «Все загруженные фото» (``photo-viewer-action-assign-current`` — назначает
   фото на это резюме) -> ``img`` в блоке аватара (ОПТИМИСТИЧНЫЙ маркер:
   рисуется до консолидации assign на сервере) -> перезагрузка страницы и
   повторная проверка img — единственное подтверждение успеха (readback,
   #955).
3. Клик по ``resume-avatar-edit-button`` сам по себе файл не загружает —
   он только открывает модалку вьювера УЖЕ гидратированного микрофроненда.

``set_input_files`` на собственный form-control страницы — штатный
UI-механизм, не внутренний API hh.ru (граница браузерных действий CLAUDE.md
не нарушается: прямой ``page.request.*`` не используется).

Анти-бот: серия быстрых headless-сессий ловит DDOS-GUARD captcha (наблюдено
2026-09-02); повтор — после паузы, лучше headed.
"""

from __future__ import annotations

import stat as stat_module
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import (
    dismiss_cookie_banner,
    dump_page_html,
    goto_hh,
    require_authenticated_page,
    wait_for_react_hydration,
)
from .selector_groups.resume_photo import (
    PHOTO_ACCEPTED_EXT,
    RESUME_AVATAR_BLOCK,
    RESUME_AVATAR_IMAGE,
    RESUME_AVATAR_PLACEHOLDER,
    RESUME_PHOTO_EDITOR_APPLY,
    RESUME_PHOTO_FILE_INPUT,
    RESUME_PHOTO_MFE_CONTAINER,
    RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
    RESUME_PHOTO_VIEWER_LIMIT,
)

# Живая подсказка hh.ru про лимиты размера в дампе не встретилась — лимит
# взят консервативно (типичный потолок загрузок hh.ru), до подтверждения.
MAX_PHOTO_BYTES = 5 * 1024 * 1024

# «commit не значит отрисовано» и upload+assign небыстры: каждый следующий
# экран ждётся с честным дедлайном, ранний отказ здесь — ложный uncertain.
_CONFIRM_TIMEOUT_MS = 30_000

# Ленивая гидратация микрофроненда после скролла: reactProps появлялся за
# 2-4с, берём запас. Аватар отрисовывается SPA — отдельный inline-таймаут.
_HYDRATION_TIMEOUT_MS = 20_000
_AVATAR_WAIT_TIMEOUT_MS = 15_000
_EDITOR_WAIT_TIMEOUT_MS = 15_000
_ASSIGN_WAIT_TIMEOUT_MS = 30_000
# Пауза после появления модалки назначения до клика assign: анимация
# overlay'а перехватывает клики (боевой кейс 2026-09-03 — uncertain при
# открытой модалке).
_ASSIGN_MODAL_SETTLE_MS = 2_500
# Явный скролл assign-кнопки перед кликом (боевой кейс 2026-09-04:
# кнопка stable, но вне вьюпорта — 57 ретраев клика вхолостую).
_ASSIGN_SCROLL_TIMEOUT_MS = 5_000
# Ленивая гидратация чанка модалки назначения: активация ДО неё теряется
# молча (боевой прогон 8, 2026-09-04: focus дошёл до кнопки, Enter и
# dispatch_event ушли в пустоту — hasPhoto остался false). Ждём React-
# привязку на самой кнопке перед фолбэк-активацией.
_ASSIGN_HYDRATION_TIMEOUT_MS = 15_000
# Пауза после появления маркера до readback-перезагрузки: маркер рисуется
# оптимистично, до консолидации assign-запроса на сервере (боевой кейс
# 2026-09-02: img на месте, hasPhoto на сервере остался false). Пауза не
# доказательство, а снижение частоты ложного uncertain: если assign не
# уложился, readback после перезагрузки честно вернёт uncertain.
_ASSIGN_SETTLE_MS = 5_000
# Readback (#955): после видимости блока аватара img может быть ещё не
# дорендерен (SPA вставляет асинхронно) — отсутствие подтверждаем ТОЛЬКО
# явным плейсхолдером «фото нет», в пределах этого бюджета.
_READBACK_CONFIRM_TIMEOUT_MS = 15_000
_READBACK_POLL_MS = 500

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
)

# Viewport контекста upload-photo. Модалка назначения — Magritte MediaViewer
# (aria-modal, z-index 1170), assign-кнопка — иконочная кнопка в правом слоте
# её шапки; при дефолтных 1366x900 шапка стабильно «outside of the viewport»,
# скролл блокирует overflow контейнера (4 боевых прогона 2026-09-04, дампы
# photo_assign_click_uncertain_*). Высокий viewport — легитимный сценарий
# большого монитора: шапка помещается целиком при любом положении модалки.
PHOTO_VIEWPORT = {"width": 1366, "height": 2400}


@dataclass(frozen=True)
class PhotoFile:
    path: Path
    size_bytes: int  # снапшот на момент валидации, для плана dry-run
    kind: str  # "jpeg" | "png" — по магическим байтам, не по расширению


@dataclass
class UploadPhotoResult:
    success: bool = False
    reason: str = ""
    uncertain: bool = False
    photo_present: bool | None = None  # None = состояние страницы не подтверждено


def validate_photo(path: Path) -> PhotoFile:
    """Fail-closed валидация файла без браузера; ValueError с причиной."""
    try:
        st = path.stat()
    except OSError:
        raise ValueError(f"файл не найден: {path}") from None
    if stat_module.S_ISDIR(st.st_mode):
        raise ValueError(f"это директория, а не файл: {path}")
    suffix = path.suffix.lower()
    if suffix not in PHOTO_ACCEPTED_EXT:
        joined = ", ".join(PHOTO_ACCEPTED_EXT)
        raise ValueError(f"расширение {suffix!r} не поддерживается (допустимо: {joined})")
    if st.st_size == 0:
        raise ValueError("файл пустой")
    if st.st_size > MAX_PHOTO_BYTES:
        raise ValueError(f"файл больше {MAX_PHOTO_BYTES} байт ({st.st_size})")
    with path.open("rb") as fh:
        head = fh.read(16)
    kind = next((name for magic, name in _MAGIC if head.startswith(magic)), None)
    if kind is None:
        raise ValueError(
            "содержимое не похоже на JPEG/PNG по магическим байтам "
            "(например, сохранённая HTML-страница с расширением .jpg)"
        )
    return PhotoFile(path=path, size_bytes=st.st_size, kind=kind)


def photo_upload_plan(photo: PhotoFile, resume_id: str) -> str:
    return (
        f"резюме {resume_id}: фото {photo.path} ({photo.size_bytes} байт, "
        f"{photo.kind}) — гидратация resumePhotoViewer, set_input_files, "
        "photo-editor-apply, photo-viewer-action-assign-current"
    )


def upload_photo_on_hh(
    page: Page,
    resume,
    photo: PhotoFile,
    dry_run: bool,
    *,
    before_click=None,
) -> UploadPhotoResult:
    """Загрузить фото; мутирующая цепочка — set_input_files, apply, assign.

    ``before_click`` (seam #476, имя историческое) вызывается ровно один раз
    и строго до первой мутации (set_input_files): после него отказ возможен
    только как ``uncertain``. Dry-run ограничивается read-only осмотром блока.
    """
    goto_hh(page, resume.resume_url)
    require_authenticated_page(page)
    dismiss_cookie_banner(page)
    avatar = page.locator(RESUME_AVATAR_BLOCK).first
    try:
        # SPA-страница: блок аватара появляется после гидратации React
        # (паттерн «commit не значит отрисовано»), строгая проверка до
        # ожидания видела бы count=0 и списала бы это на селектор.
        avatar.wait_for(state="visible", timeout=_AVATAR_WAIT_TIMEOUT_MS)
    except PlaywrightError as exc:
        return UploadPhotoResult(
            reason=f"блок аватара не отрисовался на {page.url}: {exc}",
            photo_present=None,
        )
    if page.locator(RESUME_AVATAR_IMAGE).count() > 0:
        return UploadPhotoResult(
            reason="у резюме уже есть фото; замена не поддерживается",
            photo_present=True,
        )
    if dry_run:
        return UploadPhotoResult(
            success=True,
            reason=(
                "фото нет, блок аватара подтверждён; боевой режим гидратирует "
                "микрофронтенд и передаст файл в скрытый file-input"
            ),
            photo_present=False,
        )

    # До первой мутации: негидратированный инпут — чистый pre-click отказ,
    # файл никуда не передаём (повтор разрешён).
    page.evaluate(
        """(sel) => {
          const c = document.querySelector(sel);
          if (c) c.scrollIntoView({block: "center"});
        }""",
        RESUME_PHOTO_MFE_CONTAINER,
    )
    if not wait_for_react_hydration(
        page, RESUME_PHOTO_FILE_INPUT, timeout_ms=_HYDRATION_TIMEOUT_MS
    ):
        return UploadPhotoResult(
            reason=(
                "микрофронтенд resumePhotoViewer не гидратировался "
                f"за {_HYDRATION_TIMEOUT_MS // 1000}с — file-input без "
                "React-привязки; запись запрещена"
            ),
            photo_present=False,
        )
    inputs = page.locator(RESUME_PHOTO_FILE_INPUT)
    input_count = inputs.count()
    if input_count != 1:
        # Fail-closed: разметка уехала — файл никуда не передаём.
        return UploadPhotoResult(
            reason=(
                f"file-input не подтверждён однозначно (совпадений: {input_count}); "
                "запись запрещена"
            ),
            photo_present=False,
        )
    if before_click is not None:
        before_click()

    # Точка невозврата: файл передаётся инпуту. Все дальнейшие отказы —
    # uncertain (файл мог быть принят и обработан микрофронтендом).
    try:
        inputs.first.set_input_files(str(photo.path))
    except PlaywrightError as exc:
        return UploadPhotoResult(
            reason=f"ошибка передачи файла (файл мог быть передан): {exc}",
            uncertain=True,
            photo_present=None,
        )

    try:
        editor_apply = page.locator(RESUME_PHOTO_EDITOR_APPLY).first
        editor_apply.wait_for(state="visible", timeout=_EDITOR_WAIT_TIMEOUT_MS)
    except PlaywrightError as exc:
        # Лимит галереи — ДОКАЗАННЫЙ отказ загрузки (боевой прогон 5,
        # 2026-09-04, дамп photo_editor_missing_uncertain_*: модалка
        # photo-viewer-limit «8 фото — это максимум», файл отклонён,
        # мутации нет) — чистый fail без uncertain. Галерея фото общая
        # НА АККАУНТ, не на резюме: «чистый» черновик не спасает.
        if page.locator(RESUME_PHOTO_VIEWER_LIMIT).count() > 0:
            return UploadPhotoResult(
                reason=(
                    "галерея фото аккаунта переполнена (лимит hh.ru): загрузка "
                    "отклонена, фото не добавлено; освободите место в галерее "
                    "и повторите"
                ),
                photo_present=False,
            )
        # Иная причина — дамп для разбора альтернативного UI.
        dump_path = dump_page_html(page, "photo_editor_missing_uncertain")
        reason = f"файл передан, но crop-редактор не открылся: {exc}"
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return _uncertain(reason)
    try:
        editor_apply.click()
    except PlaywrightError as exc:
        return _uncertain(f"редактор открыт, клик apply не удался (upload не запущен): {exc}")

    try:
        assign_btn = page.locator(RESUME_PHOTO_VIEWER_ASSIGN_CURRENT).first
        assign_btn.wait_for(state="visible", timeout=_ASSIGN_WAIT_TIMEOUT_MS)
    except PlaywrightError as exc:
        return _uncertain(
            "crop-apply отправлен, но модалка назначения не открылась за "
            f"{_ASSIGN_WAIT_TIMEOUT_MS / 1000:.0f}с — фото могло загрузиться в галерею: {exc}"
        )
    # Модалка анимируется: overlay перехватывает pointer events, Playwright
    # ретраит клик и падает по таймауту (бои 2026-09-02 и 2026-09-03:
    # explore-пауза 2500мс пропускала клик, командная без неё — нет).
    page.wait_for_timeout(_ASSIGN_MODAL_SETTLE_MS)
    assign_click_error: str | None = None
    # Боевой кейс 2026-09-04 (#955, прогоны 1-8): кнопка assign в шапке
    # MediaViewer резолвилась и была stable, но стабильно «outside of the
    # viewport»: NavBar модалки отрисован НАД оверлеем (живой замер IAB
    # 2026-09-04: top = -56px при любом viewport), скролл ничего не решает.
    # Перед кликом нормализуем геометрию (скролл документа наверх + явный
    # scroll_into_view) — для конфигураций, где кнопка досягаема (успех
    # 2026-09-02). Ошибки скролла не фатальны — исход классифицирует маркер.
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except PlaywrightError as exc:
        print(f"[INFO] скролл документа наверх не удался (клик продолжается): {exc}")
    try:
        assign_btn.scroll_into_view_if_needed(timeout=_ASSIGN_SCROLL_TIMEOUT_MS)
    except PlaywrightError as exc:
        print(f"[INFO] assign-кнопка не проскроллилась (клик продолжается): {exc}")
    try:
        assign_btn.click()
    except PlaywrightError as click_exc:
        # Позиционный клик не удался (кнопка вне вьюпорта). Активация без
        # геометрии, но ТОЛЬКО после гидратации: ленивый чанк модалки
        # привязывает обработчики ПОСЛЕ открытия, и активация до неё
        # теряется молча (прогон 8: focus дошёл до кнопки — focus-visible
        # в дампе — но Enter и dispatch_event в негидратированный DOM
        # не сработали; живой замер IAB: гидратированная кнопка
        # назначает фото по программному клику). Исход в любом случае
        # классифицирует маркерное ожидание ниже; без маркера — честный
        # uncertain с дампом (fail-closed #955).
        assign_click_error = str(click_exc)
        hydrated = wait_for_react_hydration(
            page, RESUME_PHOTO_VIEWER_ASSIGN_CURRENT, timeout_ms=_ASSIGN_HYDRATION_TIMEOUT_MS
        )
        if not hydrated:
            print(
                "[INFO] React-привязка assign-кнопки не подтверждена за "
                f"{_ASSIGN_HYDRATION_TIMEOUT_MS // 1000}с — активация всё равно отправлена"
            )
        kb_error: str | None = None
        try:
            assign_btn.focus()
            page.keyboard.press("Enter")
            print("[INFO] клик assign не удался, отправлен клавиатурный Enter")
        except PlaywrightError as exc:
            kb_error = str(exc)
        try:
            assign_btn.dispatch_event("click")
            print("[INFO] отправлен dispatch_event('click') по assign-кнопке")
        except PlaywrightError as dispatch_exc:
            dump_path = dump_page_html(page, "photo_assign_click_uncertain")
            reason = (
                "модалка назначения открыта, клик assign не удался "
                f"(клавиатурный фолбэк и dispatch_event тоже): {click_exc}; "
                f"focus/Enter: {kb_error}; dispatch_event: {dispatch_exc}"
            )
            if dump_path is not None:
                reason += f"; дамп: {dump_path}"
            return _uncertain(reason)

    try:
        # Маркер — появление <img> в DOM («attached», не «visible»: сам блок
        # аватара уже подтверждён видимым выше).
        page.locator(RESUME_AVATAR_IMAGE).first.wait_for(
            state="attached", timeout=_CONFIRM_TIMEOUT_MS
        )
    except PlaywrightError:
        dump_path = dump_page_html(page, "photo_upload_uncertain")
        reason = (
            "assign отправлен, но маркер успеха (img в блоке аватара) не появился "
            f"за {_CONFIRM_TIMEOUT_MS // 1000}с"
        )
        if assign_click_error is not None:
            reason += f"; позиционный клик не удался: {assign_click_error[:300]}"
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return _uncertain(reason)
    # Маркер в DOM появляется ОПТИМИСТИЧНО: hh.ru рисует <img> до того,
    # как assign-запрос консолидировался на сервере (боевой кейс
    # 2026-09-02: браузер закрылся сразу после маркера — img остался,
    # а hasPhoto на сервере остался false). Успех подтверждает только
    # readback персистентного состояния: перезагрузка страницы резюме.
    page.wait_for_timeout(_ASSIGN_SETTLE_MS)
    confirmed, readback_reason = _readback_photo_persisted(page, resume.resume_url)
    if confirmed is True:
        return UploadPhotoResult(
            success=True,
            reason="фото загружено и назначено (подтверждено перезагрузкой страницы)",
            photo_present=True,
        )
    if confirmed is False:
        # Страница перечитана, img отсутствует: назначение на сервере не
        # произошло (файл мог остаться в галерее) — fail-closed uncertain.
        return UploadPhotoResult(
            reason=(
                "assign отправлен, но readback не подтвердил: "
                f"{readback_reason}; файл мог остаться в галерее фото"
            ),
            uncertain=True,
            photo_present=False,
        )
    return _uncertain(f"assign отправлен, но readback не выполнился: {readback_reason}")


def _readback_photo_persisted(page: Page, resume_url: str) -> tuple[bool | None, str]:
    """Перечитать страницу резюме и проверить персистентный признак фото.

    Возвращает (confirmed, reason): True — img аватара есть на свежезагруженной
    странице (серверное состояние, не оптимистичный DOM); False — страница
    прочитана, но img отсутствует; None — страница не прочитана (навигация или
    отрисовка не удались).
    """
    try:
        goto_hh(page, resume_url)
    except PlaywrightError as exc:
        return None, f"страница резюме не перечиталась: {exc}"
    try:
        page.locator(RESUME_AVATAR_BLOCK).first.wait_for(
            state="visible", timeout=_AVATAR_WAIT_TIMEOUT_MS
        )
    except PlaywrightError as exc:
        return None, f"блок аватара не отрисовался при перечитывании: {exc}"
    # Видимость блока не доказывает, что состояние фото дорендерено (SPA
    # вставляет img асинхронно, а плейсхолдер «фото нет» виден и ДО вставки
    # img — замечание ревью #962): сначала ограниченный бюджет ждём
    # ПОЗИТИВНЫЙ признак (img), и только после его исчерпания отсутствие
    # фото подтверждаем явным плейсхолдером. Ни того, ни другого —
    # состояние не определено.
    # ОГРАНИЧЕНИЕ: плейсхолдер подтверждён только на мужском профиле
    # (Magritte рендерит аватар по полу, селектор группы — см. комментарий
    # у RESUME_AVATAR_PLACEHOLDER); на женском аккаунте absence-ветка
    # выродится в honest uncertain, что fail-closed-корректно.
    deadline = time.monotonic() + _READBACK_CONFIRM_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        if page.locator(RESUME_AVATAR_IMAGE).count() > 0:
            return True, ""
        page.wait_for_timeout(_READBACK_POLL_MS)
    # Оба uncertain-исхода readback — единственный путь после assign без
    # артефакта (маркерный путь уже пишет photo_upload_uncertain): дамп
    # перечитанной страницы — первый подозреваемый при дрейфе селектора.
    dump_path = dump_page_html(page, "photo_readback_uncertain")
    dump_note = "" if dump_path is None else f"; дамп: {dump_path}"
    if (
        page.locator(RESUME_AVATAR_IMAGE).count() == 0
        and page.locator(RESUME_AVATAR_PLACEHOLDER).count() > 0
    ):
        return (
            False,
            "подтверждено состояние «фото нет» (плейсхолдер, img не появился "
            f"за {_READBACK_CONFIRM_TIMEOUT_MS // 1000}с) "
            f"на свежезагруженной странице{dump_note}",
        )
    return None, (
        "состояние фото не определено за "
        f"{_READBACK_CONFIRM_TIMEOUT_MS // 1000}с — ни img, ни плейсхолдера"
        f"{dump_note}"
    )


def _uncertain(reason: str) -> UploadPhotoResult:
    return UploadPhotoResult(reason=reason, uncertain=True, photo_present=None)

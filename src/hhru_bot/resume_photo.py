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

import re
import stat as stat_module
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import (
    RESUME_UNAVAILABLE_REASON,
    dismiss_cookie_banner,
    dump_page_html,
    goto_hh,
    has_resume_error_banner,
    require_authenticated_page,
    wait_for_react_hydration,
)
from .selector_groups.resume_photo import (
    PHOTO_ACCEPTED_EXT,
    RESUME_AVATAR_BLOCK,
    RESUME_AVATAR_EDIT_BUTTON,
    RESUME_AVATAR_IMAGE,
    RESUME_AVATAR_PLACEHOLDER,
    RESUME_PHOTO_EDITOR_APPLY,
    RESUME_PHOTO_FILE_INPUT,
    RESUME_PHOTO_MFE_CONTAINER,
    RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
    RESUME_PHOTO_VIEWER_ASSIGN_RESUME_TEMPLATE,
    RESUME_PHOTO_VIEWER_ASSIGN_SUBMIT,
    RESUME_PHOTO_VIEWER_CLOSE,
    RESUME_PHOTO_VIEWER_LIMIT,
    RESUME_PHOTO_VIEWER_ROOT,
    RESUME_PHOTO_VIEWER_THUMBNAILS,
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
# Пикер «Куда поставим фото?» (чекбоксы photo-viewer-assign-resume-<id>):
# окно ожидания его появления после неудачного первого маркерного бюджета.
_PICKER_WAIT_TIMEOUT_MS = 10_000
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
    # #972: сбойный экран /resume/{id} — внятный отказ вместо таймаута на
    # ожидании блока аватара. Pre-mutation (файл никуда не передан).
    if has_resume_error_banner(page):
        return UploadPhotoResult(reason=RESUME_UNAVAILABLE_REASON, photo_present=None)
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
    # Фикс #953: в дампе боевого прогона уходящий overlay держит
    # magritte-animation-exit-*-active; ждём его исчезновения поллингом,
    # потом оседающая пауза (пилюля #955 про геометрию кнопки — ниже).
    _wait_overlays_settled(page, _OVERLAY_SETTLE_TIMEOUT_MS)
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
        # Бои 8-12 (2026-09-04/05, #955): после crop-upload hh.ru показывает
        # рядом с MediaViewer пикер «Куда поставим фото?» — чекбоксы строк
        # резюме (photo-viewer-assign-resume-<id>) + футерная кнопка
        # «Выбрать и установить», DISABLED до выбора. Это in-body модалка —
        # обычные клики работают; путь через assign-current NavBar мёртв
        # для blob. Порядок фолбэков: пикер -> переоткрытие вьювера.
        # Бой 12: пикер РЕАЛЬНО назначил фото (live-readback IAB: аватар
        # есть после перезагрузки), но аватар на странице под модалкой
        # SPA не перерисовал за 30с — отсутствие маркера после фолбэка
        # НЕ uncertain: решает readback персистентного состояния ниже
        # (success только по свежему DOM, fail-closed не тронут).
        if _assign_via_resume_picker(page, resume.resume_id):
            pass
        elif _assign_via_viewer_reopen(page):
            try:
                page.locator(RESUME_AVATAR_IMAGE).first.wait_for(
                    state="attached", timeout=_CONFIRM_TIMEOUT_MS
                )
            except PlaywrightError:
                # назначение могло консолидироваться без перерисовки —
                # решает readback ниже
                pass
        else:
            dump_path = dump_page_html(page, "photo_upload_uncertain")
            reason = (
                "assign отправлен, но маркер успеха (img в блоке аватара) не появился "
                f"за {_CONFIRM_TIMEOUT_MS // 1000}с; фолбэки (пикер, переоткрытие "
                "вьювера) не удались (см. [INFO] в логе)"
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


def _assign_via_resume_picker(page: Page, resume_id: str) -> bool:
    """Фолбэк «пикер назначения»: чекбокс нашего резюме + «Выбрать и установить».

    Бои 8-11 (2026-09-04/05, #955): после crop-upload hh.ru показывает
    модалку «Куда поставим фото?» (обычная in-body magritte-modal, дамп
    photo_upload_uncertain_20260905_004737) — чекбоксы строк резюме
    photo-viewer-assign-resume-<resume_id> и футерная кнопка
    photo-viewer-assign-submit «Выбрать и установить», DISABLED до выбора
    строки. В отличие от assign-current (detached NavBar, мёртвая
    активация для blob) здесь работают обычные позиционные клики.
    Все шаги best-effort: False = фолбэк не удался, решение за вызывающим
    (честный uncertain, fail-closed).
    """
    checkbox_selector = RESUME_PHOTO_VIEWER_ASSIGN_RESUME_TEMPLATE.format(resume_id=resume_id)
    try:
        checkbox = page.locator(checkbox_selector).first
        checkbox.wait_for(state="attached", timeout=_PICKER_WAIT_TIMEOUT_MS)
    except PlaywrightError:
        return False  # пикера нет — пробуем следующий фолбэк
    wait_for_react_hydration(page, checkbox_selector, timeout_ms=_ASSIGN_HYDRATION_TIMEOUT_MS)
    try:
        # Magritte input невидим (opacity:0), но имеет ненулевой bbox —
        # Playwright check() кликает связанный контрол; при отказе — dispatch.
        try:
            checkbox.check(timeout=_ASSIGN_SCROLL_TIMEOUT_MS)
        except PlaywrightError:
            checkbox.dispatch_event("click")
    except PlaywrightError as exc:
        print(f"[INFO] фолбэк-пикер: чекбокс резюме не выбран: {exc}")
        return False
    try:
        submit = page.locator(RESUME_PHOTO_VIEWER_ASSIGN_SUBMIT).first
        submit.wait_for(state="visible", timeout=_ASSIGN_WAIT_TIMEOUT_MS)
        # кнопка DISABLED до выбора строки; click дождётся enabled в рамках
        # таймаута и упадёт честно, если выбор не засчитался
        submit.click(timeout=_CONFIRM_TIMEOUT_MS)
    except PlaywrightError as exc:
        print(f"[INFO] фолбэк-пикер: клик «Выбрать и установить» не удался: {exc}")
        return False
    print("[INFO] фолбэк-пикер: резюме выбрано, «Выбрать и установить» отправлено")
    return True


def _assign_via_viewer_reopen(page: Page) -> bool:
    """Фолбэк «переоткрыть вьювер»: назначить фото через карандаш.

    Бои 8-9 (2026-09-04, #955): после crop-upload активация assign-current
    в открытой модалке молча не работает (current — blob без photo id), а
    для персистентных фото галереи тот же dispatch_event назначает фото
    (диагностика explore_photo_assign_activation на «Дворнике»: modal
    закрылась, маркер появился). Вьювер по карандашу открывается на
    НОВЕЙШЕМ фото галереи — то есть на только что загруженном файле.
    Все шаги best-effort: False = фолбэк не удался, решение за вызывающим
    (честный uncertain, fail-closed).
    """
    # 1. Закрыть модалку (крестик в том же detached NavBar — только dispatch).
    try:
        page.locator(RESUME_PHOTO_VIEWER_CLOSE).first.dispatch_event("click")
    except PlaywrightError as exc:
        print(f"[INFO] фолбэк: не удалось закрыть модалку вьювера: {exc}")
        return False
    # 2. Открыть вьювер карандашом (MFE уже гидратирован на этом шаге).
    try:
        page.locator(RESUME_AVATAR_EDIT_BUTTON).first.click()
    except PlaywrightError as exc:
        print(f"[INFO] фолбэк: клик по карандашу не удался: {exc}")
        return False
    # 3. после переоткрытия assign-кнопка бывает в ДВУХ видах (дампы
    #    photo_upload_uncertain_20260904_233024/20260905_003039):
    #    assign-current в detached NavBar (бои 8-9 — активация мертва для
    #    blob) или assign-submit в теле модалки — подтверждение «назначить
    #    это фото» именно для свежей загрузки. Ждём любую из двух и
    #    активируем по фактическому data-qa: сначала позиционный клик
    #    (in-body кнопка досягаема), при отказе — dispatch_event.
    try:
        combined = page.locator(
            f"{RESUME_PHOTO_VIEWER_ASSIGN_CURRENT}, {RESUME_PHOTO_VIEWER_ASSIGN_SUBMIT}"
        ).first
        combined.wait_for(state="attached", timeout=_ASSIGN_WAIT_TIMEOUT_MS)
        page.wait_for_timeout(_ASSIGN_MODAL_SETTLE_MS)
        resolved_qa = combined.get_attribute("data-qa")
        resolved_selector = f"[data-qa='{resolved_qa}']"
        if not wait_for_react_hydration(
            page, resolved_selector, timeout_ms=_ASSIGN_HYDRATION_TIMEOUT_MS
        ):
            print(
                "[INFO] фолбэк: React-привязка assign-кнопки не подтверждена — "
                "активация всё равно отправлена"
            )
        try:
            combined.click(timeout=_ASSIGN_SCROLL_TIMEOUT_MS)
        except PlaywrightError:
            combined.dispatch_event("click")
    except PlaywrightError as exc:
        print(f"[INFO] фолбэк: активация assign после переоткрытия не удалась: {exc}")
        return False
    print("[INFO] фолбэк: переоткрытый вьювер, активация assign отправлена")
    return True


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


# Магриттовские overlay-анимации в дампах боевого прогона живут парами классов:
# «уходит» — magritte-animation-exit-center___hash + magritte-animation-exit-
# center-active___hash, «входит» — аналогично enter. Признак «overlay ещё
# перехватывает клики» — класс с "animation-exit" И "-active" одновременно
# (enter-active и хэш-хвосты не совпадают с этой парой). Верхняя модалка
# стопки (бои 2026-09-03 при непустой библиотеке) гаснет не мгновенно:
# фикс-пауза 2500мс в бою не помогла — ждём исчезновения exit-active
# классов поллингом.
_OVERLAY_SETTLE_TIMEOUT_MS = 10_000
_OVERLAY_SETTLED_JS = """() => {
  const overlays = document.querySelectorAll("[data-qa='modal-overlay']");
  return !Array.from(overlays).some((el) =>
    Array.from(el.classList).some(
      (c) => c.includes("animation-exit") && c.includes("-active"),
    ));
}"""

# Диагностический инвентарь стопки модалок (ишью #953: боевой кейс 2026-09-03
# диагностировался по retry-логу именно из-за его отсутствия): сколько
# overlay, какой z-index, какая анимация активна.
_OVERLAY_INVENTORY_JS = """() => Array.from(
  document.querySelectorAll("[data-qa='modal-overlay']"),
).map((el) => ({
  zIndex: el.style.zIndex || "",
  exitActive: Array.from(el.classList).some(
    (c) => c.includes("animation-exit") && c.includes("-active"),
  ),
}))"""


def _wait_overlays_settled(page: Page, timeout_ms: int) -> bool:
    """Дождаться, пока ни один modal-overlay не держит exit-анимацию.

    False по таймауту — решение об отказе за вызывающим (клик всё равно
    делаем: Playwright сам ретраит, а его падение даёт честный uncertain).
    """
    try:
        page.wait_for_function(_OVERLAY_SETTLED_JS, timeout=timeout_ms)
    except PlaywrightError:
        return False
    return True


def _overlay_inventory(page) -> str:
    try:
        overlays = page.evaluate(_OVERLAY_INVENTORY_JS)
    except PlaywrightError:
        return "инвентарь modal-overlay недоступен"
    if not overlays:
        return "modal-overlay: 0 шт"
    parts = [
        f"z={item.get('zIndex', '?')} exit_active={item.get('exitActive')}" for item in overlays
    ]
    return f"modal-overlay x{len(overlays)}: " + "; ".join(parts)


# --- Выбор фото из библиотеки (select-photo, #953) -------------------------

# Идентичность фото в DOM — числовой id в пути URL (живой дамп боевого прогона
# 2026-09-02: thumbnails модалки назначения, src вида
# https://img.hhcdn.ru/photo/{id}.jpeg?t=...&h=... или относительный
# /photo/{id}.jpeg?...). Один и тот же id рендерится с разными query (кропы/
# размеры) — дедуп по id, порядок первый встречи.
_PHOTO_ID_RE = re.compile(r"/photo/(\d+)")

# Инвентарь библиотеки внутри вьюера: лента миниатюр футера (по одной на
# фото, порядок = порядок слайдера), счётчик «N из M» и маркер «Назначено»
# для ТЕКУЩЕГО слайда. Живой дамп 2026-09-04: вьюер — role=dialog
# magritte-media-viewer, лента — magritte-preview-list, overlay нет.
_VIEWER_STATE_JS = """() => {
  const root = document.querySelector(
    "[role='dialog'][class*='magritte-media-viewer___']",
  );
  if (!root) return null;
  const photos = [];
  root.querySelectorAll("[class*='magritte-preview-list___'] img").forEach(
    (img) => {
      const src = img.currentSrc || img.src || "";
      const m = src.match(/\\/photo\\/(\\d+)/);
      if (m) photos.push({photoId: m[1], src: src});
    },
  );
  const counter = root.querySelector("[class*='magritte-counter-number___']");
  const cm = counter ? counter.textContent.match(/(\\d+)\\s*из\\s*(\\d+)/) : null;
  return {
    photos: photos,
    index: cm ? Number(cm[1]) : null,
    total: cm ? Number(cm[2]) : null,
    assigned: !!root.querySelector("[data-qa='photo-viewer-action-assigned']"),
  };
}"""


@dataclass(frozen=True)
class LibraryPhoto:
    photo_id: str
    src: str


def parse_photo_id(src: str) -> str | None:
    """Числовой id фото из URL (``/photo/{id}.jpeg?...``); None если не фото."""
    match = _PHOTO_ID_RE.search(src)
    return match.group(1) if match else None


def parse_library_photos(items) -> tuple[LibraryPhoto, ...]:  # noqa: ANN001 - JS payload
    """Дедуп JS-инвентаря по id с сохранением первого порядка встречи."""
    photos: list[LibraryPhoto] = []
    seen: set[str] = set()
    for item in items:
        photo_id = str(item.get("photoId", ""))
        if not photo_id or photo_id in seen:
            continue
        seen.add(photo_id)
        photos.append(LibraryPhoto(photo_id=photo_id, src=str(item.get("src", ""))))
    return tuple(photos)


@dataclass
class SelectPhotoResult:
    success: bool = False
    reason: str = ""
    uncertain: bool = False
    photos: tuple[LibraryPhoto, ...] = ()  # инвентарь библиотеки (dry-run/list)
    assigned_photo_id: str | None = None  # подтверждённый id после assign
    avatar_src: str = ""  # src img аватара (dry-run: текущее фото резюме)


def select_photo_plan(resume_id: str, photo_id: str | None) -> str:
    target = f"photo {photo_id}" if photo_id else "инвентарь библиотеки фото"
    return (
        f"резюме {resume_id}: {target} — гидратация resumePhotoViewer, "
        "клик resume-avatar-edit-button (read-only открывает вьюер), "
        "выбор фото в галерее, photo-viewer-action-assign-current"
    )


def _hydrate_and_open_viewer(page: Page) -> UploadPhotoResult | None:
    """Общая подготовка select-photo: гидратация MFE + открытие вьюера.

    Возвращает None при успехе, иначе честный pre-mutation отказ (до
    before_click — повтор разрешён без reconciliation).
    """
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
                f"за {_HYDRATION_TIMEOUT_MS // 1000}с — кнопка карандаша "
                "не откроет вьюер"
            ),
        )
    try:
        pencil = page.locator(RESUME_AVATAR_EDIT_BUTTON).first
        # Паттерн «commit не значит отрисовано»: строгая проверка до
        # ожидания видела бы count=0 после гидратации SPA.
        pencil.wait_for(state="visible", timeout=_AVATAR_WAIT_TIMEOUT_MS)
        pencil.click()
    except PlaywrightError as exc:
        return UploadPhotoResult(reason=f"кнопка карандаша не кликнулась: {exc}")
    try:
        # Маркер открытого вьюера — корень MediaViewer (живой дамп 2026-09-04).
        # НЕ кнопка назначения: у уже назначенного фото вместо assign-current
        # рендерится disabled photo-viewer-action-assigned (дамп 2026-09-04).
        page.locator(RESUME_PHOTO_VIEWER_ROOT).first.wait_for(
            state="visible", timeout=_ASSIGN_WAIT_TIMEOUT_MS
        )
    except PlaywrightError as exc:
        dump_path = dump_page_html(page, "photo_viewer_open_failed")
        reason = f"вьюер фото не открылся по кнопке карандаша ({_overlay_inventory(page)}): {exc}"
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return UploadPhotoResult(reason=reason)
    _wait_overlays_settled(page, _OVERLAY_SETTLE_TIMEOUT_MS)
    return None


def select_photo_on_hh(
    page: Page,
    resume,
    photo_id: str | None,
    dry_run: bool,
    *,
    before_click=None,
) -> SelectPhotoResult:
    """Выбрать фото из библиотеки и назначить резюме; dry-run — инвентарь.

    Карандашный поток (бои 2026-09-02/03): открывает вьюер БЕЗ стопки
    перехватывающих модалок. Клик по карандашу и переключение фото в галерее
    — read-only; единственная мутация — клик assign-current, начиная с него
    работает seam ``before_click`` (#476).
    """
    goto_hh(page, resume.resume_url)
    require_authenticated_page(page)
    dismiss_cookie_banner(page)
    # #972: сбойный экран /resume/{id} — внятный отказ вместо таймаута на
    # ожидании блока аватара. Pre-mutation (карандаш/галерея не тронуты).
    if has_resume_error_banner(page):
        return SelectPhotoResult(reason=RESUME_UNAVAILABLE_REASON)
    avatar = page.locator(RESUME_AVATAR_BLOCK).first
    try:
        avatar.wait_for(state="visible", timeout=_AVATAR_WAIT_TIMEOUT_MS)
    except PlaywrightError as exc:
        return SelectPhotoResult(reason=f"блок аватара не отрисовался на {page.url}: {exc}")
    fail = _hydrate_and_open_viewer(page)
    if fail is not None:
        return SelectPhotoResult(reason=fail.reason, uncertain=fail.uncertain)
    state = _read_viewer_state(page)
    if state is None:
        dump_path = dump_page_html(page, "photo_select_no_viewer")
        reason = "вьюер фото не подтверждён после карандаша (корень MediaViewer не найден)"
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return SelectPhotoResult(reason=reason)
    photos = state.photos
    if dry_run:
        reason = select_photo_plan(resume.id, photo_id)
        if photos:
            ids = ", ".join(p.photo_id for p in photos)
            reason += f"; в библиотеке {len(photos)} фото: {ids}"
        else:
            reason += "; библиотека пуста"
        current = (
            f"текущее фото: {state.thumb_ids[state.index - 1]}"
            if state.index and 0 < state.index <= len(state.thumb_ids)
            else "текущее фото: не определено"
        )
        reason += f"; слайд {state.index} из {state.total}; {current}; назначено: {state.assigned}"
        # get_attribute — ждущий метод: на резюме без фото дал бы 30-секундный
        # вис (ревью #967, раунд 4) — гейт по count(), как в upload-потоке.
        avatar_loc = page.locator(RESUME_AVATAR_IMAGE)
        avatar_src = avatar_loc.first.get_attribute("src") or "" if avatar_loc.count() > 0 else ""
        return SelectPhotoResult(success=True, reason=reason, photos=photos, avatar_src=avatar_src)
    return _select_and_assign(page, resume, state, photo_id, before_click)


@dataclass
class ViewerState:
    photos: tuple[LibraryPhoto, ...]  # дедуп по id — для вывода dry-run
    thumb_ids: tuple[str, ...]  # СЫРОЙ порядок ленты — индексы для nth()
    index: int | None  # текущий слайд, 1-based («N из M»)
    total: int | None
    assigned: bool  # маркер «Назначено» для ТЕКУЩЕГО слайда


def _read_viewer_state(page: Page) -> ViewerState | None:
    try:
        raw = page.evaluate(_VIEWER_STATE_JS)
    except PlaywrightError:
        return None
    if not raw:
        return None
    return ViewerState(
        photos=parse_library_photos(raw.get("photos", [])),
        thumb_ids=tuple(str(item.get("photoId", "")) for item in raw.get("photos", [])),
        index=raw.get("index"),
        total=raw.get("total"),
        assigned=bool(raw.get("assigned")),
    )


def _current_photo_id(state: ViewerState) -> str | None:
    # Счётчик слайдера адресует СЫРОЙ порядок ленты (thumb_ids), не дедуп:
    # при повторе id в ленте индексы после дубликата разъезжаются (ревью #967,
    # раунд 3).
    if state.index and 0 < state.index <= len(state.thumb_ids):
        return state.thumb_ids[state.index - 1]
    return None


def _variant_sibling(avatar_id: str | None, target_id: str, state: ViewerState) -> str:
    """Классификация серверного id после assign (боевой факт 2026-09-04).

    hh.ru хранит каждую загрузку ПАРОЙ соседних id (N/N+1: 913960391/392,
    912941964/965, 908279072/073, 637550758/759, 637549023/024): в ленте и
    вьюере рендерится display-вариант, а назначает hh.ru канонический
    СОСЕДНИЙ СТАРШИЙ id. Возврат: "same" — id совпал; "sibling" — avatar_id
    == target + 1 (наблюдаемое направление боем 2026-09-04; направление −1
    сознательно НЕ принято: при подряд выделенных парах A=N/N+1, B=N+2/N+3
    промах по B при назначенном A дал бы ложный sibling) И avatar_id не
    встречается в ленте как отдельное фото (защита от соседнего id чужой
    загрузки); "other" — всё остальное (назначено не то).
    """
    if avatar_id is None:
        return "other"
    if avatar_id == target_id:
        return "same"
    strip_ids = set(state.thumb_ids) | {p.photo_id for p in state.photos}
    try:
        if int(avatar_id) == int(target_id) + 1 and avatar_id not in strip_ids:
            return "sibling"
    except ValueError:
        pass
    return "other"


def _select_and_assign(
    page: Page,
    resume,
    state: ViewerState,
    photo_id: str | None,
    before_click,
) -> SelectPhotoResult:
    """Боевой путь: выбор конкретного фото и назначение (см. select_photo_on_hh).

    Идентичность назначаемого фото доказывается трижды: target найден в
    сырой ленте (thumb_ids); после клика миниатюры слайдер стоит на этом id;
    после перезагрузки страницы ``img`` аватара ведёт на тот же id или его
    канонический sibling-вариант (``_variant_sibling``; серверное состояние,
    не оптимистичный маркер).
    """
    if not photo_id:
        return SelectPhotoResult(
            reason="боевой режим требует --photo-id (идентификатор из dry-run инвентаря)"
        )
    if not state.photos:
        return SelectPhotoResult(reason="библиотека фото пуста — назначать нечего")
    # Однозначность — по различимым id (повтор того же id в ленте — это то
    # же фото с другим query-кропом, не неоднозначность), а ИНДЕКС клика —
    # по сырому порядку ленты: nth() локатора адресует сырой DOM, позиции
    # после дубликата id в дедупе сдвигаются (ревью #967).
    if photo_id not in state.thumb_ids:
        return SelectPhotoResult(
            reason=(
                f"photo {photo_id} не подтверждён в ленте "
                f"(0 из {len(state.thumb_ids)} миниатюр); запись запрещена"
            )
        )
    raw_index = state.thumb_ids.index(photo_id)
    try:
        # Локатор скоупится корнем вьюера — тем же скоупом, что и JS-инвентарь.
        thumbs = page.locator(RESUME_PHOTO_VIEWER_ROOT).locator(RESUME_PHOTO_VIEWER_THUMBNAILS)
        thumbs.nth(raw_index).click()
    except PlaywrightError as exc:
        return SelectPhotoResult(reason=f"клик по миниатюре photo {photo_id} не удался: {exc}")
    # Переключение слайда — React-рендер: строгая проверка до ожидания
    # увидела бы прежний слайд («commit не значит отрисовано»). Поллим
    # чтение состояния вьюера: каждый промах — честный fail-closed отказ
    # до мутации, ранний отказ здесь дороже лишней секунды.
    switched: ViewerState | None = None
    for _ in range(5):
        page.wait_for_timeout(1_000)
        switched = _read_viewer_state(page)
        if switched is not None and _current_photo_id(switched) == photo_id:
            break
    if switched is None or _current_photo_id(switched) != photo_id:
        current = _current_photo_id(switched) if switched else None
        return SelectPhotoResult(
            reason=(
                f"после клика по миниатюре слайдер не подтвердил photo {photo_id} "
                f"(текущий: {current}); запись запрещена"
            )
        )
    if switched.assigned:
        return SelectPhotoResult(
            success=True,
            reason=f"photo {photo_id} уже назначен этому резюме (маркер «Назначено»)",
            photos=switched.photos,
            assigned_photo_id=photo_id,
        )
    try:
        assign_btn = page.locator(RESUME_PHOTO_VIEWER_ASSIGN_CURRENT).first
        assign_btn.wait_for(state="visible", timeout=_ASSIGN_WAIT_TIMEOUT_MS)
    except PlaywrightError as exc:
        # До before_click мутации не было — чистый fail-closed отказ
        # (seam #476), uncertain здесь запрещён контрактом. Единственное
        # опасение — поздний авто-assign; перечитываем маркер «Назначено»:
        # если слайд успели назначить, это честный success no-op.
        late = _read_viewer_state(page)
        if late is not None and late.assigned and _current_photo_id(late) == photo_id:
            return SelectPhotoResult(
                success=True,
                reason=f"photo {photo_id} назначен (маркер «Назначено» без клика)",
                photos=late.photos,
                assigned_photo_id=photo_id,
            )
        return SelectPhotoResult(
            reason=f"кнопка назначения не появилась для photo {photo_id}: {exc}"
        )
    _wait_overlays_settled(page, _OVERLAY_SETTLE_TIMEOUT_MS)
    if before_click is not None:
        before_click()
    try:
        assign_btn.click()
    except PlaywrightError as exc:
        dump_path = dump_page_html(page, "photo_select_intercepted")
        reason = f"клик assign не удался ({_overlay_inventory(page)}): {exc}"
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return SelectPhotoResult(reason=reason, uncertain=True)

    try:
        # Оптимистичный маркер: <img> появляется в аватаре до консолидации.
        page.locator(RESUME_AVATAR_IMAGE).first.wait_for(
            state="attached", timeout=_CONFIRM_TIMEOUT_MS
        )
    except PlaywrightError:
        dump_path = dump_page_html(page, "photo_select_uncertain")
        reason = (
            "assign отправлен, но маркер (img в аватаре) не появился "
            f"за {_CONFIRM_TIMEOUT_MS // 1000}с"
        )
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return SelectPhotoResult(reason=reason, uncertain=True)
    page.wait_for_timeout(_ASSIGN_SETTLE_MS)
    # Серверная сверка: перезагрузка и чтение img аватара (InitialState).
    goto_hh(page, resume.resume_url)
    require_authenticated_page(page)
    try:
        page.locator(RESUME_AVATAR_IMAGE).first.wait_for(
            state="attached", timeout=_AVATAR_WAIT_TIMEOUT_MS
        )
    except PlaywrightError:
        return SelectPhotoResult(
            reason="после перезагрузки фото в аватаре не подтвердилось сервером",
            uncertain=True,
        )
    avatar_src = page.locator(RESUME_AVATAR_IMAGE).first.get_attribute("src") or ""
    avatar_id = parse_photo_id(avatar_src)
    variant = _variant_sibling(avatar_id, photo_id, state)
    if variant == "other":
        return SelectPhotoResult(
            reason=(
                f"на резюме подтверждено фото {avatar_id}, а не {photo_id}; "
                "проверьте резюме на hh.ru вручную (reconciliation по протоколу CLAUDE.md)"
            ),
            uncertain=True,
        )
    return SelectPhotoResult(
        success=True,
        reason=(
            f"назначено фото {photo_id} (подтверждено после перезагрузки"
            + (f"; канонический id {avatar_id}" if variant == "sibling" else "")
            + ")"
        ),
        photos=switched.photos,
        assigned_photo_id=avatar_id or photo_id,
    )


def _uncertain(reason: str) -> UploadPhotoResult:
    return UploadPhotoResult(reason=reason, uncertain=True, photo_present=None)

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
   фото на это резюме) -> ``img`` в блоке аватара (подтверждённый маркер
   успеха, 2026-09-02).
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
    RESUME_PHOTO_EDITOR_APPLY,
    RESUME_PHOTO_FILE_INPUT,
    RESUME_PHOTO_MFE_CONTAINER,
    RESUME_PHOTO_VIEWER_ASSIGN_CURRENT,
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
# Пауза после появления маркера до объявления успеха: assign-запрос должен
# уйти и подтвердиться, иначе закрытие браузера прямо после маркера может
# оборвать его (боевой кейс 2026-09-02: img на месте, hasPhoto на сервере
# остался false).
_ASSIGN_SETTLE_MS = 5_000

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
)


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
        return _uncertain(f"файл передан, но crop-редактор не открылся: {exc}")
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
    try:
        assign_btn.click()
    except PlaywrightError as exc:
        return _uncertain(f"модалка назначения открыта, клик assign не удался: {exc}")

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
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return _uncertain(reason)
    # Маркер в DOM появляется ОПТИМИСТИЧНО: hh.ru рисует <img> до того,
    # как assign-запрос консолидировался на сервере (боевой кейс
    # 2026-09-02: браузер закрылся сразу после маркера — img остался,
    # а hasPhoto на сервере остался false; фикс живым прогоном).
    page.wait_for_timeout(_ASSIGN_SETTLE_MS)
    return UploadPhotoResult(success=True, reason="фото загружено и назначено", photo_present=True)


def _uncertain(reason: str) -> UploadPhotoResult:
    return UploadPhotoResult(reason=reason, uncertain=True, photo_present=None)

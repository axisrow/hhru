"""Удаление фото из библиотеки и скрытие фото из резюме (#966).

Оба действия открываются из одного more-меню «Действия с фото» вьюера
(``photo-viewer-more``), но имеют РАЗНЫЕ точки невозврата и разный охват
(живой DOM 2026-09-05, дампы ``photo_more_menu_*`` и
``photo_delete_confirm_*`` в data/logs):

- **Скрытие** (без ``--from-library``): пункт ``photo-viewer-action-hide``
  «Скрыть фото из резюме» — снимает фото с ОДНОГО резюме, фото остаётся в
  библиотеке. Пункт рендерится ТОЛЬКО для фото, назначенного текущему
  резюме (в одном меню с disabled ``photo-viewer-action-assigned``; у
  неназначенного вместо него ``assign-current``). Клик мутирует СРАЗУ,
  без confirm-диалога (в словаре локализации MFE нет ``hide.confirm``;
  обратимость — повторное назначение select-photo) — точка невозврата,
  ``before_click`` стоит на самом клике.
- **Удаление** (``--from-library``): пункт ``photo-viewer-action-delete``
  открывает confirm-диалог ``photo-viewer-delete`` «Удалить фото?» с
  описанием «Оно удалится из резюме, где было установлено» — бьёт по ВСЕМ
  резюме аккаунта с этим фото, необратимо. Клик по пункту сам НЕ мутирует
  (доказано живым read-only прогоном 2026-09-05: клик открыл диалог,
  подтверждение не нажималось, все фото библиотеки на месте при следующем
  прогоне). Единственная мутация — клик ``photo-viewer-delete-confirm``,
  ``before_click`` стоит на нём.

Идентичность фото доказывается до мутации общим с select-photo шагом
``switch_viewer_photo`` (лента -> слайдер). Успех доказывается только
readback персистентного состояния: скрытие — плейсхолдер «фото нет» на
перечитанной странице резюме (``_readback_photo_persisted``); удаление —
фото исчезло из ленты заново открытого вьюера.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import dismiss_cookie_banner, dump_page_html, goto_hh, require_authenticated_page
from .resume_photo import (
    LibraryPhoto,
    ViewerState,
    _hydrate_and_open_viewer,
    _read_viewer_state,
    _readback_photo_persisted,
    switch_viewer_photo,
)
from .selector_groups.resume_photo import (
    RESUME_PHOTO_VIEWER_ACTION_DELETE,
    RESUME_PHOTO_VIEWER_ACTION_HIDE,
    RESUME_PHOTO_VIEWER_DELETE_CONFIRM,
    RESUME_PHOTO_VIEWER_DELETE_DIALOG,
    RESUME_PHOTO_VIEWER_MORE,
)

# Открытие more-меню: drop-панель Magritte появляется в DOM вместе с
# пунктами action-* (без открытого меню их в DOM нет вовсе — дампы вьюера
# 2026-09-04), анимация панели короткая, но «commit не значит отрисовано».
_MENU_WAIT_TIMEOUT_MS = 10_000
# Позиционный клик по кнопкам NavBar вьюера (fallback dispatch_event —
# тот же detached-NavBar-паттерн assign-current/close, #955).
_MENU_CLICK_TIMEOUT_MS = 5_000
# Confirm-диалог рендерится React-ом после клика по пункту «Удалить»
# (живой прогон 2026-09-05: открылся мгновенно, но проверяем с бюджетом).
_DIALOG_WAIT_TIMEOUT_MS = 10_000
# Закрытие диалога после confirm-клика — переходный сигнал, не доказательство.
_DIALOG_GONE_TIMEOUT_MS = 15_000
# Пауза до readback: серверная консолидация удаления может отставать от
# закрытия диалога (тот же класс гонок, что оптимистичный img-маркер #955).
_READBACK_SETTLE_MS = 5_000

HIDE_ACTION = "hide_photo"
DELETE_ACTION = "delete_photo"

# Инвентарь пунктов more-меню: панель — magritte drop [data-qa='drop'] в
# портале body (живой дамп 2026-09-05), скоупимся ею, чтобы не подтягивать
# NavBar-кнопки того же префикса (assign-current/assigned — шапка вьюера, а
# не пункты меню; замечание ревью PR #973). При дрейфе контейнера — фолбэк
# на весь документ: инвентарь шире реальности лучше молча пустого.
_MENU_ACTIONS_JS = """() => {
  const scoped = document.querySelectorAll(
    "[data-qa='drop'] [data-qa^='photo-viewer-action-']",
  );
  const nodes = scoped.length
    ? scoped
    : document.querySelectorAll("[data-qa^='photo-viewer-action-']");
  return Array.from(nodes).map((el) => ({
    qa: el.getAttribute("data-qa") || "",
    text: (el.textContent || "").trim().slice(0, 100),
  }));
}"""


@dataclass(frozen=True)
class MenuAction:
    qa: str  # data-qa пункта (photo-viewer-action-*)
    text: str


@dataclass
class DeletePhotoResult:
    success: bool = False
    reason: str = ""
    uncertain: bool = False
    action: str = ""  # HIDE_ACTION | DELETE_ACTION (для аудита и вывода)
    photos: tuple[LibraryPhoto, ...] = ()  # инвентарь библиотеки (dry-run)
    menu_actions: tuple[MenuAction, ...] = ()  # пункты more-меню (dry-run)


def delete_photo_plan(resume_id: str, photo_id: str | None, from_library: bool) -> str:
    target = f"фото {photo_id}" if photo_id else "фото (не указано)"
    if from_library:
        return (
            f"резюме {resume_id}: {target} — открыть вьюер, more-меню "
            "«Действия с фото», пункт «Удалить», confirm-диалог «Удалить "
            "фото?» (необратимо удаляет фото из библиотеки и из ВСЕХ резюме, "
            "где оно установлено)"
        )
    return (
        f"резюме {resume_id}: {target} — открыть вьюер, more-меню «Действия "
        "с фото», пункт «Скрыть фото из резюме» (снимает фото только с этого "
        "резюме; фото остаётся в библиотеке)"
    )


def _open_more_menu(page: Page) -> tuple[tuple[MenuAction, ...] | None, str]:
    """Открыть more-меню «Действия с фото» и снять инвентарь пунктов.

    Read-only: и клик по ``photo-viewer-more``, и сама drop-панель ничего
    не мутируют. Маркер открытого меню — видимость пункта «Удалить»:
    пункты action-* присутствуют в DOM только при открытой панели.
    Возвращает ``(items, "")`` либо ``(None, reason)``.
    """
    more = page.locator(RESUME_PHOTO_VIEWER_MORE).first
    try:
        more.wait_for(state="attached", timeout=_MENU_WAIT_TIMEOUT_MS)
        more.click(timeout=_MENU_CLICK_TIMEOUT_MS)
    except PlaywrightError as exc:
        # Позиционный клик в NavBar может не пройти (detached-геометрия,
        # #955) — активация dispatch_event, как у assign/close в #953.
        # Но клик, упавший по таймауту, мог дойти с опозданием: панель —
        # toggle, и повторная активация закрыла бы только что открывшееся
        # меню, дав вводящий в заблуждение отказ «меню не открылось»
        # (находка cycle-review PR #973). Перед повтором проверяем, не
        # открылась ли панель уже.
        if page.locator(RESUME_PHOTO_VIEWER_ACTION_DELETE).count() > 0:
            print("[INFO] more: панель открылась после таймаута клика — повтор не нужен")
        else:
            try:
                more.dispatch_event("click")
            except PlaywrightError as dispatch_exc:
                return None, f"клик «Действия с фото» не удался: {exc}; dispatch: {dispatch_exc}"
    try:
        page.locator(RESUME_PHOTO_VIEWER_ACTION_DELETE).first.wait_for(
            state="visible", timeout=_MENU_WAIT_TIMEOUT_MS
        )
    except PlaywrightError as exc:
        dump_path = dump_page_html(page, "photo_more_menu_missing")
        reason = f"more-меню не открылось (пункт «Удалить» не появился): {exc}"
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return None, reason
    try:
        raw = page.evaluate(_MENU_ACTIONS_JS) or []
    except PlaywrightError as exc:
        return None, f"инвентарь more-меню не прочитан: {exc}"
    return tuple(MenuAction(qa=str(i.get("qa", "")), text=str(i.get("text", ""))) for i in raw), ""


def delete_photo_on_hh(
    page: Page,
    resume,
    photo_id: str | None,
    from_library: bool,
    dry_run: bool,
    *,
    before_click=None,
) -> DeletePhotoResult:
    """Скрыть фото из резюме (или удалить из библиотеки ``from_library``).

    ``before_click`` (seam #476) вызывается ровно один раз и строго до
    первой мутации: для скрытия — перед кликом пункта hide (клик и есть
    точка невозврата), для удаления — перед кликом confirm-кнопки диалога.
    Dry-run — read-only: инвентарь библиотеки и пунктов more-меню, ни один
    пункт меню не кликается.
    """
    action = DELETE_ACTION if from_library else HIDE_ACTION
    goto_hh(page, resume.resume_url)
    require_authenticated_page(page)
    dismiss_cookie_banner(page)
    fail = _hydrate_and_open_viewer(page)
    if fail is not None:
        return DeletePhotoResult(reason=fail.reason, action=action)
    state = _read_viewer_state(page)
    if state is None:
        dump_path = dump_page_html(page, "photo_delete_no_viewer")
        reason = "вьюер фото не подтверждён после карандаша (корень MediaViewer не найден)"
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return DeletePhotoResult(reason=reason, action=action)
    switched = state
    if photo_id:
        switched, switch_error = switch_viewer_photo(page, state, photo_id)
        if switched is None:
            return DeletePhotoResult(reason=switch_error, action=action, photos=state.photos)
    if dry_run:
        items, menu_error = _open_more_menu(page)
        if items is None:
            return DeletePhotoResult(reason=menu_error, action=action, photos=switched.photos)
        return DeletePhotoResult(
            success=True,
            reason=delete_photo_plan(resume.id, photo_id, from_library),
            action=action,
            photos=switched.photos,
            menu_actions=items,
        )
    if not photo_id:
        return DeletePhotoResult(
            reason="боевой режим требует --photo-id (идентификатор из dry-run инвентаря)",
            action=action,
        )
    if from_library:
        return _delete_from_library(page, resume, photo_id, before_click)
    return _hide_from_resume(page, resume, switched, photo_id, before_click)


def _hide_from_resume(
    page: Page,
    resume,
    switched: ViewerState,
    photo_id: str,
    before_click,
) -> DeletePhotoResult:
    """Боевой путь скрытия: пункт hide кликается СРАЗУ как мутация (см. модуль)."""
    # Пункт hide рендерится только для назначенного текущему резюме фото —
    # маркер assigned закрывает и это, и «скрывать нечего» ДО открытия меню.
    if not switched.assigned:
        return DeletePhotoResult(
            reason=(
                f"photo {photo_id} не назначен резюме {resume.id} — скрывать "
                "нечего (маркер «Установлено в резюме» отсутствует)"
            ),
            action=HIDE_ACTION,
        )
    items, menu_error = _open_more_menu(page)
    if items is None:
        return DeletePhotoResult(reason=menu_error, action=HIDE_ACTION)
    if page.locator(RESUME_PHOTO_VIEWER_ACTION_HIDE).count() == 0:
        return DeletePhotoResult(
            reason=("пункт «Скрыть фото из резюме» не подтверждён в more-меню — запись запрещена"),
            action=HIDE_ACTION,
        )
    if before_click is not None:
        before_click()
    try:
        page.locator(RESUME_PHOTO_VIEWER_ACTION_HIDE).first.click(timeout=_MENU_CLICK_TIMEOUT_MS)
    except PlaywrightError as exc:
        dump_path = dump_page_html(page, "photo_hide_click_uncertain")
        reason = f"клик «Скрыть фото из резюме» не удался (мог уйти): {exc}"
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return DeletePhotoResult(reason=reason, uncertain=True, action=HIDE_ACTION)
    page.wait_for_timeout(_READBACK_SETTLE_MS)
    confirmed, readback_reason = _readback_photo_persisted(page, resume.resume_url)
    if confirmed is False:
        return DeletePhotoResult(
            success=True,
            reason=(
                f"фото {photo_id} скрыто из резюме {resume.id} (подтверждено "
                f"плейсхолдером «фото нет» после перезагрузки); фото осталось "
                "в библиотеке — вернуть можно select-photo"
            ),
            action=HIDE_ACTION,
        )
    if confirmed is True:
        return DeletePhotoResult(
            reason=(
                "скрытие отправлено, но readback показывает фото на месте; "
                f"проверьте резюме на hh.ru вручную ({readback_reason})"
            ),
            uncertain=True,
            action=HIDE_ACTION,
        )
    return DeletePhotoResult(
        reason=f"скрытие отправлено, но readback не выполнен: {readback_reason}",
        uncertain=True,
        action=HIDE_ACTION,
    )


def _delete_from_library(
    page: Page,
    resume,
    photo_id: str,
    before_click,
) -> DeletePhotoResult:
    """Боевой путь удаления: confirm-кнопка — единственная мутация (см. модуль)."""
    items, menu_error = _open_more_menu(page)
    if items is None:
        return DeletePhotoResult(reason=menu_error, action=DELETE_ACTION)
    try:
        delete_item = page.locator(RESUME_PHOTO_VIEWER_ACTION_DELETE).first
        delete_item.wait_for(state="visible", timeout=_MENU_WAIT_TIMEOUT_MS)
        delete_item.click(timeout=_MENU_CLICK_TIMEOUT_MS)
    except PlaywrightError as exc:
        # Клик по пункту открывает диалог, а не мутирует (живой факт
        # 2026-09-05) — чистый fail, повтор разрешён без reconciliation.
        return DeletePhotoResult(
            reason=f"клик по пункту «Удалить» не удался: {exc}", action=DELETE_ACTION
        )
    try:
        # Диалог рендерится React-ом после клика: строгая проверка до
        # ожидания увидела бы count=0 («commit не значит отрисовано»).
        page.locator(RESUME_PHOTO_VIEWER_DELETE_DIALOG).first.wait_for(
            state="visible", timeout=_DIALOG_WAIT_TIMEOUT_MS
        )
    except PlaywrightError as exc:
        dump_path = dump_page_html(page, "photo_delete_no_dialog")
        reason = (
            f"confirm-диалог удаления не открылся после клика по пункту (мутации не было): {exc}"
        )
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return DeletePhotoResult(reason=reason, action=DELETE_ACTION)
    try:
        confirm = page.locator(RESUME_PHOTO_VIEWER_DELETE_CONFIRM)
        if confirm.count() != 1:
            return DeletePhotoResult(
                reason=(
                    "кнопка подтверждения удаления не подтверждена однозначно "
                    f"(совпадений: {confirm.count()}); запись запрещена"
                ),
                action=DELETE_ACTION,
            )
        confirm.first.wait_for(state="visible", timeout=_MENU_WAIT_TIMEOUT_MS)
    except PlaywrightError as exc:
        return DeletePhotoResult(
            reason=f"кнопка подтверждения удаления не появилась: {exc}",
            action=DELETE_ACTION,
        )
    if before_click is not None:
        before_click()
    try:
        confirm.first.click(timeout=_MENU_CLICK_TIMEOUT_MS)
    except PlaywrightError as exc:
        return DeletePhotoResult(
            reason=f"клик подтверждения не удался (удаление могло уйти): {exc}",
            uncertain=True,
            action=DELETE_ACTION,
        )
    # Точка невозврата пройдена. Закрытие диалога — переходный сигнал, не
    # доказательство: решает readback заново открытого вьюера ниже.
    try:
        page.locator(RESUME_PHOTO_VIEWER_DELETE_DIALOG).first.wait_for(
            state="detached", timeout=_DIALOG_GONE_TIMEOUT_MS
        )
    except PlaywrightError:
        pass
    page.wait_for_timeout(_READBACK_SETTLE_MS)
    try:
        goto_hh(page, resume.resume_url)
    except PlaywrightError as exc:
        return DeletePhotoResult(
            reason=f"удаление отправлено, но страница резюме не перечиталась: {exc}",
            uncertain=True,
            action=DELETE_ACTION,
        )
    require_authenticated_page(page)
    dismiss_cookie_banner(page)
    fail = _hydrate_and_open_viewer(page)
    if fail is not None:
        return DeletePhotoResult(
            reason=f"удаление отправлено, но вьюер не переоткрыт для readback: {fail.reason}",
            uncertain=True,
            action=DELETE_ACTION,
        )
    state = _read_viewer_state(page)
    if state is None:
        dump_path = dump_page_html(page, "photo_delete_readback_uncertain")
        reason = "удаление отправлено, но лента библиотеки не прочитана при readback"
        if dump_path is not None:
            reason += f"; дамп: {dump_path}"
        return DeletePhotoResult(reason=reason, uncertain=True, action=DELETE_ACTION)
    if photo_id in state.thumb_ids:
        return DeletePhotoResult(
            reason=(
                f"подтверждение отправлено, но photo {photo_id} всё ещё в "
                "библиотеке; проверьте hh.ru вручную (reconciliation по "
                "протоколу CLAUDE.md)"
            ),
            uncertain=True,
            action=DELETE_ACTION,
        )
    return DeletePhotoResult(
        success=True,
        reason=(
            f"фото {photo_id} удалено из библиотеки (в ленте осталось "
            f"{len(state.photos)}; подтверждено переоткрытием вьюера)"
        ),
        action=DELETE_ACTION,
    )

"""Загрузка изображения в галерею портфолио аккаунта (/applicant/gallery).

Граница браузерных действий: файл передаётся в скрытый file-input рядом с
кнопкой «Добавить работу» (``add-image-PORTFOLIO``) — тот же UI-механизм,
что у человека при выборе файла, без внутренних endpoint'ов hh.ru.

Селекторы с живого census 2026-09-12/14 (ишью #1121):
- ``add-image-PORTFOLIO`` — кнопка «Добавить работу» на /applicant/gallery;
- ``gallery-image-{photo_id}`` — карточки изображений галереи;
- ``gallery-item-delete-RESUME_PHOTO`` — удаление (не используется здесь).

File-input в DOM НЕ существует до клика; клик по «Добавить работу» открывает
filechooser ИЛИ модалку загрузки со своим input (оба shape поддерживаются,
см. upload_portfolio_image).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import (
    PageStateIndeterminate,
    goto_hh,
    has_login_form,
    wait_for_react_hydration,
)

logger = logging.getLogger("hhru_bot.portfolio_upload")

PORTFOLIO_ADD_BUTTON = "[data-qa='add-image-PORTFOLIO']"
GALLERY_IMAGE_QA = "gallery-image-{photo_id}"

# Бюджет открытия файлового диалога после клика (магриттовый клик может
# ждать гидрации — «commit не значит отрисовано»).
FORM_TIMEOUT_MS = 10_000
CHOOSER_TIMEOUT_MS = 3_000

# После set_input_files hh.ru грузит файл и отрисовывает новую карточку
# галереи; потолок с запасом на медленную сеть (консервативно, как в
# resume_photo: живой хелп-текст лимитов не подтверждён).
UPLOAD_TIMEOUT_MS = 60_000

# Инвентарь id карточек галереи ДО и ПОСЛЕ загрузки — единственный
# подтверждённый маркер успеха: новая карточка gallery-image-{id}.
_INVENTORY_JS = """() => {
  const ids = [];
  for (const img of document.querySelectorAll("img[class*='gallery-image']")) {
    const qa = img.getAttribute('data-qa') || '';
    const m = qa.match(/^gallery-image-(\\d+)$/);
    if (m) ids.push(m[1]);
  }
  return ids;
}"""


@dataclass(frozen=True)
class PortfolioUploadResult:
    success: bool
    photo_id: str = ""
    reason: str = ""
    uncertain: bool = False


def _inventory(page: Page) -> list[str]:
    return page.evaluate(_INVENTORY_JS)


def inspect_gallery_upload(page: Page) -> dict[str, object]:
    """Read-only осмотр мишени загрузки: dry-run и pre-flight боя."""
    goto_hh(page, "https://hh.ru/applicant/gallery")
    if has_login_form(page):
        raise PageStateIndeterminate({"state": "login_form", "reason": "hh.ru показал форму входа"})
    add_button = page.locator(PORTFOLIO_ADD_BUTTON)
    return {
        "add_button_count": add_button.count(),
        "existing_ids": _inventory(page),
    }


def _state_dict(state: dict[str, object]) -> tuple[int, list[str]]:
    """Типизированный доступ к полю осмотра (Pyright-friendly)."""
    return (
        int(state["add_button_count"]),  # type: ignore[arg-type]
        list(state["existing_ids"]),  # type: ignore[arg-type]
    )


def upload_portfolio_image(page: Page, photo: Path, *, dry_run: bool) -> PortfolioUploadResult:
    """Загрузить ``photo`` в галерею портфолио; успех — только новая карточка.

    Живой факт 2026-09-14 (dry-run): file-input в DOM галереи НЕ существует
    до клика — «Добавить работу» создаёт его в обработчике, поэтому файл
    передаётся через page.expect_file_chooser вокруг UI-клика по кнопке
    (автоматизация штатного диалога выбора файла, не внутренний API).
    """
    try:
        state = inspect_gallery_upload(page)
    except PageStateIndeterminate as exc:
        return PortfolioUploadResult(False, reason=str(exc))
    add_button_count, existing_ids = _state_dict(state)
    if dry_run:
        return PortfolioUploadResult(
            True,
            reason=(
                f"add-image-PORTFOLIO={add_button_count}, карточек в галерее: {len(existing_ids)}"
            ),
        )
    if add_button_count != 1:
        return PortfolioUploadResult(
            False,
            reason=f"кнопка «Добавить работу» не найдена однозначно ({add_button_count})",
        )
    before = set(existing_ids)
    add_button = page.locator(PORTFOLIO_ADD_BUTTON)
    try:
        # Бой 2026-09-14: клик по SSR-кнопке до гидрации теряется молча
        # («visible != гидратирован») — ждём гидрации контрола обязательно.
        wait_for_react_hydration(page, PORTFOLIO_ADD_BUTTON, timeout_ms=FORM_TIMEOUT_MS)
        # Два shape запуска выбора файла: кнопка может открыть filechooser
        # сразу ИЛИ модалку загрузки со своим input[type=file]. Ждём chooser
        # коротким бюджетом, иначе — модалку.
        chooser_error: PlaywrightError | None = None
        file_chooser = None
        try:
            with page.expect_file_chooser(timeout=CHOOSER_TIMEOUT_MS) as chooser_info:
                add_button.click()
            file_chooser = chooser_info.value
        except PlaywrightError as exc:
            # Chooser не открылся — пробуем второй shape: модалку загрузки.
            chooser_error = exc
        if file_chooser is not None:
            # Chooser открылся; ошибка set_files — честная причина, а не
            # «chooser не открылся» (cycle-review PR #1128).
            try:
                file_chooser.set_files(str(photo))
            except PlaywrightError as exc:
                return PortfolioUploadResult(False, reason=f"файл не передан в chooser: {exc}")
        else:
            modal_input = page.locator("[data-qa='modal-overlay'] input[type='file']")
            try:
                modal_input.first.wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
            except PlaywrightError as modal_exc:
                # Диагностика по живому DOM: что клик реально отрисовал
                # (census-принцип — не гадать по дампу).
                seen = page.evaluate(
                    """() => {
                      const out = {overlays: 0, inputs: [], buttons: []};
                      out.overlays =
                        document.querySelectorAll("[data-qa='modal-overlay']").length;
                      for (const i of document.querySelectorAll("input")) {
                        out.inputs.push(i.type + '|' + (i.className||'').slice(0,60));
                      }
                      for (const b of document.querySelectorAll(
                        "[data-qa^='add-image'], [data-qa*='upload'], [data-qa*='Upload']"
                      )) {
                        out.buttons.push(b.getAttribute('data-qa'));
                      }
                      return out;
                    }"""
                )
                return PortfolioUploadResult(
                    False,
                    reason=(
                        "после клика ни filechooser, ни input в модалке: "
                        f"{chooser_error}; {modal_exc}; DOM: {seen}"
                    ),
                )
            if modal_input.count() != 1:
                return PortfolioUploadResult(
                    False,
                    reason=(
                        f"input[type=file] в модалке загрузки не уникален "
                        f"({modal_input.count()}) — обновите селектор по живому DOM"
                    ),
                )
            modal_input.set_input_files(str(photo))
    except PlaywrightError as exc:
        return PortfolioUploadResult(False, reason=f"файл не передан: {exc}")
    try:
        page.wait_for_function(
            """(before) => {
              const ids = [];
              for (const img of document.querySelectorAll("img[class*='gallery-image']")) {
                const m = (img.getAttribute('data-qa') || '').match(/^gallery-image-(\\d+)$/);
                if (m) ids.push(m[1]);
              }
              return ids.some((id) => !before.includes(id));
            }""",
            arg=sorted(before),
            timeout=UPLOAD_TIMEOUT_MS,
        )
    except PlaywrightError:
        # set_input_files уже ушёл: локальный таймаут не доказывает, что
        # загрузки нет (тот же fail-closed принцип, что #176/#207).
        return PortfolioUploadResult(
            False,
            reason=(
                "новая карточка галереи не появилась за бюджет; проверьте "
                "/applicant/gallery вручную — upload мог дойти (модалка crop?)"
            ),
            uncertain=True,
        )
    after = _inventory(page)
    new_ids = [photo_id for photo_id in after if photo_id not in before]
    if len(new_ids) != 1:
        return PortfolioUploadResult(
            False,
            reason=f"неоднозначный результат загрузки: новые карточки {new_ids}",
            uncertain=True,
        )
    return PortfolioUploadResult(True, photo_id=new_ids[0])

"""CLI-владение wizard-экранами черновика: educations, keyskills, experience.

Живой факт 2026-09-06 (#1010, прогон #865/#1009): флаг
``nextIncompleteScreenId`` двигается ТОЛЬКО сабмитом экранов визарда —
данные, полные сами по себе, флаг не снимают (случай common: экран был
предзаполнен, но флаг стоял до «Сохранить и продолжить»). Клик «Сохранить и
продолжить» (resume-profile-next-screen) на ПОСЛЕДНЕМ незакрытом экране hh.ru
отвечает публикацией резюме (ветка #900, подтверждена живьём) — окна между
«все экраны закрыты» и «опубликовано» у wizard-черновика нет. Поэтому каждый
боевой клик этого модуля потенциально публикует резюме: командный слой
требует явного --allow-auto-publish.

Экраны common и professional_role здесь не сабмятся: у них собственные
CLI-владельцы (``hhru common``, ``hhru resume-position``), и отказ называет
владельца, а не молча берёт их работу на себя.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import (
    HH_BASE_URL,
    dismiss_cookie_banner,
    dump_page_html,
    goto_hh,
    open_confirmed_resume,
    require_authenticated_page,
    wait_for_react_hydration,
)
from .config import ResumeConfig
from .resume_state import ResumeState, is_published, parse_resume_state
from .selector_groups.resume_page import RESUME_CREATION_NEXT

WIZARD_BASE_PATH = "/profile/resume"

# Экраны, которые этот модуль умеет сабмитить. common/professional_role —
# у своих владельцев; всё остальное — честный отказ без владельца.
SUPPORTED_SCREENS = ("educations", "keyskills", "experience")

# #1012: hh.ru публикует черновик сам ровно на ПОСЛЕДНЕМ незакрытом экране
# (#900). Подтверждено двумя независимыми прогонами: #1009 («Дворник-бригадир»)
# и «Повар» 2026-09-06 — оба прошли educations/keyskills без публикации,
# автопубликация случилась на experience. Порядок экранов стабилен:
# common → educations → keyskills → experience. Если hh.ru добавит экран
# после experience, прогноз сломается в безопасную сторону: readback
# wizard-next печатает автопубликацию фактом, а флаг просто не понадобился.
PUBLISHABLE_SCREENS = (SUPPORTED_SCREENS[-1],)

SCREEN_OWNERS = {
    "common": "hhru common --resume <id> --force",
    "professional_role": "hhru resume-position --resume <id> …",
}

# Бюджет перехода после NEXT — тот же защищённый клик, что у common
# (common.py: исход решает wait_for_url, не сам click()).
_SCREEN_NAV_TIMEOUT_MS = 30_000
# #991: окно гидратации NEXT перед кликом; две попытки, как в save_common.
_HYDRATION_TIMEOUT_MS = 15_000


class WizardScreenRefused(RuntimeError):
    """Fail-closed отказ ДО мутирующего клика (клик не отправлялся)."""


@dataclass
class WizardAdvanceResult:
    screen: str
    success: bool
    reason: str
    acted: bool = False
    uncertain: bool = False


def screen_path(screen: str) -> str:
    return f"{WIZARD_BASE_PATH}/{screen}"


def read_resume_state(page: Page, resume_id: str) -> ResumeState:
    """Identity-bound состояние резюме со страницы /resume/<id> (#225)."""
    open_confirmed_resume(page, resume_id)
    return parse_resume_state(page.content(), resume_id)


def is_publishing_screen(target: str) -> bool:
    """Прогноз #1012: сабмит этого экрана опубликует черновик (#900)."""
    return target in PUBLISHABLE_SCREENS


def resolve_target_screen(state: ResumeState, requested: str | None) -> str:
    """Выбрать экран для сабмита; любой тупик — WizardScreenRefused.

    ``requested=None`` означает «текущий незавершённый экран». Явный
    ``--screen`` расходится с ним только при рассинхроне состояния —
    сабмит чужого экрана вслепую не выполняется.
    """
    if is_published(state):
        raise WizardScreenRefused(
            "резюме уже опубликовано: wizard-экраны существуют только у черновиков (#995)"
        )
    current = state.next_incomplete_screen_id
    if not current:
        raise WizardScreenRefused(
            "nextIncompleteScreenId пуст — незавершённых экранов нет; "
            "публикация — отдельное действие: hhru publish-resume --resume <id> --dry-run"
        )
    if requested and requested != current:
        raise WizardScreenRefused(
            f"визард ждёт экран «{current}», а запрошен «{requested}» — "
            "экраны сабмятся в порядке незавершённости"
        )
    target = requested or current
    if target in SCREEN_OWNERS:
        raise WizardScreenRefused(f"экран «{target}» — владелец: {SCREEN_OWNERS[target]}")
    if target not in SUPPORTED_SCREENS:
        raise WizardScreenRefused(
            f"у экрана «{target}» нет CLI-владельца; поддерживаются: "
            + ", ".join(SUPPORTED_SCREENS)
        )
    return target


def _open_screen(page: Page, resume_id: str, target: str) -> None:
    """Открыть identity-bound экран; ушедший редирект — честный отказ (#999)."""
    goto_hh(page, f"{HH_BASE_URL}{screen_path(target)}?resume={resume_id}")
    require_authenticated_page(page)
    route = urlsplit(page.url)
    if route.path != screen_path(target) or parse_qs(route.query).get("resume") != [resume_id]:
        suffix = route.path.rstrip("/").rsplit("/", 1)[-1]
        raise WizardScreenRefused(
            f"экран «{target}» не открыт: визард стоит на «{suffix}» — hh.ru мог "
            "автопродвинуть визард (#999); состояние: hhru publish-resume --resume "
            "<id> --dry-run"
        )


def inspect_wizard_screen(page: Page, resume_id: str, target: str) -> str:
    """Read-only сверка экрана для --dry-run: identity + ровно одна NEXT."""
    _open_screen(page, resume_id, target)
    button = page.locator(RESUME_CREATION_NEXT)
    count = button.count()
    if count != 1:
        raise WizardScreenRefused(
            f"кнопка «Сохранить и продолжить» найдена {count} раз — экран не опознан"
        )
    return (button.first.inner_text() or "").strip() or "Сохранить и продолжить"


def submit_wizard_screen(
    page: Page,
    resume: ResumeConfig,
    target: str,
    *,
    before_click: Callable[[], None] | None = None,
) -> WizardAdvanceResult:
    """Сабмит одного экрана кликом «Сохранить и продолжить».

    Гидрационный гейт #991 стоит ДО ``before_click``: пока React не привязан
    (#858), клик теряется молча, и это честный failed («клик не отправлялся»),
    а не uncertain. Исход самого клика решает ``wait_for_url`` (уход с пути
    экрана): падение click() при состоявшемся переходе — не ошибка (#913),
    а непереход в пределах бюджета — uncertain (#176).
    """
    _open_screen(page, resume.resume_id, target)
    button = page.locator(RESUME_CREATION_NEXT)
    if button.count() != 1:
        raise WizardScreenRefused(
            f"кнопка «Сохранить и продолжить» найдена {button.count()} раз — экран не опознан"
        )
    dismiss_cookie_banner(page)
    hydrated = False
    for _attempt in range(2):
        if wait_for_react_hydration(page, RESUME_CREATION_NEXT, timeout_ms=_HYDRATION_TIMEOUT_MS):
            hydrated = True
            break
    if not hydrated:
        # Дамп покажет, жива ли форма и есть ли form-helper-error — иначе
        # «не гидратирован» неотличим от «страница умерла».
        try:
            dump_page_html(page, "wizard_next_failure")
        except Exception:  # noqa: BLE001 — диагностика не должна маскировать исход
            pass
        return WizardAdvanceResult(
            target,
            False,
            f"NEXT не гидратирован за {2 * _HYDRATION_TIMEOUT_MS // 1000}с — "
            "клик не отправлялся (мутации нет)",
        )
    if before_click is not None:
        before_click()
    click_error: str | None = None
    try:
        button.click()
    except PlaywrightError as exc:
        click_error = str(exc)
    try:
        page.wait_for_url(
            lambda url: urlsplit(str(url)).path != screen_path(target),
            wait_until="commit",
            timeout=_SCREEN_NAV_TIMEOUT_MS,
        )
    except PlaywrightError as exc:
        # Клик мог уйти на hh.ru (#176) — дампим экран: в нём виден
        # text form-helper-error, которым hh.ru объясняет отказ валидации.
        try:
            dump_page_html(page, "wizard_next_failure")
        except Exception:  # noqa: BLE001 — диагностика не должна заменять исходную ошибку
            pass
        # #990: текст падения клика сохраняется — иначе «дошёл ли клик»
        # недиагностируем (см. save_common).
        reason = f"переход с экрана «{target}» не подтверждён: {exc}"
        if click_error is not None:
            reason += f"; ошибка клика: {click_error[:300]}"
        return WizardAdvanceResult(target, False, reason, acted=True, uncertain=True)
    return WizardAdvanceResult(target, True, f"экран «{target}» подтверждён", acted=True)

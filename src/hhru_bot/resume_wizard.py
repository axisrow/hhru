"""CLI-владение wizard-экранами черновика: educations, keyskills, skill_levels, experience.

Живой факт 2026-09-06 (#1010, прогон #865/#1009): флаг
``nextIncompleteScreenId`` двигается ТОЛЬКО сабмитом экранов визарда —
данные, полные сами по себе, флаг не снимают (случай common: экран был
предзаполнен, но флаг стоял до «Сохранить и продолжить»). Клик «Сохранить и
продолжить» (resume-profile-next-screen) на ПОСЛЕДНЕМ незакрытом экране hh.ru
отвечает публикацией резюме (ветка #900, подтверждена живьём) — окна между
«все экраны закрыты» и «опубликовано» у wizard-черновика нет. Поэтому каждый
боевой клик этого модуля потенциально публикует резюме: командный слой
требует явного --allow-auto-publish.

Экран skill_levels — единственный, живущий НЕ на маршруте /profile/resume/*:
прямой GET /profile/resume/skill_levels рендерит пустой shell без wizard-хрома
(census 2026-09-07, оба регистра имени). Реальный экран — редактор уровней
/resume/edit/{id}/skillsLevels?fromBlock=keySkills (#813, подтверждён живьём),
куда с карточки черновика ведёт собственная кнопка hh.ru «Указать уровни»;
его Save и сабмитит экран.

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
from .selector_groups.resume_page import RESUME_CREATION_NEXT, RESUME_PARTIAL_EDIT_SAVE

WIZARD_BASE_PATH = "/profile/resume"

# Экраны, которые этот модуль умеет сабмитить. common/professional_role —
# у своих владельцев; всё остальное — честный отказ без владельца.
# skill_levels встаёт между keyskills и experience ТОЛЬКО у черновиков
# с навыками: прогоны #1012 «Повар» (без навыков) его не видели, живой
# прогон 2026-09-07 «Сантехник» после edit-skills — получил. experience
# остаётся последним, прогноз публикации #1012 не меняется.
SUPPORTED_SCREENS = ("educations", "keyskills", "skill_levels", "experience")

# #1012: hh.ru публикует черновик сам ровно на ПОСЛЕДНЕМ незакрытом экране
# (#900). Подтверждено двумя независимыми прогонами: #1009 («Дворник-бригадир»)
# и «Повар» 2026-09-06 — оба прошли educations/keyskills без публикации,
# автопубликация случилась на experience. Порядок экранов стабилен:
# common → educations → keyskills → (skill_levels — только у черновиков
# с навыками, живой факт 2026-09-07) → experience. Если hh.ru добавит экран
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


_SKILL_LEVELS_SCREEN = "skill_levels"
_SKILL_LEVELS_PATH = "/resume/edit/{resume_id}/skillsLevels"


def _screen_url(screen: str, resume_id: str) -> str:
    """URL открытия экрана: wizard-маршрут или редактор уровней для skill_levels."""
    if screen == _SKILL_LEVELS_SCREEN:
        return f"{HH_BASE_URL}{_SKILL_LEVELS_PATH.format(resume_id=resume_id)}?fromBlock=keySkills"
    return f"{HH_BASE_URL}{screen_path(screen)}?resume={resume_id}"


def _screen_route_path(screen: str, resume_id: str) -> str:
    """Path-маршрут экрана — предикат ухода с экрана в wait_for_url."""
    if screen == _SKILL_LEVELS_SCREEN:
        return _SKILL_LEVELS_PATH.format(resume_id=resume_id)
    return screen_path(screen)


def _screen_submit_button(screen: str) -> str:
    """Селектор сабмита: NEXT визарда у wizard-экранов, Save редактора (#813)
    у skill_levels — тот же RESUME_PARTIAL_EDIT_SAVE, что кликает skills.py."""
    if screen == _SKILL_LEVELS_SCREEN:
        return RESUME_PARTIAL_EDIT_SAVE
    return RESUME_CREATION_NEXT


def _screen_location_ok(screen: str, resume_id: str, route) -> bool:
    """Identity-проверка открытого маршрута (#999). У wizard-экранов resume_id
    живёт в query (?resume=), у редактора skill_levels — в самом пути."""
    if screen == _SKILL_LEVELS_SCREEN:
        return route.path == _SKILL_LEVELS_PATH.format(resume_id=resume_id)
    return route.path == screen_path(screen) and parse_qs(route.query).get("resume") == [resume_id]


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
    goto_hh(page, _screen_url(target, resume_id))
    require_authenticated_page(page)
    route = urlsplit(page.url)
    if not _screen_location_ok(target, resume_id, route):
        suffix = route.path.rstrip("/").rsplit("/", 1)[-1]
        raise WizardScreenRefused(
            f"экран «{target}» не открыт: визард стоит на «{suffix}» — hh.ru мог "
            "автопродвинуть визард (#999); состояние: hhru publish-resume --resume "
            "<id> --dry-run"
        )


def inspect_wizard_screen(page: Page, resume_id: str, target: str) -> str:
    """Read-only сверка экрана для --dry-run: identity + ровно один сабмит."""
    _open_screen(page, resume_id, target)
    button = page.locator(_screen_submit_button(target))
    count = button.count()
    if count != 1:
        raise WizardScreenRefused(f"кнопка сабмита экрана найдена {count} раз — экран не опознан")
    return (button.first.inner_text() or "").strip() or "Сохранить и продолжить"


def submit_wizard_screen(
    page: Page,
    resume: ResumeConfig,
    target: str,
    *,
    before_click: Callable[[], None] | None = None,
) -> WizardAdvanceResult:
    """Сабмит одного экрана: NEXT визарда или Save редактора skill_levels.

    Гидрационный гейт #991 стоит ДО ``before_click``: пока React не привязан
    (#858), клик теряется молча, и это честный failed («клик не отправлялся»),
    а не uncertain. Исход самого клика решает ``wait_for_url`` (уход с пути
    экрана): падение click() при состоявшемся переходе — не ошибка (#913),
    а непереход в пределах бюджета — uncertain (#176). Для skill_levels
    переход — необходимое, но не достаточное условие: успех дополнительно
    доказывается identity-bound readback'ом флага (#1014, живой факт:
    Save редактора флаг не двигает).
    """
    _open_screen(page, resume.resume_id, target)
    submit_selector = _screen_submit_button(target)
    button = page.locator(submit_selector)
    if button.count() != 1:
        raise WizardScreenRefused(
            f"кнопка сабмита экрана найдена {button.count()} раз — экран не опознан"
        )
    dismiss_cookie_banner(page)
    hydrated = False
    for _attempt in range(2):
        if wait_for_react_hydration(page, submit_selector, timeout_ms=_HYDRATION_TIMEOUT_MS):
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
            lambda url: urlsplit(str(url)).path != _screen_route_path(target, resume.resume_id),
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
    if target == _SKILL_LEVELS_SCREEN:
        # v2 (#1014, живой факт 2026-09-07): Save редактора уровней кликается
        # и уходит с маршрута, но nextIncompleteScreenId не двигает. Успех
        # этого экрана доказывается только identity-bound readback'ом флага,
        # а не фактом перехода; непрошедший флаг — честный failed (исход
        # известен, не uncertain), повтор того же клика бессмыслен.
        after_state = read_resume_state(page, resume.resume_id)
        if after_state.next_incomplete_screen_id == _SKILL_LEVELS_SCREEN:
            return WizardAdvanceResult(
                target,
                False,
                "переход с редактора подтверждён, но экран «skill_levels» не закрыт: "
                "nextIncompleteScreenId не двигается — Save редактора (#813) "
                "wizard-флаг не сабмитит (#1014); настоящий сабмит, вероятно, "
                "за кнопкой «Указать уровни» на карточке резюме",
                acted=True,
            )
    return WizardAdvanceResult(target, True, f"экран «{target}» подтверждён", acted=True)

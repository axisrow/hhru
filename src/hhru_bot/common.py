"""UI editing of the simple fields on a resume's ``common`` screen (#876).

This module intentionally contains no transport code: navigation and saving are
performed only by the visible hh.ru form.  The selectors below are the
data-qa handles confirmed during the read-only common-screen probe.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import (
    HH_BASE_URL,
    PageStateIndeterminate,
    dismiss_cookie_banner,
    dump_page_html,
    goto_hh,
    labelled_field,
    optional_labelled_field,
    require_authenticated_page,
    wait_for_react_hydration,
)
from .config import ResumeConfig
from .selector_groups import account_profile
from .selector_groups.resume_page import RESUME_POSITION_DROPDOWN

FORM = account_profile.RESUME_COMMON_FORM
FIRST_NAME = account_profile.RESUME_COMMON_FIRST_NAME
LAST_NAME = account_profile.RESUME_COMMON_LAST_NAME
BIRTHDAY = account_profile.RESUME_COMMON_BIRTHDAY_DAY
GENDER = account_profile.RESUME_COMMON_GENDER_MALE
GENDER_FEMALE = account_profile.RESUME_COMMON_GENDER_FEMALE
PHONE = account_profile.RESUME_COMMON_PHONE
AREA = "[data-qa='resume-edit-area']"
METRO = "[data-qa='resume-edit-metro']"
CITIZENSHIP = "[data-qa='resume-edit-citizenship']"
TREE_MODAL = "[data-qa='tree-selector-modal']"
TREE_SEARCH = "[data-qa='tree-selector-search-input']"
TREE_OPTION = "[data-qa^='tree-selector-item tree-selector-item-'][data-qa*='tree-selector-child-']"
TREE_SUBMIT = "[data-qa='tree-selector-submit']"
WORK_TICKET = "Наличие трудовой книжки"
RELOCATION = "Готовность к переезду"
SCHEDULE = "График работы"
EMPLOYMENT = "Тип занятости"
WORK_FORMAT = "Формат работы"
BUSINESS_TRIP = "Готовность к командировкам"
SCHEDULE_LABELS = {
    "full_day": "Полный день",
    "shift": "Сменный график",
    "flexible": "Гибкий график",
    "remote": "Удалённая работа",
}
EMPLOYMENT_LABELS = {
    "full_time": "Постоянная работа",
    "part_time": "Подработка",
    "internship": "Стажировка",
    "volunteer": "Волонтёрство",
}
WORK_FORMAT_LABELS = {"office": "Офис", "hybrid": "Гибрид", "remote": "Удалённо"}
SAVE = account_profile.RESUME_COMMON_NEXT
CANCEL = account_profile.RESUME_COMMON_PREV
# #985: маршрут identity-bound экрана common визарда «Дополнить» —
# /profile/resume?resume=<id> редиректит сюда (подтверждено живым DOM
# 2026-09-06, read-only dump черновика-свидетеля #978).
COMMON_SCREEN_PATH = "/profile/resume/common"
# Бюджет перехода после «Сохранить и продолжить» — тот же защищённый NEXT-клик,
# что у визарда professional_role (resume_position #913): исход решает
# wait_for_url, а не сам click().
_COMMON_SCREEN_NAV_TIMEOUT_MS = 30_000
_WAIT_MS = 5_000
# #991: окно гидратации SAVE перед NEXT-кликом (одно ожидание). Две попытки
# подряд в save_common дают суммарные ~30с — сопоставимо с бюджетом навигации.
_SAVE_HYDRATION_TIMEOUT_MS = 15_000

# Поля, без которых hh.ru не пускает черновик дальше к публикации (#982).
# Авто-режим сохраняет предзаполненное только когда все они непусты.
# Города (area) в списке нет: живой дамп 2026-09-06 (черновик владельца,
# data/logs/common_failure_*.html) показывает, что этот shape экрана common
# поле города не рендерит вовсе — требовать его неоткуда.
REQUIRED_FIELDS = (
    "first_name",
    "last_name",
    "birthday",
    "gender",
    "phone",
    "citizenship",
)
# Состав даты рождения и гражданство на живом экране — magritte-combobox'ы
# внутри контейнеров с этими data-qa (тот же дамп): у activator нет собственного
# data-qa, читается его видимый текст.
BIRTHDAY_MONTH = "[data-qa='resume-profile-common-birthday-month-selector']"
BIRTHDAY_YEAR = "[data-qa='resume-profile-common-birthday-year-input']"
CITIZENSHIP_SELECTOR = "[data-qa='resume-profile-common-citizenship-selector']"


@dataclass(frozen=True)
class CommonValues:
    first_name: str | None = None
    last_name: str | None = None
    birthday: str | None = None
    gender: str | None = None
    phone: str | None = None
    area: str | None = None
    metro: list[str] | None = None
    citizenship: list[str] | None = None
    # #997: «Разрешение на работу» на wizard-shape — отдельный контрол
    # (WORK_PERMIT_WIZARD); display-only: в provided() НЕ входит, записи нет.
    work_permit: str | None = None
    work_ticket: str | None = None
    relocation: str | None = None
    schedule: list[str] | None = None
    employment: list[str] | None = None
    work_format: list[str] | None = None
    business_trip: str | None = None

    def provided(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "firstName": self.first_name,
                "lastName": self.last_name,
                "birthday": self.birthday,
                "gender": self.gender,
                "phone": self.phone,
                "area": self.area,
                "metro": self.metro,
                "citizenship": self.citizenship,
                "workTicket": self.work_ticket,
                "relocation": self.relocation,
                "schedule": self.schedule,
                "employment": self.employment,
                "work_format": self.work_format,
                "businessTrip": self.business_trip,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class CommonResult:
    success: bool
    reason: str
    acted: bool = False
    uncertain: bool = False


def _strict(page: Page, selector: str, label: str):
    loc = page.locator(selector)
    if loc.count() != 1:
        raise RuntimeError(f"поле {label} не подтверждено однозначно")
    return loc.first


def open_common_form(page: Page, resume: ResumeConfig):
    """Open and identity-bind the common editor; never guess from a redirect."""
    return _open_common_screen(page, resume.resume_id)


def _dump_and_raise(page: Page, message: str, cause: BaseException | None = None):
    """Дамп на отказе открытия common (#995): диагностика не должна
    заменять исходную ошибку — упавший дамп не съедает отказ."""
    try:
        dump_page_html(page, "common_open_failure")
    except Exception:  # noqa: BLE001 — см. докстринг
        pass
    raise RuntimeError(message) from cause


def _open_common_screen(page: Page, resume_id: str):
    goto_hh(page, f"{HH_BASE_URL}{account_profile.RESUME_COMMON_PATH}?resume={resume_id}")
    require_authenticated_page(page)
    # #995 (live 2026-09-06): редирект с /profile/resume?resume=… зависит от
    # состояния резюме — у ЧЕРНОВИКА он ведёт на /profile/resume/common
    # (визард), у ОПУБЛИКОВАННОГО — на /resume/{resume_id} (страница
    # просмотра; визанда common у published не существует, а старый
    # /applicant/resumes/edit/common отдаёт голый JSON). Дождаться
    # разрешения редиректа, прежде чем судить о форме.
    try:
        page.wait_for_url(
            lambda url: urlsplit(str(url)).path != account_profile.RESUME_COMMON_PATH,
            wait_until="commit",
            timeout=_COMMON_SCREEN_NAV_TIMEOUT_MS,
        )
    except PlaywrightError as exc:
        _dump_and_raise(
            page,
            "форма common не открылась: редирект с /profile/resume не разрешился "
            f"за {_COMMON_SCREEN_NAV_TIMEOUT_MS // 1000}с ({page.url})",
            cause=exc,
        )
    if urlsplit(page.url).path == f"/resume/{resume_id}":
        # Опубликованное резюме (#995): честная семантика вместо
        # «форма common не открылась».
        _dump_and_raise(
            page,
            "экран common визарда существует только у черновиков: резюме "
            "опубликовано, hh.ru редиректит на страницу просмотра — правки "
            "common через визард невозможны",
        )
    # Гонка «DCL ≠ отрисовано»: SPA-экран монтируется после domcontentloaded —
    # ждать монтирование (attached), а не судить по мгновенному count() (#995).
    editor = page.locator(FORM)
    try:
        editor.first.wait_for(state="attached", timeout=_WAIT_MS)
    except PlaywrightError as exc:
        _dump_and_raise(page, "форма common не открылась: экран не смонтировался", cause=exc)
    if editor.count() != 1:
        _dump_and_raise(page, "форма common не открылась: контейнер неоднозначен")
    route = urlsplit(page.url)
    if route.path != COMMON_SCREEN_PATH or parse_qs(route.query).get("resume") != [resume_id]:
        raise RuntimeError("форма common открыта не для запрошенного резюме")
    editor.first.wait_for(state="visible", timeout=_WAIT_MS)
    return editor


def _wizard_prefill_problems(page: Page) -> list[str]:
    """Незаполненные обязательные поля экрана common ДО клика (#985).

    Живой DOM 2026-09-06 (read-only dump черновика-свидетеля #978): у
    fresh-черновика экран common визарда УЖЕ предзаполнен hh.ru из профиля
    аккаунта (ФИО/телефон/ДР/пол/гражданство) — «заполнение» сводится к
    подтверждению экрана. Если какое-то поле в профиле отсутствует, клик
    «Сохранить и продолжить» не выполняется: вердикт честно остаётся
    draft_started, а не превращается в uncertain-окно после клика.
    Переиспользует чтение/признаки #982 (read_common + missing_required),
    а не второй перечень обязательных полей.
    """
    return missing_required(read_common(page))


def confirm_common_screen(
    page: Page,
    resume_id: str,
    *,
    before_click: Callable[[], None] | None = None,
) -> CommonResult:
    """Подтвердить экран common fresh-черновика — перевести draft_started
    дальше по пайплайну create→publish (#985).

    Открывает identity-bound экран, проверяет предзаполнение из профиля
    аккаунта (fail-closed ДО клика) и нажимает «Сохранить и продолжить» —
    мутирующий клик (NEXT визарда, см. #936/#913), поэтому ``before_click``
    ставится вызывающим кодом в DurableMutationAttempt-seam. Исход клика
    решает ``wait_for_url`` (уход с /profile/resume/common): сам click()
    может упасть при состоявшемся переходе (паттерн #913), а непереход в
    пределах бюджета — uncertain (клик мог уйти, #176).
    """
    _open_common_screen(page, resume_id)
    problems = _wizard_prefill_problems(page)
    if problems:
        return CommonResult(
            False,
            "экран common не предзаполнен профилем аккаунта: "
            + ", ".join(problems)
            + " — заполните вручную и повторите",
        )
    save = _strict(page, SAVE, "кнопка «Сохранить и продолжить» экрана common")
    # Баннер закрывается ДО резервирования uncertain-маркера — семантика seam
    # «маркер вплотную к мутирующему клику» (тот же порядок, что у
    # create_resume._click_one(before_click=...) и _click_wizard_next).
    dismiss_cookie_banner(page)
    if before_click is not None:
        before_click()
    try:
        save.click()
    except PlaywrightError:
        # Переход мог состояться (анимация модалки/оверлея перехватывает
        # pointer events, #913) — исход решает wait_for_url ниже.
        pass
    try:
        page.wait_for_url(
            lambda url: urlsplit(str(url)).path != COMMON_SCREEN_PATH,
            wait_until="commit",
            timeout=_COMMON_SCREEN_NAV_TIMEOUT_MS,
        )
    except PlaywrightError as exc:
        return CommonResult(False, f"переход с экрана common не подтверждён: {exc}", True, True)
    return CommonResult(True, "экран common подтверждён", True)


def read_common(page: Page) -> CommonValues:
    """Read only the fields owned by this slice from an already-open form.

    При любом отказе читатель дампирует HTML открытой формы (тот же
    best-effort-принцип, что у семейства ``_dump_*_failure``): отказы strict-
    чтения на fresh-черновиках — первый сигнал дрейфа DOM этой формы, без
    дампа они недиагностируемы.
    """
    try:
        return _read_common(page)
    except Exception:
        try:
            dump_page_html(page, "common_read_failure")
        except Exception:  # noqa: BLE001 — диагностика не должна заменять исходную ошибку
            pass
        raise


def _read_common(page: Page) -> CommonValues:
    def value(selector: str) -> str:
        loc = _strict(page, selector, selector)
        return loc.input_value().strip()

    def soft_value(selector: str, label: str) -> str:
        loc = page.locator(selector)
        count = loc.count()
        if count == 0:
            return ""
        if count > 1:
            raise PageStateIndeterminate(f"поле {label} не подтверждено однозначно")
        return loc.first.input_value().strip()

    def activator_value(selector: str, label: str) -> str:
        """Видимый текст magritte-combobox внутри контейнера с data-qa."""
        loc = page.locator(selector)
        count = loc.count()
        if count == 0:
            return ""
        if count > 1:
            raise PageStateIndeterminate(f"поле {label} не подтверждено однозначно")
        activator = loc.first.locator("[data-qa='magritte-select-activator']")
        source = activator.first if activator.count() >= 1 else loc.first
        return source.inner_text().replace(" ", " ").strip()

    def gender_value() -> str:
        male = page.locator(GENDER)
        female = page.locator(GENDER_FEMALE)
        # Radio-chips: значение — атрибут value у всех, состояние — checked.
        if male.count() == 1 and male.first.is_checked():
            return "male"
        if female.count() == 1 and female.first.is_checked():
            return "female"
        return ""

    def labelled_value(label: str):
        # Живой shape визарда (дамп 2026-09-06) не содержит полей условий
        # работы вовсе: отсутствующее поле это пустое значение, неоднозначное
        # — отказ через общий инвариант optional_labelled_field.
        field = optional_labelled_field(page, label)
        if field is None:
            return ""
        tag = field.evaluate("e=>e.tagName")
        if tag == "SELECT" and field.get_attribute("multiple") is not None:
            return field.evaluate("e=>Array.from(e.selectedOptions).map(o=>o.value)")
        if tag in ("INPUT", "TEXTAREA"):
            return field.input_value().strip()
        # Magritte may bind a label to a div/button trigger rather than an
        # input; its visible text is the readable state.
        return field.inner_text().strip()

    def work_permit_value():
        """#997: контрол WORK_PERMIT_WIZARD — «Разрешение на работу»
        (страна), это НЕ трудовая книжка; читается отдельным полем.
        Ревью #994: placeholder пустого селекта прочтётся как значение —
        чтение best-effort до живой фиксации."""
        activator = _wizard_ticket_activator(page)
        if activator is None:
            return None
        return activator.inner_text().strip() or None

    birthday = " ".join(
        part
        for part in (
            soft_value(BIRTHDAY, "birthday-day"),
            activator_value(BIRTHDAY_MONTH, "birthday-month"),
            activator_value(BIRTHDAY_YEAR, "birthday-year"),
        )
        if part
    )
    citizenship_text = activator_value(CITIZENSHIP_SELECTOR, "citizenship")
    return CommonValues(
        first_name=value(FIRST_NAME),
        last_name=value(LAST_NAME),
        birthday=birthday,
        gender=gender_value(),
        phone=value(PHONE),
        area=soft_value(AREA, "area"),
        metro=None,
        citizenship=[citizenship_text] if citizenship_text else None,
        work_ticket=labelled_value(WORK_TICKET),
        work_permit=work_permit_value(),
        relocation=labelled_value(RELOCATION),
        schedule=labelled_value(SCHEDULE),
        employment=labelled_value(EMPLOYMENT),
        work_format=labelled_value(WORK_FORMAT),
        business_trip=labelled_value(BUSINESS_TRIP),
    )


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return any(isinstance(item, str) and item.strip() for item in value)


def merge_prefilled(
    requested: CommonValues, current: CommonValues
) -> tuple[CommonValues, list[tuple[str, Any]]]:
    """Оставить из запрошенного только поля, пустые на форме (#982).

    hh.ru предзаполняет экран common из профиля аккаунта; предзаполненное —
    источник истины, CLI его не затирает. Возвращает значения для заполнения
    и перечень (поле, предзаполненное значение), который команда показывает
    владельцу.
    """
    keep: dict[str, Any] = {}
    skipped: list[tuple[str, Any]] = []
    for field in dataclasses.fields(CommonValues):
        name = field.name
        wanted = getattr(requested, name)
        if wanted is None:
            continue
        present = getattr(current, name)
        if _nonempty(present):
            skipped.append((name, present))
        else:
            keep[name] = wanted
    return CommonValues(**keep), skipped


def missing_required(current: CommonValues) -> list[str]:
    """Обязательные поля, оставшиеся пустыми после предзаполнения hh.ru."""
    return [name for name in REQUIRED_FIELDS if not _nonempty(getattr(current, name))]


def _set_tree(page: Page, trigger_selector: str, values: list[str], label: str) -> None:
    """Select exact leaves in a common-screen tree selector.

    A leaf can be rendered once per parent category.  Its complete data-qa is
    the identity, so repeated identical IDs are accepted while different IDs
    for one label are rejected.
    """
    trigger = _strict(page, trigger_selector, label)
    trigger.click()
    modal = page.locator(TREE_MODAL)
    search = modal.locator(TREE_SEARCH)
    submit = modal.locator(TREE_SUBMIT)
    if modal.count() != 1 or search.count() != 1 or submit.count() != 1:
        raise RuntimeError(f"панель выбора {label} не подтверждена")
    modal.first.wait_for(state="visible", timeout=_WAIT_MS)
    # The picker is multi-select and keeps its previous state between opens.
    # Clear each selected leaf by its stable id, not once per category row:
    # hh.ru may render the same leaf under several parent categories.
    # Keep the state probe independent from the option's compound data-qa
    # selector; hh.ru has used both a plain item class and the tree-item class
    # on selected rows across these pickers.
    selected = modal.locator("[aria-selected='true'][data-qa*='tree-selector-child-']")
    selected_ids = {
        selected.nth(index).get_attribute("data-qa") for index in range(selected.count())
    }
    for selected_id in selected_ids:
        if selected_id:
            modal.locator(f"[data-qa='{selected_id}']").first.click()

    for value in values:
        search.first.fill(value)
        option = modal.locator(TREE_OPTION).filter(has_text=re.compile(rf"^{re.escape(value)}$"))
        try:
            # fill starts an async React tree render; count() alone races it.
            option.first.wait_for(state="visible", timeout=_WAIT_MS)
        except PlaywrightError as exc:
            raise RuntimeError(f"{label} не найдено в дереве: {value}") from exc
        ids = {option.nth(index).get_attribute("data-qa") for index in range(option.count())}
        if not ids or len(ids) != 1:
            raise RuntimeError(f"вариант {label} не найден однозначно: {value}")
        option.first.click()
    submit.first.click()
    modal.first.wait_for(state="hidden", timeout=_WAIT_MS)


def apply_common(page: Page, values: CommonValues) -> None:
    """Fill explicit values only; all controls must resolve exactly once."""
    selectors = {
        "first_name": (FIRST_NAME, values.first_name),
        "last_name": (LAST_NAME, values.last_name),
        "birthday": (BIRTHDAY, values.birthday),
        "gender": (GENDER, values.gender),
        "phone": (PHONE, values.phone),
    }
    for name, (selector, value) in selectors.items():
        if value is not None:
            loc = _strict(page, selector, name)
            if name == "gender":
                loc.select_option(value)
            else:
                loc.fill(value)
    if values.area is not None:
        # #993: на экране common визарда черновика поле города не рендерится
        # вовсе (live 2026-09-05) — прежняя ошибка «не подтверждено
        # однозначно» не объясняла, что делать.
        if page.locator(AREA).count() == 0:
            raise PageStateIndeterminate(
                "поле «Город» не рендерится на экране common визарда черновика — "
                "укажите город в резюме вручную или на другом экране"
            )
        _set_tree(page, AREA, [values.area], "area")
    if values.metro is not None:
        _set_tree(page, METRO, values.metro, "metro")
    if values.citizenship is not None:
        _set_tree(page, CITIZENSHIP, values.citizenship, "citizenship")

    controls = (
        # #997: контрол WORK_PERMIT_WIZARD на визарде — «Разрешение на
        # работу» (значение-страна), НЕ «Наличие трудовой книжки»;
        # писать «Да/Нет» в него запрещено — см. _work_ticket_field.
        (WORK_TICKET, values.work_ticket, {"true": "Да", "false": "Нет"}),
        (
            RELOCATION,
            values.relocation,
            {
                "ready": "Готов к переезду",
                "consider": "Рассмотрю",
                "not_ready": "Не готов к переезду",
            },
        ),
        (BUSINESS_TRIP, values.business_trip, {"true": "Могу", "false": "Не могу"}),
    )
    for label, value, labels in controls:
        if value is not None:
            field = (
                _work_ticket_field(page) if label == WORK_TICKET else _condition_field(page, label)
            )
            _set_control(page, field, value, labels)
    for label, value, labels in (
        (SCHEDULE, values.schedule, SCHEDULE_LABELS),
        (EMPLOYMENT, values.employment, EMPLOYMENT_LABELS),
        (WORK_FORMAT, values.work_format, WORK_FORMAT_LABELS),
    ):
        if value is not None:
            _set_many(page, _condition_field(page, label), value, labels)


def _on_wizard_common(page: Page) -> bool:
    """True on the draft-wizard common screen (resume-profile-screen_common)."""
    return page.locator(account_profile.RESUME_COMMON_FORM).count() == 1


def _condition_field(page: Page, label: str):
    """Resolve a work-condition control by its visible label, honestly.

    #993: the draft-wizard common screen does not render these fields at all
    (live 2026-09-05). A bare «не найдено однозначно (совпадений: 0)» left
    the combat runs without an actionable reason — on the wizard shape the
    refusal names the screen instead.
    """
    try:
        return labelled_field(page, label)
    except PageStateIndeterminate as exc:
        if _on_wizard_common(page):
            raise PageStateIndeterminate(
                f"поле {label!r} не рендерится на экране common визарда черновика — "
                "эти условия работы задаются на другом экране или вручную"
            ) from exc
        raise


def _wizard_ticket_activator(page: Page):
    """Активатор «Разрешение на работу» на wizard-shape или None.

    #997: контейнер WORK_PERMIT_WIZARD (data-qa work-ticket-selector) — это
    селект страны «Разрешение на работу», НЕ трудовая книжка; активатор
    используется только для display-чтения work_permit. Ревью #994: точное
    размещение активатора относительно контейнера сохранённым дампом не
    зафиксировано (ценз 2026-09-05 — read-only наблюдение без HTML). Ищем
    каскадом: сначала ВНУТРИ контейнера, затем от его родителя (надмножество —
    покрывает и соседний случай). Каждый шаг требует count()==1: второй
    magritte-активатор в общем родителе не даст молча выбрать не тот элемент —
    каскад вернёт None, дальше отказ.
    """
    container = page.locator(account_profile.WORK_PERMIT_WIZARD)
    if container.count() != 1:
        return None
    for scope in (container, container.locator("xpath=..")):
        activator = scope.locator("[data-qa='magritte-select-activator']")
        if activator.count() == 1:
            return activator.first
    return None


def _work_ticket_field(page: Page):
    """Resolve «Наличие трудовой книжки» for writing (#997).

    Edit shape: a <label>-bound control (labelled_field, as before). Draft-
    wizard shape (#997, live screenshot common_screen_996.png): the wizard
    renders «Разрешение на работу» (WORK_PERMIT_WIZARD, value = country) and
    NO «Наличие трудовой книжки» — writing «Да/Нет» into the work-permit
    select would mutate a DIFFERENT field, so the #993 fallback is removed:
    the wizard shape refuses honestly.
    """
    try:
        return labelled_field(page, WORK_TICKET)
    except PageStateIndeterminate as exc:
        if _wizard_ticket_activator(page) is not None or _on_wizard_common(page):
            raise PageStateIndeterminate(
                "поле «Наличие трудовой книжки» не рендерится на экране common "
                "визарда черновика (контрол work-ticket-selector — это "
                "«Разрешение на работу», писать в него «Да/Нет» нельзя)"
            ) from exc
        raise


def _set_control(page, field, value: str, labels: dict[str, str]) -> None:
    """Set a labelled native/custom single-choice control without guessing."""
    if value not in labels:
        raise ValueError(f"недопустимое значение common: {value}")
    tag = field.evaluate("e=>e.tagName")
    if tag == "SELECT":
        field.select_option(value)
    elif tag == "INPUT" and field.get_attribute("type") == "checkbox":
        (field.check if value == "true" else field.uncheck)()
    else:
        # Magritte renders these as a labelled trigger.  The caller's exact
        # label binding is the identity check; the option's exact accessible
        # name is the value check.
        field.click()
        popup = page.locator(RESUME_POSITION_DROPDOWN)
        popup.wait_for(state="visible", timeout=_WAIT_MS)
        options = popup.get_by_role("option", name=labels[value], exact=True)
        if options.count() != 1:
            raise RuntimeError(f"вариант формы не найден: {labels[value]}")
        options.first.click()
        page.mouse.click(0, 0)
        popup.wait_for(state="hidden", timeout=_WAIT_MS)


def _set_many(page, field, values: list[str], labels: dict[str, str]) -> None:
    if not isinstance(values, list):
        raise ValueError("мультивыбор common должен быть списком значений")
    unknown = [value for value in values if value not in labels]
    if unknown:
        raise ValueError(f"недопустимое значение common: {unknown[0]}")
    tag = field.evaluate("e=>e.tagName")
    if tag == "SELECT":
        field.select_option(values)
        return
    # A checkbox collection is returned by the exact labelled binding.  Set
    # the requested state explicitly, so repeated application is idempotent
    # and stale selections are removed rather than silently retained.
    field.click()
    popup = page.locator(RESUME_POSITION_DROPDOWN)
    popup.wait_for(state="visible", timeout=_WAIT_MS)
    options = popup.get_by_role("option")
    wanted = {labels[value] for value in values}
    for index in range(options.count()):
        option = options.nth(index)
        if (
            option.get_attribute("aria-selected") == "true"
            and option.inner_text().strip() not in wanted
        ):
            option.click()
    for value in values:
        option = popup.get_by_role("option", name=labels[value], exact=True)
        if option.count() != 1:
            raise RuntimeError(f"вариант формы не найден: {value}")
        if option.first.get_attribute("aria-selected") != "true":
            option.first.click()
    page.mouse.click(0, 0)
    popup.wait_for(state="hidden", timeout=_WAIT_MS)


def save_common(
    page: Page,
    values: CommonValues,
    *,
    before_click: Callable[[], None] | None = None,
) -> CommonResult:
    apply_common(page, values)
    save = _strict(page, SAVE, "кнопка сохранения common")
    # #991: #990 сделал исход честным, но корневая причина боевых uncertain
    # (#982/#986) — NEXT-клик до гидратации SPA (#858; живой аналог измерен в
    # #840: активатор месяца гидрируется СЕКУНДЫ, клик в этом окне теряется).
    # Гейт: до клика ждём React-привязку SAVE; две попытки без гидрации —
    # клик не отправлялся вовсе, это честный failed (мутации точно не было),
    # а не uncertain. Гейт — pre-click проверка (ревью #992: как launch_context/
    # «форма не найдена» из #476), поэтому стоит ДО before_click: резерв
    # uncertain-маркера не должен висеть всё 30с-окно ожидания гидрации,
    # в котором клик структурно невозможен.
    hydrated = False
    for _attempt in range(2):
        if wait_for_react_hydration(page, SAVE, timeout_ms=_SAVE_HYDRATION_TIMEOUT_MS):
            hydrated = True
            break
    if not hydrated:
        # Дамп покажет, жива ли форма и есть ли form-helper-error — иначе
        # «не гидратирован» неотличим от «страница умерла».
        try:
            dump_page_html(page, "common_save_failure")
        except Exception:  # noqa: BLE001 — диагностика не должна маскировать исход
            pass
        return CommonResult(
            False,
            f"SAVE не гидратирован за {2 * _SAVE_HYDRATION_TIMEOUT_MS // 1000}с — "
            "клик не отправлялся (мутации нет)",
        )
    if before_click:
        before_click()
    # #989: тот же защищённый клик, что у confirm_common_screen (#986,
    # паттерн #913): исход решает wait_for_url (уход с
    # /profile/resume/common, бюджет 30с), падение click() при состоявшемся
    # переходе — не ошибка. Текст падения клика сохраняется в reason при
    # неуспехе (ревью #990: иначе «дошёл ли клик» недиагностируемо).
    click_error: str | None = None
    try:
        save.click()
    except PlaywrightError as exc:
        click_error = str(exc)
    try:
        page.wait_for_url(
            lambda url: urlsplit(str(url)).path != COMMON_SCREEN_PATH,
            wait_until="commit",
            timeout=_COMMON_SCREEN_NAV_TIMEOUT_MS,
        )
    except PlaywrightError as exc:
        # Клик мог уйти на hh.ru (#176) — дампим экран: в нём виден
        # text form-helper-error, которым hh.ru объясняет отказ валидации,
        # и это единственная диагностика этого исхода (#989). Пустой дамп
        # по ошибкам валидации = потерянный клик (#991) — см. дискриминатор
        # в #991 для следующей итерации.
        try:
            dump_page_html(page, "common_save_failure")
        except Exception:  # noqa: BLE001 — диагностика не должна заменять исходную ошибку
            pass
        reason = f"сохранение common не подтверждено: {exc}; гидрация SAVE: ок"
        if click_error is not None:
            reason += f"; ошибка клика: {click_error[:300]}"
        return CommonResult(False, reason, True, True)
    return CommonResult(True, "поля common сохранены", True)

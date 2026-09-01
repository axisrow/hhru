"""Browser step for changing resume visibility and the employer stop-list.

Экран `/resume/edit/{resume_id}/visibility` подтверждён живым DOM 2026-08-29
(issue #746; предыдущая fail-closed заглушка была от issue #566). Действие
только UI-кликами, без ``page.request.*`` — как весь проект.

Пять режимов видимости (CLAUDE.md/docs/cli-spec.md, #566):
``everyone``/``no-one``/``link-only`` не имеют списка компаний.
``whitelist``/``blacklist`` открывают блок "Кто видит"/"Кто не видит" со
своим списком работодателей — команда может редактировать этот список
независимо от того, меняется ли режим в этом же вызове (issue #746: стоп-лист
обычно общий для всех резюме аккаунта, а смена режима — отдельное решение).

Ранний ``return`` (например неоднозначный поиск работодателя) может оставить
модалку списка открытой в DOM — намеренно не закрывается явно. Следующий вызов
этой функции для другого резюме (``--resume all`` в ``commands/resume_visibility.py``)
начинается с ``goto_hh``, то есть с полной навигации браузера (``page.goto``),
а не SPA push-state — она полностью уничтожает предыдущий DOM независимо от
того, была ли модалка открыта; React-модалка hh.ru не является нативным
browser-диалогом (`alert`/`confirm`/`beforeunload`) и не блокирует навигацию
(#746 review round 2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import HH_BASE_URL, goto_hh
from .external_forms.detect import normalize
from .selector_groups.resume_visibility import (
    RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DATA_QA_PREFIX,
    RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DELETE,
    RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_PREFIX,
    RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT,
    RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_CHECKBOX,
    RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX,
    RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_PREFIX,
    RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_NAME,
    RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_BLACKLIST,
    RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_WHITELIST,
    RESUME_VISIBILITY_MODAL_CLOSE,
    RESUME_VISIBILITY_MODAL_CONFIRM,
    RESUME_VISIBILITY_MODE_BLACKLIST,
    RESUME_VISIBILITY_MODE_EVERYONE,
    RESUME_VISIBILITY_MODE_LINK_ONLY,
    RESUME_VISIBILITY_MODE_NO_ONE,
    RESUME_VISIBILITY_MODE_RADIO,
    RESUME_VISIBILITY_MODE_WHITELIST,
    RESUME_VISIBILITY_SAVE,
)

# Каноническая пятёрка режимов (docs/cli-spec.md §resume-visibility, #566).
# Значения — то, что принимает --mode; ключи, оставшиеся из issue #746
# ("public"/"selected"/"hidden-from"/"hidden") сюда сознательно не взяты —
# сигнатура #566 уже задокументирована и слита в main, дублировать её другим
# словарём означало бы два конкурирующих контракта для одной команды.
VISIBILITY_MODES = ("everyone", "no-one", "link-only", "whitelist", "blacklist")

_MODE_SELECTORS: dict[str, str] = {
    "everyone": RESUME_VISIBILITY_MODE_EVERYONE,
    "whitelist": RESUME_VISIBILITY_MODE_WHITELIST,
    "blacklist": RESUME_VISIBILITY_MODE_BLACKLIST,
    "link-only": RESUME_VISIBILITY_MODE_LINK_ONLY,
    "no-one": RESUME_VISIBILITY_MODE_NO_ONE,
}
# Только whitelist/blacklist рендерят блок со списком компаний.
_EMPLOYER_LIST_MODES = ("whitelist", "blacklist")
_ACTIVATOR_SELECTORS: dict[str, str] = {
    "whitelist": RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_WHITELIST,
    "blacklist": RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_BLACKLIST,
}


def visibility_url(resume_id: str) -> str:
    return f"{HH_BASE_URL}/resume/edit/{resume_id}/visibility"


@dataclass
class EmployerCandidate:
    employer_id: str
    name: str
    city: str = ""


@dataclass
class ResumeVisibilityResult:
    resume_id: str
    success: bool
    reason: str
    uncertain: bool = False
    # Неоднозначные найденные компании (несколько карточек на одно имя) —
    # разрешение остаётся за пользователем (issue #746), команда не выбирает
    # автоматически. Заполняется, когда success=False именно по этой причине.
    ambiguous_candidates: list[EmployerCandidate] = field(default_factory=list)
    ambiguous_query: str = ""


def _one(page: Page, selector: str, label: str) -> tuple[Locator | None, str]:
    locator = page.locator(selector)
    count = locator.count()
    if count != 1:
        return None, f"{label} не подтверждён однозначно (совпадений: {count})"
    return locator.first, ""


def _click_mode(page: Page, mode: str) -> str:
    """Click the outer access-type label; inputs share value='on'/blank name.

    #746 review round 3: a fire-and-forget .click() with no post-click check
    would let a stale locator or a click that silently misses its target (DOM
    drift, an intercepting overlay) leave the actual hh.ru-selected mode
    different from what the caller asked for — everything downstream (the
    employer-list-mode detection when --mode is explicit, per its own comment
    above) would then trust the wrong mode. Verify the nested radio input is
    checked afterwards, fail-closed otherwise — the same discipline the rest
    of this module already applies to every other click.

    #901: карточка содержит ДВА input[type='radio'] — внешний (прямой дочерний
    label'а) и внутренний Magritte (в span[data-qa='radio-container'], readonly;
    см. RESUME_VISIBILITY_MODE_RADIO). Пост-кликовая проверка адресует внешний:
    с ним же React синхронизирует внутренний. Программное выставление checked
    (в обход клика по карточке) внутреннему/внешнему инпуту НЕ уведомляет
    Magritte — режим не доезжает до сервера (живая проверка #901 в комментарии
    к issue).

    Клик — в ЛЕВУЮ PADDING-ЗОНУ карточки (position x=10, вертикальный центр),
    не в центр bounding box (живой замер 2026-09-01, PR #917): вложенный
    label[data-qa='cell'] покрывает ВСЮ внутреннюю область карточки (текст и
    radio-container), и клик в неё перехватывается label-активацией ВНУТРЕННЕГО
    Magritte-инпута — React-состояние карточки не обновляется (внешний radio
    остаётся unchecked, прежний режим не сбрасывается), паузами не лечится —
    это не гонка гидратации. Клик по padding-зоне (вне вложенного label)
    доходит до onClick карточки (data-interactive=true) и переключает режим
    целиком: оба input'а checked + сброс прежнего. Дефолтный центр
    locator.click() попадает ровно на cell-text-content — первый боевой
    прогон #917 падал на этом («radio не отмечен после клика», мутации не
    было, attempted=0).
    """
    locator, reason = _one(page, _MODE_SELECTORS[mode], f"режим видимости «{mode}»")
    if reason:
        return reason
    assert locator is not None
    box = locator.bounding_box()
    if box is None or box["height"] <= 0:
        return f"карточка режима «{mode}» не видна для клика"
    locator.click(position={"x": 10, "y": box["height"] / 2})
    radio = locator.locator(RESUME_VISIBILITY_MODE_RADIO)
    if radio.count() != 1:
        return f"radio-инпут режима «{mode}» не подтверждён однозначно после клика"
    if not radio.first.is_checked():
        return f"клик по режиму «{mode}» не подтверждён — radio не отмечен после клика"
    return ""


def read_active_mode(page: Page) -> str | None:
    """Активный режим видимости по checked внешнего radio карточек (#901).

    Внешний radio — ПРЯМОЙ дочерний label'а (RESUME_VISIBILITY_MODE_RADIO):
    descendant-поиск нашёл бы и внутренний Magritte-инпут (#901). Возвращается
    режим только при РОВНО одном checked (fail-closed): у внешних radio пустой
    name, браузер НЕ снимает соседей при клике — эксклюзивность держит React,
    поэтому 2+ checked (дрейф DOM, момент между нативным кликом и React-
    синхронизацией) означают неопределённость, а не «первый попавшийся».
    Используется и для детекции активного whitelist/blacklist без явного
    --mode, и как позитивный маркер результата после Save.
    """
    checked: list[str] = []
    for mode, selector in _MODE_SELECTORS.items():
        radio = page.locator(selector).locator(RESUME_VISIBILITY_MODE_RADIO)
        if radio.count() == 1 and radio.first.is_checked():
            checked.append(mode)
    if len(checked) == 1:
        return checked[0]
    return None


def _open_visibility_screen(page: Page, resume_id: str) -> str:
    """Перейти на экран видимости и дождаться его отрисовки (commit != отрисовано).

    Единственный вызов для обеих точек входа: первичное открытие перед вводом
    и повторное чтение после Save (позитивный маркер #901) — тот же экран,
    тот же wait; различается только обработка ошибки вызывающей стороной
    (до Save — обычный fail, после — uncertain, клик уже ушёл).
    """
    goto_hh(page, visibility_url(resume_id))
    try:
        page.locator(RESUME_VISIBILITY_SAVE).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return f"экран видимости не отрисовался: {exc}"
    return ""


def _read_employer_search_results(page: Page) -> list[EmployerCandidate]:
    items = page.locator(RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_PREFIX)
    candidates: list[EmployerCandidate] = []
    for item in items.all():
        qa = item.get_attribute("data-qa") or ""
        employer_id = qa.removeprefix(RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX)
        name_locator = item.locator(RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_NAME)
        name = name_locator.first.text_content() if name_locator.count() else ""
        candidates.append(EmployerCandidate(employer_id=employer_id, name=(name or "").strip()))
    return candidates


def _open_employer_list_modal(page: Page, list_mode: str) -> str:
    # commit != отрисовано (CLAUDE.md): если этому вызову предшествовал клик по
    # radio-режиму (_click_mode), активатор списка — условный React-рендер, а не
    # уже присутствующий в DOM элемент. Строгая _one() сразу после клика может
    # увидеть count()=0 на ещё не отрисованном экране и ошибочно списать это на
    # "селектор не подтверждён" вместо реальной причины — race condition. Явный
    # wait_for(state="visible") перед строгой проверкой — тот же паттерн, что уже
    # в resume_position.py/skills.py/apply/steps.py и др. (#746 review round 2).
    activator_selector = _ACTIVATOR_SELECTORS[list_mode]
    try:
        page.locator(activator_selector).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return f"блок списка работодателей «{list_mode}» не отрисовался: {exc}"
    activator, reason = _one(page, activator_selector, f"блок списка работодателей «{list_mode}»")
    if reason:
        return reason
    assert activator is not None
    activator.click()
    try:
        page.locator(RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT).first.wait_for(
            state="visible", timeout=15000
        )
    except PlaywrightError as exc:
        return f"модалка списка работодателей не отрисовалась: {exc}"
    return ""


def _add_employer(page: Page, name: str) -> tuple[bool, str, list[EmployerCandidate]]:
    """Search, resolve exactly one candidate, check it, and click Добавить.

    Ambiguous/zero matches are a fail-closed refusal — issue #746 requires
    resolving multiple similarly-named employers to stay with the caller
    (interactively confirmed one level up), never an automatic pick.

    #746 review (AO reviewer): the search field is explicitly cleared after a
    successful add. The probe dump confirms the "Кто видит"/"Кто не видит"
    already-added-employers list (which ``_remove_employer`` reads) only
    renders while the search field is empty — a non-empty query switches the
    modal to the search-results container instead. hh.ru's own post-click
    behavior for the field is unconfirmed either way, so this does not rely on
    it: leaving a non-empty query behind would make a subsequent
    ``--remove-employer`` in the same call see zero already-added rows and
    fail-closed with a misleading "не найден в текущем списке", even though
    the employer is actually present — breaking the documented combined
    add+remove scenario (docs/cli-spec.md).
    """
    search, reason = _one(page, RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT, "поиск работодателя")
    if reason:
        return False, reason, []
    assert search is not None
    search.fill(name)
    try:
        page.locator(RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_PREFIX).first.wait_for(
            state="visible", timeout=15000
        )
    except PlaywrightError:
        return False, f"работодатель «{name}» не найден в поиске hh.ru", []
    candidates = _read_employer_search_results(page)
    matches = [c for c in candidates if normalize(c.name) == normalize(name)]
    if not matches:
        return False, f"работодатель «{name}» не найден в поиске hh.ru (точное совпадение)", []
    if len(matches) > 1:
        return False, f"найдено {len(matches)} работодателей с именем «{name}» — уточните", matches
    target = matches[0]
    item_selector = (
        f"[data-qa='{RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX}"
        f"{target.employer_id}']"
    )
    row, reason = _one(page, item_selector, f"карточка работодателя «{name}»")
    if reason:
        return False, reason, []
    assert row is not None
    checkbox = row.locator(RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_CHECKBOX)
    if checkbox.count() != 1:
        return False, f"чекбокс работодателя «{name}» не подтверждён однозначно", []
    checkbox.first.check()
    confirm, reason = _one(page, RESUME_VISIBILITY_MODAL_CONFIRM, "кнопка «Добавить»")
    if reason:
        return False, reason, []
    assert confirm is not None
    confirm.click()
    # See the docstring above: clear the search query so a later
    # --remove-employer in the same call sees the already-added-employers
    # list (empty-query container), not the still-filtered search results.
    search.fill("")
    return True, "", []


def _remove_employer(page: Page, name: str) -> tuple[bool, str]:
    """Remove an already-added employer by exact name match from the list view.

    #746 review round 2: an earlier revision matched by substring on the whole
    row's text_content() — that both silently over-matched (a substring hit on
    a longer employer name would remove the wrong company with no ambiguity
    warning) and was inconsistent with ``_add_employer``'s exact-match contract.
    A row's raw text_content() also concatenates the name twice (the visible
    "Кто видит"-list span and its wrapping ``<a>`` link render the same name,
    per the #746 probe dump), so exact-matching that combined string would
    never equal a single-name query either — the name is read from the row's
    single ``cell-text-content`` span instead, mirroring how the search-result
    name is read in ``_read_employer_search_results``.
    """
    items = page.locator(RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_PREFIX)
    matches: list[Locator] = []
    for item in items.all():
        qa = item.get_attribute("data-qa") or ""
        if not qa.startswith(RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DATA_QA_PREFIX):
            continue
        name_locator = item.locator("[data-qa='cell-text-content']")
        if name_locator.count() < 1:
            continue
        row_name = name_locator.first.text_content() or ""
        if normalize(row_name) != normalize(name):
            continue
        matches.append(item)
    if not matches:
        return False, f"работодатель «{name}» не найден в текущем списке"
    if len(matches) > 1:
        # #746 review round 3: matches здесь строго по exact normalize()-совпадению
        # (строка 227), а не по substring — >1 означает несколько разных карточек
        # списка с буквально одинаковым нормализованным именем (напр. два разных
        # employer_id под одинаковым отображаемым названием), а не то, что "name"
        # является подстрокой нескольких записей. Формулировка отражает это.
        return False, (
            f"в списке {len(matches)} записей с именем «{name}» — совпадение "
            "неоднозначно, уточните вручную на hh.ru"
        )
    delete_button = matches[0].locator(RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DELETE)
    if delete_button.count() != 1:
        return False, f"кнопка удаления работодателя «{name}» не подтверждена однозначно"
    delete_button.first.click()
    return True, ""


def set_resume_visibility_on_hh(
    page: Page,
    resume,  # noqa: ANN001 - kept compatible with the Playwright command seam
    mode: str | None,
    dry_run: bool,
    *,
    add_employers: tuple[str, ...] = (),
    remove_employers: tuple[str, ...] = (),
    before_click: Callable[[], None] | None = None,
) -> ResumeVisibilityResult:
    """Change the visibility mode and/or the employer stop-list for one resume.

    ``mode=None`` keeps the resume's current mode untouched — only the
    employer list is edited (issue #746's primary scenario: a stop-list
    entry usually applies without a mode change). ``add_employers``/
    ``remove_employers`` require an active whitelist/blacklist mode (either
    passed via ``mode`` or already selected on hh.ru) — editing a list that
    is not the active one would silently not apply.
    """
    resume_id = resume.resume_id
    if mode is not None and mode not in VISIBILITY_MODES:
        return ResumeVisibilityResult(resume_id, False, f"неизвестный режим видимости «{mode}»")
    if dry_run:
        parts = []
        if mode is not None:
            parts.append(f"режим будет изменён на «{mode}»")
        for name in add_employers:
            parts.append(f"будет добавлен работодатель «{name}»")
        for name in remove_employers:
            parts.append(f"будет удалён работодатель «{name}»")
        if not parts:
            return ResumeVisibilityResult(
                resume_id, False, "не задано ни --mode, ни списки работодателей"
            )
        return ResumeVisibilityResult(resume_id, True, "dry-run; " + "; ".join(parts))

    reason = _open_visibility_screen(page, resume_id)
    if reason:
        return ResumeVisibilityResult(resume_id, False, reason)

    if mode is not None:
        reason = _click_mode(page, mode)
        if reason:
            return ResumeVisibilityResult(resume_id, False, reason)

    wants_employer_edit = bool(add_employers or remove_employers)
    if wants_employer_edit:
        # Determine the active list mode: explicit --mode wins; otherwise read
        # which whitelist/blacklist radio is currently checked on hh.ru.
        # Читается внешний radio карточки (RESUME_VISIBILITY_MODE_RADIO,
        # #901): descendant-поиск находит и внутренний Magritte-инпут, а
        # `.checked` синхронен у обоих input'ов карточки — подтверждено живым
        # дампом #901 (оба checked на активном режиме); разведка #746 этот
        # факт доказать не могла — знала только один radio на карточку. Ни
        # один не подтверждён — fail-closed отказ ниже.
        list_mode = mode if mode in _EMPLOYER_LIST_MODES else None
        if list_mode is None:
            # Пре-кликовое окно (ревью PR #917): мутации ещё не было, поэтому
            # обычный failed, НЕ uncertain — серая зона начинается с клика по
            # Save ниже. Но исход тоже per-resume: не пойманный PlaywrightError
            # из count()/is_checked() оборвал бы --resume all batch сырым
            # исключением без [FAIL] — против гранулярности #746 round 3; тем
            # же соображением обёрнута и пост-Save перечитка ниже.
            try:
                active = read_active_mode(page)
            except PlaywrightError as exc:
                return ResumeVisibilityResult(
                    resume_id, False, f"активный режим не прочитан: {exc}"
                )
            if active in _EMPLOYER_LIST_MODES:
                list_mode = active
        if list_mode is None:
            return ResumeVisibilityResult(
                resume_id,
                False,
                "список работодателей задан, но активный режим не whitelist/blacklist "
                "(укажите --mode whitelist или --mode blacklist)",
            )
        reason = _open_employer_list_modal(page, list_mode)
        if reason:
            return ResumeVisibilityResult(resume_id, False, reason)

        for name in add_employers:
            ok, reason, ambiguous = _add_employer(page, name)
            if not ok:
                return ResumeVisibilityResult(
                    resume_id,
                    False,
                    reason,
                    ambiguous_candidates=ambiguous,
                    ambiguous_query=name if ambiguous else "",
                )
        if add_employers and remove_employers:
            # #746 review (AO reviewer): _add_employer clears the search query
            # after each add, but the already-added-employers list container is
            # a conditional React render (same commit-vs-hydration race already
            # guarded elsewhere in this module) — a strict lookup right after
            # the clear could still see the stale search-results container.
            # Wait for the empty-query container before _remove_employer reads
            # it, so a combined add+remove call doesn't misreport "не найден в
            # текущем списке" for an employer that is actually present.
            try:
                page.locator(RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_PREFIX).first.wait_for(
                    state="visible", timeout=15000
                )
            except PlaywrightError as exc:
                return ResumeVisibilityResult(
                    resume_id,
                    False,
                    f"список добавленных работодателей не отрисовался после add: {exc}",
                )
        for name in remove_employers:
            ok, reason = _remove_employer(page, name)
            if not ok:
                return ResumeVisibilityResult(resume_id, False, reason)

        close, reason = _one(page, RESUME_VISIBILITY_MODAL_CLOSE, "кнопка закрытия модалки списка")
        if reason:
            return ResumeVisibilityResult(resume_id, False, reason)
        assert close is not None
        close.click()

    save, reason = _one(page, RESUME_VISIBILITY_SAVE, "кнопка «Сохранить»")
    if reason:
        return ResumeVisibilityResult(resume_id, False, reason)
    assert save is not None
    # before_click зарезервирован ровно здесь, а не раньше (клики по radio-режиму,
    # "Добавить"/крестику удаления в модалке списка) — все эти промежуточные клики
    # меняют только клиентское состояние формы React-SPA, подтверждено живой
    # разведкой #746 (переключение radio без клика «Сохранить» не изменило
    # состояние резюме на сервере, проверено повторной навигацией). Реальная
    # мутация hh.ru — единственный клик ниже; "серая зона" (CLAUDE.md §3)
    # начинается именно с него, не раньше.
    try:
        if before_click is not None:
            before_click()
        save.click()
        # hh.ru пересобирает форму после сохранения; ждём исчезновения кнопки
        # или редиректа как позитивного сигнала вместо фиксированного sleep.
        #
        # #746 review round 3: это ТОЛЬКО позитивный сигнал — нет проверки
        # негативного маркера (validation-ошибка/toast), который отличал бы
        # реальный успех сохранения от React-формы, скрывшей кнопку во время
        # отображения ошибки. Аналогичный многоселекторный fail-closed паттерн
        # для "серой зоны" уже есть в apply/pipeline.py (внешний источник
        # истины — /applicant/negotiations, CLAUDE.md §3), но здесь для него
        # нет ни подтверждённого селектора ошибки, ни read-only способа
        # перепроверить итоговый список компаний без повторного открытия
        # модалки (сама модалка не идентична "источнику истины" apply).
        # Селектор ошибки НЕ подтверждён живым DOM (разведка #746 не покрывала
        # сценарий реальной ошибки сохранения) — гадать его здесь означало бы
        # нарушить тот же принцип, которым обоснован весь этот модуль (CLAUDE.md:
        # "Селекторы — статус проверки"). Известное ограничение, не забытый шаг;
        # требует отдельной живой разведки сценария ошибки прежде чем закрывать.
        page.locator(RESUME_VISIBILITY_SAVE).first.wait_for(state="hidden", timeout=15000)
    except PlaywrightError as exc:
        return ResumeVisibilityResult(
            resume_id,
            False,
            f"ошибка после клика «Сохранить»: {exc}",
            uncertain=True,
        )

    # Позитивный маркер результата (#901): исчезновение кнопки Save — сигнал
    # только об отсутствии видимой ошибки, НЕ о применённом режиме (рапорт
    # успеха без проверки факта — тот же класс дефекта, что #899). После Save
    # экран перечитывается заново и подтверждается checked внешнего radio
    # запрошенного режима; несовпадение/нечитаемость — пост-кликовая зона,
    # uncertain (клик уже ушёл на hh.ru, fail-closed как #176). Всё окно
    # перечитки — включая goto_hh и count()/is_checked() — под тем же
    # except PlaywrightError: goto_hh после ретраев ререйзит (в т.ч.
    # ThrottledChannelDetected), и не пойманное здесь исключение оборвало бы
    # --resume all batch сырым traceback вместо per-resume [FAIL] (uncertain)
    # — против решения #746 round 3 о пер-резюме гранулярности. Для mode=None
    # (только списки работодателей) read-only источника истины нет — список
    # виден только внутри модалки, известное ограничение (см. комментарий к
    # wait_for hidden выше).
    if mode is not None:
        try:
            reason = _open_visibility_screen(page, resume_id)
            if reason:
                return ResumeVisibilityResult(
                    resume_id,
                    False,
                    f"режим не перечитан после сохранения: {reason}",
                    uncertain=True,
                )
            active = read_active_mode(page)
        except PlaywrightError as exc:
            return ResumeVisibilityResult(
                resume_id,
                False,
                f"ошибка перечитки режима после «Сохранить»: {exc}",
                uncertain=True,
            )
        if active is None:
            return ResumeVisibilityResult(
                resume_id,
                False,
                f"после сохранения активный режим не определён (ожидался «{mode}»)",
                uncertain=True,
            )
        if active != mode:
            return ResumeVisibilityResult(
                resume_id,
                False,
                f"после сохранения активен режим «{active}», ожидался «{mode}»",
                uncertain=True,
            )
    return ResumeVisibilityResult(resume_id, True, "видимость сохранена")

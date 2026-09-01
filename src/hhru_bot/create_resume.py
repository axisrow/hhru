"""Browser flow for creating an empty resume through the hh.ru UI (#304)."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import (
    HH_BASE_URL,
    RESUMES_FULL_LIST_URL,
    dismiss_cookie_banner,
    goto_hh,
)
from .external_forms.detect import normalize
from .resume_titles import duplicate_title_reason, read_account_titles
from .selector_groups.resume_page import (
    RESUME_CREATE_BUTTON,
    RESUME_CREATION_CATEGORY_INPUT,
    RESUME_CREATION_CATEGORY_SEARCH,
    RESUME_CREATION_CATEGORY_SUBMIT,
    RESUME_CREATION_NEXT,
    RESUME_CREATION_POSITION,
    RESUME_CREATION_SELECT_JOB,
    RESUME_CREATION_URL,
)

CREATION_URL = f"{HH_BASE_URL}{RESUME_CREATION_URL}"
# hh.ru подтверждает сохранение ДВУМЯ формами URL: прямой страницей резюме
# (/resume/{id}) и следующим шагом визарда, где id уходит в query-параметр
# (/profile/resume/educations?resume={id}&hhtmFrom=...). Боевой прогон #778
# наблюдал вторую: резюме создавалось, но ожидание первой формы падало по
# таймауту и давало uncertain при фактическом успехе.
_RESUME_ID_RE = re.compile(r"(?:/resume/|[?&]resume=)([0-9a-f]{32,40})(?:[/?#&]|$)")
# #837: живой замер показал checked=True уже на первой проверке спустя
# +100мс после клика — секундный запас на порядок больше наблюдавшейся
# задержки, но честный (не бесконечный) дедлайн на случай, если чекбокс
# реально не переключился.
_CHECKBOX_CONFIRM_TIMEOUT = 5.0
_RESUME_LIMIT_REASON = (
    "лимит резюме hh.ru (~20) достигнут или кнопка создания недоступна; "
    "удалите ненужные резюме и повторите попытку"
)


@dataclass
class CreateResumeResult:
    success: bool
    new_resume_id: str = ""
    reason: str = ""
    uncertain: bool = False


def _one(page: Page, selector: str, label: str) -> tuple[Locator | None, str]:
    locator = page.locator(selector)
    count = locator.count()
    if count != 1:
        return None, f"{label} не подтверждён однозначно (совпадений: {count})"
    return locator.first, ""


def _require(locator: Locator | None) -> Locator:
    """Narrow ``_one()``'s optional result after its reason has been checked empty."""
    assert locator is not None
    return locator


def _click_one(
    page: Page,
    selector: str,
    label: str,
    *,
    before_click: Callable[[], None] | None = None,
) -> str:
    """Resolve exactly one locator and click it; return a non-empty reason on failure."""
    locator, reason = _one(page, selector, label)
    if reason:
        return reason
    if before_click is not None:
        before_click()
    _require(locator).click()
    return ""


def select_catalog_leaf(
    page: Page,
    area: str,
    *,
    filter_timeout: float = 15.0,
    checkbox_confirm_timeout: float = _CHECKBOX_CONFIRM_TIMEOUT,
    expected_role_id: str | None = None,
) -> str:
    """Select one exact leaf from hh.ru's full profession tree.

    ``expected_role_id`` (#913) — согласованный id роли из каталога поиска
    вакансий: id-пространства дерева модалки и того каталога совместимы
    (подтверждено live: 96 <-> ``tree-selector-item-96``). Когда id задан, лист
    с ДРУГИМ id не кликается вовсе — точное совпадение текста ещё не доказывает
    нужную роль, а молчаливая подмена записала бы чужой role_id.
    """
    # The caller arrives right after clicking the wizard's NEXT control, which
    # re-renders the catalog screen asynchronously (React); a strict _one() on
    # the search input immediately after can observe the stale blank body (the
    # same commit-vs-hydration race guarded for SELECT_JOB/POSITION above).
    try:
        page.locator(RESUME_CREATION_CATEGORY_SEARCH).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return f"экран каталога профессий не отрисовался: {exc}"
    search, reason = _one(page, RESUME_CREATION_CATEGORY_SEARCH, "поиск каталога профессий")
    if reason:
        return reason
    _require(search).fill(area)
    # The filtered tree re-renders asynchronously (React) after typing, and the
    # PRE-filter tree is already populated — so waiting for "a first node" is
    # satisfied instantly by the stale full catalog (живой замер #778: 14 узлов
    # до fill, те же 14 сразу после wait_for, и лишь через ~500 мс остаётся 1).
    # Reading .all() at that moment collects other professions and surfaces as a
    # false "профессия «…» не найдена однозначно (совпадений: 0)". Poll the tree
    # until the exact match appears instead of trusting a single read.
    # get_by_text() resolves to the inner ``cell-text-content`` span on the
    # current hh.ru DOM, while the identifier we need is on its wrapper.
    # Match the wrapper by its own rendered text instead of assuming the
    # attribute is attached to the text node.
    deadline = time.monotonic() + filter_timeout
    matches: list[Locator] = []
    candidates: list[Locator] = []
    while True:
        tree = page.locator("[data-qa*='tree-selector-item-text-']")
        try:
            # #837 (боевой прогон 2026-08-30): читать candidates=.all() один
            # раз, а затем построчно candidate.text_content() каждого элемента
            # — race. Между .all() (снимок handle-ов) и последним .text_content()
            # в цикле React успевает перерендерить дерево (переход от
            # нефильтрованного списка категорий к отфильтрованному leaf), и
            # .text_content() на уже отсоединённом handle висит полный
            # дефолтный таймаут Playwright (30с), не пойман никаким try/except
            # внутри цикла — падает наружу как generic "ошибка до сохранения
            # резюме". all_text_contents() читает тексты ВСЕХ текущих
            # совпадений селектора одним batch-вызовом Playwright, а не по
            # одному хэндлу — устраняет основной источник race. candidates
            # снимается сразу следом на том же (ещё живом на момент вызова)
            # locator; любой PlaywrightError из обоих вызовов — тот же сигнал
            # "дерево перерендерилось", не финальная ошибка.
            texts = [normalize(text) for text in tree.all_text_contents()]
            candidates = tree.all()
        except PlaywrightError:
            # Не финальная ошибка, а сигнал повторить опрос — тот же принцип,
            # что уже применяется ниже к нулю/множеству совпадений: решение
            # только после того, как список стабилизируется или истечёт
            # дедлайн.
            texts = []
            candidates = []
        if len(candidates) != len(texts):
            # all_text_contents() и .all() — два отдельных Playwright-вызова;
            # React мог перерендерить дерево МЕЖДУ ними тоже (более узкое, но
            # то же семейство окно, что и построчное чтение выше). Разная
            # длина — надёжный сигнал рассинхрона: доверять индексному
            # сопоставлению candidates[i]/texts[i] в этом случае нельзя,
            # правильнее считать итерацию неудачной и повторить опрос, чем
            # молча сопоставить чужой текст чужому элементу.
            matches = []
        else:
            matches = [
                candidate
                for candidate, text in zip(candidates, texts, strict=True)
                if text == normalize(area)
            ]
        if len(matches) == 1 or time.monotonic() >= deadline:
            break
        page.wait_for_timeout(250)
    if not matches:
        # #836: «не найдена однозначно (совпадений: 0)» не различало опечатку
        # и пропажу значения из каталога hh.ru (боевой кейс — "Программист,
        # разработчик" исчез из каталога создания резюме). Показать, что
        # каталог реально предлагает по этому запросу, — тот же принцип, что
        # #822/PR #832 закрепил для дерева специализаций резюме (сообщение
        # различает «нет совпадений» и «неоднозначность»). Текст берётся из
        # живого каталога как есть (не normalize(), который лоуеркейсит) —
        # правило проекта "перечень профессий брать из живого каталога, не
        # вшивать литералом".
        seen: dict[str, None] = {}
        for candidate in candidates:
            text = (candidate.text_content() or "").strip()
            if text:
                seen.setdefault(text, None)
        offered = list(seen)
        if offered:
            options = "; ".join(offered)
            return f"профессия «{area}» не найдена в каталоге; каталог предлагает: {options}"
        return f"профессия «{area}» не найдена в каталоге (список пуст)"
    if len(matches) > 1:
        return f"профессия «{area}» не найдена однозначно в каталоге (совпадений: {len(matches)})"
    qa = matches[0].get_attribute("data-qa") or ""
    match = re.search(r"tree-selector-item-text-(\d+)$", qa)
    if not match:
        return f"пункт каталога «{area}» не является leaf-профессией"
    if expected_role_id is not None and match.group(1) != expected_role_id:
        # Неточная цель вырождается в «Другое» (id 40) или находит лист с тем
        # же текстом, но другим id (#911/#913): «Другое» не выбирать никогда —
        # это отказ, а не выбор. Остановка ДО клика оставляет форму без
        # изменений, и повтор с корректной целью ничего не должен откатывать.
        return (
            f"профессия «{area}» найдена в каталоге с role_id={match.group(1)}, "
            f"ожидался согласованный role_id={expected_role_id}"
        )
    # The checkbox shares the tree row confirmed rendered above, but it is still
    # a distinct control the SPA attaches asynchronously; wait before the strict
    # _one() so the commit-vs-hydration pattern stays symmetric across the wizard.
    checkbox_selector = RESUME_CREATION_CATEGORY_INPUT.format(match.group(1))
    try:
        page.locator(checkbox_selector).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return f"чекбокс профессии «{area}» не отрисовался: {exc}"
    checkbox, reason = _one(page, checkbox_selector, f"чекбокс профессии «{area}»")
    if reason:
        return reason
    # ``check()`` по самому <input> не работает: hh.ru прячет его за
    # стилизованной обёрткой (``magritte-checkbox-container``), у input
    # ``tabindex="-1"``, и Playwright падает с «Clicking the checkbox did not
    # change its state» (живой прогон #778). Кликается видимая строка
    # профессии — тот же узел, по которому выше определён leaf.
    matches[0].click()
    # #837 (боевой прогон 2026-08-30, 2 из 3 живых фейлов): клик запускает
    # асинхронное React-обновление checked-состояния, а is_checked() сразу
    # после click() — синхронное чтение без ожидания. Живой замер: 2 из 6
    # прогонов ловили checked=False непосредственно после click(), при этом
    # checked=True уже на первой же проверке спустя +100мс — не редкий
    # edge case, воспроизводится стабильно в ~33% случаев. Playwright не даёт
    # wait_for(state="checked") — checked не входит в поддерживаемые состояния
    # Locator.wait_for(). Фиксированная пауза замаскировала бы гонку, а не
    # устранила: на медленном хосте/загруженном hh.ru та же гонка вернулась
    # бы. Поэтому — тот же polling-до-дедлайна, что уже применяется выше для
    # дерева, а не sleep().
    checkbox_deadline = time.monotonic() + checkbox_confirm_timeout
    while not _require(checkbox).is_checked() and time.monotonic() < checkbox_deadline:
        page.wait_for_timeout(100)
    if not _require(checkbox).is_checked():
        return f"профессия «{area}» не отмечена после клика по строке каталога"
    return _click_one(page, RESUME_CREATION_CATEGORY_SUBMIT, "кнопка каталога профессий")


def _click_until_screen_switches(
    page: Page,
    card: Locator,
    next_selector: str,
    *,
    attempts: int = 3,
    timeout: int = 7000,
) -> str:
    """Кликать карточку визарда, пока не отрисуется следующий экран.

    ``wait_for(state="visible")`` карточку не страхует: hh.ru отдаёт её
    SSR-разметкой (``<div role="button">``), которая видима сразу, а React
    привязывает обработчик лишь через несколько секунд. Клик в этом окне
    проходит без ошибки и молча не даёт эффекта (живая разведка #778: 3/3
    провала при клике сразу после ``visible``, 3/3 успеха после ожидания
    гидратации).

    Ждать ``__react*`` ключ на элементе было бы прямой проверкой причины, но
    завязало бы код на внутреннее устройство React. Вместо этого проверяется
    наблюдаемый результат — появление следующего экрана. Повтор безопасен:
    карточка выбора профессии ничего не мутирует, а лишний клик по уже
    переключённому экрану невозможен, так как цикл прерывается по первому
    успеху.
    """
    last_error = ""
    for _ in range(attempts):
        card.click()
        try:
            page.locator(next_selector).first.wait_for(state="visible", timeout=timeout)
        except PlaywrightError as exc:
            last_error = str(exc)
            continue
        return ""
    return f"экран визарда не переключился после {attempts} попыток: {last_error}"


def create_resume_on_hh(
    page: Page,
    *,
    area: str,
    title: str,
    dry_run: bool,
    before_click: Callable[[], None] | None = None,
) -> CreateResumeResult:
    """Create one draft; never uses a direct HTTP request.

    Dry-run only reads the list and wizard DOM.  In particular it never clicks
    the list button, wizard cards, catalog checkboxes, or continue controls.
    """
    goto_hh(page, RESUMES_FULL_LIST_URL)
    # The duplicate check reads the resume-list DOM; on a just-committed SPA
    # page that list may not be hydrated yet, and an unrendered page would read
    # as "no such title" and wrongly permit creation (fail-open, Codex cycle 2).
    # Anchor hydration on the create button, which the list screen always
    # renders once the SPA has drawn the page — the list itself may legitimately
    # be empty, so it cannot be the anchor. wait_until="commit" is insufficient.
    try:
        page.locator(RESUME_CREATE_BUTTON).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        # На лимите hh.ru кнопку не рендерит вовсе. Не маскировать этот
        # наблюдаемый отказ под проблему гидрации/сети: создание без кнопки
        # невозможно, поэтому остаёмся fail-closed.
        if page.locator(RESUME_CREATE_BUTTON).count() == 0:
            return CreateResumeResult(False, reason=_RESUME_LIMIT_REASON)
        return CreateResumeResult(False, reason=f"список резюме не отрисовался: {exc}")
    # Дубль-гард (#304, Codex cycles 2/3) вынесен в resume_titles (#911):
    # должности в аккаунте уникальны, дубликат надо отклонять ДО клика —
    # после клика отказ hh.ru молчит (живая проверка пользователя).
    entries, list_reason = read_account_titles(page)
    if list_reason:
        return CreateResumeResult(False, reason=list_reason)
    duplicate_reason = duplicate_title_reason(entries, title)
    if duplicate_reason:
        return CreateResumeResult(False, reason=duplicate_reason)
    create_button, reason = _one(page, RESUME_CREATE_BUTTON, "кнопка создания резюме")
    if reason:
        return CreateResumeResult(False, reason=reason)
    # count() подтверждает только наличие узла. При исчерпанном лимите hh.ru
    # узел остаётся в DOM, но становится disabled; клик по нему даёт сетевую
    # ошибку и скрывает настоящую причину.
    if _require(create_button).is_disabled():
        return CreateResumeResult(False, reason=_RESUME_LIMIT_REASON)

    if dry_run:
        goto_hh(page, CREATION_URL)
    else:
        try:
            # Баннер cookie-политики ephemeral-конекста перекрывает кнопку
            # создания (живой тур #913, 2026-09-01) — закрыть до клика.
            dismiss_cookie_banner(page)
            _require(create_button).click()
            page.wait_for_url(f"**{RESUME_CREATION_URL}**", wait_until="commit")
        except PlaywrightError as exc:
            return CreateResumeResult(False, reason=f"не удалось открыть визард: {exc}")

    # wait_until="commit" only guarantees the URL changed, not that the SPA
    # has hydrated the wizard screen yet (#304 live run: _one() saw count=0
    # on a still-blank body immediately after commit).
    select_job_locator = page.locator(RESUME_CREATION_SELECT_JOB)
    try:
        select_job_locator.first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return CreateResumeResult(False, reason=f"визард не отрисовался: {exc}")

    count = select_job_locator.count()
    if count != 1:
        return CreateResumeResult(
            False,
            reason=f"карточка выбора профессии не подтверждена однозначно (совпадений: {count})",
        )
    select_job = select_job_locator.first
    if dry_run:
        return CreateResumeResult(True, reason="dry-run; визард найден, клики не выполнены")

    # Шаги ДО точки невозврата: мутация здесь физически невозможна, поэтому
    # PlaywrightError остаётся обычным failed и не блокирует повтор (#777,
    # тот же принцип, что у before_click-seam в CLAUDE.md, раздел 6).
    try:
        switch_reason = _click_until_screen_switches(page, select_job, RESUME_CREATION_POSITION)
        if switch_reason:
            return CreateResumeResult(False, reason=switch_reason)
        position, reason = _one(page, RESUME_CREATION_POSITION, "поле поиска профессии")
        if reason:
            return CreateResumeResult(False, reason=reason)
        _require(position).fill(title)
        # The NEXT control (and the catalog screen after SUBMIT below) renders
        # asynchronously after each input; a strict count()/click right away can
        # see count=0 before the SPA hydrates (same #304 race guarded above).
        page.locator(RESUME_CREATION_NEXT).first.wait_for(state="visible", timeout=15000)
        dismiss_cookie_banner(page)
        reason = _click_one(page, RESUME_CREATION_NEXT, "кнопка продолжения визарда")
        if reason:
            return CreateResumeResult(False, reason=reason)
        category_reason = select_catalog_leaf(page, area)
        if category_reason:
            return CreateResumeResult(False, reason=category_reason)
        page.locator(RESUME_CREATION_NEXT).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return CreateResumeResult(False, reason=f"ошибка до сохранения резюме: {exc}")

    # Точка невозврата: клик ниже создаёт резюме, поэтому ЛЮБОЙ сбой начиная
    # отсюда — uncertain (fail-closed, #176): результат клика не наблюдаем.
    try:
        dismiss_cookie_banner(page)
        reason = _click_one(
            page,
            RESUME_CREATION_NEXT,
            "кнопка продолжения после каталога",
            before_click=before_click,
        )
        if reason:
            return CreateResumeResult(False, reason=reason)
        page.wait_for_url(_RESUME_ID_RE, wait_until="commit")
    except PlaywrightError as exc:
        return CreateResumeResult(
            False, reason=f"ошибка после клика сохранения: {exc}", uncertain=True
        )
    match = _RESUME_ID_RE.search(page.url)
    if not match:
        return CreateResumeResult(
            False, reason="новый resume_id не подтверждён после сохранения", uncertain=True
        )
    return CreateResumeResult(True, new_resume_id=match.group(1), reason="черновик создан")

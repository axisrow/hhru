"""Browser flow for creating an empty resume through the hh.ru UI (#304)."""

from __future__ import annotations

import logging
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
from .resume_ids import RESUME_ID_FROM_PATH_OR_QUERY_RE
from .resume_limits import resume_limit_reason
from .resume_titles import duplicate_title_reason, read_account_titles
from .selector_groups.resume_page import (
    RESUME_CREATE_BUTTON,
    RESUME_CREATION_CATEGORY_INPUT,
    RESUME_CREATION_CATEGORY_SEARCH,
    RESUME_CREATION_CATEGORY_SUBMIT,
    RESUME_CREATION_NEXT,
    RESUME_CREATION_POSITION,
    RESUME_CREATION_POSITION_SUGGEST,
    RESUME_CREATION_SELECT_JOB,
    RESUME_CREATION_URL,
)

CREATION_URL = f"{HH_BASE_URL}{RESUME_CREATION_URL}"

logger = logging.getLogger("hhru_bot.create_resume")
# #837: живой замер показал checked=True уже на первой проверке спустя
# +100мс после клика — секундный запас на порядок больше наблюдавшейся
# задержки, но честный (не бесконечный) дедлайн на случай, если чекбокс
# реально не переключился.
_CHECKBOX_CONFIRM_TIMEOUT = 5.0
# #913 (battle2, живой дамп): «Другое» — вырожденный catch-all лист каталога
# с фиксированным id; id-пространство дерева совместимо с каталогом поиска
# (подтверждено live: 96 <-> tree-selector-item-96). Автопринятие единственного
# кандидата фильтра (#920) никогда не выбирает его — это отказ, а не выбор.
OTHER_ROLE_ID = "40"

# #920 этап 2 (живая диагностика 2026-09-02): на ПЕРВОМ экране визарда поле
# должности — combobox с подсказками автодополнения hh.ru; попап открывается
# только посимвольным вводом (fill() его не триггерит) и существует только на
# этом экране — на экране каталога подсказок нет. Опции
# (RESUME_CREATION_POSITION_SUGGEST) не несут id роли в DOM: маппинг
# «текст профессии -> роль» живёт в JSON ответа shard'а подсказок, который
# страница сама уже получила (слушатель ответов читает его пассивно; прямых
# HTTP-запросов нет — граница «Чтение состояния»). КЛИКАТЬ опцию нельзя:
# после клика визард зависает (поле сбрасывается, чип подсказки остаётся
# отмеченным, повторные NEXT — молчаливые no-op без сетевой активности;
# воспроизведено headless и headed), поэтому подсказка только читается.
# Текст профессии из подсказки в дереве каталога НЕ ищется («Директор
# учебного заведения» -> «Другое»), ищется имя РОЛИ из payload, а id ролей
# совпадает с id листов дерева (подтверждено live: 132 <->
# tree-selector-item-132).
_POSITION_SUGGEST_URL_FRAGMENT = "profession_suggestions"
# #920 (прогон «Логопед» 2026-09-02): число попыток фильтра дерева на один
# вызов select_wizard_catalog_leaf. Нестабильность фильтра hh.ru подтверждена
# пользователем вручную и боевым прогоном (один и тот же запрос: боевой
# прогон -> «Другое», repro -> точный лист); см. комментарий в теле функции.
_FILTER_ATTEMPTS = 2
_SUGGEST_TYPE_DELAY_MS = 40
_SUGGEST_POLL_TIMEOUT = 6.0
_SUGGEST_POLL_INTERVAL_MS = 250


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


def select_wizard_catalog_leaf(
    page: Page,
    area: str,
    *,
    filter_timeout: float = 15.0,
    checkbox_confirm_timeout: float = _CHECKBOX_CONFIRM_TIMEOUT,
    expected_role_id: str | None = None,
) -> str:
    """Select one exact leaf from the resume wizard's profession tree.

    Это каталог ВИЗАРДА резюме (resume_wizard_roles, #908), не каталог
    поиска вакансий: id-пространства дерева модалки и фильтров поиска
    вакансий совместимы (#913, подтверждено live: 96 <->
    ``tree-selector-item-96``), поэтому ``expected_role_id`` — согласованный
    id роли из каталога поиска вакансий. Когда id задан, лист с ДРУГИМ id
    не кликается вовсе — точное совпадение текста ещё не доказывает нужную
    роль, а молчаливая подмена записала бы чужой role_id.

    #920: если точного совпадения нет, а фильтр каталога сузил дерево до
    РОВНО одного кандидата (боевой кейс: «Плотник» -> единственный лист
    «Столяр, плотник»), этот кандидат принимается — человек считает такой
    результат однозначным совпадением, имена листов составные. Принятие
    требует, чтобы запрос содержался в имени листа, и никогда не выбирает
    вырожденное «Другое» (``OTHER_ROLE_ID``); иначе — отказ ДО клика с
    именем листа для повтора. Отказ при нуле и при нескольких кандидатах
    сохранён; все гарды ниже (leaf через чекбокс, ``expected_role_id``,
    подтверждение checked) применяются к принятому кандидату так же, как к
    точному совпадению.
    """
    # The caller arrives right after clicking the wizard's NEXT control, which
    # re-renders the catalog screen asynchronously (React); a strict _one() on
    # the search input immediately after can observe the stale blank body (the
    # same commit-vs-hydration race guarded for SELECT_JOB/POSITION above).
    try:
        page.locator(RESUME_CREATION_CATEGORY_SEARCH).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return f"экран каталога визарда резюме не отрисовался: {exc}"
    search, reason = _one(page, RESUME_CREATION_CATEGORY_SEARCH, "поиск каталога визарда резюме")
    if reason:
        return reason
    # #920 (боевой прогон «Логопед» 2026-09-02): фильтр дерева hh.ru
    # НЕСТАБИЛЕН — на один и тот же запрос (полное имя роли
    # «Учитель, преподаватель, педагог») боевой прогон получил ровно один
    # узел «Другое» (= «нет результатов фильтра»), а повтор живым repro тем
    # же кодом и тем же запросом — точный лист tree-selector-item-132.
    # Причина нестабильности на стороне hh.ru не установлена; пустой
    # результат не отличим от гонки неприменённого фильтра, поэтому отказ
    # класса «точное совпадение не найдено» переспрашивается ОДИН раз
    # (полным повтором fill + опроса). Fail-closed не ослабляется: при
    # стабильном отсутствии профессии повтор возвращает тот же отказ,
    # а гарды #913/#920 и гард expected_role_id ниже выполняются на
    # финальной попытке так же, как раньше.
    fuzzy_matched = False
    refusal = ""
    matches: list[Locator] = []
    for attempt in range(_FILTER_ATTEMPTS):
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
        candidates: list[Locator] = []
        # #920: единственный кандидат последнего КОНСИСТЕНТНОГО чтения дерева.
        # Перезаписывается каждой итерацией опроса, поэтому после выхода из цикла
        # отражает финальное состояние фильтра; рассинхрон длин обнуляет его —
        # на снимке, у которого тексты и элементы от разных рендеров, принимать
        # решение нельзя.
        unique_candidate: Locator | None = None
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
            unique_candidate = None
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
                if len(candidates) == 1:
                    unique_candidate = candidates[0]
            if len(matches) == 1 or time.monotonic() >= deadline:
                break
            page.wait_for_timeout(250)
        if not matches and unique_candidate is not None:
            # #920: фильтр сузил дерево до единственного кандидата без точного
            # равенства («Плотник» -> «Столяр, плотник»). Принять его, а не
            # отказывать, можно только при ОБОИХ гардах: запрос содержится в
            # имени листа (однозначность по-человечески, а не «фильтр вернул
            # что-то одно») и лист не вырожденное «Другое» (#913: catch-all не
            # выбирается никогда). Цикл выше не выходит рано на единственном
            # кандидате НАМЕРЕННО: он ждёт дедлайн, чтобы поймать возможное
            # точное совпадение, и заодно даёт фильтру досидеть до стабильного
            # состояния — transient-снимок с одним узлом посреди перерендера не
            # доживёт до дедлайна, не сменившись финальным деревом.
            # Точечные чтения кандидата выполняются ПОСЛЕ batch-снимка:
            # перерендер дерева между ними отсоединяет хэндл (класс гонки
            # #837). Inline-таймаут ограничивает зависание detached handle,
            # PlaywrightError трактуется как сигнал перерендера — retry/
            # честный отказ, а не generic-краш наверх (#933 контроль).
            try:
                qa = unique_candidate.get_attribute("data-qa", timeout=2000) or ""
                id_match = re.search(r"tree-selector-item-text-(\d+)$", qa)
                leaf_id = id_match.group(1) if id_match else ""
                query = normalize(area)
                if (
                    leaf_id
                    and leaf_id != OTHER_ROLE_ID
                    # Пустой запрос «содержится» в любом имени — не автопринимать
                    # (fail-closed; --area "" проходит argparse).
                    and query
                    # Текст листа берётся из ТОГО ЖЕ batch-снимка, что и сам
                    # кандидат (unique_candidate == candidates[0], длины
                    # сверены выше) — точечное чтение имени не нужно.
                    and query in texts[0]
                ):
                    fuzzy_matched = True
                    matches = [unique_candidate]
                    break
                # Единственный кандидат, но не принят: вырождение в «Другое»
                # (#913), пустой запрос либо запрос не содержится в имени листа.
                # Отказ ДО клика с явным указанием, с каким именем повторять, —
                # прогон не тратит путь в никуда (#920, soft-fail). На первой
                # попытке — переспрос (см. комментарий о нестабильности фильтра
                # выше), на последней — честный отказ.
                text = (unique_candidate.text_content(timeout=2000) or "").strip()
                if leaf_id == OTHER_ROLE_ID:
                    refusal = (
                        f"профессия «{area}» не найдена в каталоге; фильтр выродился "
                        f"в «Другое» (id {OTHER_ROLE_ID}) — повторите с точным именем листа"
                    )
                else:
                    refusal = (
                        f"профессия «{area}» не найдена в каталоге; фильтр предлагает "
                        f"единственный лист «{text}» без точного совпадения — повторите с ним"
                    )
            except PlaywrightError:
                refusal = (
                    "не удалось подтвердить единственный лист каталога "
                    "(дерево перерендерилось); повторите попытку"
                )
        elif not matches:
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
                refusal = (
                    f"профессия «{area}» не найдена в каталоге визарда резюме; "
                    f"дерево предлагает: {options}"
                )
            else:
                refusal = f"профессия «{area}» не найдена в каталоге визарда резюме (список пуст)"
        else:
            break
        if attempt + 1 < _FILTER_ATTEMPTS:
            logger.info(
                "фильтр каталога не дал точного совпадения для «%s» "
                "(попытка %d из %d) — переспрашиваю",
                area,
                attempt + 1,
                _FILTER_ATTEMPTS,
            )
            continue
        return refusal
    if len(matches) > 1:
        return (
            f"профессия «{area}» не найдена однозначно в каталоге визарда "
            f"резюме (совпадений: {len(matches)})"
        )
    qa = matches[0].get_attribute("data-qa") or ""
    match = re.search(r"tree-selector-item-text-(\d+)$", qa)
    if not match:
        return f"пункт каталога визарда «{area}» не является leaf-профессией"
    if expected_role_id is not None and match.group(1) != expected_role_id:
        # Неточная цель вырождается в «Другое» (id 40) или находит лист с тем
        # же текстом, но другим id (#911/#913): «Другое» не выбирать никогда —
        # это отказ, а не выбор. Остановка ДО клика оставляет форму без
        # изменений, и повтор с корректной целью ничего не должен откатывать.
        return (
            f"профессия «{area}» найдена в каталоге визарда с role_id={match.group(1)}, "
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
        return f"профессия «{area}» не отмечена после клика по строке каталога визарда"
    if fuzzy_matched:
        # Только здесь «принят» — правда: leaf подтверждён, role_id (если
        # согласован) совпал, чекбокс отмечен. До этой точки фолбэк #920 мог
        # отказаться на любом гарде выше.
        try:
            accepted_text = (matches[0].text_content(timeout=2000) or "").strip()
        except PlaywrightError:
            # Тот же класс гонки #837, но ПОСЛЕ клика: generic-краш здесь
            # выглядел бы как отказ с фантом-подсказкой, хотя leaf уже
            # подтверждён чекбоксом — в лог идёт имя запроса.
            accepted_text = area
        logger.info(
            "профессия «%s» не совпала с листом каталога точно; "
            "принят единственный кандидат фильтра: «%s»",
            area,
            accepted_text,
        )
    return _click_one(
        page, RESUME_CREATION_CATEGORY_SUBMIT, "кнопка подтверждения каталога визарда"
    )


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


class _SuggestionCapture:
    """Пассивный сборщик ответов shard'а подсказок, полученных страницей (#920).

    Подключается слушателем ``page.on("response", ...)`` на время набора
    запроса: hh.ru сам запрашивает ``profession_suggestions`` при вводе, мы
    только читаем уже полученные телом ответы. Прямых HTTP-вызовов нет —
    это чтение состояния страницы, а не скрытый запрос (см. границу
    браузерных действий в CLAUDE.md). Тело ответа может оказаться
    недоступно Playwright (страница ушла вперёд) — такой ответ пропускается,
    это не фатально: следующий ответ того же shard'а придёт при вводе.
    """

    def __init__(self) -> None:
        self.payloads: list[object] = []

    def __call__(self, response):
        if _POSITION_SUGGEST_URL_FRAGMENT not in response.url:
            return
        try:
            self.payloads.append(response.json())
        except Exception:  # noqa: BLE001 — недоступное тело пропускаем, не падаем
            return


def _role_from_suggestion_payloads(
    payloads: list[object], option_text: str
) -> tuple[str, str] | None:
    """Сопоставить текст опции подсказки с ролью каталога (id, имя) (#920).

    Возвращает None, если текст не найден ни в одном ответе, роль не ровно
    одна, поля пусты ИЛИ разные ответы того же shard'а отрезолвили один
    текст в разные роли — неоднозначный/неполный маппинг не принимается
    (fail-closed): резюме не должно получить профессию, которую никто не
    называл, а first-match закрепил бы возможный неверный role_id.
    """
    found: tuple[str, str] | None = None
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        items = payload.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("text") != option_text:
                continue
            roles = item.get("professionalRoles") or []
            if len(roles) != 1:
                continue
            role = roles[0]
            if not isinstance(role, dict):
                continue
            role_id = str(role.get("id") or "")
            role_name = str(role.get("name") or "")
            if not role_id or not role_name:
                continue
            if found is not None and found[0] != role_id:
                return None
            if found is None:
                found = (role_id, role_name)
    return found


def _read_suggestion_texts(page: Page) -> list[str]:
    locator = page.locator(RESUME_CREATION_POSITION_SUGGEST)
    try:
        return [text.strip() for text in locator.all_text_contents() if text.strip()]
    except PlaywrightError:
        # Попап мог закрыться/перерендериться между итерациями опроса — это
        # сигнал повторить чтение, а не финальная ошибка (тот же принцип,
        # что у опроса дерева в select_wizard_catalog_leaf).
        return []


def _poll_suggestion_texts(
    page: Page, capture: _SuggestionCapture, *, timeout: float | None = None
) -> list[str]:
    """Опрос опций подсказки до двух одинаковых непустых снимков подряд (#920).

    Ответ shard'а и рендер попапа асинхронны: первые чтения видят пустой или
    недостроенный попап, поэтому решение принимается только по стабильному
    снимку (тот же принцип консистентного снимка, что #837 у дерева).
    Стабильность отсчитывается от последнего полученного ответа shard'а:
    новый payload между чтениями означает, что прежние снимки описывали
    недостроенный список — счётчик стабильности сбрасывается (#933 cycle 3:
    гонка «[A],[A] -> пришёл payload -> [A,B]» принимала бы единственную
    подсказку из неполного перечня).
    Пустой список — валидный итог: подсказок по запросу может не быть вовсе,
    тогда выбор остаётся за деревом каталога.
    """
    if timeout is None:
        # Константа читается в момент вызова, а не как дефолт параметра:
        # дефолт фиксируется при определении функции, и тестовая подмена
        # create._SUGGEST_POLL_TIMEOUT его не видела бы.
        timeout = _SUGGEST_POLL_TIMEOUT
    deadline = time.monotonic() + timeout
    previous: list[str] | None = None
    seen_payloads = len(capture.payloads)
    while True:
        texts = _read_suggestion_texts(page)
        if len(capture.payloads) != seen_payloads:
            seen_payloads = len(capture.payloads)
            previous = None
        if texts and texts == previous:
            return texts
        previous = texts
        if time.monotonic() >= deadline:
            return texts
        page.wait_for_timeout(_SUGGEST_POLL_INTERVAL_MS)


def _resolve_leaf_by_suggestions(
    page: Page, position: Locator, area: str
) -> tuple[str, str | None]:
    """#920 этап 2: подсказки автодополнения — приоритетная поверхность выбора.

    Набирает ``area`` посимвольно в поле должности первого экрана, читает
    попап подсказок и пытается сопоставить единственную подсказку с ролью
    каталога по ответам shard'а. Возвращает ``(запрос_для_дерева,
    ожидаемый_role_id | None)`` для ``select_wizard_catalog_leaf``:

    - ровно одна подсказка с однозначной ролью (и не «Другое») -> имя роли
      как запрос дерева + её id как ``expected_role_id`` (гард #913);
    - ноль подсказок, неоднозначный маппинг, «Другое» -> исходный ``area``
      без ожидаемого id — выбор остаётся за деревом каталога с его
      собственными гардами (#920/#913);
    - несколько подсказок -> ни одна не берётся автоматически (выбор между
      несколькими — за штурвалом, #920), перечень пишется в лог, дерево
      получает исходный запрос.

    Подсказка не кликается: клик по опции вешает визард (живая диагностика
    2026-09-02, см. комментарий у констант выше). Поле после опроса остаётся
    с набранным текстом — вызывающий код возвращает туда ``title`` перед
    NEXT, как и делал раньше.
    """
    leaf_area = area
    expected_leaf_id = None
    capture = _SuggestionCapture()
    page.on("response", capture)
    try:
        position.fill("")
        position.press_sequentially(area, delay=_SUGGEST_TYPE_DELAY_MS)
        texts = _poll_suggestion_texts(page, capture)
        if len(texts) > 1:
            logger.info(
                "каталог предлагает несколько подсказок для «%s»: %s; "
                "автоматически ни одна не выбрана, решает дерево каталога",
                area,
                "; ".join(texts),
            )
        elif len(texts) == 1:
            resolved = _role_from_suggestion_payloads(capture.payloads, texts[0])
            if resolved is None:
                logger.info("подсказка «%s» не сопоставлена с ролью каталога однозначно", texts[0])
            elif resolved[0] == OTHER_ROLE_ID:
                logger.info(
                    "подсказка «%s» вырождается в «Другое» (id %s) — не принимается",
                    texts[0],
                    OTHER_ROLE_ID,
                )
            else:
                role_id, role_name = resolved
                leaf_area, expected_leaf_id = role_name, role_id
                logger.info(
                    "принята подсказка каталога: «%s» — профессия «%s», роль «%s» (id %s)",
                    area,
                    texts[0],
                    role_name,
                    role_id,
                )
    finally:
        page.remove_listener("response", capture)
    return leaf_area, expected_leaf_id


def _phantom_draft_hint(title: str) -> str:
    """Предупреждение о возможном фантомном черновике (#920, appendix к отказу).

    Первый «Продолжить» визарда МОЖЕТ материализовать сущность черновика
    (живой факт 2026-09-02; мгновенные CLI-отказы следа не оставляли, но
    гарантии нет), поэтому ЛЮБОЙ отказ после него — и контролируемый отказ
    по профессии, и неожиданная ошибка Playwright — обязан называть title
    для поиска и путь уборки: молчаливый [FAIL] приучает к «на hh.ru
    ничего нет».
    """
    return (
        f" Первый «Продолжить» визарда мог уже создать черновик с title "
        f"«{title}» без профессии — проверьте list-resumes и удалите "
        f"ненужный (delete-resume)."
    )


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
        limit_reason = resume_limit_reason(page.locator(RESUME_CREATE_BUTTON))
        if limit_reason:
            return CreateResumeResult(False, reason=limit_reason)
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
    limit_reason = resume_limit_reason(create_button)
    if limit_reason:
        return CreateResumeResult(False, reason=limit_reason)

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

    # Шаги ДО точки невозврата: классификация исходов CLI здесь — обычный
    # failed/retry (uncertain начинается с финального NEXT, #777, тот же
    # принцип, что у before_click-seam в CLAUDE.md, раздел 6). НО мутация
    # уже возможна: первый «Продолжить» ниже МОЖЕТ материализовать черновик
    # (#920, живой факт 2026-09-02), поэтому любой отказ после него обязан
    # предупреждать о фантоме — включая неожиданные PlaywrightError.

    # Первый try — ДО мутирующего клика: сущности черновика появиться не из
    # чего, фантом-подсказка здесь ложный сигнал (#933 cycle 2), поэтому
    # except без неё.
    try:
        switch_reason = _click_until_screen_switches(page, select_job, RESUME_CREATION_POSITION)
        if switch_reason:
            return CreateResumeResult(False, reason=switch_reason)
        position, reason = _one(page, RESUME_CREATION_POSITION, "поле поиска профессии")
        if reason:
            return CreateResumeResult(False, reason=reason)
        position = _require(position)
        # #920 этап 2: подсказки автодополнения — приоритетная поверхность
        # выбора. Набор area посимвольно читает попап подсказок (id роли —
        # только в payload, опцию кликать нельзя), затем поле возвращается
        # к title: NEXT уходит на каталог свободным текстом, а резолвнутую
        # роль доказывает уже дерево каталога через expected_role_id.
        leaf_area, expected_leaf_id = _resolve_leaf_by_suggestions(page, position, area)
        position.fill(title)
        # The NEXT control (and the catalog screen after SUBMIT below) renders
        # asynchronously after each input; a strict count()/click right away can
        # see count=0 before the SPA hydrates (same #304 race guarded above).
        page.locator(RESUME_CREATION_NEXT).first.wait_for(state="visible", timeout=15000)
        dismiss_cookie_banner(page)
        reason = _click_one(page, RESUME_CREATION_NEXT, "кнопка продолжения визарда")
        if reason:
            return CreateResumeResult(False, reason=reason)
    except PlaywrightError as exc:
        return CreateResumeResult(False, reason=f"ошибка до сохранения резюме: {exc}")

    # Второй try — ПОСЛЕ первого NEXT: мутация уже возможна (#920), любой
    # отказ отсюда обязан предупреждать о фантоме (см. _phantom_draft_hint).
    try:
        category_reason = select_wizard_catalog_leaf(
            page, leaf_area, expected_role_id=expected_leaf_id
        )
        if category_reason:
            # #920 (живой факт 2026-09-02): молчаливый [FAIL] приучал бы к
            # «на hh.ru ничего нет» — см. комментарий над try выше.
            return CreateResumeResult(
                False, reason=f"{category_reason}{_phantom_draft_hint(title)}"
            )
        page.locator(RESUME_CREATION_NEXT).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return CreateResumeResult(
            False, reason=f"ошибка до сохранения резюме: {exc}{_phantom_draft_hint(title)}"
        )

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
        page.wait_for_url(RESUME_ID_FROM_PATH_OR_QUERY_RE, wait_until="commit")
    except PlaywrightError as exc:
        return CreateResumeResult(
            False, reason=f"ошибка после клика сохранения: {exc}", uncertain=True
        )
    match = RESUME_ID_FROM_PATH_OR_QUERY_RE.search(page.url)
    if not match:
        return CreateResumeResult(
            False, reason="новый resume_id не подтверждён после сохранения", uncertain=True
        )
    return CreateResumeResult(True, new_resume_id=match.group(1), reason="черновик создан")

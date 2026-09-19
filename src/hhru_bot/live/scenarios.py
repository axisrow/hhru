"""Сценарии этапа 2 поверх live-канала (#1161/#1162, эпик #588).

Сценарий — это ПОСЛЕДОВАТЕЛЬНОСТЬ примитивов исполнителя S2 (#1160: клик,
чтение DOM-состояния, ожидание условия, запись текста), а не новая семантика
результата. Вся боевая семантика (кулдаун/лимиты/dry-run/статусы history у
bump; дедуп/лимиты/история/анкеты у apply) живёт в командах и боевых модулях —
здесь только маппинг «шаг боевого пути -> вызов примитива канала».

Модуль импортируется без playwright и без пакета ``hhru_bot.live``-транспорта
(#1159, в полёте): сервер импортируется лениво в :meth:`LiveChannel.start`, а
сценарная функция принимает channel-like объект (unit-тесты гоняют её с
FakeChannel на голом main). При расхождении имён действий S2 правится только
таблица ``ACTION_*`` ниже и тест-стражи рядом (test_bump_live/test_apply_live).
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlsplit

# --- Маппинг сценария на действия канала (единая точка контракта S2). ------
# Имена = ACTION_ALLOWLIST extensions/hhru-live (content.js): check_element и
# get_page_state — этап 1 (#930), click_element/wait_element — исполнитель #1160,
# fill_element — текстовый примитив S4 (#1162: заполнение письма). Расхождение
# имён правится таблицей и тест-стражами рядом (test_bump_live /
# test_apply_live), одним коммитом.
ACTION_GET_STATE = "get_page_state"
ACTION_CHECK = "check_element"
ACTION_CLICK = "click_element"
ACTION_WAIT = "wait_element"
ACTION_FILL = "fill_element"

# Состояния для wait: элемент появился/исчез в бюджете ожидания.
WAIT_STATE_VISIBLE = "visible"
WAIT_STATE_HIDDEN = "hidden"

# Транспортные коды, при которых команда УЖЕ ушла в браузер, но исход
# неизвестен (коды protocol.py #1159). Ошибка после форварда с таким кодом —
# честное «действие могло выполниться»; всё остальное (нет клиента, отказ
# policy-ядра расширения, элемент не найден) — клик НЕ произошёл.
FORWARD_UNKNOWN_CODES = frozenset(
    {"timeout", "client_disconnected", "bad_response", "unexpected_message"}
)

# Позитивный маркер успеха (#1161): после реального поднятия hh.ru убирает
# кнопку и показывает disabled-хинт «поднимать рано» (тот же элемент, что
# читает боевой bump.py ДО клика). Бюджет шире боевого BUMP_TIMEOUT_MS:
# ре-рендер списка идёт через сеть и гидрацию React.
MARKER_TIMEOUT_MS = 15_000
# Второй позитивный маркер: кнопка исчезла из карточки, а хинт не появился
# (например, исчерпан дневной лимит hh.ru). Короткий добивочный бюджет.
MARKER_GONE_TIMEOUT_MS = 3_000

# Скоуп кнопки/hint своей карточкой — боевой bump ищет кнопку ВНУТРИ карточки
# резюме (card.locator(...)), на мульти-резюме аккаунта плоский селектор кнопки
# матчил бы чужую карточку. :has() — нативный CSS Chrome; если матчер S2 его
# не примет, клик честно откажется (PrimitiveError, не наш клик), не промахнётся.
RESUME_CARD_SCOPE_TEMPLATE = "[data-qa='resume']:has(a[data-qa='resume-card-link-{resume_id}'])"


def _is_hh_ru_host(netloc: str) -> bool:
    """Строгий гейт хоста вкладки: ровно hh.ru или его поддомен.

    endswith("hh.ru") пропускал бы произвольные хосты вида evil-hh.ru —
    гейт читает и кликает в этой вкладке, строгость стоит столько же.
    """
    return netloc == "hh.ru" or netloc.endswith(".hh.ru")


# --- Бюджеты сценария apply (#1162). Верхняя граница — response_timeout ---
# сервера #1159 (30 с): каждый wait-бюджет умещается в ответ канала целиком.
FORM_WAIT_TIMEOUT_MS = 15_000  # модалка отклика после клика по кнопке (гонка монтажа)
PAGE_FORM_WAIT_TIMEOUT_MS = 10_000  # второй shape: textarea полной страницы
PANEL_WAIT_TIMEOUT_MS = 5_000  # панель выбора резюме открылась/закрылась
LETTER_WAIT_TIMEOUT_MS = 5_000  # textarea после клика по letter-toggle
SUBMIT_WAIT_TIMEOUT_MS = 20_000  # success-маркеры после submit-клика
# Маркер shape «модалка» (надёжный маркер — id формы, не letter-toggle, #1006).
APPLY_MODAL_FORM = "form#RESPONSE_MODAL_FORM_ID"


class ApplyLiveResult:
    """Исход apply_via_live — структурно совместим с ApplyProgress.finish() и
    action_status() (success/uncertain/acted/skipped/skip_reason), как
    BumpResult у боевого bump. question_texts — census-тексты вопросов анкеты
    для очереди обучения (#482); их пишет команда, не сценарий."""

    def __init__(
        self,
        resume_id: str,
        vacancy_id: str,
        success: bool,
        reason: str,
        *,
        acted: bool = False,
        uncertain: bool = False,
        skipped: bool = False,
        skip_reason: str = "",
        question_texts: list[str] | None = None,
    ) -> None:
        self.resume_id = resume_id
        self.vacancy_id = vacancy_id
        self.success = success
        self.reason = reason
        self.acted = acted
        self.uncertain = uncertain
        self.skipped = skipped
        self.skip_reason = skip_reason
        self.question_texts = question_texts or []


def apply_via_live(
    channel,
    resume,
    vacancy,
    letter: str,
    dry_run: bool,
    *,
    verify=None,
    before_submit=None,
    require_resume_select: bool = True,
):
    """Отклик на вакансию через живую вкладку (#1162): зеркало боевого пути.

    Каждый шаг pipeline.py (#207/#1099/#176) превращён в примитив канала,
    вердикты те же. Анкеты не отвечаются (вопросы — в очередь, вакансия skip);
    relocation-попап не подтверждается (форма не отрисуется → серая зона →
    честный not_found). ``verify`` — внешний источник серой зоны #207:
    found → success, not_found → вердикт сайта, не прочитан → uncertain+acted;
    отказ канала при submit — uncertain+acted (#176). Dry-run останавливается
    ДО клика по кнопке отклика: one-click shape через канал недоказуем
    (stop-before-click fail-closed, #1099).
    """
    from ..browser import LOGIN_FORM
    from ..config import is_resume_url_placeholder
    from ..history import SKIP_REASONS
    from ..selector_groups.apply_form import (
        APPLY_COVER_LETTER_TEXTAREA,
        APPLY_COVER_LETTER_TEXTAREA_FORM,
        APPLY_COVER_LETTER_TOGGLE,
        APPLY_COVER_LETTER_TOGGLE_POPUP,
        APPLY_QUESTION_BODY,
        APPLY_QUESTION_TEXT,
        APPLY_RESUME_DROPDOWN,
        APPLY_RESUME_OPTION,
        APPLY_RESUME_SELECT,
        APPLY_SUBMIT_BUTTON,
    )
    from ..selector_groups.vacancy_page import (
        VACANCY_ALREADY_RESPONDED_AGAIN,
        VACANCY_ALREADY_RESPONDED_CHAT,
        VACANCY_APPLY_BUTTON,
    )

    resume_id = resume.resume_id
    vacancy_id = vacancy.vacancy_id

    def _result(
        success: bool,
        reason: str,
        *,
        acted: bool = False,
        uncertain: bool = False,
        skipped: bool = False,
        skip_reason: str = "",
        question_texts: list[str] | None = None,
    ) -> ApplyLiveResult:
        return ApplyLiveResult(
            resume_id,
            vacancy_id,
            success,
            reason,
            acted=acted,
            uncertain=uncertain,
            skipped=skipped,
            skip_reason=skip_reason,
            question_texts=question_texts,
        )

    if is_resume_url_placeholder(resume.resume_url):
        return _result(False, "плейсхолдер resume_url в конфиге — укажите реальный URL")

    # Позитивные success-маркеры submit (#7): только структурные data-qa;
    # vacancy-response-link-top (кнопка отклика позади модалки) и legacy
    # селекторы не идут — ложный positive дал бы выдуманный success.
    success_markers = (
        "[data-qa='vacancy-response-sent-message']",
        "[data-qa='vacancy-response-success']",
        "[data-qa='responded-success-attach-cover-letter']",
    )
    textarea_selector = f"{APPLY_COVER_LETTER_TEXTAREA}, {APPLY_COVER_LETTER_TEXTAREA_FORM}"
    already_selector = f"{VACANCY_ALREADY_RESPONDED_AGAIN}, {VACANCY_ALREADY_RESPONDED_CHAT}"

    def _wait(selector: str, state: str, timeout_ms: int) -> bool:
        try:
            return channel.wait(selector, state, timeout_ms)
        except (ChannelError, PrimitiveError) as exc:
            raise _ScenarioInterrupted(f"канал/исполнитель: {exc}") from exc

    def _click(selector: str, wait_for: dict) -> bool:
        try:
            return channel.click_wait_met(selector, wait_for, allow_apply=True)
        except PrimitiveError as exc:
            if exc.forwarded:
                raise _ScenarioInterrupted(f"клик отправлен, исход неопределён ({exc})") from exc
            raise _RefusedBeforeAction(f"{exc}") from exc
        except ChannelError as exc:
            raise _ScenarioInterrupted(f"канал: {exc}") from exc

    def _read(selector: str) -> dict:
        try:
            return channel.check(selector)
        except (ChannelError, PrimitiveError) as exc:
            raise _ScenarioInterrupted(f"чтение не выполнено ({exc})") from exc

    def _fill(selector: str, text: str) -> dict:
        try:
            return channel.fill(selector, text)
        except (ChannelError, PrimitiveError) as exc:
            raise _ScenarioInterrupted(f"запись письма не выполнена ({exc})") from exc

    def _grey_zone(reason: str, *, acted: bool, uncertain: bool):
        """Финализация fail-исхода ПОСЛЕ клика по кнопке отклика (#207).

        found → success; not_found → вердикт сайта (uncertain снимается —
        список подтверждённо прочитан, отклик не ушёл); не прочитан/упал →
        uncertain+acted (#176). Словарь вердиктов боевого
        _finalize_post_click_failure, ничего не расширяем.
        """
        if verify is None:
            return _result(False, reason, acted=acted, uncertain=uncertain)
        try:
            verdict = verify(vacancy_id, resume_id)
        except Exception as exc:  # noqa: BLE001 — сбой проверки = «не проверили»
            return _result(
                False,
                f"{reason}; внешняя проверка упала ({exc}) — исход неопределён",
                acted=True,
                uncertain=True,
            )
        if getattr(verdict, "found", False):
            return _result(
                True,
                f"внешняя сверка подтвердила отклик в /applicant/negotiations ({verdict.detail})",
                acted=True,
            )
        if getattr(verdict, "indeterminate", False):
            return _result(
                False,
                f"{reason}; внешняя проверка недоступна ({verdict.detail}) — исход неопределён",
                acted=True,
                uncertain=True,
            )
        return _result(
            False,
            f"{reason}; внешняя проверка: отклика в /applicant/negotiations нет",
            acted=acted,
            uncertain=False,
        )

    grey_zone = False  # True с момента клика по кнопке отклика (#207)
    try:
        # --- гейты до клика: чтения, мутировать не могут ---------------------
        try:
            url = str(channel.get_state().get("url", ""))
        except (ChannelError, PrimitiveError) as exc:
            return _result(False, f"канал/исполнитель: {exc}")
        parts = urlsplit(url)
        if parts.path.rstrip("/") != f"/vacancy/{vacancy_id}" or not _is_hh_ru_host(parts.netloc):
            return _result(
                False,
                f"живая вкладка не на странице вакансии {vacancy_id} ({url or 'URL не прочитан'}); "
                "откройте её в вкладке с расширением hhru-live",
            )
        login = _read(LOGIN_FORM)
        if login.get("found") or login.get("visible"):
            return _result(False, "Сессия недействительна: страница содержит форму входа")
        if _read(already_selector).get("found"):
            return _result(
                False,
                "на странице вакансии маркер «уже откликались»",
                skipped=True,
                skip_reason=SKIP_REASONS.ALREADY_APPLIED,
            )
        if not _wait(VACANCY_APPLY_BUTTON, WAIT_STATE_VISIBLE, FORM_WAIT_TIMEOUT_MS):
            return _result(False, "кнопка отклика не найдена на странице")

        if dry_run:
            return _result(
                True,
                "dry-run: план подтверждён, клик не выполнялся (one-click "
                "shape через канал недоказуем — stop-before-click #1099)",
            )

        if before_submit is not None:
            before_submit()

        # Клик кнопки отклика: forwarded-отказ (команда ушла в браузер, ответа
        # нет — полная навигация/обрыв) УЖЕ открывает серую зону: отклик мог
        # уйти одним кликом (#176/#1099). Не-forwarded отказ (policy/цель) —
        # клика не было: общий обработчик ниже вернёт обычный fail.
        try:
            modal_met = _click(
                VACANCY_APPLY_BUTTON,
                {
                    "selector": APPLY_MODAL_FORM,
                    "state": WAIT_STATE_VISIBLE,
                    "timeoutMs": FORM_WAIT_TIMEOUT_MS,
                },
            )
        except _ScenarioInterrupted as exc:
            return _grey_zone(str(exc), acted=True, uncertain=True)

        # --- серая зона #207: клик исполнен, fail-исходы финализирует verify -
        grey_zone = True

        if not modal_met:
            # Модалки нет: либо страница /applicant/vacancy_response (второй
            # shape), либо one-click уже отправил отклик / relocation-попап /
            # терминальный блокер hh.ru. Различает их только внешний источник.
            if not _wait(textarea_selector, WAIT_STATE_VISIBLE, PAGE_FORM_WAIT_TIMEOUT_MS):
                return _grey_zone(
                    "форма отклика не отрисовалась после клика (возможен one-click отклик)",
                    acted=True,
                    uncertain=True,
                )

        # --- форма открыта: анкеты → skip в очередь (#482), канал не отвечает -
        questions = _read(str(APPLY_QUESTION_BODY))
        if questions.get("found") or (questions.get("matchCount") or 0) > 0:
            # Осознанное ограничение: check_element отдаёт census ОДНОГО
            # элемента — в очередь идёт текст первого вопроса; полный список
            # допишет боевой extract_questions на живом прогоне.
            texts: list[str] = []
            question = _read(str(APPLY_QUESTION_TEXT))
            if question.get("text"):
                texts.append(str(question["text"]))
            return _result(
                False,
                f"форма содержит анкету ({questions.get('matchCount')} вопросов) — "
                "вопросы в очередь, канал не отвечает на анкеты сам",
                skipped=True,
                skip_reason=SKIP_REASONS.HAS_QUESTIONS,
                question_texts=texts,
            )

        # --- выбор резюме: пикер обязателен на мульти-резюме (#1144) ---------
        if require_resume_select:
            trigger = str(APPLY_RESUME_SELECT)
            panel = str(APPLY_RESUME_DROPDOWN)
            option = str(APPLY_RESUME_OPTION).format(resume_id=resume_id)
            # Факт ВЫБОРА — aria-selected="true" на опции (документирован
            # apply_form.APPLY_RESUME_DROPDOWN): атрибутный матчинг превращает
            # его в обычное чтение через check_element. Видимость опции и
            # закрытие панели выбор не доказывают — клик мог потеряться в
            # окне гидрации (#858), а submit тогда приложил бы дефолтное
            # резюме (11/11 боевых фактов #1144).
            selected = f"{option}[aria-selected='true']"
            if not _read(trigger).get("found"):
                return _grey_zone(
                    "пикер резюме не найден: подтверждённо приложить нужное резюме "
                    "невозможно — отправка запрещена",
                    acted=False,
                    uncertain=False,
                )
            panel_open = {
                "selector": panel,
                "state": WAIT_STATE_VISIBLE,
                "timeoutMs": PANEL_WAIT_TIMEOUT_MS,
            }
            if not _click(trigger, panel_open):
                return _grey_zone("панель выбора резюме не открылась", acted=False, uncertain=False)
            if not _read(option).get("found"):
                return _grey_zone(
                    f"резюме {resume_id} нет в пикере формы — отправка запрещена",
                    acted=False,
                    uncertain=False,
                )
            if not _click(
                option,
                {
                    "selector": option,
                    "state": WAIT_STATE_VISIBLE,
                    "timeoutMs": PANEL_WAIT_TIMEOUT_MS,
                },
            ):
                return _grey_zone(
                    "клик по опции резюме не подтвердился", acted=False, uncertain=False
                )
            # Панель НЕ закрывается сама (#207-форма): она перекрывает submit
            # физически — закрываем повторным кликом по триггеру и ждём скрытия
            # САМОЙ панели (опции внутри остаются visible, пока открыта).
            if not _click(
                trigger,
                {"selector": panel, "state": WAIT_STATE_HIDDEN, "timeoutMs": PANEL_WAIT_TIMEOUT_MS},
            ):
                return _grey_zone(
                    "панель выбора резюме не закрылась — submit перекрыт, отправка запрещена",
                    acted=False,
                    uncertain=False,
                )
            if not _read(selected).get("found"):
                return _grey_zone(
                    f"выбор резюме {resume_id} не подтверждён (нет aria-selected) — "
                    "отправка запрещена",
                    acted=False,
                    uncertain=False,
                )

        # --- письмо: отсутствие textarea = fail-closed отказ ДО submit --------
        if not _wait(textarea_selector, WAIT_STATE_VISIBLE, LETTER_WAIT_TIMEOUT_MS):
            # Оба варианта тоггла МОГУТ сосуществовать в DOM (дампы 2026-08-20,
            # apply_form.py) — OR-селектор дал бы ambiguous_target, поэтому
            # кликаем по одному: первый отсутствующий/невидимый откатывает ко
            # второму. Тоггла может не быть вовсе, когда hh.ru отрендерил
            # textarea уже развёрнутой — его отсутствие не отказ: решает
            # повторная проверка textarea.
            expanded = False
            for toggle in (str(APPLY_COVER_LETTER_TOGGLE_POPUP), str(APPLY_COVER_LETTER_TOGGLE)):
                try:
                    if _click(
                        toggle,
                        {
                            "selector": textarea_selector,
                            "state": WAIT_STATE_VISIBLE,
                            "timeoutMs": LETTER_WAIT_TIMEOUT_MS,
                        },
                    ):
                        expanded = True
                        break
                except _RefusedBeforeAction:
                    continue
            if not expanded and not _wait(
                textarea_selector, WAIT_STATE_VISIBLE, LETTER_WAIT_TIMEOUT_MS
            ):
                return _grey_zone(
                    "textarea письма не найдена в обоих shape — отклик без письма "
                    "не отправляем (fail-closed до submit)",
                    acted=False,
                    uncertain=False,
                )
        fill = _fill(textarea_selector, letter)
        if not fill.get("filled", False):
            return _grey_zone(
                "письмо не подтвердилось в поле (read-back не совпал) — не отправляем",
                acted=False,
                uncertain=False,
            )

        # --- submit: отказ канала здесь = uncertain+acted (#176) --------------
        submitted = _click(
            APPLY_SUBMIT_BUTTON,
            {
                "selector": ", ".join(success_markers),
                "state": WAIT_STATE_VISIBLE,
                "timeoutMs": SUBMIT_WAIT_TIMEOUT_MS,
            },
        )
    except _ScenarioInterrupted as exc:
        if not grey_zone:
            # Чтение/клик не состоялись ДО кнопки отклика — на hh.ru следа нет.
            return _result(False, str(exc))
        # Отказ ПОСЛЕ клика (обрыв/таймаут/полная навигация): отправка могла
        # состояться на любом шаге — решает внешний источник (#176).
        return _grey_zone(str(exc), acted=True, uncertain=True)
    except _RefusedBeforeAction as exc:
        if not grey_zone:
            return _result(False, f"клик по кнопке отклика не выполнен: {exc}")
        # Отказ исполнителя до конкретного клика: мутации не было (как у
        # PlaywrightError заполнения в боевом пути) — флаги чистые; вердикт
        # всё равно финализирует внешний источник.
        return _grey_zone(f"шаг формы не выполнен: {exc}", acted=False, uncertain=False)

    if submitted:
        return _result(True, "success: маркер отправки подтверждён в живой вкладке", acted=True)
    return _grey_zone(
        "маркер успешной отправки не подтвердился за бюджет",
        acted=True,
        uncertain=True,
    )


class _ScenarioInterrupted(Exception):
    """Команда ушла в браузер/канал потерян — исход действия неизвестен."""


class _RefusedBeforeAction(Exception):
    """Исполнитель отказал ДО действия (policy/цель) — мутации не было."""


class ChannelError(Exception):
    """Канал недоступен (нет клиента, сервер не поднялся) — сценарий не начат."""


def bump_via_live(channel, resume, dry_run: bool):
    """Поднятие резюме через живую вкладку (#1161): зеркало боевого bump_resume.

    Каждый шаг боевого пути (bump.py) превращён в примитив канала; порядок и
    вердикты совпадают. Транспортные отказы до клика — обычный failed
    (acted=False); после клика — acted=True и честный uncertain (fail-closed
    #176: действие могло дойти до hh.ru). Возвращает ``bump.BumpResult`` —
    та же структура, что у боевого пути, команде неважно, каким транспортом
    получен исход.
    """
    from ..browser import LOGIN_FORM
    from ..bump import (
        BUMP_HINT_TIMEOUT_MS,
        BUMP_TIMEOUT_MS,
        RESUMES_LIST_URL,
        BumpResult,
    )
    from ..config import is_resume_url_placeholder
    from ..selector_groups.resume_list import RESUME_LIST_CARD_LINK_PREFIX
    from ..selector_groups.resume_page import (
        RESUME_BUMP_BUTTON,
        RESUME_BUMP_DISABLED_HINT,
        RESUME_CARD_LINK_TEMPLATE,
    )

    if is_resume_url_placeholder(resume.resume_url):
        return BumpResult(
            resume.id,
            False,
            "В конфиге указан плейсхолдер resume_url; укажите реальный URL "
            "(получить можно через list-resumes)",
        )

    try:
        url = str(channel.get_state().get("url", ""))
        parts = urlsplit(url)
        if parts.path != "/applicant/resumes" or not _is_hh_ru_host(parts.netloc):
            return BumpResult(
                resume.id,
                False,
                f"живая вкладка не на списке резюме ({url or 'URL не прочитан'}); "
                f"откройте {RESUMES_LIST_URL} в вкладке с расширением hhru-live",
            )
        login = channel.check(LOGIN_FORM)
        if login.get("found") or login.get("visible"):
            return BumpResult(
                resume.id,
                False,
                "Сессия недействительна: страница содержит форму входа. Выполните login.",
            )

        card_link = RESUME_CARD_LINK_TEMPLATE.format(resume_id=resume.resume_id)
        scope = RESUME_CARD_SCOPE_TEMPLATE.format(resume_id=resume.resume_id)
        in_scope_button = f"{scope} {RESUME_BUMP_BUTTON}"
        in_scope_hint = f"{scope} {RESUME_BUMP_DISABLED_HINT}"

        if not channel.wait(RESUME_LIST_CARD_LINK_PREFIX, WAIT_STATE_VISIBLE, BUMP_TIMEOUT_MS):
            return BumpResult(
                resume.id,
                False,
                f"список резюме не отрисовался за {BUMP_TIMEOUT_MS // 1000} с "
                "(гидрация/медленная загрузка) — наличие резюме не подтверждено; "
                "повторите запуск позже",
            )
        if not channel.wait(card_link, WAIT_STATE_VISIBLE, BUMP_TIMEOUT_MS):
            return BumpResult(
                resume.id,
                False,
                "резюме не найдено в списке /applicant/resumes (удалено или недоступно)",
            )
        if channel.wait(in_scope_hint, WAIT_STATE_VISIBLE, BUMP_HINT_TIMEOUT_MS):
            return BumpResult(resume.id, False, "hh.ru сообщает, что поднимать ещё рано")
        if not channel.wait(in_scope_button, WAIT_STATE_VISIBLE, BUMP_TIMEOUT_MS):
            return BumpResult(resume.id, False, "кнопка поднятия резюме не найдена на странице")
    except (ChannelError, PrimitiveError) as exc:
        # Чтения до клика мутировать не могут — обычный отказ, повтор возможен.
        return BumpResult(resume.id, False, f"канал/исполнитель: {exc}")

    if dry_run:
        return BumpResult(resume.id, True, "dry-run")

    try:
        channel.click(
            in_scope_button,
            # Объявленное post-click условие обязательно (#1160: wait_required
            # отказ до клика): hh.ru после поднятия снимает кнопку и показывает
            # кулдаун-хинт — он и есть условие. Тот же маркер проверяется ниже
            # как позитивный исход; здесь он нужен исполнителю ДО клика.
            wait_for={
                "selector": in_scope_hint,
                "state": WAIT_STATE_VISIBLE,
                "timeoutMs": MARKER_TIMEOUT_MS,
            },
        )
    except PrimitiveError as exc:
        if exc.forwarded:
            # Команда ушла в браузер, ответа/исхода нет — как PlaywrightError
            # в момент клика у боевого пути: acted+uncertain (#176).
            return BumpResult(
                resume.id,
                False,
                f"клик поднятия отправлен, исход неопределён ({exc})",
                acted=True,
                uncertain=True,
            )
        return BumpResult(resume.id, False, f"клик не выполнен: {exc}")

    # Позитивный маркер успеха (#1161): кулдаун-хинт появился (hh.ru снял
    # кнопку) или хотя бы кнопка исчезла из карточки. Ни один не подтверждён —
    # выдуманный успех запрещён: acted+uncertain.
    try:
        if channel.wait(in_scope_hint, WAIT_STATE_VISIBLE, MARKER_TIMEOUT_MS):
            return BumpResult(resume.id, True, "success", acted=True)
        if not channel.wait(in_scope_button, WAIT_STATE_VISIBLE, MARKER_GONE_TIMEOUT_MS):
            return BumpResult(resume.id, True, "success: кнопка поднятия исчезла", acted=True)
        return BumpResult(
            resume.id,
            False,
            "клик выполнен, маркеры успеха не подтвердились за бюджет — исход неопределён",
            acted=True,
            uncertain=True,
        )
    except (ChannelError, PrimitiveError) as exc:
        return BumpResult(
            resume.id,
            False,
            f"клик выполнен, подтверждение не прочитано ({exc})",
            acted=True,
            uncertain=True,
        )


class PrimitiveError(Exception):
    """Действие вернуло status=error (код транспорта или самого расширения)."""

    def __init__(self, code: str, detail: str, forwarded: bool) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        # True — команда была переслана в браузер, исход неизвестен.
        self.forwarded = forwarded


class _Collector:
    """TextIO-приёмник stdout сервера: строка-ответ -> ожидание в call()."""

    def __init__(self, events: dict[str | int, threading.Event]) -> None:
        self._buf = ""
        self._lock = threading.Lock()
        self._events = events
        self.responses: dict[str | int, dict] = {}

    def write(self, text: str) -> int:
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._handle(line)
        return len(text)

    def flush(self) -> None:
        pass

    def _handle(self, line: str) -> None:
        if not line or line.startswith("[INFO]"):
            if line:
                print(line)
            return
        try:
            obj = json.loads(line)
        except ValueError:
            print(line)
            return
        if isinstance(obj, dict) and "id" in obj:
            self.responses[obj["id"]] = obj
            event = self._events.get(obj["id"])
            if event is not None:
                event.set()


class LiveChannel:
    """Клиент сценария поверх сервера #1159: команда -> ответ, по одной.

    Сервер живёт в потоке процесса (foreground-инвариант #1159 не нарушен:
    поток гасится закрытием stdin-канала и вместе с процессом, фонового
    демона нет). Команды подаются в select-цикл сервера через pipe — код
    сервера не дублируется и не правится (файл — владение S1).
    """

    def __init__(self, port: int = 0, client_timeout: float = 120.0) -> None:
        self.port = port
        self.client_timeout = client_timeout
        self._server = None  # LiveServeServer, создаётся в start()
        self._sink_fd: int | None = None
        self._source_fd: int | None = None
        self._collector: _Collector | None = None
        self._events: dict[str | int, threading.Event] = {}
        self._thread: threading.Thread | None = None
        self._counter = 0

    def start(self) -> str:
        """Поднять сервер; вернуть ws://URL для расширения."""
        from .server import LiveServeServer

        server = LiveServeServer(port=self.port)
        try:
            server.bind()
        except OSError as exc:
            raise ChannelError(f"не удалось занять порт {self.port}: {exc}") from exc
        self._server = server
        self._source_fd, self._sink_fd = os.pipe()
        self._collector = _Collector(self._events)
        self._thread = threading.Thread(
            target=server.serve, args=(self._source_fd, self._collector), daemon=True
        )
        self._thread.start()
        return server.url

    def wait_client(self) -> None:
        """Дождаться подключения расширения; ChannelError по таймауту."""
        assert self._server is not None and self._thread is not None
        deadline = time.monotonic() + self.client_timeout
        while time.monotonic() < deadline:
            if self._server.connections_seen:
                return
            if not self._thread.is_alive():
                raise ChannelError("сервер канала остановился до подключения клиента")
            time.sleep(0.2)
        raise ChannelError(
            f"расширение hhru-live не подключилось за {self.client_timeout:.0f} с "
            "(нужна вкладка hh.ru в Chrome с включённым расширением)"
        )

    # -- примитивы (маппинг на действия #1160) --------------------------------

    def get_state(self) -> dict:
        """{url, title, readyState} вкладки; расширение заворачивает их в 'page'."""
        result = self._call(ACTION_GET_STATE, {})
        return result.get("page", result)

    def check(self, selector: str) -> dict:
        result = self._call(ACTION_CHECK, {"selector": selector})
        return result.get("element", result)

    def click(self, selector: str, wait_for: dict | None = None, allow_apply: bool = False) -> dict:
        """Клик по селектору. wait_for — ОБЯЗАТЕЛЬНОЕ post-click условие
        протокола исполнителя ({selector|dataQa|label, state, timeoutMs}):
        без него расширение отказывает (wait_required) ДО клика.
        allow_apply — явная авторизация apply-шага сценария отклика (#1162):
        policy-ядро иначе отказывает apply-цели (apply_step) без клика.
        """
        payload: dict = {"selector": selector}
        if wait_for is not None:
            payload["waitFor"] = wait_for
        if allow_apply:
            payload["allowApply"] = True
        result = self._call(ACTION_CLICK, payload)
        return result

    def click_wait_met(self, selector: str, wait_for: dict, allow_apply: bool = False) -> bool:
        """Клик + факт исполнения объявленного post-click условия (wait.met)."""
        result = self.click(selector, wait_for=wait_for, allow_apply=allow_apply)
        return bool((result.get("wait") or {}).get("met", False))

    def fill(self, selector: str, text: str) -> dict:
        """Записать текст в поле (native setter + input/change; read-back в ответе)."""
        return self._call(ACTION_FILL, {"selector": selector, "text": text})

    def wait(self, selector: str, state: str, timeout_ms: int) -> bool:
        """Дождаться состояния селектора; False — бюджет истёк без события."""
        result = self._call(
            ACTION_WAIT, {"selector": selector, "state": state, "timeoutMs": timeout_ms}
        )
        # Ключ ответа — часть контракта S2: исполнитель кладёт факт в wait.met.
        return bool((result.get("wait") or result).get("met", result.get("conditionMet", False)))

    def close(self) -> None:
        """EOF в stdin сервера — цикл завершается (foreground-семантика #1159)."""
        if self._sink_fd is not None:
            os.close(self._sink_fd)
            self._sink_fd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._source_fd is not None:
            os.close(self._source_fd)
            self._source_fd = None

    # -- транспорт ------------------------------------------------------------

    def _call(self, action: str, payload: dict) -> dict:
        from .protocol import PROTOCOL_VERSION, serialize

        assert self._server is not None and self._sink_fd is not None
        assert self._collector is not None
        self._counter += 1
        command_id = f"c{self._counter}"
        done = threading.Event()
        self._events[command_id] = done
        envelope = serialize(
            {"v": PROTOCOL_VERSION, "id": command_id, "action": action, "payload": payload}
        )
        os.write(self._sink_fd, (envelope + "\n").encode("utf-8"))
        # Сервер сам отвечает TIMEOUT-ошибкой по своему response_timeout;
        # клиентский бюджет — с запасом на планировщик, не вместо него.
        if not done.wait(self._server.response_timeout + 10):
            self._events.pop(command_id, None)
            self._collector.responses.pop(command_id, None)
            raise PrimitiveError("timeout", "ответ канала не получен", forwarded=True)
        self._events.pop(command_id, None)
        response = self._collector.responses.pop(command_id)
        if response.get("status") != "ok":
            result = response.get("result") or {}
            code = str(result.get("code", "unknown"))
            detail = str(result.get("detail", ""))
            raise PrimitiveError(code, detail, forwarded=code in FORWARD_UNKNOWN_CODES)
        return response.get("result") or {}

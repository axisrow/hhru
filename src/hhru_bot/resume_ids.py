"""Чтение идентификаторов резюме: URL, карточка списка, SSR (#891).

``create_resume``/``copy_resume``/``delete_resume`` исторически независимо
решали одну задачу — «какой это resume_id и откуда он доказан» — параллельными
regex (почти одинаковыми по смыслу, но с разными вариантами матчинга), тремя
копиями обхода data-qa-ссылок карточек и двумя копиями обхода SSR
``applicantResumes``. Модуль собирает сами ЧТЕНИЯ в одном месте.

Граница модуля: только чтение идентификаторов. Решения об успехе/отказе,
строгие проверки (``count != 1``, fail-closed причины, ``uncertain``) и их
формулировки остаются у вызывающих модулей — рефакторинг #891 сознательно
сохраняет наблюдаемое поведение и сообщения трёх боевых команд, поэтому
таксономии ошибок (в том числе разные по строгости) сюда НЕ переезжают.
"""

from __future__ import annotations

import re

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .negotiations_probe import parse_initial_state
from .selector_groups.resume_list import (
    RESUME_LIST_CARD,
    RESUME_LIST_CARD_LINK_QA_PREFIX,
    RESUME_LIST_CARD_LINK_TPL,
)

# resume_id в PATH-форме URL: /resume/<hash>. Единственная форма, которую
# признаёт copy-resume: URL после WRITE-клика там только КАНДИДАТ (SPA hh.ru
# при клонировании часто не меняет URL вовсе), поэтому матч сам по себе ничего
# не доказывает — финальное слово за diff'ом карточек списка
# (copy_resume._reconcile_created_resume).
RESUME_ID_FROM_PATH_RE = re.compile(r"/resume/([0-9a-f]{32,40})")

# resume_id в ДВУХ формах URL, которыми hh.ru подтверждает сохранение (#778):
# прямой страницей резюме (/resume/<id>) и следующим шагом визарда, где id
# уходит в query-параметр (/profile/resume/educations?resume=<id>&hhtmFrom=...).
# Боевой прогон #778 наблюдал вторую: резюме создавалось, но ожидание первой
# формы падало по таймауту и давало uncertain при фактическом успехе. Эту
# форму доверяет только create-resume (wait_for_url + search(page.url)).
RESUME_ID_FROM_PATH_OR_QUERY_RE = re.compile(
    r"(?:/resume/|[?&]resume=)([0-9a-f]{32,40})(?:[/?#&]|$)"
)


def card_resume_id(card: Locator) -> str:
    """Прочитать resume_id из data-qa ссылки внутри одной карточки списка.

    Хвост ``resume-card-link-<hash>`` — и есть resume_id (identity-bound,
    #33). Пустая строка — ссылка-хэш не найдена или её data-qa без префикса:
    вызывающий код обязан трактовать это как дрейф разметки, а не молча
    пропускать карточку (тот же инвариант, что у ``list_resume_cards``, PR
    #322: частичный список — не список).
    """
    for link in card.locator(f"[data-qa^='{RESUME_LIST_CARD_LINK_QA_PREFIX}']").all():
        qa = link.get_attribute("data-qa") or ""
        if qa.startswith(RESUME_LIST_CARD_LINK_QA_PREFIX):
            return qa[len(RESUME_LIST_CARD_LINK_QA_PREFIX) :]
    return ""


def page_card_hashes(page: Page) -> set[str]:
    """Хэши всех резюме в списке (для diff карточек до/после мутации)."""
    hashes: set[str] = set()
    for link in page.locator(f"[data-qa^='{RESUME_LIST_CARD_LINK_QA_PREFIX}']").all():
        qa = link.get_attribute("data-qa") or ""
        if qa.startswith(RESUME_LIST_CARD_LINK_QA_PREFIX):
            hashes.add(qa[len(RESUME_LIST_CARD_LINK_QA_PREFIX) :])
    return hashes


def resume_card_locator(page: Page, resume_id: str) -> Locator:
    """Identity-bound карточка списка: карточка, содержащая ссылку на resume_id.

    Один конструктор селектора для copy-resume/delete-resume: привязка карточки
    к конкретному резюме через ``resume-card-link-<hash>`` — общий источник
    истины identity-проверки (#33) у обеих команд.
    """
    return page.locator(
        f"{RESUME_LIST_CARD}:has({RESUME_LIST_CARD_LINK_TPL.format(resume_id=resume_id)})"
    )


def resume_item_attrs(item) -> dict | None:  # noqa: ANN001
    """``_attributes`` одного элемента SSR ``applicantResumes``; ``None`` — невалиден.

    Элемент без ``_attributes`` (или вовсе не dict) — частичный payload; что
    с ним делать (skip / пометить список недоступным), решает вызывающий код.
    """
    attrs = item.get("_attributes") if isinstance(item, dict) else None
    return attrs if isinstance(attrs, dict) else None


def read_ssr_resume_items(page: Page) -> tuple[list, str]:
    """Прочитать список ``applicantResumes`` из SSR-состояния страницы списка.

    Возвращает ``(items, reason)``: непустая ``reason`` — SSR недоступен (шаблон
    ``HH-Lux-InitialState`` не найден, JSON невалиден или не объект, секция
    ``applicantResumes`` отсутствует, пуста или не список). Честно пустая секция
    здесь тоже недоступность: пустой SSR неотличим от незагрузившегося
    (``list_resume_cards``). Читающему родство клонов (``_resume_lineage``)
    reason безразличен: пустое/нечитаемое — пустое доказательство, никогда не
    доказательство отсутствия связи.
    """
    try:
        state = parse_initial_state(page.content())
    except (ValueError, AttributeError, PlaywrightError, PlaywrightTimeoutError) as exc:
        return [], f"SSR-состояние не прочитано: {exc}"
    if not isinstance(state, dict):
        # parse_initial_state возвращает любой валидный JSON: null/массив/строка
        # (schema-drift, интерстишл) — недоступность, не «резюме нет».
        return [], f"SSR-состояние не объект ({type(state).__name__})"
    resumes = state.get("applicantResumes")
    if not isinstance(resumes, list) or not resumes:
        return [], "секция applicantResumes отсутствует, пуста или не список"
    return resumes, ""

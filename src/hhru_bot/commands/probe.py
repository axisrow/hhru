"""Команда probe (#8, #88): диагностика селекторов hh.ru без отклика.

Top-level команда `hhru_bot probe ...` — регистрируется автоматически через
pkgutil.iter_modules в cli.register_commands (cli.py не трогается).

Два read-only режима:

* По умолчанию (#8): доходит до формы отклика целевой вакансии, заполняет
  сопроводительное письмо и сдампит screenshot + HTML в data/logs/, после чего
  останавливается. submit не вызывается — ничего не отправляется. По дампу
  сверяются непроверенные селекторы формы отклика (см. #10).
* ``--healthcheck`` (#88): открывает ключевые страницы hh.ru (search/
  negotiations/resume — URL без контекста конкретной вакансии) и считает
  ``locator.count()`` для селекторов из ``selector_groups/``. Read-only: только
  ``goto`` + ``count``, никаких кликов apply/отправки. Вывод — ASCII-таблица
  ``page | selector | status | count`` со статусами OK (>0) / NOT_FOUND (обязательный,
  0 — провал) / OPTIONAL_ABSENT (опциональный, легитимно отсутствует). Помогает при
  регрессии (CLAUDE.md: «первый подозреваемый — устаревший селектор») до реального
  падения команды. Итоговый [FAIL] считается только по обязательным селекторам.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..browser import PAGE_STATE, goto_hh, has_login_form
from ..config import is_resume_url_placeholder
from ..exit_codes import CommandExitCode
from ._common import _build_letter_provider, add_common_args, resolve_resumes

logger = logging.getLogger("hhru_bot.cli")

# ``[data-qa='name']`` → name. Селекторы hh.ru в этом проекте — только этот вид
# (см. selector_groups/). Нужен, чтобы перечислить ключевые селекторы healthcheck
# именами констант в выводе, не таща полноценный CSS-парсер.
_QA_RE = re.compile(r"^\[data-qa=(?:['\"])([A-Za-z0-9_\-]+)(?:['\"])\]$")


def _parse_qa_selector(selector: str) -> str | None:
    """Извлекает data-qa-имя из ``[data-qa='...']``; иначе None.

    Селекторы проекта — однородные (только data-qa). None здесь значит
    «селектор не нашего вида» — в FakePage/test'е count() по нему вернёт 0, как
    и по пустому DOM. В боевом Playwright count() работает с любым CSS-селектором.
    """
    m = _QA_RE.match(selector.strip())
    return m.group(1) if m else None


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "probe",
        help="Дамп формы отклика одной вакансии без отправки (диагностика селекторов)",
    )
    add_common_args(p)
    p.add_argument(
        "--vacancy-id",
        help="ID целевой вакансии (число из URL https://hh.ru/vacancy/<id>)",
    )
    p.add_argument(
        "--vacancy-url",
        help="URL целевой вакансии (альтернатива --vacancy-id)",
    )
    p.add_argument(
        "--start-page",
        type=int,
        default=0,
        help="Начальная страница поиска (нумерация с 0)",
    )
    p.add_argument(
        "--healthcheck",
        action="store_true",
        help="Read-only проверка ключевых селекторов hh.ru (OK/NOT_FOUND) без отклика (#88)",
    )
    p.add_argument(
        "--negotiations",
        action="store_true",
        help="Read-only дамп списка переговоров или чата без отправки (#107)",
    )
    p.add_argument(
        "--topic",
        help="ID topic из SSR-дампа negotiations для открытия чата (только чтение)",
    )
    p.add_argument(
        "--questionnaires-only",
        action="store_true",
        help=(
            "Read-only на hh.ru bulk-проверка анкет; подтверждённые вопросы "
            "пишутся в локальную SQLite, без заполнения, AI и submit"
        ),
    )
    p.add_argument(
        "--limit-questionnaires",
        type=int,
        default=0,
        help="Остановить bulk-проверку после N подтверждённых анкет (0 — без лимита)",
    )
    p.set_defaults(func=run)


# --- healthcheck (#88): чистая read-only логика, тестируемая без браузера ---

# Статус одного селектора. OK = найден (>0). NOT_FOUND = обязательный, но 0
# совпадений (провал). OPTIONAL_ABSENT = опциональный и отсутствует (легитимно —
# пагинация в конце выдачи, compensation после magritte-перехода hh.ru 2025).
STATUS_OK = "OK"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_OPTIONAL_ABSENT = "OPTIONAL_ABSENT"
# #120: страница не открылась (таймаут/сеть) — селекторы не проверялись.
STATUS_UNREACHABLE = PAGE_STATE["unreachable"].upper()
STATUS_UNAUTHENTICATED = PAGE_STATE["unauthenticated"].upper()
# The URL was never opened: it is the placeholder shipped in config.example.yaml.
STATUS_PLACEHOLDER = "PLACEHOLDER_CONFIG"


@dataclass
class SelectorCheck:
    """Результат проверки одного селектора на странице.

    ``name`` — человекочитаемая метка (имя константы из selector_groups),
    ``selector`` — исходный CSS, ``found`` — ``locator.count()``,
    ``required`` — обязателен ли селектор (False = легитимно может отсутствовать).

    Различение required/optional критично (Codex F1): иначе любой здоровый
    аккаунт репортится [FAIL] из-за селекторов, которые hh.ru НЕ рендерит по
    дизайну (compensation после magritte, пагинация в конце выдачи).
    """

    name: str
    selector: str
    found: int
    required: bool = True

    @property
    def status(self) -> str:
        if self.found > 0:
            return STATUS_OK
        return STATUS_NOT_FOUND if self.required else STATUS_OPTIONAL_ABSENT

    @property
    def fails(self) -> bool:
        """Провал = обязательный селектор не найден. Optional-ABSENT НЕ провал."""
        return self.required and self.found == 0


@dataclass
class PageCheck:
    """Результат проверки одной страницы: URL + список статусов селекторов."""

    name: str
    url: str
    results: list[SelectorCheck] = field(default_factory=list)
    # #120: страница не открылась (таймаут/сетевая ошибка). Селекторы не
    # проверялись — это не «все NOT_FOUND», а «проверка не состоялась».
    unreachable: bool = False
    unauthenticated: bool = False
    placeholder: bool = False

    @property
    def page_state(self) -> str:
        """Общее состояние страницы без изменения старого bool-контракта."""
        if self.unreachable:
            return PAGE_STATE["unreachable"]
        if self.unauthenticated:
            return PAGE_STATE["unauthenticated"]
        if self.placeholder:
            return PAGE_STATE["placeholder"]
        return PAGE_STATE["confirmed"]


def check_selectors(page, spec, page_loader=None):
    """Read-only прогон селекторов по страницам.

    ``spec`` — список ``(page_name, url, [(name, selector, required?), ...])``.
    ``required`` опционален (по умолчанию True — обратная совместимость с 2-tuple
    ``(name, selector)``). Для каждой страницы: открыть через ``goto_hh`` (с retry
    и backoff, как у всех путей hh.ru) и для каждого селектора взять
    ``page.locator(selector).count()`` → SelectorCheck. Ничего не кликает и не
    отправляет — это главный инвариант #88 (read-only).

    ``page_loader(page, url, name)`` — опциональный хук, вызываемый ПОСЛЕ goto:
    в боевом прогоне не нужен (DOM рендерит живой hh.ru), а в тестах через него
    подменяют HTML фикстурой (имитация «браузер загрузил страницу»).

    #120: недоступность страницы — это РЕЗУЛЬТАТ проверки, а не авария.
    ``goto_hh`` исчерпал retry (до 3 попыток) и пробросил PlaywrightTimeoutError/
    PlaywrightError — отмечаем страницу ``unreachable`` и идём дальше, таблица
    печатается всегда. Ловим именно эти два класса: ``goto_hh`` рейзит их, а
    широкий ``except Exception`` прятал бы баги в самом коде.
    """
    pages: list[PageCheck] = []
    for name, url, selectors in spec:
        if is_resume_url_placeholder(url):
            logger.warning(
                "healthcheck: страница '%s' не проверялась: resume_url содержит "
                "плейсхолдер; укажите реальный URL (получить можно через list-resumes)",
                name,
            )
            pages.append(PageCheck(name=name, url=url, results=[], placeholder=True))
            continue
        try:
            goto_hh(page, url)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            logger.warning("healthcheck: страница '%s' недоступна (%s): %s", name, url, exc)
            pages.append(PageCheck(name=name, url=url, results=[], unreachable=True))
            continue
        if page_loader is not None:
            page_loader(page, url, name)
        if has_login_form(page):
            logger.warning("healthcheck: страница '%s' требует авторизацию", name)
            pages.append(PageCheck(name=name, url=url, results=[], unauthenticated=True))
            continue
        results = [
            SelectorCheck(
                name=sel_name,
                selector=sel,
                found=page.locator(sel).count(),
                required=sel_required,
            )
            for sel_name, sel, sel_required in (_with_required(s) for s in selectors)
        ]
        pages.append(PageCheck(name=name, url=url, results=results))
    return pages


def _with_required(sel):
    """Нормализует элемент spec в 3-tuple ``(name, selector, required)``.

    Принимает 2-tuple ``(name, selector)`` (required=True по умолчанию) и
    3-tuple ``(name, selector, required)``. Так старые тесты/spec без required
    остаются обязательными, а новые помечают опциональные селекторы явно.
    """
    if len(sel) == 3:
        return sel[0], sel[1], sel[2]
    return sel[0], sel[1], True


def format_healthcheck_table(pages: list[PageCheck]) -> str:
    """ASCII-таблица статусов: ``страница | selector | status | count``.

    Переиспользует ``report._ascii_table`` (общая ASCII-рамка проекта) — НЕ
    дублирует отрисовку. Пустой список всё равно рисует шапку (контракт таблицы).
    """
    from ..report import _ascii_table

    rows: list[list[str]] = []
    for pg in pages:
        if pg.page_state == PAGE_STATE["unreachable"]:
            # #120: страница не открылась — одна строка вместо списка селекторов,
            # чтобы не выдавать «не проверено» за «не найдено».
            rows.append([pg.name, "-", STATUS_UNREACHABLE, "-"])
            continue
        if pg.page_state == PAGE_STATE["unauthenticated"]:
            rows.append([pg.name, "-", STATUS_UNAUTHENTICATED, "-"])
            continue
        if pg.page_state == PAGE_STATE["placeholder"]:
            rows.append([pg.name, "-", STATUS_PLACEHOLDER, "-"])
            continue
        for r in pg.results:
            rows.append([pg.name, r.name, r.status, str(r.found)])
    return _ascii_table(["page", "selector", "status", "count"], rows)


def _healthcheck_spec(config) -> list[tuple[str, str, list[tuple[str, str, bool]]]]:
    """Список страниц и ключевых селекторов для проверки.

    Читает селекторы из ``selector_groups/`` (не дублирует их). Состав страниц —
    только те, чьи URL доступны БЕЗ контекста конкретной вакансии (Codex F1):
    search/negotiations рендерятся как списки, resume использует resume_id из
    конфига (id РЕЗЮМЕ — корректный для /resume/<id>). Страницы vacancy/
    apply_form требуют реальный id вакансии (+ vacancyId/employerId в query),
    которого у healthcheck нет — их здесь нет, иначе goto шёл бы на 404 и
    ронял все их селекторы в NOT_FOUND на здоровом аккаунте.

    ``required`` (3-й элемент каждого селектора) — обязателен ли он. Optional
    помечены селекторы, которые hh.ru легитимно НЕ рендерит по дизайну:
    compensation (устарел после magritte-перехода 2025, см. search_page.py),
    пагинация (отсутствует в конце/начале выдачи). Иначе здоровая страница
    репортилась бы [FAIL] (Codex F1).
    """
    from ..selector_groups import negotiations, resume_page, search_page

    spec: list[tuple[str, str, list[tuple[str, str, bool]]]] = [
        (
            "search",
            _search_healthcheck_url(config),
            [
                ("VACANCY_CARD", search_page.VACANCY_CARD, True),
                ("VACANCY_CARD_TITLE_LINK", search_page.VACANCY_CARD_TITLE_LINK, True),
                ("VACANCY_CARD_COMPANY", search_page.VACANCY_CARD_COMPANY, True),
                ("VACANCY_CARD_COMPENSATION", search_page.VACANCY_CARD_COMPENSATION, False),
                # Пагинация: hh.ru отдаёт две вёрстки (#123) — с pager-next и без
                # него (только номера). Обе optional: их легитимно нет, когда
                # выдача умещается на одну страницу.
                ("PAGINATION_NEXT", search_page.PAGINATION_NEXT, False),
                ("PAGINATION_PAGE", search_page.PAGINATION_PAGE, False),
            ],
        ),
        (
            "negotiations",
            "https://hh.ru/applicant/negotiations",
            [
                ("NEGOTIATION_ITEM", negotiations.NEGOTIATION_ITEM, True),
                ("NEGOTIATION_VACANCY_LINK", negotiations.NEGOTIATION_VACANCY_LINK, True),
                ("NEGOTIATIONS_PAGINATION_NEXT", negotiations.NEGOTIATIONS_PAGINATION_NEXT, False),
            ],
        ),
    ]

    # Страница резюме — только если в конфиге есть resume_url (URL для goto).
    # resume_id здесь — id РЕЗЮМЕ (хвост resume_url), это корректный сегмент
    # /resume/<id>, а НЕ id вакансии.
    resume_url = _first_resume_url(config)
    if resume_url:
        spec.append(
            (
                "resume",
                resume_url,
                [
                    ("RESUME_BUMP_BUTTON", resume_page.RESUME_BUMP_BUTTON, True),
                ],
            )
        )
    return spec


def _search_healthcheck_url(config) -> str:
    """URL страницы поиска для healthcheck (issue #120).

    Голый ``/search/vacancy`` без параметров на живом hh.ru не доходит до
    ``domcontentloaded`` (таймаут 60 сек) — healthcheck падал на первом же шаге
    и не проверял остальные страницы. Берём URL, который реально использует
    ``search`` — через ``build_search_url`` по фильтрам первого резюме, чтобы
    проверять ту же разметку, что видит боевой сбор.
    """
    from ..search import HH_BASE_URL, build_search_url

    resumes = getattr(config, "resumes", None)
    if resumes:
        return build_search_url(resumes[0].search, 0)
    # Конфиг без резюме — минимальный осмысленный запрос вместо голого URL.
    return f"{HH_BASE_URL}/search/vacancy?text=python"


def _first_resume_url(config) -> str | None:
    """URL страницы резюме для goto (если в конфиге есть resume_url)."""
    if not getattr(config, "resumes", None):
        return None
    resume = config.resumes[0]
    return getattr(resume, "resume_url", None)


def run_healthcheck(args: argparse.Namespace) -> bool:
    """Открывает браузер и печатает ASCII-таблицу статусов ключевых селекторов.

    Read-only: только ``goto`` + ``locator.count()``. ``set_default_navigation_timeout``
    (#80) поднимается, т.к. hh.ru может грузиться медленно. apply/submit не
    трогаются.
    """
    from ..browser import launch_context
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)
    spec = _healthcheck_spec(config)

    print("[INFO] healthcheck: read-only проверка селекторов hh.ru (без отклика)")
    with launch_context(config.storage_state_file, headless=args.headless) as context:
        page = context.new_page()
        # Навигационный потолок уже выставлен context-wide в launch_context
        # (set_default_navigation_timeout(GOTO_TIMEOUT_MS), см. browser.py) — тот же
        # источник, что у всех путей hh.ru. Раньше тут был хардкод 60000 (меньше
        # GOTO_TIMEOUT_MS=90_000) — #142: убран, чтобы healthcheck не падал быстрее
        # остального кода на медленном hh.ru.
        pages = check_selectors(page, spec)

    print(format_healthcheck_table(pages))
    # Итог — только по required-селекторам (Codex F1): optional-ABSENT легитимен
    # (пагинация/compensation) и НЕ делает здоровый аккаунт «сломанным».
    required_ok = sum(1 for pg in pages for r in pg.results if r.required and r.found > 0)
    required_missing = sum(1 for pg in pages for r in pg.results if r.fails)
    optional_absent = sum(
        1 for pg in pages for r in pg.results if r.status == STATUS_OPTIONAL_ABSENT
    )
    unreachable = sum(1 for pg in pages if pg.unreachable)
    unauthenticated = sum(1 for pg in pages if pg.unauthenticated)
    placeholders = sum(1 for pg in pages if pg.placeholder)
    if required_missing or unreachable or unauthenticated or placeholders:
        parts = []
        if required_missing:
            parts.append(f"НЕ найдено {required_missing} обязательных (см. NOT_FOUND выше)")
        if unreachable:
            # #120: недоступная страница — тоже провал: её селекторы не проверены,
            # и молча рапортовать [OK] по остальным было бы враньём.
            parts.append(f"недоступно страниц: {unreachable} (см. UNREACHABLE выше)")
        if unauthenticated:
            parts.append(
                f"сессия недействительна на страниц: {unauthenticated} "
                "(выполните login; селекторы не проверялись)"
            )
        if placeholders:
            parts.append(
                f"плейсхолдеров resume_url: {placeholders} "
                "(заполните resume_url; получить реальный можно через list-resumes)"
            )
        print(
            f"[FAIL] обязательных найдено {required_ok}; "
            + "; ".join(parts)
            + f"; опциональных отсутствует {optional_absent} (норма)"
        )
        return True
    else:
        print(
            f"[OK] все {required_ok} обязательных селекторов найдены "
            f"(опциональных отсутствует {optional_absent} — норма)"
        )
        return False


# --- probe-дамп формы (#8) -------------------------------------------------


def _vacancy_from_url(url: str):
    """Строит VacancyCard для probe из URL вакансии.

    vacancy_id извлекается канонически (через search._extract_vacancy_id: срез
    ?query, валидация isdigit) — не наивным split('/')[-1], иначе query-параметр
    попадает в vacancy_id и в имя файла дампа. Невалидный ID → ValueError.
    """
    from ..search import VacancyCard, _extract_vacancy_id

    vacancy_id = _extract_vacancy_id(url)
    if not vacancy_id:
        raise ValueError(
            f"Не удалось извлечь числовой ID вакансии из URL: {url} "
            "(ожидается https://hh.ru/vacancy/<id>)"
        )
    return VacancyCard(
        vacancy_id=vacancy_id,
        title=f"(probe target #{vacancy_id})",
        company="",
        url=url,
    )


def run(args: argparse.Namespace) -> bool | CommandExitCode | None:
    if getattr(args, "healthcheck", False):
        return run_healthcheck(args)
    if getattr(args, "negotiations", False):
        return run_negotiations(args)
    if getattr(args, "questionnaires_only", False):
        return run_questionnaires(args)

    from ..apply.probe import probe_vacancy
    from ..browser import launch_context
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)

    vacancy_url = _resolve_vacancy_url(args)
    try:
        vacancy = _vacancy_from_url(vacancy_url)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    resumes = resolve_resumes(config, [args.resume] if args.resume else None)
    if not resumes:
        print("Ошибка: не выбрано резюме для probe (укажите --resume <id>).", file=sys.stderr)
        sys.exit(1)
    resume = resumes[0]
    cover_letter_template = config.cover_letter_for(resume)
    # #17 (follow-up #54): AI-письмо в probe-дампе. Провайдер строится по тому же
    # правилу, что и в apply (ai + ai_profile); None → статичный шаблон. Атомарность
    # probe не страдает: провайдер только генерирует текст письма, submit не кликается.
    letter_provider = _build_letter_provider(config, resume, cover_letter_template)

    print(f"=== probe для резюме: {resume.id} ===")
    print(f"Целевая вакансия: {vacancy.url}")

    with launch_context(config.storage_state_file, headless=args.headless) as context:
        page = context.new_page()
        result = probe_vacancy(
            page,
            vacancy,
            # resume.resume_id (хвост resume_url), а НЕ resume.id (слаг конфига):
            # форма отклика адресует опцию как
            # [data-qa='magritte-select-option-{resume_id}'], и слаг там не
            # существует — _select_resume_in_form отказывал «резюме не найдено
            # среди опций». run_apply_for_resume везде использует resume_id;
            # probe расходился с ним и потому не воспроизводил боевой путь.
            resume_id=resume.resume_id,
            cover_letter_template=cover_letter_template,
            letter_provider=letter_provider,
        )

    if result.skipped:
        # #95: форма требует анкеты — это не ошибка и не успех.
        print(f"[INFO] {result.reason}")
    elif result.success:
        png = result.dump_paths.get("screenshot")
        html = result.dump_paths.get("html")
        print("[OK] Дамп формы отклика сохранён (отправка НЕ выполнена):")
        if png:
            print(f"  screenshot: {png}")
        if html:
            print(f"  html:       {html}")
    else:
        print(f"[FAIL] {result.reason}")


def _dedupe_vacancies(vacancies):
    """Keep raw search order while removing duplicate vacancy IDs."""
    seen: set[str] = set()
    unique = []
    for vacancy in vacancies:
        if vacancy.vacancy_id in seen:
            continue
        seen.add(vacancy.vacancy_id)
        unique.append(vacancy)
    return unique


def _format_questionnaire_report(results) -> str:
    from ..apply.questionnaire import QUESTIONNAIRE, group_questions
    from ..report import _ascii_table

    rows = [
        [
            result.vacancy.vacancy_id,
            result.vacancy.title.replace("\n", " "),
            result.vacancy.company.replace("\n", " ") or "-",
            result.status,
            str(len(result.questions) if result.status == QUESTIONNAIRE else 0),
        ]
        for result in results
    ]
    report = _ascii_table(["vacancy", "title", "company", "status", "questions"], rows)
    for result in results:
        if result.status != QUESTIONNAIRE:
            continue
        report += f"\n\n[{result.vacancy.vacancy_id}] {result.vacancy.title}"
        for index, question in enumerate(result.questions, start=1):
            report += f"\n  {index}. {question.text}"
            if question.options:
                report += "\n     options: " + " | ".join(question.options)
    # #443 Этап 2: те же вопросы объединены по нормализованному тексту/типу —
    # это то, что реально повторяется у разных вакансий (шаблон работодателя),
    # без потери связи с исходными vacancy_id.
    groups = group_questions(list(results))
    if groups:
        report += "\n\n=== Уникальные вопросы (объединены по тексту) ==="
        group_rows = [
            [group.kind, group.text.replace("\n", " "), str(len(group.vacancy_ids))]
            for group in groups
        ]
        report += "\n" + _ascii_table(["kind", "question", "vacancies"], group_rows)
        for group in groups:
            if len(group.vacancy_ids) < 2:
                continue
            report += f"\n\n[{', '.join(group.vacancy_ids)}] {group.text}"
            if group.options:
                report += "\n  options: " + " | ".join(group.options)
    return report


def _print_questionnaire_progress(result, checked: int, total: int) -> None:
    """Print one durable progress line and the result as soon as it is known."""
    from ..apply.questionnaire import QUESTIONNAIRE

    title = result.vacancy.title.replace("\n", " ")
    print(f"[INFO] проверено {checked}/{total}: {result.status} — {title}", flush=True)
    if result.status != QUESTIONNAIRE:
        return
    print(
        f"[OK] анкета: {result.vacancy.title}"
        f"\n  вакансия: {result.vacancy.url}"
        f"\n  вопросов: {len(result.questions)}",
        flush=True,
    )
    for index, question in enumerate(result.questions, start=1):
        print(f"  {index}. {question.text}", flush=True)
        if question.options:
            print("     options: " + " | ".join(question.options), flush=True)


def _questionnaire_counts(results) -> dict[str, int]:
    from ..apply.questionnaire import (
        ALREADY_RESPONDED,
        NO_QUESTIONNAIRE,
        QUESTIONNAIRE,
        UNAUTHENTICATED,
        UNKNOWN,
    )

    return {
        "questionnaire": sum(r.status == QUESTIONNAIRE for r in results),
        "no_questionnaire": sum(r.status == NO_QUESTIONNAIRE for r in results),
        "already_responded": sum(r.status == ALREADY_RESPONDED for r in results),
        "unknown": sum(r.status == UNKNOWN for r in results),
        "unauthenticated": sum(r.status == UNAUTHENTICATED for r in results),
    }


def _limit_reached(results, limit: int) -> bool:
    """True when --limit-questionnaires is set and already satisfied."""
    return bool(limit) and _questionnaire_counts(results)["questionnaire"] >= limit


def _record_questionnaire_if_confirmed(history, resume_id: str, vacancy, result) -> bool:
    """Persist a confirmed questionnaire snapshot; called after every scan pass.

    Returns False only when persistence was attempted and failed — callers use
    this to surface a run-level [FAIL], not to abort the scan (see below).
    True covers both "nothing to persist" (no history configured, or the
    result isn't a confirmed questionnaire) and "persisted successfully".

    cycle-review PR #456 (Codex): вызывается и после быстрого прохода, и после
    retry (731-743 в исходной нумерации) — иначе анкета, подтверждённая только
    на retry после transient UNKNOWN, остаётся в печатном отчёте, но не попадает
    в SQLite, и исследовательская база расходится с выводом команды.

    cycle-review PR #456 round 1 (Claude /review): запись обёрнута в except
    sqlite3.Error, в отличие от голого вызова раньше — падение записи (locked
    DB, диск полон) не должно обрывать весь bulk-скан для всех оставшихся
    вакансий/резюме (команда fail-tolerant: --start-page, retry, обработка
    KeyboardInterrupt рядом же). Ловим именно sqlite3.Error, а не bare
    Exception — узкий except тут тот же принцип проекта, что и в
    run_healthcheck (см. её докстринг).

    cycle-review PR #456 round 2 (Codex): проглатывание ошибки одним только
    logger.warning превращало потерю подтверждённой анкеты в молчаливый
    success — run_questionnaires мог напечатать обычный отчёт и вернуть False,
    хотя исследовательские данные реально потеряны без единого
    машинно-обнаружимого сигнала. Возврат False здесь и накопление в
    history_write_failed (в run_questionnaires) переводят это в [FAIL] в конце
    прогона, не отменяя уже принятого решения «не обрывать скан».
    """
    from ..apply.questionnaire import QUESTIONNAIRE

    if history is None or result.status != QUESTIONNAIRE:
        return True
    try:
        history.record_questionnaire(
            resume_id,
            vacancy.vacancy_id,
            vacancy.url,
            vacancy.title,
            vacancy.company,
            [
                {
                    "body_index": question.body_index,
                    "text": question.text,
                    "kind": question.kind,
                    "is_radio": question.is_radio,
                    "options": list(question.options),
                }
                for question in result.questions
            ],
        )
        return True
    except sqlite3.Error:
        logger.warning(
            "questionnaires-only: не удалось сохранить анкету %s в историю",
            vacancy.vacancy_id,
            exc_info=True,
        )
        return False


def run_questionnaires(args: argparse.Namespace) -> bool | CommandExitCode:
    """Scan raw search cards in one context/page; confirmed questionnaires are
    persisted to SQLite via ``args.history`` (see _record_questionnaire_if_confirmed).

    cycle-review PR #456 round 2 (Claude /review): ``args.history`` is resolved
    to a real default path (``data/history.db`` or the account's history) by
    ``cli._resolve_paths`` before any command runs — through the real CLI this
    is effectively unconditional, not gated behind an explicit ``--history``
    flag. Only a direct unit-test call that bypasses the CLI layer (constructing
    ``args`` without a ``history`` attribute) leaves it ``None``.
    """
    from ..apply.questionnaire import (
        FAST_FORM_TIMEOUT_MS,
        FAST_TIMEOUT_MS,
        UNKNOWN,
        QuestionnaireScanResult,
        scan_questionnaire,
    )
    from ..browser import GOTO_TIMEOUT_MS, launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..search import VacancySearchIndeterminate, search_vacancies

    if args.vacancy_id or args.vacancy_url:
        print(
            "Ошибка: --questionnaires-only работает по поиску; уберите --vacancy-id/--vacancy-url.",
            file=sys.stderr,
        )
        return True

    config = load_config_or_exit(args.config)
    history = History(args.history) if getattr(args, "history", None) else None
    resumes = resolve_resumes(config, [args.resume] if args.resume else None)
    if not resumes:
        print("Ошибка: не выбрано резюме для bulk probe.", file=sys.stderr)
        return True
    limit = getattr(args, "limit_questionnaires", 0)
    if limit < 0:
        print("Ошибка: --limit-questionnaires не может быть отрицательным.", file=sys.stderr)
        return True

    # #486: старые запуски этой команды ключевали questionnaire_scans слагом
    # резюме (resume.id) вместо реального resume_id — накопленные строки были
    # недостижимы через --resume в questionnaire learn/pending. Нормализация
    # идемпотентна; маппинг строится из ВСЕХ резюме конфига (не только
    # запрошенного --resume), иначе сканы прочих резюме остаются осиротевшими
    # под своим slug (тот же приём в questionnaire._rekey_scanned_slugs,
    # которая и есть основная точка вызова для уже накопленных до этого фикса
    # данных — эта команда мутирует БД только при собственном запуске).
    if history is not None:
        rekeyed = history.rekey_questionnaire_scans(
            {resume.id: resume.resume_id for resume in config.resumes}
        )
        if rekeyed:
            print(
                f"[INFO] questionnaires-only: перепривязано {rekeyed} старых "
                "записей questionnaire_scans со slug на resume_id (#486)"
            )

    all_results: list[QuestionnaireScanResult] = []
    interrupted = False
    # cycle-review PR #456 round 2 (Codex): накапливает потери записи в
    # историю, не прерывая скан — см. _record_questionnaire_if_confirmed.
    history_write_failed = False
    search_failed = False
    print(
        "[INFO] questionnaires-only: read-only на hh.ru, локальная WRITE-команда "
        "(вопросы сохраняются в SQLite; без откликов, AI и артефактов)"
    )
    try:
        with launch_context(config.storage_state_file, headless=args.headless) as context:
            page = context.new_page()
            for resume in resumes:
                try:
                    vacancies = _dedupe_vacancies(
                        search_vacancies(
                            page,
                            resume.search,
                            max_pages=args.max_pages,
                            start_page=getattr(args, "start_page", 0),
                        )
                    )
                except VacancySearchIndeterminate as exc:
                    search_failed = True
                    vacancies = _dedupe_vacancies(exc.partial_results)
                    print(
                        f"[FAIL] выдача поиска не подтверждена для {resume.id}: {exc}; "
                        f"state={exc.state} page={exc.page_num} url={exc.url} "
                        f"partial_results={len(vacancies)} diagnostics={exc.diagnostics}"
                    )
                print(f"[INFO] {resume.id}: найдено уникальных вакансий: {len(vacancies)}")
                retry_ids = []
                resume_results: list[QuestionnaireScanResult] = []
                result_positions: dict[str, int] = {}
                for vacancy in vacancies:
                    result = scan_questionnaire(
                        page,
                        vacancy,
                        timeout_ms=FAST_TIMEOUT_MS,
                        form_timeout_ms=FAST_FORM_TIMEOUT_MS,
                    )
                    resume_results.append(result)
                    all_results.append(result)
                    # #486: resume.resume_id (хвост resume_url), а НЕ resume.id
                    # (слаг конфига) — questionnaire._scope() и apply-путь
                    # ключуют историю анкет реальным resume_id, слаг делал
                    # накопленные сканы недостижимыми через --resume.
                    if not _record_questionnaire_if_confirmed(
                        history, resume.resume_id, vacancy, result
                    ):
                        history_write_failed = True
                    result_positions[vacancy.vacancy_id] = len(all_results) - 1
                    _print_questionnaire_progress(result, len(resume_results), len(vacancies))
                    if result.status == UNKNOWN and result.retryable:
                        retry_ids.append(vacancy.vacancy_id)
                    if _limit_reached(all_results, limit):
                        break
                    time.sleep(
                        random.uniform(
                            config.throttle.min_delay_seconds,
                            config.throttle.max_delay_seconds,
                        )
                    )

                # cycle-review PR #450: лимит — условие «не начинать новые
                # вакансии», а не повод бросить уже накопленную неопределённость.
                # retry_ids сливается до выхода, иначе транзиентный unknown
                # навсегда репортится как unknown (#448: unknown не выдавать за
                # отсутствие анкеты).
                if retry_ids and _limit_reached(all_results, limit):
                    # cycle-review PR #450 round 2: выход по лимиту происходит до
                    # паузы основного цикла, поэтому здесь она обязательна — иначе
                    # клик последней вакансии и первый retry идут подряд без
                    # задержки (CLAUDE.md: троттлинг не ослабляем). Пауза только
                    # когда retry реально предстоит: в конце чистого прогона
                    # ждать не от чего.
                    time.sleep(
                        random.uniform(
                            config.throttle.min_delay_seconds,
                            config.throttle.max_delay_seconds,
                        )
                    )
                for vacancy_id in retry_ids:
                    vacancy = next(v for v in vacancies if v.vacancy_id == vacancy_id)
                    print(f"[INFO] retry вакансии {vacancy_id}: долгий повтор", flush=True)
                    result = scan_questionnaire(
                        page, vacancy, timeout_ms=GOTO_TIMEOUT_MS, form_timeout_ms=10_000
                    )
                    result_index = next(
                        index
                        for index, item in enumerate(resume_results)
                        if item.vacancy.vacancy_id == vacancy_id
                    )
                    resume_results[result_index] = result
                    all_results[result_positions[vacancy_id]] = result
                    if not _record_questionnaire_if_confirmed(
                        history, resume.resume_id, vacancy, result
                    ):
                        history_write_failed = True
                    # Позиция самой перепроверяемой вакансии, а не длина списка:
                    # retry вакансии 3 из 10 иначе печатал бы «проверено 10/10».
                    _print_questionnaire_progress(result, result_index + 1, len(vacancies))
                    # cycle-review PR #450 round 3: сама перепроверка тоже
                    # подтверждает анкеты, поэтому лимит проверяется и здесь —
                    # иначе --limit-questionnaires 1 прокликает весь накопленный
                    # retry и вернёт N анкет вместо одной. Пауза ниже — только
                    # перед следующим реальным действием, а не перед выходом.
                    if _limit_reached(all_results, limit):
                        break
                    time.sleep(
                        random.uniform(
                            config.throttle.min_delay_seconds,
                            config.throttle.max_delay_seconds,
                        )
                    )
                if _limit_reached(all_results, limit):
                    break
    except KeyboardInterrupt:
        interrupted = True
        print("\n[INFO] скан прерван пользователем; печатаю итог частичного прохода")

    print(_format_questionnaire_report(all_results))
    counts = _questionnaire_counts(all_results)
    unauthenticated = counts["unauthenticated"]
    unknown = counts["unknown"]
    print(
        f"[INFO] итог: вакансий {len(all_results)}, "
        f"анкет {counts['questionnaire']}, "
        f"без анкеты {counts['no_questionnaire']}, "
        f"уже откликались {counts['already_responded']}, "
        f"unknown {unknown}, "
        f"требует авторизации {unauthenticated}"
    )
    if history_write_failed:
        # cycle-review PR #456 round 2 (Codex): запись в SQLite могла упасть
        # (locked DB, диск полон) без обрыва скана (round 1: fail-tolerant,
        # см. _record_questionnaire_if_confirmed) — но это не должно выглядеть
        # как молчаливый success. Печатаем безусловно, до unauthenticated/
        # interrupted веток, чтобы строка была видна при любом сочетании.
        print("[FAIL] не удалось сохранить одну или несколько анкет в историю")
    if unauthenticated:
        # #433 cycle-review: потеря сессии посреди прогона не должна выглядеть
        # как успешный полный скан — часть вакансий не проверена. Проверка
        # стоит ВЫШЕ interrupted, чтобы строка [FAIL] о потере сессии
        # печаталась и при Ctrl-C. #452: сам exit-код это не меняет —
        # прерывание пользователем всегда старше по приоритету и возвращает
        # SIGINT, даже если до него уже обнаружена потеря сессии.
        print("[FAIL] сессия истекла во время прогона — скан неполный")
        if interrupted and unknown:
            print("[FAIL] скан прерван с неподтверждёнными вакансиями — результат неполный")
    # cycle-review PR #460 (round 2, Claude /review; round 3 fix-up): все
    # применимые [FAIL] строки выше печатаются безусловно; приоритет
    # exit-кода — единая лестница здесь, а не ранние return внутри печати.
    # #453/a460ab6 инвариант: прерывание пользователем (Ctrl-C) всегда
    # старше по приоритету и возвращает SIGINT, даже если до него уже
    # обнаружена потеря сессии ИЛИ сбой записи в историю — round 1 этой PR
    # ошибочно ввёл PERSISTENCE_FAILED, побеждающий interrupted; round 3
    # (Claude /review, эмпирически подтверждено git-историей: на main этой
    # ветки такого приоритета не было) восстанавливает исходный инвариант.
    if unauthenticated:
        if interrupted:
            return CommandExitCode.SIGINT
        return True
    if interrupted and unknown:
        # cycle-review PR #450 (Codex): неразрешённый unknown в частичном
        # прогоне делает результат неотличимым от полного скана, если выйти
        # с успехом — печатаем [FAIL]. #452: exit-код всё равно SIGINT —
        # Ctrl-C имеет приоритет над fail-closed причиной остановки.
        print("[FAIL] скан прерван с неподтверждёнными вакансиями — результат неполный")
        return CommandExitCode.SIGINT
    if interrupted:
        # Частичный отчёт уже напечатан, но Ctrl-C всегда сохраняет стандартный
        # POSIX-код SIGINT, независимо от подтверждённости результатов.
        return CommandExitCode.SIGINT
    if all_results and unknown == len(all_results):
        # #433 cycle-review round 3 fix-up: единичный unknown по конкретной
        # вакансии (timeout, drift, частично распознанная анкета) — часть
        # нормального разброса на реальной выдаче, не провал прогона (виден
        # в таблице/счётчике выше). Провал уровня прогона — это когда ВСЕ
        # вакансии unknown (массовый timeout/drift, скан не состоялся), как и
        # run_healthcheck не валит здоровый аккаунт по одной NOT_FOUND
        # строке, а только когда обязательные селекторы не найдены системно.
        print("[FAIL] все вакансии вернули неподтверждённый результат — скан не состоялся")
        return True
    if search_failed:
        return True
    # history_write_failed уже напечатан выше ([FAIL] строка); здесь только
    # решение о статусе — иначе потеря записи истории осталась бы success.
    return history_write_failed


def _resolve_vacancy_url(args: argparse.Namespace) -> str:
    if args.vacancy_url:
        return args.vacancy_url
    if args.vacancy_id:
        return f"https://hh.ru/vacancy/{args.vacancy_id}"
    print(
        "Ошибка: укажите целевую вакансию через --vacancy-id <id> или --vacancy-url <url>.",
        file=sys.stderr,
    )
    sys.exit(1)


def run_negotiations(args: argparse.Namespace) -> bool:
    """Dump negotiations/chat DOM using only GET navigation and reads."""
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..negotiations_probe import chat_url, paginated_topic_refs
    from ..report import _ascii_table
    from ..responses import NotAuthenticated, ResponsesIndeterminate
    from ..selector_groups import negotiations

    config = load_config_or_exit(args.config)
    list_url = "https://hh.ru/applicant/negotiations"
    print("[INFO] negotiations: read-only probe (goto + чтение, без кликов)")
    with launch_context(config.storage_state_file, headless=args.headless) as context:
        page = context.new_page()
        goto_hh(page, list_url)
        items = page.locator(negotiations.NEGOTIATION_ITEM)
        # /review (#201): paginated_topic_refs() re-navigates internally (and,
        # with max_pages>1, may leave `page` on the LAST visited page, not
        # this first one) — waiting for cards here before that re-navigation
        # was pointless dead work for the RAW HTML dump below, which reads
        # `items` only after pagination finishes. Wait AFTER pagination so the
        # 10s bounded wait covers the page actually rendered at dump time.
        try:
            refs = paginated_topic_refs(page, max_pages=getattr(args, "max_pages", 5))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"[FAIL] не удалось прочитать SSR state: {exc}")
            return True
        except (NotAuthenticated, ResponsesIndeterminate) as exc:
            print(f"[FAIL] не удалось прочитать SSR chat mapping: {exc}")
            return True
        try:
            items.first.wait_for(state="attached", timeout=10_000)
        except PlaywrightError:
            logger.warning("negotiations: cards did not attach within 10 seconds")

        rows = []
        for name, selector in (
            ("ITEM", negotiations.NEGOTIATION_ITEM),
            ("VACANCY", negotiations.NEGOTIATION_VACANCY_LINK),
            ("EMPLOYER", negotiations.NEGOTIATION_EMPLOYER),
            ("STATUS", "[data-qa^='negotiations-tag']"),
            ("DATE", negotiations.NEGOTIATION_DATE),
            ("OPEN_CHAT", negotiations.NEGOTIATION_CHAT_LINK),
        ):
            rows.append([name, selector, str(page.locator(selector).count())])
        print(_ascii_table(["selector", "css", "count"], rows))
        print(
            _ascii_table(
                ["topic_id", "chat_id", "direct_route"],
                [[r.topic_id, r.chat_id, chat_url(r.chat_id)] for r in refs],
            )
        )
        print("RAW HTML fragment (first card):")
        if items.count():
            print(items.first.evaluate("el => el.outerHTML")[:4000])

        if args.topic:
            ref = next((r for r in refs if r.topic_id == str(args.topic)), None)
            if ref is None:
                print(f"[FAIL] topic не найден в SSR state: {args.topic}")
                return True
            goto_hh(page, chat_url(ref.chat_id))
            print(f"[INFO] chat route: {page.url}")
            message_selector = (
                '[data-qa^="chatik-chat-message-"][data-qa$="-text"]'
                ':not([data-qa="chatik-chat-message-applicant-action-text"])'
            )
            messages = page.locator(message_selector)
            message_rows = []
            for i in range(messages.count()):
                loc = messages.nth(i)
                parent_class = loc.evaluate(
                    "(el, marker) => { for (let n = el; n; n = n.parentElement) "
                    "if (String(n.className).split(/\\s+/).includes(marker)) "
                    "return n.className; return ''; }",
                    negotiations.CHAT_MESSAGE_MY_MARKER,
                )
                message_rows.append(
                    [
                        str(i + 1),
                        loc.get_attribute("data-qa") or "-",
                        "own"
                        if negotiations.CHAT_MESSAGE_MY_MARKER in parent_class.split()
                        else "other",
                        loc.inner_text().replace("\n", " ")[:160],
                    ]
                )
            print(_ascii_table(["message", "id", "author_marker", "text"], message_rows))
            print("RAW HTML fragment (messages):")
            message_roots = page.locator("[data-qa^='chatik-chat-message-']")
            if message_roots.count():
                print(message_roots.first.evaluate("el => el.outerHTML")[:4000])
    return False

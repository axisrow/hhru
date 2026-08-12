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
import logging
import re
import sys
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..browser import goto_hh
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
        "--healthcheck",
        action="store_true",
        help="Read-only проверка ключевых селекторов hh.ru (OK/NOT_FOUND) без отклика (#88)",
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
STATUS_UNREACHABLE = "UNREACHABLE"


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
        try:
            goto_hh(page, url)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            logger.warning("healthcheck: страница '%s' недоступна (%s): %s", name, url, exc)
            pages.append(PageCheck(name=name, url=url, results=[], unreachable=True))
            continue
        if page_loader is not None:
            page_loader(page, url, name)
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
        if pg.unreachable:
            # #120: страница не открылась — одна строка вместо списка селекторов,
            # чтобы не выдавать «не проверено» за «не найдено».
            rows.append([pg.name, "-", STATUS_UNREACHABLE, "-"])
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


def run_healthcheck(args: argparse.Namespace) -> None:
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
    if required_missing or unreachable:
        parts = []
        if required_missing:
            parts.append(f"НЕ найдено {required_missing} обязательных (см. NOT_FOUND выше)")
        if unreachable:
            # #120: недоступная страница — тоже провал: её селекторы не проверены,
            # и молча рапортовать [OK] по остальным было бы враньём.
            parts.append(f"недоступно страниц: {unreachable} (см. UNREACHABLE выше)")
        print(
            f"[FAIL] обязательных найдено {required_ok}; "
            + "; ".join(parts)
            + f"; опциональных отсутствует {optional_absent} (норма)"
        )
    else:
        print(
            f"[OK] все {required_ok} обязательных селекторов найдены "
            f"(опциональных отсутствует {optional_absent} — норма)"
        )


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


def run(args: argparse.Namespace) -> None:
    if getattr(args, "healthcheck", False):
        run_healthcheck(args)
        return

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
            resume_id=resume.id,
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

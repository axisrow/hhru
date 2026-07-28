"""Команда probe (#8, #88): диагностика селекторов hh.ru без отклика.

Top-level команда `hhru_bot probe ...` — регистрируется автоматически через
pkgutil.iter_modules в cli.register_commands (cli.py не трогается).

Два read-only режима:

* По умолчанию (#8): доходит до формы отклика целевой вакансии, заполняет
  сопроводительное письмо и сдампит screenshot + HTML в logs/, после чего
  останавливается. submit не вызывается — ничего не отправляется. По дампу
  сверяются непроверенные селекторы формы отклика (см. #10).
* ``--healthcheck`` (#88): открывает ключевые страницы hh.ru (search/vacancy/
  negotiations/resume) и считает ``locator.count()`` для селекторов из
  ``selector_groups/``. Read-only: только ``goto`` + ``count``, никаких кликов
  apply/отправки. Вывод — ASCII-таблица ``selector | status | count`` со
  статусами OK (>0) / NOT_FOUND (0). Помогает при регрессии (CLAUDE.md:
  «первый подозреваемый — устаревший селектор») до реального падения команды.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field

from ._common import _build_letter_provider, add_common_args, resolve_resumes

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

# Статус одного селектора. OK = найден (>0), NOT_FOUND = 0 совпадений.
STATUS_OK = "OK"
STATUS_NOT_FOUND = "NOT_FOUND"


@dataclass
class SelectorCheck:
    """Результат проверки одного селектора на странице.

    ``name`` — человекочитаемая метка (имя константы из selector_groups),
    ``selector`` — исходный CSS, ``found`` — ``locator.count()``.
    """

    name: str
    selector: str
    found: int

    @property
    def status(self) -> str:
        return STATUS_OK if self.found > 0 else STATUS_NOT_FOUND


@dataclass
class PageCheck:
    """Результат проверки одной страницы: URL + список статусов селекторов."""

    name: str
    url: str
    results: list[SelectorCheck] = field(default_factory=list)


def check_selectors(page, spec, page_loader=None):
    """Read-only прогон селекторов по страницам.

    ``spec`` — список ``(page_name, url, [(name, selector), ...])``. Для каждой
    страницы: открыть (``page.goto``) и для каждого селектора взять
    ``page.locator(selector).count()`` → SelectorCheck. Ничего не кликает и не
    отправляет — это главный инвариант #88 (read-only).

    ``page_loader(page, url, name)`` — опциональный хук, вызываемый ПОСЛЕ goto:
    в боевом прогоне не нужен (DOM рендерит живой hh.ru), а в тестах через него
    подменяют HTML фикстурой (имитация «браузер загрузил страницу»).
    """
    pages: list[PageCheck] = []
    for name, url, selectors in spec:
        page.goto(url, wait_until="domcontentloaded")
        if page_loader is not None:
            page_loader(page, url, name)
        results = [
            SelectorCheck(name=sel_name, selector=sel, found=page.locator(sel).count())
            for sel_name, sel in selectors
        ]
        pages.append(PageCheck(name=name, url=url, results=results))
    return pages


def format_healthcheck_table(pages: list[PageCheck]) -> str:
    """ASCII-таблица статусов: ``страница | selector | status | count``.

    Переиспользует ``report._ascii_table`` (общая ASCII-рамка проекта) — НЕ
    дублирует отрисовку. Пустой список всё равно рисует шапку (контракт таблицы).
    """
    from ..report import _ascii_table

    rows: list[list[str]] = []
    for pg in pages:
        for r in pg.results:
            rows.append([pg.name, r.name, r.status, str(r.found)])
    return _ascii_table(["page", "selector", "status", "count"], rows)


def _healthcheck_spec(config) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """Список страниц и ключевых селекторов для проверки.

    Читает селекторы из ``selector_groups/`` (не дублирует их). URL вакансии и
    резюме берёт из конфига (resume_url): это единственная привязка healthcheck
    к аккаунту. Страницы без нужного URL (напр. resume, если резюме не задано)
    молча пропускаются — healthcheck остаётся read-only-обзором селекторов.
    """
    from ..selector_groups import apply_form, negotiations, resume_page, search_page, vacancy_page

    spec: list[tuple[str, str, list[tuple[str, str]]]] = [
        (
            "search",
            "https://hh.ru/search/vacancy",
            [
                ("VACANCY_CARD", search_page.VACANCY_CARD),
                ("VACANCY_CARD_TITLE_LINK", search_page.VACANCY_CARD_TITLE_LINK),
                ("VACANCY_CARD_COMPANY", search_page.VACANCY_CARD_COMPANY),
                ("VACANCY_CARD_COMPENSATION", search_page.VACANCY_CARD_COMPENSATION),
                ("PAGINATION_NEXT", search_page.PAGINATION_NEXT),
            ],
        ),
        (
            "vacancy",
            _first_vacancy_url(config),
            [
                ("VACANCY_APPLY_BUTTON", vacancy_page.VACANCY_APPLY_BUTTON),
                ("VACANCY_TITLE", vacancy_page.VACANCY_TITLE),
                ("VACANCY_COMPANY_NAME", vacancy_page.VACANCY_COMPANY_NAME),
            ],
        ),
        (
            "apply_form",
            "https://hh.ru/applicant/vacancy_response",
            [
                ("APPLY_RESUME_SELECT", apply_form.APPLY_RESUME_SELECT),
                ("APPLY_COVER_LETTER_TEXTAREA", apply_form.APPLY_COVER_LETTER_TEXTAREA),
            ],
        ),
        (
            "negotiations",
            "https://hh.ru/applicant/negotiations",
            [
                ("NEGOTIATION_ITEM", negotiations.NEGOTIATION_ITEM),
                ("NEGOTIATION_VACANCY_LINK", negotiations.NEGOTIATION_VACANCY_LINK),
                ("NEGOTIATIONS_PAGINATION_NEXT", negotiations.NEGOTIATIONS_PAGINATION_NEXT),
            ],
        ),
    ]

    # Страница резюме — только если в конфиге есть resume_url (URL для goto).
    resume_url = _first_resume_url(config)
    if resume_url:
        spec.append(
            (
                "resume",
                resume_url,
                [
                    ("RESUME_BUMP_BUTTON", resume_page.RESUME_BUMP_BUTTON),
                ],
            )
        )
    return spec


def _first_vacancy_url(config) -> str:
    """URL вакансии для healthcheck — из resume_url первого резюме конфига.

    /vacancy/<id> рендерится и анониму, но конкретный id нужен для перехода.
    Берём id из resume_url хвоста (как делает config): это валидный id страницы
    вакансии, не отклик на неё — healthcheck не кликает apply.
    """
    if not getattr(config, "resumes", None):
        return "https://hh.ru/vacancy/0"
    resume = config.resumes[0]
    rid = getattr(resume, "resume_id", None) or "0"
    return f"https://hh.ru/vacancy/{rid}"


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
        # #80: страницы hh.ru могут грузиться медленно — поднимаем навигационный
        # timeout, чтобы healthcheck не падал по таймауту на медленном соединении.
        page.set_default_navigation_timeout(60000)
        pages = check_selectors(page, spec)

    print(format_healthcheck_table(pages))
    missing = sum(1 for pg in pages for r in pg.results if r.status == STATUS_NOT_FOUND)
    found = sum(1 for pg in pages for r in pg.results if r.status == STATUS_OK)
    if missing:
        print(f"[FAIL] найдено {found}, НЕ найдено {missing} (см. NOT_FOUND выше)")
    else:
        print(f"[OK] все {found} ключевых селекторов найдены")


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

    if result.success:
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

"""Команда search: поиск вакансий по фильтрам резюме (без откликов)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import replace

from ..config import ResumeConfig, SearchFilters
from ..history import History
from ..search import SalaryInfo, VacancyCard
from ._common import add_common_args, resumes_from_args

logger = logging.getLogger("hhru_bot.cli")


def _positive_area_id(value: str) -> int:
    """Валидатор --area: id территории в каталоге hh.ru — положительное целое.

    m4 (ревью): голый type=int пропускал 0/-1 в search URL hh.ru (пустая/неясная
    выдача); прецедент — _positive_page_count/_nonnegative_limit в _common.py (#441).
    """
    area_id = int(value)
    if area_id < 1:
        raise argparse.ArgumentTypeError("--area должен быть положительным id каталога")
    return area_id


def register(subparsers) -> None:
    p = subparsers.add_parser("search", help="Найти вакансии по фильтрам резюме (без откликов)")
    add_common_args(p)
    p.add_argument(
        "--text",
        help="Разовый текст поиска; можно использовать без --resume",
    )
    p.add_argument(
        "--save",
        action="store_true",
        help=(
            "Сохранить параметрику этого поиска как автопоиск hh.ru (#1052). "
            "WRITE: по умолчанию dry-run (план без клика), боевое — --force"
        ),
    )
    p.add_argument("--name", help="Явное имя автопоиска для --save")
    p.add_argument(
        "--force",
        action="store_true",
        help="Разрешить боевой клик «Сохранить поиск» (только с --save)",
    )
    p.add_argument(
        "--saved",
        metavar="NAME",
        help="Взять параметры прогона из сохранённого автопоиска hh.ru по имени",
    )
    p.add_argument(
        "--list-saved",
        action="store_true",
        help="Показать список автопоисков аккаунта (read-only)",
    )
    p.add_argument(
        "--area",
        type=_positive_area_id,
        help="Id города/региона из каталога hh.ru (напр. 1641 = Набережные Челны, "
        "88 = Казань); разовый оверлей поверх фильтров резюме",
    )
    p.set_defaults(func=run)


def _print_saved_searches(entries: list) -> None:
    from ..report import _ascii_table

    if not entries:
        print("[INFO] Автопоисков на аккаунте нет (подтверждено пустым состоянием списка)")
        return
    rows = [[e.name, e.url or ""] for e in entries]
    print(_ascii_table(["Имя", "URL выдачи"], rows))


def _run_saved_search_modes(args: argparse.Namespace, config) -> bool | None:
    """Завершающие режимы команды search: --list-saved и --save.

    Возвращает None, если ни один из них не задан (обычный поиск или --saved,
    параметры которого подставляются в прогон отдельно), иначе bool-признак
    отказа.
    """
    from ..browser import launch_context
    from ..responses import NotAuthenticated
    from ..saved_search import (
        SavedSearchListIndeterminate,
        find_duplicate_saved_search,
        list_saved_searches,
        save_search_on_hh,
        saved_search_query_key,
    )

    list_saved = getattr(args, "list_saved", False)
    saved = getattr(args, "saved", None)
    save = getattr(args, "save", False)
    force = getattr(args, "force", False)
    name = getattr(args, "name", None)

    if not (list_saved or save):
        # Чистый --save-режим не задан; --saved сам по себе — не завершающий
        # режим (параметрика подставляется в обычный прогон в run()).
        return None

    if list_saved and (saved or save):
        print("[FAIL] --list-saved не сочетается с --saved/--save")
        return True
    if save and saved:
        print("[FAIL] --save не сочетается с --saved (сохранение и прогон по автопоиску)")
        return True
    if force and not save:
        print("[FAIL] --force имеет смысл только вместе с --save")
        return True

    resumes = _resumes_for_search(config, args)
    if save and len(resumes) != 1:
        print(
            f"[FAIL] --save требует ровно одну параметрику поиска (сейчас {len(resumes)}); "
            "сузьте выбор через --resume или --text"
        )
        return True

    failed = False
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        entries = []
        if list_saved:
            try:
                entries = list_saved_searches(page)
            except SavedSearchListIndeterminate as exc:
                print(f"[FAIL] {exc}")
                return True
            except NotAuthenticated as exc:
                print(f"[FAIL] Сессия недействительна: {exc}")
                return True
        if list_saved:
            _print_saved_searches(entries)
            print("[OK] Список автопоисков прочитан; read-only, изменений на hh.ru нет")
            return False

        # --save. Дубль-детект — по списку автопоисков: кнопка «Сохранён»
        # неперсистентна (живой census 2026-09-08), а параметрика строки
        # списка сравнивается с планом напрямую.
        try:
            existing = list_saved_searches(page)
        except SavedSearchListIndeterminate as exc:
            print(f"[FAIL] {exc}")
            return True
        except NotAuthenticated as exc:
            print(f"[FAIL] Сессия недействительна: {exc}")
            return True
        resume = resumes[0]
        duplicate = find_duplicate_saved_search(existing, resume.search)
        if duplicate is not None and force:
            print(
                f"[FAIL] Автопоиск с этой параметрикой уже сохранён "
                f"(имя hh.ru: '{duplicate.name}'); повторное сохранение не выполнено"
            )
            return True

        # Uncertain-гейт (#176/#476): unresolved-uncertain для этого
        # query_key блокирует повторное боевое сохранение до ручной сверки
        # списка автопоисков на hh.ru.
        dry_run = not force
        from ._common import ApplyProgress, DurableMutationAttempt

        query_key = saved_search_query_key(resume.search)
        run_history = History(args.history)
        attempt = (
            None
            if dry_run
            else DurableMutationAttempt(run_history, ApplyProgress(), query_key, "save_search")
        )
        if not dry_run and run_history.has_unresolved_uncertain(query_key, "save_search"):
            print(
                "[FAIL] предыдущее сохранение этого поиска не подтверждено (uncertain). "
                "Проверьте список автопоисков на hh.ru вручную перед повтором."
            )
            return True
        try:
            result = save_search_on_hh(
                page,
                resume,
                name,
                dry_run,
                before_click=attempt.before_click if attempt is not None else None,
            )
        except NotAuthenticated as exc:
            print(f"[FAIL] Сессия недействительна: {exc}")
            return True
        except BaseException as exc:
            if attempt is not None:
                attempt.interrupt(exc)
            raise
        if attempt is not None:
            attempt.finish(result)
        if result.skipped:
            print(f"[skip] {resume.id} — {result.reason}")
        elif result.success:
            print(f"[OK] {result.reason}" if dry_run else f"[OK] {resume.id} — {result.reason}")
            if dry_run:
                if duplicate is not None:
                    print(
                        f"[INFO] Внимание: автопоиск с этой параметрикой уже сохранён "
                        f"(имя hh.ru: '{duplicate.name}')"
                    )
                print("[INFO] Ничего не нажато; боевое сохранение — --force")
        else:
            prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
            print(f"{prefix} {resume.id} — {result.reason}")
            failed = True
    return failed


def _load_saved_search_filters(
    args: argparse.Namespace, config
) -> tuple[SearchFilters | None, bool]:
    """``--saved NAME``: параметрика прогона из автопоиска hh.ru.

    Возвращает (SearchFilters | None, failed). Фильтры собираются из URL
    выдачи автопоиска (parse_saved_search_url): идентичная параметрике
    автопоиска выдача получается обычным поиском с этими фильтрами.
    """
    saved = getattr(args, "saved", None)
    if not saved:
        return None, False

    from ..browser import launch_context
    from ..responses import NotAuthenticated
    from ..saved_search import (
        SavedSearchListIndeterminate,
        list_saved_searches,
        parse_saved_search_url,
    )

    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        try:
            entries = list_saved_searches(context.new_page())
        except SavedSearchListIndeterminate as exc:
            print(f"[FAIL] {exc}")
            return None, True
        except NotAuthenticated as exc:
            print(f"[FAIL] Сессия недействительна: {exc}")
            return None, True

    matches = [e for e in entries if e.name == saved]
    if not matches:
        known = ", ".join(e.name for e in entries) or "<список пуст>"
        print(f"[FAIL] Автопоиск '{saved}' не найден; список: {known}")
        return None, True
    entry = matches[0]
    if not entry.url:
        print(f"[FAIL] Автопоиск '{saved}' найден, но URL выдачи в строке не прочитан (#1052)")
        return None, True
    filters = parse_saved_search_url(entry.url)
    extras = []
    if filters.area is not None:
        extras.append(f"area={filters.area}")
    if filters.salary_from is not None:
        extras.append(f"salary={filters.salary_from}")
    if filters.experience:
        extras.append(f"experience={filters.experience}")
    if filters.schedule:
        extras.append(f"schedule={filters.schedule}")
    suffix = f" ({', '.join(extras)})" if extras else ""
    print(f"[INFO] Автопоиск '{saved}': параметры прогона text='{filters.text}'{suffix}")
    return filters, False


def _overlay_search(resume: ResumeConfig, text: str | None, area: int | None) -> ResumeConfig:
    """Иммутабельно оверлеит разовые --text/--area поверх фильтров резюме.

    None-значения не входят в оверлей: отсутствующий флаг не затирает
    настроенный фильтр. Исходный объект конфига не мутируется (dataclasses.replace).
    """
    overrides = {
        name: value for name, value in (("text", text), ("area", area)) if value is not None
    }
    if not overrides:
        return resume
    return replace(resume, search=replace(resume.search, **overrides))


def _resumes_for_search(config, args: argparse.Namespace) -> list[ResumeConfig]:
    """Resolve configured resumes, optionally overlaying an ad-hoc query."""
    text = getattr(args, "text", None)
    area = getattr(args, "area", None)
    if text is None and area is None:
        return resumes_from_args(config, args)

    if args.resume or text is None:
        # --resume: оверлей поверх выбранных резюме. Без --resume и без --text
        # один --area оверлеит все настроенные резюме (тот же смысл: разовый
        # город вместо настроенного).
        return [_overlay_search(resume, text, area) for resume in resumes_from_args(config, args)]

    # Keep history for separate ad-hoc queries isolated from configured
    # resumes and from one another.  The synthetic resume is local-only: it is
    # never used to address a resume on HH.ru.  ``area`` participates in the
    # key: the same text in different cities is a different query, and their
    # local histories must not mix.
    # m3 (ревью): склейка f"{text}|area={area}" не инъективна — текст со
    # литералом "|area=88" дал бы тот же ключ, что текст "X" + --area 88, и две
    # разные ad-hoc истории слились бы в одну. json — однозначная композиция.
    # Ветка area=None сохраняет legacy key_source=text: существующая история
    # запросов без города не сбрасывается.
    key_source = text if area is None else json.dumps([text, area], ensure_ascii=False)
    query_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()[:16]
    return [
        ResumeConfig(
            id=f"adhoc:{text}",
            resume_url=f"https://hh.ru/resume/adhoc-{query_key}",
            search=SearchFilters(text=text, area=area),
        )
    ]


def _format_salary(salary: SalaryInfo | None) -> str:
    """Человекочитаемая зарплата для вывода, пустая строка если её нет."""
    if salary is None:
        return ""
    if salary.salary_from is not None and salary.salary_to is not None:
        # Совпадающие границы — фиксированное значение, без тире.
        if salary.salary_from == salary.salary_to:
            amount = f"{salary.salary_from}"
        else:
            amount = f"{salary.salary_from}-{salary.salary_to}"
    elif salary.salary_from is not None:
        amount = f"от {salary.salary_from}"
    elif salary.salary_to is not None:
        amount = f"до {salary.salary_to}"
    else:
        return ""
    return f"{amount} {salary.currency}" if salary.currency else amount


def _format_card_line(card: VacancyCard) -> str:
    """Дополняет базовую строку карточки зарплатой, если она есть.

    Поле опционально — вакансия без зарплаты выводится как раньше, без
    «з/п не указана» и пустых скобок.
    """
    extras: list[str] = []
    salary = _format_salary(card.salary)
    if salary:
        extras.append(salary)
    suffix = f" | {' / '.join(extras)}" if extras else ""
    return f"{card.title} — {card.company} ({card.url}){suffix}"


def _record_seen(cards: list[VacancyCard], search_query: str, market=None) -> None:
    """Записывает собранные карточки в общую рыночную базу (#66, #1109).

    Побочный эффект сбора — НЕ влияет на поиск/скоринг/вывод. Пишет ВСЕ
    собранные карточки (до фильтра stop-списками/историей): рынок хочет полную
    картину сферы, а не только тех, на кого решился откликнуться. Зарплата из
    SalaryInfo (#34); None → «з/п не указана» (тоже пишется для доли рынка без
    зарплаты). Сбой записи НЕ должен валить поиск — рынок лишь удобство.

    #1109: запись ОДНА — в общую data/market.db (MarketStore); легаси-копия в
    per-account history.db убрана (vacancies_seen там больше не создаётся),
    личная аналитика (funnel/adaptive/skipped/reply) читает карточки из
    market.db. ``market`` — уже открытый MarketStore или None, если общую базу
    открыть не удалось: запись молча пропускается, поиску рынок не нужен.

    employer_tier (#93): classify_employer(company, employer_info) на каждую
    карточку — уровень известности (top_tech/big_corp/mid/unknown). Нужен для
    estimate_salary (медиана salary_to по (search_query, tier) — оценка ЗП для
    вакансий без указанной). Локальный импорт classify_employer: scoring лениво
    тянет search (цикл), тащить его на уровень модуля search-команды не нужно.

    address/is_remote/experience/snippet_requirement/snippet_responsibility
    (#517) — доп. признаки карточки для статистики/ML, прокидываются как есть
    из VacancyCard (пустая строка/None, если hh.ru не отдал блок).

    side_job/no_resume (#516) — опциональные булевы бейджи карточки. None
    означает отсутствие наблюдения и не затирает уже сохранённое значение.
    activity/hh_rating/hrbrand_winner/metro_stations (#551) — приоритет-3
    признаки; станции сохраняются JSON-массивом, чтобы поддержать 0..N.
    """
    if market is None:
        return
    from ..scoring import classify_employer

    for card in cards:
        salary = card.salary
        title = card.title.strip() or None
        company = card.company.strip() or None
        # An empty company means that this scrape did not produce a usable
        # observation. Do not turn it into ``unknown`` and overwrite a
        # previously classified employer during selector drift (#532).
        employer_info = getattr(card, "employer_info", None) if company else None
        tier = classify_employer(company, employer_info) if company else None
        # Review-финдинг (#532): classify_employer("unknown") не отличает
        # «компания реально неизвестна» от «reviews-селектор не дал сигнала на
        # этом scrape» — оба случая раньше схлопывались в строку "unknown",
        # которая проходит COALESCE(NULLIF(...)) как непустая и затирала ранее
        # подтверждённый tier (например "mid" из прошлого scrape). top_tech/
        # big_corp матчатся по имени и не зависят от employer_info, поэтому им
        # это не грозит — гейтим только unknown. Гейт широкий: «unknown»
        # достоверен лишь когда reviews_count РЕАЛЬНО прочитан (и оказался ниже
        # порога). Если reviews_count не прочитан — либо весь блок
        # employer_info=None (селектор-промах), либо partial-failure (rating/
        # trusted выжили, но reviews_count-селектор дрейфнул) — оба случая
        # неоднозначны, подавляем в None, оставляя COALESCE прежнее значение.
        # reviews_count прочитан, но мал (вкл. 0) → настоящий «небольшой
        # работодатель», unknown не подавляется.
        if tier == "unknown" and (employer_info is None or employer_info.reviews_count is None):
            tier = None
        try:
            market.upsert_vacancy_seen(
                vacancy_id=card.vacancy_id,
                search_query=search_query,
                title=title,
                company=company,
                salary_from=salary.salary_from if salary else None,
                salary_to=salary.salary_to if salary else None,
                salary_currency=salary.currency if salary else None,
                employer_tier=tier,
                vacancy_text=card.vacancy_text or None,
                published_at=card.published_at.isoformat() if card.published_at else None,
                address=card.address or None,
                is_remote=card.is_remote,
                experience=card.experience or None,
                snippet_requirement=card.snippet_requirement or None,
                snippet_responsibility=card.snippet_responsibility or None,
                side_job=card.side_job,
                no_resume=card.no_resume,
                activity=card.activity or None,
                hh_rating=card.hh_rating or None,
                hrbrand_winner=card.hrbrand_winner,
                metro_stations=json.dumps(card.metro_stations, ensure_ascii=False)
                if card.metro_stations is not None
                else None,
            )
        except Exception as e:  # noqa: BLE001 — рынок не должен валить поиск
            logger.warning("Не записать вакансию %s в рынок: %s", card.vacancy_id, e)


def run(args: argparse.Namespace) -> bool:
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..search import (
        VacancySearchIndeterminate,
        filter_candidates,
        rank_candidates,
        search_vacancies,
    )

    config = load_config_or_exit(args.config)
    history = History(args.history)
    saved_mode_failed = _run_saved_search_modes(args, config)
    if saved_mode_failed is not None:
        # Режимы --save/--list-saved завершают команду: обычный поиск
        # в тех же прогонах не выполняется.
        return saved_mode_failed
    if getattr(args, "saved", None) and (args.text or args.resume or args.area):
        # --area добавлен в guard при rebase (issue-city-search-gaps): параметрика
        # --saved приходит из URL автопоиска (включая его area) — явный оверлей
        # игнорировался бы молча, как --text/--resume.
        print("[FAIL] --saved не сочетается с --text/--resume/--area: параметрика прогона")
        print("       берётся из автопоиска, явные фильтры игнорировались бы молча")
        return True
    saved_filters, saved_failed = _load_saved_search_filters(args, config)
    if saved_failed:
        return True
    if saved_filters is not None:
        # --saved NAME: параметрика прогона — из автопоиска hh.ru, а не из
        # конфига/--text. Синтетическое резюме локальное (как у adhoc --text).
        resumes = [
            ResumeConfig(
                id=f"saved:{getattr(args, 'saved', '')}",
                resume_url=f"https://hh.ru/resume/saved-{getattr(args, 'saved', '')}",
                search=saved_filters,
            )
        ]
    else:
        resumes = _resumes_for_search(config, args)

    # #1106/#1109: общая рыночная база для записи собранных карточек. Открытие
    # мягкое (open_market возвращает None при сбое): невозможность открыть
    # market.db не должна мешать поиску (personal-история пишется независимо).
    # Рынок нужен только самому прогону поиска, поэтому открывается ПОСЛЕ
    # завершающих режимов --save/--list-saved и guard'а --saved — им он не нужен.
    from ..market_store import open_market

    market = open_market()
    failed = False
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        for resume in resumes:
            print(f"\n=== Поиск вакансий для резюме: {resume.id} ===")
            try:
                cards = search_vacancies(page, resume.search, max_pages=args.max_pages)
            except VacancySearchIndeterminate as e:
                # Не выдаём непроверенную выдачу за «вакансий нет» и не прячем
                # диагностический отказ за traceback. Следующее резюме можно
                # обработать независимо.
                #
                # cycle-review PR #460 (round 1): partial_results — недостоверный
                # снимок (сам процесс поиска не подтверждён), не «частичный успех».
                # Раньше их без continue пропускали дальше в _record_seen (рынок
                # получал недостоверные данные) и filter_candidates/rank_candidates
                # (печатались как подтверждённые кандидаты) — fail-closed: просто
                # переходим к следующему резюме, ничего не записываем и не выводим.
                print(
                    f"[FAIL] {e}; state={e.state} page={e.page_num} url={e.url} "
                    f"partial_results={len(e.partial_results)} diagnostics={e.diagnostics}"
                )
                failed = True
                continue
            # #66: запись собранных карточек в рынок (побочный эффект сбора) —
            # между search_vacancies и filter_candidates, не трогая отбор/скоринг.
            _record_seen(cards, resume.search.text, market=market)
            # pre-LLM фильтр работодателя (#85): пороги из опц. scoring.prefilter.
            scoring = getattr(resume, "scoring", None)
            prefilter = getattr(scoring, "prefilter", None)
            candidates, skipped = filter_candidates(
                cards,
                resume.search,
                resume.resume_id,
                history,
                prefilter,
                getattr(scoring, "resume_match_threshold", None),
                getattr(resume, "ai_profile", None),
            )
            ranked = rank_candidates(candidates, resume.search, resume)

            print(
                f"Найдено всего: {len(cards)}, "
                f"подходящих: {len(candidates)}, исключено: {len(skipped)}"
            )
            # E4 (issue-city-search-gaps, факт 7): без scoring-секции
            # rank_candidates работает на _ZERO_WEIGHTS и каждый кандидат
            # получает score=+0.00 BY DESIGN (порядок выдачи hh.ru). Нулевой
            # score на каждой строке — шум, похожий на поломку: печатаем один
            # [INFO] в шапке резюме и не выводим score= построчно. Логика
            # ранжирования (search.py) не меняется — только UX вывода.
            show_score = scoring is not None
            if not show_score:
                print("[INFO] scoring не сконфигурирован — порядок выдачи hh.ru")
            for c, score, breakdown in ranked:
                factors = ", ".join(
                    f"{name}={value:+.2f}" for name, value in breakdown.items() if value
                )
                detail = f" | {factors}" if factors else ""
                prefix = f"score={score:+.2f} " if show_score else ""
                print(f"  [candidate] {prefix}{_format_card_line(c)}{detail}")
            for card, reason in skipped:
                print(f"  [skip] {_format_card_line(card)} — {reason}")
    return failed

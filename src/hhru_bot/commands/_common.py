"""Общий код команд CLI: разбор резюме, общие аргументы, контекст запуска.

Владелец будущих правок здесь — #2 (stats и т.п. общие расширения). Команды
(login/search/apply/bump/run) живут каждое в своём модуле и авторегистрируются
через register(subparsers) — см. cli.build_parser.
"""

from __future__ import annotations

import argparse
import logging

from ..apply import apply_to_vacancy
from ..apply.letter import CoverLetterProvider
from ..config import AppConfig, ResumeConfig
from ..history import History
from ..search import filter_candidates, rank_candidates, search_vacancies
from ..throttle import LimitReached, Throttle

logger = logging.getLogger("hhru_bot.cli")


def add_common_args(p: argparse.ArgumentParser) -> None:
    """Общие аргументы для команд, работающих по резюме/поиску."""
    p.add_argument("--resume", help="ID резюме из конфига (по умолчанию — все)")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что будет сделано, без реальных действий",
    )
    p.add_argument("--max-pages", type=int, default=5, help="Максимум страниц поиска")


def resolve_resumes(config: AppConfig, resume_ids: list[str] | None) -> list[ResumeConfig]:
    if not resume_ids:
        return config.resumes
    return [config.get_resume(rid) for rid in resume_ids]


def resumes_from_args(config: AppConfig, args: argparse.Namespace) -> list[ResumeConfig]:
    return resolve_resumes(config, [args.resume] if args.resume else None)


def _build_letter_provider(
    config: AppConfig,
    resume: ResumeConfig,
    cover_letter_template: str,
) -> CoverLetterProvider | None:
    """Строит AI-провайдер писем, если AI включён (#17).

    AI включён = есть ТОП-ЛЕВЕЛ секция ai (LLM-провайдер, #16) И resume-секция
    ai_profile (данные кандидата). Иначе None → статичный .format (обратная
    совместимость, дефолт).

    Построение LLMClient тянет openai (lazy, но при construction). Если openai
    не установлен ([ai] optional-deps) — логируем и откатываемся на шаблон:
    отсутствие AI-зависимости не должно валить обычный отклик. Сам провайдер
    дальше устойчив (любой сбой LLM → fallback внутри), см. ai/letters.py.
    """
    ai_config = getattr(config, "ai", None)
    profile = getattr(resume, "ai_profile", None)
    if ai_config is None or profile is None:
        return None

    from ..ai.letters import AICoverLetterProvider
    from ..ai.llm_client import LLMClient

    try:
        llm_client = LLMClient(ai_config)
    except ImportError as e:
        logger.warning(
            "AI-письма недоступны для резюме '%s' (openai не установлен?): %s — "
            "используется статичный шаблон. Установите: pip install -e '.[ai]'",
            resume.id,
            e,
        )
        return None

    logger.info("AI-письма включены для резюме '%s' (провайдер: %s)", resume.id, ai_config.provider)
    return AICoverLetterProvider(
        llm_client=llm_client,
        resume_profile=profile,
        fallback_template=cover_letter_template,
    )


def run_apply_for_resume(
    page,
    config: AppConfig,
    resume: ResumeConfig,
    history: History,
    throttle: Throttle,
    args: argparse.Namespace,
) -> None:
    """Цикл откликов по одному резюме (search → filter → apply с троттлингом).

    Перенесено дословно из cli._apply_for_resume. Принципы CLAUDE.md сохранены:
    дедупликация и стоп-листы через filter_candidates (history-based),
    дневной лимит проверяется перед каждым откликом, throttle.wait между откликами.

    #17: если включён AI (секция ai + ai_profile) — отклик идёт через
    CoverLetterProvider; иначе (провайдер None) — статичный шаблон, поведение
    не меняется. letter_variant пишется в history для A/B-среза (Этап 3).
    """
    print(f"\n=== Отклики для резюме: {resume.id} ===")

    try:
        throttle.check_apply_limit(resume.resume_id, args.dry_run)
    except LimitReached as e:
        print(f"Пропуск: {e}")
        return

    cards = search_vacancies(page, resume.search, max_pages=args.max_pages)
    # pre-LLM фильтр работодателя (#85): пороги из опц. секции scoring.prefilter.
    # resume.scoring=None / prefilter=None / enabled=False → фильтр откл. (no-op
    # внутри filter_candidates), обратная совместимость.
    prefilter = getattr(getattr(resume, "scoring", None), "prefilter", None)
    candidates, skipped = filter_candidates(
        cards, resume.search, resume.resume_id, history, prefilter
    )

    for card, reason in skipped:
        logger.debug("Пропуск вакансии %s: %s", card.title, reason)

    # Ранжирование кандидатов по score (#15) — строго между filter_candidates
    # и срезом [:limit], чтобы дневной лимит уходил на лучшие совпадения.
    ranked = rank_candidates(candidates, resume.search, resume)

    limit = args.limit if args.limit else len(ranked)
    cover_letter_template = config.cover_letter_for(resume)
    letter_provider = _build_letter_provider(config, resume, cover_letter_template)

    applied_count = 0
    for card, _score, _breakdown in ranked[:limit]:
        try:
            throttle.check_apply_limit(resume.resume_id, args.dry_run)
        except LimitReached as e:
            print(f"Дневной лимит достигнут, останавливаюсь: {e}")
            break

        result = apply_to_vacancy(
            page,
            card,
            resume.resume_id,
            cover_letter_template,
            args.dry_run,
            letter_provider=letter_provider,
        )
        status = "dry_run" if args.dry_run else ("success" if result.success else "failed")
        history.record_action(
            resume.resume_id,
            card.vacancy_id,
            "apply",
            status,
            result.reason,
            letter_variant=result.letter_variant,
        )

        if result.success:
            applied_count += 1
            print(f"  [OK] {card.title} — {card.company}")
        else:
            print(f"  [FAIL] {card.title} — {result.reason}")

        throttle.wait(f"после отклика на '{card.title}'")

    print(f"Итого откликов за этот запуск: {applied_count}")

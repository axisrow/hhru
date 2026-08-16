"""Общий код команд CLI: разбор резюме, общие аргументы, контекст запуска.

Владелец будущих правок здесь — #2 (stats и т.п. общие расширения). Команды
(login/search/apply/bump/run) живут каждое в своём модуле и авторегистрируются
через register(subparsers) — см. cli.build_parser.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..apply import apply_to_vacancy
from ..apply.letter import CoverLetterProvider
from ..config import AppConfig, ResumeConfig, SearchFilters, is_resume_url_placeholder
from ..config_sections.scoring import ScoringWeights
from ..history import SKIP_REASONS, History
from ..search import (
    _LLM_SHORTLIST_DEFAULT,
    VacancyCard,
    VacancySearchIndeterminate,
    filter_candidates,
    rank_candidates,
    search_vacancies,
)
from ..throttle import LimitReached, Throttle

if TYPE_CHECKING:
    from ..scoring import LLMScoringProvider, ScoringProvider

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


def _build_scoring_provider(
    config: AppConfig,
    resume: ResumeConfig,
) -> LLMScoringProvider | None:
    """Строит ML scoring-провайдер для ранжирования, если AI включён (#81).

    Зеркало ``_build_letter_provider`` (#17): AI включён = есть ТОП-ЛЕВЕЛ секция
    ai (LLM-провайдер, #16) И resume-секция ai_profile (данные кандидата). Иначе
    None → ``rank_candidates`` без провайдера = чистая эвристика #15 (обратная
    совместимость, поведение не меняется — тот же приём, что letter_provider).

    При AI строит: LLMClient → HeuristicScoringProvider (fallback: эвристика #15
    + tier-буст #74) → LLMScoringProvider. Построение LLMClient тянет openai
    (lazy, при construction): если openai не установлен ([ai] optional-deps) —
    логируем и откатываемся на None (эвристику), как с письмами. Отсутствие
    AI-зависимости не должно валить обычный отклик. Сам провайдер дальше устойчив
    (любой сбой LLM → fallback внутри, circuit-breaker), см. scoring.py.

    Отдельный LLMClient (не из _build_letter_provider) — намеренно, ради простоты
    и независимости циклов (см. замечание по дизайну в #81).
    """
    ai_config = getattr(config, "ai", None)
    profile = getattr(resume, "ai_profile", None)
    if ai_config is None or profile is None:
        return None

    from ..scoring import HeuristicScoringProvider, LLMScoringProvider

    # weights — ровно как в rank_candidates для AI-пути: из resume.scoring, иначе
    # дефолтные ScoringWeights() (НЕ _ZERO_WEIGHTS). HeuristicScoringProvider —
    # fallback LLMScoringProvider и должен скорить на той же шкале/весах, что
    # предранжирование в rank_candidates (нормализация F2 из #74). Согласовано с
    # rank_candidates: scoring is None + provider is not None → ScoringWeights()
    # (фикс Codex-ревью #81: иначе нейтральные веса → shortlist берёт первые K
    # по входу, а не лучших).
    scoring = getattr(resume, "scoring", None)
    weights = scoring.weights if scoring is not None else ScoringWeights()
    heuristic = HeuristicScoringProvider(resume.search, weights)

    from ..ai.llm_client import LLMClient

    try:
        llm_client = LLMClient(ai_config)
    except ImportError as e:
        logger.warning(
            "ML-скоринг недоступен для резюме '%s' (openai не установлен?): %s — "
            "ранжирование идёт по эвристике. Установите: pip install -e '.[ai]'",
            resume.id,
            e,
        )
        return None

    logger.info("ML-скоринг включён для резюме '%s' (провайдер: %s)", resume.id, ai_config.provider)
    return LLMScoringProvider(
        llm_client=llm_client,
        fallback=heuristic,
        resume_profile=profile,
    )


@dataclass
class ApplyPlan:
    """План отклика по резюме: ранжированные кандидаты + статистика построения.

    Чистый результат build_apply_plan (#101) — без браузера, без побочных
    эффектов. ranked — тот же формат, что возвращает rank_candidates: список
    (карточка, score, разбивка факторов), уже урезанный по limit. total/
    after_filter/after_limit — счётчики для логов/отчётов по стадиям
    filter -> rank -> limit; skipped — пары (карточка, причина) из
    filter_candidates, для отладочного логирования вызывающей стороной.
    """

    ranked: list[tuple[VacancyCard, float, dict[str, float]]]
    skipped: list[tuple[VacancyCard, str]] = field(default_factory=list)
    total: int = 0
    after_filter: int = 0
    after_limit: int = 0


def build_apply_plan(
    candidates: list[VacancyCard],
    filters: SearchFilters,
    resume: ResumeConfig,
    history: History,
    scoring_provider: ScoringProvider | None = None,
    limit: int | None = None,
) -> ApplyPlan:
    """Строит план откликов: filter -> pre-LLM -> rank -> slice по limit.

    Чистая функция (без браузера) — вынесена из run_apply_for_resume (#101),
    чтобы решения filter/rank/limit были тестируемы без hh.ru. candidates —
    СЫРЫЕ карточки из search_vacancies (ещё без применения exclude_employers/
    exclude_keywords/истории — это делает filter_candidates внутри, см.
    CLAUDE.md про разделение поиска и фильтрации).

    limit: None или 0 (CLI-конвенция args.limit по умолчанию 0 = флаг не задан)
    означают "без среза" — берутся все ranked-кандидаты, как раньше
    (candidates[:limit] с limit=len(ranked)).
    """
    prefilter = getattr(getattr(resume, "scoring", None), "prefilter", None)
    filtered, skipped = filter_candidates(candidates, filters, resume.resume_id, history, prefilter)

    ranked = rank_candidates(
        filtered,
        filters,
        resume,
        scoring_provider=scoring_provider,
        llm_shortlist=_LLM_SHORTLIST_DEFAULT,
    )

    effective_limit = limit if limit else len(ranked)
    sliced = ranked[:effective_limit]

    return ApplyPlan(
        ranked=sliced,
        skipped=skipped,
        total=len(candidates),
        after_filter=len(filtered),
        after_limit=len(sliced),
    )


def run_apply_for_resume(
    page,
    config: AppConfig,
    resume: ResumeConfig,
    history: History,
    throttle: Throttle,
    args: argparse.Namespace,
) -> bool:
    """Цикл откликов по одному резюме (search → filter → apply с троттлингом).

    Перенесено дословно из cli._apply_for_resume. Принципы CLAUDE.md сохранены:
    дедупликация и стоп-листы через filter_candidates (history-based),
    дневной лимит проверяется перед каждым откликом, throttle.wait между откликами.

    #17: если включён AI (секция ai + ai_profile) — отклик идёт через
    CoverLetterProvider; иначе (провайдер None) — статичный шаблон, поведение
    не меняется. letter_variant пишется в history для A/B-среза (Этап 3).

    #101: план (filter → rank → limit) строится чистой build_apply_plan;
    здесь остаётся только execution-оркестрация (providers, apply_to_vacancy,
    запись истории, throttle).

    #148: возвращает True, если поиск вакансий завершился неопределённо
    (VacancySearchIndeterminate) — apply.run() агрегирует это по всем
    резюме и транслирует в ненулевой exit code через cli.main(). False —
    во всех остальных случаях (включая исчерпанный дневной лимит).

    #165: True также при плейсхолдере resume_url в конфиге — конфиг с
    плейсхолдером фейлится явно и рано (до search/истории/отправки формы),
    а не молча откликается default-резюме аккаунта.
    """
    print(f"\n=== Отклики для резюме: {resume.id} ===")

    if is_resume_url_placeholder(resume.resume_url):
        # Fail closed до любого write-действия: на форме с единственным
        # резюме hh.ru не валидирует resume_id (#165), и отклик уходит
        # default-резюме, а история пишется под фейковым id.
        print(
            f"[FAIL] {resume.id} — в конфиге указан плейсхолдер resume_url; "
            "укажите реальный URL (получить можно через list-resumes --remote)"
        )
        return True
    try:
        throttle.check_apply_limit(resume.resume_id, args.dry_run)
    except LimitReached as e:
        print(f"Пропуск: {e}")
        return False

    try:
        cards = search_vacancies(page, resume.search, max_pages=args.max_pages)
    except VacancySearchIndeterminate as e:
        # Один сбой рендера не должен скрыться как пустой apply-план или
        # остановить обработку остальных резюме в команде apply/run.
        print(f"[FAIL] {e}")
        return True
    scoring_provider = _build_scoring_provider(config, resume)
    plan = build_apply_plan(
        cards,
        resume.search,
        resume,
        history,
        scoring_provider=scoring_provider,
        limit=args.limit,
    )

    for card, reason in plan.skipped:
        logger.debug("Пропуск вакансии %s: %s", card.title, reason)

    cover_letter_template = config.cover_letter_for(resume)
    letter_provider = _build_letter_provider(config, resume, cover_letter_template)

    applied_count = 0
    for card, _score, _breakdown in plan.ranked:
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

        if result.skipped:
            # #95: форма требует анкеты — НЕ считаем откликом, НЕ пишем actions,
            # НЕ ждём throttle (отправки не было — анти-бан-пауза не нужна). Кэш
            # skipped (#87) не даст повторно дойти до формы на следующем search.
            history.record_skip(resume.resume_id, card.vacancy_id, SKIP_REASONS.HAS_QUESTIONS)
            print(f"  [skip] {card.title} — {result.reason}")
            continue

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
    return False

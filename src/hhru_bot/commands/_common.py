"""Общий код команд CLI: разбор резюме, общие аргументы, контекст запуска.

Владелец будущих правок здесь — #2 (stats и т.п. общие расширения). Команды
(login/search/apply/bump/run) живут каждое в своём модуле и авторегистрируются
через register(subparsers) — см. cli.build_parser.
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..apply import apply_to_vacancy
from ..apply.antibot import AntiBotChallengeDetected, raise_for_antibot
from ..apply.letter import CoverLetterProvider
from ..apply.verify import verify_response_in_negotiations
from ..config import AppConfig, ResumeConfig, SearchFilters, is_resume_url_placeholder
from ..config_sections.scoring import ScoringWeights
from ..copy_resume import resolve_numeric_resume_ids
from ..history import History
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
    p.add_argument(
        "--resume",
        help="Slug из конфига или resume_id HH.ru (по умолчанию — все)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что будет сделано, без реальных действий",
    )
    p.add_argument("--max-pages", type=int, default=5, help="Максимум страниц поиска")


def add_force_arg(p: argparse.ArgumentParser) -> None:
    """``--force`` — только для команд, реально вызывающих run_apply_for_resume.

    #97 cycle-review: жить в add_common_args означало бы протечь на search/bump/
    probe с чужим (apply-специфичным) help-текстом и no-op эффектом на bump —
    отдельная функция для apply.py/run.py.
    """
    p.add_argument(
        "--force",
        action="store_true",
        help="Разрешить реальную отправку отклика с LLM-ответами на вопросы",
    )


def resolve_resumes(config: AppConfig, resume_ids: list[str] | None) -> list[ResumeConfig]:
    if not resume_ids:
        return config.resumes
    return [config.get_resume(rid) for rid in resume_ids]


def resumes_from_args(config: AppConfig, args: argparse.Namespace) -> list[ResumeConfig]:
    return resolve_resumes(config, [args.resume] if args.resume else None)


# #319: реальный resume_id HH.ru — hex-хэш (в тестах укороченный), slug'и конфига
# под паттерн не попадают (это слова в нижнем регистре с дефисами).
_RESUME_HASH_RE = re.compile(r"[0-9a-f]{6,}")


def resolve_resume(config: AppConfig, key: str, needs: tuple[str, ...] = ()) -> ResumeConfig:
    """Резолв ``--resume`` по slug из конфига, реальному resume_id HH.ru или bare (#319).

    Порядок: запись конфига (slug или hash) → если не найдено и ключ похож на
    hex-хэш HH.ru — bare-резюме без настроек. ``needs`` — имена секций
    (``ai_profile``, ``education``, ...), без которых команда не имеет смысла:
    для их отсутствия поднимается точечная ConfigError про недостающую настройку,
    а не вводящая в заблуждение «резюме не найдено в конфиге».

    Массовый apply-путь (``resolve_resumes``) намеренно НЕ использует bare:
    отклик/поиск без настроек конфига не имеют смысла и должны требовать явной
    регистрации резюме.
    """
    from ..config import ConfigError, bare_resume

    try:
        resume = config.get_resume(key)
    except ConfigError:
        if _RESUME_HASH_RE.fullmatch(key):
            resume = bare_resume(key)
        else:
            raise
    for field_name in needs:
        if getattr(resume, field_name) is None:
            raise ConfigError(
                f"Для резюме '{key}' требуется настройка '{field_name}' в config.yaml "
                "(резюме не зарегистрировано в конфиге или секция не задана)."
            )
    return resume


def _build_letter_provider(
    config: AppConfig,
    resume: ResumeConfig,
    cover_letter_template: str,
) -> CoverLetterProvider | None:
    """Строит AI-провайдер писем, если AI включён (#17).

    AI включён = есть ТОП-ЛЕВЕЛ секция ai (LLM-провайдер, #16) И resume-секция
    ai_profile (данные кандидата). Иначе None → статичный .format (обратная
    совместимость, дефолт).

    Построение LLMClient тянет hermes-agent-axisrow (lazy, но при
    construction). Если пакет не установлен ([ai] optional-deps) — логируем и
    откатываемся на шаблон: отсутствие AI-зависимости не должно валить обычный
    отклик. Сам провайдер дальше устойчив (любой сбой LLM → fallback внутри),
    см. ai/letters.py.
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
            "AI-письма недоступны для резюме '%s' (hermes-agent-axisrow не установлен?): %s — "
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


def _build_question_answerer(
    config: AppConfig, resume: ResumeConfig, known_data: dict[str, str] | None = None
):
    """Build the opt-in LLM question answerer, if configured."""
    ai_config = getattr(config, "ai", None)
    if ai_config is None or not ai_config.answer_questions:
        return None
    from ..ai.llm_client import LLMClient
    from ..ai.questions import AIQuestionAnswerer

    try:
        client = LLMClient(ai_config)
    except ImportError as exc:
        logger.warning("LLM-ответы на вопросы недоступны: %s", exc)
        return None
    return AIQuestionAnswerer(client, getattr(resume, "ai_profile", None), known_data)


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
    + tier-буст #74) → LLMScoringProvider. Построение LLMClient тянет
    hermes-agent-axisrow (lazy, при construction): если пакет не установлен
    ([ai] optional-deps) — логируем и откатываемся на None (эвристику), как с
    письмами. Отсутствие AI-зависимости не должно валить обычный отклик. Сам
    провайдер дальше устойчив (любой сбой LLM → fallback внутри,
    circuit-breaker), см. scoring.py.

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
            "ML-скоринг недоступен для резюме '%s' (hermes-agent-axisrow не установлен?): %s — "
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
    cards_override=None,
    skip_scoring: bool = False,
    ranked_override=None,
) -> bool:
    """Цикл откликов по одному резюме (search → filter → apply с троттлингом).

    Перенесено дословно из cli._apply_for_resume. Принципы CLAUDE.md сохранены:
    дедупликация и стоп-листы через filter_candidates (history-based),
    дневной лимит проверяется перед каждым откликом, throttle.wait после
    реальных отправок (#163: исходы до submit — без паузы и записи в actions).

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
            "укажите реальный URL (получить можно через list-resumes)"
        )
        return True
    try:
        throttle.check_apply_limit(resume.resume_id, args.dry_run)
    except LimitReached as e:
        print(f"Пропуск: {e}")
        return False

    # #216: hh.ru silently substitutes another account resume when the
    # configured one is not_finished. Check this before searching or any
    # possible submit, and fail closed rather than creating a misleading
    # history entry under the configured resume.
    account_resume_ids: set[str] | None = None
    verify_resume_id = resume.resume_id
    if not args.dry_run:
        ids_by_hash = resolve_numeric_resume_ids(page)
        # #344: a challenge during the resume preflight is terminal for the
        # whole apply/run command, not an unknown mapping to continue past.
        raise_for_antibot(page)
        if ids_by_hash is not None:
            numeric_id = ids_by_hash.get(resume.resume_id)
            if numeric_id is not None:
                # Хэш конфига есть в маппинге аккаунта — SSR прочитан успешно
                # именно для этого резюме. not_finished фейлится явно (#216).
                # Отсутствие поля status в SSR для присутствующего в маппинге
                # хэша (schema drift) трактуем так же: мы прочитали резюме
                # аккаунта, но не можем подтвердить его готовность к откликам
                # — здесь, в отличие от отсутствия самого хэша (#212, домен
                # верификатора), у нас есть основания фейлиться до отправки,
                # а не полагаться на пост-клик verify.
                resume_status = getattr(ids_by_hash, "statuses", {}).get(resume.resume_id)
                if resume_status is None or resume_status == "not_finished":
                    print(
                        f"[FAIL] {resume.id} — статус резюме на hh.ru не подтверждён как "
                        f"готовый к отклику (status={resume_status!r}); завершите/опубликуйте "
                        "его вручную"
                    )
                    return True
                account_resume_ids = set(ids_by_hash.values())
                verify_resume_id = numeric_id
            else:
                # Конфиг-хэш отсутствует в маппинге аккаунта (устаревший/
                # неверный хэш, #212) — не наш домен: перечень НЕ заполняем,
                # атрибуция уходит в incomparable → fail-closed indeterminate
                # в самом верификаторе, апробировано тестами #212.
                logger.warning(
                    "%s — резюме конфига (%s) нет в маппинге аккаунта: атрибуция в "
                    "верификаторе уйдёт в fail-closed",
                    resume.id,
                    resume.resume_id,
                )

    if cards_override is None:
        try:
            cards = search_vacancies(page, resume.search, max_pages=args.max_pages)
        except VacancySearchIndeterminate as e:
            # Search timeouts are normally per-resume failures, but a confirmed
            # challenge must escape as the terminal AntiBotChallengeDetected state.
            raise_for_antibot(page)
            # Один сбой рендера не должен скрыться как пустой apply-план или
            # остановить обработку остальных резюме в команде apply/run.
            print(f"[FAIL] {e}")
            return True
        # Also catch challenge pages that happened to look like an empty/partial
        # search result to the parser and therefore returned without raising.
        raise_for_antibot(page)
    else:
        cards = cards_override
    scoring_provider = None if skip_scoring else _build_scoring_provider(config, resume)
    plan = build_apply_plan(
        cards,
        resume.search,
        resume,
        history,
        scoring_provider=scoring_provider,
        limit=args.limit,
    )
    if ranked_override is not None:
        allowed = {card.vacancy_id for card, _score, _breakdown in plan.ranked}
        routed_ranked = [item for item in ranked_override if item[0].vacancy_id in allowed]
        effective_limit = args.limit if args.limit else len(routed_ranked)
        routed_ranked = routed_ranked[:effective_limit]
        plan = ApplyPlan(
            ranked=routed_ranked,
            skipped=plan.skipped,
            total=plan.total,
            after_filter=plan.after_filter,
            after_limit=len(routed_ranked),
        )

    for card, reason in plan.skipped:
        logger.debug("Пропуск вакансии %s: %s", card.title, reason)

    cover_letter_template = config.cover_letter_for(resume)
    letter_provider = _build_letter_provider(config, resume, cover_letter_template)
    question_answerer = _build_question_answerer(config, resume, history.get_profile_answers())
    # M6 cycle-review #373: no longer blocks the whole run when
    # ai.answer_questions=true but --force is missing — pipeline.py gates
    # --force per vacancy, only once a questionnaire is actually detected, so
    # an ordinary apply against vacancies without questions still works. This
    # is only an upfront heads-up for the --force case; the real fail-closed
    # enforcement lives in pipeline.py.
    if question_answerer is not None and not args.dry_run and getattr(args, "force", False):
        print(
            "[WARN] --force включён: LLM-ответы на тест-вопросы будут заполнены "
            "и отправлены без дополнительного подтверждения"
        )

    # #212: атрибуция резюме в верификаторе. Конфиг знает хэш резюме, SSR
    # /applicant/negotiations — только числовой resumeId; без маппинга found
    # недостижим (false negative #3, 135170581). Один read-only goto за запуск;
    # сбой маппинга не фатален — верификатор получит хэш и уйдёт в fail-closed
    # indeterminate при совпадении вакансии. dry_run до клика не доходит —
    # верификатор не зовётся, лишний goto не нужен.
    def _verifier(page, vacancy_id, _pipeline_resume_id):  # noqa: ANN001
        # pipeline передаёт хэш конфига (ключ history) — подменяем его числовым
        # id для сравнения с SSR; запись в history и троттл остаются в домене
        # хэша, миграций нет.
        return verify_response_in_negotiations(
            page, vacancy_id, verify_resume_id, account_resume_ids=account_resume_ids
        )

    applied_count = 0
    for card, _score, _breakdown in plan.ranked:
        try:
            throttle.check_apply_limit(resume.resume_id, args.dry_run)
        except LimitReached as e:
            print(f"Дневной лимит достигнут, останавливаюсь: {e}")
            break

        action_id = None

        def _before_submit(vacancy_id: str = card.vacancy_id) -> None:
            nonlocal action_id
            # #245: commit the fail-closed audit marker immediately before
            # entering the irreversible form path. A process crash can leave
            # browser dumps (and possibly a sent application) without
            # returning an ApplyResult; waiting for the post-action record
            # would make the next run send a duplicate. Keeping this hook after
            # navigation/questions preserves the old no-action semantics for
            # confirmed pre-submit exits.
            action_id = history.begin_action(resume.resume_id, vacancy_id, "apply")

        # dict value type intentionally broad — **apply_kwargs spreads several
        # unrelated kwarg types (provider/verifier/callable/bool) into
        # apply_to_vacancy's distinct typed parameters below.
        apply_kwargs: dict[str, Any] = {
            "letter_provider": letter_provider,
        }
        if not args.dry_run:
            # #207: fail-вердикты после клика по кнопке отклика подтверждаются
            # внешней проверкой /applicant/negotiations до записи в history.
            # cycle-review round 2 (#373): defence-in-depth — dry-run must
            # never reach the external verifier at all (pipeline.py's
            # _finalize_post_click_failure also guards on ctx.dry_run, but a
            # verifier=None short-circuit here means a dry-run answerer's
            # post-click grey-zone failure can't accidentally record acted).
            apply_kwargs["verifier"] = _verifier
            apply_kwargs["before_submit"] = _before_submit
        if question_answerer is not None:
            apply_kwargs["question_answerer"] = question_answerer
            apply_kwargs["force"] = getattr(args, "force", False)
        try:
            result = apply_to_vacancy(
                page,
                card,
                resume.resume_id,
                cover_letter_template,
                args.dry_run,
                **apply_kwargs,
            )
        except AntiBotChallengeDetected as exc:
            # A post-submit challenge can arrive after before_submit reserved
            # the row. Keep its dedup/limit-safe uncertain status, but replace
            # the generic crash reason before terminating the whole command.
            if action_id is not None:
                history.finalize_action(action_id, "uncertain", str(exc))
            raise

        if result.skipped:
            # #95: форма требует анкеты — НЕ считаем откликом, НЕ пишем actions,
            # НЕ ждём throttle (отправки не было — анти-бан-пауза не нужна). Кэш
            # skipped (#87) не даст повторно дойти до формы на следующем search.
            # #226 cycle-review: причина берётся из result.skip_reason, а не
            # жёстко HAS_QUESTIONS — иначе already-responded-skip (#226) терялся
            # бы под чужой причиной (ломало clear-skipped --reason).
            # Инвариант: все persistent skip-результаты создаются через
            # ApplyContext.skip(), чей дефолт — HAS_QUESTIONS; новый skip-путь
            # обязан передать здесь собственный skip_reason явно.
            history.record_skip(resume.resume_id, card.vacancy_id, result.skip_reason)
            if action_id is not None:
                history.finalize_action(action_id, "failed", result.reason)
            print(f"  [skip] {card.title} — {result.reason}")
            continue

        # #163: actions — журнал реальных взаимодействий с hh.ru. Запись только
        # после реального submit либо для успешной dry_run-симуляции — dry_run-
        # строки НЕ трогаем: их читает дедупликация has_applied (CLAUDE.md п.2).
        # Провалы до submit (форма входа, «уже откликались», кнопка не найдена)
        # на hh.ru не отправлялись — остаются в консоли/логе, не в статистике.
        if action_id is not None:
            # #176: uncertain — submit мог уйти, но результат неизвестен. Такой
            # статус видит дедупликация has_applied (повторный запуск не
            # откликнется на ту же вакансию вторым письмом) — «просто failed»
            # не годится. dry_run по определению без клика — uncertain там
            # невозможен.
            if args.dry_run:
                status = "dry_run"
            elif result.uncertain:
                status = "uncertain"
            else:
                status = "success" if result.success else "failed"
            history.finalize_action(
                action_id,
                status,
                result.reason,
                letter_variant=result.letter_variant,
            )
        elif result.acted:
            # The verifier can positively reconcile an external submit even
            # when the pre-submit hook was not reached (for example, a
            # transitional page exposed the post-click state directly).
            history.record_action(
                resume.resume_id,
                card.vacancy_id,
                "apply",
                "uncertain" if result.uncertain else ("success" if result.success else "failed"),
                result.reason,
                letter_variant=result.letter_variant,
            )
        elif args.dry_run and result.success:
            history.record_action(
                resume.resume_id,
                card.vacancy_id,
                "apply",
                "dry_run",
                result.reason,
                letter_variant=result.letter_variant,
            )

        if result.success:
            applied_count += 1
            print(f"  [OK] {card.title} — {card.company}")
        else:
            print(f"  [FAIL] {card.title} — {result.reason}")

        # #163: анти-бан-пауза — только после реальной отправки отклика (submit).
        # Ранние выходы не оставляют на сайте следа, пауза там не от чего не
        # защищает; после submit (включая неподтверждённый успех) — обязательна.
        if result.acted:
            throttle.wait(f"после отклика на '{card.title}'")

    print(f"Итого откликов за этот запуск: {applied_count}")
    return False

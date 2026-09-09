"""Сервис исполнения откликов: план, провайдеры, цикл по одному резюме.

Вынесено из ``commands/_common.py`` (#1045, участок 2 аудита #1035). Здесь
живёт прикладной цикл откликов — подготовка кандидатов (filter → rank → план)
и исполнение одной попытки с ЕДИНЫМ адаптером фиксации результата
(:func:`_finalize_result`); разбор аргументов CLI остался в
``commands/_common.py``, надзор (progress/lease/signal) — в
``commands/supervision.py``.

Параметры исполнения приходят явной структурой :class:`ApplyRunParams`, а не
произвольным ``argparse.Namespace``. Публичный
:func:`run_apply_for_resume` сохраняет прежний args-контракт (команда
``apply``, тесты) и конвертирует Namespace → ApplyRunParams на входе.

Переиспользуется существующая машинерия, второй pipeline не создаётся:
``apply_to_vacancy`` (apply/pipeline.py), ``build_apply_plan``,
``ApplyProgress``/``ApplyPlan``/``ApplyProviders``.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..apply import apply_to_vacancy
from ..apply.antibot import AntiBotChallengeDetected, raise_for_antibot
from ..apply.letter import CoverLetterProvider
from ..apply.verify import verify_response_in_negotiations
from ..blacklist import match as blacklist_match
from ..config import AppConfig, ResumeConfig, SearchFilters, is_resume_url_placeholder
from ..config_sections.scoring import ScoringWeights
from ..copy_resume import resolve_numeric_resume_ids
from ..history import SKIP_REASONS, History
from ..search import (
    _LLM_SHORTLIST_DEFAULT,
    VacancyCard,
    VacancySearchIndeterminate,
    _has_next_page,
    current_employer_hit,
    filter_candidates,
    rank_candidates,
    search_vacancies,
)
from ..throttle import LimitReached, Throttle
from .supervision import ApplyProgress

if TYPE_CHECKING:
    from ..ai.questions import AIQuestionAnswerer
    from ..scoring import LLMScoringProvider, ScoringProvider

logger = logging.getLogger("hhru_bot.cli")


class ApplyRunStopped(RuntimeError):
    """Terminal account-level condition requiring the whole apply run to stop."""


@dataclass(frozen=True)
class ApplyRunParams:
    """Явные параметры исполнения цикла откликов (вместо argparse.Namespace).

    Источник значений — одноимённые флаги CLI; :meth:`from_args` — единственное
    место, знающее про Namespace. ``getattr``-фолбэки прежнего кода
    нормализованы в поля с дефолтами: все флаги опциональны по своей природе
    (команда ``run`` не добавляет ``--approved``/``--permit``/``--force``).
    """

    dry_run: bool = False
    limit: int = 0
    max_pages: int | None = None
    force: bool = False
    approved: int | None = None
    permit: str | None = None
    learn_questionnaires: bool = False
    # Точечный отклик (#1085): вакансия задана явно, поэтому причины её
    # отсева фильтрами печатаются в консоль, а не только в debug-лог.
    vacancy_id: str | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> ApplyRunParams:
        return cls(
            dry_run=bool(getattr(args, "dry_run", False)),
            limit=getattr(args, "limit", 0) or 0,
            max_pages=getattr(args, "max_pages", None),
            force=bool(getattr(args, "force", False)),
            approved=getattr(args, "approved", None),
            permit=getattr(args, "permit", None),
            learn_questionnaires=bool(getattr(args, "learn_questionnaires", False)),
            vacancy_id=getattr(args, "vacancy_id", None),
        )


def _build_letter_provider(
    config: AppConfig,
    resume: ResumeConfig,
    cover_letter_template: str,
    history: History | None = None,
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
        feedback=history.list_feedback(resume_id=resume.resume_id, limit=25) if history else None,
    )


def _build_question_answerer(
    config: AppConfig,
    resume: ResumeConfig,
    known_data: dict[str, str] | None = None,
    *,
    history: History | None = None,
    learn: bool = False,
):
    """Собрать отвечающего на вопросы анкеты. None — анкеты не обрабатываются.

    Гейт двухуровневый (#482):
      * ``questionnaires.enabled`` включает resolver по обучаемым шаблонам,
        который обязан работать БЕЗ AI-зависимости;
      * ``ai.answer_questions`` добавляет к нему LLM-ступень.

    Отсутствие пакета ``.[ai]`` деградирует до resolver-only, а НЕ до None:
    ``pipeline._run`` пропускает вакансию с анкетой при ``question_answerer is
    None``, то есть возврат None здесь стоил бы всего keyword-пути — ровно того,
    что issue требует сохранить работоспособным без AI.
    """
    ai_config = getattr(config, "ai", None)
    questionnaires = getattr(config, "questionnaires", None)
    templates_enabled = bool(questionnaires is not None and questionnaires.enabled and history)
    llm_enabled = ai_config is not None and ai_config.answer_questions
    if not templates_enabled and not llm_enabled:
        return None

    client = fallback = None
    if llm_enabled:
        from ..ai.llm_client import LLMClient
        from ..ai.questions import AIQuestionAnswerer

        try:
            client = LLMClient(ai_config)
        except ImportError as exc:
            if not templates_enabled:
                logger.warning("LLM-ответы на вопросы недоступны: %s", exc)
                return None
            logger.warning(
                "LLM-ступень ответов на анкеты недоступна (%s) — остаются шаблоны без AI", exc
            )
        else:
            fallback = AIQuestionAnswerer(
                client, getattr(resume, "ai_profile", None), known_data=known_data
            )

    if not templates_enabled:
        return fallback

    from ..questionnaires.answerer import TemplateQuestionAnswerer

    return TemplateQuestionAnswerer(
        history,
        resume.resume_id,
        settings=questionnaires,
        llm=client,
        llm_fallback=fallback,
        learn=learn,
    )


def _build_scoring_provider(
    config: AppConfig,
    resume: ResumeConfig,
    history: History,
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
        feedback=history.list_feedback(resume_id=resume.resume_id, limit=25),
    )


@dataclass
class ApplyPlan:
    """План отклика по резюме: ранжированные кандидаты + статистика построения.

    Чистый результат build_apply_plan (#101) — без браузера, без побочных
    эффектов. ranked — тот же формат, что возвращает rank_candidates: полный
    список (карточка, score, разбивка факторов) после фильтрации и ранжирования.
    Лимит применяется execution-циклом после runtime skip/duplicate, поэтому
    план не может преждевременно лишить цикл кандидатов для дозаполнения.
    total/after_filter — счётчики стадий; after_limit — производное свойство
    (``len(ranked)``, т.к. ranked больше не срезается по лимиту), означает
    количество кандидатов, доступных execution-циклу; target_limit — его
    целевой размер. skipped — пары (карточка, причина) из filter_candidates.
    """

    ranked: list[tuple[VacancyCard, float, dict[str, float]]]
    skipped: list[tuple[VacancyCard, str]] = field(default_factory=list)
    total: int = 0
    after_filter: int = 0
    target_limit: int | None = None

    @property
    def after_limit(self) -> int:
        return len(self.ranked)


@dataclass(frozen=True)
class ApplyResumeIdentity:
    verify_resume_id: str
    account_resume_ids: set[str] | None


@dataclass(frozen=True)
class ApplyProviders:
    """AI providers built once per resume run, reused across lazy-paging waves.

    Each of scoring/letter/question providers lazily constructs its own
    ``LLMClient`` on first use (#17/#81) — rebuilding them per search page
    would mean up to 3 redundant LLMClient instances per extra page fetched.
    """

    scoring_provider: ScoringProvider | None
    letter_provider: CoverLetterProvider | None
    question_answerer: AIQuestionAnswerer | None


def _build_apply_providers(
    config: AppConfig,
    resume: ResumeConfig,
    cover_letter_template: str,
    history: History,
    *,
    learn: bool = False,
) -> ApplyProviders:
    return ApplyProviders(
        scoring_provider=_build_scoring_provider(config, resume, history),
        letter_provider=_build_letter_provider(config, resume, cover_letter_template, history),
        question_answerer=_build_question_answerer(
            config,
            resume,
            known_data=history.get_profile_answers(),
            history=history,
            learn=learn,
        ),
    )


def _prepare_apply_resume(page, resume: ResumeConfig, dry_run: bool) -> ApplyResumeIdentity | None:
    """Confirm resume identity before search and retain verifier attribution data."""
    identity = ApplyResumeIdentity(resume.resume_id, None)
    if dry_run:
        return identity
    ids_by_hash = resolve_numeric_resume_ids(page)
    raise_for_antibot(page)
    if ids_by_hash is None:
        return identity
    numeric_id = ids_by_hash.get(resume.resume_id)
    if numeric_id is None:
        logger.warning(
            "%s — резюме конфига (%s) нет в маппинге аккаунта: атрибуция в "
            "верификаторе уйдёт в fail-closed",
            resume.id,
            resume.resume_id,
        )
        return identity
    resume_status = getattr(ids_by_hash, "statuses", {}).get(resume.resume_id)
    if resume_status is None or resume_status == "not_finished":
        print(
            f"[FAIL] {resume.id} — статус резюме на hh.ru не подтверждён как "
            f"готовый к отклику (status={resume_status!r}); завершите/опубликуйте его вручную"
        )
        return None
    return ApplyResumeIdentity(numeric_id, set(ids_by_hash.values()))


_DEFAULT_UNLIMITED_PAGE_CAP = 5


def _apply_search_page_limit_core(max_pages: int | None, limit: int) -> int:
    """Return the safe cap for lazy apply search.

    An explicit --max-pages always wins.  With a positive --limit, estimate
    five successful responses per 20-card page and reserve one extra page for
    runtime skips. ``--limit 0`` ("без ограничения" per --help) has no
    success target to size a cap from — #441 round-2 review: this used to
    return 1, silently shrinking the previous unconditional 5-page default
    for the most common invocation (plain `apply`/`run` with no flags) down
    to a single page with no warning. Falls back to the prior fixed default
    instead.
    """
    if max_pages is not None:
        return max_pages
    return math.ceil(limit / 5) + 1 if limit else _DEFAULT_UNLIMITED_PAGE_CAP


def _load_apply_page(page, filters: SearchFilters, page_num: int) -> tuple[list[VacancyCard], bool]:
    """Load a page through the command-level seam used by existing tests."""
    kwargs: dict[str, int] = {"max_pages": 1}
    if page_num:
        kwargs["start_page"] = page_num
    cards = search_vacancies(page, filters, **kwargs)
    if not cards:
        return [], False
    if not hasattr(page, "locator"):
        # Lightweight command tests replace search_vacancies and use a plain
        # object instead of a Playwright Page.  A short fixture is terminal;
        # production always takes the confirmed DOM branch below. #441 review:
        # a bare `except AttributeError` here would also swallow a real DOM
        # bug inside `_has_next_page` on a genuine Page — fail-closed by
        # checking the test-double shape up front instead of catching broadly.
        return cards, len(cards) >= 20
    return cards, _has_next_page(page, page_num)


def build_apply_plan(
    candidates: list[VacancyCard],
    filters: SearchFilters,
    resume: ResumeConfig,
    history: History,
    scoring_provider: ScoringProvider | None = None,
    limit: int | None = None,
) -> ApplyPlan:
    """Строит план откликов: filter -> pre-LLM -> rank.

    Чистая функция (без браузера) — вынесена из run_apply_for_resume (#101),
    чтобы решения filter/rank/limit были тестируемы без hh.ru. candidates —
    СЫРЫЕ карточки из search_vacancies (ещё без применения exclude_employers/
    exclude_keywords/истории — это делает filter_candidates внутри, см.
    CLAUDE.md про разделение поиска и фильтрации).

    limit: None или 0 (CLI-конвенция args.limit по умолчанию 0 = флаг не задан)
    означают "без ограничения". Положительный limit сохраняется в
    ``target_limit`` для execution-цикла, но ranked намеренно не срезается:
    runtime skip/duplicate не должен занимать место успешного отклика.
    """
    scoring = getattr(resume, "scoring", None)
    prefilter = getattr(scoring, "prefilter", None)
    filtered, skipped = filter_candidates(
        candidates,
        filters,
        resume.resume_id,
        history,
        prefilter,
        getattr(scoring, "resume_match_threshold", None),
        getattr(resume, "ai_profile", None),
    )

    ranked = rank_candidates(
        filtered,
        filters,
        resume,
        scoring_provider=scoring_provider,
        llm_shortlist=_LLM_SHORTLIST_DEFAULT,
    )

    effective_limit = limit if limit else None

    return ApplyPlan(
        ranked=ranked,
        skipped=skipped,
        total=len(candidates),
        after_filter=len(filtered),
        target_limit=effective_limit,
    )


def _action_status(result) -> tuple[str, str]:  # noqa: ANN001 - shared structural protocol
    """Единое отображение ApplyResult в (status, reason_code) для actions.

    Было продублировано в ветке finalize_action и в ветке record_action
    (#1045): ``uncertain`` — submit мог уйти (#176), поэтому статус видит
    дедупликация has_applied; ``outcome_code`` из pipeline уточняет причину,
    но не статус.
    """
    status = "uncertain" if result.uncertain else ("success" if result.success else "failed")
    reason_code = getattr(result, "outcome_code", status)
    return status, reason_code


def _finish_approved_review(
    history: History,
    approved_id: int,
    *,
    result=None,  # noqa: ANN001 - shared structural result protocol
    exc: BaseException | None = None,
    action_reserved: bool,
) -> None:
    """Единый адаптер «исход попытки → состояние review-очереди» (#1045).

    Раньше отображение было повторено в трёх ветках (post-result, anti-bot
    exception, прочее exception). #436: у finish_review нет 'uncertain', а
    review_queue — reporting view, не барьер дедупликации (барьер —
    has_applied() по actions, который уже считает uncertain как success).
    Поэтому fail-closed: uncertain и успех маппятся в 'applied' — повторный
    review этой записи никогда не выглядит безопасно повторяемым.
    Исключение: 'applied' честно только если попытка дошла до резервирования
    actions-строки (before_submit); крах до него ничего не пытался
    отправить — 'failed'.
    """
    if exc is not None:
        state = "applied" if action_reserved else "failed"
    else:
        state = "applied" if (result.success or result.uncertain) else "failed"
    history.finish_review(approved_id, state)


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
    progress: ApplyProgress | None = None,
    show_summary: bool = True,
) -> bool:
    """Args-контракт команды apply/run: конвертация в ApplyRunParams и запуск.

    Скрытая проверка плейсхолдера/лимита до первого поиска и lazy-paging
    волны — в :func:`execute_apply_for_resume`.
    """
    return execute_apply_for_resume(
        page,
        config,
        resume,
        history,
        throttle,
        ApplyRunParams.from_args(args),
        cards_override,
        skip_scoring,
        ranked_override,
        progress,
        show_summary,
    )


def execute_apply_for_resume(
    page,
    config: AppConfig,
    resume: ResumeConfig,
    history: History,
    throttle: Throttle,
    params: ApplyRunParams,
    cards_override=None,
    skip_scoring: bool = False,
    ranked_override=None,
    progress: ApplyProgress | None = None,
    show_summary: bool = True,
) -> bool:
    """Run one resume lazily, fetching a next page only after a shortfall."""
    if cards_override is not None or params.approved is not None:
        return _execute_apply_wave(
            page,
            config,
            resume,
            history,
            throttle,
            params,
            cards_override,
            skip_scoring,
            ranked_override,
            progress,
            show_summary,
        )

    # Плейсхолдер resume_url и дневной лимит — те же guard'ы, что и внутри
    # _execute_apply_wave (единственный источник истины для #165/#216 и для
    # текста ошибок); здесь проверяются ДО первого поиска, чтобы лишний page
    # load не уходил на hh.ru прежде, чем стало ясно, что резюме нельзя
    # использовать вовсе. identity общая для всех волн этого резюме — резолвится
    # один раз здесь и передаётся вниз, а не пересчитывается на каждую страницу.
    if is_resume_url_placeholder(resume.resume_url):
        print(
            f"[FAIL] {resume.id} — в конфиге указан плейсхолдер resume_url; "
            "укажите реальный URL (получить можно через list-resumes)"
        )
        return True
    try:
        throttle.check_apply_limit(resume.resume_id, params.dry_run)
    except LimitReached as e:
        print(f"Пропуск: {e}")
        return False
    identity = _prepare_apply_resume(page, resume, params.dry_run)
    if identity is None:
        return True
    # Built once per resume run, not per search page (#101 execution-only loop
    # below reuses the same providers across all lazy-paging waves).
    cover_letter_template = config.cover_letter_for(resume)
    providers = (
        None
        if skip_scoring
        else _build_apply_providers(
            config,
            resume,
            cover_letter_template,
            history,
            learn=params.learn_questionnaires,
        )
    )

    progress = progress or ApplyProgress()
    failed = False
    page_limit = _apply_search_page_limit_core(params.max_pages, params.limit)
    target_limit = params.limit or None
    has_next = False
    daily_limit_exhausted = False
    for page_num in range(page_limit):
        # #441 round-3 review: LimitReached can also fire INSIDE a wave (per
        # card, _execute_apply_wave's own loop) — that function just
        # returns False, so without this explicit re-check here the lazy-
        # paging loop couldn't tell "daily limit hit mid-wave" from "wave
        # finished normally, load more" and kept issuing live hh.ru page
        # loads after the account-wide budget was already exhausted, which
        # violates the anti-fraud throttling principle in CLAUDE.md.
        try:
            throttle.check_apply_limit(resume.resume_id, params.dry_run)
        except LimitReached as e:
            print(f"Пропуск: {e}")
            daily_limit_exhausted = True
            break
        try:
            cards, has_next = _load_apply_page(page, resume.search, page_num)
        except VacancySearchIndeterminate as e:
            raise_for_antibot(page)
            print(f"[FAIL] {e}")
            return True
        raise_for_antibot(page)
        if not cards:
            has_next = False
            break
        failed = (
            _execute_apply_wave(
                page,
                config,
                resume,
                history,
                throttle,
                params,
                cards,
                False,
                None,
                progress,
                False,
                identity,
                providers,
            )
            or failed
        )
        if progress.reached(target_limit) or not has_next:
            break
    if (
        target_limit is not None
        and not progress.reached(target_limit)
        and has_next
        and not daily_limit_exhausted
    ):
        # page_limit (auto-cap ceil(limit/5)+1 или явный --max-pages)
        # оборвал поиск раньше, чем цель была достигнута, хотя дальше по
        # выдаче ещё есть страницы — недобор неотличим от "выдача
        # кончилась" без явного сигнала, отсюда предупреждение. Если
        # реальная причина остановки — дневной лимит (daily_limit_exhausted),
        # это НЕ проблема page cap и не решается --max-pages — сообщение
        # про печатанное выше "Пропуск: Достигнут дневной лимит..." уже
        # объясняет причину, не дублируем/не путаем её этим предупреждением.
        print(
            f"[INFO] Достигнут потолок в {page_limit} страниц(ы) поиска, "
            f"цель ({target_limit}) не достигнута — попробуйте явный "
            "--max-pages с большим значением"
        )
    if show_summary:
        print(f"Итого откликов за этот запуск: {progress.applied_count}")
    return failed


def _execute_apply_wave(
    page,
    config: AppConfig,
    resume: ResumeConfig,
    history: History,
    throttle: Throttle,
    params: ApplyRunParams,
    cards_override=None,
    skip_scoring: bool = False,
    ranked_override=None,
    progress: ApplyProgress | None = None,
    show_summary: bool = True,
    identity: ApplyResumeIdentity | None = None,
    providers: ApplyProviders | None = None,
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
        throttle.check_apply_limit(resume.resume_id, params.dry_run)
    except LimitReached as e:
        print(f"Пропуск: {e}")
        return False

    identity = identity or _prepare_apply_resume(page, resume, params.dry_run)
    if identity is None:
        return True
    verify_resume_id = identity.verify_resume_id
    account_resume_ids = identity.account_resume_ids

    approved_item = None
    approved_duplicate = False
    if params.approved is not None:
        if not params.permit:
            print("[FAIL] для --approved требуется --permit из review approve")
            return True
        candidates = [item for item in history.review_items() if item["id"] == params.approved]
        if not candidates or candidates[0]["resume_id"] != resume.resume_id:
            print("[FAIL] approved-запись принадлежит другому резюме")
            return True
        approved_item = history.claim_review(params.approved, params.permit)
        cards = [
            VacancyCard(
                vacancy_id=approved_item["vacancy_id"],
                title=approved_item["title"],
                company=approved_item["company"],
                url=approved_item["vacancy_url"],
            )
        ]
        # --approved bypasses the normal search/filter plan, so preserve the
        # history-based deduplication barrier on this explicit route too.
        if history.has_applied(resume.resume_id, approved_item["vacancy_id"]):
            history.finish_review(params.approved, "skipped")
            approved_duplicate = True
        elif (blacklist_reason := blacklist_match(cards[0], history.blacklist_sets())) is not None:
            history.finish_review(params.approved, "skipped")
            history.record_skip(
                resume.resume_id, approved_item["vacancy_id"], SKIP_REASONS.BLACKLIST
            )
            print(f"[skip] {blacklist_reason} — отклик не отправлен")
            approved_duplicate = True
        elif (
            current_employer_hit(approved_item["company"], resume.search.current_employers)
            is not None
        ):
            # #524 safety-гейт действует и на явном пути --approved: запись очереди
            # могла быть одобрена до настройки account.current_employer или до смены
            # работодателя, а отклик текущему работодателю необратим. Тот же guard,
            # что в filter_candidates (current_employer_hit), поэтому поведение
            # едино; вакансия резолвится как skipped, отклик не отправляется.
            history.finish_review(params.approved, "skipped")
            history.record_skip(
                resume.resume_id, approved_item["vacancy_id"], SKIP_REASONS.CURRENT_EMPLOYER
            )
            print("[skip] вакансия текущего работодателя — отклик не отправлен")
            approved_duplicate = True
    elif cards_override is None:
        try:
            cards = search_vacancies(page, resume.search, max_pages=params.max_pages)
        except VacancySearchIndeterminate as e:
            # Search timeouts are normally per-resume failures, but a confirmed
            # challenge must escape as the terminal AntiBotChallengeDetected state.
            raise_for_antibot(page)
            print(f"[FAIL] {e}")
            return True
        raise_for_antibot(page)
    else:
        cards = cards_override
    scoring_provider = (
        None
        if approved_item or skip_scoring
        else (
            providers.scoring_provider
            if providers is not None
            else _build_scoring_provider(config, resume, history)
        )
    )
    plan = (
        ApplyPlan([], skipped=[], total=len(cards), after_filter=0)
        if approved_duplicate
        else ApplyPlan([(cards[0], approved_item["score"], json.loads(approved_item["breakdown"]))])
        if approved_item
        else build_apply_plan(
            cards,
            resume.search,
            resume,
            history,
            scoring_provider=scoring_provider,
            limit=params.limit,
        )
    )
    if ranked_override is not None:
        allowed = {card.vacancy_id for card, _score, _breakdown in plan.ranked}
        routed_ranked = [item for item in ranked_override if item[0].vacancy_id in allowed]
        plan = ApplyPlan(
            ranked=routed_ranked,
            skipped=plan.skipped,
            total=plan.total,
            after_filter=plan.after_filter,
            target_limit=plan.target_limit,
        )

    for card, reason in plan.skipped:
        # #1089 review: для точечного отклика (--vacancy-id) причина отсева
        # печатается в консоль — юзер явно назвал вакансию, молчаливый
        # «Итого откликов: 0» без объяснения заметнее, чем в поисковой выдаче.
        if params.vacancy_id:
            print(f"  [skip] {card.title} — {reason}")
        else:
            logger.debug("Пропуск вакансии %s: %s", card.title, reason)

    cover_letter_template = config.cover_letter_for(resume)
    if providers is not None:
        letter_provider = providers.letter_provider
        question_answerer = providers.question_answerer
    else:
        letter_provider = _build_letter_provider(config, resume, cover_letter_template, history)
        question_answerer = _build_question_answerer(
            config,
            resume,
            history.get_profile_answers(),
            history=history,
            learn=params.learn_questionnaires,
        )
    if approved_item:
        from ..apply.letter import LetterOutcome

        class _ApprovedLetter:
            def render(self, vacancy, resume_profile=None):  # noqa: ANN001, ARG002
                return LetterOutcome(approved_item["letter"], "approved")

        letter_provider = _ApprovedLetter()
    # M6 cycle-review #373: no longer blocks the whole run when
    # ai.answer_questions=true but --force is missing — pipeline.py gates
    # --force per vacancy, only once a questionnaire is actually detected, so
    # an ordinary apply against vacancies without questions still works. This
    # is only an upfront heads-up for the --force case; the real fail-closed
    # enforcement lives in pipeline.py.
    if question_answerer is not None and not params.dry_run and params.force:
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
            page,
            vacancy_id,
            verify_resume_id,
            account_resume_ids=account_resume_ids,
            run_id=progress.run_id if progress is not None else None,
        )

    applied_count = progress.applied_count if progress is not None else 0
    for card, _score, _breakdown in plan.ranked:
        if plan.target_limit is not None and applied_count >= plan.target_limit:
            break
        try:
            throttle.check_apply_limit(resume.resume_id, params.dry_run)
        except LimitReached as e:
            print(f"Дневной лимит достигнут, останавливаюсь: {e}")
            if approved_item:
                history.finish_review(params.approved, "skipped")
            break

        action_id = None
        effective_letter_provider = letter_provider
        if params.dry_run:
            from ..apply.letter import LetterOutcome, TemplateCoverLetterProvider

            snapshot_source = letter_provider or TemplateCoverLetterProvider(cover_letter_template)
            snapshot_outcome = snapshot_source.render(card)
            snapshot_letter = snapshot_outcome.text
            history.enqueue_review(
                resume.resume_id,
                card,
                _score,
                _breakdown,
                snapshot_letter,
                search_query=resume.search.text,
            )

            class _DryRunLetter:
                def render(
                    self,
                    vacancy,
                    resume_profile=None,
                    letter=snapshot_letter,
                    variant=snapshot_outcome.variant,
                ):  # noqa: ANN001, ARG002
                    return LetterOutcome(letter, variant)

            effective_letter_provider = _DryRunLetter()

        def _before_submit(vacancy_id: str = card.vacancy_id) -> None:
            nonlocal action_id
            # #245: commit the fail-closed audit marker immediately before
            # entering the irreversible form path. A process crash can leave
            # browser dumps (and possibly a sent application) without
            # returning an ApplyResult; waiting for the post-action record
            # would make the next run send a duplicate. Keeping this hook after
            # navigation/questions preserves the old no-action semantics for
            # confirmed pre-submit exits.
            # #420 follow-up (Codex adversarial-review round 1+2, PR #449): the
            # config's resume.search.text can have changed between the dry-run
            # that queued an --approved card and this run — attributing to the
            # *current* query would silently mislabel the funnel. Use the query
            # review_queue recorded at enqueue time instead; a pre-fix queue row
            # has none stored (NULL), which stays genuinely unattributed rather
            # than falling back to an unrelated vacancies_seen query (round 2).
            action_id = history.begin_action(
                resume.resume_id,
                vacancy_id,
                "apply",
                search_query=(
                    approved_item["search_query"] if approved_item else resume.search.text
                ),
                run_id=progress.run_id if progress is not None else None,
            )

        # dict value type intentionally broad — **apply_kwargs spreads several
        # unrelated kwarg types (provider/verifier/callable/bool) into
        # apply_to_vacancy's distinct typed parameters below.
        apply_kwargs: dict[str, Any] = {
            "letter_provider": effective_letter_provider,
        }
        if resume.search.allow_relocation:
            apply_kwargs["allow_relocation"] = True
        if not params.dry_run:
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
            apply_kwargs["force"] = params.force
            # #473: the questionnaire audit is append-only and linked to the
            # command ledger/action outcome through this run id.
            apply_kwargs["questionnaire_history"] = history
        # Every apply producer, not only questionnaire auditing, must be
        # attributable to this command run for offline incident correlation.
        if progress is not None and progress.run_id is not None:
            apply_kwargs["run_id"] = progress.run_id
        if progress is not None:
            progress.begin_attempt()
        try:
            threshold = getattr(getattr(resume, "scoring", None), "letter_match_threshold", None)
            if threshold is not None and approved_item is None:
                apply_kwargs["letter_match_threshold"] = threshold
            result = apply_to_vacancy(
                page,
                card,
                resume.resume_id,
                cover_letter_template,
                params.dry_run,
                **apply_kwargs,
            )
        except AntiBotChallengeDetected as exc:
            # A post-submit challenge can arrive after before_submit reserved
            # the row. Keep its dedup/limit-safe uncertain status, but replace
            # the generic crash reason before terminating the whole command.
            # #436/#1045: состояние review-очереди выставляет единый адаптер
            # _finish_approved_review — тот же fail-closed маппинг, что и в
            # ветке прочих исключений ниже.
            if action_id is not None:
                history.finalize_action(action_id, "uncertain", str(exc), reason_code="uncertain")
            if approved_item:
                _finish_approved_review(
                    history, params.approved, exc=exc, action_reserved=action_id is not None
                )
            raise
        except BaseException as exc:
            # A claimed review must never remain permanently in ``applying``
            # when the browser/pipeline fails before returning a result.
            if approved_item:
                # #436: action_id may be None (crash before before_submit) —
                # then nothing was attempted and 'failed' is correct. If
                # action_id is set, begin_action() already reserved the
                # actions row as 'uncertain' and it was never finalized here,
                # so the same fail-closed mapping applies as above.
                _finish_approved_review(
                    history, params.approved, exc=exc, action_reserved=action_id is not None
                )
            raise

        if result.skipped:
            if progress is not None:
                progress.finish(result)
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
            if approved_item:
                history.finish_review(params.approved, "skipped")
            if action_id is not None:
                history.finalize_action(
                    action_id,
                    "failed",
                    result.reason,
                    reason_code=getattr(result, "outcome_code", "skipped"),
                )
            print(f"  [skip] {card.title} — {result.reason}")
            # #342: сегодня терминальные блокеры приходят через ctx.stop()
            # (skipped=False) и до сюда не доходят. Проверка стоит и здесь,
            # чтобы будущий skip-путь с stop_run не проглотился этим continue.
            if getattr(result, "stop_run", False):
                print(f"  [STOP] {card.title} — {result.reason}")
                raise ApplyRunStopped(result.reason)
            continue

        # #163: actions — журнал реальных взаимодействий с hh.ru. Запись только
        # после реального submit. Dry-run ничего не отправляет и не создаёт
        # action-строку, поэтому не блокирует последующий отклик.
        # Провалы до submit (форма входа, «уже откликались», кнопка не найдена)
        # на hh.ru не отправлялись — остаются в консоли/логе, не в статистике.
        if action_id is not None:
            # #176: uncertain — submit мог уйти, но результат неизвестен. Такой
            # статус видит дедупликация has_applied (повторный запуск не
            # откликнется на ту же вакансию вторым письмом) — «просто failed»
            # не годится. dry_run по определению без клика — uncertain там
            # невозможен.
            status, reason_code = _action_status(result)
            history.finalize_action(
                action_id,
                status,
                result.reason,
                letter_variant=result.letter_variant,
                reason_code=reason_code,
            )
        elif result.acted:
            # The verifier can positively reconcile an external submit even
            # when the pre-submit hook was not reached (for example, a
            # transitional page exposed the post-click state directly).
            # #420 follow-up: same approved-item provenance handling as
            # _before_submit above — use the query review_queue recorded at
            # enqueue time, not the current config's.
            status, reason_code = _action_status(result)
            history.record_action(
                resume.resume_id,
                card.vacancy_id,
                "apply",
                status,
                result.reason,
                letter_variant=result.letter_variant,
                search_query=(
                    approved_item["search_query"] if approved_item else resume.search.text
                ),
                run_id=progress.run_id if progress is not None else None,
                reason_code=reason_code,
            )
        if progress is not None:
            progress.finish(result)
            applied_count = progress.applied_count
        elif result.success:
            applied_count += 1
        if result.success:
            print(f"  [OK] {card.title} — {card.company}")
        else:
            print(f"  [FAIL] {card.title} — {result.reason}")

        if approved_item:
            # #436/#1045: единый адаптер — 'uncertain' дедуплицируется как
            # 'applied', а не читается как безопасный повтор.
            _finish_approved_review(
                history, params.approved, result=result, action_reserved=action_id is not None
            )

        # #163: анти-бан-пауза — только после реальной отправки отклика (submit).
        # Ранние выходы не оставляют на сайте следа, пауза там не от чего не
        # защищает; после submit (включая неподтверждённый успех) — обязательна.
        if result.acted:
            throttle.wait(f"после отклика на '{card.title}'")

        # #342: остановка прогона выполняется ПОСЛЕДНЕЙ. Терминальный лимит
        # аккаунта может сопровождаться реально ушедшим откликом (внешняя
        # проверка #207 вернула found), поэтому сначала учитываем его в
        # счётчике и выдерживаем анти-бан-паузу, и только потом прерываем
        # прогон — иначе после submit паузы не будет вовсе.
        # #441 round-2 review: a per-vacancy `uncertain` result must NOT
        # abort the whole run via ApplyRunStopped — that class is reserved
        # for genuine account-level terminal conditions (its own docstring).
        # `uncertain` is also set on routine post-click fail paths (project
        # memory hhru-uncertain-counter-overcounts / #176), not only
        # genuine grey-zone ambiguity, and dedup via has_applied() already
        # keeps it from being retried/counted — no extra abort needed here.
        # Out of scope for this PR; the "[FAIL]"+"[STOP]" double-print this
        # block also caused is the known finding noted in the PR body.
        if getattr(result, "stop_run", False):
            print(f"  [STOP] {card.title} — {result.reason}")
            raise ApplyRunStopped(result.reason)

    if show_summary:
        print(f"Итого откликов за этот запуск: {applied_count}")
    return False

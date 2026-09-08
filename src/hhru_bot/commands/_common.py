"""Общий код команд CLI: разбор резюме, общие аргументы, контекст запуска.

После разделения #1045 (участок 2 аудита #1035) здесь живут только
CLI-обязанности: парсер-аргументы команд, резолв ``--resume`` и тонкие
ре-экспорты для обратной совместимости импортов. Прикладной цикл откликов —
``commands/apply_service.py``; надзор исполнения (progress, durable lease,
SIGTERM, DurableMutationAttempt) — ``commands/supervision.py``. Новому коду
импортировать из этих модулей напрямую; ре-экспорты здесь — мост для
существующих команд и тестов.
"""

from __future__ import annotations

import argparse
import re

from ..config import AppConfig, ResumeConfig

# --- Ре-экспорты #1045: цикл откликов и надзор переехали из этого модуля. ---
from .apply_service import (  # noqa: F401
    ApplyPlan,
    ApplyProviders,
    ApplyResumeIdentity,
    ApplyRunParams,
    ApplyRunStopped,
    _apply_search_page_limit_core,
    _build_apply_providers,
    _build_letter_provider,
    _build_question_answerer,
    _build_scoring_provider,
    _execute_apply_wave,
    _prepare_apply_resume,
    build_apply_plan,
    execute_apply_for_resume,
    run_apply_for_resume,
)
from .supervision import (  # noqa: F401
    ApplyProgress,
    DurableMutationAttempt,
    MutationOutcome,
    SignalTermination,
    run_single_mutation_command,
    run_supervised_command,
)

__all__ = [
    "ApplyPlan",
    "ApplyProgress",
    "ApplyProviders",
    "ApplyResumeIdentity",
    "ApplyRunParams",
    "ApplyRunStopped",
    "DurableMutationAttempt",
    "MutationOutcome",
    "SignalTermination",
    "add_common_args",
    "add_force_arg",
    "add_learn_questionnaires_arg",
    "add_limit_arg",
    "apply_search_page_limit",
    "build_apply_plan",
    "execute_apply_for_resume",
    "resolve_resume",
    "resolve_resumes",
    "resumes_from_args",
    "run_apply_for_resume",
    "run_single_mutation_command",
    "run_supervised_command",
]

# Историческое имя функции волны исполнения; новое — _execute_apply_wave.
_run_apply_for_resume = _execute_apply_wave  # noqa: F841 - обратная совместимость импортов


def apply_search_page_limit(args: argparse.Namespace) -> int:
    """Безопасный кап страниц поиска из флагов CLI; ядро — в apply_service."""
    params = ApplyRunParams.from_args(args)
    return _apply_search_page_limit_core(params.max_pages, params.limit)


def _positive_page_count(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("--max-pages должен быть не меньше 1")
    return parsed


def add_common_args(p: argparse.ArgumentParser, *, max_pages_default: int | None = 5) -> None:
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
    p.add_argument(
        "--max-pages",
        type=_positive_page_count,
        default=max_pages_default,
        help=(
            "Максимум страниц поиска"
            if max_pages_default is not None
            else "Явный максимум страниц поиска (по умолчанию — адаптивный)"
        ),
    )


def _nonnegative_limit(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("--limit не может быть отрицательным")
    return parsed


def add_limit_arg(p: argparse.ArgumentParser) -> None:
    """``--limit`` — только для apply/run (то же обоснование, что add_force_arg).

    #441 review: голый ``type=int`` пропускал отрицательные значения —
    ``--limit -1`` делал target_limit=-1, applied_count>=-1 истинно сразу
    же, запуск тихо завершался без единого отклика и без явной ошибки.
    """
    p.add_argument(
        "--limit",
        type=_nonnegative_limit,
        default=0,
        help=(
            "Целевое число успешных откликов за запуск (0 = без ограничения кроме дневного лимита)"
        ),
    )


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


def add_learn_questionnaires_arg(p: argparse.ArgumentParser) -> None:
    """``--learn-questionnaires`` — разрешение СПРАШИВАТЬ, а не отправлять (#482).

    Отдельный флаг, а не переиспользование ``--force``: тот авторизует боевую
    отправку отклика, и если бы обучение шло под ним, ``apply --force`` молча
    закреплял бы догадки модели как подтверждённые пользователем сопоставления.
    Без этого флага неизвестный вопрос сразу уходит в очередь — прогон не
    останавливается на stdin (важно для headless/cron).
    """
    p.add_argument(
        "--learn-questionnaires",
        action="store_true",
        help="Спрашивать подтверждение сопоставления вопроса анкеты с шаблоном",
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

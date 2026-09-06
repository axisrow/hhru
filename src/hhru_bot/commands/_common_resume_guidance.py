"""Actionable guidance for the resume's uncovered common screen."""

from __future__ import annotations

import shlex

# Поля экрана common (имена hh.ru). workTicket здесь НЕТ: на common-визарде
# черновика это поле не рендерится (контрол work-ticket-selector — это
# «Разрешение на работу», #997), и советовать его — ложная улика.
COMMON_RESUME_FIELDS = (
    "birthday",
    "area",
    "firstName",
    "lastName",
    "gender",
    "phone",
    "citizenship",
    "relocation",
    "metro",
    "schedule",
    "employment",
    "work_format",
    "businessTrip",
)


def print_common_resume_guidance(resume, *, include_publish: bool = False) -> None:
    """Explain why publication is blocked and provide a safe next step.

    Аудит честности CLI (#1002): единственный советуемый путь — команда
    ``hhru common``. Прежний текст утверждал «CLI пока не умеет сохранять
    общие данные» — это было неверно и уводило агента в ручной режим.
    """
    resume_arg = shlex.quote(str(resume.id))
    fields = ", ".join(COMMON_RESUME_FIELDS)
    print(f"[INFO] Экран common не заполнен; поля экрана: {fields}.")
    print("[NEXT] 1. Заполните и сохраните экран common командой hhru common")
    print("  (список флагов: hhru common --help):")
    print(f"  hhru common --resume {resume_arg} --first-name …")
    if include_publish:
        print("[NEXT] 2. После сохранения снова проверьте публикацию без клика:")
        print(f"  hhru publish-resume --resume {resume_arg} --dry-run")

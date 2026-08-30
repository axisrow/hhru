"""Actionable guidance for the resume's uncovered common screen."""

from __future__ import annotations

import shlex

COMMON_RESUME_FIELDS = (
    "birthday",
    "area",
    "workTicket",
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
    """Explain why publication is blocked and provide a safe next step."""
    resume_arg = shlex.quote(str(resume.id))
    fields = ", ".join(COMMON_RESUME_FIELDS)
    print(f"[INFO] Экран common не заполнен; проверьте поля: {fields}.")
    print(
        "[INFO] CLI пока не умеет сохранять общие данные резюме "
        "(ФИО, дату рождения, город, контакты и связанные параметры)."
    )
    print("[NEXT] 1. Откройте резюме на hh.ru и заполните экран «Основное» вручную.")
    print("[NEXT] 2. Сохраните форму и убедитесь, что обязательные поля больше не отмечены.")
    if include_publish:
        print("[NEXT] 3. После сохранения снова проверьте публикацию без клика:")
        print(f"  hhru publish-resume --resume {resume_arg} --dry-run")

"""Actionable CLI guidance for an unfinished professional-role wizard."""

from __future__ import annotations

import shlex


def print_professional_role_guidance(resume, *, include_publish: bool = False) -> None:
    search_text = getattr(getattr(resume, "search", None), "text", "") or ""
    if search_text:
        print(
            f"[INFO] resume.search.text={search_text!r} — это запрос вакансий; "
            "он не является должностью или профессией каталога hh.ru."
        )
    else:
        print(
            "[INFO] Поисковый запрос вакансий нельзя использовать как должность "
            "или профессию каталога hh.ru."
        )
    resume_arg = shlex.quote(str(resume.id))
    print("[NEXT] 1. При необходимости обновите локальный каталог:")
    print("  hhru professional-roles --refresh")
    print("[NEXT] 2. Найдите кандидатов по короткому русскому названию профессии:")
    print('  hhru professional-roles --query "<например: разработчик>"')
    print("[NEXT] 3. Проверьте заполнение без записи на hh.ru:")
    print(
        f"  hhru resume-position --resume {resume_arg} "
        '--title "<желаемая должность>" '
        '--specialization "<точная профессия из таблицы>" --dry-run'
    )
    if include_publish:
        print("[NEXT] 4. После подтверждённого сохранения снова проверьте публикацию:")
        print(f"  hhru publish-resume --resume {resume_arg} --dry-run")

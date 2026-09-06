"""[NEXT]-советы для wizard-экранов educations/keyskills/experience (#1010).

У этих экранов нет другого CLI-владельца: единственный путь —
``hhru wizard-next``. Печатается ровно то, что выполнимо: флаг
``--allow-auto-publish`` обязателен, потому что NEXT на последнем незакрытом
экране hh.ru публикует резюме сам (#900, живой факт 2026-09-06).
"""

from __future__ import annotations

import shlex

# Экраны, которыми владеет hhru wizard-next (resume_wizard.SUPPORTED_SCREENS).
WIZARD_NEXT_SCREENS = ("educations", "keyskills", "experience")


def print_wizard_next_guidance(resume, screen: str | None = None) -> None:
    """Совет «как закрыть wizard-экран» для отказавшей команды."""
    target = f" --screen {screen}" if screen else ""
    quoted = shlex.quote(str(resume.id))
    print(
        "[INFO] Незавершённый экран визарда черновика сабмитится командой "
        "hhru wizard-next (--allow-auto-publish обязателен: последний экран "
        "hh.ru публикует сам, #900)."
    )
    print(
        f"[NEXT] 1. Подтвердите экран визарда: hhru wizard-next --resume "
        f"{quoted}{target} --allow-auto-publish --force"
    )
    print(f"[NEXT] 2. Состояние без клика: hhru publish-resume --resume {quoted} --dry-run")

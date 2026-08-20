"""Deterministic skill-frequency aggregation for collected vacancies."""

from __future__ import annotations

import re
from collections import Counter

SKILLS = (
    "python",
    "java",
    "javascript",
    "typescript",
    "go",
    "c#",
    "c++",
    "sql",
    "postgresql",
    "mysql",
    "redis",
    "kafka",
    "docker",
    "kubernetes",
    "linux",
    "aws",
    "azure",
    "gcp",
    "terraform",
    "ansible",
    "git",
    "graphql",
    "rest",
    "django",
    "fastapi",
    "flask",
    "react",
    "vue",
    "angular",
    "pandas",
    "spark",
    "airflow",
    "gitlab ci",
    "ci/cd",
    "machine learning",
    "английский язык",
)


def aggregate_skill_gaps(
    texts: list[str], current_skills: list[str] | tuple[str, ...] = (), limit: int = 20
) -> list[dict[str, str | int]]:
    """Count each skill once per vacancy and omit skills in the current resume."""
    present = {skill.casefold().strip() for skill in current_skills}
    counts: Counter[str] = Counter()
    for text in texts:
        lowered = text.casefold()
        for skill in SKILLS:
            if skill.casefold() not in present and re.search(
                rf"(?<![\w+#]){re.escape(skill.casefold())}(?![\w+#])", lowered
            ):
                counts[skill] += 1
    return [{"skill": skill, "vacancies": count} for skill, count in counts.most_common(limit)]

#!/usr/bin/env python3
"""Generate a complete, validated map of employer-questionnaire questions."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

CLUSTERS = {
    "conditions": "1. Условия сотрудничества",
    "motivation": "2. Мотивация и карьерный контекст",
    "expertise": "3. Профессиональная экспертиза",
    "assessment": "4. Проверка практических навыков и знаний",
    "marketing": "5. Продуктовый и маркетинговый подход",
    "portfolio": "6. Опыт, достижения и портфолио",
    "fit": "7. Совместимость с форматом работы",
    "compliance": "8. Формальные подтверждения и комплаенс",
    "mixed": "9. Смешанные вопросы",
}

# Every database question has one primary employer goal.  Questions with two
# equally material goals belong to ``mixed`` rather than being duplicated.
QUESTION_CLUSTERS = {
    1: "motivation",
    2: "expertise",
    3: "conditions",
    4: "conditions",
    5: "portfolio",
    6: "expertise",
    7: "expertise",
    8: "conditions",
    9: "compliance",
    10: "mixed",
    11: "expertise",
    12: "expertise",
    13: "marketing",
    14: "marketing",
    15: "marketing",
    16: "expertise",
    17: "marketing",
    18: "marketing",
    19: "marketing",
    20: "marketing",
    21: "marketing",
    22: "motivation",
    23: "expertise",
    24: "conditions",
    25: "conditions",
    26: "fit",
    27: "fit",
    28: "conditions",
    29: "fit",
    30: "fit",
    31: "fit",
    32: "expertise",
    33: "conditions",
    34: "conditions",
    35: "marketing",
    36: "marketing",
    37: "marketing",
    38: "motivation",
    39: "portfolio",
    40: "portfolio",
    41: "portfolio",
    42: "marketing",
    43: "expertise",
    44: "marketing",
    45: "marketing",
    46: "conditions",
    47: "conditions",
    48: "mixed",
    49: "conditions",
    50: "portfolio",
    51: "expertise",
    52: "expertise",
    53: "expertise",
    54: "expertise",
    55: "conditions",
    56: "conditions",
    57: "expertise",
    58: "conditions",
    59: "motivation",
    60: "conditions",
    61: "compliance",
    62: "compliance",
    63: "conditions",
    64: "conditions",
    65: "motivation",
    66: "assessment",
    67: "assessment",
    68: "assessment",
    69: "assessment",
    70: "assessment",
    71: "motivation",
    72: "expertise",
    73: "conditions",
    74: "conditions",
    75: "portfolio",
    76: "expertise",
    77: "expertise",
    78: "expertise",
    79: "fit",
    80: "portfolio",
    81: "fit",
    82: "portfolio",
    83: "conditions",
    84: "assessment",
    85: "assessment",
    86: "assessment",
    87: "assessment",
    88: "assessment",
    89: "assessment",
    90: "assessment",
    91: "assessment",
    92: "assessment",
    93: "assessment",
    94: "compliance",
    95: "compliance",
    96: "conditions",
    97: "expertise",
    98: "expertise",
    99: "expertise",
    100: "expertise",
    101: "expertise",
    102: "expertise",
    103: "expertise",
    104: "expertise",
    105: "expertise",
    106: "expertise",
    107: "conditions",
    108: "conditions",
    109: "compliance",
    110: "expertise",
    111: "conditions",
    112: "conditions",
    113: "compliance",
    114: "conditions",
    115: "conditions",
    116: "compliance",
    117: "conditions",
}

# Semantic templates deliberately consolidate differently worded repeats.  A
# question absent from this map remains its own template, preserving nuance.
TEMPLATES = {
    1: ("desired_role", "Желаемая роль, функционал и задачи"),
    3: ("salary", "Зарплатные ожидания"),
    4: ("location", "Город / страна проживания"),
    8: ("salary", "Зарплатные ожидания"),
    22: ("desired_role", "Желаемая роль, функционал и задачи"),
    24: ("salary", "Зарплатные ожидания"),
    25: ("location", "Город / страна проживания"),
    28: ("salary", "Зарплатные ожидания"),
    33: ("salary", "Зарплатные ожидания"),
    34: ("salary", "Зарплатные ожидания"),
    46: ("salary", "Зарплатные ожидания"),
    49: ("salary", "Зарплатные ожидания"),
    55: ("salary", "Зарплатные ожидания"),
    58: ("salary", "Зарплатные ожидания"),
    60: ("salary", "Зарплатные ожидания"),
    63: ("salary", "Зарплатные ожидания"),
    64: ("salary", "Зарплатные ожидания"),
    71: ("desired_role", "Желаемая роль, функционал и задачи"),
    73: ("salary", "Зарплатные ожидания"),
    74: ("location", "Город / страна проживания"),
    96: ("salary", "Зарплатные ожидания"),
    107: ("salary", "Зарплатные ожидания"),
    108: ("location", "Город / страна проживания"),
    112: ("salary", "Зарплатные ожидания"),
    114: ("salary", "Зарплатные ожидания"),
    117: ("salary", "Зарплатные ожидания"),
}


def source(row: sqlite3.Row) -> str:
    return f"[{row['resume_id']}] {row['title']} — {row['company']} (vacancy {row['vacancy_id']})"


def render_question(row: sqlite3.Row) -> list[str]:
    lines = [f"- {row['text'].replace(chr(10), ' ')}", f"  - Источник: {source(row)}"]
    options = json.loads(row["options_json"])
    if options:
        lines.append("  - Варианты: " + " | ".join(options))
    return lines


def load_questions(history: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(history)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            """
            SELECT qq.id, qq.text, qq.options_json, qs.resume_id, qs.vacancy_id,
                   qs.title, qs.company
            FROM questionnaire_questions AS qq
            JOIN questionnaire_scans AS qs ON qs.id = qq.scan_id
            ORDER BY qq.id
            """
        ).fetchall()
    finally:
        connection.close()


def build_report(rows: list[sqlite3.Row]) -> str:
    ids = {row["id"] for row in rows}
    mapped = set(QUESTION_CLUSTERS)
    if ids != mapped:
        raise ValueError(
            "Question mapping mismatch: "
            f"missing={sorted(ids - mapped)}, extra={sorted(mapped - ids)}"
        )

    grouped: dict[str, dict[str, list[sqlite3.Row]]] = defaultdict(lambda: defaultdict(list))
    labels: dict[str, str] = {}
    for row in rows:
        cluster = QUESTION_CLUSTERS[row["id"]]
        template, label = TEMPLATES.get(
            row["id"],
            (f"q{row['id']}", row["text"].replace("\n", " ")),
        )
        grouped[cluster][template].append(row)
        labels[template] = label

    lines = [
        "# Карта вопросов анкет работодателей",
        "",
        "Источник: `data/history.db`, таблицы `questionnaire_scans` и `questionnaire_questions`.",
        (
            "Каждый исходный вопрос отнесён ровно к одному кластеру; "
            "смысловые аналоги сведены в один шаблон."
        ),
        "",
        (
            f"**Итого: {len(rows)} вопросов, "
            f"{sum(len(group) for group in grouped.values())} смысловых шаблонов, "
            f"{len(grouped)} кластеров.**"
        ),
    ]
    emitted = 0
    for key, title in CLUSTERS.items():
        templates = grouped[key]
        count = sum(len(items) for items in templates.values())
        emitted += count
        lines.extend(
            ["", f"## {title}", "", f"{count} вопросов; {len(templates)} смысловых шаблонов."]
        )
        for template, items in templates.items():
            lines.extend(["", f"### {labels[template]} ({len(items)})"])
            for row in items:
                lines.extend(render_question(row))
    if emitted != len(rows):
        raise AssertionError(f"Rendered {emitted} questions, expected {len(rows)}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=Path("data/history.db"))
    parser.add_argument("--output", type=Path, default=Path("data/questionnaire-clusters.md"))
    args = parser.parse_args()
    report = build_report(load_questions(args.history))
    args.output.write_text(report, encoding="utf-8")
    print(f"[OK] Wrote {args.output}")


if __name__ == "__main__":
    main()

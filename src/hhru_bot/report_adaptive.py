"""ASCII formatter for adaptive resume quality (#947)."""

from .report import _ascii_table


def format_adaptive(metrics) -> str:
    rows = []
    for metric in metrics:
        median_score = "нет данных" if metric.median_score is None else f"{metric.median_score:.1f}"
        win_rate = "-" if metric.win_rate is None else f"{metric.win_rate * 100:.1f}%"
        rows.append(
            [
                metric.label,
                metric.cluster or "universal",
                str(metric.samples),
                median_score,
                win_rate,
                str(metric.applies),
                str(metric.invitations),
                str(metric.views),
            ]
        )
    return _ascii_table(
        [
            "Резюме",
            "Кластер",
            "Оценок (N)",
            "Медиана",
            "Победа над универсальным",
            "Отклики",
            "Приглашения",
            "Просмотры",
        ],
        rows,
    )


def success_statement(metrics) -> str:
    comparisons = sum(m.comparisons for m in metrics)
    wins = sum(m.wins for m in metrics)
    if not comparisons:
        return "[INFO] нет данных: нет сопоставимых score универсального и пула"
    rate = wins / comparisons
    prefix = "[OK]" if rate > 0.5 else "[INFO]"
    return (
        f"{prefix} Критерий по данным: пул выигрывает в {rate * 100:.1f}% пар "
        f"(n={comparisons}); оправдан, если доля выше 50%"
    )

"""Аудит остаточных дубликатов фолд-ключей в data/market.db.

Нормализация ролей/навыков — процесс, а не состояние: свободный текст
владельцев резюме порождает новые варианты написания, и никакой набор
правил fold_key их не закрывает раз и навсегда. Скрипт — сетка обнаружения:
пары ключей на расстоянии <=2 (общий вариант с одной удалённой буквой) и
группы, различающиеся только цифрами (версии платформ).

Когда гонять: после крупного competitors collect, после смены правил
fold_key/KEY_ALIASES (вместе с bump маркера бэкфилла), и просто
периодически. Как читать вывод: min-count пары — размер меньшего бакета;
большие пары смотрит человек и закрывает классом (правило fold_key +
bump маркера) или точечно (запись в KEY_ALIASES в market_norm.py).
Короткие ключи (<6 символов) исключены: на аббревиатурах (AI/QA/UI/HR)
сеть расстояний даёт только ложные пары.

Read-only, только stdlib:

    python3 scripts/audit_key_dupes.py [путь-к-market.db]
"""

import sqlite3
import sys
from collections import defaultdict

MIN_LEN = 6
BUCKET_CAP = 30
TOP_PAIRS = 15
TOP_DIGIT_GROUPS = 8


def load(conn, table, keycol, rawcol):
    rows = conn.execute(
        f"SELECT {keycol} AS k, {rawcol} AS raw, COUNT(DISTINCT resume_id) AS n "
        f"FROM {table} WHERE {keycol} IS NOT NULL GROUP BY {keycol}, {rawcol}"
    ).fetchall()
    counts = defaultdict(int)
    rawforms = defaultdict(lambda: defaultdict(int))
    for r in rows:
        counts[r["k"]] += r["n"]
        rawforms[r["k"]][r["raw"]] += r["n"]
    return counts, rawforms


def top_raw(rawforms, key):
    forms = rawforms.get(key)
    if not forms:
        return key
    return max(forms.items(), key=lambda kv: (kv[1], kv[0]))[0]


def deletion_pairs(counts):
    idx = defaultdict(set)
    for k in counts:
        if len(k) < MIN_LEN:
            continue
        for i in range(len(k)):
            sig = k[:i] + k[i + 1 :]
            if len(idx[sig]) < BUCKET_CAP:
                idx[sig].add(k)
    pairs = set()
    for bucket in idx.values():
        b = sorted(bucket)
        for i, a in enumerate(b):
            for other in b[i + 1 :]:
                pairs.add((a, other))
    return sorted(pairs, key=lambda p: min(counts[p[0]], counts[p[1]]), reverse=True)


def digit_groups(counts):
    skel = defaultdict(set)
    for k in counts:
        if any(ch.isdigit() for ch in k):
            skel["".join(ch for ch in k if not ch.isdigit())].add(k)
    groups = [tuple(sorted(v)) for v in skel.values() if len(v) > 1]
    return sorted(groups, key=lambda g: -min(counts[k] for k in g))


def report(title, counts, rawforms):
    print(f"=== {title}: {len(counts)} ключей, {sum(counts.values())} резюме-упоминаний ===")
    pairs = deletion_pairs(counts)
    touched = sum(min(counts[a], counts[b]) for a, b in pairs)
    print(f"кандидаты-близнецы (расстояние <=2, ключи >= {MIN_LEN} символов): "
          f"{len(pairs)} пар, ~{touched} резюме по меньшему бакету")
    for a, b in pairs[:TOP_PAIRS]:
        print(f"  {min(counts[a], counts[b]):5} | "
              f"{top_raw(rawforms, a)} [{counts[a]}] <-> {top_raw(rawforms, b)} [{counts[b]}]")
    groups = digit_groups(counts)
    dtouched = sum(min(counts[k] for k in g) for g in groups if len(g) == 2)
    print(f"цифровые варианты (версии): {len(groups)} групп, ~{dtouched} резюме")
    for g in groups[:TOP_DIGIT_GROUPS]:
        parts = " <-> ".join(f"{top_raw(rawforms, k)} [{counts[k]}]" for k in g)
        print(f"  {min(counts[k] for k in g):5} | {parts}")
    print()


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/market.db"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report("РОЛИ (competitor_resume_roles)", *load(conn, "competitor_resume_roles", "role_key", "role"))
        report("НАВЫКИ (competitor_resume_skills)", *load(conn, "competitor_resume_skills", "skill_key", "skill"))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

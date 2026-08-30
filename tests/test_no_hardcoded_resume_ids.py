"""Архитектурный страж: реальный resume_id пользователя не хардкодится (#828).

Репозиторий публичный, а `resume_id` — 38-hex хвост `resume_url` конкретного
аккаунта — личные данные. Правило из CLAUDE.md само по себе не ограждает
(#828 — три реальных значения дословно и одно правленное вручную дожили до
публичного репо), поэтому нужна механическая проверка.

Сравнивать напрямую с реальными ID нельзя — тест сам стал бы их источником.
Вместо этого фикстура `tests/fixtures/leaked_resume_id_window_hashes.txt`
хранит SHA-256 12-символьных скользящих окон реальных ID (необратимо: из
хэша окно не восстановить). Любое найденное в отслеживаемых файлах 38-40-hex
значение режется на те же 12-символьные окна, каждое хэшируется и сверяется
со списком. Скользящее окно ловит не только точное совпадение, но и правку
1-2 символов (issue #828, п. «одно значение отличается на 1 символ») — далеко
не все окна затронуты одной точечной правкой.

Как пополнить список при новой утечке (без вывода самого ID куда-либо):

    python3 -c "
    import hashlib
    real_id = input()  # ввести интерактивно, не через argv/лог
    W = 12
    for start in range(0, len(real_id) - W + 1):
        print(hashlib.sha256(real_id[start:start+W].encode()).hexdigest())
    "

и дописать вывод в фикстуру (не сам ID).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[1]
SCAN_TARGETS = ["src", "tests", "scripts", "docs", "README.md", "CLAUDE.md"]
HEX_RUN_RE = re.compile(r"[0-9a-f]{38,40}")
WINDOW = 12

# Явно подставные паттерны (образец правильного оформления, см. CLAUDE.md/#828).
# Каждый паттерн обязан покрывать значение ЦЕЛИКОМ (fullmatch), а не только его
# префикс: матч только по началу пропустил бы строку вида "00001111<реальный
# хвост>" — заголовок читается как подставной, а хвост остаётся утечкой.
_FAKE_PATTERNS = (
    re.compile(r"0{4,5}[0-9a-f]{1,4}"),  # 00001111..., 00002222...
    re.compile(r"(0123456789abcdef)+[0-9a-f]*"),
    re.compile(r"(fedcba9876543210)+[0-9a-f]*"),
    # Счётный "0000111122223333..." стиль подставных значений, используемый
    # в этом проекте наряду с 00001111-префиксом (последняя группа может
    # быть укорочена, если общая длина значения не кратна 4).
    re.compile(r"(?:0{4}|1{4}|2{4}|3{4}|4{4}|5{4}|6{4}|7{4}|8{4}|9{4})*[0-9]{0,4}"),
)


def _is_obviously_fake(value: str) -> bool:
    return any(p.fullmatch(value) for p in _FAKE_PATTERNS)


def _iter_scanned_files():
    for target in SCAN_TARGETS:
        path = ROOT / target
        if path.is_file():
            yield path
        elif path.is_dir():
            yield from sorted(path.rglob("*"))


def _load_leaked_window_hashes() -> set[str]:
    fixture = ROOT / "tests" / "fixtures" / "leaked_resume_id_window_hashes.txt"
    hashes = set()
    for line in fixture.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        hashes.add(line)
    return hashes


def test_no_tracked_file_contains_a_leaked_resume_id_fingerprint() -> None:
    """Fail-closed: любое 38-40-hex значение, чьё окно совпало с утечкой, — брак."""
    leaked_hashes = _load_leaked_window_hashes()
    violations: list[str] = []

    for path in _iter_scanned_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in HEX_RUN_RE.finditer(text):
            value = match.group(0)
            if _is_obviously_fake(value):
                continue
            for start in range(0, len(value) - WINDOW + 1):
                window = value[start : start + WINDOW]
                digest = hashlib.sha256(window.encode()).hexdigest()
                if digest in leaked_hashes:
                    line_no = text.count("\n", 0, match.start()) + 1
                    violations.append(f"{path.relative_to(ROOT)}:{line_no}: {value[:8]}...")
                    break

    assert not violations, (
        "Найден реальный resume_id пользователя (или его правленный вариант) "
        "в отслеживаемом файле — замени на подставное значение (00001..., "
        "01234567... или fedcba98...):\n" + "\n".join(violations)
    )

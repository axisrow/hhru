"""Unit-тесты дубль-гарда создания резюме (create_resume, #304).

``_existing_title_reason`` — чистая fail-closed проверка «второе резюме с той же
должностью создать нельзя». Покрывает согласованный с Codex-review (циклы 2/3)
инвариант: карточки (подтверждённый ``RESUME_LIST_CARD``) есть, но заголовки
(неподтверждённый ``RESUME_LIST_CARD_TITLE``) не читаются → отказ, а не молчаливое
разрешение дубля.
"""

from __future__ import annotations

import pytest

from hhru_bot.create_resume import _existing_title_reason

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("card_count", "titles", "title", "expected"),
    [
        # Пустой аккаунт (0 карточек, список отрисован — якорь RESUME_CREATE_BUTTON)
        # — легитимно создаёт первое резюме.
        (0, [], "Backend developer", ""),
        # Карточки есть, но заголовки не читаются (селектор уехал) → fail-closed.
        (1, [], "Backend developer", "не удалось прочитать заголовки"),
        (3, [], "Backend developer", "не удалось прочитать заголовки"),
        # Частичный сбой (Codex cycle 3): заголовков меньше, чем карточек → fail-closed,
        # иначе пропущенная карточка была бы сочтена отсутствующей и дубль ушёл бы.
        (2, ["QA"], "Backend developer", "не удалось прочитать заголовки"),
        (3, ["QA", "DevOps"], "Backend developer", "не удалось прочитать заголовки"),
        # Пустой заголовок в читаемом наборе → нельзя доказать отсутствие дубля.
        (2, ["", "QA"], "Backend developer", "не удалось прочитать заголовки"),
        # Лишние совпадения (len > card_count) — тот же признак дрейфа селектора.
        (1, ["QA", "QA"], "Backend developer", "не удалось прочитать заголовки"),
        # Совпадение по должности → дубль запрещён.
        (1, ["Backend developer"], "Backend developer", "уже существует"),
        (2, ["QA", "Backend developer"], "Backend developer", "уже существует"),
        # Нет совпадения, все заголовки читаемы → разрешено.
        (1, ["QA"], "Backend developer", ""),
        (2, ["QA", "DevOps"], "Backend developer", ""),
    ],
)
def test_existing_title_reason(card_count, titles, title, expected):
    reason = _existing_title_reason(card_count, titles, title)
    if expected:
        assert expected in reason
    else:
        # Allowed-case assertions must pin reason == "" (not just contain ""):
        # "" in reason is trivially true for any string and would mask a
        # regression where a legitimately-allowed case starts failing closed.
        assert reason == ""


@pytest.mark.parametrize(
    ("existing", "new", "expected"),
    [
        # normalize() (external_forms/detect): collapse-whitespace + strip + casefold.
        ("  Backend   Developer  ", "backend developer", "уже существует"),
        ("QA Automation Engineer", "qa automation engineer", "уже существует"),
    ],
)
def test_existing_title_reason_normalizes(existing, new, expected):
    assert expected in _existing_title_reason(1, [existing], new)

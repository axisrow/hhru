"""Заголовок черновика из identity-bound SSR-узла (#910).

Фикстуры редуцированы из живых дампов hh.ru 2026-08-25..2026-09-01
(происхождение и состав редукции — в заголовках самих фикстур):
``scheme.resume.title`` рендерится СПИСКОМ ``[{"string": ...}]`` в обоих
состояниях ``nextIncompleteScreenId`` (professional_role и common), а не
строкой, как предполагала синтетическая фикстура #909, — из-за этого
``resume-position --dry-run`` без ``--title`` терял заголовок черновика.
Рядом с identity-узлом лежат ЧУЖИЕ заголовки (``scheme.resumes``
соседних резюме, ``screens[].title`` «Роль»); их подстановка вместо
фактического заголовка запрещена (#910: рекурсивный поиск по имени
ключа исключён, адресация — только ``scheme.resume`` записи резюме).
"""

from pathlib import Path

import pytest

from hhru_bot.resume_state import parse_resume_state

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
RESUME_ID = "00007" + "0" * 34


def _markup(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_common_state_reads_identity_title_from_list_shape():
    # Живой черновик 00002 из #910: nextIncompleteScreenId=common, заголовок
    # есть, но в list-shape — старый парсер ждал строку и возвращал None.
    state = parse_resume_state(_markup("resume_position_state_common_title_910.html"), RESUME_ID)

    assert state.next_incomplete_screen_id == "common"
    assert state.status == "not_finished"
    assert state.title == "Инженер по автоматизации"


def test_common_state_does_not_substitute_sibling_title():
    # scheme.resumes несёт заголовки ДРУГИХ резюме аккаунта («Бизнес-аналитик»
    # и далее), screens — свой title «Роль» раньше resume-узла по порядку
    # ключей: рекурсивный «первый title» подставил бы чужое значение.
    state = parse_resume_state(_markup("resume_position_state_common_title_910.html"), RESUME_ID)

    assert state.title == "Инженер по автоматизации"
    assert state.title != "Бизнес-аналитик"
    assert state.title != "Роль"


def test_professional_role_state_reads_title_from_list_shape():
    # Второе состояние: nextIncompleteScreenId=professional_role. Дамп снят
    # с открытого визарда — именно отсюда dry-run #909 раньше читал заголовок
    # DOM-ом; после #904 источник только SSR, и list-shape там же.
    state = parse_resume_state(
        _markup("resume_position_state_professional_role_title_910.html"), RESUME_ID
    )

    assert state.next_incomplete_screen_id == "professional_role"
    assert state.title == "AI Engineer / Инженер агентных систем"


def test_empty_title_returns_honest_none_despite_foreign_titles():
    # Черновик без заголовка: title — ПУСТОЙ список, а соседние scheme.resumes
    # несут непустые чужие заголовки («Грузчик» и др.). Честный None лучше
    # угаданного значения (#910).
    state = parse_resume_state(_markup("resume_position_state_empty_title_910.html"), RESUME_ID)

    assert state.next_incomplete_screen_id == "professional_role"
    assert state.title is None


def test_plain_string_title_still_supported():
    # Обратная совместимость со str-shape: подтверждённый живым дампом 2026
    # (publish probe) и синтетикой #909/#912 — парсер обязан читать и его.
    markup = (
        '{"resume":{"hash":"' + RESUME_ID + '","status":"not_finished","title":"Existing draft"}}'
    )

    state = parse_resume_state(markup, RESUME_ID)

    assert state.title == "Existing draft"

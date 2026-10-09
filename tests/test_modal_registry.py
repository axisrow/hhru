"""Реестр известных модалок как данные (#1229).

Гарды каталога: уникальность ключей, закрытые словари значений, порядок
приоритетов и границы — мутации в действия не попадают, сценарий не держит
локальных копий якорей (дрейф маркера прятал бы матчинг реестра).
"""

from __future__ import annotations

import pytest

from hhru_bot.history_skip_reasons import SKIP_REASONS
from hhru_bot.live import scenarios
from hhru_bot.live.modal_registry import (
    ACTION_DISMISS,
    ACTION_SKIP,
    ACTION_UNKNOWN,
    ACTIONS,
    GEO_REGION_MODAL,
    HH_PRO_PROMO_MODAL,
    MODAL_REGISTRY,
    MUTATION_EXTERNAL,
    MUTATION_PROFILE,
    MUTATIONS,
    STALE_CONTACTS_MODAL,
    VISIBILITY_MODAL,
    match_census_text,
)

pytestmark = pytest.mark.unit

# Боевой census модалки видимости (2026-09-23): outer-узел safe, 2 close.
BATTLE_VISIBILITY_TEXT = (
    "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме на «Видно всем работодателям»"
)
# Боевой census гео-диалога (2026-09-27).
BATTLE_GEO_TEXT = "Ваш регион — Москва? Да, верно Нет, другой"
# Боевой census промо-модалки hh PRO (#1242, 2026-10-09, дамп
# overlay_census_20261009_023322.json, узел overlay-13).
BATTLE_HH_PRO_TEXT = (
    "Хотите быстрее получить приглашение? С hh PRO: Резюме будет выше в "
    "результатах поиска ... Подключить hh PRO Нажимая, вы соглашаетесь "
    "с офертой и регулярными платежами"
)


def test_registry_rows_are_unique_and_typed() -> None:
    names = [record.name for record in MODAL_REGISTRY]
    assert len(names) == len(set(names)), "имя записи — ключ каталога"
    data_qas = [record.data_qa for record in MODAL_REGISTRY if record.data_qa]
    assert len(data_qas) == len(set(data_qas)), "data-qa-якорь адресует одну запись"
    for record in MODAL_REGISTRY:
        assert record.text_marker or record.data_qa, f"{record.name}: нужен якорь"
        assert record.mutation in MUTATIONS, record.name
        assert record.action in ACTIONS, record.name
        assert record.text_marker == record.text_marker.strip(), record.name


def test_registry_priorities_ordered_and_unique() -> None:
    priorities = [record.priority for record in MODAL_REGISTRY]
    assert priorities == sorted(priorities), "кортеж = порядок матчинга"
    assert len(priorities) == len(set(priorities))


def test_registry_text_markers_do_not_overlap() -> None:
    # Матчинг — первый матч по кортежу: пересекающиеся (тем более равные)
    # text_marker молча сменили бы запись-владельца матча. Страж запрещает
    # подстрочное пересечение в обе стороны; пара с осознанным пересечением
    # заводится правкой ЭТОГО теста вместе с приоритетом, выбирающим
    # владельца матча (докстринг match_census_text).
    markers = [(r.name, r.text_marker) for r in MODAL_REGISTRY if r.text_marker]
    for i, (name_a, marker_a) in enumerate(markers):
        for name_b, marker_b in markers[i + 1 :]:
            assert marker_a not in marker_b and marker_b not in marker_a, (
                f"{name_a} и {name_b}: text_marker пересекаются — матч уйдёт не той записи"
            )


def test_registry_actions_never_mutate_profile() -> None:
    # Граница #1229: primary-кнопки-мутации не адресуются — словарь действий
    # закрыт (dismiss/skip/continue/unknown), у profile-записи продолжение
    # потока без выбора (dismiss/skip) невозможно.
    for record in MODAL_REGISTRY:
        if record.mutation == MUTATION_PROFILE:
            assert record.action in {ACTION_DISMISS, ACTION_SKIP}, record.name


def test_match_census_text_substring_and_unknown() -> None:
    assert match_census_text(BATTLE_VISIBILITY_TEXT) is VISIBILITY_MODAL
    assert (
        match_census_text("Контакты в резюме могли устареть. Заменить на новые из профиля?")
        is STALE_CONTACTS_MODAL
    )
    assert match_census_text(BATTLE_GEO_TEXT) is GEO_REGION_MODAL
    assert match_census_text(BATTLE_HH_PRO_TEXT) is HH_PRO_PROMO_MODAL
    assert match_census_text("Тестировщик 180 000 ₽") is None
    assert match_census_text("") is None


def test_visibility_record_is_skip_with_stable_reasons() -> None:
    assert VISIBILITY_MODAL.action == ACTION_SKIP
    assert VISIBILITY_MODAL.skip_reason == SKIP_REASONS.RESUME_VISIBILITY
    assert VISIBILITY_MODAL.mutation == MUTATION_PROFILE
    assert VISIBILITY_MODAL.data_qa == "hidden-resume-warning"
    assert "вручную" in VISIBILITY_MODAL.reason
    assert "не кликается" in VISIBILITY_MODAL.reason


def test_stale_contacts_record_is_dismiss_not_mutation() -> None:
    assert STALE_CONTACTS_MODAL.action == ACTION_DISMISS
    assert STALE_CONTACTS_MODAL.mutation == MUTATION_PROFILE
    assert STALE_CONTACTS_MODAL.data_qa == "profile-contacts-sync-alert"
    # Замена контактов в объяснении названа НЕвыполненной (dismiss — не accept).
    assert "не выполнялась" in STALE_CONTACTS_MODAL.reason


def test_geo_record_is_unknown_without_automated_click() -> None:
    # Кнопки диалога НЕ подтверждены живым DOM (#1226) — код не кликает и не
    # закрывает: action=unknown == прежний отказ + census.
    assert GEO_REGION_MODAL.action == ACTION_UNKNOWN
    assert GEO_REGION_MODAL.mutation == MUTATION_EXTERNAL
    assert GEO_REGION_MODAL.data_qa == ""


def test_hh_pro_record_is_unknown_financial_promo() -> None:
    # #1242: «Подключить hh PRO» — платная подписка с регулярными платежами,
    # финансовая мутация — в действия реестра не попадает никогда. Крестик
    # живым DOM не подтверждён (census атрибуты не несёт, повторно модалка
    # read-only не воспроизведена) — dismiss не адресуется: действие unknown,
    # data-qa якорей нет.
    assert HH_PRO_PROMO_MODAL.action == ACTION_UNKNOWN
    assert HH_PRO_PROMO_MODAL.mutation == MUTATION_EXTERNAL
    assert HH_PRO_PROMO_MODAL.data_qa == ""
    assert "Подключить hh PRO" in HH_PRO_PROMO_MODAL.reason
    assert "никогда" in HH_PRO_PROMO_MODAL.reason


def test_scenarios_use_registry_not_local_marker_copies() -> None:
    # Перенос обработчиков на реестр (#1229): локальные константы-маркеры
    # убраны — копия якоря рядом с реестром скрывала бы дрейф матчинга.
    assert not hasattr(scenarios, "VISIBILITY_MODAL_TEXT_MARKER")
    assert not hasattr(scenarios, "GEO_DIALOG_TEXT_MARKER")
    assert scenarios.VISIBILITY_MODAL is VISIBILITY_MODAL
    assert scenarios.GEO_REGION_MODAL is GEO_REGION_MODAL

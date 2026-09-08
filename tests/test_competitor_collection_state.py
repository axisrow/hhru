"""Юнит-тесты типизированного состояния collection run (#1047)."""

from __future__ import annotations

import threading

import pytest

from hhru_bot.competitor_collection import CollectionRunState

pytestmark = pytest.mark.unit


def test_snapshot_is_immutable_copy_of_counters():
    state = CollectionRunState(resume_page=0, observed_page_size=None)
    state.begin_page(0)
    state.add_page_results(3)

    snap = state.snapshot()
    state.add_page_results(2)
    state.mark_saved()

    assert snap.pages == 1
    assert snap.cards == 3
    assert snap.saved == 0
    assert state.snapshot().cards == 5
    assert state.snapshot().saved == 1
    with pytest.raises(Exception):  # noqa: B017, PT011 -- frozen dataclass
        snap.cards = 100  # type: ignore[misc]


def test_complete_page_tracks_cards_completed_and_resume_page():
    state = CollectionRunState(resume_page=None, observed_page_size=None)
    state.begin_page(2)
    state.add_page_results(10)
    state.mark_saved()

    # Страница ещё не завершена: cards_completed отстаёт от cards (#660).
    assert state.snapshot().cards_completed == 0
    assert state.snapshot().resume_page == 2

    state.complete_page(2, has_next=True, cap_reached=False)
    snap = state.snapshot()
    assert snap.last_completed_page == 2
    assert snap.cards_completed == 10
    assert snap.resume_page == 3

    state.complete_page(3, has_next=False, cap_reached=False)
    assert state.snapshot().resume_page is None
    assert state.snapshot().expected_details == 1


def test_expected_details_follows_target_then_final_counts():
    state = CollectionRunState(resume_page=0, observed_page_size=None)
    state.begin_page(0)
    state.add_page_results(100)
    state.set_expected_details_from_target(3, pages_this_run=1, requested_page_size=100)
    assert state.snapshot().expected_details == 300

    state.mark_failed()
    state.mark_failed()
    state.complete_page(0, has_next=True, cap_reached=True)
    # Cap: ожидание схлопывается в фактические исходы, resume_page сохранён.
    snap = state.snapshot()
    assert snap.expected_details == 2
    assert snap.resume_page == 1


def test_mark_failed_increments_atomically():
    state = CollectionRunState(resume_page=None, observed_page_size=None)

    def worker():
        for _ in range(200):
            state.mark_failed()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state.snapshot().failed == 800


def test_concurrent_updates_are_serialized():
    state = CollectionRunState(resume_page=None, observed_page_size=None)

    def worker():
        for _ in range(200):
            state.begin_page(0)
            state.add_page_results(1)
            state.mark_saved()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snap = state.snapshot()
    assert snap.pages == 800
    assert snap.cards == 800
    assert snap.saved == 800

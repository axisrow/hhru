"""Snapshot acquisition and durable checkpoint writes must have one order."""

from __future__ import annotations

import threading

import pytest

from hhru_bot.competitor_collection.lifecycle import CollectionLifecycle
from hhru_bot.competitor_collection.state import CollectionRunState
from hhru_bot.market_store import MarketStore

pytestmark = pytest.mark.integration


def test_delayed_heartbeat_cannot_overwrite_completed_page(tmp_path):
    market = MarketStore(tmp_path / "market.db")
    lifecycle = CollectionLifecycle(market, market.start_competitor_collection("Python", 3))
    state = CollectionRunState(resume_page=0, observed_page_size=None)
    state.begin_page(0)
    state.add_page_results(20)
    captured = threading.Event()
    release = threading.Event()
    main_started = threading.Event()
    errors = []

    def delayed_snapshot():
        snapshot = state.snapshot()
        captured.set()
        assert release.wait(5)
        return snapshot

    def checkpoint(snapshot, started=None):
        try:
            if started:
                started.set()
            lifecycle.checkpoint(snapshot)
        except BaseException as exc:
            errors.append(exc)

    heartbeat = threading.Thread(target=checkpoint, args=(delayed_snapshot,))
    main = threading.Thread(target=checkpoint, args=(state.snapshot, main_started))
    heartbeat.start()
    try:
        assert captured.wait(5)
        state.mark_saved()
        state.complete_page(0, has_next=True, cap_reached=False)
        main.start()
        assert main_started.wait(5)
    finally:
        release.set()
        heartbeat.join(5)
        if main.ident is not None:
            main.join(5)
    assert not heartbeat.is_alive() and not main.is_alive()
    assert not errors
    row = market.competitor_collection_runs()[0]
    assert (row["resume_page"], row["last_completed_page"], row["cards_seen_completed"]) == (
        1,
        0,
        20,
    )
    assert row["details_saved"] == 1

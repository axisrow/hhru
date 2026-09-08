"""Unit #1045: ApplyRunParams (явные параметры вместо argparse.Namespace)
и единый адаптер фиксации результата (_finish_approved_review)."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from hhru_bot.commands.apply_service import ApplyRunParams, _finish_approved_review

pytestmark = pytest.mark.unit


class _FakeHistory:
    def __init__(self) -> None:
        self.finished: list[tuple[int, str]] = []

    def finish_review(self, approved_id: int, state: str) -> None:
        self.finished.append((approved_id, state))


def _args(**overrides) -> argparse.Namespace:
    defaults = {
        "dry_run": True,
        "limit": 3,
        "max_pages": 7,
        "force": False,
        "approved": 11,
        "permit": "tok",
        "learn_questionnaires": False,
        "resume": None,
        "history": "data/history.db",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_from_args_reads_all_known_flags() -> None:
    params = ApplyRunParams.from_args(_args())
    assert params == ApplyRunParams(
        dry_run=True,
        limit=3,
        max_pages=7,
        force=False,
        approved=11,
        permit="tok",
        learn_questionnaires=False,
    )


@pytest.mark.parametrize("missing", ["limit", "max_pages", "force", "approved", "permit"])
def test_from_args_defaults_missing_flags(missing: str) -> None:
    ns = _args()
    delattr(ns, missing)
    params = ApplyRunParams.from_args(ns)
    assert getattr(params, missing) == ApplyRunParams().__dict__[missing]


def test_from_args_zero_limit_means_unlimited() -> None:
    assert ApplyRunParams.from_args(_args(limit=0)).limit == 0


def test_finish_approved_review_maps_uncertain_to_applied_fail_closed() -> None:
    history = _FakeHistory()
    _finish_approved_review(
        history, 1, result=SimpleNamespace(success=False, uncertain=True), action_reserved=True
    )
    assert history.finished == [(1, "applied")]


def test_finish_approved_review_maps_success_and_failure() -> None:
    history = _FakeHistory()
    _finish_approved_review(
        history, 2, result=SimpleNamespace(success=True, uncertain=False), action_reserved=True
    )
    _finish_approved_review(
        history, 3, result=SimpleNamespace(success=False, uncertain=False), action_reserved=True
    )
    assert history.finished == [(2, "applied"), (3, "failed")]


def test_finish_approved_review_exception_depends_on_reservation() -> None:
    history = _FakeHistory()
    _finish_approved_review(history, 4, exc=RuntimeError("boom"), action_reserved=True)
    _finish_approved_review(history, 5, exc=RuntimeError("boom"), action_reserved=False)
    assert history.finished == [(4, "applied"), (5, "failed")]

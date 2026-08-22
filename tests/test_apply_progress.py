from __future__ import annotations

from types import SimpleNamespace

import pytest

from hhru_bot.commands._common import ApplyProgress

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("result", "column"),
    [
        (SimpleNamespace(skipped=True, uncertain=True, success=True), "skipped_count"),
        (SimpleNamespace(skipped=False, uncertain=True, success=True), "uncertain_count"),
        (SimpleNamespace(skipped=False, uncertain=False, success=True), "applied_count"),
        (SimpleNamespace(skipped=False, uncertain=False, success=False), "failed_count"),
        (SimpleNamespace(success=False, acted=True), "uncertain_count"),
    ],
)
def test_finish_has_one_structural_classification_priority(result, column: str) -> None:
    progress = ApplyProgress()
    progress.begin_attempt()

    progress.finish(result)

    counts = {
        "skipped_count": progress.skipped_count,
        "uncertain_count": progress.uncertain_count,
        "applied_count": progress.applied_count,
        "failed_count": progress.failed_count,
    }
    assert counts[column] == 1
    assert sum(counts.values()) == 1


def test_finish_counts_one_attempt_only_once() -> None:
    progress = ApplyProgress()
    progress.begin_attempt()

    progress.finish(SimpleNamespace(success=True))
    progress.finish(RuntimeError("context cleanup failed"))

    assert progress.applied_count == 1
    assert progress.failed_count == 0


def test_batch_hard_failure_wins_over_uncertain_sibling() -> None:
    progress = ApplyProgress()
    progress.begin_attempt()

    progress.finish(
        [
            SimpleNamespace(success=False, uncertain=True),
            SimpleNamespace(success=False, uncertain=False),
        ]
    )

    assert progress.failed_count == 1
    assert progress.uncertain_count == 0


def test_typed_exception_is_uncertain_without_message_matching() -> None:
    class PostClick(RuntimeError):
        pass

    progress = ApplyProgress()
    progress.begin_attempt()

    progress.finish(PostClick("arbitrary text"), uncertain_exceptions=(PostClick,))

    assert progress.uncertain_count == 1
    assert progress.failed_count == 0

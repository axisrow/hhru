"""Типизированное состояние collection run сборщика конкурентов (#1047).

Раньше `run_collect` держал изменяемый dict `state` плюс россыпь локальных
счётчиков; здесь тот же набор счётчиков собран в один потокобезопасный объект
с чистым снапшотом. Контракт с checkpoint (#660) не изменился: `cards`
включает карточки текущей незавершённой страницы, а `cards_completed` — только
завершённых, синхронно с `last_completed_page`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class RunSnapshot:
    """Иммутабельный срез счётчиков на момент чтения."""

    pages: int
    cards: int
    cards_completed: int
    saved: int
    failed: int
    last_started_page: int | None
    last_completed_page: int | None
    resume_page: int | None
    observed_page_size: int | None
    expected_details: int | None


def page_cap_reached(max_pages: int | None, pages_fetched: int, has_next: bool) -> bool:
    return has_next and max_pages is not None and pages_fetched >= max_pages


def collection_status(*, details_failed: int, limited: bool) -> str:
    if limited:
        return "limited"
    return "partial" if details_failed else "complete"


class CollectionRunState:
    """Потокобезопасные счётчики одного collection run."""

    def __init__(
        self,
        *,
        resume_page: int | None,
        observed_page_size: int | None,
    ) -> None:
        self._lock = threading.Lock()
        self._pages = 0
        self._cards = 0
        self._cards_completed = 0
        self._saved = 0
        self._failed = 0
        self._last_started_page: int | None = None
        self._last_completed_page: int | None = None
        self._resume_page = resume_page
        self._observed_page_size = observed_page_size
        self._expected_details: int | None = None

    def snapshot(self) -> RunSnapshot:
        with self._lock:
            return RunSnapshot(
                pages=self._pages,
                cards=self._cards,
                cards_completed=self._cards_completed,
                saved=self._saved,
                failed=self._failed,
                last_started_page=self._last_started_page,
                last_completed_page=self._last_completed_page,
                resume_page=self._resume_page,
                observed_page_size=self._observed_page_size,
                expected_details=self._expected_details,
            )

    def begin_page(self, page_num: int) -> None:
        with self._lock:
            self._last_started_page = page_num
            self._resume_page = page_num

    def add_page_results(self, card_count: int) -> None:
        with self._lock:
            self._pages += 1
            self._cards += card_count
            self._observed_page_size = max(self._observed_page_size or 0, card_count)

    def set_expected_details_from_target(
        self, target_pages: int, pages_this_run: int, requested_page_size: int
    ) -> None:
        with self._lock:
            self._expected_details = self._cards + max(0, target_pages - pages_this_run) * (
                requested_page_size
            )

    def mark_saved(self) -> None:
        with self._lock:
            self._saved += 1

    def set_failed(self, count: int) -> None:
        with self._lock:
            self._failed = count

    def complete_page(self, page_num: int, *, has_next: bool, cap_reached: bool) -> None:
        with self._lock:
            self._last_completed_page = page_num
            self._cards_completed = self._cards
            self._resume_page = page_num + 1 if has_next else None
            if not has_next or cap_reached:
                self._expected_details = self._saved + self._failed

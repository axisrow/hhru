"""Read and cache full vacancy descriptions without treating interstitials as data."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from playwright.sync_api import Page

from .selector_groups import vacancy_page

_ERROR_MARKERS = ("войдите", "войти", "капча", "captcha", "доступ ограничен", "страница не найдена")


@dataclass(frozen=True)
class VacancyBody:
    vacancy_id: str
    url: str
    description: str
    fetched_at: datetime
    source: str = "public"

    @property
    def valid(self) -> bool:
        return looks_parsed_ok(self.description)


def looks_parsed_ok(description: str | None) -> bool:
    """Conservative guard against login, bot-check, error and empty pages."""
    text = re.sub(r"\s+", " ", description or "").strip()
    if len(text) < 80:
        return False
    lower = text.casefold()
    if any(marker in lower for marker in _ERROR_MARKERS):
        return False
    # A description should contain words, not just a shell/interstitial.
    return len(re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", text)) >= 8


class VacancyBodyCache:
    def __init__(self, ttl_seconds: float = 3600):
        self.ttl_seconds = ttl_seconds
        self._items: dict[tuple[str, str], VacancyBody] = {}

    def get(self, vacancy_id: str, url: str, *, now: float | None = None) -> VacancyBody | None:
        item = self._items.get((str(vacancy_id), url))
        if (
            item is None
            or (now if now is not None else time.time()) - item.fetched_at.timestamp()
            >= self.ttl_seconds
        ):
            return None
        return item if item.valid else None

    def put(self, body: VacancyBody) -> VacancyBody | None:
        if not body.valid or body.vacancy_id not in body.url:
            return None
        self._items[(body.vacancy_id, body.url)] = body
        return body


def refresh_vacancy_body(
    page: Page,
    vacancy_id: str,
    url: str,
    *,
    cache: VacancyBodyCache | None = None,
    force: bool = False,
    navigate: Callable[[Page, str], None] | None = None,
) -> VacancyBody | None:
    """Read an already-open vacancy page, navigating only when necessary."""
    cache = cache or VacancyBodyCache()
    if not force:
        cached = cache.get(vacancy_id, url)
        if cached:
            return cached
    if page.url != url:
        (navigate or (lambda p, target: p.goto(target, wait_until="domcontentloaded")))(page, url)
    current = str(page.url)
    if current != url or str(vacancy_id) not in current:
        return None
    description = page.locator(vacancy_page.VACANCY_DESCRIPTION).inner_text().strip()
    body = VacancyBody(str(vacancy_id), url, description, datetime.now(UTC))
    return cache.put(body)


def refresh_card(page: Page, card, *, cache: VacancyBodyCache | None = None, force: bool = False):
    """Return a copy of a card enriched with a validated body (or unchanged)."""
    from dataclasses import replace

    body = refresh_vacancy_body(page, card.vacancy_id, card.url, cache=cache, force=force)
    return replace(card, vacancy_description=body.description) if body else card

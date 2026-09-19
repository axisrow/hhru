from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import timedelta

from .config import ThrottleConfig
from .history import History

logger = logging.getLogger("hhru_bot.throttle")

BUMP_COOLDOWN = timedelta(hours=4)


@dataclass
class LimitReached(Exception):
    resume_id: str
    action: str
    limit: int

    def __str__(self) -> str:
        # «за 24ч», а не «дневной»: лимит hh.ru — скользящее окно 24ч, счётчик
        # считает так же (#1142), сообщение не должно обещать сброс в полночь.
        return f"Достигнут лимит '{self.action}' за 24ч для резюме '{self.resume_id}': {self.limit}"


class Throttle:
    def __init__(self, config: ThrottleConfig, history: History):
        self.config = config
        self.history = history

    def wait(self, reason: str = "") -> None:
        delay = random.uniform(self.config.min_delay_seconds, self.config.max_delay_seconds)
        if reason:
            logger.info("Пауза %.1fс (%s)", delay, reason)
        else:
            logger.info("Пауза %.1fс", delay)
        time.sleep(delay)

    def check_apply_limit(self, resume_id: str, dry_run: bool) -> None:
        if dry_run:
            return
        # The limit protects the whole account.  Apply may iterate over every
        # configured resume, so checking each resume independently multiplies
        # the configured allowance by the number of resumes.
        # #1142: счётчик — скользящее окно 24ч (как реальный лимит hh.ru).
        done = self.history.count_last_24h("", "apply")
        if done >= self.config.daily_apply_limit:
            raise LimitReached("account", "apply", self.config.daily_apply_limit)

    def check_bump_limit(self, resume_id: str, dry_run: bool) -> None:
        if dry_run:
            return
        # #1142: rolling-24ч согласован с BUMP_COOLDOWN (тоже скользящий,
        # time_since_last) — второй предохранитель считает ту же семантику.
        done = self.history.count_last_24h(resume_id, "bump")
        if done >= self.config.daily_bump_limit:
            raise LimitReached(resume_id, "bump", self.config.daily_bump_limit)

    def check_reply_limit(self, dry_run: bool) -> None:
        """Apply the configured daily action limit to account-wide replies."""
        if dry_run:
            return
        done = self.history.count_last_24h("", "reply")
        if done >= self.config.daily_apply_limit:
            raise LimitReached("account", "reply", self.config.daily_apply_limit)

    def can_bump_now(self, resume_id: str) -> tuple[bool, timedelta | None]:
        since = self.history.time_since_last(resume_id, "bump")
        if since is None:
            return True, None
        if since >= BUMP_COOLDOWN:
            return True, None
        return False, BUMP_COOLDOWN - since

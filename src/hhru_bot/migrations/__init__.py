"""Пакет миграций SQLite. См. _runner.apply_migrations."""

from __future__ import annotations

from ._runner import apply_migrations

__all__ = ["apply_migrations"]

"""hhru_bot — библиотека и CLI для поиска вакансий, откликов и поднятия резюме на hh.ru.

Публичный API (контракт библиотеки) = всё, что в ``__all__``. Импортируется как::

    from hhru_bot import History, Throttle, search_vacancies, filter_candidates

Реэкспорт идёт по **логическим именам**, а не по путям модулей. Это намеренно:
внутренняя перестановка файлов (например распараллеливание #20) меняет только
строку реэкспорта ниже, а внешний контракт ``from hhru_bot import History``
остаётся стабильным. Не расширяйте ``__all__`` импульсивно — попадание в него
означает semver-обязательство.

Что намеренно НЕ в публичном API:
- ``selectors`` / ``browser.launch_context`` / ``auth.login`` / ``bump_resume``
  — внутренности браузерного слоя, привязанные к hh.ru.
- ``logging_setup`` — app-поведение.
- ``load_config_or_exit`` — делает ``sys.exit``, это CLI-поведение, не библиотечное.
"""

from __future__ import annotations

from ._version import __version__
from .apply.pipeline import ApplyResult
from .config import (
    AppConfig,
    ConfigError,
    ResumeConfig,
    SearchFilters,
    ThrottleConfig,
    load_config,
)
from .history import History
from .search import VacancyCard, build_search_url, filter_candidates, search_vacancies
from .throttle import LimitReached, Throttle

__all__ = [
    # version
    "__version__",
    # config
    "AppConfig",
    "ConfigError",
    "ResumeConfig",
    "SearchFilters",
    "ThrottleConfig",
    "load_config",
    # history
    "History",
    # throttle
    "LimitReached",
    "Throttle",
    # search + filter
    "VacancyCard",
    "build_search_url",
    "filter_candidates",
    "search_vacancies",
    # apply
    "ApplyResult",
]

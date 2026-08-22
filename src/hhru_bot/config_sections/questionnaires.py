"""Парсер TOP-LEVEL секции questionnaires → QuestionnairesConfig (#482).

Как и ``ai``, это КОРНЕВАЯ секция (уровень account), а не resume-подсекция:
шаблоны ответов общие для аккаунта, а не свои у каждого резюме (переопределения
на уровне резюме живут в history.db, а не в yaml). Поэтому парсер не
регистрируется в resume-реестре ``config_sections/_registry`` и вызывается
напрямую из ``load_config``.

Секция опциональна: при её отсутствии возвращаются дефолты с
``enabled=False`` — бот ведёт себя ровно как до #482.

Разделение с секцией ``ai`` намеренное (решение #482): ``questionnaires.enabled``
включает keyword resolver, который обязан работать БЕЗ AI-зависимости, а
``ai.answer_questions`` включает только LLM-ступень поверх него.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import ConfigError


@dataclass(frozen=True)
class QuestionnairesConfig:
    """Настройки автозаполнения анкет.

    llm_match_threshold: минимальная уверенность LLM при сопоставлении вопроса
        с шаблоном (какой это смысл), llm_answer_threshold — при генерации
        самого ответа. Разделены, потому что ошибки разной цены: неверное
        сопоставление тянет за собой заведомо чужой ответ, тогда как слабый
        ответ по верно опознанному шаблону просто уходит в очередь.
    """

    enabled: bool = False
    llm_match_threshold: float = 0.90
    llm_answer_threshold: float = 0.90


_THRESHOLD_FIELDS = ("llm_match_threshold", "llm_answer_threshold")


def _parse_threshold(raw: dict, key: str, context: str, default: float) -> float:
    value = raw.get(key, default)
    # bool — подкласс int, поэтому `questionnaires.llm_match_threshold: true`
    # прошёл бы обычную числовую проверку и дал бы порог 1.0 молча. Режем явно.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"Поле '{context}.{key}' должно быть числом от 0 до 1")
    value = float(value)
    # isfinite: yaml допускает .nan/.inf, а сравнение `nan < threshold` в Python
    # ложно — такой порог молча пропускал бы ЛЮБОЙ ответ как достаточно
    # уверенный (тот же класс дефекта, что закрыт в ai/questions.py).
    if not (math.isfinite(value) and 0.0 <= value <= 1.0):
        raise ConfigError(f"Поле '{context}.{key}' должно быть числом от 0 до 1, получено: {value!r}")
    return value


def parse_questionnaires(raw, context: str) -> QuestionnairesConfig:
    """raw — корневая секция questionnaires. Отсутствие секции → дефолты.

    ``context`` — строка для диагностики ошибок (обычно 'questionnaires').
    """
    if raw is None:
        return QuestionnairesConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"Поле '{context}.enabled' должно быть boolean")
    defaults = QuestionnairesConfig()
    thresholds = {
        key: _parse_threshold(raw, key, context, getattr(defaults, key))
        for key in _THRESHOLD_FIELDS
    }
    return QuestionnairesConfig(enabled=enabled, **thresholds)


# questionnaires — корневая секция (как account/ai), не resume-подсекция,
# поэтому в resume-реестр не регистрируется; используется из load_config.

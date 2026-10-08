"""Реестр известных модалок hh.ru как данные (#1229).

Единый каталог известных оверлеев CLI-слоя live-канала: якорь (подстрока
census-текста overlay — приём #1218, или data-qa узла страницы) -> класс
мутации цели -> действие сценария -> приоритет матчинга. Расширение остаётся
транспортом и классификацией dispositions (#1218); реестр ничего ему не
добавляет и действий сам не выполняет — потребитель (сценарий) читает запись
и решает, применяя собственные структурные гейты (disposition/closeControls
для dismiss, type=="modal" и пр.).

Fail-closed границы:

- **Мутации в действия не попадают**: primary-кнопки-мутации (переключатель
  видимости, «Заменить из профиля») не адресуются никогда; допустимы только
  dismiss (close-контрол safe-overlay), skip, continue.
- **Записи нет — действия нет**: матч None — потребитель ведёт себя
  по-прежнему (прежний отказ + census). unknown — то же отсутствие действия,
  но у НАЗВАННОЙ модалки: оператор видит, что блокер опознан.

Классы мутации (``mutation``):

- ``none`` — закрытие модалки не мутирует hh.ru;
- ``external`` — исход решает человек во вкладке, код не отвечает и не
  кликает;
- ``profile`` — primary-кнопка модалки мутирует профиль; не кликается
  никогда, допустим только dismiss close-контрола.

Боевой Playwright-путь продолжает жить собственными обработчиками
(``apply/blockers.py``, напр. ``close_stale_contacts_alert``); реестр —
каталог знаний о модалке (якоря, классификация, действие), а не его замена.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..history_skip_reasons import SKIP_REASONS

# --- Закрытые словари значений (расширяются вместе с реестром, не мимо). ----
MUTATION_NONE = "none"
MUTATION_EXTERNAL = "external"
MUTATION_PROFILE = "profile"
MUTATIONS = frozenset({MUTATION_NONE, MUTATION_EXTERNAL, MUTATION_PROFILE})

ACTION_DISMISS = "dismiss"  # закрыть safe-overlay, продолжить/перепроверить
ACTION_SKIP = "skip"  # вакансия отсеивается с record.skip_reason
ACTION_CONTINUE = "continue"  # модалка не мешает — ничего не делать
ACTION_UNKNOWN = "unknown"  # действий нет: прежний отказ + census
ACTIONS = frozenset({ACTION_DISMISS, ACTION_SKIP, ACTION_CONTINUE, ACTION_UNKNOWN})


@dataclass(frozen=True)
class ModalRecord:
    """Одна известная модалка: якоря -> классификация -> действие.

    ``text_marker`` — подстрока census-текста overlay (пустая — якоря нет);
    ``data_qa`` — data-qa узла страницы (пустой — нет); хотя бы один якорь
    обязателен (гард в тестах реестра). ``reason`` — человеческое объяснение
    для вердикта/лога; ``skip_reason`` — стабильный ключ SKIP_REASONS для
    action=skip.
    """

    name: str
    mutation: str
    action: str
    priority: int  # порядок матчинга; меньше — раньше
    text_marker: str = ""
    data_qa: str = ""
    skip_reason: str = ""
    reason: str = ""


# Модалка видимости (#1214/#1218, боевой census 2026-09-23): overlay ищется
# ПОДСТРОКЕ census-текста. Переключатель видимости — мутация профиля — не
# кликается никогда; dismiss ходит только в close-контрол safe-overlay
# (второй гейт — сам исполнитель, policy.js + content.js). data-qa узла —
# из живых дампов probe testing 2026-09-09.
VISIBILITY_MODAL = ModalRecord(
    name="resume_visibility",
    text_marker="поменяйте видимость",
    data_qa="hidden-resume-warning",
    mutation=MUTATION_PROFILE,
    action=ACTION_SKIP,
    priority=10,
    skip_reason=SKIP_REASONS.RESUME_VISIBILITY,
    reason=(
        "hh.ru требует публичную видимость резюме — поменяйте видимость "
        "вручную, автоматом переключатель не кликается"
    ),
)

# Модалка «Контакты в резюме могли устареть» (#1189, read-only census
# 2026-09-20): аккаунтовый Magritte-alert, перехватывает клики на списке
# резюме и в форме отклика. Закрывается dismiss-кнопкой «Закрыть» (cancel —
# не мутация); accept «Заменить на новые из профиля» — мутация контактов,
# кодом не адресуется никогда. Боевой Playwright-обработчик —
# apply/blockers.close_stale_contacts_alert; live-канал называет модалку
# в census отказов.
STALE_CONTACTS_MODAL = ModalRecord(
    name="stale_contacts",
    text_marker="Контакты в резюме могли устареть",
    data_qa="profile-contacts-sync-alert",
    mutation=MUTATION_PROFILE,
    action=ACTION_DISMISS,
    priority=20,
    reason=(
        "модалка «Контакты в резюме могли устареть» закрывается dismiss-кнопкой "
        "«Закрыть», замена контактов не выполнялась"
    ),
)

# Гео-диалог региона (#1226, боевой census 2026-09-27): alertdialog «Ваш
# регион — Москва? Да, верно / Нет, другой» перехватывает клик кнопки
# отклика; disposition=ambiguous, close-контроля нет — policy-ядро само его
# не закроет. Кнопки диалога НЕ подтверждены живым DOM: клик по диалогу —
# отдельное решение владельца (симметрия #1132), не код. Действие unknown —
# прежний отказ + census.
GEO_REGION_MODAL = ModalRecord(
    name="geo_region",
    text_marker="Ваш регион",
    mutation=MUTATION_EXTERNAL,
    action=ACTION_UNKNOWN,
    priority=30,
    reason="гео-диалог региона перехватывает клики — ответьте в живой вкладке вручную",
)

# Порядок кортежа = приоритет матчинга (меньше — раньше); новые записи
# добавляются сюда с приоритетом, различающим пересекающиеся маркеры.
MODAL_REGISTRY: tuple[ModalRecord, ...] = (
    VISIBILITY_MODAL,
    STALE_CONTACTS_MODAL,
    GEO_REGION_MODAL,
)


def match_census_text(text: str) -> ModalRecord | None:
    """Запись по подстроке census-текста overlay; None — модалка незнакома.

    Первый матч по порядку MODAL_REGISTRY (приоритет): подстрока — приём
    боевого census #1218, пересечение маркеров снимается приоритетом более
    специфичной записи (страж уникальности якорей — тесты реестра).
    """
    for record in MODAL_REGISTRY:
        if record.text_marker and record.text_marker in (text or ""):
            return record
    return None

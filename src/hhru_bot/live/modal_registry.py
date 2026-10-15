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
  dismiss (close-контрол safe-overlay), skip, continue. Единственное исключение
  адресуется СЦЕНАРИЕМ, а не реестром: confirm попапа переезда кликается
  apply-сценарием при ``allow_relocation`` (решение владельца #1132) — реестр
  его не авторизует (действие unknown), см. RELOCATION_MODAL.
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

# Промо-модалка hh PRO (#1242, боевой census live-канала 2026-10-09, дамп
# data/logs/overlay_census_20261009_023322.json): 4 узла apply_step + футер
# ambiguous, семейство magritte-modal-overlay -> magritte-modal (role=dialog)
# -> content-wrapper -> scroll-container -> magritte-modal-footer,
# closeControls=0 у всех пяти. Первичная кнопка «Подключить hh PRO» — платная
# подписка с регулярными платежами (оферта прямо в тексте футера) — ФИНАНСОВАЯ
# мутация: в действия реестра не попадает никогда. data-qa якоря и крестик
# census не снял (payload list_overlays атрибутов не несёт), живой DOM с
# модалкой повторно read-only не воспроизведён — dismiss НЕ подтверждён и не
# адресуется; действие unknown = прежний отказ + census, симметрия #1226.
# После живого подтверждения не-мутационного крестика запись переезжает на
# dismiss (по образцу stale_contacts).
HH_PRO_PROMO_MODAL = ModalRecord(
    name="hh_pro_promo",
    text_marker="Хотите быстрее получить приглашение",
    mutation=MUTATION_EXTERNAL,
    action=ACTION_UNKNOWN,
    priority=40,
    reason=(
        "промо-модалка hh PRO: кнопка «Подключить hh PRO» — платная подписка "
        "(финансовая мутация), код её не кликает никогда; решите модалку в "
        "живой вкладке вручную"
    ),
)

# Попап подтверждения переезда (#1135, live-бой #1251): census live-канала
# 2026-10-09 (дамп apply_live_138135538_20261009_144745.overlay-census.json,
# узел overlay-13: alertdialog/ambiguous/close=0), data-qa якоря — боевой дамп
# CLI-пути apply_136789544_form_timeout.html 2026-09-16. Монтируется ПОСЛЕ
# клика по кнопке отклика ВМЕСТО формы (гонка 1,5–8 с), canonical URL не
# меняет. Confirm «Все равно откликнуться» (data-qa relocation-warning-confirm,
# 187 боевых срабатываний CLI-пути) — МУТАЦИЯ профиля (hh.ru сам меняет сигнал
# «готов к переезду», боевой факт 2026-09-19): в действия реестра не попадает
# и реестром не авторизуется никогда — кликается только сценарием отклика при
# allow_relocation (решение владельца #1132, симметрия CLI-пути
# apply/blockers.handle_post_click_blockers); без флага — честный skip.
# Действие unknown = refuse-вердикт policy называет модалку в census, решение
# (клик confirm / skip) принимает сценарий по конфигу, как у гео-диалога
# (#1226) — с той разницей, что кнопки здесь подтверждены боем.
RELOCATION_MODAL = ModalRecord(
    name="relocation_confirm",
    text_marker="Вы откликаетесь на вакансию в другой стране",
    data_qa="relocation-warning-title",
    mutation=MUTATION_PROFILE,
    action=ACTION_UNKNOWN,
    priority=50,
    reason=(
        "попап подтверждения переезда перехватил отклик: подтвердите готовность "
        "к переезду вручную в живой вкладке или включите allow_relocation в конфиге"
    ),
)

# Порядок кортежа = приоритет матчинга (меньше — раньше); новые записи
# добавляются сюда с приоритетом, различающим пересекающиеся маркеры.
MODAL_REGISTRY: tuple[ModalRecord, ...] = (
    VISIBILITY_MODAL,
    STALE_CONTACTS_MODAL,
    GEO_REGION_MODAL,
    HH_PRO_PROMO_MODAL,
    RELOCATION_MODAL,
)


def match_census_text(text: str) -> ModalRecord | None:
    """Запись по подстроке census-текста overlay; None — модалка незнакома.

    Первый матч по порядку MODAL_REGISTRY (приоритет); подстрока — приём
    боевого census #1218. Пересечение маркеров запрещено стражем
    ``test_registry_text_markers_do_not_overlap``: матч — первый по кортежу,
    пересекающийся маркер молча сменил бы запись-владельца. Осознанное
    пересечение заводится правкой этого стража вместе с приоритетом,
    выбирающим владельца матча.
    """
    for record in MODAL_REGISTRY:
        if record.text_marker and record.text_marker in (text or ""):
            return record
    return None

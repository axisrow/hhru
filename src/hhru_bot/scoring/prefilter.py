"""Эвристический pre-LLM фильтр работодателя (#85).

Отсекает «слепые отклики в пустоту» ДО LLM-скоринга (#74) — бесплатно, без
токенов. Чистая функция (тестируется без браузера). Решение: известная компания
(top_tech/big_corp), рейтинг/отзывы выше порога ИЛИ работодатель раньше
приглашал/смотрел резюме → ОСТАВЛЯЕМ; неизвестная компания без всего →
отсекаем как «low employer signal». Пороги — PrefilterConfig (#85), opt-in:
по умолчанию фильтр ОТКЛЮЧЕН (обратная совместимость — без конфига ничего не
меняется). Переиспользует classify_employer/EmployerInfo (#74).
"""

from __future__ import annotations

from .employer import KnownCompanyTier, classify_employer

# Причина отсева в filter_candidates (как «стоп-слово»/«уже откликались»).
# Строго без эмодзи (CLI-принцип). Префикс [skip] добавляет вызывающая сторона.
PREFILTER_SKIP_REASON = "low employer signal (no tier/rating/reviews/interaction)"


def employer_passes_prefilter(card, history, resume_id, thresholds) -> tuple[bool, str]:
    """Эвристический pre-LLM фильтр: стоит ли вообще откликаться (0 токенов, #85).

    Чистая функция. Возвращает ``(passes, reason)``: reason пустой, если карточка
    проходит; иначе — PREFILTER_SKIP_REASON (для лога filter_candidates). Вызывается
    в ``filter_candidates`` ПОСЛЕ дедупликации/стоп-листов и ДО ``rank_candidates``
    (который может звать LLM #74) — отсечённые карточки не доходят до LLM.

    Логика (по убыванию силы сигнала): любое срабатывание → проходит; только
    отсутствие ВСЕХ сигналов → отсев:
      1. thresholds is None / not thresholds.enabled → проходит (фильтр отключен,
         обратная совместимость). history=None тоже → проходит (нет данных о
         взаимодействии — не можем его учесть, безопасно пропустить).
      2. classify_employer (#74) → top_tech/big_corp: известная компания → проходит.
         (mid/unknown — НЕ достаточно: mid это слабый буст в скоринге, не «имя».)
      3. info.rating >= thresholds.rating_min → проходит.
      4. info.reviews_count >= thresholds.reviews_min → проходит.
      5. history.employer_interacted(vacancy_id, employer, resume_id) → проходит
         (работодатель раньше приглашал/смотрел — сильнейший позитивный сигнал).
      6. Иначе → отсев (PREFILTER_SKIP_REASON).

    ``info.trusted`` (бейдж «надёжный работодатель» hh.ru) НЕ используется как
    сигнал: залогиненный дамп (#118) показал, что hh.ru проставляет его 98%
    карточек, поэтому он не различает хороших и плохих работодателей и раньше
    сводил весь фильтр к no-op.

    ``card.employer_info`` может быть None (hh.ru не отдал блоков) — тогда пункты
    3-4 пропускаются, и решение опирается на tier по имени + взаимодействие.
    """
    # Фильтр отключен (нет конфига / enabled=False) или нет history — не отсекаем.
    if thresholds is None or not getattr(thresholds, "enabled", False):
        return True, ""
    if history is None:
        return True, ""

    info = getattr(card, "employer_info", None)
    tier = classify_employer(card.company, info)

    # 2. Известная компания (top_tech/big_corp) — сильный сигнал имени.
    if tier in (KnownCompanyTier.TOP_TECH, KnownCompanyTier.BIG_CORP):
        return True, ""

    if info is not None:
        # 3. Рейтинг выше порога.
        if info.rating is not None and info.rating >= thresholds.rating_min:
            return True, ""
        # 4. Отзывов выше порога (заметный работодатель).
        if info.reviews_count is not None and info.reviews_count >= thresholds.reviews_min:
            return True, ""

    # 5. Работодатель раньше приглашал/смотрел резюме (account-scope #12).
    if history.employer_interacted(
        vacancy_id=getattr(card, "vacancy_id", None),
        employer=card.company or None,
        resume_id=resume_id,
    ):
        return True, ""

    # 6. Никакого сигнала — отсев.
    return False, PREFILTER_SKIP_REASON

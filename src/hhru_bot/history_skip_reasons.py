"""Стабильные enum-ключи причин отсева (#87, cli-spec §clear-skipped).

Leaf-модуль: не зависит ни от фасада, ни от доменных миксинов — импортируется
и ``history`` (ре-экспорт), и доменами (profile/questionnaires).
"""

from __future__ import annotations


class _SkipReasons:
    """Стабильные enum-ключи причин отсева (#87, cli-spec §clear-skipped).

    Хранятся в ``skipped.reason`` и идут в ``--reason`` команды clear-skipped.
    НЕ человекочитаемые строки filter_candidates (``"уже откликались ранее"`` и
    т.п.) — маппинг строка→ключ делает ``filter_candidates`` в search.py. Так
    вывод фильтра остаётся локализованным для людей, а ключи в БД стабильны
    между запусками (cli-spec: ключи — проектируемый enum, привязанный к
    ПРИЧИНАМ filter_candidates, не к их строкам напрямую).

    Зарезервированы и будущие причины (#85 pre-LLM ``low_employer_signal`` и
    #84 ``has_questions``) — EnumExtension точка: новые значения добавляются
    сюда, миграций не требуется (``reason`` — свободный TEXT, валидация только
    на уровне команды clear-skipped через choices).
    """

    STOPWORD_TITLE = "stopword_title"  # exclude_keywords совпал в названии
    BLACKLIST = "blacklist"
    STOPWORD_EMPLOYER = "stopword_employer"  # exclude_employers — стоп-компания
    INCLUDE_EMPLOYER_MISS = "include_employer_miss"  # include_employers — вне списка интереса
    SALARY_OVER_LIMIT = "salary_over_limit"  # salary_to — salary_from выше лимита
    CURRENT_EMPLOYER = "current_employer"  # account.current_employer
    ALREADY_APPLIED = "already_applied"  # history.has_applied — уже откликались
    LOW_EMPLOYER_SIGNAL = "low_employer_signal"  # #85 pre-LLM фильтр (зарезервирован)
    LOW_LLM_SCORE = "low_llm_score"  # будущий отсев по LLM-скорингу #74
    LOW_RESUME_MATCH = "low_resume_match"
    LOW_LETTER_MATCH = "low_letter_match"
    HAS_QUESTIONS = "has_questions"  # #84 идея №7 (зарезервирован)
    QUESTION_LOW_CONFIDENCE = "question_skipped_low_confidence"
    # #482: вопрос анкеты ушёл в очередь на ручное решение. Отдельно от
    # QUESTION_LOW_CONFIDENCE: та причина означает «LLM ответил неуверенно» и
    # снимается только вручную, а эта снимается автоматически, когда оператор
    # обучил соответствующий шаблон (`questionnaire learn`).
    QUESTIONNAIRE_PENDING = "questionnaire_pending"
    RESUME_VISIBILITY = "resume_visibility"  # отклик заблокирован видимостью резюме
    DUPLICATE = "duplicate"  # дубликат вакансии в одном сборе
    RELOCATION_NOT_ALLOWED = "relocation_not_allowed"
    DIRECT_APPLICATION = "direct_application"
    RESPONSE_REJECTED = "response_rejected"


#: Enum-объект причин отсева. Используется как ``SKIP_REASONS.STOPWORD_TITLE``
#: — читаемее строковых литералов в filter_candidates/команде. Значения полей =
#: стабильные ключи в ``skipped.reason``.
SKIP_REASONS = _SkipReasons()

#: Все стабильные причины отсева (для ``--reason`` choices в clear-skipped и
#: валидации). Кортеж, не set — порядок стабилен для ``--help``.
SKIP_REASON_VALUES = (
    _SkipReasons.BLACKLIST,
    _SkipReasons.STOPWORD_TITLE,
    _SkipReasons.STOPWORD_EMPLOYER,
    _SkipReasons.INCLUDE_EMPLOYER_MISS,
    _SkipReasons.SALARY_OVER_LIMIT,
    _SkipReasons.CURRENT_EMPLOYER,
    _SkipReasons.ALREADY_APPLIED,
    _SkipReasons.LOW_EMPLOYER_SIGNAL,
    _SkipReasons.LOW_RESUME_MATCH,
    _SkipReasons.LOW_LETTER_MATCH,
    _SkipReasons.LOW_LLM_SCORE,
    _SkipReasons.HAS_QUESTIONS,
    _SkipReasons.QUESTION_LOW_CONFIDENCE,
    _SkipReasons.QUESTIONNAIRE_PENDING,
    _SkipReasons.RESUME_VISIBILITY,
    _SkipReasons.DUPLICATE,
    _SkipReasons.RELOCATION_NOT_ALLOWED,
    _SkipReasons.DIRECT_APPLICATION,
    _SkipReasons.RESPONSE_REJECTED,
)

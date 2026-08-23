"""Keyword-скоринг соответствия профиля кандидата вакансии (#492, Этап 1).

Чистая функция без браузера, без сети и без новых зависимостей (Тир 0 по
issue): взвешенное пересечение токенов ``AIProfile`` и ``VacancyCard.vacancy_text``.
Первый шаг фундамента под будущую генерацию резюме/письма под вакансию —
предварительный критерий «насколько уже близко».

Три решения, неочевидные из кода:

1. **Шкала — общая ``ScoreOutcome.score_0_100`` (0-100), не отдельная 0-1.**
   Смешение шкал уже ломало сортировку смешанных батчей эвристика/LLM
   (Codex #74 F2, см. ``vacancy.py``); свой диапазон здесь воспроизвёл бы тот
   же дефект при любом будущем смешении с vacancy-скором. Ориентир порога
   «0.9» из обсуждения на этой шкале означает ``90.0``.

2. **Матч строгий токенный с ограниченным стеммингом, а не подстрочный.**
   Подстрока даёт ложные совпадения («Go» внутри «гошных», «meta» внутри
   «Metallurg» — тот же класс, что чинил Codex #74 F4 в ``employer.py``).
   Но чистое равенство токенов промахивается на русской морфологии
   («разработчик» в профиле vs «разработчика» в вакансии) — ровно та проблема,
   из-за которой в ``questionnaires`` seed-признаки сделаны стемами, а не
   словоформами. Компромисс: префиксный матч разрешён только начиная с
   ``_MIN_STEM_LEN`` символов, короткие токены («go», «1с») сравниваются
   строго.

3. **Отрицание перед токеном снимает совпадение (митигация класса #490).**
   Keyword-стратегия видит ТЕМУ, но не НАМЕРЕНИЕ: «без опыта Python» содержит
   токен «python», и наивное пересечение засчитало бы его как совпадение,
   завысив score вакансии, которая явно требует ОТСУТСТВИЯ навыка. Полностью
   класс ошибки keyword-подходом не закрывается (это и зафиксировано в #490);
   здесь закрыт узкий, но реальный и дешёвый случай — явный маркер отрицания
   в небольшом окне вокруг токена. Направление fail-closed: недосчитать
   совпадение безопаснее, чем завысить соответствие.

Этап 1 (эта реализация) — только вычисление и логирование score, БЕЗ порога и
БЕЗ отсева: сперва нужно увидеть реальное распределение на живых прогонах,
иначе порог калибруется вслепую. Этап 2 (порог + отсев через ту же точку, что
``employer_passes_prefilter``) намеренно не реализован — см. issue #492.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .types import ScoreOutcome

if TYPE_CHECKING:
    from ..config_sections.ai_profile import AIProfile
    from ..search import VacancyCard

# Маркер источника скоринга в ScoreOutcome.mode — рядом с 'heuristic'/'llm'
# (#74). Отдельное значение, чтобы смешанные логи/батчи было видно по источнику.
RESUME_MATCH_MODE = "resume_match"

# Отметка в ScoreOutcome.rationale для случая «считать было нечего» (нет профиля,
# пустой профиль, пустой vacancy_text). Score при этом 0.0 — тот же, что у
# «честно не совпало», поэтому различать их можно ТОЛЬКО по этой строке. Для
# Этапа 1 это и есть смысл: распределение, по которому калибруется порог Этапа 2,
# должно строиться на карточках, где скоринг реально что-то считал.
NO_DATA_RATIONALE = "нет данных для сопоставления"

# Веса факторов профиля. skills — самый прямой сигнал соответствия (конкретные
# технологии), desired_role — сильный, но один; summary/highlights — рыхлый
# свободный текст, поэтому вес ниже: иначе длинное «о себе» с общими словами
# («опыт», «команда») перевешивало бы точное совпадение стека.
_FACTOR_WEIGHTS: dict[str, float] = {
    "skills": 0.5,
    "desired_role": 0.25,
    "summary": 0.15,
    "highlights": 0.10,
}

# Токенизация текста: те же границы, что _tokenize_name в employer.py —
# разбиение по любым не-буквенно-цифровым символам, кириллица входит в \w с re.U.
_TOKEN_SEP = re.compile(r"[^\w]+", re.UNICODE)

# С какой длины токена разрешён префиксный матч (стемминг русской морфологии:
# «разработчик» ~ «разработчика»). Короче — только точное равенство: на 2-3
# символах префиксный матч ловил бы слишком много чужого («го» в «город»).
_MIN_STEM_LEN = 4

# Стоп-слова: служебные и общие слова, встречающиеся почти в каждой вакансии и
# почти в каждом summary. Их совпадение не несёт информации о соответствии, но
# раздувает пересечение — тот же принцип, что trusted-бейдж в #118: признак с
# околостопроцентным покрытием не различает ничего.
_STOPWORDS = frozenset(
    {
        "и",
        "в",
        "на",
        "с",
        "по",
        "для",
        "не",
        "из",
        "к",
        "от",
        "до",
        "или",
        "а",
        "но",
        "the",
        "and",
        "or",
        "of",
        "in",
        "for",
        "with",
        "опыт",
        "работа",
        "работы",
        "знание",
        "умение",
        "команда",
        "компания",
        "год",
        "года",
        "лет",
        "мы",
        "вы",
        "наш",
        "ваш",
    }
)

# Маркеры отрицания (#490). «не»/«без»/«нет» перед токеном и «не требуется»/
# «не нужен» после него — самые частые формулировки hh.ru для «навык не нужен».
_NEGATION_BEFORE = frozenset({"не", "без", "нет", "кроме", "исключая", "no", "without"})
_NEGATION_AFTER = frozenset({"требуется", "нужен", "нужно", "нужна", "обязателен", "обязательно"})

# Окно поиска маркера отрицания вокруг токена (в токенах). Узкое намеренно:
# широкое окно гасило бы совпадения из соседних предложений («Требуется Django;
# знание Python не требуется» гасило бы и Django), т.е. чинило бы один класс
# ошибок, создавая другой. WINDOW_AFTER=2 означает: пара «не требуется» должна
# стоять непосредственно за токеном.
_NEGATION_WINDOW_BEFORE = 2
_NEGATION_WINDOW_AFTER = 2


def _tokenize(text: str) -> list[str]:
    """Токенизирует текст в нижнем регистре, отбрасывая пустое и стоп-слова.

    Маркеры отрицания (``_NEGATION_BEFORE``/``_NEGATION_AFTER``) сохраняются,
    даже когда они же перечислены в стоп-словах: выброшенное «не» разрывало бы
    окно ``_is_negated`` и «Python не требуется» снова читалось бы как
    совпадение — ровно тот дефект #490, который эта функция и должна гасить.
    """
    return [
        t
        for t in _TOKEN_SEP.split(text.lower())
        if t and (t not in _STOPWORDS or t in _NEGATION_BEFORE or t in _NEGATION_AFTER)
    ]


def _tokens_match(profile_token: str, vacancy_token: str) -> bool:
    """Матчит токен профиля с токеном вакансии (строго + ограниченный стемминг).

    Точное равенство — всегда. Префиксный матч (в любую сторону: профиль может
    нести как более полную, так и более короткую форму) разрешён только если
    ОБА токена не короче ``_MIN_STEM_LEN`` — иначе короткие токены («go», «1с»)
    ложно матчили бы длинные слова, начинающиеся с тех же букв.
    """
    if profile_token == vacancy_token:
        return True
    if len(profile_token) < _MIN_STEM_LEN or len(vacancy_token) < _MIN_STEM_LEN:
        return False
    return profile_token.startswith(vacancy_token) or vacancy_token.startswith(profile_token)


def _is_negated(vacancy_tokens: list[str], index: int) -> bool:
    """Стоит ли токен вакансии под отрицанием (#490: тема есть, намерение обратное).

    Проверяется узкое окно: маркер отрицания непосредственно ПЕРЕД токеном
    («без опыта Python») или отрицание + маркер требования ПОСЛЕ него
    («Python не требуется»).
    """
    start = max(0, index - _NEGATION_WINDOW_BEFORE)
    if any(t in _NEGATION_BEFORE for t in vacancy_tokens[start:index]):
        return True

    # «Python не требуется»: ищем СМЕЖНУЮ пару отрицание+требование, а не два
    # маркера порознь в окне. Порознь они склеивали бы разные предложения —
    # «Требуется Django; знание Python не требуется» гасило бы и Django.
    # strict=False обязателен: tail и tail[1:] заведомо разной длины (последний
    # элемент пары не имеет), strict=True бросал бы ValueError на каждом вызове.
    tail = vacancy_tokens[index + 1 : index + 1 + _NEGATION_WINDOW_AFTER]
    return any(
        first in _NEGATION_BEFORE and second in _NEGATION_AFTER
        for first, second in zip(tail, tail[1:], strict=False)
    )


def _matched_ratio(profile_text: str, vacancy_tokens: list[str]) -> float:
    """Доля токенов профиля [0, 1], подтверждённых текстом вакансии без отрицания.

    Доля, а не абсолютное число хитов: длинный список навыков не должен
    автоматически давать более высокий score, чем короткий и точный — иначе
    фактор мерил бы объём профиля, а не соответствие.
    """
    # Маркеры отрицания сохраняются в тексте ВАКАНСИИ (нужны для _is_negated),
    # но в профиле они — служебный шум: «без опыта» в summary не является
    # навыком, который стоит искать в вакансии.
    profile_tokens = [
        t for t in _tokenize(profile_text) if t not in _NEGATION_BEFORE and t not in _NEGATION_AFTER
    ]
    if not profile_tokens or not vacancy_tokens:
        return 0.0

    matched = 0
    for p_token in profile_tokens:
        hit = False
        for index, v_token in enumerate(vacancy_tokens):
            if not _tokens_match(p_token, v_token):
                continue
            if _is_negated(vacancy_tokens, index):
                # Совпадение под отрицанием не засчитываем, но продолжаем искать:
                # тот же навык может упоминаться в тексте ещё раз без отрицания.
                continue
            hit = True
            break
        if hit:
            matched += 1

    return matched / len(profile_tokens)


def resume_match_score(
    card: VacancyCard,
    profile: AIProfile | None,
) -> ScoreOutcome:
    """Оценивает соответствие профиля кандидата вакансии по ключевым словам (#492).

    Чистая функция: не ходит в браузер, не зовёт LLM, не бросает исключений.
    Источник текста вакансии — ``card.vacancy_text``, который заполняется уже на
    search-шаге (``card.inner_text()`` карточки), поэтому поход на страницу
    вакансии не нужен.

    Возвращает ``ScoreOutcome`` на шкале 0-100 (``mode=RESUME_MATCH_MODE``) с
    ``breakdown`` по факторам профиля — Этап 1 использует его только для
    логирования и наблюдения за распределением, отсева здесь НЕТ.

    Вырожденные входы дают строгий ``0.0``: нет профиля, пустой профиль, пустой
    ``vacancy_text``. Пустой текст вакансии — именно ноль, а не «идеальное
    совпадение»: отсутствие данных не доказывает соответствия (fail-closed —
    общий принцип проекта, ср. ``PageStateIndeterminate``).

    Но ноль «нет данных» и ноль «честно не совпало» РАЗЛИЧАЮТСЯ в ``rationale``
    (``NO_DATA_RATIONALE``). Для Этапа 1 это принципиально: оба вида нулей
    попадают в одно значение шкалы, и без пометки распределение, по которому
    Этап 2 будет калибровать порог, оказалось бы загрязнено карточками, где
    скоринг просто нечего было считать. Отдельный ``mode``/шкала для этого не
    заводятся — ``mode`` остаётся маркером ИСТОЧНИКА скоринга (#74).
    """
    if profile is None:
        return ScoreOutcome(
            score_0_100=0.0,
            mode=RESUME_MATCH_MODE,
            rationale=NO_DATA_RATIONALE,
            breakdown={},
        )

    vacancy_tokens = _tokenize(getattr(card, "vacancy_text", "") or "")
    if not vacancy_tokens:
        return ScoreOutcome(
            score_0_100=0.0,
            mode=RESUME_MATCH_MODE,
            rationale=NO_DATA_RATIONALE,
            breakdown={},
        )

    sources: dict[str, str] = {
        "skills": " ".join(profile.skills or []),
        "desired_role": profile.desired_role or "",
        "summary": profile.summary or "",
        "highlights": " ".join(profile.highlights or []),
    }

    breakdown: dict[str, float] = {}
    weighted_sum = 0.0
    weight_total = 0.0
    for factor, text in sources.items():
        if not text.strip():
            # Незаполненный фактор не штрафует: он выпадает и из числителя, и из
            # знаменателя, поэтому профиль из одних skills может дать 100, а не
            # четверть максимума за «пустые» summary/highlights.
            continue
        weight = _FACTOR_WEIGHTS[factor]
        ratio = _matched_ratio(text, vacancy_tokens)
        breakdown[factor] = round(ratio * 100.0, 2)
        weighted_sum += ratio * weight
        weight_total += weight

    if weight_total <= 0.0:
        # Профиль пуст — считать было нечего, это тоже «нет данных», а не «не совпало».
        return ScoreOutcome(
            score_0_100=0.0,
            mode=RESUME_MATCH_MODE,
            rationale=NO_DATA_RATIONALE,
            breakdown={},
        )

    score = round((weighted_sum / weight_total) * 100.0, 2)
    matched_factors = ", ".join(f"{k}={v:.0f}" for k, v in breakdown.items() if v > 0.0)
    rationale = (
        f"keyword-match по профилю: {matched_factors}" if matched_factors else "совпадений нет"
    )

    return ScoreOutcome(
        score_0_100=score,
        mode=RESUME_MATCH_MODE,
        rationale=rationale,
        breakdown=breakdown,
    )

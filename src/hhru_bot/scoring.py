"""ML-скоринг вакансий простым путём: классификатор работодателя + LLMScoringProvider (#74).

Расширение #15 (эвристика по title/ключевым словам) поверх двух новых факторов:
  1. **classify_employer** — «известная/неизвестная компания» как фактор скоринга
     (буст за Яндекс/Сбер/FAANG и пр.). БЕЗ ML: O(1) lookup по встроенному списку
     гигантов + эвристики по trusted/reviews_count из карточки поиска hh.ru.
  2. **ScoringProvider / LLMScoringProvider** — «ML простым путём»: LLM (#16
     LLMClient) оценивает релевантность карточки профилю кандидата, возвращая
     score 0-100 + rationale. Любой сбой (None-контент, плохой JSON, исключение,
     нет openai) → молчаливый fallback на эвристику ``search._score_card`` БЕЗ
     исключения. Паттерн — как ``CoverLetterProvider``/``AICoverLetterProvider``
     (#17): abstraction + AI-реализация поверх LLMClient + устойчивый fallback.

Принципы проекта соблюдены: никакого обучения моделей/датасетов/deepagents/внешних
сайтов/новостей — только hh.ru (карточка) + LLM. Опциональность: без секции ``ai``
в конфиге провайдер не строится, ранжирование идёт по эвристике (#15).
``openai`` не нужен для импорта этого модуля — LLMClient лениво тянет его только
в момент ``chat()`` (см. ``hhru_bot.ai.letters``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .ai import LLMClient
    from .config_sections.ai_profile import AIProfile
    from .search import VacancyCard

logger = logging.getLogger("hhru_bot.scoring")


# --- классификатор «известная/неизвестная компания» (Этап 2) ------------------
#
# Чистая функция без ML. Два источника решения:
#   1. O(1) lookup по встроенному списку гигантов (RU big tech + банки + global
#      FAANG/BigTech). Список намеренно короткий и правится руками — никаких
#      парсингов википедии/новостей (принцип простоты).
#   2. Эвристики по данным карточки hh.ru: trusted=True → известная;
#      reviews_count >= порога → известная (много отзывов = крупная компания).


class KnownCompanyTier:
    """Уровень «известности» работодателя (строковые константы, не Enum — ради простоты).

    Порядок «известности»: top_tech > big_corp > mid > unknown. Буст в скоринге
    зависит от tier (см. TIER_BOOST). unknown = ничего не знаем о компании.
    """

    TOP_TECH = "top_tech"  # Яндекс/Сбер/VK/FAANG — буст максимальный
    BIG_CORP = "big_corp"  # МТС/Альфа/Авито и пр. крупные — буст средний
    MID = "mid"  # средний бизнес (много отзывов, но не в списке) — слабый буст
    UNKNOWN = "unknown"  # ООО «Ромашка» без отзывов — буста нет


# Гиганты RU: top tech + крупные банки/ритейл. Проверяем СТРОГИМ токен-матчем
# (см. _name_matches), НЕ подстрокой — иначе «Metallurg» ложно матчит «meta»,
# а «vk» — любое имя с этим сочетанием букв. Бренды, а не юр. лица: пользователь
# видит на hh.ru именно бренд («Яндекс», не «ООО Яндекс»).
_KNOWN_TOP_TECH_RU = (
    "яндекс",
    "yandex",
    "сбербанк",
    "сбер",
    "sber",
    "vk",
    "вконтакте",
    "vkontakte",
    "ozon",
    "озон",
    "тинькофф",
    "tinkoff",
    "т-банк",
    "авито",
    "avito",
    "wildberries",
    "headhunter",
    "газпромбанк",
    "ржд",
)

# Крупный бизнес второй линии (банки/телеком/ритейл): буст ниже top tech, но
# выше неизвестной компании. Т-Банк здесь дублируется с top_tech намеренно —
# бренд «т-банк»/«тинькофф» — топ-тех, но alias «tinkoff» в обоих списках
# безвреден (раньше проверка top_tech идёт первой).
_KNOWN_BIG_CORP_RU = (
    "мтс",
    "mts",
    "альфа-банк",
    "alfabank",
    "втб",
    "vtb",
    "мегамаркет",
    "ламода",
    "контур",
    "билайн",
    "beeline",
    "мегафон",
    "megafon",
    "ростелеком",
    "rtk",
    "озон тех",
)

# Глобальные гиганты (FAANG/BigTech): высокий буст, как у RU top tech.
# Одиночные короткие alias'ы («meta», «ibm») матчатся строго как отдельный
# токен имени — «Metallurg»/«IBMeter» НЕ матчат.
_KNOWN_GLOBAL = (
    "google",
    "гугл",
    "amazon",
    "амазон",
    "apple",
    "эпл",
    "microsoft",
    "майкрософт",
    "meta",
    "facebook",
    "netflix",
    "tesla",
    "nvidia",
    "openai",
    "ibm",
    "intel",
    "oracle",
    "salesforce",
)

# Порог reviews_count для эвристики MID: компания с таким количеством отзывов на
# hh.ru — заметный игрок рынка (но не обязательно из списка гигантов). Значение
# консервативное, чтобы не завышать tier мелким employer'ам.
_REVIEWS_COUNT_MID_THRESHOLD = 100


# Regex для токенизации имени работодателя: разбиваем по любым не-буквенно-
# цифровым границам (пробелы, дефисы hh.ru «альфа-банк»/«т-банк», точки «Яндекс.
# Такси», кавычки ООО). Цифры оставляем (напр. «1С»), но брендов с цифрами в
# списке нет — это просто сохраняет информацию. Кириллица входит в \w с re.U.
_TOKEN_SEP = re.compile(r"[^\w]+", re.UNICODE)


def _tokenize_name(name_lower: str) -> list[str]:
    """Токенизирует имя работодателя в нижнем регистре по не-буквенно-цифровым границам.

    «ООО Яндекс.Такси» → ['ооо', 'яндекс', 'такси']; «Alpha-Bank» → ['alpha', 'bank'].
    Пустые токены фильтруются.
    """
    return [t for t in _TOKEN_SEP.split(name_lower) if t]


def _name_matches(name_lower: str, brands: tuple[str, ...]) -> bool:
    """Строгий матч бренда против имени работодателя.

    Бренд матчит, если:
      - однословный бренд ('meta', 'vk') равен одному из ТОКЕНОВ имени (точное
        равенство, не подстрока) — «Metallurg» → токен 'metallurg' ≠ 'meta';
      - многословный бренд ('ozon tech', 'альфа-банк') присутствует в имени как
        идущая ПОДРЯД последовательность токенов.

    Так исключается спуфинг короткими alias'ами и подстроками (Codex #74 F4),
    но сохраняется матч бренда внутри юр. лица/дочки («Яндекс.Такси»).
    """
    if not name_lower:
        return False
    tokens = _tokenize_name(name_lower)
    if not tokens:
        return False
    token_set = set(tokens)
    for brand in brands:
        b_tokens = _tokenize_name(brand)
        if not b_tokens:
            continue
        if len(b_tokens) == 1:
            if b_tokens[0] in token_set:
                return True
        else:
            # Многословный бренд: ищем как подпоследовательность идущих подряд токенов.
            first = b_tokens[0]
            n = len(b_tokens)
            for i in (idx for idx, t in enumerate(tokens) if t == first):
                if tokens[i : i + n] == b_tokens:
                    return True
    return False


def classify_employer(name: str | None, info: EmployerInfo | None = None) -> str:
    """Классифицирует работодателя по уровню известности (Этап 2, #74).

    Чистая O(1) функция без ML. Возвращает одну из констант ``KnownCompanyTier``.

    Логика (по убыванию приоритета):
      1. Имя бренда в списке RU top tech ИЛИ глобальных гигантов → ``top_tech``.
      2. Имя бренда в списке RU big corp → ``big_corp``.
      3. ``info.trusted`` (надёжный работодатель по hh.ru) → ``big_corp``:
         hh.ru присваивает бейдж крупным/проверенным, это надёжный сигнал.
      4. ``info.reviews_count`` >= порога → ``mid`` (много отзывов, но не гигант).
      5. Иначе → ``unknown``.

    ``info`` опционален: без него работает только lookup по имени. Регистр
    игнорируется; матчится СТРОГИЙ токен-матч (бренд как отдельный токен имени
    или идущая подряд последовательность токенов), а не подстрока — чтобы
    «Metallurg» не ложно матчило «meta» (Codex #74 F4).
    """
    name_lower = (name or "").lower()

    if name_lower and (
        _name_matches(name_lower, _KNOWN_TOP_TECH_RU) or _name_matches(name_lower, _KNOWN_GLOBAL)
    ):
        return KnownCompanyTier.TOP_TECH

    if name_lower and _name_matches(name_lower, _KNOWN_BIG_CORP_RU):
        return KnownCompanyTier.BIG_CORP

    if info is not None:
        if info.trusted:
            return KnownCompanyTier.BIG_CORP
        if info.reviews_count is not None and info.reviews_count >= _REVIEWS_COUNT_MID_THRESHOLD:
            return KnownCompanyTier.MID

    return KnownCompanyTier.UNKNOWN


# Буст к эвристическому score за tier работодателя (#74 Этап 2). Накладывается
# поверх факторов #15 (must_have/nice_to_have/text_match/exclude_keyword), чтобы
# известная компания поднимала вакансию в ранжировании, но не перекрывала
# релевантность полностью. top_tech — заметный буст; unknown — ноль (нейтрально).
TIER_BOOST: dict[str, float] = {
    KnownCompanyTier.TOP_TECH: 4.0,
    KnownCompanyTier.BIG_CORP: 2.0,
    KnownCompanyTier.MID: 1.0,
    KnownCompanyTier.UNKNOWN: 0.0,
}


@dataclass
class EmployerInfo:
    """Распарсенная информация о работодателе из карточки поиска (Этап 1, #74).

    Все поля опциональны — hh.ru показывает рейтинг/trusted не для всех карточек
    (напр. у нового работодателя без отзывов). rating — средняя оценка 0-5;
    reviews_count — число отзывов; trusted — бейдж «надёжный работодатель».
    """

    rating: float | None = None
    reviews_count: int | None = None
    trusted: bool = False


# --- ScoringProvider: abstraction + AI-реализация (Этап 3) -------------------


@dataclass
class ScoreOutcome:
    """Результат скоринга одной вакансии (0-100 + rationale + разбивка).

    score_0_100 — итоговый скор В ДИАПАЗОНЕ [0, 100]. LLM отдаёт 0-100 напрямую;
    эвристика нормализует свой сырой score в [0, 100] монотонным clamp'ом (см.
    HeuristicScoringProvider), чтобы fallback и LLM-скор были на ОДНОЙ шкале —
    иначе при частичном сбое LLM таймаут на релевантной вакансии опускал бы её
    ниже посредственных LLM-успехов (Codex #74 F2). mode — источник скоринга
    ('heuristic' | 'llm') для логов/A/B и диагностики смешанных батчей. rationale
    — короткое текстовое объяснение. breakdown — факторы по имени.
    """

    score_0_100: float
    mode: str = "heuristic"
    rationale: str = ""
    breakdown: dict[str, float] = field(default_factory=dict)


# Диапазон нормализации эвристического score (Codex #74 F2). Эвристика #15 —
# взвешенная сумма факторов, может быть отрицательной (штраф за стоп-слово) или
# большой (много must_have-хитов + tier-буст). Монотонный clamp в [0, 100]:
# сохраняет ОТНОСИТЕЛЬНЫЙ порядок карточек эвристики (важно для ранжирования) и
# приводит шкалу к LLM-диапазону, чтобы смешанный батч сортировался корректно.
_HEURISTIC_SCORE_FLOOR = 0.0
_HEURISTIC_SCORE_CEIL = 100.0


def _normalize_heuristic_score(raw: float) -> float:
    """Монотонно отображает сырой эвристический score в [0, 100].

    Clamp (не сигмоид): проще, детерминированно, сохраняет порядок эвристики.
    Карточки с raw >= 100 склеиваются в 100 (теряется разрешение только при
    больших пересечениях стека — редкий случай, приемлемо для v1).
    """
    if raw < _HEURISTIC_SCORE_FLOOR:
        return _HEURISTIC_SCORE_FLOOR
    if raw > _HEURISTIC_SCORE_CEIL:
        return _HEURISTIC_SCORE_CEIL
    return raw


class ScoringProvider(Protocol):
    """Абстракция провайдера скоринга вакансий (#74).

    ``score`` вызывается на каждую карточку в ``rank_candidates``. Провайдер
    обязан вернуть ``ScoreOutcome`` и НЕ бросать исключение: любой сбой (нет
    AI, None-контент, плохой JSON) обрабатывается внутри через fallback на
    эвристику. Так ранжирование никогда не падает целиком из-за временной
    недоступности LLM — критично для автоматики hh.ru (как и с письмами в #17).
    """

    def score(
        self,
        card: VacancyCard,
        resume_profile: AIProfile | None = None,
    ) -> ScoreOutcome: ...


# --- эвристический pre-LLM фильтр работодателя (#85) -------------------------
#
# Отсекает «слепые отклики в пустоту» ДО LLM-скоринга (#74) — бесплатно, без
# токенов. Чистая функция (тестируется без браузера). Решение: известная компания
# (top_tech/big_corp), trusted-бейдж, рейтинг/отзывы выше порога ИЛИ работодатель
# раньше приглашал/смотрел резюме → ОСТАВЛЯЕМ; неизвестная компания без всего →
# отсекаем как «low employer signal». Пороги — PrefilterConfig (#85), opt-in:
# по умолчанию фильтр ОТКЛЮЧЕН (обратная совместимость — без конфига ничего не
# меняется). Переиспользует classify_employer/EmployerInfo (#74).

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
      3. info.trusted (бейдж «надёжный работодатель» hh.ru) → проходит.
      4. info.rating >= thresholds.rating_min → проходит.
      5. info.reviews_count >= thresholds.reviews_min → проходит.
      6. history.employer_interacted(vacancy_id, employer, resume_id) → проходит
         (работодатель раньше приглашал/смотрел — сильнейший позитивный сигнал).
      7. Иначе → отсев (PREFILTER_SKIP_REASON).

    ``card.employer_info`` может быть None (hh.ru не отдал блоков) — тогда пункты
    3-5 пропускаются, и решение опирается на tier по имени + взаимодействие.
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
        # 3. Бейдж «надёжный работодатель».
        if info.trusted:
            return True, ""
        # 4. Рейтинг выше порога.
        if info.rating is not None and info.rating >= thresholds.rating_min:
            return True, ""
        # 5. Отзывов выше порога (заметный работодатель).
        if info.reviews_count is not None and info.reviews_count >= thresholds.reviews_min:
            return True, ""

    # 6. Работодатель раньше приглашал/смотрел резюме (account-scope #12).
    if history.employer_interacted(
        vacancy_id=getattr(card, "vacancy_id", None),
        employer=card.company or None,
        resume_id=resume_id,
    ):
        return True, ""

    # 7. Никакого сигнала — отсев.
    return False, PREFILTER_SKIP_REASON


# --- эвристический провайдер (обёртка над #15 + tier-буст) -------------------
#
# Связывает classify_employer (#74 Этап 2) с эвристикой #15: поверх взвешенной
# суммы факторов из ``search._score_card`` накладывается буст за tier компании.
# Это fallback для LLMScoringProvider и дефолт для ``rank_candidates`` (когда
# провайдер не передан) — но ``rank_candidates`` продолжает вызывать ``_score_card``
# напрямую для чистоты обратной совместимости; этот провайдер нужен LLM-провайдеру
# как точка отката и может использоваться тестами/явно.


def heuristic_score(
    card: VacancyCard,
    filters,
    weights,
) -> tuple[float, dict[str, float]]:
    """Эвристика #15 + tier-буст работодателя (#74 Этап 2).

    Возвращает (score, breakdown). breakdown содержит факторы #15 И отдельный
    ключ ``employer_tier`` с величиной буста (для прозрачности/логов). Импортирует
    ``_score_card`` лениво, чтобы избежать цикла ``search`` <-> ``scoring``.
    """
    from .search import _score_card  # локальный импорт: разрыв цикла

    base, breakdown = _score_card(card, filters, weights)
    tier = classify_employer(card.company, getattr(card, "employer_info", None))
    boost = TIER_BOOST.get(tier, 0.0)
    breakdown = {**breakdown, "employer_tier": boost}
    return base + boost, breakdown


class HeuristicScoringProvider:
    """Провайдер, реализующий интерфейс ScoringProvider поверх эвристики #15.

    Нужен как явный «дефолтный» провайдер и как fallback в LLMScoringProvider.
    Хранит filters/weights (из резюме), чтобы ``score`` соответствовал сигнатуре
    протокола (card, resume_profile) и не требовал передачи фильтров каждый раз.
    """

    def __init__(self, filters, weights):
        self._filters = filters
        self._weights = weights

    def score(self, card: VacancyCard, resume_profile=None) -> ScoreOutcome:  # noqa: ARG002
        raw, breakdown = heuristic_score(card, self._filters, self._weights)
        # Нормализация в [0, 100]: шкала эвристики приводится к LLM-диапазону,
        # чтобы смешанный батч (LLM-успех + fallback) сортировался корректно
        # (Codex #74 F2). mode='heuristic' — маркер источника для логов/A/B.
        return ScoreOutcome(
            score_0_100=_normalize_heuristic_score(raw),
            mode="heuristic",
            breakdown=breakdown,
        )


# --- LLMScoringProvider (#74 Этап 3) -----------------------------------------


# Ожидаемая структура JSON-ответа от LLM: {"score": 0-100, "rationale": "...",
# "factors": {...}}. factors опциональны — главное поле score. Рационально
# ограничиваем длину, чтобы не тащить простыню текста в breakdown/логи.
_MAX_RATIONALE_LEN = 240

# Границы LLM-запроса (Codex #74 F3). JSON-ответ скоринга короткий → 256 токенов
# с запасом; жёсткий timeout, чтобы деградировавший endpoint не держал автоматику
# hh.ru бесконечно (принцип «не выглядеть подозрительно» — меньше зависших
# сессий). LLMClient.forward'ит **params в transport (temperature/max_tokens/timeout).
_LLM_MAX_TOKENS = 256
_LLM_TIMEOUT = 30.0

# Circuit breaker: сколько ПОДРЯД fallback'ов терпим, прежде чем решить, что
# endpoint деградировал, и перестать звать LLM до конца батча (Codex #74 F3).
# Любой успех обнуляет счётчик (endpoint ожил). Без этого деградация грозила
# десятками повисших синхронных запросов к hh.ru/LLM.
_LLM_CIRCUIT_FAILURE_THRESHOLD = 3


class LLMScoringProvider:
    """Скоринг вакансии через LLM (#16), с устойчивым fallback на эвристику (#74).

    llm_client — готовый ``LLMClient`` (построен в commands из AppConfig.ai).
    fallback — ``HeuristicScoringProvider`` (эвристика #15 + tier-буст), на
    который откатываемся при любом сбое AI: исключение, None-контент, пустой
    текст, невалидный JSON, score вне [0,100]. resume_profile — данные кандидата
    для релевантного промпта; None → скоринг по вакансии без профиля.

    Главный инвариант: ``score`` НИКОГДА не бросает — сбой LLM не должен валить
    ранжирование (иначе автоматика hh.ru теряет лучшие совпадения из-за временной
    недоступности LLM). Точно как AICoverLetterProvider в #17.

    Circuit breaker (Codex #74 F3): после ``circuit_failure_threshold`` ПОДРЯД
    fallback'ов подряд следующие карточки сразу уходят на эвристику, не делая
    LLM-запрос — деградировавший endpoint не тратит время/деньги и не плодит
    подозрительную нагрузку. Любой успех обнуляет счётчик. Жёсткий timeout +
    max_tokens ограничивают каждый запрос. stateful — счётчик живёт один батч
    (один вызов rank_candidates).
    """

    def __init__(
        self,
        llm_client: LLMClient,
        fallback: HeuristicScoringProvider,
        resume_profile: AIProfile | None = None,
        *,
        temperature: float = 0.3,
        max_tokens: int = _LLM_MAX_TOKENS,
        timeout: float = _LLM_TIMEOUT,
        circuit_failure_threshold: int = _LLM_CIRCUIT_FAILURE_THRESHOLD,
    ):
        self._llm = llm_client
        self._fallback = fallback
        self._profile = resume_profile
        # Низкая temperature: скоринг должен быть детерминированным/оценочным,
        # а не «творческим» (как письмо). Меньше разброса между одинаковыми
        # вакансиями → стабильнее ранжирование.
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._circuit_failure_threshold = circuit_failure_threshold
        # Счётчик подряд идущих fallback'ов (circuit breaker). Обнуляется на успехе.
        self._consecutive_failures = 0

    def score(self, card: VacancyCard, resume_profile=None) -> ScoreOutcome:
        profile = resume_profile or self._profile

        # Circuit breaker открыт: endpoint деградировал, не делаем запрос —
        # сразу эвристика (без наращивания счётчика: мы LLM не звали).
        if self._consecutive_failures >= self._circuit_failure_threshold:
            logger.info(
                "LLM scoring circuit open for '%s' — heuristic fallback (no LLM call)",
                card.title,
            )
            return self._fallback.score(card, profile)

        messages = _build_scoring_prompt(card, profile)
        try:
            response = self._llm.chat(
                messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                timeout=self._timeout,
            )
        except Exception as e:  # noqa: BLE001 — широкий: любой сбой AI → fallback
            logger.warning(
                "LLM scoring failed for '%s': %s — fallback to heuristic",
                card.title,
                e,
            )
            return self._fallback_for_failure(card, profile)

        content = response.content if response is not None else None
        outcome = _parse_llm_score(content, card)
        if outcome is None:
            logger.warning(
                "LLM returned unusable score for '%s' (finish_reason=%s) — fallback to heuristic",
                card.title,
                getattr(response, "finish_reason", "?"),
            )
            return self._fallback_for_failure(card, profile)

        # Успех: обнуляем счётчик подряд-сбоев (endpoint ожил).
        self._consecutive_failures = 0
        return outcome

    def _fallback_for_failure(self, card: VacancyCard, profile) -> ScoreOutcome:
        """Фиксирует подряд-сбой в счётчике breaker'а и отдаёт эвристику."""
        self._consecutive_failures += 1
        return self._fallback.score(card, profile)


def _parse_llm_score(content: str | None, card: VacancyCard) -> ScoreOutcome | None:
    """Разбирает JSON-ответ LLM в ScoreOutcome, либо None (→ fallback).

    Принимает «грязный» контент: LLM может обернуть JSON в markdown-блок
    ```json ... ``` или добавить пояснения. Извлекаем первый JSON-объект
    regex'ом и парсим. None/пусто/не-JSON/score вне [0,100] → None (точка
    fallback, НЕ исключение — см. инвариант LLMScoringProvider).
    """
    if not content or not content.strip():
        return None

    text = content.strip()
    # Снимаем markdown-обёртку ```json ... ``` (LLM часто добавляет).
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    raw_score = data.get("score")
    if raw_score is None:
        return None
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return None

    # Score обязан быть в [0, 100]; иначе считаем ответ невалидным → fallback.
    if not 0.0 <= score <= 100.0:
        return None

    rationale = str(data.get("rationale", "") or "").strip()[:_MAX_RATIONALE_LEN]

    factors = data.get("factors")
    breakdown: dict[str, float] = {}
    if isinstance(factors, dict):
        # Берём только числовые факторы, отсекая мусор; имена факторов — ключи.
        for key, value in factors.items():
            try:
                breakdown[str(key)] = float(value)
            except (TypeError, ValueError):
                continue

    return ScoreOutcome(score_0_100=score, mode="llm", rationale=rationale, breakdown=breakdown)


def _build_scoring_prompt(card: VacancyCard, profile: AIProfile | None) -> list[dict[str, str]]:
    """Собирает chat-completion сообщения для скоринга вакансии под кандидата.

    Контекст — карточка поиска (title/company/salary + employer_info: рейтинг/
    trusted/tier) и профиль кандидата (skills/role/исключения). Просим вернуть
    СТРОГО JSON без markdown/пояснений, чтобы ``_parse_llm_score`` отработал.
    """
    system = (
        "Ты оцениваешь релевантность вакансии кандидату. Оценивай только по "
        "данным из карточки и профиля, без выдумок. Верни ТОЛЬКО валидный JSON "
        "без markdown и пояснений вида "
        '{"score": <0-100>, "rationale": "<коротко>", "factors": {}}. '
        "score — насколько вакансия подходит кандидату (0-100). "
        "rationale — одна-две фразы по-русски. factors — числовые факторы."
    )

    lines = [f"Вакансия: {card.title}.", f"Компания: {card.company or 'не указана'}."]
    salary = card.salary
    if salary is not None:
        lines.append(f"Зарплата: {salary.raw}.")
    info = getattr(card, "employer_info", None)
    if info is not None:
        tier = classify_employer(card.company, info)
        parts = [f"tier работодателя: {tier}"]
        if info.rating is not None:
            parts.append(f"рейтинг: {info.rating}")
        if info.reviews_count is not None:
            parts.append(f"отзывов: {info.reviews_count}")
        if info.trusted:
            parts.append("надёжный работодатель")
        lines.append("Работодатель: " + ", ".join(parts) + ".")

    if profile is not None:
        if profile.summary:
            lines.append(f"О кандидате: {profile.summary}.")
        if profile.skills:
            lines.append("Ключевые навыки: " + ", ".join(profile.skills) + ".")
        if profile.desired_role:
            lines.append(f"Желаемая роль: {profile.desired_role}.")

    lines.append("Оцени релевантность этой вакансии кандидату и верни JSON.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines)},
    ]

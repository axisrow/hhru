"""Классификатор «известная/неизвестная компания» (Этап 2, #74).

Чистая функция без ML. Два источника решения:
  1. O(1) lookup по встроенному списку гигантов (RU big tech + банки + global
     FAANG/BigTech). Список намеренно короткий и правится руками — никаких
     парсингов википедии/новостей (принцип простоты).
  2. Эвристика по данным карточки hh.ru: reviews_count >= порога → известная
     (много отзывов = крупная компания). Поле trusted НЕ используется (#118).
"""

from __future__ import annotations

import re

from .types import EmployerInfo


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
      3. ``info.reviews_count`` >= порога → ``mid`` (много отзывов, но не гигант).
      4. Иначе → ``unknown``.

    ``info.trusted`` (бейдж «надёжный работодатель» hh.ru) НЕ используется:
    залогиненный дамп (#118) показал, что hh.ru проставляет его 98% карточек
    поиска — сигнал с таким покрытием не различает известные и неизвестные
    компании (раньше он ложно поднимал их до ``big_corp`` и тем же путём
    сводил на нет pre-LLM фильтр #85, см. ``employer_passes_prefilter``).

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

    if (
        info is not None
        and info.reviews_count is not None
        and info.reviews_count >= _REVIEWS_COUNT_MID_THRESHOLD
    ):
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

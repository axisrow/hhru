"""Фолд-ключ, разбор составных ролей и канонический display (market_norm)."""

from __future__ import annotations

from collections import Counter

import pytest

from hhru_bot.market_norm import (
    CANONICAL_DISPLAY,
    canonical_display,
    fold_key,
    split_roles,
)

pytestmark = pytest.mark.unit


def test_fold_key_unifies_script_register_and_spacing():
    # Латинская C, кириллические С/с, пробел внутри — один ключ.
    assert fold_key("1C") == "1c"
    assert fold_key("1С") == "1c"
    assert fold_key("1с") == "1c"
    assert fold_key("1 c") == "1c"
    assert fold_key("1 С") == "1c"
    assert fold_key("ChatGPT") == fold_key("chatgpt") == fold_key("Chat GPT") == "chatgpt"


def test_fold_key_unifies_spacing_and_punctuation_in_product_names():
    assert fold_key("1С: Предприятие 8") == fold_key("1С:Предприятие 8")
    assert fold_key("1С: Предприятие 8") != fold_key("1С: Предприятие 8.3")


def test_fold_key_does_not_transliterate():
    # Осознанное решение: «чатGPT» — отдельный ключ от «ChatGPT», регистр
    # внутри одного алфавита фолдится (точный литерал ключа не фиксируем:
    # гомоглифы переводят и кириллические буквы слова «чат»).
    assert fold_key("чатGPT") == fold_key("чатgpt")
    assert fold_key("чатGPT") != fold_key("ChatGPT")


def test_fold_key_treats_nbsp_family_like_space():
    # NBSP (\u00a0), тонкий (\u2009) и узкий (\u202f) — только escape-формой:
    # невидимые литералы в исходнике нечитаемы.
    assert fold_key("Оператор\u00a01С") == fold_key("Оператор 1С")
    assert fold_key("QA\u2009Engineer") == fold_key("QA Engineer")
    assert fold_key("Оператор\u202f1С") == fold_key("Оператор 1С")


def test_fold_key_keeps_language_symbols():
    # C#, C++, C♯ несут символ как часть имени и не должны сливаться с C.
    assert fold_key("C++") == "c++"
    assert fold_key("C#") == "c#"
    assert fold_key("C") == "c"
    # Музыкальная диеза — зрительный гомоглиф решётки: C♯ пишут вместо C#.
    assert fold_key("C♯") == fold_key("C#")
    assert len({fold_key("C"), fold_key("C#"), fold_key("C++")}) == 3
    # Разделители по-прежнему фолдятся.
    assert fold_key("1С: Предприятие 8") == fold_key("1С:Предприятие 8")
    assert fold_key("CI/CD") == fold_key("CI CD")


def test_split_roles_keeps_symbol_languages():
    # Составная роль «C/C++» разбирается: «C++» выживает (ключ «c++» длиннее
    # символа фильтра), одиночная «C» остаётся в мусор-фильтре осознанно —
    # одиночно-буквенных ролей в живой базе нет.
    assert split_roles("C/C++") == ["C++"]
    assert split_roles("C#") == ["C#"]


def test_fold_key_resolves_yo_to_e():
    assert fold_key("ёлка") == fold_key("елка")


def test_fold_key_drops_emoji_and_punctuation_only():
    assert fold_key("🎥 🎬") == ""
    assert fold_key("—") == ""


def test_split_roles_cuts_separators():
    assert split_roles("Оператор 1C, кладовщик") == ["Оператор 1C", "кладовщик"]
    assert split_roles("Оператор ПК/1С") == ["Оператор ПК", "1С"]
    assert split_roles("Видеограф | Видеомонтажёр") == ["Видеограф", "Видеомонтажёр"]
    assert split_roles("Менеджер; бухгалтер") == ["Менеджер", "бухгалтер"]


def test_split_roles_keeps_hyphen_and_inner_spaces():
    assert split_roles("Продавец-консультант") == ["Продавец-консультант"]
    assert split_roles("AI-тренер") == ["AI-тренер"]
    assert split_roles("Оператор ПК 1С") == ["Оператор ПК 1С"]
    # «1С» — ключ из двух символов, роль, а не мусор.
    assert split_roles("1С") == ["1С"]


def test_split_roles_drops_junk_parts():
    # Эмодзи-only часть дропнута (фолд-ключ пуст); текстовая часть выживает
    # целиком — эмодзи внутри роли не режем, фолд-ключ его проигнорирует.
    assert split_roles("🎥 Видеограф | 🎬") == ["🎥 Видеограф"]
    assert split_roles(", Оператор 1C") == ["Оператор 1C"]
    assert split_roles("🎬") == []


def test_canonical_display_prefers_dictionary():
    key = fold_key("Оператор 1С")
    assert key in CANONICAL_DISPLAY
    forms = Counter({"Оператор 1C": 518, "Оператор 1С": 259, "Оператор 1с": 100})
    # Словарь побеждает самую частую сырую форму (латинскую).
    assert canonical_display(key, forms) == "Оператор 1С"


def test_canonical_display_falls_back_to_most_frequent_form():
    assert canonical_display("неттакогоключа", Counter({"Beta": 3, "Alpha": 1})) == "Beta"


def test_canonical_display_tie_is_lexicographic():
    assert canonical_display("неттакогоключа", Counter({"Beta": 2, "Alpha": 2})) == "Alpha"


def test_canonical_display_values_are_unique():
    # Два ключа с одинаковым display слились бы в один бакет отчёта.
    assert len(set(CANONICAL_DISPLAY.values())) == len(CANONICAL_DISPLAY)

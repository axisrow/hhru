"""Persistent, deliberately narrow vacancy blacklist matching."""
import re

def normalize_value(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()

def validate_value(entry_type: str, value: str) -> None:
    if entry_type not in {"company", "keyword", "vacancy"}:
        raise ValueError("тип должен быть company, keyword или vacancy")
    if not value or (entry_type == "keyword" and len(value) < 2):
        raise ValueError("пустое или слишком широкое правило")

def match(card, rules: dict[str, set[str]]) -> str | None:
    if normalize_value(card.company) in rules["company"]:
        return f"blacklist company: {card.company}"
    if normalize_value(card.vacancy_id) in rules["vacancy"]:
        return f"blacklist vacancy: {card.vacancy_id}"
    title = normalize_value(card.title)
    hit = next((k for k in rules["keyword"] if k in title), None)
    return f"blacklist keyword: {hit}" if hit else None

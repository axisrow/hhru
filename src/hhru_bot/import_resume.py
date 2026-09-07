"""Импорт резюме из экспорта ``export-resume`` в другой аккаунт (#1023).

Источник данных — JSON-файл схемы :data:`hhru_bot.export_resume.EXPORT_SCHEMA`
(единственный формат, чужой JSON отвергается). Заполнение — ТОЛЬКО уже
подтверждёнными браузерными путями существующих секционных команд: каркас —
``create_resume_on_hh`` (роль — прямым путём визарда #913), позиция — editor
`/resume/edit/{id}/position` (НЕ hh.ru-копия), секции — ``edit_*_on_hh``,
фото — ``upload_photo_on_hh``. Никаких новых селекторов этот модуль не вводит.

Принципы:
- **Честные пропуски.** Секция, которую экспорт не содержит или которую
  невозможно перенести без выдумывания значения (уровень навыка, CEFR
  «Родной», период опыта без дат), не переносится — причина попадает в
  ``unavailable``/отчёт. Значения-заглушки запрещены (#1023).
- **Роль не маскируется.** Создание через плейсхолдер «Другое»
  (``CreateResumeResult.placeholder_role``) или расхождение роли с экспортом —
  отдельная строка расхождения в финальном отчёте, не «успех».
- **Разбор строк — чистые функции** (``plan_*``, ``diff_export``):
  тестируются без браузера; браузерная часть командного файла только
  применяет готовые планы и собирает вердикты.
- **Никаких удалений** — ни в экспорте, ни в импорте.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .experience import ExperienceEntry, ExperiencePlan
from .export_resume import EXPORT_SCHEMA
from .languages import CEFR_LEVELS, Language
from .resume_education import EducationPlan, EducationRecord
from .resume_position import (
    DISPLAY_EMPLOYMENT,
    DISPLAY_WORK,
    EMPLOYMENT_LABELS,
    TRAVEL_LABELS,
    WORK_LABELS,
    PositionValues,
)
from .skills import Skill

# Уровень навыка не входит в экспорт (DOM страницы резюме его не отдаёт).
# Единая детерминированная конвенция вместо выдумывания per-skill значений:
# записывается intermediate и ПЕЧАТАЕТСЯ в отчёте импорта отдельной строкой —
# не молча.
IMPORT_SKILL_LEVEL = "intermediate"

GALLERY_LIMIT = 8

# Валюта по текстовым маркерам в нижнем регистре: hh.ru пишет и «руб.»,
# и код валюты текстом («EUR»), не только символ (#1028 review).
CURRENCY_MARKERS = {
    "RUR": ("₽", "руб"),
    "EUR": ("€", "eur"),
    "USD": ("$", "usd"),
}

RU_MONTHS = {
    "январь": 1,
    "февраль": 2,
    "март": 3,
    "апрель": 4,
    "май": 5,
    "июнь": 6,
    "июль": 7,
    "август": 8,
    "сентябрь": 9,
    "октябрь": 10,
    "ноябрь": 11,
    "декабрь": 12,
}

CURRENT_PERIOD_MARKERS = ("настоящее время", "по н.в.", "сейчас")


class ImportPlanError(ValueError):
    """Экспорт не может быть импортирован (схема/содержимое), до браузера."""


def load_export(path: Path) -> dict:
    """Прочитать и валидировать JSON экспорта; чужая схема — отказ."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ImportPlanError(f"файл экспорта не читается: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ImportPlanError(f"файл экспорта не JSON: {exc}") from None
    if not isinstance(payload, dict) or payload.get("schema") != EXPORT_SCHEMA:
        got = payload.get("schema") if isinstance(payload, dict) else type(payload).__name__
        raise ImportPlanError(f"неожиданная схема файла: ожидался {EXPORT_SCHEMA}, получено {got}")
    if not payload.get("resume_id") or not payload.get("resume_url"):
        raise ImportPlanError("в экспорте нет resume_id/resume_url исходного резюме")
    return payload


def export_photos_on_disk(payload: dict) -> tuple[list[Path], list[str]]:
    """Файлы скачанных фото, существующие на диске; отсутствующие — в unavailable."""
    files: list[Path] = []
    unavailable: list[str] = []
    for record in payload.get("photos", []):
        raw = record.get("file") if isinstance(record, dict) else None
        if not raw or record.get("status") != "downloaded":
            continue
        path = Path(raw)
        if path.is_file():
            files.append(path)
        else:
            unavailable.append(f"фото {record.get('photo_id')}: файла нет на диске ({raw})")
    return files, unavailable


def parse_salary_text(text: str | None) -> tuple[int | None, str | None]:
    """«150 000 руб.» → (150000, 'RUR'); без цифр/валюты — (None, None).

    Валюта распознаётся и по символу (₽/€/$), и по текстовой форме hh.ru
    («руб.», «EUR», «USD»): `resume-block-salary` использует оба вида.
    """
    if not text:
        return None, None
    digits = re.sub(r"[^0-9]", "", text)
    salary = int(digits) if digits else None
    lowered = text.lower()
    currency = next(
        (
            code
            for code, markers in CURRENCY_MARKERS.items()
            if any(marker in lowered for marker in markers)
        ),
        None,
    )
    return salary, currency


def _codes_for(text: str | None, *label_maps: dict[str, str]) -> list[str]:
    """Коды опций по отображаемому тексту (у hh.ru два набора лейблов на поле).

    Многозначный текст hh.ru («Полная занятость, Подработка») даёт несколько
    кодов; решение, что с ними делать (форма подтверждает одно значение),
    принимает вызывающий код — здесь значения не теряются.
    """
    if not text:
        return []
    codes: list[str] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        for labels in label_maps:
            for code, label in labels.items():
                if label in part and code not in codes:
                    codes.append(code)
    return codes


def plan_position(payload: dict) -> tuple[PositionValues, list[str]]:
    """План секции «Позиция» из экспорта; нераспознанное — в unavailable.

    Специализации (роль) экспортом не переносится — роль ставит визард
    создания по ``title``; для точности сравнения она проверяется readback'ом.
    """
    unavailable: list[str] = []
    position = payload.get("position") or {}
    title = position.get("title")
    if not title:
        unavailable.append("позиция: title не экспортирован — импорт невозможен без профессии")
    salary, currency = parse_salary_text(position.get("salary_text"))
    if position.get("salary_text") and salary is None:
        unavailable.append("позиция: в salary_text нет числового значения")
    if position.get("salary_text") and currency is None and salary is not None:
        unavailable.append(f"позиция: валюта «{position.get('salary_text')}» не распознана")
    fields = {
        str(row.get("field")): row.get("text")
        for row in position.get("fields", [])
        if isinstance(row, dict) and row.get("field")
    }
    employment_codes = _codes_for(
        fields.get("employmentForms"), DISPLAY_EMPLOYMENT, EMPLOYMENT_LABELS
    )
    employment: str | None = None
    if fields.get("employmentForms") and not employment_codes:
        unavailable.append(f"позиция: занятость «{fields['employmentForms']}» не распознана")
    elif len(employment_codes) > 1:
        # apply_position подтверждает только одно значение занятости (#526);
        # второе и далее не теряем молча — поле целиком уходит в unavailable.
        unavailable.append(
            f"позиция: несколько значений занятости "
            f"({', '.join(employment_codes)}) — форма подтверждает одно, "
            "поле не перенесено"
        )
    elif employment_codes:
        employment = employment_codes[0]
    work_codes = _codes_for(fields.get("workFormats"), DISPLAY_WORK, WORK_LABELS)
    work_format: str | None = None
    if fields.get("workFormats") and not work_codes:
        unavailable.append(f"позиция: формат «{fields['workFormats']}» не распознан")
    elif len(work_codes) > 1:
        unavailable.append(
            f"позиция: несколько значений формата работы "
            f"({', '.join(work_codes)}) — форма подтверждает одно, "
            "поле не перенесено"
        )
    elif work_codes:
        work_format = work_codes[0]
    commute_codes = _codes_for(fields.get("travelTime"), TRAVEL_LABELS)
    commute = commute_codes[0] if len(commute_codes) == 1 else None
    if fields.get("travelTime") and not commute_codes:
        unavailable.append(f"позиция: время в пути «{fields['travelTime']}» не распознано")
    trips_text = fields.get("businessTripReadiness")
    trips = (
        True
        if trips_text and "Могу" in trips_text and "Не могу" not in trips_text
        else False
        if trips_text and "Не могу" in trips_text
        else None
    )
    if trips_text and trips is None:
        unavailable.append(f"позиция: командировки «{trips_text}» не распознаны")
    plan = PositionValues(
        title=title,
        salary=salary,
        currency=currency,
        employment=[employment] if employment else None,
        work_format=[work_format] if work_format else None,
        commute=commute,
        business_trips=trips,
    )
    return plan, unavailable


def parse_period(period: str | None) -> tuple[str, str, str, str, bool] | None:
    """«Март 2020 — Март 2024» → ('3','2020','3','2024', False); None = не разобрали.

    Открытый период («… — по настоящее время») даёт current=True с пустым
    концом. Дробить период на выдуманные даты нельзя — нераспознанный период
    честно уходит в unavailable вызывающего кода.
    """
    if not period or not period.strip():
        return None
    parts = [part.strip() for part in period.split("—") if part.strip()]
    if not parts:
        return None
    start = _parse_month_year(parts[0])
    if start is None:
        return None
    if len(parts) == 1:
        return start[1], start[0], "", "", True
    end_text = parts[1].lower()
    if any(marker in end_text for marker in CURRENT_PERIOD_MARKERS):
        return start[1], start[0], "", "", True
    end = _parse_month_year(parts[1])
    if end is None:
        return None
    return start[1], start[0], end[1], end[0], False


def _parse_month_year(text: str) -> tuple[str, str] | None:
    """«Март 2020» → ('2020','3'); иначе None."""
    cleaned = " ".join(text.split()).strip().rstrip(".")
    lowered = cleaned.lower()
    for name, number in RU_MONTHS.items():
        if lowered.startswith(name):
            year_match = re.search(r"(\d{4})", lowered)
            return (year_match.group(1), str(number)) if year_match else None
    return None


def plan_experience(payload: dict) -> tuple[ExperiencePlan, list[str]]:
    """План опыта из экспортных карточек; запись без распознаваемого периода — пропуск."""
    unavailable: list[str] = []
    entries: list[ExperienceEntry] = []
    companies = (payload.get("experience") or {}).get("companies") or []
    for company in companies:
        name = company.get("company") or ""
        if not name:
            unavailable.append("опыт: карточка компании без названия — запись пропущена")
            continue
        for pos in company.get("positions", []):
            period = parse_period(pos.get("period"))
            if period is None:
                unavailable.append(
                    f"опыт {name} — «{pos.get('title')}»: период не разобран "
                    f"({pos.get('period')!r}), запись пропущена"
                )
                continue
            start_month, start_year, end_month, end_year, current = period
            entries.append(
                ExperienceEntry(
                    company=name,
                    position=pos.get("title") or "",
                    start_month=start_month,
                    start_year=start_year,
                    end_month="" if current else end_month,
                    end_year="" if current else end_year,
                    current=current,
                    duties=pos.get("description") or "",
                )
            )
    if companies and not entries:
        unavailable.append("опыт: ни одна запись не переносима без выдумывания дат")
    return ExperiencePlan(entries=entries), unavailable


def plan_education(payload: dict) -> tuple[EducationPlan, list[str]]:
    """План основного образования; экспортный элемент без institution — пропуск.

    Экспорт страницы резюме не различает уровень/факультет надёжно: переносится
    только то, что подтверждено элементом (institution=title, specialty из
    description/subtitle). Уровень записи hh.ru в форме не имеет отдельного поля.
    """
    unavailable: list[str] = []
    records: list[EducationRecord] = []
    for item in payload.get("education", []):
        institution = item.get("title")
        if not institution:
            unavailable.append(
                "образование: элемент без названия учебного заведения — запись пропущена"
            )
            continue
        raw_text = " ".join(
            filter(
                None, [item.get("subtitle"), item.get("description"), *(item.get("lines") or [])]
            )
        )
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", raw_text)
        records.append(
            EducationRecord(
                institution=institution,
                level="",
                faculty="",
                organization="",
                specialty="",
                year=year_match.group(1) if year_match else "",
            )
        )
    return EducationPlan(primary=records, mode="from_scratch"), unavailable


def plan_skills(payload: dict) -> tuple[list[Skill], list[str]]:
    """Навыки по именам; уровень — конвенция IMPORT_SKILL_LEVEL (печатается в отчёте)."""
    unavailable: list[str] = []
    skills: list[Skill] = []
    seen: set[str] = set()
    for row in payload.get("skills", []):
        name = (row.get("name") or "").strip()
        if not name:
            unavailable.append("навыки: пустой тег пропущен")
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        skills.append(Skill(name=name, level=IMPORT_SKILL_LEVEL))
    if payload.get("skills") and not skills:
        unavailable.append("навыки: ни одного непустого тега")
    return skills, unavailable


def plan_languages(payload: dict) -> tuple[list[Language], list[str]]:
    """Языки «Название — B2» → Language; не-CEFR уровень («Родной») — пропуск."""
    unavailable: list[str] = []
    languages: list[Language] = []
    for item in payload.get("languages", []):
        lines = item.get("lines") or []
        text = " — ".join(filter(None, [item.get("title"), item.get("subtitle")])) or "\n".join(
            lines
        )
        match = re.search(r"^(.+?)\s*[—–-]\s*(\w+)", text.strip()) if text.strip() else None
        if not match:
            unavailable.append(f"языки: строка не разобрана ({text.strip()!r})")
            continue
        name, level = match.group(1).strip(), match.group(2).strip().upper()
        if level not in CEFR_LEVELS:
            unavailable.append(
                f"языки: «{name} — {match.group(2)}» — уровень не CEFR, запись пропущена"
            )
            continue
        languages.append(Language(name=name, level=level))
    if payload.get("languages") and not languages:
        unavailable.append("языки: ни одна запись не переносима без выдумывания уровня")
    return languages, unavailable


def _norm(text: str | None) -> str:
    return " ".join(str(text or "").split())


def diff_export(source: dict, imported: dict) -> list[str]:
    """Сверка экспорт↔импорт по секциям; список расхождений, [] = совпало.

    Чистая функция над двумя payload'ами (источник и повторное чтение
    созданного резюме тем же read-путём, что и экспорт) — расхождение роли
    фиксируется вызывающим кодом по ``placeholder_role``/readback, здесь
    сравниваются только переносимые текстовые секции.
    """
    diffs: list[str] = []
    src_pos = source.get("position") or {}
    imp_pos = imported.get("position") or {}
    if _norm(src_pos.get("title")) != _norm(imp_pos.get("title")):
        diffs.append(f"позиция: title «{src_pos.get('title')}» != «{imp_pos.get('title')}»")
    src_salary, src_currency = parse_salary_text(src_pos.get("salary_text"))
    imp_salary, imp_currency = parse_salary_text(imp_pos.get("salary_text"))
    if src_salary != imp_salary:
        diffs.append(f"позиция: зарплата {src_salary} != {imp_salary}")
    if src_currency != imp_currency:
        diffs.append(f"позиция: валюта {src_currency} != {imp_currency}")
    if _norm(source.get("about")) != _norm(imported.get("about")):
        diffs.append("о себе: текст не совпал")
    src_exp = [
        (c.get("company"), p.get("title"))
        for c in (source.get("experience") or {}).get("companies", [])
        for p in c.get("positions", [])
    ]
    imp_exp = [
        (c.get("company"), p.get("title"))
        for c in (imported.get("experience") or {}).get("companies", [])
        for p in c.get("positions", [])
    ]
    if len(src_exp) != len(imp_exp):
        diffs.append(f"опыт: записей {len(src_exp)} != {len(imp_exp)}")
    else:
        for (src_company, src_title), (imp_company, imp_title) in zip(
            src_exp, imp_exp, strict=True
        ):
            if _norm(src_company) != _norm(imp_company) or _norm(src_title) != _norm(imp_title):
                diffs.append(
                    f"опыт: «{src_company} / {src_title}» != «{imp_company} / {imp_title}»"
                )
    if len(source.get("education", [])) != len(imported.get("education", [])):
        diffs.append(
            f"образование: записей {len(source.get('education', []))} != "
            f"{len(imported.get('education', []))}"
        )
    src_skills = sorted({_norm(s.get("name")).casefold() for s in source.get("skills", [])} - {""})
    imp_skills = sorted(
        {_norm(s.get("name")).casefold() for s in imported.get("skills", [])} - {""}
    )
    if src_skills != imp_skills:
        missing = sorted(set(src_skills) - set(imp_skills))
        extra = sorted(set(imp_skills) - set(src_skills))
        detail = []
        if missing:
            detail.append("не перенеслись: " + ", ".join(missing))
        if extra:
            detail.append("лишние: " + ", ".join(extra))
        diffs.append("навыки: " + "; ".join(detail) if detail else "навыки: наборы не совпали")
    src_lang = sorted(
        {_norm(i.get("title")).casefold() for i in source.get("languages", [])} - {""}
    )
    imp_lang = sorted(
        {_norm(i.get("title")).casefold() for i in imported.get("languages", [])} - {""}
    )
    if src_lang != imp_lang:
        diffs.append(f"языки: {src_lang} != {imp_lang}")
    return diffs

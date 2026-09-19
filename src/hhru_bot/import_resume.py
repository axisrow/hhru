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
from dataclasses import dataclass, field
from pathlib import Path

from .experience import ExperienceEntry, ExperiencePlan
from .export_resume import EXPORT_SCHEMA, EXPORT_SCHEMA_V1
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
from .resume_sections import (
    OUTCOME_DUPLICATE,
    Certificate,
    Contact,
    ManualRow,
    PortfolioItem,
    ResumeSectionsPlan,
    RowOutcome,
    _dedupe,
    contact_from_manual,
    contacts_from_manual_rows,
    validate_portfolio_item,
)
from .skills import Skill

# Поддержанные схемы экспорта (#1123): v2 переносит блоковые секции
# (контакты/сертификаты/портфолио), v1 — исторический формат без них.
SUPPORTED_EXPORT_SCHEMAS = (EXPORT_SCHEMA, EXPORT_SCHEMA_V1)

# Уровень навыка не входит в экспорт (DOM страницы резюме его не отдаёт).
# Единая детерминированная конвенция вместо выдумывания per-skill значений:
# записывается intermediate и ПЕЧАТАЕТСЯ в отчёте импорта отдельной строкой —
# не молча.
IMPORT_SKILL_LEVEL = "intermediate"

# Лимит общей на аккаунт галереи фото hh.ru. Числовой константы в коде
# проекта нет — единственный источник — модалка photo-viewer-limit
# (RESUME_PHOTO_VIEWER_LIMIT, «8 фото — это максимум», бои 2026-09-02/03);
# при изменении лимита hh.ru обновлять здесь.
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


def is_legacy_export(payload: dict) -> bool:
    """Экспорт схемы v1: first-class блоковых секций в файле нет (#1123).

    Такой файл импортируется как раньше — блоковые секции (контакты/
    сертификаты/портфолио) не переносятся, команда печатает прежнее
    предупреждение; ошибкой это не является.
    """
    return payload.get("schema") == EXPORT_SCHEMA_V1


def load_export(path: Path) -> dict:
    """Прочитать и валидировать JSON экспорта; чужая схема — отказ."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ImportPlanError(f"файл экспорта не читается: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ImportPlanError(f"файл экспорта не JSON: {exc}") from None
    if not isinstance(payload, dict) or payload.get("schema") not in SUPPORTED_EXPORT_SCHEMAS:
        got = payload.get("schema") if isinstance(payload, dict) else type(payload).__name__
        raise ImportPlanError(
            f"неожиданная схема файла: ожидалась одна из "
            f"{', '.join(SUPPORTED_EXPORT_SCHEMAS)}, получено {got}"
        )
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
    # Разделитель тысяч у hh.ru — не только обычный пробел: боевой экспорт
    # несёт тонкий (U+2009) и узкий NBSP (U+202F); NBSP (U+00A0) тоже (#1166).
    compact = re.sub(r"[\s\u00a0\u202f]", "", text)
    digit_groups = re.findall(r"\d+", compact)
    if len(digit_groups) > 1:
        # Диапазон («от 150 000 до 200 000 ₽») склейкой цифр не переносится —
        # честный отказ вызывающему коду через salary=None.
        return None, None
    salary = int(digit_groups[0]) if digit_groups else None
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
    elif len(commute_codes) > 1:
        unavailable.append(
            f"позиция: несколько значений времени в пути "
            f"({', '.join(commute_codes)}) — форма подтверждает одно, "
            "поле не перенесено"
        )
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
        # Одиночная дата без тире не доказывает открытый период — не
        # выдумываем current=True, честный отказ.
        return None
    end_text = parts[1].lower()
    if any(marker in end_text for marker in CURRENT_PERIOD_MARKERS):
        return start[1], start[0], "", "", True
    end = _parse_month_year(parts[1])
    if end is None:
        return None
    return start[1], start[0], end[1], end[0], False


def _parse_month_year(text: str) -> tuple[str, str] | None:
    """«Март 2020» → ('2020','3'); иначе None.

    Месяц сверяется со СЛОВОМ целиком (первый токен), не префиксом: «май»
    не должен матчит «майор».
    """
    tokens = " ".join(text.split()).strip().rstrip(".").lower().split()
    if not tokens:
        return None
    number = RU_MONTHS.get(tokens[0])
    if number is None:
        return None
    year_match = re.search(r"(\d{4})", " ".join(tokens[1:]))
    return (year_match.group(1), str(number)) if year_match else None


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

    Экспорт страницы резюме не различает уровень/факультет/специальность
    надёжно: переносится только подтверждённое — institution=title и год из
    текста элемента (level/faculty/organization/specialty остаются пустыми,
    не выдумываются). Уровня записи в форме hh.ru отдельного поля нет.
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


# --- Блоковые секции round-trip (#1123) --------------------------------------


@dataclass
class BlocksImportPlan:
    """Блоковые секции импорта: план + per-row исходы фундамента #1118.

    ``plan`` — ResumeSectionsPlan с заполненными contacts/certificates/
    portfolio; ``outcomes`` — RowOutcome по строкам каждого блока (включая
    duplicate — в боевой проход они не попадут, но видны в отчёте).
    ``legacy_export`` — экспорт v1: блоковые секции файлом не переносятся,
    план пуст, команда печатает прежнее предупреждение.
    """

    plan: ResumeSectionsPlan = field(default_factory=ResumeSectionsPlan)
    outcomes: list[RowOutcome] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    legacy_export: bool = False


def plan_contacts(payload: dict) -> tuple[list[Contact], list[RowOutcome], list[str]]:
    """План блока контактов из экспорта (#1123).

    Переиспользует строгую валидацию #1119 (``contact_from_manual``): RU-телефон
    (SMS-подтверждение hh.ru), незнакомый тип, preferred вне true/false —
    строка пропускается с причиной в unavailable, остальные переносятся.
    Конфликт схемы формы (два значения одного поля, два preferred) не
    переносит блок целиком — поле формы одно, «победил бы» невидимый выбор.
    """
    unavailable: list[str] = []
    rows: list[ManualRow] = []
    for index, row in enumerate(payload.get("contacts") or []):
        if not isinstance(row, dict):
            unavailable.append(f"контакты: строка {index} не объект — пропущена")
            continue
        fields = {
            "type": str(row.get("type") or ""),
            "value": str(row.get("value") or ""),
            "comment": str(row.get("comment") or ""),
            "preferred": "true" if row.get("preferred") else "",
        }
        try:
            contact_from_manual(fields)
        except ValueError as exc:
            unavailable.append(f"контакты: строка {index}: {exc}")
            continue
        rows.append(ManualRow(block="contact", fields=fields))
    kept, outcomes = _dedupe("contacts", rows, lambda row: tuple(sorted(row.fields.items())))
    contacts: list[Contact] = []
    if kept:
        try:
            contacts = contacts_from_manual_rows(kept)
        except ValueError as exc:
            # Секция целиком не переносится — исходов плана нет: непустая
            # карта при пустом плане уронила бы _apply_contacts гвардом
            # выравнивания посреди боевого прохода (review PR #1157).
            # Причина остаётся в unavailable, который печатается и в бою.
            unavailable.append(f"контакты: секция не переносится: {exc}")
            return [], [], unavailable
    return contacts, outcomes, unavailable


def plan_certificates(payload: dict) -> tuple[list[Certificate], list[RowOutcome], list[str]]:
    """План блока сертификатов из first-class секции экспорта (#1123).

    name обязателен; непустой year обязан быть 4-значным годом, url —
    http(s)-ссылкой: нераспознанное поле не выдумывается, строка пропускается
    с явной причиной. Повтор полного набора полей внутри блока — duplicate
    (фундамент #1118), вторая запись не планируется.
    """
    unavailable: list[str] = []
    items: list[Certificate] = []
    for index, row in enumerate(payload.get("certificates") or []):
        if not isinstance(row, dict):
            unavailable.append(f"сертификаты: строка {index} не объект — пропущена")
            continue
        name = str(row.get("name") or "").strip()
        year = str(row.get("year") or "").strip()
        url = str(row.get("url") or "").strip()
        if not name:
            unavailable.append(f"сертификаты: строка {index} без названия — пропущена")
            continue
        if year and not re.fullmatch(r"(19|20)\d{2}", year):
            unavailable.append(
                f"сертификаты: «{name}» — год {year!r} не распознан, строка пропущена"
            )
            continue
        if url and not url.startswith(("http://", "https://")):
            unavailable.append(f"сертификаты: «{name}» — url не http(s), строка пропущена")
            continue
        items.append(Certificate(name=name, year=year, url=url))
    kept, outcomes = _dedupe("certificates", items, lambda item: tuple(item.__dict__.values()))
    return kept, outcomes, unavailable


def plan_portfolio(payload: dict) -> tuple[list[PortfolioItem], list[RowOutcome], list[str]]:
    """План блока портфолио из first-class секции экспорта (#1123).

    Экспорт содержит только image-строки (состав работ подтверждает SSR —
    DOM-карточки портфолио не несут photo_id); link-строки (portfolioUrls)
    UI hh.ru не имеет (#1121) и остаются честно неподдержанными. Валидация —
    ``validate_portfolio_item`` (#1121).
    """
    unavailable: list[str] = []
    items: list[PortfolioItem] = []
    for index, row in enumerate(payload.get("portfolio") or []):
        if not isinstance(row, dict):
            unavailable.append(f"портфолио: строка {index} не объект — пропущена")
            continue
        kind = str(row.get("kind") or "")
        photo_id = str(row.get("photo_id") or "")
        if kind != "image":
            unavailable.append(
                f"портфолио: строка {index} (тип {kind or 'не указан'}) — поддержаны "
                "только image-строки (состав работ из SSR); строка не переносится"
            )
            continue
        item = PortfolioItem(kind="image", photo_id=photo_id)
        reason = validate_portfolio_item(item.kind, item.photo_id, item.title, item.url)
        if reason:
            unavailable.append(f"портфолио: строка {index}: {reason}")
            continue
        items.append(item)
    kept, outcomes = _dedupe("portfolio", items, lambda item: tuple(item.__dict__.values()))
    return kept, outcomes, unavailable


def plan_blocks(payload: dict) -> BlocksImportPlan:
    """План блоковых секций round-trip (#1123); экспорт v1 — пустой план."""
    if is_legacy_export(payload):
        return BlocksImportPlan(legacy_export=True)
    plan = ResumeSectionsPlan()
    outcomes: list[RowOutcome] = []
    unavailable: list[str] = []
    plan.contacts, contact_outcomes, notes = plan_contacts(payload)
    outcomes += contact_outcomes
    unavailable += notes
    plan.certificates, certificate_outcomes, notes = plan_certificates(payload)
    outcomes += certificate_outcomes
    unavailable += notes
    plan.portfolio, portfolio_outcomes, notes = plan_portfolio(payload)
    outcomes += portfolio_outcomes
    unavailable += notes
    return BlocksImportPlan(
        plan=plan, outcomes=outcomes, unavailable=unavailable, legacy_export=False
    )


def blocks_outcome_map(blocks: BlocksImportPlan) -> dict[str, list[RowOutcome]]:
    """Карта исходов для apply_plan: не-дубликаты, выровненные по строкам плана.

    Дубликаты в карту не попадают — тот же контракт, что у команды
    resume-sections: _apply_* проверяют len(outcomes) == len(items) плана.
    """
    return {
        block: [
            outcome
            for outcome in blocks.outcomes
            if outcome.block == block and outcome.status != OUTCOME_DUPLICATE
        ]
        for block in ("attestations", "recommendations", "certificates", "contacts", "portfolio")
    }


def _contact_key(contact: dict) -> str:
    """Ключ контакта для сверки: телефон — по национальным цифрам (маска
    hh.ru переформатирует строку), остальное — по нормализованному значению."""
    value = _norm(contact.get("value"))
    if contact.get("type") == "phone":
        digits = re.sub(r"\D", "", value)[-10:]
        return f"phone:{digits}"
    return f"{contact.get('type')}:{value.casefold()}"


def diff_export(source: dict, imported: dict, *, blocks_source: dict | None = None) -> list[str]:
    """Сверка экспорт↔импорт по секциям; список расхождений, [] = совпало.

    Чистая функция над двумя payload'ами (источник и повторное чтение
    созданного резюме тем же read-путём, что и экспорт) — расхождение роли
    фиксируется вызывающим кодом по ``placeholder_role``/readback, здесь
    сравниваются только переносимые текстовые секции. Блоковые секции
    (#1123) сверяются только при заданном ``blocks_source`` — секциях,
    которые ПЛАН реально переносил: план честно пропускает часть строк
    источника (RU-телефон, link-строки, неполные записи), и сверка с сырым
    source превратила бы каждый такой пропуск в вечное ложное расхождение
    (cycle-review PR #1157).
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
    if blocks_source is not None:
        src_certs = sorted(
            {_norm(c.get("name")).casefold() for c in blocks_source.get("certificates", [])} - {""}
        )
        imp_certs = sorted(
            {_norm(c.get("name")).casefold() for c in imported.get("certificates", [])} - {""}
        )
        if src_certs != imp_certs:
            diffs.append(f"сертификаты: {src_certs} != {imp_certs}")
        src_contacts = sorted(
            {_contact_key(c) for c in blocks_source.get("contacts", []) if _norm(c.get("value"))}
        )
        imp_contacts = sorted(
            {_contact_key(c) for c in imported.get("contacts", []) if _norm(c.get("value"))}
        )
        if src_contacts != imp_contacts:
            diffs.append(f"контакты: {src_contacts} != {imp_contacts}")
        src_portfolio = len(
            [p for p in blocks_source.get("portfolio", []) if p.get("kind") == "image"]
        )
        imp_portfolio = len([p for p in imported.get("portfolio", []) if p.get("kind") == "image"])
        if src_portfolio != imp_portfolio:
            # photo_id между аккаунтами заведомо разные (фото загружаются
            # заново) — сверяется число работ, не идентификаторы.
            diffs.append(f"портфолио: работ {src_portfolio} != {imp_portfolio}")
    return diffs

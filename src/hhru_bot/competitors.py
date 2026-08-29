"""Read-only collection and deterministic reporting for competitor resumes (#578)."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .apply.antibot import raise_for_antibot
from .browser import HH_BASE_URL, goto_hh, require_authenticated_page, resume_identity_matches
from .search import parse_salary
from .selector_groups import competitor_resume as sel

SEARCH_URL = f"{HH_BASE_URL}/search/resume"
RENDER_TIMEOUT_MS = 30_000
FULL_PAGE_RENDER_TIMEOUT_MS = 5_000
ITEMS_PER_PAGE = 100

# hh.ru `pos` — область текстового поиска по резюме. Все три значения замерены
# на живой выдаче 26.08 запросом «AI»:
#   position  ->  619 резюме — только желаемая должность (заголовок резюме);
#                 топ: AI-инженер, AI Engineer, AI Creator
#   keywords  -> ~3800 резюме — по ключевым навыкам; уже подмешивает дизайнеров
#   full_text -> ~5000 резюме — вся анкета: должность, навыки, описание опыта,
#                 достижения; топ-роль «Графический дизайнер», ~81% мусора,
#                 потому что `.ai` — формат Adobe Illustrator в навыках
# Дефолт — `position`: он единственный не смешивает профессию с инструментом.
SEARCH_IN_VALUES = frozenset({"full_text", "position", "keywords"})
DEFAULT_SEARCH_IN = "position"
_TOTAL_RESULTS_RE = re.compile(
    r"(?:показали|найдено)\s+([\d\s\u00a0\u202f]+)\s+резюм",
    re.IGNORECASE,
)
_EMPLOYER_REGISTRATION_MARKER = "после регистрации работодателя"


class CompetitorSearchIndeterminate(RuntimeError):
    """The current page does not prove a valid resume-search result."""


class CompetitorResumeIndeterminate(RuntimeError):
    """The current page does not prove a valid competitor resume."""


@dataclass(frozen=True)
class CompetitorSearchCoverage:
    total_results: int | None
    available_pages: int | None
    employer_registration_required: bool
    observed_page_size: int | None = None
    requested_page_size: int = ITEMS_PER_PAGE


@dataclass(frozen=True)
class CompetitorSearchCard:
    resume_id: str
    resume_url: str
    desired_role: str
    rank: int
    area: str | None = None
    business_trips: str | None = None


@dataclass(frozen=True)
class CompetitorSkill:
    name: str
    proficiency: str | None = None


@dataclass(frozen=True)
class CompetitorResume:
    resume_id: str
    resume_url: str
    desired_role: str
    area: str | None = None
    relocation: str | None = None
    business_trips: str | None = None
    metro_station: str | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    salary_currency: str | None = None
    experience_months: int | None = None
    specializations: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)
    work_formats: list[str] = field(default_factory=list)
    skills: list[CompetitorSkill] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    education: list[str] = field(default_factory=list)
    experience_summary: str | None = None
    achievements: str | None = None

    def content_hash(self) -> str:
        payload = {
            key: value
            for key, value in self.__dict__.items()
            if key not in {"resume_id", "resume_url"}
        }
        payload["skills"] = [skill.__dict__ for skill in self.skills]
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def build_competitor_search_url(
    text: str,
    page_num: int,
    *,
    items_per_page: int = ITEMS_PER_PAGE,
    search_in: str = DEFAULT_SEARCH_IN,
) -> str:
    if not text.strip():
        raise ValueError("--text не может быть пустым")
    if page_num < 0:
        raise ValueError("page_num должен быть >= 0")
    if not 1 <= items_per_page <= ITEMS_PER_PAGE:
        raise ValueError(f"items_per_page должен быть от 1 до {ITEMS_PER_PAGE}")
    if search_in not in SEARCH_IN_VALUES:
        raise ValueError(f"search_in должен быть одним из {sorted(SEARCH_IN_VALUES)}")
    params = {
        "text": text,
        "pos": search_in,
        "logic": "normal",
        "exp_period": "all_time",
        "ored_clusters": "true",
        "order_by": "relevance",
        "search_period": "0",
        "items_on_page": str(items_per_page),
        "page": str(page_num),
    }
    return f"{SEARCH_URL}?{urlencode(params)}"


def parse_search_result_count(text: str) -> int | None:
    """Parse the result count rendered by hh.ru without guessing from cards."""
    match = _TOTAL_RESULTS_RE.search(text)
    if match is None:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def available_search_page_count(page: Page, current_page: int) -> int | None:
    """Return the largest page count explicitly exposed by pagination."""
    page_indices: set[int] = set()
    pages = page.locator(sel.PAGINATION_PAGE)
    for index in range(pages.count()):
        label = pages.nth(index).inner_text().strip()
        try:
            displayed_page = int(label)
        except ValueError:
            continue
        if displayed_page >= 1:
            page_indices.add(displayed_page - 1)

    links = page.locator(sel.PAGINATION_LINK)
    for index in range(links.count()):
        href = links.nth(index).get_attribute("href") or ""
        raw_page = parse_qs(urlsplit(href).query).get("page", [])
        try:
            if raw_page:
                page_indices.add(int(raw_page[0]))
        except ValueError:
            continue

    if page_indices:
        return max(page_indices | {current_page}) + 1
    if (
        page.locator(sel.PAGINATION_BLOCK).count() == 0
        and page.locator(sel.PAGINATION_NEXT).count() == 0
    ):
        return 1
    return None


def inspect_search_coverage(
    page: Page,
    current_page: int = 0,
    *,
    observed_page_size: int | None = None,
    requested_page_size: int = ITEMS_PER_PAGE,
) -> CompetitorSearchCoverage:
    """Read best-effort coverage metadata from the search page."""
    try:
        text = page.locator(sel.SEARCH_MAIN).inner_text()
    except PlaywrightError:
        text = ""
    try:
        available_pages = available_search_page_count(page, current_page)
    except PlaywrightError:
        available_pages = None
    return CompetitorSearchCoverage(
        total_results=parse_search_result_count(text),
        available_pages=available_pages,
        employer_registration_required=_EMPLOYER_REGISTRATION_MARKER in text.casefold(),
        observed_page_size=observed_page_size,
        requested_page_size=requested_page_size,
    )


def coverage_warning(coverage: CompetitorSearchCoverage) -> str | None:
    """Explain when hh.ru exposes fewer cards than its headline result count."""
    warnings: list[str] = []
    page_size = coverage.observed_page_size or coverage.requested_page_size
    if coverage.observed_page_size and coverage.observed_page_size < coverage.requested_page_size:
        warnings.append(
            f"запрошено items_on_page={coverage.requested_page_size}, фактически hh.ru вернул "
            f"{coverage.observed_page_size} карточек на первой странице"
        )
    if coverage.total_results is None or coverage.available_pages is None:
        return "; ".join(warnings) or None
    visible_capacity = coverage.available_pages * page_size
    if coverage.total_results <= visible_capacity:
        return "; ".join(warnings) or None
    suffix = (
        "; остальные hh.ru показывает после регистрации работодателя"
        if coverage.employer_registration_required
        else ""
    )
    warnings.append(
        f"hh.ru сообщает {coverage.total_results} резюме, но текущей сессии "
        f"доступно не более {visible_capacity} "
        f"({coverage.available_pages} стр. x {page_size}){suffix}"
    )
    return "; ".join(warnings)


def _resume_identity(href: str) -> tuple[str, str] | None:
    path = urlsplit(href).path.rstrip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2 or parts[0] != "resume" or not parts[1]:
        return None
    resume_id = parts[1]
    return resume_id, f"{HH_BASE_URL}/resume/{resume_id}"


def parse_search_links(
    rows: list[tuple[str, str]], *, rank_offset: int = 0
) -> list[CompetitorSearchCard]:
    """Parse already-observed (href, title) pairs without keeping personal data."""
    result: list[CompetitorSearchCard] = []
    seen: set[str] = set()
    for href, title in rows:
        identity = _resume_identity(href)
        role = title.strip()
        if identity is None or not role or identity[0] in seen:
            continue
        seen.add(identity[0])
        result.append(
            CompetitorSearchCard(
                resume_id=identity[0],
                resume_url=identity[1],
                desired_role=role,
                rank=rank_offset + len(result) + 1,
            )
        )
    return result


_GEO_EMPTY_VALUES = frozenset({"—", "-", "не указано", "не указана", "не указан"})
_BUSINESS_TRIPS_RE = re.compile(
    r"(?:не\s+)?(?:готов(?:а|ы)?|готов)\s+к\s+(?:\S+\s+)?командировкам(?:\s+[^,]+)?"
    r"|(?:not\s+)?prepared\s+for\s+business\s+trips",
    re.IGNORECASE,
)
_METRO_RE = re.compile(r"(?:\bм\.\s*|\bметро\s+)([^,;]+)", re.IGNORECASE)


def _clean_geo_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace("\u00a0", " ").replace("\u202f", " ").strip()
    return None if not normalized or normalized.casefold() in _GEO_EMPTY_VALUES else normalized


def parse_search_area_and_business_trips(value: str | None) -> tuple[str | None, str | None]:
    """Split the single area/travel field rendered in a resume search card."""
    normalized = _clean_geo_value(value)
    if normalized is None:
        return None, None
    parts = [part.strip() for part in re.split(r"\s*[•·]\s*", normalized) if part.strip()]
    if not parts:
        return None, None
    trip = next((part for part in parts[1:] if _BUSINESS_TRIPS_RE.search(part)), None)
    if trip is None and _BUSINESS_TRIPS_RE.search(parts[0]):
        trip = parts[0]
        area = None
    else:
        area = _clean_geo_value(parts[0])
    return area, _clean_geo_value(trip)


def parse_detail_business_trips_and_metro(value: str | None) -> tuple[str | None, str | None]:
    """Read optional travel/metro text from the public resume header."""
    normalized = _clean_geo_value(value)
    if normalized is None:
        return None, None
    trip_match = _BUSINESS_TRIPS_RE.search(normalized)
    trip = _clean_geo_value(trip_match.group(0)) if trip_match else None
    metro_match = _METRO_RE.search(normalized)
    metro = _clean_geo_value(metro_match.group(1)) if metro_match else None
    return trip, metro


def parse_search_page(
    page: Page,
    *,
    rank_offset: int = 0,
    expected_page_size: int = ITEMS_PER_PAGE,
    require_authentication: bool = True,
) -> list[CompetitorSearchCard]:
    raise_for_antibot(page)
    if require_authentication:
        require_authenticated_page(page)
    links = page.locator(sel.SEARCH_RESULT_LINK)
    try:
        links.first.wait_for(state="attached", timeout=RENDER_TIMEOUT_MS)
    except PlaywrightError:
        # Applicant search currently has no stable empty data-qa across layouts.
        # Accept an explicit zero-result phrase only; every other blank page is unknown.
        main_text = page.locator("main").inner_text().casefold()
        if "резюме не найден" in main_text or "ничего не найден" in main_text:
            return []
        raise CompetitorSearchIndeterminate(
            "карточки резюме или подтверждённый empty-state не появились"
        ) from None

    # hh.ru can attach the first 20 links before finishing the requested
    # 100-card page. Reading count() immediately races that incremental render.
    # A short wait for the requested last item keeps full pages complete while
    # still allowing a genuinely short final page after the timeout.
    try:
        links.nth(expected_page_size - 1).wait_for(
            state="attached", timeout=FULL_PAGE_RENDER_TIMEOUT_MS
        )
    except PlaywrightError:
        pass

    observed: list[tuple[str, str]] = []
    for index in range(links.count()):
        link = links.nth(index)
        observed.append((link.get_attribute("href") or "", link.inner_text()))
    cards = parse_search_links(observed, rank_offset=rank_offset)
    if not cards:
        raise CompetitorSearchIndeterminate("ссылки выдачи не содержат подтверждённых resume ID")

    # The search card already exposes the area and business-trip readiness.
    # Keep this optional: the title link remains the identity-bearing fallback
    # for layouts that omit the newer card data-qa markers.
    search_cards = page.locator(sel.SEARCH_CARD)
    if search_cards.count() == 0:
        return cards
    geo_by_resume: dict[str, tuple[str | None, str | None]] = {}
    for index in range(search_cards.count()):
        card = search_cards.nth(index)
        title_link = card.locator(sel.SEARCH_RESULT_TITLE_LINK)
        if title_link.count() != 1:
            continue
        identity = _resume_identity(title_link.get_attribute("href") or "")
        if identity is None:
            continue
        area_locator = card.locator(sel.SEARCH_AREA_AND_RELOCATION)
        area_text = area_locator.inner_text() if area_locator.count() == 1 else None
        geo_by_resume[identity[0]] = parse_search_area_and_business_trips(area_text)
    return [
        replace(
            card,
            area=geo_by_resume.get(card.resume_id, (None, None))[0],
            business_trips=geo_by_resume.get(card.resume_id, (None, None))[1],
        )
        for card in cards
    ]


def has_next_search_page(page: Page, page_num: int) -> bool:
    next_links = page.locator(sel.PAGINATION_NEXT)
    pages = page.locator(sel.PAGINATION_PAGE)
    links = page.locator(sel.PAGINATION_LINK)
    pagination = page.locator(sel.PAGINATION_BLOCK)
    if pagination.count() == 0:
        if links.count() == 0 and next_links.count() == 0:
            # Cards can render before either pagination representation. Wait
            # once for the block or fallback links before declaring page end.
            try:
                page.locator(f"{sel.PAGINATION_BLOCK}, {sel.PAGINATION_LINK}").first.wait_for(
                    state="attached", timeout=RENDER_TIMEOUT_MS
                )
            except PlaywrightError:
                return False
            pagination = page.locator(sel.PAGINATION_BLOCK)
            links = page.locator(sel.PAGINATION_LINK)
        if pagination.count() == 0:
            # Current applicant layout may render numbered links without data-qa.
            return _has_next_search_link(next_links, page_num) or _has_next_search_link(
                links, page_num
            )

    if pages.count() == 0 and links.count() == 0:
        try:
            page.locator(f"{sel.PAGINATION_PAGE}, {sel.PAGINATION_LINK}").first.wait_for(
                state="attached", timeout=RENDER_TIMEOUT_MS
            )
        except PlaywrightError:
            raise CompetitorSearchIndeterminate(
                f"пагинация страницы поиска резюме {page_num} не подтверждена: "
                f"маркер страницы не появился за {RENDER_TIMEOUT_MS} мс"
            ) from None
        if pages.count() == 0 and links.count() == 0:
            raise CompetitorSearchIndeterminate(
                f"пагинация страницы поиска резюме {page_num} не подтверждена: "
                "маркер страницы исчез после ожидания"
            )

    for index in range(pages.count()):
        label = pages.nth(index).inner_text().strip()
        try:
            if int(label) > page_num + 1:
                return True
        except ValueError:
            continue
    return _has_next_search_link(next_links, page_num) or _has_next_search_link(links, page_num)


def _has_next_search_link(links, page_num: int) -> bool:
    for index in range(links.count()):
        href = links.nth(index).get_attribute("href") or ""
        raw_page = parse_qs(urlsplit(href).query).get("page", [])
        try:
            if raw_page and int(raw_page[0]) > page_num:
                return True
        except ValueError:
            continue
    return False


# hh.ru рендерит подписи разделов на языке, которым соискатель ЗАПОЛНЯЛ анкету,
# а не по локали браузера: `browser.py` ставит locale=ru-RU и <html lang="ru">,
# но резюме, созданное в английской версии, всё равно приходит с «Work experience»
# вместо «Опыт работы». Переключить нельзя — ни ?locale=RU, ни ?lang=RU не
# действуют (проверено на живых страницах 26.08). Разбор по одним русским
# подписям молча терял ВСЕ секции такого резюме: в базе оседали
# specializations='[]', experience_months=NULL, 0 навыков при полной странице.
# Потеря была смещена в самый технический сегмент (Machine Learning 34%,
# Data Scientist 18%, «промпт инженер» 0%), потому что англоязычную анкету
# заполняют в основном международные ИТ-специалисты.
#
# Каждая секция описана парой подписей RU/EN. Ключ разбора — русская подпись:
# она остаётся каноническим именем внутри парсера, а `_section()` через
# `_SECTION_ALIASES` сопоставляет ей английский вариант. Так двуязычность
# не расползается по коду отдельными ветками if/else на каждую секцию.
_SECTION_ALIASES = {
    "Skills": "Навыки",
    "Driving experience": "Опыт вождения",
    "Education": "Образование",
    "Languages": "Знание языков",
    "Citizenship, travel time to work": "Гражданство, время в пути до работы",
    "About me": "Обо мне",
    "Key achievements": "Ключевые достижения",
    "Specific achievements": "Конкретные достижения",
}
_SECTION_HEADINGS = {
    "Навыки",
    "Опыт вождения",
    "Образование",
    "Знание языков",
    "Гражданство, время в пути до работы",
    "Обо мне",
    "Ключевые достижения",
    "Конкретные достижения",
    *_SECTION_ALIASES,
}
_PROFICIENCY_ALIASES = {
    "Basic level": "Базовый уровень",
    "Beginner level": "Начальный уровень",
    "Medium level": "Средний уровень",
    "Advanced level": "Продвинутый уровень",
    "Level not specified": "Уровень не указан",
}
_PROFICIENCY = {
    "Базовый уровень",
    "Начальный уровень",
    "Средний уровень",
    "Продвинутый уровень",
    "Уровень не указан",
    *_PROFICIENCY_ALIASES,
}
# Строчные подписи-префиксы: у них после двоеточия идёт CSV-хвост (_csv_tail),
# либо они открывают перечисление (специализации).
_SPECIALIZATIONS_PREFIXES = ("Специализации:", "Specializations:")
_EMPLOYMENT_PREFIXES = ("Тип занятости:", "Employment type:")
_FORMAT_PREFIXES = ("Формат работы:", "Work format:")
_EXPERIENCE_PREFIXES = ("Опыт работы", "Work experience")
_CITIZENSHIP_PREFIXES = ("Гражданство", "Citizenship")
_SKILL_LEVELS_CAPTION = ("Уровни владения навыками", "Skill proficiency levels")
_SALARY_HEADING_RE = re.compile(
    r"\d.*(?:₽|\$|€|руб(?:\.|лей)?|RUB|USD|EUR|KZT|тенге|Br\b|so['’ʼʻ]m|сом|на руки)",
    re.IGNORECASE,
)


def redact_free_text(value: str) -> str | None:
    """Compatibility helper: preserve collected free text without filtering."""
    text = value.strip()
    return text or None


def _months(text: str) -> int | None:
    # «5 лет 3 месяца» и «9 years 11 months» — один разбор на обе локали.
    years = re.search(r"(\d+)\s+(?:год|года|лет|years?)", text, re.IGNORECASE)
    months = re.search(r"(\d+)\s+(?:месяц|months?)", text, re.IGNORECASE)
    if not years and not months:
        return None
    return (int(years.group(1)) * 12 if years else 0) + (int(months.group(1)) if months else 0)


def _csv_tail(line: str) -> list[str]:
    _, _, tail = line.partition(":")
    return [part.strip() for part in tail.split(",") if part.strip()]


def _section(lines: list[str], heading: str) -> list[str]:
    """Строки раздела до следующей подписи. Ищет раздел в обеих локалях."""
    aliases = [heading, *(en for en, ru in _SECTION_ALIASES.items() if ru == heading)]
    start = next(
        (lines.index(alias) + 1 for alias in aliases if alias in lines),
        None,
    )
    if start is None:
        return []
    result: list[str] = []
    for line in lines[start:]:
        if line in _SECTION_HEADINGS or line.startswith(_CITIZENSHIP_PREFIXES):
            break
        result.append(line)
    return result


def parse_competitor_resume_text(
    text: str,
    *,
    resume_id: str,
    resume_url: str,
    headings: list[str],
    desired_role: str,
) -> CompetitorResume:
    """Parse the applicant-visible main section, excluding header identity fields."""

    def normalize(value: str) -> str:
        return value.replace("\u00a0", " ").replace("\u2009", " ").replace("\u202f", " ").strip()

    lines = [normalize(line) for line in text.splitlines() if line.strip()]
    normalized_headings = [normalize(value) for value in headings]
    if not lines:
        raise CompetitorResumeIndeterminate("основной блок резюме пуст")

    def is_salary_heading(value: str) -> bool:
        return bool(_SALARY_HEADING_RE.search(normalize(value)))

    desired_role = normalize(desired_role)
    if not desired_role:
        raise CompetitorResumeIndeterminate("desired_role не подтверждён")

    salary_heading = next(
        (value for value in normalized_headings if is_salary_heading(value)), None
    )
    salary = parse_salary(salary_heading) if salary_heading else None
    experience_heading = next(
        (value for value in normalized_headings if value.startswith(_EXPERIENCE_PREFIXES)), ""
    )

    specializations: list[str] = []
    spec_index = next(
        (lines.index(prefix) for prefix in _SPECIALIZATIONS_PREFIXES if prefix in lines),
        None,
    )
    if spec_index is not None:
        for line in lines[spec_index + 1 :]:
            if line.startswith(_EMPLOYMENT_PREFIXES):
                break
            specializations.append(line.lstrip("— ").strip())

    employment = next((line for line in lines if line.startswith(_EMPLOYMENT_PREFIXES)), "")
    formats = next((line for line in lines if line.startswith(_FORMAT_PREFIXES)), "")

    skill_lines = _section(lines, "Навыки")
    skills: list[CompetitorSkill] = []
    proficiency: str | None = None
    for line in skill_lines:
        if line in _SKILL_LEVELS_CAPTION:
            continue
        if line in _PROFICIENCY:
            # Уровень нормализуем к русскому имени: значение уезжает в БД и
            # попадает в отчёты, где английский дубль расщепил бы бакет.
            proficiency = _PROFICIENCY_ALIASES.get(line, line)
            continue
        skill_name = line.strip()
        if skill_name:
            skills.append(CompetitorSkill(skill_name, proficiency))

    education = _section(lines, "Образование")
    languages = _section(lines, "Знание языков")
    about = redact_free_text("\n".join(_section(lines, "Обо мне")))
    achievements_lines = _section(lines, "Ключевые достижения") or _section(
        lines, "Конкретные достижения"
    )
    achievements = redact_free_text("\n".join(achievements_lines))

    return CompetitorResume(
        resume_id=resume_id,
        resume_url=resume_url,
        desired_role=desired_role,
        salary_from=salary.salary_from if salary else None,
        salary_to=salary.salary_to if salary else None,
        salary_currency=salary.currency if salary else None,
        experience_months=_months(experience_heading),
        specializations=specializations,
        employment_types=_csv_tail(employment),
        work_formats=_csv_tail(formats),
        skills=skills,
        languages=languages,
        education=education,
        experience_summary=about,
        achievements=achievements,
    )


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2)


def fetch_competitor_resume(
    page: Page,
    card: CompetitorSearchCard,
    *,
    require_authentication: bool = True,
) -> CompetitorResume:
    goto_hh(page, card.resume_url)
    raise_for_antibot(page)
    if require_authentication:
        require_authenticated_page(page)
    if not resume_identity_matches(page, card.resume_id):
        raise CompetitorResumeIndeterminate("identity открытого резюме не подтверждён")
    main = page.locator(sel.DETAIL_MAIN)
    try:
        main.wait_for(state="attached", timeout=RENDER_TIMEOUT_MS)
    except PlaywrightError:
        raise CompetitorResumeIndeterminate("основной блок резюме не появился") from None
    headings = [value.strip() for value in page.locator(sel.DETAIL_HEADING).all_inner_texts()]
    title_locator = page.locator(sel.DETAIL_TITLE_POSITION)
    if title_locator.count() != 1:
        raise CompetitorResumeIndeterminate("desired_role не подтверждён")
    snapshot = parse_competitor_resume_text(
        main.inner_text(),
        resume_id=card.resume_id,
        resume_url=card.resume_url,
        headings=headings,
        desired_role=title_locator.inner_text(),
    )
    address_locator = page.locator(sel.DETAIL_PERSONAL_ADDRESS)
    relocation_locator = page.locator(sel.DETAIL_RELOCATION)
    personal_locator = page.locator(sel.DETAIL_PERSONAL_INFO)
    detail_area = (
        _clean_geo_value(address_locator.inner_text()) if address_locator.count() == 1 else None
    )
    # The result card may include the parent region, e.g. ``Подольск
    # (Московская область)``; prefer that richer value over the detail-page
    # city-only label while retaining the detail fallback for older layouts.
    area = card.area or detail_area
    relocation = (
        _clean_geo_value(relocation_locator.inner_text())
        if relocation_locator.count() == 1
        else None
    )
    personal_text = personal_locator.inner_text() if personal_locator.count() == 1 else None
    business_trips, metro_station = parse_detail_business_trips_and_metro(personal_text)
    return replace(
        snapshot,
        area=area,
        relocation=relocation,
        business_trips=business_trips or card.business_trips,
        metro_station=metro_station,
    )


def report_competitors(rows: list[dict], *, top: int, limited_runs: int = 0) -> str:
    """Build a deterministic report from latest stored snapshots."""
    roles = Counter(str(row["desired_role"]) for row in rows if row.get("desired_role"))
    skills = Counter(
        skill["name"] for row in rows for skill in row.get("skills", []) if skill.get("name")
    )
    specializations = Counter(
        value for row in rows for value in row.get("specializations", []) if value
    )
    experience = [int(row["experience_months"]) for row in rows if row.get("experience_months")]
    salaries: dict[str, list[int]] = {}
    skill_pairs: Counter[tuple[str, str]] = Counter()
    for row in rows:
        amount = row.get("salary_to") or row.get("salary_from")
        currency = row.get("salary_currency")
        if amount and currency:
            salaries.setdefault(str(currency), []).append(int(amount))
        names = sorted({skill["name"] for skill in row.get("skills", []) if skill.get("name")})
        for index, first in enumerate(names):
            for second in names[index + 1 :]:
                skill_pairs[(first, second)] += 1

    lines = [f"Резюме в выборке: {len(rows)}"]
    if limited_runs:
        lines.append(f"Внимание: ограниченных запусков (--max-pages): {limited_runs}")

    def add_counter(title: str, values: Counter) -> None:
        lines.append(f"\n{title}:")
        if not values:
            lines.append("  (нет данных)")
            return
        for value, count in values.most_common(top):
            lines.append(f"  {count}  {value}")

    add_counter("Частые роли", roles)
    add_counter("Частые специализации", specializations)
    add_counter("Частые навыки", skills)
    lines.append("\nЧастые сочетания навыков:")
    if skill_pairs:
        for (first, second), count in skill_pairs.most_common(top):
            lines.append(f"  {count}  {first} + {second}")
    else:
        lines.append("  (нет данных)")
    if experience:
        lines.append(f"\nМедианный опыт: {_median(experience)} мес.")
    if salaries:
        lines.append("\nЗарплата (медиана верхней границы, иначе нижней):")
        for currency, amounts in sorted(salaries.items()):
            lines.append(f"  {currency}: {_median(amounts)} (n={len(amounts)})")

    lines.extend(
        [
            "\nРекомендации для AIProfile:",
            "  Используйте наблюдаемые названия ролей и навыков как поисковую лексику.",
            "  Добавляйте навык только если он подтверждён вашим реальным опытом.",
            "Рекомендации для cover letter:",
            "  Выберите 3-5 подтверждённых навыков, совпадающих с конкретной вакансией.",
            "  Подкрепите их собственным результатом; не копируйте факты из чужих резюме.",
        ]
    )
    return "\n".join(lines)

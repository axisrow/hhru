"""Read-only collection and deterministic reporting for competitor resumes (#578)."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .apply.antibot import raise_for_antibot
from .browser import HH_BASE_URL, goto_hh, require_authenticated_page, resume_identity_matches
from .search import parse_salary
from .selector_groups import competitor_resume as sel

SEARCH_URL = f"{HH_BASE_URL}/search/resume"
RENDER_TIMEOUT_MS = 30_000


class CompetitorSearchIndeterminate(RuntimeError):
    """The current page does not prove a valid resume-search result."""


class CompetitorResumeIndeterminate(RuntimeError):
    """The current page does not prove a valid competitor resume."""


@dataclass(frozen=True)
class CompetitorSearchCard:
    resume_id: str
    resume_url: str
    desired_role: str
    rank: int


@dataclass(frozen=True)
class CompetitorSkill:
    name: str
    proficiency: str | None = None


@dataclass(frozen=True)
class CompetitorResume:
    resume_id: str
    resume_url: str
    desired_role: str
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


def build_competitor_search_url(text: str, page_num: int) -> str:
    if not text.strip():
        raise ValueError("--text не может быть пустым")
    if page_num < 0:
        raise ValueError("page_num должен быть >= 0")
    params = {
        "text": text,
        "pos": "full_text",
        "logic": "normal",
        "exp_period": "all_time",
        "ored_clusters": "true",
        "order_by": "relevance",
        "search_period": "0",
        "page": str(page_num),
    }
    return f"{SEARCH_URL}?{urlencode(params)}"


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


def parse_search_page(page: Page, *, rank_offset: int = 0) -> list[CompetitorSearchCard]:
    raise_for_antibot(page)
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

    observed: list[tuple[str, str]] = []
    for index in range(links.count()):
        link = links.nth(index)
        observed.append((link.get_attribute("href") or "", link.inner_text()))
    cards = parse_search_links(observed, rank_offset=rank_offset)
    if not cards:
        raise CompetitorSearchIndeterminate("ссылки выдачи не содержат подтверждённых resume ID")
    return cards


def has_next_search_page(page: Page, page_num: int) -> bool:
    if page.locator(sel.PAGINATION_NEXT).count() > 0:
        return True
    pages = page.locator(sel.PAGINATION_PAGE)
    for index in range(pages.count()):
        label = pages.nth(index).inner_text().strip()
        try:
            if int(label) > page_num + 1:
                return True
        except ValueError:
            continue
    # Current applicant layout may render numbered links without data-qa.
    links = page.locator(sel.PAGINATION_LINK)
    for index in range(links.count()):
        href = links.nth(index).get_attribute("href") or ""
        raw_page = parse_qs(urlsplit(href).query).get("page", [])
        try:
            if raw_page and int(raw_page[0]) > page_num:
                return True
        except ValueError:
            continue
    return False


_SECTION_HEADINGS = {
    "Навыки",
    "Опыт вождения",
    "Образование",
    "Знание языков",
    "Гражданство, время в пути до работы",
    "Обо мне",
    "Ключевые достижения",
    "Конкретные достижения",
}
_PROFICIENCY = {
    "Базовый уровень",
    "Начальный уровень",
    "Средний уровень",
    "Продвинутый уровень",
    "Уровень не указан",
}
_SALARY_HEADING_RE = re.compile(
    r"\d.*(?:₽|\$|€|руб(?:\.|лей)?|RUB|USD|EUR|KZT|тенге|на руки)", re.IGNORECASE
)
_CONTACT_RE = re.compile(
    r"(?:[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}|https?://\S+|www\.\S+|@[A-Za-z0-9_.-]+|"
    r"(?:\+?\d[\d\s().-]{8,}\d))",
    re.IGNORECASE,
)
_NAME_LIKE_RE = re.compile(
    r"(?:\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\b|\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\b)"
)


def redact_free_text(value: str) -> str | None:
    """Remove contact tokens; drop text that still looks like a person's name."""
    text = _CONTACT_RE.sub("[redacted]", value).strip()
    if not text or "меня зовут" in text.casefold() or _NAME_LIKE_RE.search(text):
        return None
    return text


def _months(text: str) -> int | None:
    years = re.search(r"(\d+)\s+(?:год|года|лет)", text, re.IGNORECASE)
    months = re.search(r"(\d+)\s+месяц", text, re.IGNORECASE)
    if not years and not months:
        return None
    return (int(years.group(1)) * 12 if years else 0) + (int(months.group(1)) if months else 0)


def _csv_tail(line: str) -> list[str]:
    _, _, tail = line.partition(":")
    return [part.strip() for part in tail.split(",") if part.strip()]


def _section(lines: list[str], heading: str) -> list[str]:
    try:
        start = lines.index(heading) + 1
    except ValueError:
        return []
    result: list[str] = []
    for line in lines[start:]:
        if line in _SECTION_HEADINGS or line.startswith("Гражданство"):
            break
        result.append(line)
    return result


def parse_competitor_resume_text(
    text: str,
    *,
    resume_id: str,
    resume_url: str,
    headings: list[str],
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

    desired_candidates = [
        value.strip()
        for value in normalized_headings
        if value.strip()
        and value.strip() not in _SECTION_HEADINGS
        and not value.strip().startswith("Опыт работы")
        and not is_salary_heading(value.strip())
    ]
    if not desired_candidates:
        raise CompetitorResumeIndeterminate("desired_role не подтверждён")
    desired_role = desired_candidates[0]

    salary_heading = next(
        (value for value in normalized_headings if is_salary_heading(value)), None
    )
    salary = parse_salary(salary_heading) if salary_heading else None
    experience_heading = next(
        (value for value in normalized_headings if value.startswith("Опыт работы")), ""
    )

    specializations: list[str] = []
    if "Специализации:" in lines:
        start = lines.index("Специализации:") + 1
        for line in lines[start:]:
            if line.startswith("Тип занятости:"):
                break
            specializations.append(line.lstrip("— ").strip())

    employment = next((line for line in lines if line.startswith("Тип занятости:")), "")
    formats = next((line for line in lines if line.startswith("Формат работы:")), "")

    skill_lines = _section(lines, "Навыки")
    skills: list[CompetitorSkill] = []
    proficiency: str | None = None
    for line in skill_lines:
        if line == "Уровни владения навыками":
            continue
        normalized_level = line.replace("не указан", "не указан")
        if normalized_level in _PROFICIENCY:
            proficiency = normalized_level
            continue
        skills.append(CompetitorSkill(line, proficiency))

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


def fetch_competitor_resume(page: Page, card: CompetitorSearchCard) -> CompetitorResume:
    goto_hh(page, card.resume_url)
    raise_for_antibot(page)
    require_authenticated_page(page)
    if not resume_identity_matches(page, card.resume_id):
        raise CompetitorResumeIndeterminate("identity открытого резюме не подтверждён")
    main = page.locator(sel.DETAIL_MAIN)
    try:
        main.wait_for(state="attached", timeout=RENDER_TIMEOUT_MS)
    except PlaywrightError:
        raise CompetitorResumeIndeterminate("основной блок резюме не появился") from None
    headings = [value.strip() for value in page.locator(sel.DETAIL_HEADING).all_inner_texts()]
    return parse_competitor_resume_text(
        main.inner_text(),
        resume_id=card.resume_id,
        resume_url=card.resume_url,
        headings=headings,
    )


def report_competitors(rows: list[dict], *, top: int, limited_runs: int = 0) -> str:
    """Build a deterministic, PII-free report from latest stored snapshots."""
    roles = Counter(str(row["desired_role"]) for row in rows if row.get("desired_role"))
    skills = Counter(
        skill["name"]
        for row in rows
        for skill in row.get("skills", [])
        if skill.get("name")
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
        ordered = sorted(experience)
        lines.append(f"\nМедианный опыт: {ordered[len(ordered) // 2]} мес.")
    if salaries:
        lines.append("\nЗарплата (медиана верхней границы, иначе нижней):")
        for currency, amounts in sorted(salaries.items()):
            ordered = sorted(amounts)
            lines.append(f"  {currency}: {ordered[len(ordered) // 2]} (n={len(ordered)})")

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

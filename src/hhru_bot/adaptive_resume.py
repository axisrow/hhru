"""LLM-генерация содержимого резюме, адаптированного под кластер вакансий (#753).

Часть эпика #750 (адаптивные резюме: пул под кластеры). Зависимость A1 (#751,
``config_sections/candidate_facts.py``) уже даёт структурированные, помеченные
тегами факты о кандидате; A2 (#752) зафиксировал четыре кластера
(``resume_clusters.py``). Этот модуль — sub-issue A3: по кластеру и
``CandidateFacts`` собрать содержимое адаптированного резюме.

**Глубина адаптации (решено в эпике #750, не пересматривается здесь):**
переписываются формулировки (заголовок, «обо мне», порядок/акценты навыков),
отбираются нерелевантные места работы и проекты. Компании, даты, образование
НЕ выдумываются — только фильтруются/сокращаются существующие факты.

**Политика отбора фактов (открытый вопрос issue #753, решение зафиксировано
здесь):** нерелевантное место работы СОКРАЩАЕТСЯ до одной строки, а не
скрывается целиком. Скрытие меняет видимый стаж и создаёт пробел в датах —
на резюме это выглядит как необъяснённый перерыв и провоцирует ровно тот
вопрос от работодателя, которого политика должна избегать (сам issue называет
это риском). Сокращение сохраняет правдивую хронологию (тот же принцип, что
и общий запрет выдумывать даты/компании) и просто убирает акцент — размер
описания, а не факт наличия записи, несёт признак релевантности. Функция
``_select_experience`` реализует это: релевантные записи (пересечение
``tags``) проходят как есть, нерелевантные — с description, урезанным до
первого предложения (``_shorten_to_one_line``).

Сама генерация idet тем же путём, что и остальные разделы (``about.py``,
``experience.py``, ``resume_sections.py``): один вызов ``LLMClient.chat()``
со строгим JSON-контрактом, fail-closed парсинг, детерминированный fallback
без LLM (никогда не выдумывает факты, только реордеринг/усечение того, что
уже есть в ``CandidateFacts``). Промпт получает уже урезанный по политике выше
набор фактов — LLM не решает, что скрывать, только переписывает формулировки.

Эта команда/модуль намеренно НЕ пишет ничего на hh.ru (issue #753 — PR-1 среза
"генерация + --dry-run"; применение существующими ``edit_*_on_hh`` — отдельный
follow-up PR-2, см. тело issue "Оценка"/"Риск превышения лимита").
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .resume_clusters import ResumeCluster

if TYPE_CHECKING:
    from .config_sections.candidate_facts import (
        CandidateFacts,
        ProjectFact,
        WorkExperienceFact,
    )

logger = logging.getLogger("hhru_bot.adaptive_resume")

# Общий с about.py/experience.py масштаб — консистентность промптов проекта,
# не самостоятельное решение (about.py:94, resume_sections.py:153 — та же
# неоднородность, зафиксированная в теле issue #753 как "не чинить чужое").
TEMPERATURE = 0.4
MAX_TOKENS = 900


@dataclass(frozen=True)
class AdaptiveResumeContent:
    """Итог генерации: то, что показывает ``--dry-run`` и что применит PR-2."""

    cluster_key: str
    title: str
    about: str
    skills: tuple[str, ...]
    work_experience: tuple[str, ...]
    projects: tuple[str, ...]
    source: str  # "llm" | "fallback"
    hidden_note: str = ""


def _fact_tags(fact: Any) -> set[str]:
    return set(getattr(fact, "tags", None) or ())


def _is_relevant(fact: Any, cluster: ResumeCluster) -> bool:
    tags = _fact_tags(fact)
    if not tags:
        # Пустой tags — факт релевантен всем кластерам (контракт #751:
        # "кластеризация ещё не проведена" не должен молча стирать запись).
        return True
    return bool(tags & set(cluster.tags))


def _shorten_to_one_line(description: str) -> str:
    """Первое предложение исходного описания — политика 'сократить, не скрыть'.

    Не выдумывает новый текст (запрет issue #750): берёт только префикс уже
    существующего ``description``. Ищет первую границу предложения среди
    '.', '!', '?', перевода строки; если границы нет — весь текст короткий
    сам по себе и возвращается как есть (не обрезаем на произвольном месте).
    """
    text = description.strip()
    if not text:
        return ""
    match = re.search(r"[.!?\n]", text)
    if match is None:
        return text
    return text[: match.start() + 1].strip()


def _select_experience(
    entries: tuple[WorkExperienceFact, ...] | list[WorkExperienceFact],
    cluster: ResumeCluster,
) -> list[WorkExperienceFact]:
    """Релевантные записи — как есть; нерелевантные — description в одну строку.

    Ни одна запись не удаляется (see module docstring: политика "сократить,
    не скрыть" ради непрерывной видимой хронологии дат).
    """
    from dataclasses import replace

    result = []
    for entry in entries:
        if _is_relevant(entry, cluster):
            result.append(entry)
        else:
            result.append(replace(entry, description=_shorten_to_one_line(entry.description)))
    return result


def _select_projects(
    entries: tuple[ProjectFact, ...] | list[ProjectFact],
    cluster: ResumeCluster,
) -> list[ProjectFact]:
    """Проекты, в отличие от мест работы, не несут дат — нерелевантные просто
    не включаются (нет риска "пробела в хронологии", который мотивирует
    политику сокращения у work_experience)."""
    return [p for p in entries if _is_relevant(p, cluster)]


def _ordered_skills(
    work_experience: list[WorkExperienceFact],
    projects: list[ProjectFact],
    cluster: ResumeCluster,
) -> list[str]:
    """Навыки кластера — вперёд, остальные — за ними; дубликаты убраны с
    сохранением первого порядка появления (без выдумывания новых навыков)."""
    seen: dict[str, None] = {}
    for entry in (*work_experience, *projects):
        for skill in getattr(entry, "skills", None) or ():
            seen.setdefault(skill, None)
    keyword_set = {k.casefold() for k in cluster.keywords}

    def _rank(skill: str) -> tuple[int, int]:
        matched = skill.casefold() in keyword_set or any(
            kw.casefold() in skill.casefold() for kw in cluster.keywords
        )
        return (0 if matched else 1, 0)

    ordered = sorted(seen.keys(), key=_rank)
    return ordered


def _facts_summary(facts: CandidateFacts, cluster: ResumeCluster) -> dict[str, Any]:
    work = _select_experience(facts.work_experience, cluster)
    projects = _select_projects(facts.projects, cluster)
    return {
        "cluster": cluster.title,
        "cluster_keywords": list(cluster.keywords),
        "work_experience": [
            {
                "company": w.company,
                "position": w.position,
                "period": f"{w.period_from}–{w.period_to}".strip("–"),
                "description": w.description,
                "relevant": _is_relevant(w, cluster),
            }
            for w in work
        ],
        "projects": [
            {"name": p.name, "description": p.description, "skills": p.skills} for p in projects
        ],
        "skills": _ordered_skills(work, projects, cluster),
    }


def build_prompt(facts: CandidateFacts, cluster: ResumeCluster) -> list[dict[str, str]]:
    """Строгий JSON-only промпт. Факты уже отобраны/урезаны до вызова LLM —
    модель только переписывает формулировки, не решает, что показать (#750:
    отбор фактов — детерминированная логика проекта, а не решение модели)."""
    system = (
        "Ты адаптируешь содержимое резюме кандидата под конкретный кластер вакансий "
        "на hh.ru. Отвечай только JSON-объектом с ключами title, about, work_experience, "
        "projects — где work_experience и projects — списки строк описаний в том же "
        "порядке и количестве, что во входных данных (только переформулируй description, "
        "не меняй компанию/должность/даты и не добавляй новые записи). "
        "Не выдумывай факты, компании, даты, метрики или навыки, которых нет во входных "
        "данных. Пиши по-русски, профессионально, без эмодзи."
    )
    summary = _facts_summary(facts, cluster)
    user = (
        f"Кластер: {cluster.title}. Характерные термины кластера: "
        f"{', '.join(cluster.keywords)}.\n"
        f"Факты кандидата (JSON):\n{json.dumps(summary, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_response(content: str | None, facts_summary: dict[str, Any]) -> dict[str, Any] | None:
    if not content or not content.strip():
        return None
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        raw = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    about = raw.get("about")
    work = raw.get("work_experience")
    projects = raw.get("projects")
    if not isinstance(title, str) or not isinstance(about, str):
        return None
    if not isinstance(work, list) or not all(isinstance(v, str) for v in work):
        return None
    if not isinstance(projects, list) or not all(isinstance(v, str) for v in projects):
        return None
    # Модель обязана сохранить число записей — иначе неясно, какому факту
    # соответствует какая переформулировка (fail-closed: не гадаем маппинг).
    if len(work) != len(facts_summary["work_experience"]):
        return None
    if len(projects) != len(facts_summary["projects"]):
        return None
    if not title.strip() or not about.strip():
        return None
    return {"title": title.strip(), "about": about.strip(), "work": work, "projects": projects}


def _fallback_content(facts: CandidateFacts, cluster: ResumeCluster) -> AdaptiveResumeContent:
    """Без LLM: не переформулирует текст, только реордеринг/усечение —
    те же структурные решения (_select_experience/_ordered_skills), что и
    LLM-путь, применённые напрямую к существующим строкам."""
    work = _select_experience(facts.work_experience, cluster)
    projects = _select_projects(facts.projects, cluster)
    skills = _ordered_skills(work, projects, cluster)
    title = cluster.title
    about = (
        f"Специализация: {cluster.title}. Ключевые навыки: {', '.join(skills[:6])}."
        if skills
        else f"Специализация: {cluster.title}."
    )
    return AdaptiveResumeContent(
        cluster_key=cluster.key,
        title=title,
        about=about,
        skills=tuple(skills),
        work_experience=tuple(w.description for w in work),
        projects=tuple(p.description for p in projects),
        source="fallback",
    )


def generate_adaptive_resume(
    llm_client: Any | None,
    facts: CandidateFacts,
    cluster: ResumeCluster,
) -> AdaptiveResumeContent:
    """Единственная точка входа генерации. Fail-closed: отсутствующий клиент
    (нет секции ``ai`` в конфиге), любой сбой LLM или невалидный ответ уходят
    в детерминированный fallback, никогда в пустой/испорченный контент
    (issue #753 "Ограничения")."""
    facts_summary = _facts_summary(facts, cluster)
    if llm_client is None:
        return _fallback_content(facts, cluster)
    if not facts_summary["work_experience"] and not facts_summary["projects"]:
        logger.warning("Нет фактов кандидата для кластера %s — используется fallback", cluster.key)
        return _fallback_content(facts, cluster)
    try:
        response = llm_client.chat(
            build_prompt(facts, cluster), temperature=TEMPERATURE, max_tokens=MAX_TOKENS
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, must not crash a dry-run
        logger.warning("Adaptive resume generation failed: %s", exc)
        return _fallback_content(facts, cluster)
    parsed = _parse_response(getattr(response, "content", None), facts_summary)
    if parsed is None:
        logger.warning("LLM вернул невалидный JSON для адаптивного резюме кластера %s", cluster.key)
        return _fallback_content(facts, cluster)
    work_entries = facts_summary["work_experience"]
    hidden_count = sum(1 for w in work_entries if not w["relevant"])
    hidden_note = (
        f"{hidden_count} мест(о/а) работы сокращены до одной строки как нерелевантные "
        f"кластеру «{cluster.title}» (не скрыты — политика #753)."
        if hidden_count
        else ""
    )
    return AdaptiveResumeContent(
        cluster_key=cluster.key,
        title=parsed["title"],
        about=parsed["about"],
        skills=tuple(facts_summary["skills"]),
        work_experience=tuple(parsed["work"]),
        projects=tuple(parsed["projects"]),
        source="llm",
        hidden_note=hidden_note,
    )


__all__ = [
    "AdaptiveResumeContent",
    "build_prompt",
    "generate_adaptive_resume",
]

"""Typed planning and UI editing for supported additional resume sections."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import (
    HH_BASE_URL,
    RESUME_UNAVAILABLE_REASON,
    PageStateIndeterminate,
    goto_hh,
    has_auth_cookie,
    has_login_form,
    has_resume_error_banner,
    labelled_field,
    open_hydrated_resume_editor,
)

if TYPE_CHECKING:
    from .config_sections.ai_profile import AIProfile
    from .config_sections.resume_sections import ResumeSectionsConfig

logger = logging.getLogger("hhru_bot.resume_sections")
FORM_TIMEOUT_MS = 10_000

# Потолок ожидания закрытия inline-редактора после save (#331).
SAVE_TIMEOUT_MS = 30_000

RESUME_EDIT_BUTTON = {
    "attestations": "[data-qa^='resume-edit-button-attestationEducation-']",
    "recommendations": "[data-qa^='resume-edit-button-recommendation-']",
    # Census 2026-09-12 (#1120): кнопки строк блока сертификатов индексируются
    # так же, как attestation/recommendation.
    "certificates": "[data-qa^='resume-edit-button-certificate-']",
}
# Both section editors embed the resume id in their route, so both patterns are
# bound per-call rather than kept static: a stale or misdirected edit link for a
# DIFFERENT resume must not pass the route guard (#368 cycle-review round 1,
# codex finding).
#
# The attestation entry used to be static and pointed at /profile/edit/... — the
# same live read-only probe that renamed ATTESTATION_FIELDS below (#703,
# 2026-08-30) showed the real route is /resume/edit/<resume_id>/
# attestationEducation/<n>. The guard therefore never matched, and opening an
# attestation editor always failed with wrong_route_error; fixing the field
# names alone would not have made this path work.


def _attestation_route(resume_id: str) -> re.Pattern[str]:
    return re.compile(rf"/resume/edit/{re.escape(resume_id)}/attestationEducation(?:/[^/?#]+)?")


def _recommendation_route(resume_id: str) -> re.Pattern[str]:
    return re.compile(rf"/resume/edit/{re.escape(resume_id)}/recommendation(?:/[^/?#]+)?")


def _certificate_route(resume_id: str) -> re.Pattern[str]:
    # Census 2026-09-12 (#1120): обе формы маршрута отвечают формой из трёх
    # полей — с item_id (редактирование строки) и голый (добавление новой).
    return re.compile(rf"/resume/edit/{re.escape(resume_id)}/certificate(?:/[^/?#]+)?")


def _contacts_route(resume_id: str) -> re.Pattern[str]:
    return re.compile(rf"/resume/edit/{re.escape(resume_id)}/contacts")


SECTION_ROUTES = {
    "attestations": _attestation_route,
    "recommendations": _recommendation_route,
    "certificates": _certificate_route,
    "contacts": _contacts_route,
}

FIRST_SECTION_EDIT_PATHS = {
    "attestations": "attestationEducation",
    "recommendations": "recommendation",
    "certificates": "certificate",
    "contacts": "contacts",
}
# Live-confirmed on the empty draft probe (2026-09-02): these suggestion
# buttons are rendered for an actually empty block.  Require the corresponding
# marker before treating zero row triggers as an empty section; otherwise a
# hydration/anti-bot failure could be mistaken for permission to create a row.
EMPTY_SECTION_MARKERS = {
    "attestations": "[data-qa='suitable-vacancies-suggest-item-attestationEducation']",
    "recommendations": "[data-qa='suitable-vacancies-suggest-item-recommendation']",
    # #1120: live-подтверждён боевой прогон 2026-09-14 — черновик qa-2
    # (testing) с пустым блоком сертификатов рендерит чип
    # suitable-vacancies-suggest-item-certificate, и путь первой строки
    # (маркер → голый маршрут /resume/edit/<id>/certificate → save) создал
    # строку с позитивным readback.
    "certificates": "[data-qa='suitable-vacancies-suggest-item-certificate']",
}


# Live-confirmed 2026-08-30 on draft resume a1d75539… at
# /resume/edit/<id>/attestationEducation: the form DOES expose data-qa on every
# input, but under a different family than the historical candidates
# (``profile-education-attestation-*`` / ``profile-education-year-input``, all
# count=0 there).  Note the third field: hh.ru labels it "Специализация" but
# names the attribute ``-result``; keep the code's field order aligned with
# Attestation, not with the attribute wording.
ATTESTATION_FIELDS = (
    "resume-attestation-education-input-name",
    "resume-attestation-education-input-organization",
    "resume-attestation-education-input-result",
    "resume-attestation-education-input-year",
)

# Live-confirmed census 2026-09-12 (ишью #1119, артефакт
# data/logs/census_contacts_1119.md): редактор контактов — ЕДИНСТВЕННАЯ форма
# без повторяемых строк: ровно одно поле телефона, одно email и одна radio-
# группа предпочтительного способа связи. Селекторы сняты с живого DOM двух
# резюме (черновик + опубликованное, shape идентичен). Мессенджеры/сайты в
# редакторе НЕ редактируются (живут текстом в комментарии телефона), поэтому
# схема контакта ограничена type=phone|email.
CONTACT_FIELDS = {
    "phone": "resume-phone-cell_phone",
    "email": "resume-editor-email-input",
}
CONTACT_PHONE_COMMENT = "resume-editor-phone-comment-input"
CONTACT_PREFERRED_RADIO = {
    "phone": "resume-editor-preferred-contact-cell_phone-checked",
    "email": "resume-editor-preferred-contact-email-checked",
}


@dataclass(frozen=True)
class Attestation:
    name: str
    organization: str
    specialty: str
    year: str


@dataclass(frozen=True)
class Recommendation:
    text: str
    company: str
    name: str = ""
    position: str = ""


@dataclass(frozen=True)
class Certificate:
    """Строка блока «Сертификаты» (#1120).

    Схема — ровно по read-only census 2026-09-12: у формы ТРИ поля
    («Название», «Год получения», «Ссылка, если есть»); полей «организация»
    и «специализация» в форме сертификатов НЕТ (это отличие от
    attestationEducation). Файл сертификатом загрузить нельзя: в DOM формы
    нет ни одного input[type=file], URL — единственный путь подтверждения;
    заменять файл выдуманным URL запрещено, поэтому url — просто
    опциональное строковое поле.
    """

    name: str
    year: str
    url: str


@dataclass(frozen=True)
class Contact:
    """Контакт блока «Контакты резюме» (#1119, census-схема 2026-09-12).

    Форма редактора — ровно одно поле телефона и одно email, поэтому строка
    не создаёт НОВУЮ запись, а ЗАМЕЩАЕТ значение соответствующего поля
    (update-семантика). comment допустим только у телефона; preferred
    управляет единственной radio-группой формы — True максимум у одной строки.
    """

    type: str  # "phone" | "email"
    value: str
    comment: str = ""  # только type="phone"
    preferred: bool = False


@dataclass
class ResumeSectionsPlan:
    attestations: list[Attestation] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    certificates: list[Certificate] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # Ручные строки блоков #1118 (--contact/--portfolio/--link).  Блоковые
    # ишью (#1119-1122) добавляют типизированные dataclass + fill-row и
    # переносят свои строки из manual в типизированные поля выше.
    manual: list[ManualRow] = field(default_factory=list)


# --- ручной per-row контракт (#1118) ----------------------------------------

# Статусы исхода строки. `updated` зарезервирован: обновление требует
# readback существующих строк на странице резюме (доменная разведка — задача
# блоковых ишью #1119-1122); этот фундамент update не выполняет никогда.
# `uncertain` (#176-семантика): клик сохранения мог уйти, а readback не смог
# ни подтвердить, ни опровергнуть запись — повтор команды безопасен.
OUTCOME_PLANNED = "planned"
OUTCOME_APPENDED = "appended"
OUTCOME_UPDATED = "updated"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_UNCERTAIN = "uncertain"
OUTCOME_FAILED = "failed"


@dataclass(frozen=True)
class ManualRow:
    """Одна ручная строка произвольного поддержанного блока.

    Поля уже строго провалидированы парсером команды: ключи совпадают со
    схемой блока, значения — непустые либо отсутствуют.
    """

    block: str
    fields: dict[str, str]


# Предварительные схемы ручных блоков. Минимальные наборы из эпика #1117;
# ТОЧНЫЕ поля фиксирует read-only census живой формы (#1119-1122) — блоковое
# ишью сужает/расширяет кортеж по факту, парсер строг к лишним ключам уже
# здесь, так что смена схемы не ослабляет валидацию.
MANUAL_BLOCK_SCHEMAS: dict[str, tuple[str, ...]] = {
    # Схема contact (#1119, census 2026-09-12): type/value/comment/preferred.
    # Сертификата здесь больше нет (#1120 cycle-review): блок перенесён в
    # типизированный путь (_TYPED_BLOCK_SPECS + dataclass Certificate), через
    # ManualRow/plan_from_rows он недостижим — мёртвая запись схемы убрана.
    # Имена ключей = _MANUAL_BLOCKS в config_sections (singular).
    "contact": ("type", "value", "comment", "preferred"),
    "portfolio": ("name", "url"),
    "link": ("name", "url"),
}


def contact_from_manual(fields: dict[str, str]) -> Contact:
    """Конвертация ручной строки блока contact в типизированный Contact (#1119).

    Строгая валидация по census-схеме: неизвестный type, комментарий у email
    и preferred вне {true, false} — явные ошибки, а не молчаливый пропуск.
    """
    ctype = fields.get("type", "")
    if ctype not in CONTACT_FIELDS:
        raise ValueError(
            f"contact: type должен быть одним из {', '.join(sorted(CONTACT_FIELDS))}, "
            f"получено {ctype!r}"
        )
    value = fields.get("value", "")
    if not value:
        raise ValueError("contact: поле value обязательно")
    comment = fields.get("comment", "")
    if comment and ctype != "phone":
        raise ValueError("contact: comment допустим только для type=phone")
    raw_preferred = fields.get("preferred", "")
    preferred = {"true": True, "false": False, "": False}.get(raw_preferred)
    if preferred is None:
        raise ValueError(f"contact: preferred принимает true/false, получено {raw_preferred!r}")
    return Contact(type=ctype, value=value, comment=comment, preferred=preferred)


def contacts_from_manual_rows(rows: list[ManualRow]) -> list[Contact]:
    """Перенос ручных строк contact в типизированные Contact (#1119).

    Дедуп по (type, value) выполняется ранее в plan_from_rows; здесь ловим
    конфликты, которые дедуп не видит: два разных значения одного поля формы
    и более одного preferred (radio на форме одна).
    """
    contacts: list[Contact] = []
    seen_types: set[str] = set()
    preferred_count = 0
    for row in rows:
        contact = contact_from_manual(row.fields)
        if contact.type in seen_types:
            raise ValueError(
                f"contact: повторная строка type={contact.type} — в форме ровно "
                "одно поле этого типа, второе значение перезаписало бы первое"
            )
        seen_types.add(contact.type)
        if contact.preferred:
            preferred_count += 1
            if preferred_count > 1:
                raise ValueError(
                    "contact: preferred=True указан более чем у одной строки — "
                    "radio предпочтительного способа связи на форме одна"
                )
        contacts.append(contact)
    return contacts


@dataclass(frozen=True)
class RowOutcome:
    """Исход одной строки плана (#1118, per-row контракт).

    Честный итог команды: частичный успех одной строки не выдаётся за успех
    всего блока — неудачная строка несёт status=failed с причиной, а команды
    завершаются ненулевым кодом при любом failed.
    """

    block: str
    index: int
    status: str
    reason: str = ""


def _dedupe(block: str, items: list, key_of) -> tuple[list, list[RowOutcome]]:
    """Append/dedup семантика #1118: полное совпадение ВСЕХ полей строки внутри
    одного блока — это duplicate, вторая запись не планируется. Молчаливой
    перезаписи/удаления существующих строк нет: обновление существующей строки
    — только по явному ключу совпадения и только там, где блоковое ишью
    реализует readback существующих строк; этот фундамент новые строки только
    добавляет (appended/planned), никогда не переписывает."""
    kept: list = []
    outcomes: list[RowOutcome] = []
    seen: dict = {}
    for index, item in enumerate(items):
        key = key_of(item)
        if key in seen:
            outcomes.append(
                RowOutcome(block, index, OUTCOME_DUPLICATE, f"повтор строки {seen[key]}")
            )
        else:
            seen[key] = index
            kept.append(item)
            outcomes.append(RowOutcome(block, index, OUTCOME_PLANNED))
    return kept, outcomes


def plan_from_rows(rows: list[ManualRow]) -> tuple[ResumeSectionsPlan, list[RowOutcome]]:
    """Build a manual plan without LLM (#1118). Тот же ResumeSectionsPlan, что
    и у LLM-пути; LLM-путь (build_messages/generate_plan) не затрагивается.
    Дедуп — один канон `_dedupe`, прогнанный per-block (cycle-review PR #1125);
    исходы сохраняют исходный порядок строк."""
    plan = ResumeSectionsPlan()
    outcomes: list[RowOutcome | None] = [None] * len(rows)
    by_block: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if row.block not in MANUAL_BLOCK_SCHEMAS:
            outcomes[index] = RowOutcome(
                row.block, index, OUTCOME_FAILED, f"неизвестный блок {row.block!r}"
            )
        else:
            by_block.setdefault(row.block, []).append(index)
    for block, indices in by_block.items():
        kept, block_outcomes = _dedupe(
            block,
            [rows[index] for index in indices],
            lambda row: tuple(sorted(row.fields.items())),
        )
        plan.manual.extend(kept)
        for local_index, outcome in zip(indices, block_outcomes, strict=True):
            outcomes[local_index] = RowOutcome(block, local_index, outcome.status, outcome.reason)
    return plan, [outcome for outcome in outcomes if outcome is not None]


def fail_tail(outcomes: list[RowOutcome] | None, block: str, start: int, reason: str) -> None:
    """Пометить строки [start; конец] блока failed — запись блока остановлена."""
    if outcomes is None:
        return
    for index in range(start, len(outcomes)):
        outcomes[index] = RowOutcome(block, index, OUTCOME_FAILED, reason)


def _profile_text(profile: AIProfile | None) -> str:
    if profile is None:
        return ""
    return "\n".join(
        part
        for part in (
            getattr(profile, "summary", ""),
            getattr(profile, "desired_role", ""),
            "Навыки: " + ", ".join(getattr(profile, "skills", [])),
            "Достижения: " + "; ".join(getattr(profile, "highlights", [])),
        )
        if part
    )


def build_messages(config: ResumeSectionsConfig, profile: AIProfile | None) -> list[dict[str, str]]:
    """Build a strict JSON-only prompt; user text is context, not instructions."""
    system = (
        "Сформируй дополнительные разделы резюме. Ответь только JSON-объектом с "
        "массивами attestations и recommendations. Не выдумывай факты: неизвестное "
        "оставляй пустым. Каждая аттестация: name, organization, specialty, year. "
        "Каждая рекомендация: company, name, position. Поле text не поддерживается "
        "текущей формой HH.ru и не будет сохранено (#367) — не заполняй его."
    )
    user = (
        f"Режим: {config.mode}. Нужные блоки: {', '.join(config.blocks)}.\n"
        f"Данные пользователя:\n{_profile_text(profile)}\n{config.context}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_plan(content: str | None, blocks: list[str]) -> ResumeSectionsPlan:
    """Parse LLM output fail-closed; malformed output produces no writes."""
    plan = ResumeSectionsPlan()
    try:
        raw = json.loads(content or "")
        if not isinstance(raw, dict):
            raise ValueError("JSON должен быть объектом")
        if "attestations" in blocks:
            for item in raw.get("attestations", []):
                if isinstance(item, dict):
                    plan.attestations.append(
                        Attestation(
                            *(
                                _text(item.get(k))
                                for k in ("name", "organization", "specialty", "year")
                            )
                        )
                    )
        if "recommendations" in blocks:
            for item in raw.get("recommendations", []):
                if isinstance(item, dict):
                    plan.recommendations.append(
                        Recommendation(
                            *(_text(item.get(k)) for k in ("text", "company", "name", "position"))
                        )
                    )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        plan.skipped.append(f"ответ LLM не разобран: {exc}")
    return plan


def generate_plan(
    llm_client, config: ResumeSectionsConfig, profile: AIProfile | None
) -> ResumeSectionsPlan:
    try:
        response = llm_client.chat(
            build_messages(config, profile), temperature=0.2, max_tokens=1200
        )
    except Exception as exc:  # noqa: BLE001 - LLM failure must not become a write
        logger.warning("LLM additional-sections failed: %s", exc)
        return ResumeSectionsPlan(skipped=[f"LLM недоступен: {exc}"])
    return parse_plan(getattr(response, "content", None), config.blocks)


def _fill(locator, value: str) -> None:
    if not value:
        return
    locator.fill(value)


def _fill_attestation_row(page: Page, item: Attestation) -> Locator:
    for qa_field, value in zip(ATTESTATION_FIELDS, item.__dict__.values(), strict=True):
        _fill(page.locator(f"[data-qa='{qa_field}']"), value)
    # The attestation editor is a resume-scoped partial edit, not the profile
    # layout: live probe 2026-08-30 found resume-partial-edit-save (count=1)
    # and no profile-layout-save-button on this screen.
    return page.locator("[data-qa='resume-partial-edit-save']")


def _fill_recommendation_row(page: Page, item: Recommendation) -> Locator:
    if item.text:
        raise PlaywrightError(
            "текущая форма рекомендации не содержит поля текста; запись остановлена"
        )

    def labelled(label: str):
        try:
            return labelled_field(page, label)
        except PageStateIndeterminate as exc:
            raise PlaywrightError(f"поле рекомендации {label!r} не найдено однозначно") from exc

    _fill(labelled("Имя человека"), item.name)
    _fill(labelled("Должность"), item.position)
    company = page.locator("input[name='company']")
    if company.count() != 1:
        raise PlaywrightError("поле рекомендации 'Организация' не найдено однозначно")
    _fill(company, item.company)
    return page.locator("[data-qa='resume-partial-edit-save']")


def _fill_certificate_row(page: Page, item: Certificate) -> Locator:
    # Census 2026-09-12 (#1120): у формы сертификатов нет data-qa на полях —
    # все три input это Magritte-поля без атрибутов (qa="", одинаковые
    # classes). Адресация по видимой подписи через get_by_label — тот же
    # приём, что у формы рекомендаций; неоднозначный label fail-closed
    # внутри labelled_field.
    def labelled(label: str):
        try:
            return labelled_field(page, label)
        except PageStateIndeterminate as exc:
            raise PlaywrightError(f"поле сертификата {label!r} не найдено однозначно") from exc

    _fill(labelled("Название"), item.name)
    _fill(labelled("Год получения"), item.year)
    _fill(labelled("Ссылка, если есть"), item.url)
    return page.locator("[data-qa='resume-partial-edit-save']")


# Первый контрол формы блока, по которому подтверждается открытие редактора
# (wait_for visible + editor_selector в open_hydrated_resume_editor).
BLOCK_READY_SELECTORS = {
    "attestations": f"[data-qa='{ATTESTATION_FIELDS[0]}']",
    "recommendations": "input[name='company']",
    # Census #1120: data-qa у полей нет, подтверждение открытия — видимая
    # подпись первого поля формы.
    "certificates": "label:text-is('Название')",
}


def _contacts_ready(page: Page) -> None:
    """commit != отрисовано (#858): телефонное поле рендерится на форме всегда
    (census #1119), ждём его видимость перед первой строгой проверкой."""
    page.locator(f"[data-qa='{CONTACT_FIELDS['phone']}']").first.wait_for(
        state="visible", timeout=FORM_TIMEOUT_MS
    )


def _on_contacts_route(page: Page, resume_id: str) -> bool:
    current_path = urlsplit(page.url).path.rstrip("/")
    return SECTION_ROUTES["contacts"](resume_id).fullmatch(current_path) is not None


def _apply_contacts(
    page: Page,
    resume_id: str,
    items: list[Contact],
    *,
    dry_run: bool,
    outcomes: list[RowOutcome] | None = None,
) -> list[str]:
    """Заполнить блок контактов (#1119): ОДИН заход в редактор, одна кнопка
    save, readback после сохранения. В отличие от attestations/recommendations
    здесь нет повторяемых строк и триггеров: форма всегда отрисована, поля
    замещают текущие значения (update-семантика, `OUTCOME_UPDATED`), исход
    без подтверждённого readback — `OUTCOME_UNCERTAIN` (#176-семантика).
    """
    errors: list[str] = []
    if outcomes is not None and len(outcomes) != len(items):
        raise ValueError("outcomes должен быть выровнен по строкам блока")
    if not items:
        return errors

    def _fail_all(status: str, reason: str) -> None:
        if outcomes is None:
            return
        # Перезаписываем только незавершённые (planned) строки: исходы, уже
        # зафиксированные readback-ом (updated/uncertain), честнее любого
        # обобщённого статуса (cycle-review PR #1127).
        for index, outcome in enumerate(outcomes):
            if outcome.status == OUTCOME_PLANNED:
                outcomes[index] = RowOutcome("contacts", index, status, reason)

    edit_url = f"{HH_BASE_URL}/resume/edit/{resume_id}/contacts"
    # Граница «клик мог уйти» — сам save.click(), а не подстрока в тексте
    # исключения: таймаут readback-перехода после успешного клика — это
    # #176-семантика (uncertain), ошибка до клика — обычный failed/retry
    # (cycle-review PR #1127).
    past_click = False
    try:
        goto_hh(page, edit_url)
        if not _on_contacts_route(page, resume_id):
            raise RuntimeError("contacts: редактор открыт не для того резюме")
        _contacts_ready(page)
        for item in items:
            field = page.locator(f"[data-qa='{CONTACT_FIELDS[item.type]}']")
            if field.count() != 1:
                raise RuntimeError(f"contacts: поле {item.type} не найдено однозначно")
            _fill(field, item.value)
            if item.type == "phone" and item.comment:
                comment = page.locator(f"[data-qa='{CONTACT_PHONE_COMMENT}']")
                if comment.count() != 1:
                    raise RuntimeError("contacts: комментарий телефона не найден однозначно")
                _fill(comment, item.comment)
            if item.preferred:
                radio = page.locator(f"[data-qa='{CONTACT_PREFERRED_RADIO[item.type]}']")
                if radio.count() != 1:
                    raise RuntimeError(
                        f"contacts: радио preferred ({item.type}) не найдено однозначно"
                    )
                radio.first.wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
                radio.click()
        save = page.locator("[data-qa='resume-partial-edit-save']")
        if save.count() != 1:
            raise RuntimeError("contacts: неоднозначная кнопка сохранения")
        if dry_run:
            # Leave the editor like the row blocks do: cancel is the confirmed
            # exit control of the resume-scoped partial editor (census #1119).
            cancel = page.locator("[data-qa='resume-partial-edit-cancel']")
            if cancel.count() != 1:
                raise RuntimeError("contacts: неоднозначная кнопка отмены")
            cancel.click()
            return errors
        try:
            save.click()
            past_click = True
            save.wait_for(state="hidden", timeout=SAVE_TIMEOUT_MS)
        except (PlaywrightError, RuntimeError) as exc:
            raise PlaywrightError(
                f"сохранение не подтверждено после клика (uncertain): {exc}"
            ) from exc
        # Readback (#1119 п.4): переоткрыть редактор и сверить значения —
        # позитивная проверка именно этого резюме. Таймаут/чужой маршрут
        # здесь тоже uncertain: клик мог уйти.
        goto_hh(page, edit_url)
        if not _on_contacts_route(page, resume_id):
            raise PlaywrightError("contacts: readback открыл не тот редактор (uncertain)")
        _contacts_ready(page)
        uncertain: list[int] = []
        details: list[str] = []
        for index, item in enumerate(items):
            actual = page.locator(f"[data-qa='{CONTACT_FIELDS[item.type]}']").first.input_value()
            ok = actual == item.value
            if ok and item.type == "phone" and item.comment:
                ok = (
                    page.locator(f"[data-qa='{CONTACT_PHONE_COMMENT}']").first.input_value()
                    == item.comment
                )
            if ok and item.preferred:
                radio = page.locator(f"[data-qa='{CONTACT_PREFERRED_RADIO[item.type]}']")
                # Census #1119: состояние radio читается по классу
                # magritte-radio-input-checked (aria-атрибуты не источник).
                ok = radio.count() == 1 and "magritte-radio-input-checked" in (
                    radio.get_attribute("class") or ""
                )
            if outcomes is not None:
                outcomes[index] = RowOutcome(
                    "contacts",
                    index,
                    OUTCOME_UPDATED if ok else OUTCOME_UNCERTAIN,
                    "readback совпал"
                    if ok
                    else f"readback не совпал: ожидалось {item.value!r}, получено {actual!r}",
                )
            if not ok:
                uncertain.append(index)
                details.append(
                    f"строка {index} ({item.type}): ожидалось {item.value!r}, получено {actual!r}"
                )
        if uncertain:
            listed = ", ".join(str(index) for index in uncertain)
            errors.append(
                f"contacts: readback не совпал для строк {listed} (uncertain): "
                + "; ".join(details)
            )
    except (PlaywrightError, RuntimeError) as exc:
        # Ошибка до клика — обычный failed/retry; после save.click() — uncertain
        # независимо от текста исключения (readback goto может упасть по таймауту
        # без всякого «uncertain» в сообщении).
        if past_click:
            _fail_all(OUTCOME_UNCERTAIN, str(exc))
            errors.append(f"contacts: не подтверждено (uncertain): {exc}")
        else:
            _fail_all(OUTCOME_FAILED, str(exc))
            errors.append(f"contacts: не подтверждено: {exc}")
    return errors


def _apply_rows(
    page: Page,
    block: str,
    items: list[Attestation] | list[Recommendation] | list[Certificate],
    fill_row,
    *,
    resume_id: str = "",
    dry_run: bool,
    outcomes: list[RowOutcome] | None = None,
) -> list[str]:
    errors: list[str] = []
    if outcomes is not None and len(outcomes) != len(items):
        raise ValueError("outcomes должен быть выровнен по строкам блока")
    if resume_id and items:
        # A previous empty-section editor may leave the page on its own route
        # after cancel/save.  Re-open the resume before inspecting this block
        # so its row trigger and empty marker are always read from the same
        # deterministic page (#922).
        goto_hh(page, f"{HH_BASE_URL}/resume/{resume_id}")
        if has_login_form(page):
            return ["hh.ru показал форму входа"]
    trigger = page.locator(RESUME_EDIT_BUTTON[block])
    for index, item in enumerate(items):
        # The current HH.ru recommendation editor has no text control. Reject
        # such rows before opening the editor so fail-closed handling cannot
        # leave a partially opened form behind (#367).
        if block == "recommendations" and getattr(item, "text", ""):
            reason = "текущая форма рекомендации не содержит поля текста; запись остановлена"
            errors.append(f"{block}: строка {index} не подтверждена: {reason}")
            if outcomes is not None:
                outcomes[index] = RowOutcome(block, index, OUTCOME_FAILED, reason)
            fail_tail(outcomes, block, index + 1, "запись блока остановлена")
            break
        ready_selector = BLOCK_READY_SELECTORS[block]
        try:
            # trigger.count() itself can raise on iterations after a previous
            # row's save.click() already succeeded (#352/codex round 3) — the
            # whole per-row body must stay inside this guard, not just the
            # click/wait_for, so no browser call here can escape apply_plan
            # uncaught and hide which earlier rows already saved. This also
            # covers save.click()/cancel.click() themselves (#331 cycle-review
            # round 3): an element-detached or navigation error from either
            # must not propagate and crash apply_plan.
            trigger_count = trigger.count()
            if index == 0 and trigger_count == 0 and resume_id:
                # An empty section has no row trigger.  HH.ru's confirmed
                # resume-scoped editor route opens the first row directly;
                # unlike a suggestion chip, it cannot silently bind the row
                # to another resume.  This is the only creation path here:
                # later missing rows remain rejected below.
                empty_marker = page.locator(EMPTY_SECTION_MARKERS[block])
                if empty_marker.count() != 1:
                    raise RuntimeError(f"{block}: пустой блок не подтверждён однозначно")
                edit_path = f"/resume/edit/{resume_id}/{FIRST_SECTION_EDIT_PATHS[block]}"
                goto_hh(page, f"{HH_BASE_URL}{edit_path}")
                current_path = urlsplit(page.url).path.rstrip("/")
                if not SECTION_ROUTES[block](resume_id).fullmatch(current_path):
                    raise RuntimeError(f"{block}: первая строка открыта не для того резюме")
                page.locator(ready_selector).wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
            elif index >= trigger_count:
                errors.append(f"{block}: строка {index} отсутствует; добавление не подтверждено")
                if outcomes is not None:
                    outcomes[index] = RowOutcome(
                        block, index, OUTCOME_FAILED, "строка отсутствует на странице"
                    )
                continue
            elif resume_id:
                edit_path = SECTION_ROUTES[block](resume_id)
                open_hydrated_resume_editor(
                    page,
                    trigger_selector=trigger.nth(index),
                    editor_selector=ready_selector,
                    profile_path=f"/resume/{resume_id}",
                    edit_path=edit_path,
                    click_trigger=True,
                    timeout=FORM_TIMEOUT_MS,
                    trigger_error=f"{block}: строка {index} не найдена однозначно",
                    open_error=f"{block}: строка {index} не открылась",
                    wrong_route_error=f"{block}: строка {index} открыта не для того резюме",
                )
            else:
                # Keep the pure unit fake focused on row-level error handling;
                # live callers always provide resume_id and use the hydrated
                # editor helper above.
                trigger.nth(index).click()
                page.locator(ready_selector).wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
            save = fill_row(page, item)
            if not dry_run:
                if save.count() != 1:
                    errors.append(f"{block}: неоднозначная кнопка сохранения")
                    if outcomes is not None:
                        outcomes[index] = RowOutcome(
                            block, index, OUTCOME_FAILED, "неоднозначная кнопка сохранения"
                        )
                    fail_tail(outcomes, block, index + 1, "запись блока остановлена")
                    # The row editor is left open in this state; querying the
                    # next trigger against it would be unreliable (#331).
                    break
                # The page is already on /resume/{resume_id} before this click
                # (see apply_plan below), and a successful save closes the
                # inline editor in place without changing the URL — so
                # page.wait_for_url() against that same URL would resolve
                # immediately regardless of whether the save actually
                # succeeded (#331: false-positive success). The editor
                # closing (the save button disappearing) is the positive,
                # save-specific signal instead. A timeout here means the
                # editor is likely still open (same rationale as the ambiguous
                # save/cancel branches below), so it falls through to the
                # shared except below and stops the block, rather than
                # clicking the next row's trigger against an unresolved
                # editor state (#331, codex+claude cycle-review round 2).
                try:
                    save.click()
                    save.wait_for(state="hidden", timeout=SAVE_TIMEOUT_MS)
                    if outcomes is not None:
                        outcomes[index] = RowOutcome(
                            block, index, OUTCOME_APPENDED, "сохранение подтверждено"
                        )
                except (PlaywrightError, RuntimeError) as exc:
                    raise PlaywrightError(
                        f"сохранение не подтверждено (uncertain) после клика: {exc}"
                    ) from exc
            else:
                # Leave the row editor before moving to the next row.  Otherwise
                # the next trigger is queried while the previous form is still open.
                # Both supported blocks render the resume-scoped partial editor,
                # so the cancel control is the same for each (live probe
                # 2026-08-30: resume-partial-edit-cancel count=1 on the
                # attestation form, profile-layout-cancel-button count=0).
                cancel = page.locator("[data-qa='resume-partial-edit-cancel']")
                if cancel.count() != 1:
                    errors.append(f"{block}: неоднозначная кнопка отмены")
                    if outcomes is not None:
                        outcomes[index] = RowOutcome(
                            block, index, OUTCOME_FAILED, "неоднозначная кнопка отмены"
                        )
                    fail_tail(outcomes, block, index + 1, "запись блока остановлена")
                    # Same reasoning as the save branch: the editor stays open,
                    # so stop this block instead of leaving it open (#331).
                    break
                cancel.click()
        except (PlaywrightError, RuntimeError) as exc:
            # A hydration timeout here may follow an already-successful save.click()
            # on a previous row (#352/codex round 3), including a save.wait_for
            # timeout right after save.click() (#331/codex+claude): fail closed
            # with an explicit error for this row and stop the block instead of
            # letting the exception escape apply_plan and hide which earlier
            # rows already saved.
            errors.append(f"{block}: строка {index} не подтверждена: {exc}")
            if outcomes is not None:
                outcomes[index] = RowOutcome(block, index, OUTCOME_FAILED, str(exc))
            fail_tail(outcomes, block, index + 1, "запись блока остановлена")
            break
    return errors


def apply_plan(
    page: Page,
    resume_id: str,
    plan: ResumeSectionsPlan,
    *,
    dry_run: bool,
    outcomes: dict[str, list[RowOutcome]] | None = None,
) -> list[str]:
    """Apply rows, creating the first row through the confirmed empty-section route.

    outcomes (#1118) — необязательный per-row контракт: словарь «блок → список
    RowOutcome, выровненный по строкам плана». Ранний выход (нет auth, форма
    входа, сбойный экран) честно помечает ВСЕ строки failed — ни одна строка
    не остаётся «planned» при ненулевом итоге команды.
    """

    def _fail_all(reason: str) -> None:
        if outcomes is None:
            return
        for block in ("attestations", "recommendations", "certificates", "contacts"):
            fail_tail(outcomes.get(block), block, 0, reason)

    if not has_auth_cookie(page):
        _fail_all("отсутствует auth cookie")
        return ["отсутствует auth cookie"]
    goto_hh(page, f"{HH_BASE_URL}/resume/{resume_id}")
    if has_login_form(page):
        _fail_all("hh.ru показал форму входа")
        return ["hh.ru показал форму входа"]
    # #972: сбойный экран /resume/{id} — внятный отказ вместо таймаута на
    # поиске триггеров секций. Pre-mutation, обычный failed/retry.
    if has_resume_error_banner(page):
        _fail_all(RESUME_UNAVAILABLE_REASON)
        return [RESUME_UNAVAILABLE_REASON]
    errors = list(plan.skipped)
    errors += _apply_rows(
        page,
        "attestations",
        plan.attestations,
        _fill_attestation_row,
        resume_id=resume_id,
        dry_run=dry_run,
        outcomes=None if outcomes is None else outcomes.get("attestations"),
    )
    errors += _apply_rows(
        page,
        "recommendations",
        plan.recommendations,
        _fill_recommendation_row,
        resume_id=resume_id,
        dry_run=dry_run,
        outcomes=None if outcomes is None else outcomes.get("recommendations"),
    )
    errors += _apply_rows(
        page,
        "certificates",
        plan.certificates,
        _fill_certificate_row,
        resume_id=resume_id,
        dry_run=dry_run,
        outcomes=None if outcomes is None else outcomes.get("certificates"),
    )
    errors += _apply_contacts(
        page,
        resume_id,
        plan.contacts,
        dry_run=dry_run,
        outcomes=None if outcomes is None else outcomes.get("contacts"),
    )
    return errors

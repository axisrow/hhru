"""Read-only экспорт живого резюме hh.ru в локальное JSON-хранилище (#1023).

Единственный источник данных — отрисованный DOM страницы резюме
``/resume/{resume_id}`` (та же страница, что у `competitors`/`select-photo`),
никаких записей на hh.ru. Селекторы сняты с живого DOM владельца 2026-09-07
(census /resume/{id}): resume-block-title-position, resume-block-salary,
resume-position-field-*, resume-contact-*, .profile-experience-company-card,
resume-list-card-<block>[-item-<id>], skill-tag-<id>, resume-about-card.

Принципы:
- **Честные пропуски.** Секция, которой нет на странице (языки, портфолио) или
  селектор которой не подтверждён однозначно, попадает в ``unavailable`` с
  причиной — никогда не выдумывается значение-заглушка.
- **Структура из DOM, разбор строк — в чистых функциях** (тестируемы без
  браузера): JS-оценка возвращает сырые тексты/строки, их нормализацию и
  раскладку по секциям делают ``build_export_payload`` и парсеры ниже.
- **Фото — скачивание браузером.** URL оригиналов берутся из read-only
  инвентаря библиотеки (``resume_photo.select_photo_on_hh`` dry-run,
  клик-карандаш не мутирует — бои 2026-09-02/03); сами байты получает
  Playwright через загрузку картинки в отдельной вкладке контекста
  (``page.goto`` + ``expect_response``). Прямой HTTP из кода не используется —
  та же граница браузерных действий, что и везде (страж
  ``tests/test_no_page_request.py``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError

from .apply.antibot import raise_for_antibot
from .browser import (
    goto_hh,
    has_resume_error_banner,
    require_authenticated_page,
    resume_identity_matches,
)
from .resume_photo import _MAGIC, LibraryPhoto, select_photo_on_hh

logger = logging.getLogger("hhru_bot.export_resume")

EXPORT_SCHEMA = "export-resume/v1"
PHOTO_DOWNLOAD_TIMEOUT_MS = 30_000

# Страница опыта сворачивает длинные описания за кнопкой «Развернуть»;
# innerText свёрнутого шага описание не содержит — честная пометка, не клик.
EXPERIENCE_EXPAND_MARKER = "Развернуть"

# Блоки, разбираемые построчно в самостоятельные секции экспорта; остальные
# resume-list-card-<block> попадают в payload как есть (generic blocks).
EDUCATION_BLOCK = "education"


class ResumeExportIndeterminate(RuntimeError):
    """Страница не подтверждена как читаемое резюме — пустой экспорт недопустим."""


_COLLECT_JS = """
() => {
  const norm = (s) => (s || '').replace(/\\u00a0/g, ' ').trim();
  const one = (sel) => {
    const els = document.querySelectorAll(sel);
    return {count: els.length, text: els.length === 1 ? norm(els[0].innerText) : null};
  };
  const cards = [];
  document.querySelectorAll('[data-qa]').forEach((el) => {
    const qa = el.getAttribute('data-qa') || '';
    if (/^resume-list-card-[a-z]+$/.test(qa)) {
      cards.push({block: qa.slice('resume-list-card-'.length), text: norm(el.innerText)});
    }
  });
  const items = [];
  document.querySelectorAll('[data-qa^="resume-list-card-"]').forEach((el) => {
    const qa = el.getAttribute('data-qa') || '';
    const m = qa.match(/^resume-list-card-([a-z]+)-item-(\\d+)$/);
    if (!m) return;
    const block = m[1];
    const id = m[2];
    const part = (suffix) => {
      const c = el.querySelector('[data-qa$="-' + suffix + '-' + id + '"]');
      return c ? norm(c.innerText) : null;
    };
    items.push({
      block,
      id,
      title: part('title'),
      subtitle: part('subtitle'),
      description: part('description'),
      text: norm(el.innerText),
    });
  });
  const contacts = [];
  document.querySelectorAll('[data-qa^="resume-contact-"]').forEach((el) => {
    const a = el.matches('a') ? el : el.querySelector('a');
    if (!a) return;
    contacts.push({
      qa: el.getAttribute('data-qa') || '',
      text: norm(a.innerText),
      href: a.href || null,
    });
  });
  const experience = [...document.querySelectorAll("[data-qa='profile-experience-company-card']")]
    .map((card) => ({
      header: [...card.querySelectorAll("[data-qa='cell-text-content']")]
        .filter((e) => !e.closest("[data-qa='magritte-stepper']"))
        .map((e) => norm(e.innerText)),
      steps: [...card.querySelectorAll("[data-qa='magritte-stepper-step']")]
        .map((st) => st.innerText.split('\\n').map(norm).filter(Boolean)),
    }));
  const skills = [...document.querySelectorAll('[data-qa^="skill-tag-"]')].map((el) => ({
    qa: el.getAttribute('data-qa') || '',
    text: norm(el.innerText),
  }));
  const fields = [...document.querySelectorAll('[data-qa^="resume-position-field-"]')]
    .map((el) => ({
      qa: el.getAttribute('data-qa') || '',
      text: norm(el.innerText),
    }));
  const avatar = document.querySelector("[data-qa='resume-avatar'] img");
  const expand = [...document.querySelectorAll('button')]
    .some((b) => norm(b.innerText) === 'Развернуть');
  return {
    title: one("[data-qa='resume-block-title-position']"),
    salary: one("[data-qa='resume-block-salary']"),
    about: one("[data-qa='resume-about-card']"),
    avatar_img: avatar ? avatar.src : null,
    expand_button: expand,
    cards,
    items,
    contacts,
    experience,
    skills,
    fields,
  };
}
"""


@dataclass
class ResumeExportResult:
    resume_id: str
    slug: str
    payload: dict = field(default_factory=dict)
    path: Path | None = None
    photos: list[dict] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    success: bool = False
    reason: str = ""


def _photo_id_from_url(src: str) -> str | None:
    match = re.search(r"/photo/(\d+)\.(?:jpeg|jpg|png)", urlsplit(src).path or "")
    return match.group(1) if match else None


def parse_contacts(rows: list[dict]) -> list[dict]:
    """Разложить contact-строки по типам; родительская (короткая) qa выигрывает.

    На живой странице контакт рендерится парой: родитель
    ``resume-contact-phone`` (или ``-value-preferred`` у ссылки) — берём самый
    короткий qa каждого типа, «preferred» фиксируем отдельным флагом.
    """
    best: dict[str, dict] = {}
    for row in rows:
        qa = str(row.get("qa", ""))
        name = qa[len("resume-contact-") :] if qa.startswith("resume-contact-") else qa
        if not name:
            continue
        contact_type = name.split("-value-")[0].split("-value")[0]
        preferred = "preferred" in name
        candidate = {
            "type": contact_type,
            "preferred": preferred,
            "value": str(row.get("text", "")) or None,
            "href": row.get("href") or None,
        }
        current = best.get(contact_type)
        if current is None:
            candidate["_qa"] = name
            best[contact_type] = candidate
            continue
        # Одна сущность под двумя qa (родитель + -value): самый короткий qa
        # определяет тип, непустые text/href мержатся — заглушка не заводится.
        current["preferred"] = current["preferred"] or preferred
        current["value"] = current["value"] or candidate["value"]
        current["href"] = current["href"] or candidate["href"]
        if len(name) < len(str(current["_qa"])):
            current["_qa"] = name
    result = []
    for contact in best.values():
        contact.pop("_qa", None)
        result.append(contact)
    result.sort(key=lambda item: item["type"])
    return result


def parse_experience_company(card: dict) -> dict:
    """Одна карточка опыта: header[0]=компания, header[1]=стаж; шаги = позиции."""
    header = [line for line in card.get("header", []) if line]
    steps = card.get("steps", [])
    positions = []
    for step_lines in steps:
        lines = [line for line in step_lines if line]
        if not lines:
            continue  # пустой li.magritte-stepper-step (наполнитель hh.ru)
        entry: dict = {"title": None, "period": None, "description": None}
        if len(lines) >= 1:
            entry["title"] = lines[0]
        if len(lines) >= 2:
            entry["period"] = lines[1]
        if len(lines) >= 3:
            entry["description"] = "\n".join(lines[2:])
        positions.append(entry)
    return {
        "company": header[0] if len(header) >= 1 else None,
        "duration": header[1] if len(header) >= 2 else None,
        "positions": positions,
    }


def parse_block_items(items: list[dict]) -> list[dict]:
    """Элементы одного resume-list-card-<block> в единый shape."""
    parsed = []
    for item in items:
        entry = {
            "id": item.get("id"),
            "title": item.get("title"),
            "subtitle": item.get("subtitle"),
            "description": item.get("description"),
        }
        if not any(entry[key] for key in ("title", "subtitle", "description")):
            # Структура -title/-subtitle/-description не подтвердилась —
            # сохраняем строки и сырой текст, чтобы импорт-фоллоуап не
            # потерял данные (у education-item поимённых детей нет).
            entry["lines"] = [
                line for line in str(item.get("text") or "").split("\n") if line.strip()
            ]
            entry["raw_text"] = item.get("text") or None
        parsed.append(entry)
    return parsed


def build_export_payload(
    raw: dict, *, resume_id: str, resume_url: str, slug: str
) -> tuple[dict, list[str]]:
    """Нормализовать сырую JS-оценку в payload экспорта; пропуски — в unavailable.

    Чистая функция: тестируется без браузера. Секция отсутствует в payload
    (None/[]) ТОЛЬКО вместе с причиной в ``unavailable`` — значения-заглушки
    запрещены (#1023).
    """
    unavailable: list[str] = []

    def one(section: str, read: dict) -> str | None:
        count = int(read.get("count", 0))
        if count == 1:
            return read.get("text") or None
        if count == 0:
            unavailable.append(f"{section}: блок не отрисован на странице (вероятно, не заполнен)")
        else:
            unavailable.append(
                f"{section}: неоднозначный DOM ({count} блоков), значение не зафиксировано"
            )
        return None

    title = one("позиция", raw.get("title") or {"count": 0})
    salary = one("зарплата", raw.get("salary") or {"count": 0})
    about = one("о себе", raw.get("about") or {"count": 0})

    fields = [
        {"field": str(row.get("qa", ""))[len("resume-position-field-") :], "text": row.get("text")}
        for row in raw.get("fields", [])
        if str(row.get("qa", "")).startswith("resume-position-field-")
    ]

    companies = [parse_experience_company(card) for card in raw.get("experience", [])]
    if not companies:
        unavailable.append("опыт работы: карточки компаний не найдены на странице")

    if raw.get("expand_button"):
        unavailable.append(
            "опыт работы: на странице есть свёрнутые описания («Развернуть»), "
            "экспортированные описания могут быть неполными"
        )

    blocks: dict[str, list] = {}
    education_items: list[dict] = []
    card_blocks = {card.get("block"): card for card in raw.get("cards", []) if card.get("block")}
    items_by_block: dict[str, list[dict]] = {}
    for item in raw.get("items", []):
        items_by_block.setdefault(str(item.get("block")), []).append(item)
    for block in sorted(card_blocks):
        if block == EDUCATION_BLOCK:
            education_items = parse_block_items(items_by_block.get(block, []))
            if not education_items:
                unavailable.append("образование: карточка есть, элементы не разобраны")
            continue
        blocks[block] = parse_block_items(items_by_block.get(block, []))

    # Языки — самостоятельная секция; их отсутствие (нет карточки) — не ошибка.
    languages = parse_block_items(items_by_block.pop("language", []))
    blocks.pop("language", None)

    skills = []
    for row in raw.get("skills", []):
        qa = str(row.get("qa", ""))
        match = re.fullmatch(r"skill-tag-(\d+)", qa)
        skills.append({"id": match.group(1) if match else None, "name": row.get("text") or None})
    if not skills:
        unavailable.append("навыки: теги skill-tag не найдены на странице")

    payload: dict = {
        "schema": EXPORT_SCHEMA,
        "resume_id": resume_id,
        "slug": slug,
        "resume_url": resume_url,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "position": {
            "title": title,
            "salary_text": salary,
            "fields": fields,
        },
        "contacts": parse_contacts(raw.get("contacts", [])),
        "experience": {
            "companies": companies,
        },
        "education": education_items,
        "blocks": blocks,
        "skills": skills,
        "languages": languages,
        "about": about,
        "avatar_src": raw.get("avatar_img") or None,
    }
    if not payload["contacts"]:
        unavailable.append("контакты: строки resume-contact-* не найдены")
    return payload, unavailable


def _sniff_image_kind(body: bytes) -> str | None:
    return next((name for magic, name in _MAGIC if body.startswith(magic)), None)


def download_photo(context: BrowserContext, photo: LibraryPhoto, dest_dir: Path) -> dict:
    """Скачать оригинал фото браузером контекста; статус-словарь, без исключений."""
    photo_id = photo.photo_id or _photo_id_from_url(photo.src) or "unknown"
    record: dict = {
        "photo_id": photo_id,
        "src": photo.src,
        "file": None,
        "status": "failed",
        "reason": None,
    }
    page = context.new_page()
    try:
        with page.expect_response(
            lambda response: urlsplit(response.url).path == urlsplit(photo.src).path,
            timeout=PHOTO_DOWNLOAD_TIMEOUT_MS,
        ) as response_info:
            page.goto(photo.src)
        response = response_info.value
        body = response.body()
        kind = _sniff_image_kind(body)
        if kind is None:
            record["reason"] = "содержимое не JPEG/PNG по магическим байтам (страница-заглушка?)"
            return record
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"photo_{photo_id}.{kind}"
        path.write_bytes(body)
        record["file"] = str(path)
        record["status"] = "downloaded"
        return record
    except (PlaywrightError, OSError) as exc:
        record["reason"] = str(exc).split("\n")[0][:300]
        return record
    finally:
        page.close()


def collect_resume_dom(page: Page) -> dict:
    return page.evaluate(_COLLECT_JS)


# Инвентарь ВСЕХ отрисованных вариантов фото (src + натуральный размер):
# лента миниатюр вьюера отдаёт 100x100 варианты, у основного слайда размер
# больше. Выбираем крупнейший подтверждённый вариант, а не выдуманный
# «оригинальный» URL (подпись ?h= валидна только для выданных hh.ru src).
_VARIANTS_JS = """
() => {
  const out = [];
  document.querySelectorAll('img').forEach((img) => {
    const src = img.currentSrc || img.src || '';
    if (!src.includes('/photo/')) return;
    out.push({
      src: src,
      width: img.naturalWidth,
      height: img.naturalHeight,
    });
  });
  return out;
}
"""


def collect_photo_variants(page: Page) -> dict[str, dict]:
    """id -> крупнейший отрисованный вариант фото (src+w+h); read-only."""
    try:
        rows = page.evaluate(_VARIANTS_JS)
    except PlaywrightError:
        return {}
    best: dict[str, dict] = {}
    for row in rows:
        photo_id = _photo_id_from_url(str(row.get("src", "")))
        if not photo_id:
            continue
        # 0x0 — незагруженный lazy-img: натуральный размер не подтверждён,
        # вариант игнорируем вместо ложного «оригинала».
        if not row.get("width") or not row.get("height"):
            continue
        if photo_id not in best or (row.get("width", 0) * row.get("height", 0)) > (
            best[photo_id]["width"] * best[photo_id]["height"]
        ):
            best[photo_id] = row
    return best


def export_resume_on_hh(
    context: BrowserContext,
    resume,
    *,
    output_dir: Path,
    with_photos: bool = True,
) -> ResumeExportResult:
    """Прочитать резюме и (опционально) фото в JSON-хранилище; read-only на hh.ru."""
    result = ResumeExportResult(resume_id=resume.resume_id, slug=resume.id)
    page = context.new_page()
    try:
        goto_hh(page, resume.resume_url)
        require_authenticated_page(page)
        raise_for_antibot(page)
        if has_resume_error_banner(page):
            result.reason = "hh.ru показал экран недоступности резюме"
            return result
        if not resume_identity_matches(page, resume.resume_id):
            result.reason = "identity открытой страницы не совпал с resume_id"
            return result
        raw = collect_resume_dom(page)
    finally:
        page.close()

    payload, unavailable = build_export_payload(
        raw, resume_id=resume.resume_id, resume_url=resume.resume_url, slug=resume.id
    )

    library: list[LibraryPhoto] = []
    variant_sizes: dict[str, dict] = {}
    if with_photos:
        photo_page = context.new_page()
        try:
            inventory = select_photo_on_hh(photo_page, resume, None, True)
            # Вьюер открыт тем же read-only карандашом: снимаем все
            # отрисованные варианты фото ДО закрытия страницы.
            variant_sizes = collect_photo_variants(photo_page)
        finally:
            photo_page.close()
        if inventory.photos:
            for photo in inventory.photos:
                variant = variant_sizes.get(photo.photo_id)
                library.append(
                    LibraryPhoto(
                        photo_id=photo.photo_id,
                        src=variant["src"] if variant else photo.src,
                    )
                )
        else:
            unavailable.append(
                "фото: инвентарь библиотеки не прочитан"
                + (f" ({inventory.reason})" if inventory.reason else "")
            )
        photos_dir = output_dir / "photos"
        records = [download_photo(context, photo, photos_dir) for photo in library]
        for record in records:
            variant = variant_sizes.get(str(record["photo_id"]))
            if variant:
                record["width"] = variant.get("width")
                record["height"] = variant.get("height")
                if int(variant.get("width") or 0) < 400:
                    unavailable.append(
                        f"фото {record['photo_id']}: крупнейший подтверждённый "
                        f"вариант {variant.get('width')}x{variant.get('height')} — "
                        "оригинальный размер hh.ru не отдаёт"
                    )
            else:
                # Вьюер не отрисовал ни одного варианта этого фото: скачан src
                # из инвентаря (лента миниатюр — по живому прогону 100x100).
                # Размер не подтверждён — молча выдавать его за «фото» нельзя.
                record["width"] = None
                record["height"] = None
                unavailable.append(
                    f"фото {record['photo_id']}: вьюер не отрисовал ни одного "
                    "варианта — скачан src из инвентаря, размер не подтверждён "
                    "(вероятна миниатюра)"
                )
            if record["status"] != "downloaded":
                unavailable.append(f"фото {record['photo_id']}: не скачано ({record['reason']})")
        payload["photos"] = records
    else:
        payload["photos"] = []

    stamp = time.strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"resume_{resume.id}_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    result.payload = payload
    result.path = path
    result.photos = payload.get("photos", [])
    result.unavailable = unavailable
    result.success = True
    return result

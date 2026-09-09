"""Диагностический пакет дрейфа DOM hh.ru (#1069).

Обёртка над fail-closed отказами селекторов: семантика отказа не меняется
(никаких ретраев и перебора селекторов), но при провале CLI печатает пакет
диагностики — команду, URL, ожидаемый селектор из реестра ``selector_groups``,
найденных кандидатов и замаскированный фрагмент DOM — плюс готовую команду
``gh issue create`` с уже записанным body-файлом, чтобы ишью по дрейфу
создавалось не вслепую.

Всё чтение страницы — один read-only ``page.evaluate``; если страница уже
закрыта/недоступна, диагностика деградирует до того, что известно без DOM
(URL из ``page.url``), и никогда не роняет сам отказ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import logging_setup

# Cap'ы читаемости вывода CLI: фрагмент DOM и число строк census-кандидатов.
_DOM_FRAGMENT_CAP = 1200
_MAX_CANDIDATES = 8


# --- Маскирование личных данных ------------------------------------------- #
# Правило проекта: никаких реальных ID, email, телефонов и ФИО даже в
# диагностиках (#828). Структурно распознаваемое (email/телефон/hex/digit-id)
# маскируется регэкспами; имена работодателей/рекрутеров структурно не
# распознать — census-текст элементов с company/employer-признаком в data-qa
# или классах маскируется целиком (см. _mask_census_rows).

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?:\+7|8)[\s(-]?\d{3}[\s)-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}")
_HEX_ID_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_LONG_DIGITS_RE = re.compile(r"\b\d{7,}\b")


def mask_personal_data(text: str) -> str:
    """Замаскировать структурно распознаваемые личные данные.

    Порядок важен: email/телефон раньше digit-id, чтобы номер не раздробился
    на куски раньше полной маски. Каждой категории — свой счётчик для
    стабильных сквозных имён (``<id-1>``, ``<id-2>``).
    """
    counters: dict[str, int] = {}

    def _sub(tag: str, _match: re.Match) -> str:
        counters[tag] = counters.get(tag, 0) + 1
        return f"<{tag}-{counters[tag]}>"

    text = _EMAIL_RE.sub(lambda m: _sub("email", m), text)
    text = _PHONE_RE.sub(lambda m: _sub("телефон", m), text)
    text = _HEX_ID_RE.sub(lambda m: _sub("hex-id", m), text)
    text = _LONG_DIGITS_RE.sub(lambda m: _sub("id", m), text)
    return text


_EMPLOYER_MARKERS_RE = re.compile(r"company|employer", re.IGNORECASE)


def _mask_census_rows(rows: list[dict]) -> list[dict]:
    """Маскировать census-строки: PII в любом поле + текст company/employer."""
    masked = []
    for row in rows:
        row = dict(row)
        for key in ("qa", "tag", "role", "label", "text", "classes"):
            if row.get(key):
                row[key] = mask_personal_data(str(row[key]))
        identity_hint = f"{row.get('qa', '')} {row.get('classes', '')}"
        if _EMPLOYER_MARKERS_RE.search(identity_hint):
            row["text"] = "<работодатель>"
            row["label"] = "<работодатель>"
        masked.append(row)
    return masked


# --- Реестр селекторов: обратный поиск имени ------------------------------- #


def _registry_values() -> dict[str, str]:
    from .selector_groups import _generated

    return _generated.VALUES


def registry_lookup(selector: str) -> str | None:
    """Имя селектора в реестре ``selector_groups/_generated`` (``module.NAME``).

    Ожидаемый селектор в отчёте дрейфа обязан приходить из реестра, а не из
    хардкода вызова (#1069 п.1). None — селектор локальный/собранный.
    """
    for name, value in _registry_values().items():
        if value == selector:
            return name
    return None


_DATA_QA_RE = re.compile(r"data-qa=[\"']([^\"']+)[\"']")


def _selector_qa_key(selector: str) -> str:
    """Ключ data-qa из селектора для сопоставления с кандидатами (prefix-match)."""
    match = _DATA_QA_RE.search(selector)
    return match.group(1) if match else ""


# --- Контекст текущей команды ------------------------------------------------ #


@dataclass
class DriftContext:
    """Что CLI знает о текущей команде в момент провала селектора."""

    command: str = ""
    step: str = ""
    screen: str = ""
    expected: tuple[str, ...] = ()


_current_context = DriftContext()


def begin_drift_session(command: str) -> None:
    """Зафиксировать команду (вызывается из cli-диспетчера один раз на запуск)."""
    global _current_context
    _current_context = DriftContext(command=command)


def note_drift_step(step: str, *, expected: tuple[str, ...] = (), screen: str = "") -> None:
    """Уточнить контекст шагом пайплайна и ожидаемыми селекторами (opt-in).

    Модули могут обновлять контекст по ходу команды; центральные хелперы
    browser.py подхватывают его при провале. Селекторы передаются из
    selector_groups — при провале они попадают в отчёт с именем из реестра.
    """
    _current_context.step = step
    _current_context.screen = screen or _current_context.screen
    _current_context.expected = expected or _current_context.expected


def get_drift_context() -> DriftContext:
    return _current_context


# --- Сбор пакета ------------------------------------------------------------ #


@dataclass
class DriftReport:
    command: str
    step: str
    screen: str
    url: str
    error: str
    # (селектор, имя в реестре или None)
    expected: list[tuple[str, str | None]] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    fragment: str = ""
    fragment_truncated: bool = False


# Один read-only evaluate: census кандидатов с prefix-скорингом против
# ожидаемых data-qa + фрагмент DOM вокруг ближайшего кандидата (или body).
_DRIFT_JS = r"""(keys) => {
  const sel = 'input, textarea, select, button, a[href], label, ' +
    '[role="button"], [role="combobox"], [role="checkbox"], [role="radio"], [data-qa]';
  const rows = [];
  for (const el of document.querySelectorAll(sel)) {
    const qa = el.getAttribute('data-qa') || '';
    let score = 0;
    for (const key of keys) {
      if (!key || !qa) continue;
      const hops = Math.min(key.length, qa.length);
      let common = 0;
      while (common < hops && key[common] === qa[common]) common += 1;
      score = Math.max(score, common);
    }
    const style = window.getComputedStyle(el);
    rows.push({
      qa,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      label: (el.getAttribute('aria-label') || '').slice(0, 80),
      text: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80),
      classes: (typeof el.className === 'string' ? el.className : '').trim().slice(0, 120),
      visible: style.display !== 'none' && style.visibility !== 'hidden' &&
        (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0),
      score,
    });
  }
  rows.sort((a, b) => b.score - a.score);
  const top = rows.filter((r) => r.score > 0).slice(0, 8);
  let root = document.body;
  if (top.length && top[0].score >= 3) {
    const best = document.querySelector(
      "[data-qa='" + (top[0].qa.replace(/'/g, "\\'")) + "']");
    if (best) root = best.parentElement || best;
  }
  const fragment = root ? root.outerHTML || '' : '';
  return {url: location.href, candidates: top, fragment};
}"""


def _safe_page_url(page) -> str:
    try:
        return mask_personal_data(str(page.url))
    except Exception:
        return "<неизвестен>"


def collect_drift_report(
    page, error: Exception, *, step: str = "", expected_selectors: tuple[str, ...] = ()
) -> DriftReport:
    """Собрать пакет дрейфа. Read-only, деградирует без DOM, никогда не бросает."""
    ctx = get_drift_context()
    expected = list(expected_selectors) or list(ctx.expected)
    report = DriftReport(
        command=ctx.command or "<неизвестная команда>",
        step=step or ctx.step,
        screen=ctx.screen,
        url=_safe_page_url(page),
        error=str(error),
        expected=[(selector, registry_lookup(selector)) for selector in expected],
    )
    keys = [_selector_qa_key(selector) for selector in expected]
    try:
        data = page.evaluate(_DRIFT_JS, keys)
    except Exception:
        # Страница закрыта/крашнута — DOM недоступен; отчёт без кандидатов
        # всё равно полезен (команда, URL, ожидаемый селектор, body-файл).
        return report
    report.candidates = _mask_census_rows(list(data.get("candidates", [])))
    fragment = mask_personal_data(str(data.get("fragment", "")))
    if len(fragment) > _DOM_FRAGMENT_CAP:
        fragment = fragment[:_DOM_FRAGMENT_CAP]
        report.fragment_truncated = True
    report.fragment = fragment
    return report


# --- Рендер и body-файл ------------------------------------------------------ #


def _issue_title(report: DriftReport) -> str:
    first = report.expected[0] if report.expected else None
    selector = first[1] or first[0] if first else "<селектор не указан>"
    place = report.screen or report.step or "страница"
    return f"drift(hh.ru): {report.command}: селектор {selector} не подтверждён ({place})"


def render_drift_block(report: DriftReport, body_path: Path | None) -> str:
    """Печатный блок «Похоже на дрейф DOM» для stdout."""
    from .browser import census_table

    lines = [
        "[DRIFT] Похоже на дрейф DOM hh.ru (fail-closed отказ, без ретраев).",
        f"[DRIFT] Команда: {report.command}"
        + (f" | Шаг: {report.step}" if report.step else "")
        + (f" | Экран: {report.screen}" if report.screen else ""),
        f"[DRIFT] URL: {report.url}",
    ]
    if report.expected:
        for selector, name in report.expected:
            lines.append(f"[DRIFT] Ожидался селектор: {name or '<вне реестра>'} = {selector}")
    else:
        lines.append("[DRIFT] Ожидался селектор: <не указан>")
    if report.candidates:
        lines.append("[DRIFT] Найденные кандидаты (ближайшие по data-qa):")
        lines.append(census_table(report.candidates[:_MAX_CANDIDATES]))
    else:
        lines.append("[DRIFT] Кандидаты в DOM не сняты (страница недоступна).")
    if report.fragment:
        note = f", обрезан до {_DOM_FRAGMENT_CAP} символов" if report.fragment_truncated else ""
        lines.append(f"[DRIFT] Фрагмент DOM вокруг точки провала (замаскирован{note}):")
        lines.extend(f"  {line}" for line in report.fragment.splitlines()[:30])
    if body_path is not None:
        lines.append(f"[DRIFT] Диагностический пакет записан: {body_path}")
        lines.append("[DRIFT] Готовая команда создания ишью:")
        lines.append(f'gh issue create --title "{_issue_title(report)}" --body-file {body_path}')
    return "\n".join(lines)


_BODY_TEMPLATE = """## Что наблюдалось

{error}

Команда: `{command}`{step_line}{screen_line}

## Ожидалось

{expected_block}

## URL

`{url}`

## Найденные кандидаты (census)

{candidates_block}

## Фрагмент DOM (замаскирован)

```html
{fragment}
```
"""


def write_issue_body(report: DriftReport, log_dir: Path | None = None) -> Path | None:
    """Записать body-файл ишью дрейфа в LOG_DIR. Best-effort: OSError -> None."""
    from .browser import census_table

    # Через модуль, а не from-import: conftest-изоляция логов патчит
    # logging_setup.LOG_DIR — drift обязан видеть подменённое значение.
    log_dir = log_dir or logging_setup.LOG_DIR
    expected_block = (
        "\n".join(
            f"- `{name or '<вне реестра>'}` = `{selector}`" for selector, name in report.expected
        )
        or "- <селектор не указан>"
    )
    candidates_block = (
        census_table(report.candidates[:_MAX_CANDIDATES])
        if report.candidates
        else "<DOM недоступен>"
    )
    fragment = report.fragment or "<недоступен>"
    text = _BODY_TEMPLATE.format(
        error=report.error,
        command=report.command,
        step_line=f"\nШаг: `{report.step}`" if report.step else "",
        screen_line=f"\nЭкран: `{report.screen}`" if report.screen else "",
        expected_block=expected_block,
        url=report.url,
        candidates_block=candidates_block,
        fragment=fragment,
    )
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"drift_issue_{stamp}.md"
        path.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return path


def emit_drift_report(
    page, error: Exception, *, step: str = "", expected_selectors: tuple[str, ...] = ()
) -> Path | None:
    """Собрать пакет, записать body-файл, напечатать блок. Никогда не бросает.

    Вызывается из центральных точек отказа селекторов (browser.py) ПЕРЕД
    пробросом исходного исключения: диагностика — обёртка над fail-closed,
    исход отказа (тип, сообщение, статус) не меняется.
    """
    try:
        report = collect_drift_report(page, error, step=step, expected_selectors=expected_selectors)
        body_path = write_issue_body(report)
        print(render_drift_block(report, body_path))
        return body_path
    except Exception:
        return None

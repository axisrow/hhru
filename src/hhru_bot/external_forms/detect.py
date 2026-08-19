"""Generic, fail-closed DOM inspection for external forms.

The module deliberately knows nothing about Yandex Forms.  It reads accessible
labels and control types, and never clicks a button or submits a form.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

_SPACE = re.compile(r"\s+")

# Fields the LLM must never auto-select, even at high confidence (#280 review
# round 3): a confident-but-mismatched guess on these is the costliest failure
# mode. They remain fillable only via an exact form_profile.answers match —
# the same, already-accepted disclosure boundary from #276/#277/#280.
_LLM_DENIED_KEY_PATTERN = re.compile(
    r"телефон|phone|email|e-mail|почта|паспорт|passport|снилс|инн", re.IGNORECASE
)


def normalize(text: str) -> str:
    return _SPACE.sub(" ", text).strip().casefold()


def _question_text(text: str) -> str:
    """Remove presentation-only required markers from accessible labels."""
    text = re.sub(r"\bобязательное поле\b", "", text, flags=re.IGNORECASE)
    return normalize(text.replace("*", " "))


@dataclass(frozen=True)
class FormField:
    kind: str
    selector: str
    label: str
    required: bool
    options: tuple[str, ...] = ()
    state: str = "confirmed"


@dataclass
class FormScan:
    fields: list[FormField] = field(default_factory=list)
    state: str = "confirmed"
    reason: str = ""

    @property
    def indeterminate(self) -> bool:
        return self.state != "confirmed"


def _label(page: Page, control) -> str:
    labelled = control.get_attribute("aria-labelledby") or ""
    if labelled:
        text = " ".join(
            sum((page.locator(f"#{part}").all_inner_texts() for part in labelled.split()), [])
        )
        if text.strip():
            return text
    control_id = control.get_attribute("id")
    if control_id:
        labels = page.locator(f"label[for='{control_id}']").all_inner_texts()
        if labels:
            return " ".join(labels)
    return control.get_attribute("aria-label") or ""


def _radio_label(page: Page, control) -> str:
    return " ".join(control.locator("xpath=ancestor::label[1]").all_inner_texts()).strip()


def scan_form(page: Page) -> FormScan:
    """Inspect the first form, returning ``indeterminate`` on DOM errors."""
    try:
        forms = page.locator("form")
        if forms.count() != 1:
            return FormScan(
                state="indeterminate", reason="не удалось однозначно определить одну форму"
            )
        form = forms.first
        fields: list[FormField] = []
        controls = form.locator("input, textarea, select")
        seen_radio_names: set[str] = set()
        selector_indexes: dict[str, int] = {}
        for i in range(controls.count()):
            control = controls.nth(i)
            kind = (control.get_attribute("type") or "text").casefold()
            if kind in {"hidden", "submit", "button", "reset"}:
                continue
            if kind not in {"text", "email", "tel", "number", "radio", "checkbox", "file"}:
                kind = "select" if control.evaluate("e => e.tagName") == "SELECT" else "unknown"
            options: tuple[str, ...] = ()
            if kind == "radio":
                name = control.get_attribute("name") or ""
                if name in seen_radio_names:
                    continue
                seen_radio_names.add(name)
                group = form.locator(f"input[type='radio'][name='{name}']")
                group_node = group.first.locator("xpath=ancestor::*[@role='radiogroup'][1]")
                label = _label(page, group_node)
                options = tuple(
                    normalize(_radio_label(page, group.nth(j))) for j in range(group.count())
                )
                selector = f"form input[type='radio'][name='{name}']"
                required = (
                    group_node.get_attribute("aria-required") == "true"
                    or "обязательное поле" in label.casefold()
                )
            else:
                label = _label(page, control)
                required = (
                    control.get_attribute("aria-required") or ""
                ).casefold() == "true" or control.get_attribute("required") is not None
                control_id = control.get_attribute("id")
                tag = control.evaluate("e => e.tagName").lower()
                if tag == "select":
                    options = tuple(
                        normalize(option)
                        for option in control.locator("option").all_inner_texts()
                        if normalize(option)
                    )
                if control_id:
                    selector = f"#{control_id}"
                else:
                    if tag == "textarea":
                        selector_key = "textarea"
                        selector_base = "form textarea"
                    elif tag == "select":
                        selector_key = "select"
                        selector_base = "form select"
                    elif kind == "text" and control.get_attribute("type") is None:
                        # A missing input[type] is equivalent to type=text, but
                        # must remain in the same selector family for nth.
                        selector_key = "input:text"
                        selector_base = "form input:not([type]), form input[type='text']"
                    else:
                        selector_key = f"input:{kind}"
                        selector_base = f"form input[type='{kind}']"
                    local_index = selector_indexes.get(selector_key, 0)
                    selector_indexes[selector_key] = local_index + 1
                    selector = f"{selector_base} >> nth={local_index}"
            clean_label = _question_text(label)
            state = "confirmed" if clean_label else "indeterminate"
            fields.append(
                FormField(
                    kind,
                    selector,
                    clean_label,
                    required,
                    options,
                    state,
                )
            )
        if any(f.kind in {"file", "unknown"} or f.state != "confirmed" for f in fields):
            return FormScan(fields, "indeterminate", "есть неподдерживаемое или неразмеченное поле")
        return FormScan(fields)
    except PlaywrightError as exc:
        return FormScan(state="indeterminate", reason=f"ошибка чтения DOM: {exc}")


def apply_answers(page: Page, scan: FormScan, answers: dict[str, str]) -> tuple[bool, list[str]]:
    """Fill only exact, configured matches. Never clicks navigation/submit."""
    missing: list[str] = []
    normalized = {normalize(k): v for k, v in answers.items()}
    for form_field in scan.fields:
        value = normalized.get(form_field.label)
        if value is None:
            if form_field.required:
                missing.append(form_field.label or "<без подписи>")
            continue
        loc = page.locator(form_field.selector)
        if form_field.kind == "radio":
            if normalize(value) not in form_field.options:
                missing.append(form_field.label)
            else:
                # form_field.selector already scopes to this exact radio group
                # (input[type='radio'][name=...]) inside the single confirmed
                # form scan_form validated — never search the whole page.
                for option in loc.all():
                    if normalize(_radio_label(page, option)) == normalize(value):
                        option.check()
                        break
        elif form_field.kind in {"text", "email", "tel", "number"}:
            loc.fill(value)
        elif form_field.kind == "select":
            if normalize(value) not in form_field.options:
                missing.append(form_field.label)
            else:
                loc.select_option(label=value)
        else:
            missing.append(form_field.label)
    return not missing, missing


def match_answer_llm(question: str, known_data: dict[str, str], client) -> str | None:
    """Return a known value whose meaning matches *question*, or ``None``.

    The model is only a classifier: it must select a key from the supplied
    facts and report a high confidence.  The selected value is then looked up
    locally, so a generated answer can never reach a form.
    """
    if not known_data:
        return None
    # Only the field NAMES are sent to the model — it is a key-classifier and
    # never needs the underlying values, which may hold PII (phone, email).
    # The selected value is looked up locally from known_data below, so no
    # contact data ever leaves the process.
    prompt = (
        "Сопоставь вопрос анкеты с одним из известных полей кандидата по названию поля. "
        "Не придумывай ответ. Верни только JSON вида "
        '{"key": "точный ключ или null", "confidence": 0.0}. '
        "Выбирай ключ только если поле действительно отвечает на вопрос; "
        "иначе key=null. Уверенный порог: confidence >= 0.85.\n"
        + json.dumps({"question": question, "known_keys": sorted(known_data)}, ensure_ascii=False)
    )
    try:
        response = client.chat([{"role": "user", "content": prompt}], temperature=0)
        raw = response.content if response else None
        if not raw:
            return None
        data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        key = data.get("key") if isinstance(data, dict) else None
        confidence = data.get("confidence") if isinstance(data, dict) else None
        if (
            isinstance(key, str)
            and key in known_data
            and not _LLM_DENIED_KEY_PATTERN.search(key)
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and confidence >= 0.85
        ):
            return known_data[key]
    except Exception:  # noqa: BLE001 — любой сбой LLM/транспорта -> откат к exact-match,
        # см. тот же паттерн в ai/letters.py и scoring.py.
        return None
    return None


def resolve_answers(
    scan: FormScan,
    answers: dict[str, str],
    *,
    known_data: dict[str, str] | None = None,
    client=None,
) -> tuple[dict[str, str], set[str]]:
    """Merge exact profile answers with safe semantic matches from known data.

    Returns ``(resolved, llm_matched_labels)``: ``apply_answers`` treats every
    key of ``resolved`` alike ("exact, configured match"), so the LLM-derived
    subset must stay visible to the caller — the dry-run output reports which
    labels were filled by inference rather than by an exact configured/known
    match (#280 review round 2: an LLM guess must never look identical to a
    user-approved answer in the reviewable dump).
    """
    resolved = dict(answers)
    normalized = {normalize(key) for key in resolved}
    facts = known_data or {}
    llm_matched: set[str] = set()
    for form_field in getattr(scan, "fields", []):
        if not form_field.label or normalize(form_field.label) in normalized or client is None:
            continue
        value = match_answer_llm(form_field.label, facts, client)
        if value is not None:
            resolved[form_field.label] = value
            normalized.add(normalize(form_field.label))
            llm_matched.add(form_field.label)
    return resolved, llm_matched

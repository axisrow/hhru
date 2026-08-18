"""Generic, fail-closed DOM inspection for external forms.

The module deliberately knows nothing about Yandex Forms.  It reads accessible
labels and control types, and never clicks a button or submits a form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

_SPACE = re.compile(r"\s+")


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
                selector = f"input[type='radio'][name='{name}']"
                required = (
                    group_node.get_attribute("aria-required") == "true"
                    or "обязательное поле" in label.casefold()
                )
            else:
                label = _label(page, control)
                required = (
                    control.get_attribute("aria-required") or ""
                ).casefold() == "true" or control.get_attribute("required") is not None
                selector = (
                    f"#{control.get_attribute('id')}"
                    if control.get_attribute("id")
                    else f"{kind}:nth-of-type({i + 1})"
                )
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
                # Radio groups are identified by their accessible label; only
                # check the exact option, and do not activate the form's buttons.
                for option in page.locator("input[type='radio']").all():
                    if normalize(_radio_label(page, option)) == normalize(value):
                        option.check()
                        break
        elif form_field.kind in {"text", "email", "tel", "number"}:
            loc.fill(value)
        else:
            missing.append(form_field.label)
    return not missing, missing

"""Reusable questionnaire templates and keyword/LLM resolution (#482)."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config_sections.ai_profile import AIProfile
    from .config_sections.questionnaires import QuestionnaireConfig
    from .history import History
    from .search import VacancyCard

CLUSTERS = frozenset(
    {
        "conditions",
        "motivation",
        "expertise",
        "assessment",
        "marketing",
        "portfolio",
        "fit",
        "compliance",
        "mixed",
    }
)
MODES = frozenset({"static", "contextual"})
SCOPES = frozenset({"account", "resume"})
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SENSITIVE_RE = re.compile(
    r"паспорт|passport|снилс|инн|гражданств|воинск|военн|юридическ|"
    r"достоверност|территори[ия].*рф|находитесь.*рф",
    re.IGNORECASE,
)


def normalize_question(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def is_template_key(value: str) -> bool:
    """Return whether a user/LLM supplied template key is safe and canonical."""
    return bool(_KEY_RE.fullmatch(value))


def question_fingerprint(text: str, kind: str, options: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "text": normalize_question(text),
            "kind": kind,
            "options": [normalize_question(option) for option in options],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class QuestionnaireTemplate:
    key: str
    label: str
    cluster: str
    default_mode: str
    default_scope: str
    instruction: str = ""
    sensitive: bool = False
    source: str = "seed"
    confirmed: bool = True


@dataclass(frozen=True)
class TemplateMatch:
    template: QuestionnaireTemplate
    source: str
    confidence: float
    first_use: bool = False


@dataclass(frozen=True)
class ResolvedQuestion:
    status: str
    answer: str = ""
    choice_labels: tuple[str, ...] = ()
    confidence: float = 0.0
    answer_source: str = ""
    template_key: str = ""
    cluster: str = ""
    match_source: str = ""
    match_confidence: float = 0.0
    confirmed: bool = False
    reason: str = ""


SEED_TEMPLATES = (
    QuestionnaireTemplate(
        "salary",
        "Зарплатные ожидания",
        "conditions",
        "contextual",
        "resume",
        "Ответь о зарплатных ожиданиях правдиво и в формате текущего вопроса.",
    ),
    QuestionnaireTemplate(
        "location", "Город / страна проживания", "conditions", "static", "account"
    ),
    QuestionnaireTemplate(
        "desired_role",
        "Желаемая роль, функционал и задачи",
        "motivation",
        "contextual",
        "resume",
        "Опиши желаемую роль и задачи на основе профиля кандидата.",
    ),
    QuestionnaireTemplate(
        "business_segments",
        "Сегменты бизнеса",
        "expertise",
        "static",
        "resume",
    ),
)


def sync_seed_templates(history: History) -> None:
    for template in SEED_TEMPLATES:
        history.upsert_questionnaire_template(
            template.key,
            template.label,
            template.cluster,
            template.default_mode,
            template.default_scope,
            instruction=template.instruction,
            sensitive=template.sensitive,
            source="seed",
            confirmed=True,
        )


_KEYWORD_RULES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "salary",
        tuple(
            re.compile(pattern)
            for pattern in (
                r"\bзарплат",
                r"\bзаработн\w* плат",
                r"\bуровень доход",
                r"\bоклад",
                r"\bуровень оплат",
            )
        ),
    ),
    (
        "location",
        tuple(
            re.compile(pattern)
            for pattern in (
                r"\bв каком городе\b",
                r"\bгород\w* прожив",
                r"\bгде\w* жив",
                r"\bстрана\w* прожив",
            )
        ),
    ),
    (
        "desired_role",
        tuple(
            re.compile(pattern)
            for pattern in (
                r"\bкакая роль\b",
                r"\bроль\w* интерес",
                r"\bкакие задачи\w* хотел",
                r"\bжелаем\w* рол",
            )
        ),
    ),
    (
        "business_segments",
        tuple(
            re.compile(pattern)
            for pattern in (
                r"\bсегмент\w* бизнеса\b",
                r"\bb2b\b.*\bb2c\b",
                r"\bb2c\b.*\bb2b\b",
            )
        ),
    ),
)


def _template_from_row(row: dict) -> QuestionnaireTemplate:
    return QuestionnaireTemplate(
        key=row["template_key"],
        label=row["label"],
        cluster=row["cluster"],
        default_mode=row["default_mode"],
        default_scope=row["default_scope"],
        instruction=row.get("instruction") or "",
        sensitive=bool(row.get("sensitive")),
        source=row.get("source", "user"),
        confirmed=bool(row.get("confirmed", True)),
    )


class QuestionnaireResolver:
    """Resolve one visible question using stored facts, keywords, then LLM."""

    def __init__(
        self,
        history: History,
        config: QuestionnaireConfig,
        *,
        llm=None,
        profile: AIProfile | None = None,
        known_data: dict[str, str] | None = None,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ):
        self.history = history
        self.config = config
        self.llm = llm
        self.profile = profile
        self.known_data = known_data or {}
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.vacancy: VacancyCard | None = None
        self.resume_id = ""
        self.interactive = False
        sync_seed_templates(self.history)

    def set_context(
        self, vacancy: VacancyCard, resume_id: str, *, interactive: bool = False
    ) -> None:
        self.vacancy = vacancy
        self.resume_id = resume_id
        self.interactive = interactive

    def _templates(self) -> dict[str, QuestionnaireTemplate]:
        return {
            row["template_key"]: _template_from_row(row)
            for row in self.history.list_questionnaire_templates()
            if row.get("confirmed")
        }

    @staticmethod
    def _finite_confidence(value) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        result = float(value)
        return result if math.isfinite(result) and 0.0 <= result <= 1.0 else None

    def _keyword_match(self, text: str, templates: dict[str, QuestionnaireTemplate]):
        normalized = normalize_question(text)
        matches = []
        for template_key, patterns in _KEYWORD_RULES:
            if template_key in templates and any(
                pattern.search(normalized) for pattern in patterns
            ):
                matches.append(template_key)
        if len(matches) != 1:
            return None
        return TemplateMatch(templates[matches[0]], "keyword", 1.0)

    def _match(
        self, text: str, kind: str, options: tuple[str, ...]
    ) -> tuple[TemplateMatch | None, dict[str, object] | None]:
        fingerprint = question_fingerprint(text, kind, options)
        templates = self._templates()
        alias = self.history.get_questionnaire_alias(fingerprint)
        if alias is not None and alias["template_key"] in templates:
            return TemplateMatch(templates[alias["template_key"]], "alias", 1.0), None
        keyword = self._keyword_match(text, templates)
        if keyword is not None:
            self.history.upsert_questionnaire_alias(
                fingerprint,
                text,
                normalize_question(text),
                kind,
                options,
                keyword.template.key,
                keyword.template.cluster,
                source="keyword",
                confirmed=True,
            )
            return keyword, None
        proposal = self._llm_proposal(text, kind, options, templates)
        if proposal is None:
            return None, None
        confidence = self._finite_confidence(proposal.get("match_confidence"))
        if confidence is None or confidence < self.config.llm_match_threshold:
            return None, proposal
        template_key = proposal.get("template_key")
        if isinstance(template_key, str) and template_key in templates:
            template = templates[template_key]
        else:
            new_template = self._new_template(proposal)
            if new_template is None:
                return None, proposal
            template = new_template
        return TemplateMatch(template, "llm", confidence, first_use=True), proposal

    def _new_template(self, proposal: dict[str, object]) -> QuestionnaireTemplate | None:
        key = proposal.get("template_key")
        label = proposal.get("label")
        cluster = proposal.get("cluster")
        mode = proposal.get("mode")
        scope = proposal.get("scope")
        if (
            not isinstance(key, str)
            or not is_template_key(key)
            or not isinstance(label, str)
            or not label.strip()
            or cluster not in CLUSTERS
            or mode not in MODES
            or scope not in SCOPES
        ):
            return None
        sensitive = bool(proposal.get("sensitive")) or cluster == "compliance"
        if sensitive and mode != "static":
            return None
        instruction = proposal.get("instruction")
        return QuestionnaireTemplate(
            key,
            label.strip(),
            str(cluster),
            str(mode),
            str(scope),
            instruction.strip() if isinstance(instruction, str) else "",
            sensitive,
            source="llm",
            confirmed=False,
        )

    def _profile_context(self) -> dict[str, object]:
        if self.profile is None:
            return {}
        return {
            "summary": self.profile.summary,
            "skills": self.profile.skills,
            "highlights": self.profile.highlights,
            "desired_role": self.profile.desired_role,
        }

    def _vacancy_context(self) -> dict[str, str]:
        if self.vacancy is None:
            return {}
        return {
            "title": self.vacancy.title,
            "company": self.vacancy.company,
            "text": self.vacancy.vacancy_text,
        }

    def _llm_proposal(
        self,
        text: str,
        kind: str,
        options: tuple[str, ...],
        templates: dict[str, QuestionnaireTemplate],
    ) -> dict[str, object] | None:
        if self.llm is None:
            return None
        catalog = [
            {
                "template_key": item.key,
                "label": item.label,
                "cluster": item.cluster,
                "mode": item.default_mode,
                "scope": item.default_scope,
                "sensitive": item.sensitive,
            }
            for item in templates.values()
        ]
        prompt = {
            "question": text,
            "kind": kind,
            "options": options,
            "templates": catalog,
            "profile": self._profile_context(),
            "vacancy": self._vacancy_context(),
        }
        system = (
            "Сопоставь вопрос анкеты с существующим шаблоном или предложи новый. "
            "Не выдумывай факты. Верни только JSON с полями template_key, label, "
            "cluster, mode (static/contextual), scope (account/resume), sensitive, "
            "instruction, match_confidence, answer_confidence и answer. "
            "answer имеет поля text и choices. Для radio choices содержит ровно один "
            "видимый вариант, для checkbox один или несколько."
        )
        try:
            response = self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                temperature=0.1,
            )
            raw = (response.content or "").strip()
            value = json.loads(raw.removeprefix("```json").removesuffix("```").strip())
            return value if isinstance(value, dict) else None
        except Exception:  # noqa: BLE001 - fallback must remain fail-closed
            return None

    def _known_answer(self, template: QuestionnaireTemplate, question_text: str) -> str | None:
        normalized = {normalize_question(key): value for key, value in self.known_data.items()}
        exact = normalized.get(normalize_question(question_text))
        if isinstance(exact, str) and exact.strip():
            return exact.strip()
        if template.key == "location":
            for key in ("город", "место проживания", "локация"):
                value = normalized.get(normalize_question(key))
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if template.key == "desired_role" and self.profile and self.profile.desired_role.strip():
            return self.profile.desired_role.strip()
        return None

    @staticmethod
    def _answer_from_payload(
        payload: dict[str, object], kind: str, options: tuple[str, ...]
    ) -> tuple[str, tuple[str, ...]] | None:
        answer = payload.get("answer")
        if isinstance(answer, dict):
            text = answer.get("text", "")
            choices = answer.get("choices", [])
        else:
            text = payload.get("text", "")
            choices = payload.get("choices", [])
        text = text.strip() if isinstance(text, str) else ""
        if kind == "text":
            return (text, ()) if text else None
        if (not isinstance(choices, list) or not choices) and text:
            choices = [item.strip() for item in text.split("|") if item.strip()]
        if (
            not isinstance(choices, list)
            or not choices
            or not all(isinstance(choice, str) and choice.strip() for choice in choices)
        ):
            return None
        visible = {normalize_question(option): option for option in options}
        resolved = []
        for choice in choices:
            option = visible.get(normalize_question(choice))
            if option is None:
                return None
            resolved.append(option)
        return text or ", ".join(resolved), tuple(resolved)

    def _stored_answer(
        self,
        template: QuestionnaireTemplate,
        text: str,
        kind: str,
        options: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...], str] | None:
        stored = self.history.get_questionnaire_answer(template.key, self.resume_id)
        if stored is not None and stored["mode"] == "static":
            answer = self._answer_from_payload(stored["payload"], kind, options)
            if answer is not None:
                return answer[0], answer[1], "template"
        known = self._known_answer(template, text)
        if known is not None:
            answer = self._answer_from_payload(
                {"text": known, "choices": [known] if kind == "choice" else []},
                kind,
                options,
            )
            if answer is not None:
                return answer[0], answer[1], "profile"
        return None

    def _contextual_answer(
        self,
        template: QuestionnaireTemplate,
        text: str,
        kind: str,
        options: tuple[str, ...],
        proposal: dict[str, object] | None,
    ) -> tuple[str, tuple[str, ...], float] | None:
        if self.llm is None or template.sensitive or _SENSITIVE_RE.search(text):
            return None
        stored = self.history.get_questionnaire_answer(template.key, self.resume_id)
        if proposal is None:
            instruction = (
                stored["payload"].get("instruction", template.instruction)
                if stored and stored["mode"] == "contextual"
                else template.instruction
            )
            examples = (
                stored["payload"].get("examples", [])
                if stored and stored["mode"] == "contextual"
                else []
            )
            prompt = {
                "question": text,
                "kind": kind,
                "options": options,
                "instruction": instruction,
                "examples": examples,
                "profile": self._profile_context(),
                "vacancy": self._vacancy_context(),
            }
            try:
                response = self.llm.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Сформируй правдивый ответ по данным профиля. Верни только "
                                'JSON: {"answer":{"text":"","choices":[]},'
                                '"answer_confidence":0.0}. Не добавляй отсутствующие факты.'
                            ),
                        },
                        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                    ],
                    temperature=0.2,
                )
                raw = (response.content or "").strip()
                proposal = json.loads(raw.removeprefix("```json").removesuffix("```").strip())
            except Exception:  # noqa: BLE001
                return None
        confidence = self._finite_confidence(proposal.get("answer_confidence"))
        if confidence is None or confidence < self.config.llm_answer_threshold:
            return None
        answer = self._answer_from_payload(proposal, kind, options)
        if answer is None:
            return None
        return answer[0], answer[1], confidence

    def _confirm(
        self,
        match: TemplateMatch,
        proposal: dict[str, object],
        text: str,
        kind: str,
        options: tuple[str, ...],
    ) -> tuple[QuestionnaireTemplate, dict[str, object]] | None:
        template = match.template
        self.output_fn(f"[INFO] Новый вопрос: {text}")
        self.output_fn(
            f"[INFO] Предложение: {template.key} [{template.cluster}] — {template.label}"
        )
        self.output_fn(f"[INFO] Ответ: {json.dumps(proposal.get('answer'), ensure_ascii=False)}")
        while True:
            action = (
                self.input_fn(
                    "Подтвердить [y], изменить [e], сопоставить [m], новый [n], пропустить [s]? "
                )
                .strip()
                .lower()
            )
            if action in {"y", "yes", "д", "да"}:
                break
            if action == "s":
                return None
            if action == "m":
                key = self.input_fn("Ключ существующего шаблона: ").strip()
                row = self.history.get_questionnaire_template(key)
                if row is None:
                    self.output_fn("[FAIL] Шаблон не найден")
                    continue
                template = _template_from_row(row)
                break
            if action == "n":
                key = self.input_fn("Ключ нового шаблона: ").strip()
                label = self.input_fn("Название: ").strip()
                cluster = self.input_fn("Кластер: ").strip()
                mode = self.input_fn("Режим static/contextual: ").strip()
                scope = self.input_fn("Область account/resume: ").strip()
                candidate = self._new_template(
                    {
                        "template_key": key,
                        "label": label,
                        "cluster": cluster,
                        "mode": mode,
                        "scope": scope,
                        "sensitive": cluster == "compliance",
                    }
                )
                if candidate is None:
                    self.output_fn("[FAIL] Некорректные параметры шаблона")
                    continue
                template = candidate
                break
            if action == "e":
                value = self.input_fn("Новый текст ответа: ").strip()
                if value:
                    proposal = dict(proposal)
                    choices = (
                        [item.strip() for item in value.split(",") if item.strip()]
                        if kind == "choice"
                        else []
                    )
                    proposal["answer"] = {"text": value, "choices": choices}
                    proposal["answer_confidence"] = 1.0
                    proposal["_user_edited_answer"] = True
                    break
                continue
            self.output_fn("[INFO] Выберите y/e/m/n/s")
        if (template.sensitive or _SENSITIVE_RE.search(text)) and not proposal.get(
            "_user_edited_answer"
        ):
            if kind == "choice":
                self.output_fn("[INFO] Варианты: " + " | ".join(options))
            value = self.input_fn("Введите явный ответ (пусто = пропустить): ").strip()
            if not value:
                return None
            choices = (
                [item.strip() for item in value.split(",") if item.strip()]
                if kind == "choice"
                else []
            )
            proposal = dict(proposal)
            proposal["answer"] = {"text": value, "choices": choices}
            proposal["answer_confidence"] = 1.0
            proposal["_user_edited_answer"] = True
        confirmed = QuestionnaireTemplate(
            **{**asdict(template), "source": "user", "confirmed": True}
        )
        return confirmed, proposal

    def _manual_match(
        self,
        proposal: dict[str, object] | None,
        text: str,
        kind: str,
        options: tuple[str, ...],
    ) -> tuple[QuestionnaireTemplate, dict[str, object]] | None:
        """Ask for a template when keyword and confident LLM matching both failed."""
        templates = self._templates()
        candidate: QuestionnaireTemplate | None = None
        if proposal is not None:
            key = proposal.get("template_key")
            if isinstance(key, str) and key in templates:
                candidate = templates[key]
            else:
                candidate = self._new_template(proposal)
        if candidate is not None:
            assert proposal is not None
            confidence = self._finite_confidence(proposal.get("match_confidence")) or 0.0
            return self._confirm(
                TemplateMatch(candidate, "llm", confidence, first_use=True),
                proposal,
                text,
                kind,
                options,
            )

        self.output_fn(f"[INFO] Новый вопрос: {text}")
        while True:
            action = (
                self.input_fn("Сопоставить с шаблоном [m], создать новый [n], пропустить [s]? ")
                .strip()
                .lower()
            )
            if action == "s":
                return None
            if action == "m":
                key = self.input_fn("Ключ существующего шаблона: ").strip()
                row = self.history.get_questionnaire_template(key)
                if row is None:
                    self.output_fn("[FAIL] Шаблон не найден")
                    continue
                template = _template_from_row(row)
                return template, {}
            if action == "n":
                key = self.input_fn("Ключ нового шаблона: ").strip()
                label = self.input_fn("Название: ").strip()
                cluster = self.input_fn("Кластер: ").strip()
                mode = self.input_fn("Режим static/contextual: ").strip()
                scope = self.input_fn("Область account/resume: ").strip()
                instruction = (
                    self.input_fn("Инструкция для будущих ответов: ").strip()
                    if mode == "contextual"
                    else ""
                )
                new_template = self._new_template(
                    {
                        "template_key": key,
                        "label": label,
                        "cluster": cluster,
                        "mode": mode,
                        "scope": scope,
                        "instruction": instruction,
                        "sensitive": cluster == "compliance",
                    }
                )
                if new_template is None:
                    self.output_fn("[FAIL] Некорректные параметры шаблона")
                    continue
                return QuestionnaireTemplate(
                    **{**asdict(new_template), "source": "user", "confirmed": True}
                ), {}
            self.output_fn("[INFO] Выберите m/n/s")

    def _interactive_answer(
        self,
        template: QuestionnaireTemplate,
        match: TemplateMatch,
        text: str,
        kind: str,
        options: tuple[str, ...],
    ) -> ResolvedQuestion | None:
        """Collect and persist an explicit answer without asking the LLM to invent facts."""
        if kind == "choice":
            self.output_fn("[INFO] Варианты: " + " | ".join(options))
        while True:
            value = self.input_fn("Введите ответ (пусто = пропустить): ").strip()
            if not value:
                return None
            choices = (
                [item.strip() for item in value.split(",") if item.strip()]
                if kind == "choice"
                else []
            )
            proposal: dict[str, object] = {
                "answer": {"text": value, "choices": choices},
                "answer_confidence": 1.0,
                "_user_edited_answer": True,
            }
            answer = self._answer_from_payload(proposal, kind, options)
            if answer is None:
                self.output_fn("[FAIL] Ответ не совпадает с видимыми вариантами")
                continue
            self._persist_confirmation(template, proposal, text, kind, options)
            return ResolvedQuestion(
                "resolved",
                answer[0],
                answer[1],
                1.0,
                "user",
                template.key,
                template.cluster,
                match.source,
                match.confidence,
                True,
            )

    def _persist_mapping(
        self,
        template: QuestionnaireTemplate,
        text: str,
        kind: str,
        options: tuple[str, ...],
    ) -> None:
        self.history.upsert_questionnaire_template(
            template.key,
            template.label,
            template.cluster,
            template.default_mode,
            template.default_scope,
            instruction=template.instruction,
            sensitive=template.sensitive,
            source="user",
            confirmed=True,
        )
        fingerprint = question_fingerprint(text, kind, options)
        self.history.upsert_questionnaire_alias(
            fingerprint,
            text,
            normalize_question(text),
            kind,
            options,
            template.key,
            template.cluster,
            source="user",
            confirmed=True,
        )

    def _persist_confirmation(
        self,
        template: QuestionnaireTemplate,
        proposal: dict[str, object],
        text: str,
        kind: str,
        options: tuple[str, ...],
    ) -> None:
        self._persist_mapping(template, text, kind, options)
        scope_id = self.resume_id if template.default_scope == "resume" else ""
        existing_answer = self.history.get_questionnaire_answer(template.key, self.resume_id)
        if existing_answer is not None and not proposal.get("_user_edited_answer"):
            return
        answer = self._answer_from_payload(proposal, kind, options)
        if template.default_mode == "static" and answer is not None:
            payload: dict[str, object] = {"text": answer[0], "choices": list(answer[1])}
        else:
            answer_value = proposal.get("answer")
            example = answer_value.get("text", "") if isinstance(answer_value, dict) else ""
            payload = {
                "instruction": template.instruction,
                "examples": [example] if isinstance(example, str) and example else [],
            }
        self.history.set_questionnaire_answer(
            template.key,
            scope_id=scope_id,
            mode=template.default_mode,
            payload=payload,
            source="user",
            confirmed=True,
        )

    def _pending(
        self,
        text: str,
        kind: str,
        options: tuple[str, ...],
        proposal: dict[str, object] | None,
        reason: str,
    ) -> ResolvedQuestion:
        self.history.enqueue_questionnaire_pending(
            question_fingerprint(text, kind, options),
            self.resume_id,
            self.vacancy.vacancy_id if self.vacancy else None,
            text,
            kind,
            options,
            proposal=proposal,
            reason=reason,
        )
        return ResolvedQuestion(status="pending", reason=reason)

    def resolve(self, question) -> ResolvedQuestion:
        text = question.text
        kind = question.kind
        options = tuple(question.options)
        match, proposal = self._match(text, kind, options)
        if match is None:
            if not self.interactive:
                return self._pending(text, kind, options, proposal, "шаблон не определён")
            confirmation = self._manual_match(proposal, text, kind, options)
            if confirmation is None:
                return self._pending(text, kind, options, proposal, "пользователь пропустил вопрос")
            template, proposal = confirmation
            match = TemplateMatch(template, "user", 1.0)
            if self._answer_from_payload(proposal, kind, options) is not None:
                self._persist_confirmation(template, proposal, text, kind, options)
            else:
                self._persist_mapping(template, text, kind, options)
        template = match.template
        if match.first_use:
            if not self.interactive or proposal is None:
                return self._pending(
                    text, kind, options, proposal, "первое LLM-сопоставление не подтверждено"
                )
            confirmation = self._confirm(match, proposal, text, kind, options)
            if confirmation is None:
                return self._pending(text, kind, options, proposal, "пользователь пропустил вопрос")
            template, proposal = confirmation
            self._persist_confirmation(template, proposal, text, kind, options)
        stored = self._stored_answer(template, text, kind, options)
        if stored is not None:
            return ResolvedQuestion(
                "resolved",
                stored[0],
                stored[1],
                1.0,
                stored[2],
                template.key,
                template.cluster,
                match.source,
                match.confidence,
                True,
            )
        if template.sensitive or _SENSITIVE_RE.search(text):
            if self.interactive:
                explicit = self._interactive_answer(template, match, text, kind, options)
                if explicit is not None:
                    return explicit
            return self._pending(
                text, kind, options, proposal, "чувствительное поле требует явного ответа"
            )
        answer_config = self.history.get_questionnaire_answer(template.key, self.resume_id)
        effective_mode = answer_config["mode"] if answer_config else template.default_mode
        if effective_mode != "contextual":
            if self.interactive:
                explicit = self._interactive_answer(template, match, text, kind, options)
                if explicit is not None:
                    return explicit
            return self._pending(
                text, kind, options, proposal, "статическое поле требует явного ответа"
            )
        contextual = self._contextual_answer(template, text, kind, options, proposal)
        if contextual is None:
            if self.interactive:
                explicit = self._interactive_answer(template, match, text, kind, options)
                if explicit is not None:
                    return explicit
            return self._pending(text, kind, options, proposal, "ответ не определён уверенно")
        return ResolvedQuestion(
            "resolved",
            contextual[0],
            contextual[1],
            contextual[2],
            "llm",
            template.key,
            template.cluster,
            match.source,
            match.confidence,
            template.confirmed or match.first_use,
        )

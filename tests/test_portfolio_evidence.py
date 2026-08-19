"""Fixtures for vacancy portfolio-evidence detection (#348)."""

import pytest

from hhru_bot.portfolio_evidence import (
    PortfolioEvidenceRequirement,
    classify_portfolio_evidence,
    detect_portfolio_evidence,
)
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "text",
    [
        "Пришлите ссылки на GitHub и примеры проектов.",
        "Please attach links to your portfolio, demos or deployed services.",
        "Обязательно приложите кейсы и ссылки на рабочие сервисы.",
    ],
)
def test_explicit_links_are_detected_and_source_is_preserved(text):
    result = detect_portfolio_evidence(text)
    assert result.level == "required"
    assert result.evidence == (text,)
    assert result.source == "keyword"


@pytest.mark.parametrize(
    "text",
    [
        "GitHub Actions будет использоваться в CI.",
        "Ссылка на GitHub компании находится на сайте.",
        "Опыт с Telegram-ботами и GitLab обязателен.",
    ],
)
def test_incidental_mentions_do_not_create_requirement(text):
    assert detect_portfolio_evidence(text).level == "none"


def test_optional_request_is_preferred():
    result = detect_portfolio_evidence("Портфолио и ссылки на проекты будут плюсом.")
    assert result == PortfolioEvidenceRequirement(
        "preferred",
        evidence=("Портфолио и ссылки на проекты будут плюсом.",),
        rationale="explicit evidence request detected by keyword rules",
        confidence=0.82,
        source="keyword",
    )


class _LLM:
    def __init__(self, response):
        self.response = response

    def classify(self, text):  # noqa: ARG002
        return self.response


def test_llm_refines_keyword_result_but_keeps_source_text():
    result = classify_portfolio_evidence(
        "Please include GitHub links.",
        _LLM('{"level":"preferred","confidence":0.9,"rationale":"Useful evidence"}'),
    )
    assert result.level == "preferred"
    assert result.evidence == ("Please include GitHub links.",)
    assert result.source == "keyword+llm"


def test_llm_failure_falls_back_to_keyword_result():
    result = classify_portfolio_evidence("Please include GitHub links.", _LLM("not json"))
    assert result.level == "preferred"
    assert result.source == "keyword"


class _RaisingLLM:
    def __init__(self, exc):
        self.exc = exc

    def classify(self, text):  # noqa: ARG002
        raise self.exc


def test_llm_transport_failure_falls_back_to_keyword_result():
    """A real LLM client can raise transport errors (network/timeout), not
    just malformed-JSON errors.  The documented contract (transport failures
    fall back to the keyword result) must hold for those too."""
    result = classify_portfolio_evidence(
        "Please include GitHub links.", _RaisingLLM(ConnectionError("upstream unreachable"))
    )
    assert result.level == "preferred"
    assert result.source == "keyword"


def test_vacancy_card_exposes_signal_for_downstream_consumers():
    card = VacancyCard(
        vacancy_id="1",
        title="Developer",
        company="Acme",
        url="https://hh.ru/vacancy/1",
        vacancy_text="Please include links to your GitHub projects.",
    )
    assert card.portfolio_evidence_requirement is not None
    assert card.portfolio_evidence_requirement.level == "preferred"


def test_evidence_sentence_is_truncated_to_avoid_unbounded_prompt_injection():
    """A pathologically long, unpunctuated vacancy sentence must not produce
    unbounded evidence text: unbounded evidence flows verbatim into the LLM
    scoring prompt (scoring.py), risking prompt-size/token inflation and
    injection attempts from adversarial vacancy descriptions."""
    long_tail = "x" * 5000
    text = f"Please attach a link to your GitHub portfolio {long_tail}."
    result = detect_portfolio_evidence(text)
    assert result.level != "none"
    assert len(result.evidence) == 1
    assert len(result.evidence[0]) <= 501

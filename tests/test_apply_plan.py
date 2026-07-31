"""TDD #101: build_apply_plan — чистая план-конструкция, вынесенная из run_apply_for_resume.

Проверяет контракты filter -> pre-LLM -> rank -> limit без браузера: собирает
из filter_candidates/rank_candidates ApplyPlan (ранжированные кандидаты +
статистика). Реюзает фикстуры-паттерны test_apply_scoring_integration.py.
"""

from __future__ import annotations

from hhru_bot.commands._common import ApplyPlan, build_apply_plan
from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.history import History
from hhru_bot.search import VacancyCard


def _card(vacancy_id: str, title: str = "Python Dev", company: str = "Acme") -> VacancyCard:
    return VacancyCard(
        vacancy_id=vacancy_id,
        title=title,
        company=company,
        url=f"https://hh.ru/vacancy/{vacancy_id}",
    )


def _resume(search: SearchFilters | None = None, **kwargs) -> ResumeConfig:
    return ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=search if search is not None else SearchFilters(text="python developer"),
        **kwargs,
    )


def _history(tmp_path) -> History:
    return History(tmp_path / "history.db")


def test_build_apply_plan_returns_all_candidates_without_limit(tmp_path):
    cards = [_card("1"), _card("2"), _card("3")]
    resume = _resume()
    history = _history(tmp_path)

    plan = build_apply_plan(cards, resume.search, resume, history)

    assert isinstance(plan, ApplyPlan)
    assert [c.vacancy_id for c, _score, _breakdown in plan.ranked] == ["1", "2", "3"]
    assert plan.total == 3
    assert plan.after_filter == 3
    assert plan.after_limit == 3
    assert plan.skipped == []


def test_build_apply_plan_filters_already_applied(tmp_path):
    cards = [_card("1"), _card("2")]
    resume = _resume()
    history = _history(tmp_path)
    history.record_action(resume.resume_id, "1", "apply", "success", "ok")

    plan = build_apply_plan(cards, resume.search, resume, history)

    assert [c.vacancy_id for c, _s, _b in plan.ranked] == ["2"]
    assert plan.total == 2
    assert plan.after_filter == 1
    assert plan.after_limit == 1
    assert len(plan.skipped) == 1
    assert plan.skipped[0][0].vacancy_id == "1"


def test_build_apply_plan_filters_stoplist_employer(tmp_path):
    cards = [_card("1", company="BadCorp"), _card("2", company="GoodCorp")]
    resume = _resume(search=SearchFilters(text="python", exclude_employers=["BadCorp"]))
    history = _history(tmp_path)

    plan = build_apply_plan(cards, resume.search, resume, history)

    assert [c.vacancy_id for c, _s, _b in plan.ranked] == ["2"]
    assert plan.after_filter == 1


def test_build_apply_plan_applies_limit_after_ranking(tmp_path):
    cards = [_card("1", title="Junior PHP"), _card("2", title="Senior Python")]
    resume = _resume(
        search=SearchFilters(text="python", must_have=["python"]),
    )
    history = _history(tmp_path)

    plan = build_apply_plan(cards, resume.search, resume, history, limit=1)

    assert plan.total == 2
    assert plan.after_filter == 2
    assert plan.after_limit == 1
    # Без scoring-секции веса нейтральны (_ZERO_WEIGHTS) -> порядок входа сохранён,
    # даже если "Senior Python" совпадает с must_have по названию сильнее.
    assert [c.vacancy_id for c, _s, _b in plan.ranked] == ["1"]


def test_build_apply_plan_limit_none_means_no_slice(tmp_path):
    cards = [_card(str(i)) for i in range(5)]
    resume = _resume()
    history = _history(tmp_path)

    plan = build_apply_plan(cards, resume.search, resume, history, limit=None)

    assert plan.after_limit == 5


def test_build_apply_plan_limit_zero_means_no_slice(tmp_path):
    """CLI-конвенция: args.limit=0 ('--limit' не передан) означает "без лимита"."""
    cards = [_card(str(i)) for i in range(4)]
    resume = _resume()
    history = _history(tmp_path)

    plan = build_apply_plan(cards, resume.search, resume, history, limit=0)

    assert plan.after_limit == 4


def test_build_apply_plan_uses_scoring_provider_when_given(tmp_path):
    cards = [_card("1", title="Junior"), _card("2", title="Senior")]
    resume = _resume()
    history = _history(tmp_path)

    class _FixedScoreProvider:
        def score(self, card, resume_profile=None):  # noqa: ARG002
            from hhru_bot.scoring import ScoreOutcome

            score = 90.0 if card.vacancy_id == "1" else 10.0
            return ScoreOutcome(score_0_100=score, breakdown={}, rationale="")

    plan = build_apply_plan(
        cards, resume.search, resume, history, scoring_provider=_FixedScoreProvider()
    )

    assert [c.vacancy_id for c, _s, _b in plan.ranked] == ["1", "2"]


def test_build_apply_plan_empty_candidates(tmp_path):
    resume = _resume()
    history = _history(tmp_path)

    plan = build_apply_plan([], resume.search, resume, history)

    assert plan.ranked == []
    assert plan.total == 0
    assert plan.after_filter == 0
    assert plan.after_limit == 0

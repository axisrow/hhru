"""Тесты предупреждения о неработающих must_have/nice_to_have (#121).

Ключевые слова живут в секции ``search``, а включаются секцией ``scoring``:
без неё веса нулевые и score всех вакансий = 0.00. Поведение оставлено как
есть (обратная совместимость), но молчать о нём нельзя — конфиг выглядит
настроенным, а фича не работает.

Проверяем ровно диагностику: предупреждение есть там, где фича инертна, и
нет там, где всё в порядке. Поведение загрузки конфига НЕ меняется.
"""

from __future__ import annotations

import logging

import pytest

from hhru_bot.config import load_config

pytestmark = pytest.mark.unit

_BASE = """
account:
  storage_state_file: ../data/storage_state/hh_session.json
resumes:
  - id: "r1"
    resume_url: "https://hh.ru/resume/abc123"
    search:
      text: "python"
{extra}
"""


def _write(tmp_path, extra: str):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_BASE.format(extra=extra), encoding="utf-8")
    return cfg


_KEYWORDS = """      must_have:
        - "python"
"""

_SCORING = """    scoring:
      weights:
        must_have: 2.0
"""


def test_warns_when_keywords_without_scoring(tmp_path, caplog):
    cfg = _write(tmp_path, _KEYWORDS)
    with caplog.at_level(logging.WARNING, logger="hhru_bot.config"):
        load_config(cfg)
    assert "scoring" in caplog.text
    assert "resumes[0]" in caplog.text


def test_no_warning_when_scoring_present(tmp_path, caplog):
    cfg = _write(tmp_path, _KEYWORDS + _SCORING)
    with caplog.at_level(logging.WARNING, logger="hhru_bot.config"):
        load_config(cfg)
    assert caplog.text == ""


def test_no_warning_without_keywords(tmp_path, caplog):
    """Нет ключевых слов — нечему быть инертным, молчим."""
    cfg = _write(tmp_path, "")
    with caplog.at_level(logging.WARNING, logger="hhru_bot.config"):
        load_config(cfg)
    assert caplog.text == ""


def test_warns_for_nice_to_have_alone(tmp_path, caplog):
    cfg = _write(
        tmp_path,
        """      nice_to_have:
        - "django"
""",
    )
    with caplog.at_level(logging.WARNING, logger="hhru_bot.config"):
        load_config(cfg)
    assert "scoring" in caplog.text


def test_config_still_loads_normally(tmp_path):
    """Предупреждение — только диагностика: конфиг грузится как раньше."""
    cfg = _write(tmp_path, _KEYWORDS)
    config = load_config(cfg)
    assert config.resumes[0].id == "r1"
    assert config.resumes[0].search.must_have == ["python"]
    assert config.resumes[0].scoring is None


@pytest.mark.parametrize("extra", ["", _KEYWORDS, _KEYWORDS + _SCORING])
def test_load_never_raises_on_these_shapes(tmp_path, extra):
    load_config(_write(tmp_path, extra))

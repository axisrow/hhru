"""Тесты команды probe (#8): построение VacancyCard из URL вакансии.

Характеризация: vacancy_id должен извлекаться канонически (срез query, валидация),
а не наивным split('/')[-1] — иначе ?query-параметр попадает в vacancy_id и в имя
файла дампа. Без браузера — тестируется только чистая функция _vacancy_from_url.
"""

from __future__ import annotations

import pytest

from hhru_bot.commands.probe import _vacancy_from_url

pytestmark = pytest.mark.integration


def test_vacancy_from_url_plain():
    v = _vacancy_from_url("https://hh.ru/vacancy/12345")
    assert v.vacancy_id == "12345"
    assert v.url == "https://hh.ru/vacancy/12345"


def test_vacancy_from_url_strips_query():
    # ?query не должен попадать в vacancy_id и в имя файла дампа
    v = _vacancy_from_url("https://hh.ru/vacancy/12345?from=cl&query=x")
    assert v.vacancy_id == "12345"
    assert "?" not in v.vacancy_id


def test_vacancy_from_url_strips_trailing_slash():
    v = _vacancy_from_url("https://hh.ru/vacancy/12345/")
    assert v.vacancy_id == "12345"


def test_vacancy_from_url_invalid_id_raises():
    with pytest.raises(ValueError):
        _vacancy_from_url("https://hh.ru/vacancy/not-a-number")


def test_probe_passes_resume_id_not_config_slug(monkeypatch, tmp_path):
    """probe обязан передавать resume_id (хвост resume_url), а не слаг конфига.

    Форма отклика адресует опцию резюме как
    `[data-qa='magritte-select-option-{resume_id}']`, где resume_id — хвост
    resume_url. Слаг из конфига ("python") там не существует, поэтому
    _select_resume_in_form не находит опцию и отказывает:
        Резюме 'python' не найдено среди опций формы отклика — отправка отменена

    Боевой probe 2026-08-20 упирался именно в это. run_apply_for_resume
    (_common.py) везде использует resume.resume_id — probe расходился с ним.
    """
    from types import SimpleNamespace

    from hhru_bot.commands import probe as probe_cmd
    from hhru_bot.config import bare_resume

    # bare_resume строит resume_id из resume_url; подменяем id на слаг конфига,
    # чтобы воспроизвести реальную ситуацию «слаг != resume_id».
    real_resume_id = "00001111222233334444555566667777888899"
    resume = bare_resume(real_resume_id)
    resume.id = "python"

    captured: dict[str, str] = {}

    def _fake_probe_vacancy(_page, _vacancy, *, resume_id, **_kwargs):
        captured["resume_id"] = resume_id
        return SimpleNamespace(success=True, skipped=False, reason="", dump_paths={})

    class _NullContext:
        def __enter__(self):
            return SimpleNamespace(new_page=lambda: object())

        def __exit__(self, *_exc):
            return False

    config = SimpleNamespace(
        storage_state_file=tmp_path / "state.json",
        cover_letter_for=lambda _r: "письмо",
        ai=None,
    )
    monkeypatch.setattr("hhru_bot.apply.probe.probe_vacancy", _fake_probe_vacancy)
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *_a, **_k: _NullContext())
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _p: config)
    monkeypatch.setattr(probe_cmd, "resolve_resumes", lambda _c, _n: [resume])
    monkeypatch.setattr(probe_cmd, "_build_letter_provider", lambda *_a, **_k: None)

    args = SimpleNamespace(
        resume="python",
        headless=True,
        vacancy_id="136190065",
        vacancy_url=None,
        config=None,
        healthcheck=False,
        negotiations=False,
        questionnaires_only=False,
    )
    probe_cmd.run(args)

    assert captured["resume_id"] == real_resume_id
    assert captured["resume_id"] != "python"

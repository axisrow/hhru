"""Live-smoke AI-транспорта поверх hermes-agent-axisrow (issues #230, #237, #242).

Реальный LLM-вызов за деньги пользователя (категория live_read по #230:
мутаций нигде нет, единственный побочный эффект — токены провайдера).
По умолчанию исключён из прогона (pytest -m live_read); без явного opt-in
тихо скипается:

    HHRU_AI_LIVE=1

Запуск под pytest:
    HHRU_AI_LIVE=1 pytest -m live_read tests/test_ai_live_transport.py

Проверяет полный Hermes resolver-chain путь (в т.ч. генерацию сопроводительного
письма) с существующими Hermes credentials/config и ненулевым текстом в ответе.
Ключи и endpoint hhru не читает и не передаёт.

**Важно (issue #242): под pytest этот тест физически не может дойти до
реального ответа Hermes.** `hermes_cli/auth.py:_auth_file_path()` содержит
намеренный seatbelt-guard пакета: он отказывает, когда одновременно видит
`PYTEST_CURRENT_TEST` (её всегда ставит сам pytest вокруг выполнения теста) и
`HERMES_HOME`, резолвящийся в реальный `~/.hermes/auth.json`. Это защита
самого Hermes от порчи боевого auth-store тестами — обходить её здесь
(подменять `HERMES_HOME`, копировать credentials) нельзя, это не наш guard.

`PYTEST_CURRENT_TEST` ставит сам pytest на время КАЖДОГО теста без исключений,
поэтому под pytest guard срабатывает всегда — но переменную pytest выставляет
только в фазе setup каждого теста, ПОСЛЕ импорта модуля (`pytestmark` на
уровне модуля читает её на импорте и всегда видит `None` — не годится). Вместо
этого условие проверяет autouse-фикстура `_skip_under_pytest_seatbelt_guard`
(она выполняется в setup, когда переменная уже установлена) и скипает ДО тела
теста — без перехвата текста чужого `RuntimeError` постфактум, так тесты не
привязаны к формулировке исключения стороннего пакета, которая может
измениться в новой версии.

Для реальной проверки живого Hermes-вызова (включая генерацию письма) вне
pytest см. `docs/testing.md` (раздел "AI-транспорт (Hermes)") — там дана
команда запуска отдельным `python3`-скриптом, без pytest-раннера.
"""

from __future__ import annotations

import os

import pytest

from hhru_bot.ai.letters import AICoverLetterProvider, _build_prompt
from hhru_bot.ai.llm_client import LLMClient
from hhru_bot.config_sections.ai import AiConfig
from hhru_bot.search import VacancyCard

_LIVE = os.environ.get("HHRU_AI_LIVE", "") == "1"

_SEATBELT_GUARD_SKIP_REASON = (
    "Hermes seatbelt-guard (hermes_cli/auth.py:_auth_file_path()) блокирует "
    "доступ к ~/.hermes/auth.json, пока установлена PYTEST_CURRENT_TEST — а "
    "pytest ставит её всегда. Штатная защита пакета, не баг hhru. Реальный "
    'прогон — см. docs/testing.md, раздел "AI-транспорт (Hermes)", отдельный '
    "скрипт вне pytest."
)

pytestmark = [
    pytest.mark.live_read,
    pytest.mark.skipif(
        not _LIVE,
        reason="требуется явный opt-in HHRU_AI_LIVE=1 и настроенный Hermes",
    ),
]


@pytest.fixture(autouse=True)
def _skip_under_pytest_seatbelt_guard():
    if os.environ.get("PYTEST_CURRENT_TEST"):
        pytest.skip(_SEATBELT_GUARD_SKIP_REASON)


def test_live_single_call_returns_text():
    cfg = AiConfig()
    client = LLMClient(cfg)
    nr = client.chat(
        [{"role": "user", "content": "Ответь ровно одним словом: ping"}],
        timeout=60.0,
    )
    assert nr.content and nr.content.strip(), f"пустой ответ (finish_reason={nr.finish_reason})"


def test_live_cover_letter_uses_hermes_route(caplog):
    """Генерирует письмо через AICoverLetterProvider, не отправляя отклик."""
    client = LLMClient(AiConfig())
    vacancy = VacancyCard(
        vacancy_id="live-test-237",
        title="Python backend developer",
        company="Acme Technologies",
        url="https://hh.ru/vacancy/live-test-237",
    )

    class _RecordingClient:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.response = None

        def chat(self, messages, **params):
            self.response = self.wrapped.chat(messages, **params)
            return self.response

    recording_client = _RecordingClient(client)
    provider = AICoverLetterProvider(
        llm_client=recording_client,
        fallback_template="Рассматриваю вакансию {vacancy_title} в {company_name}.",
    )

    with caplog.at_level("INFO", logger="hhru_bot.ai.llm_client"):
        outcome = provider.render(vacancy)

    assert outcome.variant == "ai"
    assert outcome.text.strip()
    assert "{vacancy_title}" not in outcome.text
    assert "{company_name}" not in outcome.text
    assert vacancy.title in outcome.text or vacancy.company in outcome.text

    prompt = _build_prompt(vacancy, None)
    assert vacancy.title in str(prompt)
    assert vacancy.company in str(prompt)
    assert recording_client.response is not None
    route = (recording_client.response.provider_data or {}).get("hermes_route")
    assert route and route.get("provider") and route.get("model")
    assert any(
        "Hermes AI request completed via provider=" in record.message
        and route["provider"] in record.message
        and route["model"] in record.message
        for record in caplog.records
    )
    print(f"live cover letter: {outcome.text}")
    print(f"hermes route: {route}")

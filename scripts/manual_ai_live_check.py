"""Ручная проверка живого AI-транспорта (Hermes) вне pytest (issue #242).

Внутри pytest hermes-agent-axisrow отказывается трогать реальный
~/.hermes/auth.json — это намеренный seatbelt-guard пакета
(hermes_cli/auth.py:_auth_file_path()), срабатывающий, когда одновременно
установлена PYTEST_CURRENT_TEST (её всегда ставит сам pytest) и HERMES_HOME
резолвится в боевой auth-store. tests/test_ai_live_transport.py документирует
этот контракт и корректно скипается под pytest; получить реальный ответ можно
только запуском этого файла обычным python3, без pytest-раннера.

Запуск (с настроенным ~/.hermes и живой Hermes-сессией):
    python3 scripts/manual_ai_live_check.py

Ключи и endpoint hhru не читает и не передаёт — маршрут выбирает Hermes.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from hhru_bot.ai.letters import AICoverLetterProvider
    from hhru_bot.ai.llm_client import LLMClient
    from hhru_bot.config_sections.ai import AiConfig
    from hhru_bot.search import VacancyCard

    client = LLMClient(AiConfig())

    nr = client.chat(
        [{"role": "user", "content": "Ответь ровно одним словом: ping"}],
        timeout=60.0,
    )
    print(f"[ping] content={nr.content!r} finish_reason={nr.finish_reason}")
    print(f"[ping] hermes_route={(nr.provider_data or {}).get('hermes_route')}")

    vacancy = VacancyCard(
        vacancy_id="manual-live-check-242",
        title="Python backend developer",
        company="Acme Technologies",
        url="https://hh.ru/vacancy/manual-live-check-242",
    )
    provider = AICoverLetterProvider(
        llm_client=client,
        fallback_template="Рассматриваю вакансию {vacancy_title} в {company_name}.",
    )
    outcome = provider.render(vacancy)
    print(f"[letter] variant={outcome.variant}")
    print(f"[letter] text={outcome.text!r}")
    if outcome.variant != "ai":
        print(
            "[letter] WARNING: получен fallback-вариант, не AI — "
            "Hermes не ответил или вернул пустой текст, см. лог выше",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

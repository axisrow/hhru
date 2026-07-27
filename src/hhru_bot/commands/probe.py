"""Команда probe (#8): дамп формы отклика одной вакансии без отправки.

Top-level команда `hhru_bot probe ...` — регистрируется автоматически через
pkgutil.iter_modules в cli.register_commands (cli.py не трогается).

Безопасный диагностический режим: доходит до формы отклика целевой вакансии,
заполняет сопроводительное письмо и сдампит screenshot + HTML в logs/, после
чего останавчивается. submit не вызывается — ничего не отправляется.
По дампу сверяются непроверенные селекторы формы отклика (см. #10).
"""

from __future__ import annotations

import argparse
import sys

from ._common import _build_letter_provider, add_common_args, resolve_resumes


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "probe",
        help="Дамп формы отклика одной вакансии без отправки (диагностика селекторов)",
    )
    add_common_args(p)
    p.add_argument(
        "--vacancy-id",
        help="ID целевой вакансии (число из URL https://hh.ru/vacancy/<id>)",
    )
    p.add_argument(
        "--vacancy-url",
        help="URL целевой вакансии (альтернатива --vacancy-id)",
    )
    p.set_defaults(func=run)


def _vacancy_from_url(url: str):
    """Строит VacancyCard для probe из URL вакансии.

    vacancy_id извлекается канонически (через search._extract_vacancy_id: срез
    ?query, валидация isdigit) — не наивным split('/')[-1], иначе query-параметр
    попадает в vacancy_id и в имя файла дампа. Невалидный ID → ValueError.
    """
    from ..search import VacancyCard, _extract_vacancy_id

    vacancy_id = _extract_vacancy_id(url)
    if not vacancy_id:
        raise ValueError(
            f"Не удалось извлечь числовой ID вакансии из URL: {url} "
            "(ожидается https://hh.ru/vacancy/<id>)"
        )
    return VacancyCard(
        vacancy_id=vacancy_id,
        title=f"(probe target #{vacancy_id})",
        company="",
        url=url,
    )


def run(args: argparse.Namespace) -> None:
    from ..apply.probe import probe_vacancy
    from ..browser import launch_context
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)

    vacancy_url = _resolve_vacancy_url(args)
    try:
        vacancy = _vacancy_from_url(vacancy_url)
    except ValueError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    resumes = resolve_resumes(config, [args.resume] if args.resume else None)
    if not resumes:
        print("Ошибка: не выбрано резюме для probe (укажите --resume <id>).", file=sys.stderr)
        sys.exit(1)
    resume = resumes[0]
    cover_letter_template = config.cover_letter_for(resume)
    # #17 (follow-up #54): AI-письмо в probe-дампе. Провайдер строится по тому же
    # правилу, что и в apply (ai + ai_profile); None → статичный шаблон. Атомарность
    # probe не страдает: провайдер только генерирует текст письма, submit не кликается.
    letter_provider = _build_letter_provider(config, resume, cover_letter_template)

    print(f"=== probe для резюме: {resume.id} ===")
    print(f"Целевая вакансия: {vacancy.url}")

    with launch_context(config.storage_state_file, headless=args.headless) as context:
        page = context.new_page()
        result = probe_vacancy(
            page,
            vacancy,
            resume_id=resume.id,
            cover_letter_template=cover_letter_template,
            letter_provider=letter_provider,
        )

    if result.success:
        png = result.dump_paths.get("screenshot")
        html = result.dump_paths.get("html")
        print("[OK] Дамп формы отклика сохранён (отправка НЕ выполнена):")
        if png:
            print(f"  screenshot: {png}")
        if html:
            print(f"  html:       {html}")
    else:
        print(f"[FAIL] {result.reason}")


def _resolve_vacancy_url(args: argparse.Namespace) -> str:
    if args.vacancy_url:
        return args.vacancy_url
    if args.vacancy_id:
        return f"https://hh.ru/vacancy/{args.vacancy_id}"
    print(
        "Ошибка: укажите целевую вакансию через --vacancy-id <id> или --vacancy-url <url>.",
        file=sys.stderr,
    )
    sys.exit(1)

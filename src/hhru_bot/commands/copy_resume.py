"""Команда copy-resume: создать копию резюме на hh.ru (#116, WRITE-hh-ru).

На hh.ru действие называется «Дублировать»; в референсе s3rgeym — clone-resume.
Единый контракт подтверждения WRITE-hh-ru (§1 cli-spec): боевой режим требует
--force ИЛИ интерактивного prompt в TTY; неинтерактивно без --force — exit 1.
Дневные лимиты/кулдауны throttle к copy-resume не применяются (разовая
операция, согласовано в cli-spec §3.3), но аудит в actions обязателен.

Новый resume_id по умолчанию только печатается. Флаг --write-config добавляет
копию секции исходного резюме в config.yaml после подтверждённого успеха.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path

import yaml

from ._common import ApplyProgress, DurableMutationAttempt, run_supervised_command


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "copy-resume",
        help="Создать копию резюме на hh.ru (дублировать)",
        description=(
            "Создаёт копию резюме на hh.ru — то же, что «Дублировать» в меню резюме "
            "(в референсах — клонирование). WRITE-команда: боевой режим требует --force "
            "или интерактивного подтверждения; --dry-run ничего не отправляет."
        ),
    )
    p.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru (#319)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что будет сделано, без реальных действий",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Подтвердить боевой запуск без интерактивного вопроса",
    )
    p.add_argument(
        "--write-config", action="store_true", help="Добавить новое резюме в config.yaml"
    )
    p.add_argument("--slug", help="Slug нового резюме (по умолчанию <исходный>-copy)")
    p.set_defaults(func=run)


def confirm_write(
    force: bool,
    *,
    prompt: str,
    isatty_fn=None,
    input_fn=input,
) -> bool:
    """Единый контракт подтверждения WRITE-hh-ru (§1 cli-spec, общий с будущими
    reply-employers/clear-negotiations): --force → да; интерактивный TTY →
    спросить [y/N]; неинтерактивно без --force → отказ."""
    if force:
        return True
    isatty = (isatty_fn or sys.stdin.isatty)()
    if not isatty:
        return False
    answer = input_fn(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes", "д", "да")


def format_config_snippet(new_resume_id: str) -> str:
    """YAML-фрагмент для ручной вставки в config.yaml (автозаписи нет намеренно)."""
    return (
        "[INFO] Добавьте в config.yaml:\n"
        '         - id: "<придумайте-id>"\n'
        f'           resume_url: "https://hh.ru/resume/{new_resume_id}"'
    )


def _resume_mapping(resume, slug: str, new_resume_id: str) -> dict:
    values = asdict(resume) if is_dataclass(resume) else {}
    values["id"] = slug
    values["resume_url"] = f"https://hh.ru/resume/{new_resume_id}"
    return {key: value for key, value in values.items() if value is not None}


def reject_bare_source(resume) -> None:
    """Отказ регистрировать bare-резюме (#319) в конфиге.

    ``resolve_resume`` отдаёт bare-резюме для сырого resume_id, которого нет в
    конфиге: все секции None, ``search.text`` — пустая заглушка. Массовый
    apply-путь (``resolve_resumes``) намеренно не использует bare именно потому,
    что поиск без настроек не имеет смысла. Но ``--write-config`` — это и есть
    регистрация: записав такую заглушку, мы бы добавили в config.yaml резюме с
    пустым запросом, и следующий ``apply`` без ``--resume`` пошёл бы по нему
    откликаться по неограниченной выдаче. Запрос виден работодателю, откат
    невозможен — поэтому fail-closed отказ, а не запись с предупреждением.
    """
    if not resume.search.text.strip():
        print(
            f"[FAIL] У резюме '{resume.id}' нет настроек search в config.yaml — "
            "нечего копировать в новую секцию. Добавьте исходное резюме в конфиг "
            "и повторите."
        )
        sys.exit(1)


def _config_with_resume(text: str, resume, slug: str, new_resume_id: str) -> str:
    """Текст конфига с дописанной секцией нового резюме.

    Правка текстовая, а не через safe_load+safe_dump всего файла: полный
    round-trip уничтожил бы комментарии и форматирование пользователя (явное
    требование #520). Валидность результата гарантирует не эта функция, а
    проверка кандидата продакшн-загрузчиком в write_resume_config.
    """
    dumped = yaml.safe_dump(
        [_resume_mapping(resume, slug, new_resume_id)],
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    entry = "\n".join("  " + line for line in dumped.rstrip("\n").splitlines())
    lines = text.splitlines(keepends=True)
    resumes_line = next(
        (
            i
            for i, line in enumerate(lines)
            if line.startswith("resumes:") and not line.startswith(" ")
        ),
        None,
    )
    # Секции нет вовсе, либо она записана inline (`resumes: []`, `resumes:` со
    # значением на той же строке): дописать элемент внутрь такой формы нельзя,
    # поэтому заводим собственный блок в конце файла. Пустой inline-список при
    # этом остаётся в файле и сливается с новым блоком при загрузке — нет, YAML
    # запрещает дубль ключа, поэтому inline-форму сначала убираем.
    inline = resumes_line is not None and bool(lines[resumes_line].split(":", 1)[1].strip())
    if resumes_line is None or inline:
        if resumes_line is not None:
            del lines[resumes_line]
            text = "".join(lines)
        suffix = "\n" if text and not text.endswith("\n") else ""
        return text + suffix + "resumes:\n" + entry + "\n"
    insert_at = len(lines)
    for i in range(resumes_line + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith((" ", "\t", "#")):
            insert_at = i
            break
    # Файл без финального перевода строки: без него дописанный элемент склеился
    # бы с последней строкой в одну (`text: python  - id: ...`) и конфиг стал бы
    # нечитаемым — а произошло бы это уже ПОСЛЕ успешного копирования на hh.ru.
    if insert_at == len(lines) and lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines[insert_at:insert_at] = [entry + "\n"]
    return "".join(lines)


def write_resume_config(path: str | Path, resume, slug: str, new_resume_id: str) -> None:
    """Append a complete entry while leaving existing YAML/comments untouched.

    Кандидат сначала пишется во временный файл рядом с конфигом, проверяется
    продакшн-загрузчиком и только потом атомарно подменяет оригинал — тот же
    контракт, что у ``config_cmd._validated_replace``. Запись идёт после
    подтверждённого копирования на hh.ru: испорченный или обрезанный config.yaml
    тут уже не откатить, поэтому оригинал не трогается, пока кандидат не признан
    загружаемым.
    """
    from ..config import load_config

    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8")
    candidate_text = _config_with_resume(text, resume, slug, new_resume_id)

    fd, name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
    )
    candidate = Path(name)
    try:
        os.close(fd)
        candidate.write_text(candidate_text, encoding="utf-8")
        load_config(candidate)
        os.replace(candidate, config_path)
    finally:
        candidate.unlink(missing_ok=True)


def run(args: argparse.Namespace):
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..copy_resume import copy_resume_on_hh
    from ..history import History
    from ..responses import NotAuthenticated

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)
    write_config = bool(getattr(args, "write_config", False))
    slug = getattr(args, "slug", None) or f"{resume.id}-copy"
    if write_config:
        if not slug.strip() or any(char in slug for char in "\r\n"):
            print("[FAIL] Slug не может быть пустым или содержать перевод строки")
            sys.exit(1)
        existing = getattr(config, "resumes", [])
        if any(slug == item.id or slug == item.resume_id for item in existing):
            print(f"[FAIL] Резюме со slug '{slug}' уже есть в конфиге")
            sys.exit(1)
        # Проверки записи — ДО браузера: отказ после успешного копирования на
        # hh.ru оставил бы лишний дубль в аккаунте (та же логика, что у проверки
        # коллизии слагов выше — «отказ до записи», #520).
        reject_bare_source(resume)
    history = History(args.history)

    if args.dry_run:
        print(f"[DRY-RUN] Копирование резюме {resume.id} (resume_id {resume.resume_id})")
    else:
        if not confirm_write(args.force, prompt=f"Создать копию резюме '{resume.id}' на hh.ru?"):
            print(
                "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
                "Ничего не отправлено."
            )
            sys.exit(1)
        if history.has_unresolved_uncertain(resume.resume_id, "copy_resume"):
            print(
                f"[FAIL] {resume.id} — предыдущее копирование не подтверждено (uncertain). "
                "Проверьте статус резюме на hh.ru вручную перед повтором."
            )
            sys.exit(1)
        if history.count_today(resume.resume_id, "copy_resume") > 0:
            print(
                f"[INFO] Уже копировали {resume.id} сегодня — "
                "повторный запуск создаст ещё один дубль на hh.ru."
            )

    def _body(progress: ApplyProgress) -> bool:
        attempt = (
            None
            if args.dry_run
            else DurableMutationAttempt(history, progress, resume.resume_id, "copy_resume")
        )
        try:
            with launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context:
                page = context.new_page()
                result = copy_resume_on_hh(
                    page,
                    resume,
                    args.dry_run,
                    before_click=attempt.before_click if attempt is not None else None,
                )
        except NotAuthenticated as e:
            print(f"[FAIL] {resume.id} — Сессия недействительна: {e}")
            return True
        except BaseException as e:
            if attempt is not None:
                attempt.interrupt(e)
            raise

        # Fail-closed (#116): success без нового id или с id, совпавшим с исходным,
        # успехом не считается — копия не подтверждена.
        if result.success and not args.dry_run:
            if not result.new_resume_id or result.new_resume_id == resume.resume_id:
                result.success = False
                result.reason = "новый resume_id не подтверждён (совпал с исходным или пуст)"

        if attempt is not None:
            if result.success:
                result.reason = f"new_resume_id={result.new_resume_id}"
            attempt.finish(result)

        if args.dry_run:
            if not result.success:
                print(f"[FAIL] {resume.id} — {result.reason}")
                return True
            print("[INFO] Ничего не отправлено.")
        elif result.success:
            print(f"[OK] Резюме {resume.id} скопировано. Новый resume_id: {result.new_resume_id}")
            if write_config:
                try:
                    write_resume_config(args.config, resume, slug, result.new_resume_id)
                except (ConfigError, OSError, yaml.YAMLError) as exc:
                    # Копия на hh.ru уже создана и необратима, а конфиг остался
                    # прежним (подмена атомарная) — единственное, что осталось
                    # дать пользователю, это фрагмент для ручной вставки.
                    print(f"[FAIL] Копия создана, но config.yaml не обновлён: {exc}")
                    print(format_config_snippet(result.new_resume_id))
                    return True
                print(f"[OK] Резюме '{slug}' добавлено в config.yaml")
            else:
                print(format_config_snippet(result.new_resume_id))
        else:
            prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
            print(f"{prefix} {resume.id} — {result.reason}")
            return True
        return False

    return run_supervised_command(
        command=getattr(args, "command", "copy-resume"),
        history=history,
        requested_limit=None,
        body=_body,
    )

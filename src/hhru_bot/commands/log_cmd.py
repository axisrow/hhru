"""Команда log (#58): хвост logs/hhru_bot.log (READ, #21 §log).

``log`` — последние N строк лога (по умолчанию 50). ``log -f`` — follow
(``tail -f``): polling seek+read в цикле с коротким sleep, прерывается по Ctrl-C.
``log -n <count>`` — количество строк.

READ: читает только локальный файл, ничего не меняет ни на hh.ru, ни локально.
Чувствительные ID (resume_id, сессия) уже редачены уровнями логирования — команда
их не фильтрует (контракт #21). follow реализован polling-циклом, НЕ фоновым
демоном (без скрытых/фоновых режимов — см. CLAUDE.md). Прерывание по
KeyboardInterrupt -> exit 130 (как ``main``).

Путь к логу — relative-to-cwd через logging_setup.LOG_DIR (там же, куда пишет
setup_logging; см. комментарий в cli.py про отказ от PROJECT_ROOT). Для
тестируемости путь хранится в args.log_path, а не берётся из глобали в run().

READ-контракт и setup_logging: cli.main ПРОПУСКАЕТ setup_logging для команды log
(см. cli.main) — иначе FileHandler создал бы logs/hhru_bot.log на запись до run(),
что (а) нарушает READ-контракт «команда ничего не меняет локально», (б) делает
ветку «файл не найден» недостижимой (setup_logging создаёт пустой лог), (в) падает
с PermissionError в read-only-директории. Поэтому для log файл НЕ создаётся и
missing-file-ветка достижима (цикл ревью #61, находка Codex).

Имя файла ``log_cmd.py`` (не ``log.py``) — чтобы не конфликтовать со stdlib
``logging`` и ключевым именем в namespace. Команда регистрируется как ``log``;
авторегистрация pkgutil в cli.register_commands.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from pathlib import Path

# Дефолтный путь к логу — туда же, куда setup_logging пишет FileHandler.
# Вычисляется здесь (а не в run()) через set_defaults, чтобы run() принимал
# путь параметром и тестировался на tmp-файлах без monkeypatch глобалей.
from ..logging_setup import LOG_DIR

DEFAULT_LOG_PATH = LOG_DIR / "hhru_bot.log"
DEFAULT_LINES = 50
# follow: пауза между polling-итерациями. Короткая — чтобы вывод появлялся
# быстро, но не жечь CPU. Стандартный tail-loop.
FOLLOW_POLL_INTERVAL = 0.5


def _positive_int(value: str) -> int:
    """``argparse type=``: -n должно быть целым >= 1.

    Без этого ``-n -1`` роняет команду ValueError'ом внутри deque(maxlen=-1),
    а ``-n 0`` молча печатает пустой хвост (deque(maxlen=0)). Явная ошибка с
    понятным сообщением лучше некрасивого трейса (цикл ревью #61).
    """
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"требуется положительное целое, получено {value!r}")
    return n


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "log",
        help="Последние строки logs/hhru_bot.log (log -f — follow, как tail -f)",
    )
    p.add_argument(
        "-n",
        "--lines",
        type=_positive_int,
        default=DEFAULT_LINES,
        help=f"Количество строк (по умолчанию {DEFAULT_LINES})",
    )
    p.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Следить за логом (tail -f); прерывается по Ctrl-C",
    )
    p.set_defaults(func=run, log_path=str(DEFAULT_LOG_PATH))


def _tail_from(lines_view, n: int) -> list[str]:
    """Последние ``n`` строк из итерируемого ``lines_view`` (str с \\n).

    Если строк меньше ``n`` — возвращает все. Завершающая пустая строка
    (артефакт перевода строки ``...c\\n`` → ["a","b","c",""]) отбрасывается,
    как ``tail -n``. Общая для tail_lines (sync read) и follow (тот же файл).
    """
    tail = deque(lines_view, maxlen=n)
    out = [line.rstrip("\n") for line in tail]
    while out and out[-1] == "":
        out.pop()
    return out


def tail_lines(path: Path, n: int) -> list[str]:
    """Возвращает последние ``n`` строк файла ``path`` (как ``tail -n``).

    Чистая функция без побочных эффектов — тестируется без браузера.
    """
    with open(path, encoding="utf-8") as f:
        return _tail_from(f, n)


def follow(
    path: Path,
    emit,
    initial_lines: int = 0,
    sleep_interval: float = FOLLOW_POLL_INTERVAL,
    stop_after: int | None = None,
    before_read=None,
) -> None:
    """Слежение за ``path`` (tail -f): печать дописываемых строк в ``emit``.

    Один открытый дескриптор на весь цикл — начальный хвост и polling читаются
    из него, позиция непрерывна. Это устраняет TOCTOU-гонку «прочитали хвост →
    переоткрыли → seek к новому EOF», при которой строка, дописанная между
    первым read-до-EOF и вторым seek, терялась (не в снапшоте и позади
    follow-offset). Цикл ревью #61, находка Codex.

    Порядок итерации:
      1. Печатаем последние ``initial_lines`` строк (если > 0) — это хвост, как
         ``tail -n`` перед ``tail -f``. После него позиция указывает за эти
         строки (но НЕ обязательно на EOF снапшота — см. ниже).
      2. polling: ``before_read`` (хук для тестов), ``read()`` новые байты,
         ``emit``, короткий ``sleep``.

    Гонка всё ещё принципиально возможна только между шагом 1 и первым read
    шага 2: ``deque(maxlen=initial_lines)`` дочитывает файл целиком до EOF, так
    что на шаге 2 позиция уже на EOF — дописанные позже строки подхватятся
    первым read. Таким образом начальный хвост + polling на одном дескрипторе
    не теряют строки.

    Параметры для тестируемости (не меняют контракт в боевом режиме):
    - ``stop_after``: ограничение числа polling-итераций (None = бесконечно).
    - ``before_read(path, pos)``: хук перед ``read`` на каждой итерации; тесты
      через него дописывают строку (проверяя, что follow её подхватит).

    ``KeyboardInterrupt`` (Ctrl-C в sleep, read или хуке) переводится в
    ``sys.exit(130)`` — как ``main``. Ловится здесь, а не в ``run``, потому что
    прерывание tail-loop — ответственность самой функции следования.
    """
    with open(path, encoding="utf-8") as f:
        if initial_lines > 0:
            # _tail_from итерирует f до EOF → позиция после него = EOF, и дописанные
            # позже строки подхватятся первым read. Снапшот и follow на одном
            # дескрипторе без race (цикл ревью #61).
            for line in _tail_from(f, initial_lines):
                emit(line + "\n")
        else:
            # без начального хвоста — встаём в EOF, печатаем только НОВЫЕ строки.
            f.seek(0, 2)
        iteration = 0
        while True:
            try:
                if before_read is not None:
                    before_read(path, f.tell())
                chunk = f.read()
                if chunk:
                    emit(chunk)
                iteration += 1
                if stop_after is not None and iteration >= stop_after:
                    return
                time.sleep(sleep_interval)
            except KeyboardInterrupt:
                sys.exit(130)


def run(args: argparse.Namespace) -> None:
    path = Path(args.log_path)

    if not path.is_file():
        print(
            f"[FAIL] Файл лога не найден: {path}\n"
            "[INFO] Лог создаётся при первом запуске любой WRITE-команды "
            "(setup_logging открывает FileHandler). Запустите, например, "
            "`hhru_bot search --dry-run`, затем `hhru_bot log`.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.follow:
        # initial_lines + polling на одном дескрипторе — без race между хвостом
        # и follow. Ctrl-C -> exit 130 (внутри follow); вне follow ловит cli.main.
        follow(path, sys.stdout.write, initial_lines=args.lines)
        sys.stdout.flush()
    else:
        for line in tail_lines(path, args.lines):
            print(line)

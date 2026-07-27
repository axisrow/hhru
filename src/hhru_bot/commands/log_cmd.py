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

Имя файла ``log_cmd.py`` (не ``log.py``) — чтобы не конфликтовать со stdlib
``logging`` и ключевым именем в namespace. Команда регистрируется как ``log``;
авторегистрация pkgutil в cli.register_commands (cli.py не трогается).
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


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "log",
        help="Последние строки logs/hhru_bot.log (log -f — follow, как tail -f)",
    )
    p.add_argument(
        "-n",
        "--lines",
        type=int,
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


def tail_lines(path: Path, n: int) -> list[str]:
    """Возвращает последние ``n`` строк файла ``path`` (как ``tail -n``).

    Если строк меньше ``n`` — возвращает все. Файл оканчивается переводом строки
    → завершающая пустая строка не возвращается (поведение tail). Чистая функция
    без побочных эффектов — тестируется без браузера.
    """
    with open(path, encoding="utf-8") as f:
        # deque(maxlen=n) держит только хвост, не загружая весь файл в память.
        tail = deque(f, maxlen=n)
    lines = [line.rstrip("\n") for line in tail]
    # Файл "...c\n\n" даёт ["a", "b", "c", ""] — отбрасываем хвостовой пустой
    # элемент (артефакт завершающего перевода строки), как tail -n.
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def follow(
    path: Path,
    emit,
    sleep_interval: float = FOLLOW_POLL_INTERVAL,
    stop_after: int | None = None,
    before_wait=None,
) -> None:
    """Слежение за ``path`` (tail -f): печать дописываемых строк в ``emit``.

    Стандартный tail-loop: встаём в EOF (позиция конца файла), затем в цикле
    даём тесту/внешнему процессу дописать (``before_wait``), ``read()`` новые
    байты и короткий ``sleep``. Дописанные в лог строки тут же отдаются в
    ``emit(str)``.

    Параметры для тестируемости (не меняют контракт в боевом режиме):
    - ``stop_after``: ограничение числа polling-итераций (None = бесконечно);
      используется тестами для одного тика, чтобы не зависеть от времени.
    - ``before_wait(path, pos)``: хук перед ``read`` на каждой итерации; тесты
      через него дописывают строку (проверяя, что follow её подхватит).

    ``KeyboardInterrupt`` (Ctrl-C в sleep, read или хуке) переводится в
    ``sys.exit(130)`` — как ``main``. Ловится здесь, а не в ``run``, потому что
    прерывание tail-loop — ответственность самой функции следования.
    """
    with open(path, encoding="utf-8") as f:
        f.seek(0, 2)  # в конец файла (EOF) — печатаем только НОВЫЕ строки
        iteration = 0
        while True:
            try:
                if before_wait is not None:
                    before_wait(path, f.tell())
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
            "[INFO] Лог создаётся при первом запуске любой команды "
            "(setup_logging). Запустите, например, `hhru_bot search --dry-run`.",
            file=sys.stderr,
        )
        sys.exit(1)

    for line in tail_lines(path, args.lines):
        print(line)

    if args.follow:
        # Ctrl-C -> exit 130. Ловится внутри follow (tail-loop). Если Ctrl-C
        # прилетит вне follow (печать хвоста), его перехватит cli.main -> exit 130.
        follow(path, sys.stdout.write)
        sys.stdout.flush()

"""Typed exit statuses returned by commands to the CLI dispatcher.

Здесь же — общий инвариант exit-кодов (#1141): строка stdout, начинающаяся с
``[FAIL]`` (см. :class:`FailWatchStream`), означает неудачу команды и даёт
ненулевой exit без опроса каждого места печати в командах.
"""

from enum import Enum


class CommandExitCode(Enum):
    """A command-level status that must be handled by :mod:`hhru_bot.cli`."""

    PERSISTENCE_FAILED = 2
    # ``responses --alert-new`` uses a distinct non-error status so an
    # external scheduler can distinguish a newly discovered invitation from
    # an ordinary successful poll.
    NEW_INVITATIONS = 10
    # The command reached a confirmed unauthenticated page before any
    # irreversible action.  Keep this distinct from the generic fail-closed
    # status (1), persistence failures, and POSIX signal statuses so scheduled
    # callers can stop retrying and surface the required manual remediation.
    SESSION_EXPIRED = 78
    SIGHUP = 129
    SIGINT = 130
    SIGTERM = 143


# Префикс-вердикт из docs/cli-spec.md §2.2 («действие не удалось»). Инвариант
# смотрит начало строки с точностью до отступа: построчные отказы batch-команд
# печатаются с отступом (``  [FAIL] resume — причина``) и тоже означают неудачу
# прогона. Ячейки ASCII-таблиц начинаются с ``|`` и под инвариант не попадают.
FAIL_LINE_PREFIX = "[FAIL]"
# Решение «строка начинается с [FAIL]» принимается по голове строки: отступ +
# сам префикс умещаются заведомо. Длинные строки (без \n в пределах write-чанка)
# классифицируются один раз по этому окну и дальше не копятся — память O(1).
FAIL_JUDGE_WINDOW = 256


class FailWatchStream:
    """Write-through обёртка над stdout, замечающая строки-вердикты ``[FAIL]``.

    Зачем: команды печатают отказ префиксом ``[FAIL]`` (docs/cli-spec.md §2.2),
    но исторически возвращают ``None`` — диспетчер CLI не мог отличить отказ от
    успеха по exit-коду (#1141: list-resumes/census при невалидной сессии
    печатали ``[FAIL]`` и завершались с exit 0). Вместо опроса ~200 мест печати
    диспетчер оборачивает stdout команды в этот поток и после возврата читает
    :attr:`saw_fail_line`.

    Потоковая семантика сохраняется: каждый write() прозрачно уходит в исходный
    поток (прогресс-строки команд не буферизуются до конца прогона). Остальные
    атрибуты (encoding, isatty, buffer...) делегируются через ``__getattr__``.

    Память O(1): хранится только голова текущей незакрытой строки
    (<= ``FAIL_JUDGE_WINDOW``); строка длиннее окна судится один раз по голове,
    дальнейшее её содержимое пропускается до ``\\n``. Разбиение вердикта между
    write-чанками (``"[FA"`` + ``"IL] ..."``) обрабатывается переносом головы.
    """

    def __init__(self, stream):
        self._stream = stream
        self.saw_fail_line = False
        # Голова текущей строки: последняя незакрытая часть длиной до окна.
        self._head = ""
        # Строка длиннее окна — уже судена, до \n только пропускаем содержимое.
        self._head_decided = False

    def write(self, text: str) -> int:
        written = self._stream.write(text)
        self._scan(text)
        return written

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        # isatty/encoding/buffer/... — всё живёт в исходном потоке; подмена
        # должна быть неотличима для кода, проверяющего свойства stdout.
        return getattr(self._stream, name)

    def _scan(self, text: str) -> None:
        if self._head_decided:
            newline = text.find("\n")
            if newline == -1:
                return
            text = text[newline + 1 :]
            self._head_decided = False
        if self._head:
            text = self._head + text
        self._head = ""
        lines = text.split("\n")
        tail = lines.pop()  # хвост без \n ("" если чанк кончился переводом)
        for line in lines:
            self._judge(line)
        if not tail:
            return
        if len(tail) > FAIL_JUDGE_WINDOW:
            self._judge(tail)
            self._head_decided = True
        else:
            self._head = tail

    def _judge(self, line: str) -> None:
        if line[:FAIL_JUDGE_WINDOW].lstrip().startswith(FAIL_LINE_PREFIX):
            self.saw_fail_line = True

"""Сценарии этапа 2 поверх live-канала (#1161, эпик #588).

Сценарий — это ПОСЛЕДОВАТЕЛЬНОСТЬ примитивов исполнителя S2 (#1160: клик,
чтение DOM-состояния, ожидание условия), а не новая семантика результата.
Вся боевая семантика поднятия (кулдаун 4ч, дневной лимит, dry-run, статусы
history) живёт в команде и throttle.py — здесь только маппинг «шаг боевого
bump.py -> вызов примитива канала».

Модуль импортируется без playwright и без пакета ``hhru_bot.live``-транспорта
(#1159, в полёте): сервер импортируется лениво в :meth:`LiveChannel.start`, а
сценарная функция принимает channel-like объект (unit-тесты гоняют её с
FakeChannel на голом main). При расхождении имён действий S2 правится только
таблица ``ACTION_*`` ниже и тест-страж рядом с ней.
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlsplit

# --- Маппинг сценария на действия канала (единая точка контракта S2). ------
# Имена — как их объявляет расширение (content.js ACTION_ALLOWLIST,
# background.js RELAY_ACTIONS): get_page_state/check_element/click_element/
# wait_element. Расхождение ловит тест-страж рядом (test_action_names_match_s2_contract).
ACTION_GET_STATE = "get_page_state"
ACTION_CHECK = "check_element"
ACTION_CLICK = "click_element"
ACTION_WAIT = "wait_element"

# Состояния для wait: элемент появился/исчез в бюджете ожидания.
WAIT_STATE_VISIBLE = "visible"
WAIT_STATE_HIDDEN = "hidden"

# Транспортные коды, при которых команда УЖЕ ушла в браузер, но исход
# неизвестен (коды protocol.py #1159). Ошибка после форварда с таким кодом —
# честное «действие могло выполниться»; всё остальное (нет клиента, отказ
# policy-ядра расширения, элемент не найден) — клик НЕ произошёл.
FORWARD_UNKNOWN_CODES = frozenset(
    {"timeout", "client_disconnected", "bad_response", "unexpected_message"}
)

# Позитивный маркер успеха (#1161): после реального поднятия hh.ru убирает
# кнопку и показывает disabled-хинт «поднимать рано» (тот же элемент, что
# читает боевой bump.py ДО клика). Бюджет шире боевого BUMP_TIMEOUT_MS:
# ре-рендер списка идёт через сеть и гидрацию React.
MARKER_TIMEOUT_MS = 15_000
# Второй позитивный маркер: кнопка исчезла из карточки, а хинт не появился
# (например, исчерпан дневной лимит hh.ru). Короткий добивочный бюджет.
MARKER_GONE_TIMEOUT_MS = 3_000

# Скоуп кнопки/hint своей карточкой — боевой bump ищет кнопку ВНУТРИ карточки
# резюме (card.locator(...)), на мульти-резюме аккаунта плоский селектор кнопки
# матчил бы чужую карточку. :has() — нативный CSS Chrome; если матчер S2 его
# не примет, клик честно откажется (PrimitiveError, не наш клик), не промахнётся.
RESUME_CARD_SCOPE_TEMPLATE = "[data-qa='resume']:has(a[data-qa='resume-card-link-{resume_id}'])"


class ChannelError(Exception):
    """Канал недоступен (нет клиента, сервер не поднялся) — сценарий не начат."""


def bump_via_live(channel, resume, dry_run: bool):
    """Поднятие резюме через живую вкладку (#1161): зеркало боевого bump_resume.

    Каждый шаг боевого пути (bump.py) превращён в примитив канала; порядок и
    вердикты совпадают. Транспортные отказы до клика — обычный failed
    (acted=False); после клика — acted=True и честный uncertain (fail-closed
    #176: действие могло дойти до hh.ru). Возвращает ``bump.BumpResult`` —
    та же структура, что у боевого пути, команде неважно, каким транспортом
    получен исход.
    """
    from ..browser import LOGIN_FORM
    from ..bump import (
        BUMP_HINT_TIMEOUT_MS,
        BUMP_TIMEOUT_MS,
        RESUMES_LIST_URL,
        BumpResult,
    )
    from ..config import is_resume_url_placeholder
    from ..selector_groups.resume_list import RESUME_LIST_CARD_LINK_PREFIX
    from ..selector_groups.resume_page import (
        RESUME_BUMP_BUTTON,
        RESUME_BUMP_DISABLED_HINT,
        RESUME_CARD_LINK_TEMPLATE,
    )

    if is_resume_url_placeholder(resume.resume_url):
        return BumpResult(
            resume.id,
            False,
            "В конфиге указан плейсхолдер resume_url; укажите реальный URL "
            "(получить можно через list-resumes)",
        )

    try:
        url = str(channel.get_state().get("url", ""))
        parts = urlsplit(url)
        if parts.path != "/applicant/resumes" or not parts.netloc.endswith("hh.ru"):
            return BumpResult(
                resume.id,
                False,
                f"живая вкладка не на списке резюме ({url or 'URL не прочитан'}); "
                f"откройте {RESUMES_LIST_URL} в вкладке с расширением hhru-live",
            )
        login = channel.check(LOGIN_FORM)
        if login.get("found") or login.get("visible"):
            return BumpResult(
                resume.id,
                False,
                "Сессия недействительна: страница содержит форму входа. Выполните login.",
            )

        card_link = RESUME_CARD_LINK_TEMPLATE.format(resume_id=resume.resume_id)
        scope = RESUME_CARD_SCOPE_TEMPLATE.format(resume_id=resume.resume_id)
        in_scope_button = f"{scope} {RESUME_BUMP_BUTTON}"
        in_scope_hint = f"{scope} {RESUME_BUMP_DISABLED_HINT}"

        if not channel.wait(RESUME_LIST_CARD_LINK_PREFIX, WAIT_STATE_VISIBLE, BUMP_TIMEOUT_MS):
            return BumpResult(
                resume.id,
                False,
                f"список резюме не отрисовался за {BUMP_TIMEOUT_MS // 1000} с "
                "(гидрация/медленная загрузка) — наличие резюме не подтверждено; "
                "повторите запуск позже",
            )
        if not channel.wait(card_link, WAIT_STATE_VISIBLE, BUMP_TIMEOUT_MS):
            return BumpResult(
                resume.id,
                False,
                "резюме не найдено в списке /applicant/resumes (удалено или недоступно)",
            )
        if channel.wait(in_scope_hint, WAIT_STATE_VISIBLE, BUMP_HINT_TIMEOUT_MS):
            return BumpResult(resume.id, False, "hh.ru сообщает, что поднимать ещё рано")
        if not channel.wait(in_scope_button, WAIT_STATE_VISIBLE, BUMP_TIMEOUT_MS):
            return BumpResult(resume.id, False, "кнопка поднятия резюме не найдена на странице")
    except (ChannelError, PrimitiveError) as exc:
        # Чтения до клика мутировать не могут — обычный отказ, повтор возможен.
        return BumpResult(resume.id, False, f"канал/исполнитель: {exc}")

    if dry_run:
        return BumpResult(resume.id, True, "dry-run")

    try:
        channel.click(in_scope_button)
    except PrimitiveError as exc:
        if exc.forwarded:
            # Команда ушла в браузер, ответа/исхода нет — как PlaywrightError
            # в момент клика у боевого пути: acted+uncertain (#176).
            return BumpResult(
                resume.id,
                False,
                f"клик поднятия отправлен, исход неопределён ({exc})",
                acted=True,
                uncertain=True,
            )
        return BumpResult(resume.id, False, f"клик не выполнен: {exc}")

    # Позитивный маркер успеха (#1161): кулдаун-хинт появился (hh.ru снял
    # кнопку) или хотя бы кнопка исчезла из карточки. Ни один не подтверждён —
    # выдуманный успех запрещён: acted+uncertain.
    try:
        if channel.wait(in_scope_hint, WAIT_STATE_VISIBLE, MARKER_TIMEOUT_MS):
            return BumpResult(resume.id, True, "success", acted=True)
        if not channel.wait(in_scope_button, WAIT_STATE_VISIBLE, MARKER_GONE_TIMEOUT_MS):
            return BumpResult(resume.id, True, "success: кнопка поднятия исчезла", acted=True)
        return BumpResult(
            resume.id,
            False,
            "клик выполнен, маркеры успеха не подтвердились за бюджет — исход неопределён",
            acted=True,
            uncertain=True,
        )
    except (ChannelError, PrimitiveError) as exc:
        return BumpResult(
            resume.id,
            False,
            f"клик выполнен, подтверждение не прочитано ({exc})",
            acted=True,
            uncertain=True,
        )


class PrimitiveError(Exception):
    """Действие вернуло status=error (код транспорта или самого расширения)."""

    def __init__(self, code: str, detail: str, forwarded: bool) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        # True — команда была переслана в браузер, исход неизвестен.
        self.forwarded = forwarded


class _Collector:
    """TextIO-приёмник stdout сервера: строка-ответ -> ожидание в call()."""

    def __init__(self, events: dict[str | int, threading.Event]) -> None:
        self._buf = ""
        self._lock = threading.Lock()
        self._events = events
        self.responses: dict[str | int, dict] = {}

    def write(self, text: str) -> int:
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._handle(line)
        return len(text)

    def flush(self) -> None:
        pass

    def _handle(self, line: str) -> None:
        if not line or line.startswith("[INFO]"):
            if line:
                print(line)
            return
        try:
            obj = json.loads(line)
        except ValueError:
            print(line)
            return
        if isinstance(obj, dict) and "id" in obj:
            self.responses[obj["id"]] = obj
            event = self._events.get(obj["id"])
            if event is not None:
                event.set()


class LiveChannel:
    """Клиент сценария поверх сервера #1159: команда -> ответ, по одной.

    Сервер живёт в потоке процесса (foreground-инвариант #1159 не нарушен:
    поток гасится закрытием stdin-канала и вместе с процессом, фонового
    демона нет). Команды подаются в select-цикл сервера через pipe — код
    сервера не дублируется и не правится (файл — владение S1).
    """

    def __init__(self, port: int = 0, client_timeout: float = 120.0) -> None:
        self.port = port
        self.client_timeout = client_timeout
        self._server = None  # LiveServeServer, создаётся в start()
        self._sink_fd: int | None = None
        self._source_fd: int | None = None
        self._collector: _Collector | None = None
        self._events: dict[str | int, threading.Event] = {}
        self._thread: threading.Thread | None = None
        self._counter = 0

    def start(self) -> str:
        """Поднять сервер; вернуть ws://URL для расширения."""
        from .server import LiveServeServer

        server = LiveServeServer(port=self.port)
        try:
            server.bind()
        except OSError as exc:
            raise ChannelError(f"не удалось занять порт {self.port}: {exc}") from exc
        self._server = server
        self._source_fd, self._sink_fd = os.pipe()
        self._collector = _Collector(self._events)
        self._thread = threading.Thread(
            target=server.serve, args=(self._source_fd, self._collector), daemon=True
        )
        self._thread.start()
        return server.url

    def wait_client(self) -> None:
        """Дождаться подключения расширения; ChannelError по таймауту."""
        assert self._server is not None and self._thread is not None
        deadline = time.monotonic() + self.client_timeout
        while time.monotonic() < deadline:
            if self._server.connections_seen:
                return
            if not self._thread.is_alive():
                raise ChannelError("сервер канала остановился до подключения клиента")
            time.sleep(0.2)
        raise ChannelError(
            f"расширение hhru-live не подключилось за {self.client_timeout:.0f} с "
            "(нужна вкладка hh.ru в Chrome с включённым расширением)"
        )

    # -- примитивы (маппинг на действия #1160) --------------------------------

    def get_state(self) -> dict:
        return self._call(ACTION_GET_STATE, {})

    def check(self, selector: str) -> dict:
        return self._call(ACTION_CHECK, {"selector": selector})

    def click(self, selector: str) -> dict:
        return self._call(ACTION_CLICK, {"selector": selector})

    def wait(self, selector: str, state: str, timeout_ms: int) -> bool:
        """Дождаться состояния селектора; False — бюджет истёк без события."""
        result = self._call(
            ACTION_WAIT, {"selector": selector, "state": state, "timeoutMs": timeout_ms}
        )
        # Ключ ответа — часть контракта S2 (executor.js waitElement отвечает
        # result.wait.met): при расхождении правится здесь.
        return bool((result.get("wait") or {}).get("met", False))

    def close(self) -> None:
        """EOF в stdin сервера — цикл завершается (foreground-семантика #1159)."""
        if self._sink_fd is not None:
            os.close(self._sink_fd)
            self._sink_fd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._source_fd is not None:
            os.close(self._source_fd)
            self._source_fd = None

    # -- транспорт ------------------------------------------------------------

    def _call(self, action: str, payload: dict) -> dict:
        from .protocol import PROTOCOL_VERSION, serialize

        assert self._server is not None and self._sink_fd is not None
        assert self._collector is not None
        self._counter += 1
        command_id = f"c{self._counter}"
        done = threading.Event()
        self._events[command_id] = done
        envelope = serialize(
            {"v": PROTOCOL_VERSION, "id": command_id, "action": action, "payload": payload}
        )
        os.write(self._sink_fd, (envelope + "\n").encode("utf-8"))
        # Сервер сам отвечает TIMEOUT-ошибкой по своему response_timeout;
        # клиентский бюджет — с запасом на планировщик, не вместо него.
        if not done.wait(self._server.response_timeout + 10):
            self._events.pop(command_id, None)
            self._collector.responses.pop(command_id, None)
            raise PrimitiveError("timeout", "ответ канала не получен", forwarded=True)
        self._events.pop(command_id, None)
        response = self._collector.responses.pop(command_id)
        if response.get("status") != "ok":
            result = response.get("result") or {}
            code = str(result.get("code", "unknown"))
            detail = str(result.get("detail", ""))
            raise PrimitiveError(code, detail, forwarded=code in FORWARD_UNKNOWN_CODES)
        return response.get("result") or {}

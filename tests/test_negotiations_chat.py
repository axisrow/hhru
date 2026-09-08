import logging
from typing import cast

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from hhru_bot.negotiations_chat import (
    CHAT_MESSAGE_META_JS,
    ChatMessage,
    NoQuickReply,
    author_from_ancestors,
    click_quick_reply,
    extract_external_test_link,
    find_quick_replies,
    is_robot_questionnaire,
    matches_marker,
    needs_follow_up,
    needs_reply,
    read_chat,
    wait_reply_confirmation,
)
from hhru_bot.selector_groups.negotiations import (
    QUICK_REPLY_BUTTON,
    QUICK_REPLY_BUTTONS_WRAPPER,
)

pytestmark = pytest.mark.integration


def test_needs_reply_when_last_message_is_from_employer():
    decision = needs_reply(ChatMessage("employer", "message-1"))
    assert decision.should_reply is True
    assert decision.reason == "last_message_from_employer"


def test_needs_reply_skips_our_last_message():
    decision = needs_reply(ChatMessage("me", "message-1"))
    assert decision.should_reply is False
    assert decision.reason == "last_message_from_us"


def test_needs_reply_is_fail_closed_for_empty_chat():
    assert needs_reply(None).reason == "empty_chat"


def test_needs_reply_is_fail_closed_for_unknown_author_or_marker():
    assert needs_reply(ChatMessage(None, "message-1")).should_reply is False
    assert needs_reply(ChatMessage("employer", None)).reason == "inbound_marker_unknown"


# --- needs_follow_up (#710): mirror image of needs_reply --------------------


def test_needs_follow_up_when_last_message_is_ours():
    decision = needs_follow_up(ChatMessage("me", "message-1"))
    assert decision.should_reply is True
    assert decision.reason == "last_message_from_us"


def test_needs_follow_up_skips_when_employer_already_answered():
    decision = needs_follow_up(ChatMessage("employer", "message-1"))
    assert decision.should_reply is False
    assert decision.reason == "last_message_from_employer"


def test_needs_follow_up_is_fail_closed_for_empty_chat():
    assert needs_follow_up(None).reason == "empty_chat"


def test_needs_follow_up_is_fail_closed_for_unknown_author_or_marker():
    assert needs_follow_up(ChatMessage(None, "message-1")).should_reply is False
    assert needs_follow_up(ChatMessage("me", None)).reason == "inbound_marker_unknown"


def test_robot_questionnaire_detects_two_employer_questions():
    messages = [
        ChatMessage("employer", "1", "Какой у вас опыт?"),
        ChatMessage("employer", "2", "Когда готовы приступить?"),
    ]
    assert is_robot_questionnaire(messages)


def test_robot_questionnaire_detects_explicit_bot_author():
    assert is_robot_questionnaire([ChatMessage("employer", "1", "", "Robot HH")])


def test_robot_questionnaire_does_not_count_our_questions():
    assert not is_robot_questionnaire(
        [ChatMessage("employer", "1", "Какой у вас опыт?"), ChatMessage("me", "2", "Почему?")]
    )


def test_robot_author_match_is_not_a_substring():
    assert not is_robot_questionnaire([ChatMessage("employer", "1", "", "Работодатель")])
    assert is_robot_questionnaire([ChatMessage("employer", "1", "", "Автобот")])


def test_robot_questionnaire_counts_adjacent_questions_in_one_message():
    assert is_robot_questionnaire(
        [ChatMessage("employer", "1", "Какой у вас опыт? Когда готовы приступить?")]
    )


def test_repeated_question_punctuation_counts_as_one_question():
    assert not is_robot_questionnaire(
        [ChatMessage("employer", "1", "Вы ещё рассматриваете вакансию??")]
    )
    assert not is_robot_questionnaire([ChatMessage("employer", "1", "Вы готовы?!?!")])
    assert not is_robot_questionnaire([ChatMessage("employer", "1", "Спасибо! Ждём вас!")])


def test_question_sentence_detection_handles_mixed_punctuation():
    assert not is_robot_questionnaire([ChatMessage("employer", "1", "Вы готовы?!")])
    assert is_robot_questionnaire([ChatMessage("employer", "1", "Готовы? Успеете?")])
    assert is_robot_questionnaire(
        [ChatMessage("employer", "1", "«Какой у вас опыт?» «Когда готовы?»")]
    )
    assert not is_robot_questionnaire(
        [ChatMessage("employer", "1", "Вы видели вопрос «Когда?»🙂 в анкете?")]
    )


# --- эвристика скорости ответа: смежная пара me→employer быстрее 15 минут ---
#
# Живые факты 2026-09-08: Сбер «ГигаРекрутер» ответил в ту же минуту
# (12:29 → 12:29), Яндекс Крауд — через 14 минут; человек-образный ответ
# Найди.Про — 2ч14м. Время пузыря — только HH:MM (суточное окно).


def _pair(ours_at: str, theirs_at: str) -> list[ChatMessage]:
    return [
        ChatMessage("me", "1", "Отклик на вакансию", time_text=ours_at),
        ChatMessage("employer", "2", "Здравствуйте! Пройдите интервью", time_text=theirs_at),
    ]


def test_same_minute_reply_is_robot():
    assert is_robot_questionnaire(_pair("12:29", "12:29"))


def test_reply_within_threshold_is_robot():
    assert is_robot_questionnaire(_pair("12:22", "12:36"))  # Яндекс Крауд, 14 мин


def test_slow_reply_is_not_robot_by_speed():
    """2ч14м (Найди.Про, Алиса) — скорость молчит; этого детекта хватает
    только вердикту пользователя или эвристике вопросов."""
    assert not is_robot_questionnaire(_pair("12:24", "14:38"))


def test_boundary_reply_exactly_at_threshold_is_not_robot():
    # Ровно 15 минут — порог строгий (<), не нестрогий (<=): живой рекрутер
    # на границе не должен попадать в robot-очередь.
    assert not is_robot_questionnaire(_pair("10:00", "10:15"))


def test_midnight_crossing_pair_is_skipped_not_robot():
    # Пузырь несёт только HH:MM: 23:58 → 00:01 формально отрицательная дельта
    # (переход через полночь), пара молча пропускается (fail-open).
    assert not is_robot_questionnaire(_pair("23:58", "00:01"))


def test_unparseable_time_pair_is_skipped_not_robot():
    assert not is_robot_questionnaire(_pair("сегодня", "12:30"))
    assert not is_robot_questionnaire(_pair("12:00", ""))


def test_speed_signal_requires_our_message_first():
    # Ответ работодателя без предшествующего нашего сообщения (например,
    # входящее в пустом чате) — пары нет, скорость не считается.
    assert not is_robot_questionnaire(
        [ChatMessage("employer", "1", "Здравствуйте!", time_text="12:30")]
    )


def test_read_chat_logs_and_fails_closed_for_unmapped_topic(caplog):
    # An unmapped topic returns before ``page`` is touched, so a typed stand-in
    # is enough — no real Playwright Page is needed for this branch.
    fake_page = cast(Page, object())
    with caplog.at_level(logging.WARNING, logger="hhru_bot.negotiations_chat"):
        result = read_chat(fake_page, topic="unknown-topic", topic_to_chat_id={})

    assert result is None
    assert any("unknown-topic" in record.message for record in caplog.records)


def test_extracts_external_link_from_message():
    assert extract_external_test_link("Пройдите тест: https://yay-tech.ru/test") == (
        "https://yay-tech.ru/test"
    )


def test_message_without_link_returns_none():
    assert extract_external_test_link("Добрый день, готовы обсудить вакансию") is None


def test_returns_first_external_link_when_message_has_several():
    assert extract_external_test_link("Тест: https://example.com/a и https://other.test/b") == (
        "https://example.com/a"
    )


def test_company_link_without_test_context_is_ignored():
    assert extract_external_test_link("Сайт компании: https://example.com/careers") is None


def test_external_link_with_nearby_test_context_is_detected():
    assert extract_external_test_link("Пройдите тест по ссылке: https://example.com/test") == (
        "https://example.com/test"
    )


def test_external_link_with_following_test_context_is_detected():
    assert (
        extract_external_test_link(
            "Перейдите по ссылке https://example.com/quiz и выполните тестовое задание"
        )
        == "https://example.com/quiz"
    )


def test_unrelated_word_does_not_count_as_test_context():
    assert (
        extract_external_test_link("В заданное время подключитесь: https://meet.example/abc")
        is None
    )


def test_known_test_platform_is_detected_without_context():
    assert extract_external_test_link("https://candidate.typeform.com/to/abc") == (
        "https://candidate.typeform.com/to/abc"
    )


def test_skips_unrelated_external_link_before_test_link():
    assert (
        extract_external_test_link(
            "Сайт компании https://example.com и тестовое задание https://other.example/task"
        )
        == "https://other.example/task"
    )


def test_hh_links_are_not_external():
    assert extract_external_test_link("https://hh.ru/vacancy/1 https://cdn.hhcdn.ru/file") is None


def test_trailing_sentence_punctuation_is_not_part_of_url():
    assert extract_external_test_link("Тест: https://example.com/test.") == (
        "https://example.com/test"
    )


# --- wait_reply_confirmation (Codex review, PR #198) ------------------------
#
# send_reply_current() only clicks the send button; it never confirms the
# server actually accepted the message. wait_reply_confirmation() is the
# positive-signal poll that closes that gap: it waits for the last chat
# message to become "ours" (same CHAT_MESSAGE_MY_MARKER used by
# read_last_message), mirroring apply/success.wait_success_confirmation's
# positive-only, timeout-is-false-negative contract.


class _FakeMessage:
    def __init__(self, is_own: bool):
        self._is_own = is_own

    def evaluate(self, script, markers):
        # #1044: резолвер один и тот же (CHAT_MESSAGE_META_JS +
        # author_markers) — двойник воспроизводит его вердикт по is_own,
        # не исполняя JS. Третий элемент (time_text) пуст: время не прочитано.
        assert script == CHAT_MESSAGE_META_JS
        assert set(markers) == {"my", "other"}
        return ["me" if self._is_own else "employer", "", ""]


class _FakeMessages:
    """Imitates a Playwright Locator over CHAT_MESSAGE_TEXT: count()/nth()."""

    def __init__(self, authors: list[bool]):
        self._authors = authors

    def count(self) -> int:
        return len(self._authors)

    def nth(self, index: int) -> _FakeMessage:
        return _FakeMessage(self._authors[index])


class _FakeChatPage:
    """Minimal Page stand-in: only locator()/wait_for_timeout()/url are used."""

    def __init__(self, authors: list[bool] | None = None, *, late_after: int | None = None):
        self._authors = authors if authors is not None else []
        self._late_after = late_after
        self._polls = 0
        self.url = "https://hh.ru/chat/1"

    def wait_for_timeout(self, _ms: float) -> None:
        return None

    def locator(self, _selector: str) -> _FakeMessages:
        self._polls += 1
        if self._late_after is not None and self._polls <= self._late_after:
            return _FakeMessages([False])
        return _FakeMessages(self._authors)


def test_confirms_when_last_message_becomes_ours():
    page = _FakeChatPage(authors=[True])
    assert wait_reply_confirmation(cast(Page, page)) is True


def test_not_confirmed_when_last_message_is_still_employers():
    """The click may have failed silently (server rejection, network error);
    the last message staying the employer's is not a delivery signal."""
    page = _FakeChatPage(authors=[False])
    assert wait_reply_confirmation(cast(Page, page), timeout_ms=0) is False


def test_not_confirmed_on_empty_chat():
    page = _FakeChatPage(authors=[])
    assert wait_reply_confirmation(cast(Page, page), timeout_ms=0) is False


def test_confirms_via_late_async_render():
    """hh.ru may render the sent message asynchronously; the poll loop must
    catch it within the timeout rather than judging only the first read."""
    page = _FakeChatPage(authors=[True], late_after=1)
    assert wait_reply_confirmation(cast(Page, page), timeout_ms=2000) is True


def test_confirmation_timeout_logs_page_url(caplog):
    page = _FakeChatPage(authors=[False])
    page.url = "https://hh.ru/chat/42"
    with caplog.at_level(logging.WARNING, logger="hhru_bot.negotiations_chat"):
        result = wait_reply_confirmation(cast(Page, page), timeout_ms=0)
    assert result is False
    assert any("https://hh.ru/chat/42" in record.message for record in caplog.records)


# --- min_count (#710, cycle-review round 2) ---------------------------------
#
# For --follow-up, "last message is ours" is already TRUE before the send
# click (needs_follow_up's precondition) -- unlike a plain reply, where the
# pre-click last message is the employer's. Without min_count, a silently
# failed follow-up click (network drop, no exception) would still pass this
# check on the very first poll and be journaled as a false 'success'.


def test_min_count_rejects_unchanged_message_list():
    """The message that is 'ours' pre-existed the click -- not new evidence."""
    page = _FakeChatPage(authors=[True])  # same single "our" message as before
    assert wait_reply_confirmation(cast(Page, page), timeout_ms=0, min_count=2) is False


def test_min_count_confirms_once_a_new_message_renders():
    page = _FakeChatPage(authors=[True, True])  # a second "our" message appeared
    assert wait_reply_confirmation(cast(Page, page), timeout_ms=0, min_count=2) is True


def test_min_count_default_preserves_plain_reply_behaviour():
    """min_count=1 (default) is exactly the pre-existing contract."""
    page = _FakeChatPage(authors=[True])
    assert wait_reply_confirmation(cast(Page, page)) is True


# --- #1044: дрейф DOM-маркеров автора на CSS-модули -------------------------
# Живой census 2026-09-08 (колонки classes/ancestors из `census --json`,
# чаты аккаунта):
# собственное сообщение:
#   chat-bubble--TFjICp8IMFIhojGy chat-bubble_with-right-tail--J0NYWVDENy0KLcgZ
#   chat-bubble_outgoing--C1wSnUG6DlswSx6I | chat-bubble-container--xlH6p5aV4o4u_38b
#   | message--WgE5qGIVMRAXYnLL message_my--PpRVpLiDQMcfwKlp | ...
# работодатель-человек: chat-bubble_incoming--CgAKL4FOU0shOYzo
# работодатель-бот:     chat-bubble_bot--ATOUBrnmcY8nZkrY
# системное (participant-action): ни одного маркера.


def test_marker_matches_css_module_hash_suffix():
    assert matches_marker("message_my--PpRVpLiDQMcfwKlp", "message_my")
    assert matches_marker("message_my", "message_my")  # старая разметка
    assert not matches_marker("message_mine--xxx", "message_my")  # граница --
    assert not matches_marker("chat-bubble_outgoing--C1wSnUG6DlswSx6I", "message_my")


def test_own_message_recognized_through_outgoing_bubble_chain():
    chain = [
        "chat-bubble-content--qOhTaYyg5ygPNstt",
        "chat-bubble--TFjICp8IMFIhojGy chat-bubble_with-right-tail--J0NYWVDENy0KLcgZ "
        "chat-bubble_outgoing--C1wSnUG6DlswSx6I",
        "chat-bubble-container--xlH6p5aV4o4u_38b",
        "message--WgE5qGIVMRAXYnLL message_my--PpRVpLiDQMcfwKlp",
    ]
    assert author_from_ancestors(chain) == "me"


def test_incoming_human_message_is_employer():
    chain = [
        "chat-bubble-content--qOhTaYyg5ygPNstt",
        "chat-bubble--TFjICp8IMFIhojGy chat-bubble_with-left-tail--__mmuGUjNcDqugJS "
        "chat-bubble_incoming--CgAKL4FOU0shOYzo",
    ]
    assert author_from_ancestors(chain) == "employer"


def test_bot_message_is_employer():
    chain = [
        "chat-bubble-content--qOhTaYyg5ygPNstt",
        "chat-bubble--TFjICp8IMFIhojGy chat-bubble_with-left-tail--__mmuGUjNcDqugJS "
        "chat-bubble_bot--ATOUBrnmcY8nZkrY",
    ]
    assert author_from_ancestors(chain) == "employer"


def test_system_participant_message_has_no_author():
    chain = [
        "content--qbFEo4QXKAQupjDJ",
        "magritte-scroll-container___QzFOQ_1-0-33 scroll-wrapper--BANmTN9Sz6EwRGzp",
    ]
    assert author_from_ancestors(chain) is None


def test_legacy_exact_markers_still_classify():
    assert author_from_ancestors(["message_my"]) == "me"
    assert author_from_ancestors(["message_other"]) == "employer"


# --- robot-reply: кнопки быстрых ответов робота-анкеты ----------------------
# Живой census 2026-09-08: <button> magritte-button_mode-secondary--<hash> в
# div.buttons-wrapper--<hash> под вопросом бота; data-qa нет; гидратация ~6с.


class _FakeQuickButtonHandle:
    def __init__(self, text: str, page: "_FakeQuickReplyPage"):
        self._text = text
        self._page = page

    def inner_text(self) -> str:
        return self._text

    def wait_for(self, *, state=None, timeout=None):
        # гидратация: кнопка видима только если была в исходном наборе
        if self._text not in self._page.buttons:
            raise PlaywrightError("timeout")

    def click(self):
        self._page.clicked.append(self._text)


class _FakeQuickButtonList:
    def __init__(self, page: "_FakeQuickReplyPage"):
        self._page = page
        self.first = _FakeQuickButtonHandle(
            self._page.buttons[0] if self._page.buttons else "", self._page
        )

    def count(self) -> int:
        return len(self._page.buttons)

    def nth(self, index: int) -> _FakeQuickButtonHandle:
        return _FakeQuickButtonHandle(self._page.buttons[index], self._page)


class _FakeQuickRoleLocator:
    """Двойник wrapper.get_by_role('button', name=label, exact=True)."""

    def __init__(self, page: "_FakeQuickReplyPage", label: str):
        self._page = page
        self._label = label

    def count(self) -> int:
        return sum(1 for text in self._page.buttons if text == self._label)

    @property
    def first(self) -> _FakeQuickButtonHandle:
        return _FakeQuickButtonHandle(self._label, self._page)


class _FakeQuickWrapperLocator:
    def __init__(self, page: "_FakeQuickReplyPage"):
        self._page = page

    def get_by_role(self, _role, *, name=None, exact=None):
        assert exact is True
        return _FakeQuickRoleLocator(self._page, name)


class _FakeQuickReplyPage:
    """Page-двойник для find_quick_replies/click_quick_reply: только локаторы
    QUICK_REPLY_* (контроль: прочие селекторы сюда не приходят)."""

    def __init__(self, buttons: list[str]):
        self.buttons = buttons
        self.clicked: list[str] = []

    def locator(self, selector: str):
        assert selector in (QUICK_REPLY_BUTTON, QUICK_REPLY_BUTTONS_WRAPPER)
        if selector == QUICK_REPLY_BUTTON:
            return _FakeQuickButtonList(self)
        return _FakeQuickWrapperLocator(self)


def test_find_quick_replies_returns_button_texts():
    page = _FakeQuickReplyPage(["Да", "Нет"])
    assert find_quick_replies(cast(Page, page), timeout_ms=10) == ["Да", "Нет"]


def test_find_quick_replies_timeout_returns_empty_list():
    """Кнопок нет (уже отвечено / гидратация не уложилась) — [], не исключение:
    решение об отказе принимает вызывающий."""
    page = _FakeQuickReplyPage([])
    assert find_quick_replies(cast(Page, page), timeout_ms=10) == []


def test_click_quick_reply_clicks_the_only_exact_match():
    page = _FakeQuickReplyPage(["Да", "Нет"])
    click_quick_reply(cast(Page, page), "Нет")
    assert page.clicked == ["Нет"]


def test_click_quick_reply_zero_matches_fails_closed_before_click():
    page = _FakeQuickReplyPage(["Да", "Нет"])
    with pytest.raises(NoQuickReply) as exc:
        click_quick_reply(cast(Page, page), "Не знаю")
    assert page.clicked == []
    assert "Да, Нет" in str(exc.value)


def test_click_quick_reply_ambiguous_duplicate_buttons_refuse():
    """Два вопроса бота с одинаковыми кнопками не различимы — отказ, не догадка."""
    page = _FakeQuickReplyPage(["Да", "Нет", "Нет"])
    with pytest.raises(NoQuickReply):
        click_quick_reply(cast(Page, page), "Нет")
    assert page.clicked == []


class _UnrenderableQuickPage(_FakeQuickReplyPage):
    """Кнопка посчитана count(), но не отрисовалась: pre-click wait_for
    по таймауту кидает PlaywrightError — клика ещё НЕ было."""

    class _Handle:
        def inner_text(self) -> str:
            return "Нет"

        def wait_for(self, *, state=None, timeout=None):
            raise PlaywrightError("Timeout 3000ms exceeded")

        def click(self):  # pragma: no cover — до клика дело дойти не должно
            raise AssertionError("click не должен вызываться после таймаута wait_for")

    class _RoleLocator:
        def __init__(self, page: "_UnrenderableQuickPage", label: str):
            self._page = page
            self._label = label

        def count(self) -> int:
            return 1 if self._label in self._page.buttons else 0

        @property
        def first(self):
            return _UnrenderableQuickPage._Handle()

    def get_by_role(self, _role, *, name=None, exact=None):
        assert exact is True
        return _UnrenderableQuickPage._RoleLocator(self, name)

    def locator(self, selector: str):
        assert selector in (QUICK_REPLY_BUTTON, QUICK_REPLY_BUTTONS_WRAPPER)
        if selector == QUICK_REPLY_BUTTON:
            return _FakeQuickButtonList(self)
        return self


def test_click_quick_reply_pre_click_wait_timeout_is_no_quick_reply():
    """Таймаут pre-click wait_for — NoQuickReply (pre-click failed, #163),
    НЕ исключение клик-границы: клик ещё не выполнялся."""
    page = _UnrenderableQuickPage(["Да", "Нет"])
    with pytest.raises(NoQuickReply):
        click_quick_reply(cast(Page, page), "Нет")
    assert page.clicked == []

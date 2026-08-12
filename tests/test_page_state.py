"""Общий контракт неопределённого состояния браузерных страниц (#143)."""

from hhru_bot.apply.questions import QuestionDetection
from hhru_bot.browser import PAGE_STATE, PageStateIndeterminate
from hhru_bot.commands.probe import PageCheck
from hhru_bot.copy_resume import ResumeListIndeterminate
from hhru_bot.responses import NotAuthenticated


def test_browser_paths_share_indeterminate_exception_base():
    assert issubclass(NotAuthenticated, PageStateIndeterminate)
    assert issubclass(ResumeListIndeterminate, PageStateIndeterminate)


def test_flagged_page_states_use_common_vocabulary():
    assert QuestionDetection.no().page_state == PAGE_STATE["confirmed"]
    assert QuestionDetection.indeterminate_scope("неизвестно").page_state == PAGE_STATE[
        "indeterminate"
    ]
    assert PageCheck("x", "https://hh.ru", unreachable=True).page_state == PAGE_STATE[
        "unreachable"
    ]

"""Tests for the fail-closed transient overlay observer."""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from hhru_bot.transient_overlays import _SCRIPT

pytestmark = pytest.mark.integration


def test_observer_contract_is_narrow_and_hydration_aware() -> None:
    """Keep the safety boundary visible even when browser tests are deselected."""
    assert "cookies-policy-informer-accept" in _SCRIPT
    assert 'notification-close"] button[aria-label="Удалить"]' in _SCRIPT
    assert "observe(document" in _SCRIPT
    assert "data-qa', 'aria-label', 'role'" in _SCRIPT
    assert "Date.now() + 5000" in _SCRIPT
    assert "state.attempts >= 12" in _SCRIPT
    assert "querySelectorAll(candidateSelector)" in _SCRIPT
    assert "резюме доставлено|уведомлен|notification|toast" not in _SCRIPT


@pytest.mark.live_read
def test_observer_retries_ssr_hydration_and_rejects_unsafe_notification() -> None:
    """Exercise incremental mount/hydration in Chromium without navigating hh.ru."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content("<main></main>")
        page.add_script_tag(content=_SCRIPT)
        page.evaluate(
            """() => {
              const shell = document.createElement('section');
              shell.textContent = 'Работодатели не знают ваш статус поиска';
              document.querySelector('main').append(shell);
              setTimeout(() => {
                shell.setAttribute('data-qa', 'notification notification_info');
                const close = document.createElement('div');
                const button = document.createElement('button');
                button.onclick = () => shell.remove();
                close.append(button);
                shell.append(close);
                setTimeout(() => {
                  close.setAttribute('data-qa', 'notification-close');
                  button.setAttribute('aria-label', 'Удалить');
                }, 150);
              }, 150);
            }"""
        )
        page.wait_for_timeout(900)
        evidence = page.evaluate("() => window.__hhruTransientOverlayEvidence")
        assert len(evidence) == 1
        assert evidence[0]["selector"] == '[aria-label="Удалить"]'
        assert page.locator('[data-qa^="notification"]').count() == 0

        page.evaluate(
            """() => {
              const shell = document.createElement('section');
              shell.setAttribute('data-qa', 'notification notification_info');
              shell.textContent = 'Подтвердите отклик';
              const close = document.createElement('div');
              close.setAttribute('data-qa', 'notification-close');
              const button = document.createElement('button');
              button.setAttribute('aria-label', 'Удалить');
              close.append(button);
              shell.append(close);
              document.querySelector('main').append(shell);
            }"""
        )
        page.wait_for_timeout(300)
        assert page.locator('[data-qa^="notification"]').count() == 2
        assert len(page.evaluate("() => window.__hhruTransientOverlayEvidence")) == 1
        browser.close()

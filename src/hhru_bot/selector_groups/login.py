"""Authenticated login selectors backed by the selector contract catalog."""

from __future__ import annotations

from ._generated import selector as _selector

LOGIN_CODE_REQUEST_BUTTON = _selector("selectors.LOGIN_CODE_REQUEST_BUTTON")
LOGIN_EMAIL_TYPE = _selector("selectors.LOGIN_EMAIL_TYPE")
LOGIN_EMAIL_INPUT = _selector("selectors.LOGIN_EMAIL_INPUT")
LOGIN_PHONE_INPUT = _selector("selectors.LOGIN_PHONE_INPUT")
LOGIN_CODE_INPUT = _selector("selectors.LOGIN_CODE_INPUT")

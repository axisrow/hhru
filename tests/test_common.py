"""Unit tests for the simple common-screen editor."""

from unittest.mock import MagicMock

import pytest

import hhru_bot.common as common
from hhru_bot.common import CommonValues, apply_common, read_common

pytestmark = pytest.mark.unit


def test_common_values_exposes_only_first_slice():
    values = CommonValues(first_name="Ada", last_name="Lovelace", gender="female")
    assert values.provided() == {
        "firstName": "Ada",
        "lastName": "Lovelace",
        "gender": "female",
    }


def test_apply_common_fills_inputs_and_selects_gender():
    page = MagicMock()
    locators = {}
    for selector in (
        common.FIRST_NAME,
        common.LAST_NAME,
        common.BIRTHDAY,
        common.GENDER,
        common.PHONE,
    ):
        locators[selector] = MagicMock()
        locators[selector].count.return_value = 1
    page.locator.side_effect = lambda selector: locators[selector]

    apply_common(
        page,
        CommonValues(
            first_name="Ada",
            last_name="Lovelace",
            birthday="1815-12-10",
            gender="female",
            phone="+7",
        ),
    )

    locators[common.FIRST_NAME].first.fill.assert_called_once_with("Ada")
    locators[common.LAST_NAME].first.fill.assert_called_once_with("Lovelace")
    locators[common.BIRTHDAY].first.fill.assert_called_once_with("1815-12-10")
    locators[common.PHONE].first.fill.assert_called_once_with("+7")
    locators[common.GENDER].first.select_option.assert_called_once_with("female")


def test_read_common_fails_closed_on_ambiguous_selector():
    page = MagicMock()
    field = MagicMock()
    field.count.return_value = 2
    page.locator.return_value = field

    with pytest.raises(RuntimeError, match="не подтверждено однозначно"):
        read_common(page)

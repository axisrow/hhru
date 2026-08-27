"""Selectors for the applicant-visible resume search and public resume page."""

from ._generated import selector as _selector

# Keep the search link scoped to main: the header also contains resume links.
SEARCH_RESULT_LINK = "main a[href^='/resume/']"
SEARCH_MAIN = "main"
SEARCH_CARD = _selector("competitor_resume.SEARCH_CARD")
SEARCH_RESULT_TITLE_LINK = _selector("competitor_resume.SEARCH_RESULT_TITLE_LINK")
SEARCH_AREA_AND_RELOCATION = _selector("competitor_resume.SEARCH_AREA_AND_RELOCATION")
SEARCH_EMPTY = _selector("competitor_resume.SEARCH_EMPTY")
PAGINATION_NEXT = _selector("competitor_resume.PAGINATION_NEXT")
PAGINATION_BLOCK = _selector("competitor_resume.PAGINATION_BLOCK")
PAGINATION_PAGE = _selector("competitor_resume.PAGINATION_PAGE")
PAGINATION_LINK = "main a[href*='/search/resume'][href*='page=']"

DETAIL_MAIN = "main"
DETAIL_HEADING = "main h2"
DETAIL_PERSONAL_ADDRESS = _selector("competitor_resume.DETAIL_PERSONAL_ADDRESS")
DETAIL_RELOCATION = _selector("competitor_resume.DETAIL_RELOCATION")
DETAIL_PERSONAL_INFO = _selector("competitor_resume.DETAIL_PERSONAL_INFO")

"""Selectors for the applicant-visible resume search and public resume page."""

# Keep the search link scoped to main: the header also contains resume links.
SEARCH_RESULT_LINK = "main a[href^='/resume/']"
SEARCH_EMPTY = "[data-qa='resume-search-empty'], [data-qa='bloko-header-2']"
PAGINATION_NEXT = "[data-qa*='pager-next'], [data-qa*='pagination-next'], a[rel='next']"
PAGINATION_BLOCK = "[data-qa*='pager-block'], [data-qa*='pagination']"
PAGINATION_PAGE = "[data-qa*='pager-page'], [data-qa*='pagination-page']"
PAGINATION_LINK = "main a[href*='/search/resume'][href*='page=']"

DETAIL_MAIN = "main"
DETAIL_HEADING = "main h2"

"""Контактные данные аккаунта на ``/profile/me`` и в резюме.

Проверено live-дампом залогиненной сессии 2026-08-24 (read-only).

Каноничный account-level источник — ``/profile/me`` (ссылка из меню
профиля), а не указанный в первоначальном плане ``/applicant/personal``:
последний сейчас отвечает 404. На ``/applicant/resumes`` рендерятся имя в
шапке профиля и карточки резюме, но контактных данных там нет.

Отсутствие ``profile-common-card-city`` или отдельных contact-item на
конкретном аккаунте является легитимным результатом (поле не заполнено либо
скрыто настройками приватности), а не доказательством поломки селектора.
Чтение должно проверять count==1 и непустое значение; неоднозначность — не
записывать поле.
"""

from __future__ import annotations

from ._generated import selector as _selector

# ``/profile/me`` redirects to ``/applicant/profile/me``.  Its single visible,
# non-empty h1 is the current name/readiness marker; it must not by itself
# authorize absence-based cleanup of the older field schema below.
ACCOUNT_PROFILE_PATH = "/profile/me"
ACCOUNT_PROFILE_READY = _selector("account_profile.ACCOUNT_PROFILE_READY")

# Account-level fields from the authenticated live DOM observed on 2026-08-18.
# The common-card name/city selectors were absent on 2026-08-24, so the first
# name remains a separate schema marker before any stale values may be removed.
ACCOUNT_PROFILE_FIRST_NAME = _selector("account_profile.ACCOUNT_PROFILE_FIRST_NAME")
ACCOUNT_PROFILE_LAST_NAME = _selector("account_profile.ACCOUNT_PROFILE_LAST_NAME")
ACCOUNT_PROFILE_CITY = _selector("account_profile.ACCOUNT_PROFILE_CITY")
ACCOUNT_PROFILE_PHONE = _selector("account_profile.ACCOUNT_PROFILE_PHONE")
ACCOUNT_PROFILE_EMAIL = _selector("account_profile.ACCOUNT_PROFILE_EMAIL")

# Resume-level contact fields: confirmed in the authenticated live DOM of
# /resume/{resume_id}; name and city were not rendered as account fields there.
RESUME_CONTACT_PHONE = _selector("account_profile.RESUME_CONTACT_PHONE")
RESUME_CONTACT_EMAIL = _selector("account_profile.RESUME_CONTACT_EMAIL")

# /applicant/resumes: confirmed only as profile-header name; no contact fields
# were present in the observed DOM. Kept separate from account-level selectors.
RESUME_LIST_PROFILE_NAME = _selector("account_profile.RESUME_LIST_PROFILE_NAME")

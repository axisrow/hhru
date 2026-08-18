"""Контактные данные аккаунта на ``/profile/me`` и в резюме.

Проверено live-дампом залогиненной сессии 2026-08-18 (read-only).

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

# Account-level fields: confirmed in the authenticated live DOM of /profile/me.
ACCOUNT_PROFILE_PATH = "/profile/me"
ACCOUNT_PROFILE_FIRST_NAME = "[data-qa='profile-common-card-firstname']"
ACCOUNT_PROFILE_LAST_NAME = "[data-qa='profile-common-card-lastname']"
ACCOUNT_PROFILE_CITY = "[data-qa='profile-common-card-city']"
ACCOUNT_PROFILE_PHONE = "[data-qa='profile-contact-item-phone']"
ACCOUNT_PROFILE_EMAIL = "[data-qa='profile-contact-item-email']"

# Resume-level contact fields: confirmed in the authenticated live DOM of
# /resume/{resume_id}; name and city were not rendered as account fields there.
RESUME_CONTACT_PHONE = "[data-qa='resume-contact-phone-value-preferred']"
RESUME_CONTACT_EMAIL = "[data-qa='resume-contact-email-value']"

# /applicant/resumes: confirmed only as profile-header name; no contact fields
# were present in the observed DOM. Kept separate from account-level selectors.
RESUME_LIST_PROFILE_NAME = "[data-qa='profile-activator-fullname']"

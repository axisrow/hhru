"""Страница редактирования резюме (/resume/{hash}).

Публикация подтверждена разведкой #219 только в живом DOM после гидратации:
SSR содержит локализацию, но не кнопку. Значения ниже намеренно не считаются
подтверждёнными статическим HTML; браузерный код ждёт их в живом DOM и работает
fail-closed.
"""

from __future__ import annotations

# Existing bump selectors (confirmed by the bump feature's live check).
RESUME_BUMP_BUTTON = "[data-qa='resume-update-button']"
RESUME_BUMP_DISABLED_HINT = "[data-qa='resume-update-button-disabled']"

# Подтверждено живым DOM после React-гидратации (#219). Текстовый fallback нужен
# потому, что data-qa кнопки в SSR отсутствует.
RESUME_PUBLISH_BUTTON = "button:has-text('Опубликовать')"
RESUME_PUBLISH_BUTTON_DATA_QA = "[data-qa='resume-publish']"
# Только read-only сообщение о текущей видимости; команда его не нажимает.
RESUME_VISIBILITY_BUTTON = "button:has-text('Изменить видимость')"

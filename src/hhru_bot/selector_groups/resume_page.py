"""Страница резюме (/resume/{hash}).

На живом DOM issue #225 кнопка публикации не появилась: свежая копия была
заблокирована незавершённым шагом ``professional_role``. Селекторы публикации
сохраняются как fail-closed кандидаты до отдельного подтверждения на готовом
к публикации черновике.
"""

from __future__ import annotations

# Existing bump selectors (confirmed by the bump feature's live check).
RESUME_BUMP_BUTTON = "[data-qa='resume-update-button']"
RESUME_BUMP_DISABLED_HINT = "[data-qa='resume-update-button-disabled']"

# Кандидаты из исходного флоу (#219); на заблокированной копии #225 ни один не
# присутствовал в живом DOM. Команда обязана проверять count и не угадывать.
RESUME_PUBLISH_BUTTON = "button:has-text('Опубликовать')"
RESUME_PUBLISH_BUTTON_DATA_QA = "[data-qa='resume-publish']"
# Только read-only сообщение о текущей видимости; команда его не нажимает.
RESUME_VISIBILITY_BUTTON = "button:has-text('Изменить видимость')"

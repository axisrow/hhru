"""Блок фото (аватара) на странице резюме — подтверждено живым DOM
залогиненного пользователя 2026-09-02 на черновике резюме в состоянии
«фото нет» (read-only дампы ``explore_photo_block.py``) и ПОЛНЫМ БОЕВЫМ
ПРОГОНОМ загрузки (``explore_photo_full_flow`` / дампы ``photo_flow_*``,
``photo_assign_final_*`` в data/logs): фото загружено и назначено.

Разметка: аватар рендерит родительская страница (``data-qa="resume-avatar"``
с placeholder-иконкой ``placeholder-male`` в состоянии «фото нет»), а
загрузку — отдельный микрофронтенд ``resumePhotoViewer``
(``HH-ContainerForMicroFrontend-resumePhotoViewer``) с двумя скрытыми
``input[type=file]`` (gallery и camera, ``accept="image/jpeg,image/png,
image/jpg"``).

КЛЮЧЕВОЙ ИНВАРИАНТ — ГИДРАТАЦИЯ: в SSR-разметке file-input есть сразу, но БЕЗ
React-привязки (нет ``__reactProps$`` на узле) — change-событие никто не
обрабатывает, файл принимается в никуда. Микрофронтенд гидратируется ЛЕНИВО:
контейнер нужно проскроллить в вьюпорт, привязка появляется за ~2-4с
(подтверждено диагностикой explore_photo_upload_net 2026-09-02).

Боевой поток (каждый шаг подтверждён живым прогоном 2026-09-02):
1. скролл контейнера + ожидание React-привязки gallery-input
   (общий хелпер ``browser.wait_for_react_hydration``);
2. ``set_input_files`` на гидратированный gallery-input;
3. открывается crop-редактор -> клик ``photo-editor-apply`` (запускает upload);
4. открывается модалка «Все загруженные фото» -> клик
   ``photo-viewer-action-assign-current`` (назначает фото на резюме);
5. маркер успеха — НЕ ``<img>`` внутри ``resume-avatar`` сам по себе: он
   рисуется оптимистично, до консолидации assign-запроса на сервере
   (боевой кейс 2026-09-02: img остался, серверный ``hasPhoto`` — false).
   Подтверждение успеха — readback (#955): перезагрузка страницы резюме и
   повторное присутствие того же ``<img>`` в блоке аватара на свежем DOM.

Клик по ``resume-avatar-edit-button`` открывает модалку вьювера только УЖЕ
гидратированного микрофроненда и сам по себе загрузку не запускает.
"""

from __future__ import annotations

from ._generated import selector as _selector

# Контейнер микрофроненда resumePhotoViewer — цель scrollIntoView перед
# ожиданием гидратации (ленивая гидратация запускается попаданием в
# вьюпорт). Класс-селектор подтверждён живыми дампами 2026-09-02
# (photo_flow_*); без скролла React-привязка на file-input не появляется.
RESUME_PHOTO_MFE_CONTAINER = _selector("resume_photo.RESUME_PHOTO_MFE_CONTAINER")

# Скрытый постоянный input[type=file] микрофроненда resumePhotoViewer.
# accept="image/jpeg,image/png,image/jpg" подтверждён живым DOM 2026-09-02
# (read-only дамп authenticated-сессии). Один и тот же узел обслуживает и
# поллинг гидратации (wait_for_react_hydration), и адресацию set_input_files:
# различает вызов, а не константа — отдельный дубликат не заводится
# (прецедент #840: совпадающие data-qa не дублируются константами).
# ВАЖНО: перед передачей файла дождаться гидратации — негидратированный
# инпут молча съедает файл (см. docstring модуля).
RESUME_PHOTO_FILE_INPUT = _selector("resume_photo.RESUME_PHOTO_FILE_INPUT")

# Контейнер аватара на странице резюме. Подтверждён живым DOM 2026-09-02;
# в состоянии «фото нет» содержит placeholder-иконку без <img>.
RESUME_AVATAR_BLOCK = _selector("resume_photo.RESUME_AVATAR_BLOCK")

# Круглая кнопка-карандаш поверх аватара. Подтверждена живым DOM 2026-09-02;
# открывает модалку вьювера только после гидратации микрофроненда, сама
# по себе загрузку не запускает (боевой поток идёт через set_input_files).
RESUME_AVATAR_EDIT_BUTTON = _selector("resume_photo.RESUME_AVATAR_EDIT_BUTTON")

# <img> внутри контейнера аватара. Подтверждён БОЕВЫМ ПРОГОНОМ 2026-09-02:
# после assign-current <img> появился в блоке аватара (дамп
# photo_assign_final_*). Как КОМАНДНЫЙ сигнал успеха НЕ используется напрямую:
# маркер оптимистичный (#955) — успехом считается только его наличие ПОСЛЕ
# перезагрузки страницы (readback персистентного состояния,
# resume_photo._readback_photo_persisted).
RESUME_AVATAR_IMAGE = _selector("resume_photo.RESUME_AVATAR_IMAGE")

# Явный маркер состояния «фото нет»: плейсхолдер-иконка вместо <img> внутри
# того же блока аватара. Подтверждён живым read-only дампом 2026-09-02
# (explore_photo_full_*: data-qa='placeholder-male'; в дампе с фото
# photo_assign_final_* отсутствует). Readback #955: отсутствие фото
# считается подтверждённым только по этому маркеру — простое отсутствие
# img в момент проверки не доказывает ничего (img может быть ещё не
# дорендерен SPA).
RESUME_AVATAR_PLACEHOLDER = _selector("resume_photo.RESUME_AVATAR_PLACEHOLDER")

# Кнопка применения crop-редактора — второй шаг боевого потока, запускает
# upload. Подтверждена боевым прогоном 2026-09-02 (модалка редактора
# открылась после set_input_files, клик привёл к модалке назначения;
# дамп photo_flow_editor_*).
RESUME_PHOTO_EDITOR_APPLY = _selector("resume_photo.RESUME_PHOTO_EDITOR_APPLY")

# Кнопка «назначить на текущее резюме» в модалке «Все загруженные фото» —
# третий шаг боевого потока. Подтверждена боевым прогоном 2026-09-02:
# клик привёл к появлению <img> в аватаре (дамп photo_assign_final_*).
# Модалка анимируется: кликать после затухания overlay, иначе Playwright
# ретраит и падает по таймауту (наблюдено).
RESUME_PHOTO_VIEWER_ASSIGN_CURRENT = _selector("resume_photo.RESUME_PHOTO_VIEWER_ASSIGN_CURRENT")

# Допустимые расширения — из подтверждённого accept-атрибута input выше
# (живой DOM 2026-09-02), не из головы.
PHOTO_ACCEPTED_EXT: tuple[str, ...] = (".jpg", ".jpeg", ".png")

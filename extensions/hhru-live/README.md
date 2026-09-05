# HH.ru Live Overlay Diagnostics (MVP первого этапа #588)

MV3-расширение для живой вкладки hh.ru: обнаруживает динамически добавленные
modal/dialog/toast/notification/cookie-баннеры, классифицирует их по политике
и закрывает **только безопасные** — через реальные DOM-клики в браузере
пользователя, не через Playwright. Опасные и неоднозначные окна блокируются
и возвращаются агенту/пользователю на решение.

## Установка

Быстрый запуск одной командой (загружает расширение флагом `--load-extension`
в небрендированный Chromium из кэша Playwright, профиль в
`data/extension-profile/`):

```bash
./scripts/run_extension_chrome.sh
```

Ручная альтернатива: `chrome://extensions` → Developer mode → Load unpacked →
каталог `extensions/hhru-live/`. Открыть `https://hh.ru`, кликнуть иконку
расширения: popup показывает статус подключения, кнопку «Scan overlays» и
журнал диагностики.

## Политика классификации (fail-closed)

С #929 классификация живёт в отдельном `policy.js`, загружаемом manifest.json
**до** `content.js` (его top-level биндинги — глоблы того же isolated world);
`content.js` отвечает за детект/реестр/команды и потребляет политику.
Сценарии классификации напрямую — `tests/js_harness/run_policy_scenario.js`.

`policy.js`/`content.js` присваивают каждому видимому overlay одну из четырёх
групп — пересчитывая её заново в момент ЛЮБОГО решения, а не доверяя
закешированной:

| Группа | Что это | Автодействие |
|---|---|---|
| `dangerous` | Текстовые якоря опасности: captcha/«не робот», удалить/удаление (не «удалённый»), отозвать, необратим, «вы уверены», подтвердить/confirm | Никогда. Только репорт |
| `apply_step` | Форма отклика — только структурные якоря (`RESPONSE_MODAL_FORM_ID`, `data-qa*="vacancy-response"` — оба shape), тест-вопросы (`task-question`/`task-body`), тексты «сопроводительное письмо/тестовое задание/анкета» | Никогда. Часть сценария отклика |
| `safe` | Toast/notification/cookie-баннер по природе (включая тост «Отклик отправлен»); modal/overlay — только при явном видимом close-контроле | Разрешён dismiss |
| `ambiguous` | Modal/overlay без close-контрола и без сигналов | Блокирован, решение за агентом |

Якоря опасности нарочно узкие: промах оставляет окно просто заблокированным
(безопасная сторона). Известный trade-off: cookie-баннер в формулировке
«подтвердите согласие» попадёт в `dangerous` — осознанный fail-closed. Голое
`/отклик/i` в apply-якорях НЕТ: тост «Отклик отправлен» — штатное
подтверждение после submit, он должен оставаться закрываемым (PR #935 review).

**Close-контролы — только явные close-маркеры**: `aria-label`/`title`
«закрыть/close/dismiss» (авторская намеренность, достаточна сама по себе),
`data-qa`/class `close`, глиф `×`/`✕` у интерактивного элемента (одиночная
латинская `x` — только на настоящей кнопке: `button`/`a`/`[role=button]`,
декоративный спан с data-qa не считается — PR #935 review). «Понятно»,
«Отмена», «Сохранить», «Принять» close-маркерами НЕ являются никогда.
Кнопка «Сохранить» в тосте «Резюме доставлено» (#586 — выбор статуса поиска,
профильные данные) не кликается ни при каком раскладе; закрывается только
крестик. DOM не удаляется (`element.remove` запрещён стражем) — окно закрывает
сам сайт, реагируя на клик по крестику.

## Команды (allowlist)

Ровно три действия, всё прочее — `action_not_allowed` (fail-closed):

- `list_overlays` — видимые overlay с `{id, type, disposition, closeControls, text}`;
- `dismiss_overlay {id, selector?}` — закрыть `safe`-overlay кликом по
  close-контролу; возвращает `{type, disposition, action, overlayGone,
  elements, finalState}` и опционально подтверждение доступности следующего
  элемента (`check_element` по `selector`);
- `check_element {selector}` — `found/visible` + obstruction-проба через
  `elementFromPoint` (там, где API доступен; иначе честно
  `obstructionChecked: false`).

Транспорт: popup → `background.js` (relay `agent_command` в активную hh.ru
вкладку) → `content.js`. content.js принимает команды только от своего же
расширения (`sender.id === chrome.runtime.id`).

С #931 relay исполняется тестами по-настоящему: `run_background_scenario.js`
(стаб chrome.tabs/storage.session) покрывает пересылку в hh.ru-таб и обе
доменные ошибки (`no_hhru_tab`, `content_script_unreachable`), отказ чужому
sender и действию вне allowlist, а также сохранность diagnostics-пути
(`overlay_detected` → storage.session); ветки чужого происхождения — по
образцу sender-validation #743.

Канал `chrome.runtime.connect({name: "hhru-agent"})` по-прежнему **не
достижим никем**: ни content.js, ни popup.js его не открывают, `background.js`
регистрирует только `chrome.runtime.onConnect` (не `onConnectExternal`), в
`manifest.json` нет `externally_connectable` — внешний caller отклоняется ещё
до слушателя (см. #743). Полноценный агентский мост (Native Messaging) —
второй этап #588, здесь его нет намеренно.

## Селекторы — статус проверки (#932, сверка с живым DOM 2026-09-05)

Якоря, упомянутые ниже, с #929 определены в `policy.js` (детект-селекторы,
включая `[data-qa*="cookie"]`, — в `content.js`).

Формат — по образцу «Селекторы — статус проверки» из CLAUDE.md. Сверка
строго read-only: анонимная главная через curl-дамп, залогиненный профиль —
через живую вкладку (ценз видимых overlay + cross-check с реестром
селекторов `selectors/reference-map.yaml`, все его записи `documented_live`).

| Якорь / контрол | Статус | Живое доказательство |
|---|---|---|
| Cookie-информер: `div[data-qa="cookies-policy-informer"]`, класс `wrapper--*` (без «cookie»), кнопка «Понятно» `data-qa="cookies-policy-informer-accept"` | подтверждено живым DOM (анонимный curl-дамп 2026-09-05) | информер НЕ ловится `[class*="cookie"]` → в `OVERLAY_SELECTORS` добавлен `[data-qa*="cookie"]`; «Понятно» — не close-маркер, dismiss вернёт `no_close_control`, согласие не кликается никогда |
| State-класс баннера `cookie-policy-banner-enabled` на `<body>` | подтверждено живым DOM 2026-09-02 (анонимная главная) | страж html/body в `reportIfNewlyVisible` |
| Крестики модалок: `data-qa="profile-modal-button-close"`, `photo-viewer-close`, `bloko-modal-close`, `editor-modal-close-icon`, `resume-delete-close` | подтверждено живым DOM (профиль 2026-09-05 + реестр селекторов, все `documented_live`) | реальные крестики — всегда data-qa `*-close`, БЕЗ aria-label «закрыть» и без глифа × (svg-иконка); рабочее плечо `findCloseControls` — `/close/` по data-qa |
| Нотификации: контейнер `Bloko-Notification-Manager notification-manager` присутствует на странице всегда, даже пустой | подтверждено живым DOM (профиль 2026-09-05) | детектор репортит его как `notification`/`safe` с `no_close_control` — шум, но безопасный (клик невозможен); фильтрация пустых контейнеров — осознанно НЕ делалась (не якорь, а логика) |
| Кнопка удаления в нотификациях: `[data-qa='notification-close'] button[aria-label='Удалить']` | подтверждено реестром селекторов (живой DOM более ранних прогонов) | aria-label не виден в textContent → `collectText` теперь включают `aria-label` потомков: такая нотификация попадёт в `dangerous` (/удалить/i), автозакрытие исключено |
| Форма отклика: `form#RESPONSE_MODAL_FORM_ID`, `data-qa*="vacancy-response"`, task-question/task-body | **UNCONFIRMED** | клик «Откликнуться» создаёт тему отклика (инцидент 2026-08-16) — живой DOM модалки не снимался; закроет боевой apply второго этапа |
| Тост «Резюме доставлено» / «Отклик отправлен» | **UNCONFIRMED** | transient UI (#586: popup исчез до снятия); ловить только перехватом сразу после боевого действия |

Permissions минимальны: `storage` (журнал диагностики в `storage.session`,
переживает рестарт MV3 service worker) + host hh.ru. Диагностика подключения
— статус в popup + журнал `connected`/`overlay_detected`.

## CLI question

An MV3 extension cannot install or launch a local CLI: Chrome extension APIs do not provide arbitrary process execution. A future bridge can use **Native Messaging** (a separately installed host manifest and executable) or a CLI-owned local HTTP/WebSocket server. Native Messaging is the selected future option because it avoids exposing a listening network port; it is intentionally not part of this stage.

Issue #588 status: первый этап реализован этим MVP — детект (#644/#743/#767) +
классификация + allowlist-команды + закрытие безопасных + транспорт popup→relay.
Вне первого этапа остались: read-only сверка якорей на живом DOM (#932) и
агентский мост на Native Messaging (второй этап).

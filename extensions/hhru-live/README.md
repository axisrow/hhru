# HH.ru Live Overlay Diagnostics (этапы 1–2 #588)

MV3-расширение для живой вкладки hh.ru: обнаруживает динамически добавленные
modal/dialog/toast/notification/cookie-баннеры, классифицирует их по политике
и закрывает **только безопасные** — через реальные DOM-клики в браузере
пользователя, не через Playwright. Опасные и неоднозначные окна блокируются
и возвращаются агенту/пользователю на решение. С этапа 2 (#1160) поверх
детектора работает исполнитель allowlist-команд внешнего агента: клик по
data-qa/лейблу, чтение DOM-состояния, ожидание условия — каждый клик через
policy-ядро, опасные цели отказываются без клика.

## Установка

Основной способ — CLI-команда `live-browser` (#1209): Playwright запускает
браузер с расширением и **нативно грузит сессию аккаунта** (storage_state) —
hh.ru принимает этот класс клиента (#1206: клон кук в чужой браузер антифрод
отвергает):

```bash
./scripts/run.sh live-browser            # вкладка /applicant/resumes
./scripts/run.sh live-browser --headless # без окна
```

Запасной способ — загружает расширение флагом `--load-extension` в
небрендированный Chromium из кэша Playwright, профиль в
`data/extension-profile/` (профиль остаётся БЕЗ сессии — сессию в него
не засеять, #1206):

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

Семь действий, всё прочее — `action_not_allowed` (fail-closed). Allowlist
живёт дословно в двух копиях — `ACTION_ALLOWLIST` (content.js) и
`RELAY_ACTIONS` (background.js); гвард-тест требует совпадения литералов
(и зеркала в `src/hhru_bot/live/allowlist.py`):

- `list_overlays` — видимые overlay с `{id, type, disposition, closeControls, text}`;
- `dismiss_overlay {id, selector?}` — закрыть `safe`-overlay кликом по
  close-контролу; возвращает `{type, disposition, action, overlayGone,
  elements, finalState}` и опционально подтверждение доступности следующего
  элемента (`check_element` по `selector`);
- `check_element {selector}` — `found/visible` + census-`text` (нормализованный
  текст ≤200 символов, без сырого HTML) + obstruction-проба через
  `elementFromPoint` (там, где API доступен; иначе честно
  `obstructionChecked: false`);
- `click_element {dataQa|label|selector, waitFor, allowApply?}` — клик по цели
  через policy-ядро (см. «Исполнитель» ниже);
- `wait_element {dataQa|label|selector, state, timeoutMs}` — ждать появления
  (`state: "visible"`) или исчезновения (`state: "hidden"`) с ЯВНЫМ таймаутом;
- `get_page_state` — текущий URL, title, readyState;
- `fill_element {dataQa|label|selector, text}` (#1162) — записать текст в
  поле через native value setter + input/change (React-совместимый путь);
  гейт опасности (captcha-якоря по тексту И атрибутам — пустой input смысл
  несёт в data-qa/id, не в textContent); значение читается обратно,
  расхождение — `ok:false`, а не молчаливая полузаполненная форма.

## Исполнитель (executor.js, #1160)

`executor.js` грузится манифестом между `policy.js` и `content.js`:
потребляет policy-ядро, а `content.js` диспетчеризует его функции. Ровно
один клик-сайт в файле — `target.click()` в `clickElement()`, достигается
только после полной цепочки гейтов:

1. **Адресация** — ровно один режим на команду: `dataQa` (точный атрибут),
   `label` (aria-label / title / видимый текст, нормализация пробелов,
   без регистра) или CSS `selector` (как у `check_element`). Ноль совпадений —
   `element_not_found`, больше одного — `ambiguous_target` с matchCount,
   невидимая цель — `element_not_visible`. Никаких «кликнем первый молча».
2. **Policy-гейт до клика** (ядро #929 переиспользуется, якоря не
   расширяются): якоря опасности по тексту поддерева цели + aria-label
   (`dangerous`), apply-сигналы (`apply_step` — только БЕЗ явной авторизации,
   см. ниже), затем disposition overlay-
   ПРЕДКА цели (`ambiguous` — тоже отказ). Отказ = `policy_refused` со
   структурированным вердиктом и НУЛЁМ кликов. Предок ищется строго выше
   цели: hh.ru кладёт подстроки «cookie»/«modal» и в data-qa листовых
   контролов, цель классифицируется по содержимому, а не по удаче подстрок.
   Предок — ВЕРХНИЙ matching-узел цепочки: одна модалка hh.ru — семейство
   вложенных overlay-узлов (#1214, боевой прогон testing 2026-09-23: outer
   `apply_step`, inner `ambiguous`, клик бьёт по inner), и семейство
   сворачивается в один оверлей — иначе ближайший safe/ambiguous-inner
   маскировал бы вердикт всей модалки, а safe-inner обходил бы
   dangerous-наружную. Остаточное направление свёртки осознано: опасный/
   ambiguous оверлей, вложенный СТРОГО внутрь safe/apply_step-верха,
   вердиктится по верхнему (раньше ближайший опасный отказывал); реальные
   опасные модалки hh.ru порталит в body, а не внутрь safe-обёрток.
   «Строжайший disposition в цепочке» сознательно не делался: при свёртке
   inner-узел не оценивается вовсе (не «понижается» allowApply), и строгий
   проход вернул бы отказ ambiguous-inner в боевом случае #1214 — клик по
   пикеру внутри apply_step-верха.
   **allowApply (#1162)**: явная команда сценария отклика понижает ТОЛЬКО
   apply_step-отказ самой цели (context `apply_flow`) — это решение агента,
   как явное имя цели; `dangerous` и ambiguous-предок отказывают и с
   allowApply. Без флага (этап 1, авто-режимы) поведение не изменилось.
3. **Объявлённое post-click условие обязательно** (`visible != гидратирован`,
   CLAUDE.md): клик без `waitFor {state, timeoutMs, dataQa|label|selector}`
   отказывается (`wait_required`) ДО клика — исход клика, запускающего
   React-рендер, доказывается только дождиваньем состояния DOM, никогда
   самим фактом клика. Таймауты явные всегда (`timeout_required`), жёсткий
   потолок 60 с.
4. **Структурированный результат** каждой команды: `action`, `policy`
   (вердикт + контекст), `target` (tag/data-qa/текст), `wait` (met +
   elapsedMs), `finalState` (url, состояние цели после клика, снимок
   реестра overlay без текстов). Истёкший таймаут `wait_element` — честный
   `ok` с `met: false`, не ошибка.

**Полная навигация рвёт контракт «каждому id — ответ»** (review #1169):
клик по ссылке с полноценной загрузкой страницы (пагинация, обычные
переходы hh.ru) уничтожает content script до `sendResponse`. SPA-переходы
(pushState) этим не страдают — content script переживает их, и исход
доказывается `waitFor`; полная загрузка — нет, и изнутри вкладки это не
починить. Слой релея конвертирует закрытие порта в
`{error: "content_script_unreachable"}`, но семантика такого ответа —
«исход неопределён, клик мог сработать» (по смыслу `uncertain` #176), а не
«клик не прошёл»; доставка port-close при выгрузке документа Chrome'ом
гарантированно не доказана. Поэтому обязанность верхнего слоя (контракт с
S1, #1159): сервер держит per-command timeout и по его истечении отдаёт
агенту ЯВНЫЙ error-исход — тишина на проводе становится различимым
вердиктом, а не подвисанием.

Явная команда агента — не авто-dismiss этапа 1: `dismiss_overlay` по-прежнему
никогда не выбирает кнопки-действия («Понятно», «Сохранить») как
close-контролы, а `click_element` исполняет явное решение агента — за тем же
классификатором.

Транспорт команд: WebSocket-мост (ниже) или popup → `background.js`
(relay `agent_command` в активную hh.ru вкладку) → `content.js`. content.js
принимает команды только от своего же расширения (`sender.id ===
chrome.runtime.id`).

## WebSocket-мост (background.js, #1160 поверх #1159)

Решение владельца (#1159): транспорт — **loopback WebSocket**, не Native
Messaging. Серверная половина — CLI-команда `live-serve` (пакет
`src/hhru_bot/live/`, отдельный PR этапа 2); там же — единый источник
описания протокола и allowlist. Расширение зеркалит литерал allowlist и
версию протокола; гвард-тесты не дают копиям разъехаться.

- Подключение: service worker сам открывает `ws://127.0.0.1:8765` при
  старте (константа `LIVE_SERVE_URL`; `live-serve` поднимает порт по
  умолчанию согласованно).
- Envelope: `{v, id, action, payload}` → ответ `{id, status, result}`
  (`status: "ok"|"error"`; в `result` — ответ content-скрипта без флага
  `ok`, ошибки релея — как `{error: "no_hhru_tab"}` и т.п.).
- Fail-closed: неизвестная версия (`unsupported_version`), действие вне
  allowlist (`action_not_allowed`), команда без id
  (`command_id_required`) — явные ошибки, в кладку не идут. Без активного
  соединения команды не исполняются физически — сокет единственная точка
  входа.
- В payload копируются только перечисленные скалярные поля
  (`COMMAND_FIELDS`/`WAIT_FIELDS`) — прочее содержимое отбрасывается.
- Heartbeat: `{kind: "heartbeat"}` каждые 15 с пока соединение открыто —
  сервер отличает живого клиента от мёртвого. Разрыв — не ошибка сценария:
  reconnect с backoff 1с → 30с (потолок).
- Keep-alive канала (#1187, #1197): цепочка reconnect-setTimeout живёт
  только пока жив service worker — MV3 усыпляет его по idle (~30 с), и если
  сервера не было при старте браузера, ~90 с попыток исчерпывались и канал
  не поднимался уже никогда. Пока открыта вкладка hh.ru, content.js пингует
  background каждые 20 с (`kind: "keepalive"`, answered-and-discarded):
  каждое сообщение контент-скрипта будит заснувший SW и сбрасывает его
  idle-таймер, так что воркер и его ретраи не умирают, и сервер, поднятый
  после запуска Chrome, ловится сам. Самый быстрый путь при холодном старте
  — прежний порядок «сначала сервер, потом вкладка»
  (docs/live-checklist.md, шаг 0): `connected` при загрузке вкладки
  коннектится в первый же ретрай.
- Per-command timeout — ответственность сервера (review #1169): при полной
  навигации вкладки после клика ответ по id может не уйти вовсе (см.
  «Исполнитель»); сервер обязан по таймауту команды отдавать явный
  error-исход, превращая тишину на проводе в различимый вердикт.
- Аутентификация канала — открытая точка контракта с S1 (review #1169):
  любой локальный процесс, занявший порт до старта `live-serve` или в окне
  reconnect после его остановки, становится источником команд (в границах
  policy-ядра, но всё же). Направление решения — разделяемый токен: сервер
  печатает его при старте, в расширение он попадает через
  popup/chrome.storage; сделать ДО обрастания примитивов сценариями S3/S4.
- Канал `chrome.runtime.connect({name: "hhru-agent"})` по-прежнему не
  достижим никем из самого расширения и не используется мостом: ни
  content.js, ни popup.js его не открывают, `background.js` регистрирует
  только `chrome.runtime.onConnect` (не `onConnectExternal`), в
  `manifest.json` нет `externally_connectable` — внешний caller отклоняется
  ещё до слушателя (см. #743).

С #931 relay исполняется тестами по-настоящему: `run_background_scenario.js`
(стаб chrome.tabs/storage.session) покрывает пересылку в hh.ru-таб и обе
доменные ошибки (`no_hhru_tab`, `content_script_unreachable`), отказ чужому
sender и действию вне allowlist, а также сохранность diagnostics-пути
(`overlay_detected` → storage.session); ветки чужого происхождения — по
образцу sender-validation #743. Исполнитель и мост покрыты так же:
`run_executor_scenario.js` (гейты клика, отказы без клика, явные ожидания)
и `run_ws_bridge_scenario.js` (envelope, fail-closed версии/действия,
белый список payload, reconnect, heartbeat).

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
| Форма отклика: `form#RESPONSE_MODAL_FORM_ID`, `data-qa*="vacancy-response"` | подтверждено боевым apply-live (#1162, 2026-09-20 + #1214, 2026-09-23): модалка классифицируется `apply_step` по структурным якорям, клики по пикеру/письму/submit проходят с allowApply | census канала #1214: одна модалка — семейство вложенных overlay-узлов (outer `apply_step`, inner `ambiguous`); вердикт клика снимается по верхнему предку (см. «Исполнитель») |
| Тест-вопросы: task-question/task-body | **UNCONFIRMED** | заимствовано из konard reference, живым DOM не сверено; подтвердится боевым apply на вакансии с анкетой |
| Warning скрытого резюме внутри формы: `[data-qa='hidden-resume-warning']` (collapsible «поменяйте видимость резюме на …») | подтверждено живыми дампами probe на testing (2026-09-09: 136244658, 137014905 — элемент внутри `RESPONSE_MODAL_FORM_ID`, в неблокированных формах отсутствует) | боевой случай #1214 (2026-09-23): модалка требования видимости → skip `resume_visibility` в сценарии apply-live; переключатель видимости — мутация профиля, кодом не кликается |
| Тост «Резюме доставлено» / «Отклик отправлен» | **UNCONFIRMED** | transient UI (#586: popup исчез до снятия); ловить только перехватом сразу после боевого действия |

Permissions минимальны: `storage` (журнал диагностики в `storage.session`,
переживает рестарт MV3 service worker), `alarms` (периодический self-wake SW:
reconnect-таймеры умирают вместе со спящим SW, alarm пересоздаёт соединение
к уже запущенному CLI-серверу — порядок «сервер → вкладка» перестаёт быть
обязательным, #1181; демона нет — foreground-инвариант #1159 цел) + host
hh.ru. Диагностика подключения — статус в popup + журнал
`connected`/`overlay_detected`.

## CLI bridge

An MV3 extension cannot install or launch a local CLI: Chrome extension APIs do not provide arbitrary process execution. Earlier stage-1 notes picked Native Messaging as the future option; the owner's stage-2 decision (#1159) selected a **loopback WebSocket** instead: the CLI `live-serve` command listens on 127.0.0.1, the extension's service worker connects out to it (see «WebSocket-мост» above) — no extra manifest permissions, no native-host installation.

Issue #588 status: первый этап реализован MVP — детект (#644/#743/#767) +
классификация (#929: policy.js) + allowlist-команды + закрытие безопасных
(#930: аудит 2026-09-05 подтвердил полное соответствие) + внутренний транспорт
popup→relay (#931; MVP #935, тестовый добор — PR #984).
Живая read-only сверка якорей — #932 (выполнена 2026-09-05). Второй этап:
транспорт — loopback WebSocket (#1159, CLI `live-serve`), исполнитель команд
в живой вкладке — #1160 (этот PR). Сценарии поверх примитивов (bump/apply) —
S3/S4 (#1161/#1162).

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

`content.js` присваивает каждому видимому overlay одну из четырёх групп —
пересчитывая её заново в момент ЛЮБОГО решения, а не доверяя закешированной:

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

Канал `chrome.runtime.connect({name: "hhru-agent"})` по-прежнему **не
достижим никем**: ни content.js, ни popup.js его не открывают, `background.js`
регистрирует только `chrome.runtime.onConnect` (не `onConnectExternal`), в
`manifest.json` нет `externally_connectable` — внешний caller отклоняется ещё
до слушателя (см. #743). Полноценный агентский мост (Native Messaging) —
второй этап #588, здесь его нет намеренно.

## Что НЕ подтверждено живым DOM (честно)

Якоря классификации и close-маркеры — гипотезы, написанные по документации
проекта (CLAUDE.md, #586) и структуре hh.ru, но **не снятые с живого DOM**:

- фактические классы/aria-label/data-qa крестиков модалок и тостов hh.ru;
- реальный DOM тоста «Резюме доставлено» (transient UI — см. #586, popup
  исчез до снятия селекторов);
- close-маркеры cookie-баннера hh.ru (частично подтверждено живым DOM
  2026-09-02, анонимная главная: баннер включается state-классом
  `cookie-policy-banner-enabled` на `<body>` — отсюда страж, что `html`/`body`
  никогда не регистрируются как overlay; сам элемент баннера самоудаляется
  до осмотра, его крестик не снят).

Первая боевая проверка — вручную через этот MVP в своём Chrome; системная
read-only сверка — вынесена в follow-up (#932). При расхождении симптом такой:
окно репортится как `ambiguous`/не закрывается — сверить DOM вкладки (F12) и
поправить якоря в `content.js`.

Permissions минимальны: `storage` (журнал диагностики в `storage.session`,
переживает рестарт MV3 service worker) + host hh.ru. Диагностика подключения
— статус в popup + журнал `connected`/`overlay_detected`.

## CLI question

An MV3 extension cannot install or launch a local CLI: Chrome extension APIs do not provide arbitrary process execution. A future bridge can use **Native Messaging** (a separately installed host manifest and executable) or a CLI-owned local HTTP/WebSocket server. Native Messaging is the selected future option because it avoids exposing a listening network port; it is intentionally not part of this stage.

Issue #588 status: первый этап реализован этим MVP — детект (#644/#743/#767) +
классификация + allowlist-команды + закрытие безопасных + транспорт popup→relay.
Вне первого этапа остались: read-only сверка якорей на живом DOM (#932) и
агентский мост на Native Messaging (второй этап).

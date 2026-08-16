# Design-док: data-model для WRITE-команд над negotiations + граница эталон/реализация в cli-spec

**Ишью:** [#55](https://github.com/axisrow/hhru/issues/55) (архитектурный долг из 4-циклового review PR #53).
**Статус:** design-док **для решения пользователем**. Здесь варианты и рекомендация —
финальное решение не принято. Этот документ **ничего не закрывает**; после выбора модель
разбивается на sub-issues (расширение схемы, рефакторинг спеки, реализация команд).

---

## 0. Контекст и почему это дизайн-проблема, а не задача «сделай по аналогии»

PR #53 (Closes #21, спецификация CLI) прошёл **4 цикла cycle-review** с 13 FIX для
docs-only PR. Каждый цикл вскрывал новое противоречие между write-контрактом спеки и
read-only data-model, поверх которой она построена. Симптом повторяемый → значит, проблема
в фундаменте (data-model под write не спроектирован), а не в формулировках спеки.

Этот док отвечает на два вопроса ишью:
- **ЧАСТЬ A.** Какая data-model нужна для write-операций над negotiations.
- **ЧАСТЬ B.** Где проходит граница «эталон интерфейса» vs «дизайн реализации» в
  `docs/cli-spec.md`, и что вынести в feature-ишью.

---

## 1. Подтверждение противоречий (диагноз #55 — по коду)

Три противоречия из тела #55 сверены с исходным кодом. **Все три подтверждены.**

### 1.1. Схема `responses` — read-only модель без write-semantics

`src/hhru_bot/history.py` SCHEMA (таблица `responses`, строки 36-50):

```sql
CREATE TABLE IF NOT EXISTS responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT,              -- опционален, НЕ в ключе UNIQUE
    vacancy_id TEXT NOT NULL,
    topic TEXT,
    employer TEXT,
    status TEXT NOT NULL,        -- последний бейдж hh.ru (invitation/discard/...)
    last_status TEXT,
    chat_url TEXT,
    response_date TEXT,
    last_seen_at TEXT NOT NULL,
    status_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (vacancy_id, topic)
);
```

Что здесь **нет** (а write-командам нужно):
- `message_id` последнего сообщения — чтобы дедуплицировать ответ по конкретному
  входящему, а не по `(vacancy_id, topic)` в целом.
- **Автора последнего сообщения** (работодатель vs соискатель) — `reply-employers` должен
  отвечать только там, где последнее слово за HR. Бейдж `status` этого не даёт: `read` не
  означает «HR написал последнее», а `response` не гарантирует, что мы ещё не ответили.
- **Read-state** — факта «мы видели/отвечали на это сообщение».
- **Факта отправленного нами ответа** — нет колонки/строки «мы ответили в topic=N».

`upsert_response()` (history.py:258-346) — upsert-«свежий статус», перезаписывает строку
при каждом scrape. Это правильно для мониторинга (#12) и **неправильно** для идемпотентной
записи об отправке: scrape затёр бы «мы ответили» свежим бейджем hh.ru.

### 1.2. Парсер `responses.py` не извлекает write-полей

`src/hhru_bot/responses.py`, `ResponseItem` (строки 109-128) и `parse_response_card()`
(строки 195-232) извлекают только: `vacancy_id`, `status` (нормализованный бейдж),
`employer`, `chat_url`, `topic`, `date`, `raw_status`. **Ни автора, ни message-id, ни
read-state.** Селекторы `selector_groups/negotiations.py` тоже покрывают только этот набор
и помечены **«НЕ подтверждено»** (рендерятся только залогиненному через JS — анонимный curl
их не отдаёт, сверять негде до первого боевого входа).

**Следствие:** ответ «нужен ли ответ в этом чате» сегодня **невозможно** принять по
`responses`-данным — там нет ни автора последнего сообщения, ни факта нашей отправки.
Спека (§3.3, строки 309-311) это признаёт и требует «решать по живой истории чата», но
живой истории в модели нет — её ещё нужно завести.

### 1.3. Account-scope vs `--resume` (ОСНОВАНИЕ ОПРОВЕРГНУТО, см. #200)

> **ОПРОВЕРГНУТО 2026-08-16 (#200).** Проверка на живой сессии показала, что SSR
> `applicantNegotiations.topicList[]` содержит поле **`resumeId`**, заполненное у
> **7 из 7** переписок аккаунта (дублируется в `resources[type=RESUME]`, а также
> отдаётся `chatik.hh.ru/chatik/api/chats` на тех же куках). Привязка чата к резюме
> **существует и доступна браузеру** — она просто не выводится в видимую разметку
> карточки, и `negotiations_probe.topic_refs()` до #200 читал лишь 3 поля из ~52,
> молча отбрасывая остальные.
>
> Исходное рассуждение ниже опиралось на осмотр видимого DOM и на тест **собственной
> схемы БД**: `test_upsert_same_vacancy_different_topics_are_distinct_rows` доказывает
> лишь, что наша SQLite умеет хранить несколько `topic` на вакансию, и ничего не
> говорит о том, какие данные отдаёт hh.ru. Формулировка «подтверждено тестом» была
> подменой: проверили свой код, вывод сделали про чужой сервер.
>
> **Что осталось в силе:** `UNIQUE(vacancy_id, topic)` как ключ (одна вакансия
> действительно даёт несколько переписок), опциональность `resume_id` в схеме (теперь
> как защита от дрейфа разметки, а не как «атрибуция невозможна»), и запрет `--resume`
> у `reply-employers` — но уже НЕ по причине недостоверности: вопрос о scope этой
> WRITE-команды пересматривается отдельно, цена ошибки там выше, чем у аналитики.
> Реализовано в #200: `TopicRef.resume_id`, заполнение `replies.resume_id` и
> `actions.resume_id` в `record_reply_and_action`.

Исходное (историческое) рассуждение: страница `/applicant/negotiations` общая по
аккаунту; карточка не несёт достоверного признака резюме. Тест
`tests/test_responses_history.py:166`
(`test_upsert_same_vacancy_different_topics_are_distinct_rows`) буквально доказывает:
одна вакансия даёт несколько `topic` (отклик с разных резюме) → ключ `UNIQUE(vacancy_id, topic)`,
`resume_id` опционален и в ключ не входит.

**Подтверждённые противоречия, пойманные в cycle-review PR #53:**

| # | Противоречие | Где в спеке | Подтверждение кодом |
|---|--------------|-------------|---------------------|
| 1 | `reply-employers --resume` обещал scope по резюме, но account-scope игнорирует атрибуцию | §3.3 строки 285-290: спека **уже** запретила `--resume` именно поэтому | `resume_id` опционален в `responses` (history.py:38) |
| 2 | `reply-employers` TOCTOU + state machine `pending→in_flight→sent` поверх claim `(topic, inbound_message_id)` | §3.3 строки 312-343: контракт описан детально | **claim-таблицы не существует** — фундамент под state machine отсутствует |
| 3 | `clear-negotiations --vacancy` не идентифицирует единственный отклик (multi-topic) | §3.3 строки 436-457: literal `--vacancy --force` → всегда exit 1 | `test_..._different_topics_are_distinct_rows` (tests:166-193) |

**Вывод:** спека описывает write-контракты **поверх модели, которой нет**. Именно это
порождало неисчерпаемые циклы ревью.

---

## ЧАСТЬ A. Data-model для write-команд над negotiations

### A.0. Требования к модели (сводятся из #55 и спеки)

1. **Идемпотентность `reply-employers`** — атомарная заявка на ответ ДО отправки
   (claim), чтобы закрыть TOCTOU между «проверили чат» и «записали результат».
2. **State machine `pending → in_flight → sent`** с явной семантикой:
   - `pending` — pre-submit, retry-safe (чистый снимок claim при падении до обращения к hh.ru).
   - `in_flight` — post-submit, reconcilable (внешний эффект мог наступить).
   - `sent` — терминальное, подтверждено hh.ru.
   - Никакое прерванное nonterminal-состояние не висит вечно и не слепо retrится.
3. **Достоверное «нужен ли ответ»** — автор последнего сообщения + read-state из живого
   чата (live-reconciliation для `in_flight`).
4. **Атрибуция** — либо надёжная связь ответ→резюме, либо явное закрепление account-scope
   (см. §1.3: достоверной связи нет → account-scope).

### A.1. Вариант A1 — Расширить `responses` колонками (in-place)

Добавить в существующую таблицу `responses` колонки: `last_message_author` (employer/applicant),
`last_message_id`, `read_state`, `replied_at` (метка нашей отправки), `reply_state`
(pending/in_flight/sent). Claim — через partial UNIQUE-индекс по `(topic, last_message_id)
WHERE reply_state IS NOT NULL`.

**Плюсы:**
- Одна таблица, JOIN воронки/`responses` не меняется.
- Меньше DDL-площади.

**Минусы (критично):**
- **Сломает инвариант `upsert_response`.** Сегодня `upsert` перезаписывает строку при
  каждом scrape (#12). Если claim живёт в той же строке, scrape **затрёт `reply_state`**
  свежим бейджем hh.ru → потеря идемпотентности ровно там, где она нужна. Это та же
  болезнь, что у «offer в responses» (почему `manual_offers` вынесли отдельно, history.py:55-57).
- `responses` — ключ `(vacancy_id, topic)`, а claim идемпотентен по `(topic, message_id)`:
  новое сообщение от HR в том же `topic` создаёт **новый claim**, а `responses`-строка одна.
  Колонка-состояние не выражает «сколько заявок было по этому чату».
- Read-state/автор сообщения — write-time поля, засоряют read-only мониторинг.

**Риск:** высокий. Повторяет ошибку, от которой проект уже ушёл (offer → отдельная таблица).

**Влияние на код:** `upsert_response` усложняется защитой claim-колонок от затирания;
`funnel`/`dead_responses` получают лишние колонки (хотя их запросы не пострадают — они
читают `status`). Тесты `test_responses_history` нужно расширить.

**Вердикт:** **не рекомендую.** Смешение read-only scrape-данных и write-time state в
одной строке — корень противоречий #55.

### A.2. Вариант A2 — Отдельная таблица `reply_claims` (рекомендуемый)

Новая таблица в общем `SCHEMA`-блоке `history.py` (по правилу проекта — `CREATE TABLE IF
NOT EXISTS`, **без миграций**, см. CLAUDE.md / #50):

```sql
-- reply_claims — идемпотентность reply-employers (#55, write-semantics).
-- ОТДЕЛЬНО от responses (#12): responses перезаписывается каждым scrape'ом и
-- затёр бы состояние отправки; reply_claims — append-only журнал заявок на ответ,
-- keyed по (topic, inbound_message_id). topic — та же переписка, что в responses;
-- inbound_message_id — конкретное входящее сообщение HR (одно чат-окно может дать
-- несколько заявок по мере прихода новых сообщений). resume_id опционален
-- (account-scope, см. §1.3 — достоверной атрибуции к резюме нет).
CREATE TABLE IF NOT EXISTS reply_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,              -- immutable владелец аккаунта (см. A.4, аудит Codex)
    topic TEXT NOT NULL,
    inbound_message_id TEXT NOT NULL,      -- id сообщения HR, на которое отвечаем (= idempotency key)
    vacancy_id TEXT,                       -- для связи с responses/воронкой (опц.)
    resume_id TEXT,                        -- опционален, НЕ в ключе (account-scope, недостоверная атрибуция)
    reply_state TEXT NOT NULL,             -- pending | in_flight | sent
    attempt INTEGER NOT NULL DEFAULT 0,    -- retry-счётчик (audit Codex: retry с idempotency key)
    claim_created_at TEXT NOT NULL,        -- момент взятия claim (pre-submit)
    submit_started_at TEXT,                -- переход pending→in_flight
    lease_expires_at TEXT,                 -- lease/timeout зависшего in_flight (audit Codex §2)
    settled_at TEXT,                       -- переход в sent (или reconciled)
    note TEXT,                             -- причина FAIL / неоднозначный результат
    UNIQUE (account_id, topic, inbound_message_id)  -- один claim на (аккаунт, чат, сообщение)
);

CREATE INDEX IF NOT EXISTS idx_reply_claims_state
    ON reply_claims(reply_state);
-- Найти зависшие in_flight для reconciliation/lease-cleanup:
--   WHERE reply_state = 'in_flight' AND lease_expires_at < now
CREATE INDEX IF NOT EXISTS idx_reply_claims_in_flight_lease
    ON reply_claims(reply_state, lease_expires_at)
    WHERE reply_state = 'in_flight';
```

`responses` **не трогается** — остаётся read-only мониторингом. Write-semantics живут в
`reply_claims`. Связь с чатом — по `topic` (общий ключ с `responses.topic`).

**Плюсы:**
- Чистое разделение: scrape (read-only, перезаписываемый) ≠ claim (write, append-only).
  Тот же принцип, что `manual_offers` отдельно от `responses` (history.py:55).
- UNIQUE `(account_id, topic, inbound_message_id)` — физическая основа state machine из
  спеки: claim создаётся `INSERT` ДО submit (атомарно), коллизия = уже взят другим процессом.
  `inbound_message_id` одновременно служит **idempotency key** для retry (audit Codex §2).
- `reply_state` + `attempt` выражают «сколько заявок по чату и сколько retry» по строкам.
- `lease_expires_at` + partial index на `in_flight` дают готовую точку для reconciliation
  и cleanup зависших claims (см. §A.5 «Codex-audit»).
- Append-only → аудит «что мы пытались отправить и когда» бесплатен.
- Линейно ложится на методы-владельца в `history.py` (`claim_reply`, `mark_in_flight`,
  `mark_sent`, `reconcile_in_flight`, `expire_in_flight_leases`), каждый — `with self._connect()`.

**Минусы:**
- Новый `message_id`/автор должны **появиться в парсере** (`responses.py` /
  отдельный read-chat-модуль) — а селекторы negotiations **не подтверждены** (см. §1.2).
  Значит, реализация блочится на ручной сверке F12, как и непроверенные apply-селекторы.
  Это **внешний** блокер (источник данных), не модели.
- Больше таблица → больше методов в `history.py`.

**Риск:** средний, и весь он — в источнике данных (селекторы), не в модели. Модель устойчива:
если `message_id` недоступен, claim можно брать по `(topic, скользящий-маркер)` — но это
**второе** решение, которое тоже требует верификации на живой сессии.

**Влияние на код:**
- `history.py`: +1 таблица в `SCHEMA`, +4-5 методов (`claim_reply`, `mark_in_flight`,
  `mark_sent`, `reconcile_in_flight`, возможно `pending_claims_for_topic`). Существующие
  методы **не трогаются** (правило «новые методы в конец файла» уже соблюдается в history.py).
- `responses.py` или новый `negotiations_chat.py`: расширение парсера живого чата
  (автор/message_id). Это feature-ишью на сам `reply-employers`, не на модель.
- `funnel`/`dead_responses`: не затронуты.
- Тесты: новые characterization-тесты claim-state-machine без браузера
  (pending→in_flight→sent, retry-safe из pending, reconciliation из in_flight).

**Вердикт:** **рекомендую.** Разделение scrape vs claim повторяет уже принятый в проекте
паттерн (`manual_offers`), UNIQUE прямо даёт state machine, append-only даёт аудит.

### A.3. Вариант A3 — Гибрид: `reply_claims` + расширение `responses` read-полями

A2 для write-state + добавить в `responses` **только read-поля** (`last_message_author`,
`last_message_id`) через `ALTER TABLE ADD COLUMN` под идемпотентной обёрткой `_ensure_column`
(паттерн уже есть в history.py:96 для `letter_variant`, caveat #51 задокументирован).

**Плюсы:**
- Read-поля живут в `responses` (ближе к источнику — scrape чата), write-state — отдельно.
  «Нужен ли ответ» читается из `responses.last_message_author` без отдельного обхода.
- `_ensure_column` уже есть → добавление колонок идемпотентно, без пересоздания БД.

**Минусы:**
- Те же read-поля будут **перезаписываться** scrape'ом — но это **корректно** для read-полей
  (последнее сообщение меняется со временем). Главное — write-state (`reply_state`) живёт в
  `reply_claims` и scrape'ом не трогается. Так что «сломает инвариант» из A1 **не возникает**.
- Две таблицы правят одним чатом — связка по `topic`, чуть сложнее для понимания.
- Read-поля в `responses` требуют их извлечения в парсере (тот же блокер «селекторы не
  подтверждены», что и в A2).

**Риск:** средний. Чуть больше движущихся частей, но семантически чистый (read в read-таблице,
write в write-таблице).

**Влияние на код:** сумма A2 + `ALTER` на `responses`. `upsert_response` расширить
аргументами `last_message_author`/`last_message_id` (optional, по умолчанию None — обратная
совместимость с тестами).

**Вердикт:** **допустим**, если хочется «нужен ли ответ» решать одним SELECT к `responses`,
не открывая чат отдельно. Но это оптимизация; A2 достаточен.

### A.4. Атрибуция к резюме и аккаунту (отдельный подвопрос)

Достоверной связи ответ→резюме на `/applicant/negotiations` **нет** (§1.3). Поэтому:

- **Account-scope закрепить явно.** Аудит Codex (§4) усиливает рекомендацию: `account_scope`
  должен входить в ключи/foreign keys атрибуции. Поэтому в `reply_claims` `account_id`
  (**immutable** идентификатор аккаунта) — **NOT NULL и часть UNIQUE-ключа**, а не
  опциональный `resume_id`. Один текста/profile URL недостаточно: резюме меняются, `topic`
  может переиспользоваться — immutable `account_id` страхует от смешения записей разных
  аккаунтов в одной БД (сейчас проект single-account, но ключ закладывает корректность
  на будущее без стоимости).
- `resume_id` — **опционален**, НЕ в ключе (недостоверная атрибуция). Если hh.ru когда-то
  отдаст надёжный признак — дополнительно храним snapshot резюме (Codex: «надёжнее хранить
  immutable identity + snapshot», одного текущего URL мало).
- **Не** пытаться фабриковать атрибуцию (клонировать ответ под все резюме) — это нарушало
  бы принцип «не фабриковать данные», уже зафиксированный в `upsert_response`
  (history.py:268-279).

### A.5. Codex-audit: что нужно добавить к A2 (внешний аудит gpt-5.6-sol/xhigh)

Внешний аудит (Codex session `019fa440-...`) подтвердил рекомендацию A2, но указал, что
`UNIQUE + pending→in_flight→sent` — это **«хорошая база, но не полная state machine»**.
Ниже — пробелы и третий путь хранения read-state, перенесённые из аудита в модель. **Это
дополнения к A2, не смена рекомендации.**

#### A.5.1. Lease / timeout для зависших `in_flight` (audit §2, §5)

Моя исходная модель опиралась на reconciliation «следующий запуск рассосёт `in_flight`». Но
если следующий запуск не приходит (пользователь перестал пользоваться ботом), `in_flight`
висит вечно, а «зависший» claim без таймстампа нельзя отличить от «только что ушёл в полёт».

**Решение (уже в SQL A.2):** колонка `lease_expires_at` ставится при `pending→in_flight`,
равна `submit_started_at + LEASE_TTL` (напр. 1 час — больше любого разумного submit +
timeout). Partial index `WHERE reply_state='in_flight'` даёт дешёвый выборку
`lease_expires_at < now` для reconciliation. Метод `expire_in_flight_leases()` помечает
просроченные → кандидат на live-reconciliation (не авто-retry вслепую).

#### A.5.2. Retry с idempotency key (audit §2)

`inbound_message_id` = idempotency key: retry того же claim'а (после clean-fail из `pending`,
где внешнего эффекта не было) инкрементирует `attempt`, не создаёт новую строку. UNIQUE
гарантирует: второй параллельный процесс на тот же `(account, topic, message)` упрётся в
коллизию, а не отправит дубль.

#### A.5.3. Где хранить author/read-state: третий путь — `live_chat_snapshot` (audit §3)

Codex предпочитает **отдельную таблицу-снапшот** моим A3-колонкам в `responses»:

| Подход | Что хранит | Плюс | Минус |
|--------|------------|------|-------|
| Колонки в `responses` (A3) | `last_message_author`/`last_message_id` | один SELECT | перезапись scrape'ом; нет «качества источника» |
| Live-обход в момент запуска | ничего (читаем hh.ru на лету) | всегда свежие | **нет аудита** + TOCTOU-гонка (Codex §3) |
| **`live_chat_snapshot` (Codex)** | `(account_id, topic, observed_author, observed_read_state, source_quality, observed_at)` | аудит + «наблюдали в момент T» + отметка достоверности источника | +1 таблица |

`source_quality` (`verified` | `inferred` | `unknown`) — критично при **неподтверждённых
селекторах** (§1.2): если автор/`message_id` вытащен из ненадёжного селектора, метка
`inferred` не даёт модели молча поверить грязному источнику — fail-closed (§принцип проекта:
лучше пропустить чат, чем ответить не туда). Это третий подвариант хранения read-state,
альтернатива A3-колонкам; выбор между ними — за feature-ишью на `reply-employers` после
верификации селекторов.

#### A.5.4. Dual-write сбой (audit §2)

Claim и outbound-результат пишутся в две точки (claim-state в `reply_claims`, аудит в
`actions`). Сбой между ними → рассинхрон. Codex предлагает transactional outbox или явную
сверку claim/outbound. Для SQLite + single-writer проекта достаточна **одна транзакция** на
оба INSERT (в одном `with self._connect()` → один `commit`), без outbox: это закрывает
dual-write в рамках модели. Outbox — овереринжиниринг здесь (нет распределённой записи).

#### A.5.5. Edge cases (audit §5) — перенесены в требования feature-ишью

Реализация `reply-employers` обязана покрыть: повторный inbound (новый `message_id` → новый
claim, старый остаётся `sent`), reordered events, concurrent workers (UNIQUE закрывает),
удалённый/изменённый чат (live-reconciliation находит пустой чат → `sent`-候选 не
ретраится вслепую), partial provider failure, claim после отправки без локального commit.
Retention/compaction append-only `reply_claims` (и `live_chat_snapshot`, если выбран) —
`DELETE WHERE settled_at < cutoff AND reply_state='sent'` раз в N, не критично при объёмах
проекта, но должно быть задокументировано.

### A.6. Сводная таблица вариантов

| Вариант | Where | Идемпотентность | Риск | Влияние на funnel/dead | Рекомендация |
|---------|-------|-----------------|------|------------------------|--------------|
| A1 — расширить `responses` | in-place | ❌ ломается scrape'ом | высокий | нет | **нет** |
| **A2 — таблица `reply_claims`** | новая | ✅ UNIQUE + append-only | средний (в селекторах) | нет | **да (основной)** |
| A2+ (A2 + Codex-audit §A.5) | новая | ✅ + lease/timeout + idempotency key (`attempt`) + `account_id` в ключе | средний (в селекторах) | нет | **да (с дополнениями)** |
| A3 — A2 + read-поля в `responses` | гибрид | ✅ | средний | нет | допустим |
| read-state: `live_chat_snapshot` (Codex §A.5.3) | новая (для author/read-state) | ✅ + `source_quality` для fail-closed при грязном селекторе | средний (в селекторах) | нет | альтернатива A3-колонкам |

**Блокер реализации (общий для A2/A3):** селекторы negotiations **не подтверждены** (§1.2).
До первого боевого входа и сверки F12 мы не знаем, отдаёт ли hh.ru `message_id`/автора на
странице чата. Это не модельный вопрос, но он определяет, реализуем ли claim по
`(topic, message_id)` (A2/A3) или по скользящему маркеру. **Решение по модели можно принять
до верификации** (A2 устойчив к обоим случаям), но реализация команд ждёт сверки.

---

## ЧАСТЬ B. Граница «эталон интерфейса» vs «дизайн реализации» в cli-spec.md

### B.1. Принцип разделения

`docs/cli-spec.md` — **эталон интерфейса** (на него ссылаются feature-ишью). Эталон должен
фиксировать высокоуровневый контракт, **стабильный** между реализациями:

- **Оставить в эталоне:** имена команд, сигнатуры (флаги), природа READ/WRITE,
  формат вывода, safety-границы (scope = account-wide; боевой режим требует подтверждения;
  идемпотентность обязательна; `--vacancy --force` всегда exit 1).
- **Вынести в feature-ишью (дизайн реализации):** state machine и переходы состояний,
  reconciliation-логику, literal fail-closed условия, выбор data-model (A1/A2/A3).

Причина: эталон описывает **что** команда гарантирует пользователю; state machine и
claim-таблица — **как** это достигается. Детали реализации меняются (именно так родились 4
цикла ревью — спека описала «как», а «как» ещё не определено).

### B.2. Что вынести из текущей спеки (~180 строк write-контрактов)

Сейчас §3.3 содержит детальные write-контракты для трёх команд (для `reply-employers` —
строки 303-348, почти 50 строк на state machine/TOCTOU; для `clear-negotiations` — строки
436-475, ~40 строк на literal-контракт; плюс `clone-resume` строки 355-384).

**Предлагаемый срез** — оставить high-level, удалить реализационные детали, дать ссылку на
этот design-док.

#### B.2.1. `reply-employers` — что оставить / что вынести

**Оставить в эталоне (high-level):**
- Природа WRITE-hh-ru, сигнатура `reply-employers [--dry-run] [--limit <n>] [--template <text>] --force`.
- Scope = **account-wide** (`--resume` не принимается — недостоверная атрибуция, см. #55 §1.3).
- Боевой режим требует подтверждения (`--force`/prompt, единый контракт §1).
- Семантика: отвечает в чатах, где последнее сообщение от работодателя и ответ ещё не отправлен.
- **Safety-контракт (эталонный, без реализации):** идемпотентность обязательна — боевой
  режим не должен отправлять повторный ответ в чат, где уже ответили; конкурентность
  (параллельные/ручной+плановый запуск) обрабатывается fail-closed (лучше пропустить чат,
  чем ответить повторно). **Детали idempotency/state machine — feature-ишью, см. [#55 ЧАСТЬ A].**

**Вынести в feature-ишью на `reply-employers`:**
- Весь блок «Idempotency / безопасность» (строки 303-348): state machine `pending→in_flight→sent`,
  TOCTOU-анализ, live-reconciliation из `in_flight`, dual-write-сбой, сериализация по topic.
- Выбор модели (A2 `reply_claims` или A3) — после решения пользователя по этому доку.

#### B.2.2. `clear-negotiations` — что оставить / что вынести

**Оставить в эталоне (high-level):**
- Природа WRITE-hh-ru (деструктивная), сигнатура с `--topic`/`--vacancy`/`--resume`/`--account-wide`/`--force`.
- Боевой режим требует подтверждения (`--force`/prompt).
- **Safety-граница (эталонная):** боевой отзыв идентифицируется **только** по `--topic`
  (или явным `--account-wide`); `--vacancy`/`--resume` — только dry-run-план. **Конкретные
  условия fail-closed (literal `--vacancy --force` → всегда exit 1) — feature-ишью, см. [#55].**
- Аудит: каждая успешная операция → `actions` (`action='withdraw'`, `status='success'`).

**Вынести в feature-ишью на `clear-negotiations`:**
- Literal-контракт «`--vacancy --force` → всегда exit 1 независимо от числа совпадений»
  (строки 444-451) с обоснованием «число переписок меняется во времени».
- Логику «отзыв по `--topic` → точная идентификация единственной переписки через `topic`
  из `chat_url`».

#### B.2.3. `clone-resume` — оставить как есть

`clone-resume` (строки 355-384) — high-level по сути: нет state machine, нет idempotency
выше троттлинга, аудит в `actions`. Срез не требуется; только убрать перекрёстные ссылки
на «единый контракт state machine», если такие есть (их нет). **Оставить.**

### B.3. Diff-план среза cli-spec.md (конкретные строки)

> Это **план рефакторинга спеки**, а не сам рефакторинг. Применяется отдельным
> feature-ишью после решения по ЧАСТИ A.

| Действие | Где (cli-spec.md) | Во что |
|----------|-------------------|--------|
| Сжать блок `reply-employers` «Idempotency» | строки 303-348 (~46 строк) | ~8 строк: safety-контракт + ссылка «детали state machine/idempotency — feature-ишью, модель в [#55 ЧАСТЬ A]» |
| Сжать блок `clear-negotiations` граница | строки 436-457 (~22 строки) | ~6 строк: safety-граница `--topic`/`--account-wide` + ссылка «literal fail-closed — feature-ишью, см. [#55]» |
| Оставить `clone-resume` | строки 355-384 | без изменений |
| Добавить в §3.3 преамбулу | перед строкой 110 | 2 строки: «write-контракты команд приведены high-level; idempotency/state machine/reconciliation — design-решение [#55], реализуется feature-ишью» |
| **Итог** | ~180 строк детальных write-контрактов | ~+16 строк high-level + 3 ссылки на #55 |

Чистый эффект: эталон теряет ~160 строк реализационных деталей, получает явную ссылку на
design-док. Противоречия «write-контракт vs read-only-модель» больше не живут в спеке —
они перенесены туда, где им место (в data-model-решение + feature-ишью).

### B.4. Что НЕ выносить (остаётся в эталоне как стабильный контракт)

Эти пункты **стабильны** (не зависят от выбора модели) и должны остаться в спеке:

- READ/WRITE-маркировка всех команд (§1, §3.1).
- Единый контракт подтверждения `--force`/prompt для трёх опасных команд (§1, строки 27-37).
- Account-scope `reply-employers`/`clear-negotiations` (запрет `--resume` в бою).
- Формат вывода (§2) — текст/ASCII, без эмодзи.
- write-hh-ru через **браузер**, не через OAuth API (строки 263-270) — это
  архитектурное ограничение (нет OAuth-токена), стабильно.

---

## Рекомендация (для решения пользователем)

**ЧАСТЬ A — Вариант A2+ (отдельная таблица `reply_claims` + дополнения Codex-аудита §A.5).**
- Чистое разделение read-only scrape (`responses`) и write state (`reply_claims`) —
  повторяет уже принятый паттерн `manual_offers`. Подтверждено внешним аудитом (Codex).
- UNIQUE `(account_id, topic, inbound_message_id)`, где `inbound_message_id` = idempotency
  key, а `account_id` immutable в ключе — физическая основа state machine из спеки.
- Append-only даёт аудит бесплатно.
- Дополнения §A.5 (Codex): `lease_expires_at` + partial index для cleanup зависших
  `in_flight`; `attempt` для retry с idempotency key; одна транзакция на dual-write
  claim+audit (без outbox — достаточно SQLite single-writer).
- Хранение author/read-state: на выбор feature-ишью — A3-колонки в `responses` **или**
  отдельная `live_chat_snapshot` с `source_quality` (Codex §A.5.3; последний предпочтительнее
  при неподтверждённых селекторах, т.к. fail-closed по качеству источника).
- `resume_id` опционален, account-scope закреплён явно через immutable `account_id` (§A.4).
- **Блокер реализации** (общий): селекторы negotiations не подтверждены — модель можно
  принять сейчас, реализация команд ждёт ручной сверки F12.

**ЧАСТЬ B — Срез §3.3 по плану B.3.**
- Оставить high-level safety-контракты (scope, подтверждение, идемпотентность обязательна).
- Вынести state machine / TOCTOU / literal fail-closed / reconciliation в feature-ишью
  со ссылкой на этот design-док.
- Чистый эффект: ~−160 строк реализационных деталей из эталона.

**После решения пользователя — разбить на sub-issues:**
1. Расширение `SCHEMA`: таблица `reply_claims` (A2+, с `lease_expires_at`/`attempt`/
   `account_id`) + методы `claim_reply`/`mark_in_flight`/`mark_sent`/`reconcile_in_flight`/
   `expire_in_flight_leases` + characterization-тесты state machine (включая lease/timeout,
   idempotency-retry, dual-write в одной транзакции).
2. (Опционально, на выбор feature-ишью) `live_chat_snapshot` с `source_quality` ИЛИ
   read-колонки в `responses` (A3) — после верификации селекторов.
3. Рефакторинг `docs/cli-spec.md` §3.3 по плану B.3 (срез write-контрактов).
4. Реализация `reply-employers` (после верификации селекторов negotiations) поверх `reply_claims`.
5. Реализация `clear-negotiations` (literal fail-closed по `--topic`).

---

_Это design-док для ишью [#55](https://github.com/axisrow/hhru/issues/55). Решение по
ЧАСТИ A и B принимает пользователь. Документ ничего не закрывает._

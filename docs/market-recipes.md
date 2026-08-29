# Рецепты рынка вакансий (#66)

READ-only анализ рынка с целью **максимизация дохода** — не «сколько вакансий в
моей сфере», а **сравнение сфер по медианной зарплате** и подсветка выгодных
направлений (Этап 1 roadmap #65).

## Откуда данные

Команда `search` при каждом запуске пишет собранные карточки вакансий в таблицу
`vacancies_seen` (побочный эффект сбора, не трогает отбор/скоринг/вывод).
Поля: `vacancy_id`, `title`, `company`, `salary_from`, `salary_to`,
`salary_currency`, `search_query` (по какому тексту найдено),
`first_seen_at`, `last_seen_at`. Ключ `UNIQUE(vacancy_id, search_query)` — одна
вакансия по разным запросам хранится отдельными строками. Свежесть вакансии =
`first_seen_at`/`last_seen_at` — дату публикации hh.ru на карточке поиска не
отдаёт ни под каким `data-qa` (#117).

Сначала **соберите данные** по нескольким сферам — прогоните search с разными
`text` в `config.yaml` (или несколькими резюме). Для именованного аккаунта
конфиг и история автоматически берутся из `data/accounts/<name>/`:

```
./scripts/run.sh search --resume <id> --max-pages 5
./scripts/run.sh --account marketing search --resume <id> --max-pages 5
```

Дальше — всё через `query` (read-only SELECT к `history.db`):

```
./scripts/run.sh query "<SQL отсюда>"
./scripts/run.sh query "<SQL>" --csv   # машинный экспорт
./scripts/run.sh query "<SQL>" -o out.csv
```

`--config`/`--history` — глобальные флаги (до подкоманды), по умолчанию
`data/config.yaml` и `data/history.db`:

```
./scripts/run.sh --history data/history.db query "<SQL>"
```

Для отдельного аккаунта достаточно одного флага (он также должен стоять до
подкоманды):

```
./scripts/run.sh --account marketing query "<SQL отсюда>"
./scripts/run.sh --account marketing query "<SQL>" --csv
```

Аккаунт `marketing` должен иметь файл `data/accounts/marketing/config.yaml`;
его история хранится в `data/accounts/marketing/history.db`. Явно переданные
`--config` и `--history` имеют приоритет над соответствующими путями аккаунта.

Все запросы ниже — `SELECT`/`WITH`, безопасны (read-only).

> Зарплата. Вилка на hh.ru часто односторонняя: `salary_to` (верхняя граница или
> фикс. значение) пуст у вакансий «от N», а `salary_from` пуст у «до N».
> Поэтому медиан **две** — по нижней границе и по верхней, каждая со своим `n`
> (#125). Считать одну медиану только по `salary_to` нельзя: вакансии «от N»
> выпадают из расчёта целиком (до 28% выборки, смещение до 20%).
> `salary_currency` НЕ нормализована — сравнивайте сферы в одной валюте (обычно
> внутри одного `search_query` она однородна). Поэтому каждый запрос ниже либо
> группирует результат по `salary_currency`, либо явно выбирает одну валюту.
> Нельзя считать `AVG`/медиану по `salary_from` или `salary_to` без этого
> ограничения: сумма в RUB, USD, EUR и KZT — разные шкалы.

> Чего в этих рецептах намеренно НЕТ. `COALESCE(salary_to, salary_from)` в один
> ряд — смешение разных величин: «от 300 000» и «до 300 000» это не одно и то же
> число. Середина вилки `(from + to) / 2` у односторонних вакансий — выдуманные
> данные: второй границы там не существует.

> Медиана «от» может быть ВЫШЕ медианы «до» — это не ошибка. Их считают разные
> подмножества вакансий: если щедрые работодатели пишут «от 900 000» без потолка,
> а полную вилку указывают те, кто платит меньше, нижняя медиана честно окажется
> выше верхней. Это два независимых среза, а не границы одного интервала.

## 1. Медианы зарплаты по сфере (главный запрос)

Сравнение сфер по доходу — выгодные наверху. Две медианы дают коридор
(«от 150 000 до 250 000»), который информативнее одной цифры:

```
SELECT
    search_query AS сфера,
    salary_currency AS валюта,
    -- медиана нижних границ: среднее двух центральных значений
    COALESCE((
        SELECT AVG(salary_from) FROM (
            SELECT salary_from, ROW_NUMBER() OVER (ORDER BY salary_from) AS rn,
                   COUNT(*) OVER () AS total
            FROM vacancies_seen
            WHERE search_query = v.search_query
              AND salary_currency IS v.salary_currency
              AND salary_from IS NOT NULL
        ) WHERE rn IN ((total + 1) / 2, (total + 2) / 2)
    ), 0) AS медиана_от,
    COUNT(salary_from) AS n_от,
    -- медиана верхних границ, тем же приёмом
    COALESCE((
        SELECT AVG(salary_to) FROM (
            SELECT salary_to, ROW_NUMBER() OVER (ORDER BY salary_to) AS rn,
                   COUNT(*) OVER () AS total
            FROM vacancies_seen
            WHERE search_query = v.search_query
              AND salary_currency IS v.salary_currency
              AND salary_to IS NOT NULL
        ) WHERE rn IN ((total + 1) / 2, (total + 2) / 2)
    ), 0) AS медиана_до,
    COUNT(salary_to) AS n_до,
    COUNT(*) AS вакансий
FROM vacancies_seen AS v
WHERE salary_currency IS NOT NULL
GROUP BY search_query, salary_currency
ORDER BY медиана_до DESC, вакансий DESC;
```

Здесь каждая строка — отдельная пара «сфера + валюта». Если нужен только один
рынок, добавьте, например, `WHERE salary_currency = 'RUB'` во внешний запрос и
замените `IS v.salary_currency` на тот же явный фильтр во вложенных запросах.

`n_от` / `n_до` — coverage каждой медианы по отдельности: выборки у них разные,
и если `n` мал (в отчёте порог 5), медиана шаткая. Смотрите на `n` рядом с
цифрой, а не на общее число вакансий: сфера на `n=2` может «обогнать» реальную
только из-за размера выборки.

## 2. Сравнение «Python vs performance vs data engineer»

Те же данные, отфильтрованные по конкретным сферам:

```
SELECT search_query AS сфера, salary_currency AS валюта, COUNT(*) AS n,
       ROUND(AVG(salary_to)) AS средняя_to,
       MIN(salary_to) AS минимум, MAX(salary_to) AS максимум
FROM vacancies_seen
WHERE search_query IN ('python', 'performance', 'data engineer')
  AND salary_currency = 'RUB'
  AND salary_to IS NOT NULL
GROUP BY search_query, salary_currency
ORDER BY средняя_to DESC;
```

## 3. Топ работодателей по сфере (кого массово нанимают)

```
SELECT company AS работодатель, salary_currency AS валюта, COUNT(*) AS вакансий,
       ROUND(AVG(salary_to)) AS средняя_to
FROM vacancies_seen
WHERE search_query = 'python'
  AND salary_currency = 'RUB'
  AND salary_to IS NOT NULL
GROUP BY company, salary_currency
ORDER BY вакансий DESC
LIMIT 20;
```

## 4. Распределение зарплат (где «потолок» сферы)

Сколько вакансий в каждом диапазоне:

```
SELECT
    CASE
        WHEN salary_to < 100000 THEN 'до 100к'
        WHEN salary_to < 200000 THEN '100-200к'
        WHEN salary_to < 300000 THEN '200-300к'
        WHEN salary_to < 500000 THEN '300-500к'
        ELSE '500к+'
    END AS диапазон,
    COUNT(*) AS вакансий
FROM vacancies_seen
WHERE search_query = 'python'
  AND salary_currency = 'RUB'
  AND salary_to IS NOT NULL
GROUP BY диапазон
ORDER BY MIN(salary_to);
```

## 5. Доля вакансий без зарплаты по сфере

Где рынок «темнит» с вилкой (мало данных → сфера непрозрачная):

```
SELECT search_query AS сфера,
       COUNT(*) AS всего,
       COUNT(salary_to) AS с_зп,
       ROUND(100.0 * (COUNT(*) - COUNT(salary_to)) / COUNT(*), 1) AS pct_без_зп
FROM vacancies_seen
GROUP BY search_query
ORDER BY pct_без_зп DESC;
```

## 6. Свежесть данных (когда последний раз скрейпили сферу)

```
SELECT search_query AS сфера,
       COUNT(*) AS вакансий,
       MAX(last_seen_at) AS последний_сбор
FROM vacancies_seen
GROUP BY search_query
ORDER BY последний_сбор DESC;
```

## 7. Разбор отказов (за что отказывают, по работодателю и вилке ЗП)

В отличие от рецептов 1-6 это не голый SQL к `vacancies_seen` — отказ живёт в
`responses`/`actions` (история переписки с работодателем, не карточка поиска),
и связка полей SQL-агрегата дублировать не стоит: она уже реализована как
готовая команда `funnel --rejections` (#709). Каждая строка — работодатель +
поисковый запрос + вилка зарплаты вакансии, по которой пришёл отказ:

```
./scripts/run.sh funnel --rejections
./scripts/run.sh funnel --rejections --period 0        # за всё время, не 30 дней
./scripts/run.sh funnel --rejections --resume <id>      # отказы по одному резюме
./scripts/run.sh funnel --rejections --format md        # markdown вместо ASCII-таблицы
```

Пустые `Поисковый запрос`/`ЗП от`/`ЗП до` — отказ по вакансии, которую не
собирала команда `search` (нет карточки в `vacancies_seen`); это не ошибка,
а честное отсутствие данных для сопоставления. Реальный вывод (read-only,
собрано 2026-08-29):

```
+-------------------+------------------+--------+--------+--------+---------+
| Работодатель      | Поисковый запрос | ЗП от  | ЗП до  | Валюта | Отказов |
+-------------------+------------------+--------+--------+--------+---------+
| AVS Agency        | python backend   | 30000  | 45000  | RUB    | 1       |
| Anecole           |                  |        |        |        | 1       |
| CVisionLab        | python backend   |        |        |        | 1       |
| KTS               | python backend   |        |        |        | 1       |
| Maxima.tech       | python backend   |        |        |        | 1       |
| Sapiens solutions | python backend   |        |        |        | 1       |
| Тензор            | python           | 150000 | 250000 | RUB    | 1       |
| Тензор            | python           | 150000 | 260000 | RUB    | 1       |
| Тензор            | python backend   | 150000 | 250000 | RUB    | 1       |
| Тензор            | python backend   | 150000 | 260000 | RUB    | 1       |
+-------------------+------------------+--------+--------+--------+---------+
```

Полезно смотреть рядом с обычной воронкой (`funnel` без флагов) — доля отказов
относительно всех откликов и особенно относительно `--dead` («мёртвая зона»)
подсказывает, стоит ли переформулировать резюме под сферу с наибольшим числом
отказов, или сфера просто слишком конкурентна на этой вилке зарплаты.

## Программный вывод (ASCII-таблица)

Для красивого вывода вилки ЗП без ручного SQL — `History.market_salary_by_query()`
возвращает строки {search_query, median_from, median_to, with_from, with_to,
count, with_salary, currency, other_currency, estimated, low_sample}, а
`report_market.market_summary(rows)` рендерит ASCII-таблицу (без эмодзи,
переиспользует `report._ascii_table`). Это и есть команда `market`.

Отличия от голого SQL выше (то, ради чего стоит брать API, а не рецепт):

- медианы считаются только по **доминирующей валюте** сферы, остальные вакансии
  выносятся в `other_currency` и сноску (#122);
- `with_salary` — покрытие по **любой** границе (вакансия «от N» это данные);
- `low_sample` — сфера, где реальных ЗП меньше 5: помечается `!` и уходит вниз
  таблицы, чтобы строка на двух вакансиях не читалась как лидер рынка;
- `estimated` (#93) — в `median_to` вошли эвристические оценки, помечается `~`.
  Оценка строится на `salary_to`, то есть это оценка **верхней** границы, и
  `median_from` она не трогает: подмешать её туда значило бы выдать верхнюю
  границу за нижнюю.

## Эталонные цифры (read-only, собрано 2026-08-01, 518 карточек)

Для валидации рецептов — реальные медианы на эту дату (только RUB):

| Сфера | n «от» | медиана «от» | n «до» | медиана «до» |
|---|---|---|---|---|
| ai engineer | 33 | 150 000 | 31 | 250 000 |
| llm engineer | 21 | 170 000 | 15 | 200 000 |
| claude code | 38 | 110 000 | 36 | 180 000 |
| python | 47 | 100 000 | 42 | 170 000 |

Рецепты должны давать сопоставимые цифры при тех же `search_query`.

## Каталог `scripts/queries/`

Место для собственных именованных SQL-запросов к `history.db`, которые не
входят в рецепты выше (специфичные фильтры под свой поиск работы, ad-hoc
разведка). Каталог **целиком в `.gitignore`** (см. комментарий в `.gitignore`)
— как и `data/`, это личные пользовательские артефакты, версионировать их не
нужно; репозиторий не содержит и не ожидает никаких файлов внутри.

Формат файла: комментарий-назначение сверху (`--`, обычный SQL-комментарий —
что запрос ищет и чего НЕ ловит), затем сам SELECT. Пример из практики
(`mature_ai_employers.sql` — компании с инженерной зрелостью в AI, а не просто
«умеем пользоваться ChatGPT» в вакансии):

```sql
-- Компании с инженерной зрелостью в AI: строят собственную обвязку,
-- различают уровни абстракции, считают экономику инференса.
-- Не ловит "умею пользоваться ChatGPT" (маркетологи/ассистенты).
with v as (
  select distinct vacancy_id, title, company, experience, ...
  from vacancies_seen
)
select company, title, zp, experience, r, rq, rs from v
where (t like '%harness%' or t like '%guardrail%' or ...)
  and (t like '%python%' or t like '%llm%' or ...)
order by company;
```

Запускается так же, как и рецепты выше — через `query`, содержимое файла
подставляется вместо `<SQL>`. `query` принимает только SELECT/WITH first token
(read-only guard, #45), поэтому ведущий комментарий-назначение нужно отрезать
перед подстановкой:

```
./scripts/run.sh query "$(grep -v '^--' scripts/queries/mature_ai_employers.sql)"
```

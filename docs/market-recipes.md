# Рецепты рынка вакансий (#66)

READ-only анализ рынка с целью **максимизация дохода** — не «сколько вакансий в
моей сфере», а **сравнение сфер по медианной зарплате** и подсветка выгодных
направлений (Этап 1 roadmap #65).

## Откуда данные

Команда `search` при каждом запуске пишет собранные карточки вакансий в таблицу
`vacancies_seen` (побочный эффект сбора, не трогает отбор/скоринг/вывод).
Поля: `vacancy_id`, `title`, `company`, `salary_from`, `salary_to`,
`salary_currency`, `raw_date`, `search_query` (по какому тексту найдено),
`first_seen_at`, `last_seen_at`. Ключ `UNIQUE(vacancy_id, search_query)` — одна
вакансия по разным запросам хранится отдельными строками.

Сначала **соберите данные** по нескольким сферам — прогоните search с разными
`text` в `config.yaml` (или несколькими резюме):

```
./scripts/run.sh search --resume <id> --max-pages 5
```

Дальше — всё через `query` (read-only SELECT к `history.db`):

```
./scripts/run.sh query "<SQL отсюда>"
./scripts/run.sh query "<SQL>" --csv   # машинный экспорт
./scripts/run.sh query "<SQL>" -o out.csv
```

`--config`/`--history` — глобальные флаги (до подкоманды), по умолчанию
`config/config.yaml` и `data/history.db`:

```
./scripts/run.sh --history data/history.db query "<SQL>"
```

Все запросы ниже — `SELECT`/`WITH`, безопасны (read-only).

> Зарплата. `salary_to` — верхняя граница вилки или фикс. значение; в вакансиях
> «от N» это NULL. Медиана ниже считается по `salary_to` (потолок предложения).
> `salary_currency` НЕ нормализована — сравнивайте сферы в одной валюте (обычно
> внутри одного `search_query` она однородна).

## 1. Медиана зарплаты по сфере (главный запрос)

Сравнение сфер по доходу — выгодные наверху:

```
SELECT
    search_query AS сфера,
    -- медиана по salary_to: среднее двух центральных значений
    COALESCE((
        SELECT AVG(salary_to) FROM (
            SELECT salary_to, ROW_NUMBER() OVER (ORDER BY salary_to) AS rn,
                   COUNT(*) OVER () AS total
            FROM vacancies_seen
            WHERE search_query = v.search_query AND salary_to IS NOT NULL
        ) WHERE rn IN ((total + 1) / 2, (total + 2) / 2)
    ), 0) AS медиана_to,
    COUNT(*) AS вакансий,
    COUNT(salary_to) AS с_зп
FROM vacancies_seen AS v
GROUP BY search_query
ORDER BY медиана_to DESC, вакансий DESC;
```

`с_зп` (vacancies with salary) — coverage: если их мало, медиана шаткая.

## 2. Сравнение «Python vs performance vs data engineer»

Те же данные, отфильтрованные по конкретным сферам:

```
SELECT search_query AS сфера, COUNT(*) AS n,
       ROUND(AVG(salary_to)) AS средняя_to,
       MIN(salary_to) AS минимум, MAX(salary_to) AS максимум
FROM vacancies_seen
WHERE search_query IN ('python', 'performance', 'data engineer')
  AND salary_to IS NOT NULL
GROUP BY search_query
ORDER BY средняя_to DESC;
```

## 3. Топ работодателей по сфере (кого массово нанимают)

```
SELECT company AS работодатель, COUNT(*) AS вакансий,
       ROUND(AVG(salary_to)) AS средняя_to
FROM vacancies_seen
WHERE search_query = 'python' AND salary_to IS NOT NULL
GROUP BY company
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
WHERE search_query = 'python' AND salary_to IS NOT NULL
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

## Программный вывод (ASCII-таблица)

Для красивого вывода вилки ЗП без ручного SQL — `History.market_salary_by_query()`
возвращает строки {search_query, median_to, count, with_salary}, а
`report_market.market_summary(rows)` рендерит ASCII-таблицу (без эмодзи,
переиспользует `report._ascii_table`). Пример использования — будущая команда
`market` или любой скрипт поверх API.

## Эталонные цифры (read-only, собрано 2026-07-28)

Для валидации рецептов — реальные медианы по `salary_to` на эту дату:

- Python backend — ~300 000 RUB
- LLM engineer — ~250 000 RUB
- performance маркетолог — ~150 000 RUB

Рецепты должны давать сопоставимые цифры при тех же `search_query`.

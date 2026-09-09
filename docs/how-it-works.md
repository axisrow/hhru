# Как это устроено

- `config.py` — загрузка `data/config.yaml`.
- `history.py` — локальная SQLite-история (`data/history.db`).
- `throttle.py` — случайные паузы и дневные лимиты.
- `browser.py`/`auth.py` — запуск Playwright и вход в аккаунт.
- `search.py` — поиск вакансий и фильтрация.
- `apply.py` — отклик с сопроводительным письмом.
- `bump.py` — поднятие резюме.
- `selectors.py` — все CSS/data-qa селекторы hh.ru в одном месте.

Всё — в `src/hhru_bot/`.


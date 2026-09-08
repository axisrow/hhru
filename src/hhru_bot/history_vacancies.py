"""Вакансии_seen: учёт увиденных карточек и возраста выдачи (#1035).

Выделено из ``history.py`` механически.
"""

from __future__ import annotations

from datetime import datetime


class VacanciesMixin:
    def upsert_vacancy_seen(
        self,
        vacancy_id: str,
        search_query: str,
        title: str | None = None,
        company: str | None = None,
        salary_from: int | None = None,
        salary_to: int | None = None,
        salary_currency: str | None = None,
        employer_tier: str | None = None,
        vacancy_text: str | None = None,
        published_at: str | None = None,
        address: str | None = None,
        is_remote: bool | None = None,
        experience: str | None = None,
        snippet_requirement: str | None = None,
        snippet_responsibility: str | None = None,
        side_job: bool | None = None,
        no_resume: bool | None = None,
        activity: str | None = None,
        hh_rating: str | None = None,
        hrbrand_winner: bool | None = None,
        metro_stations: str | None = None,
    ) -> None:
        """Записывает/освежает карточку вакансии по (vacancy_id, search_query).

        Ключ UNIQUE(vacancy_id, search_query): одна вакансия по разным поисковым
        запросам — отдельные строки (рынок хочет видеть, по каким запросам что
        находится и за сколько). При повторном scrape та же пара обновляет
        title/company/salary (hh.ru мог поменять вилку) и двигает
        ``last_seen_at``; ``first_seen_at`` хранит ПЕРВОЕ появление и не трогается.
        Пустое значение не затирает ранее подтверждённое: это позволяет
        пережить дрейф/временный сбой селектора без потери истории.

        Зарплата приходит из ``SalaryInfo`` (#34): ``salary_from``/``salary_to``
        оба NULL = «з/п не указана» (``parse_salary`` вернул None) — такая
        вакансия тоже пишется, для подсчёта доли рынка без зарплаты. Валюта НЕ
        нормализуется в одну: медиана считается в рамках одного search_query
        (внутри сферы валюта обычно однородна).

        ``employer_tier`` (#93) — уровень известности работодателя
        (``classify_employer``: top_tech/big_corp/mid/unknown). Записывается при
        сборе для группировки медианы в ``estimate_salary``. При обновлении
        существующей строки tier тоже освежается (компания могла накопить
        отзывов между scrape'ами; trusted-бейдж hh.ru на tier не влияет — #118).

        ``published_at`` — дата публикации вакансии на hh.ru, неизменна по
        своей природе. Селектор для неё опционален (см. ``selector_groups/
        search_page.py``), поэтому при повторном scrape без даты уже известное
        значение сохраняется (``COALESCE``), а не затирается NULL'ом.

        ``address``/``experience``/``snippet_requirement``/
        ``snippet_responsibility`` (#517) — доп. признаки карточки для
        статистики/ML. В отличие от title/company/salary эти блоки НЕ
        рендерятся гарантированно (опциональны в разметке hh.ru): пустое
        значение неотличимо от «карточка реально не отдала блок при этом
        конкретном scrape» (транзиентный DOM-промах). Поэтому, как и
        ``published_at``, освежаются через ``COALESCE`` — новое непустое
        значение перезаписывает старое, а пропуск при повторном scrape НЕ
        затирает ранее собранные данные NULL'ом.

        ``is_remote`` — тристейтный сигнал: ``True`` и ``False`` — наблюдения,
        ``None`` — селектор не дал наблюдения. Поэтому ``None`` не затирает
        сохранённое значение, а явный ``False`` всё ещё может обновить
        ``True``.

        ``side_job``/``no_resume`` — тристейтные сигналы для опциональных
        бейджей карточки. ``None`` означает «наблюдения нет» и сохраняет
        ранее известное значение; ``True``/``False`` — наблюдения.

        Для всех полей действует консервативная политика «best known»: NULL и
        пустые строки из нового scrape не удаляют подтверждённые данные. Это
        предотвращает массовую потерю истории при дрейфе селектора (#532).
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            # INSERT ... ON CONFLICT DO UPDATE: атомарный upsert по
            # UNIQUE(vacancy_id, search_query). first_seen_at — из исходной
            # строки (excluded.first_seen_at = текущий now, но ON CONFLICT
            # перезаписывает только перечисленные поля, first_seen_at не трогаем).
            conn.execute(
                """
                INSERT INTO vacancies_seen
                    (vacancy_id, title, company, salary_from, salary_to,
                     salary_currency, search_query, first_seen_at,
                     last_seen_at, employer_tier, vacancy_text, published_at,
                     address, is_remote, experience, snippet_requirement,
                     snippet_responsibility, side_job, no_resume, activity,
                     hh_rating, hrbrand_winner, metro_stations)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vacancy_id, search_query) DO UPDATE SET
                    title = COALESCE(NULLIF(excluded.title, ''), title),
                    company = COALESCE(NULLIF(excluded.company, ''), company),
                    salary_from = CASE
                        WHEN excluded.salary_from IS NOT NULL
                          OR excluded.salary_to IS NOT NULL
                          OR NULLIF(excluded.salary_currency, '') IS NOT NULL
                        THEN excluded.salary_from
                        ELSE salary_from
                    END,
                    salary_to = CASE
                        WHEN excluded.salary_from IS NOT NULL
                          OR excluded.salary_to IS NOT NULL
                          OR NULLIF(excluded.salary_currency, '') IS NOT NULL
                        THEN excluded.salary_to
                        ELSE salary_to
                    END,
                    salary_currency = CASE
                        WHEN excluded.salary_from IS NOT NULL
                          OR excluded.salary_to IS NOT NULL
                          OR NULLIF(excluded.salary_currency, '') IS NOT NULL
                        THEN excluded.salary_currency
                        ELSE salary_currency
                    END,
                    employer_tier = COALESCE(
                        NULLIF(excluded.employer_tier, ''), employer_tier
                    ),
                    vacancy_text = COALESCE(
                        NULLIF(excluded.vacancy_text, ''), vacancy_text
                    ),
                    published_at = COALESCE(
                        NULLIF(excluded.published_at, ''), published_at
                    ),
                    address = COALESCE(NULLIF(excluded.address, ''), address),
                    is_remote = COALESCE(excluded.is_remote, is_remote),
                    experience = COALESCE(NULLIF(excluded.experience, ''), experience),
                    snippet_requirement = COALESCE(
                        NULLIF(excluded.snippet_requirement, ''), snippet_requirement
                    ),
                    snippet_responsibility = COALESCE(
                        NULLIF(excluded.snippet_responsibility, ''), snippet_responsibility
                    ),
                    side_job = COALESCE(excluded.side_job, side_job),
                    no_resume = COALESCE(excluded.no_resume, no_resume),
                    activity = COALESCE(NULLIF(excluded.activity, ''), activity),
                    hh_rating = COALESCE(NULLIF(excluded.hh_rating, ''), hh_rating),
                    hrbrand_winner = COALESCE(excluded.hrbrand_winner, hrbrand_winner),
                    metro_stations = COALESCE(NULLIF(excluded.metro_stations, ''), metro_stations),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    vacancy_id,
                    title,
                    company,
                    salary_from,
                    salary_to,
                    salary_currency,
                    search_query,
                    now,
                    now,
                    employer_tier,
                    vacancy_text,
                    published_at,
                    address,
                    None if is_remote is None else int(is_remote),
                    experience,
                    snippet_requirement,
                    snippet_responsibility,
                    None if side_job is None else int(side_job),
                    None if no_resume is None else int(no_resume),
                    activity,
                    hh_rating,
                    None if hrbrand_winner is None else int(hrbrand_winner),
                    metro_stations,
                ),
            )

    def list_vacancies_seen(self) -> list[dict]:
        """Все собранные вакансии, свежие первыми (по last_seen_at).

        Для диагностики и прямого SELECT из query (#45). Возвращает словари со
        всеми колонками таблицы.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT vacancy_id, title, company, salary_from, salary_to, salary_currency, "
                "search_query, first_seen_at, last_seen_at, employer_tier, vacancy_text, "
                "published_at, address, is_remote, experience, snippet_requirement, "
                "snippet_responsibility, side_job, no_resume, activity, hh_rating, "
                "hrbrand_winner, metro_stations "
                "FROM vacancies_seen ORDER BY last_seen_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_vacancy_texts(self) -> list[str]:
        """Возвращает непустые тексты собранных вакансий для read-only отчётов."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT MAX(vacancy_text) AS vacancy_text FROM vacancies_seen "
                "WHERE vacancy_text IS NOT NULL AND vacancy_text != '' "
                "GROUP BY vacancy_id"
            ).fetchall()
        return [row["vacancy_text"] for row in rows]

    def vacancy_age_distribution(self, now: datetime | None = None) -> dict[str, int]:
        """Count observed vacancies by age of hh.ru publication date.

        UNIQUE-индекс — (vacancy_id, search_query), поэтому одна и та же
        вакансия, встреченная под несколькими поисковыми запросами, даёт
        несколько строк. Группируем по vacancy_id, чтобы посчитать каждую
        вакансию один раз (как list_vacancy_texts/estimate_salary).
        """
        now = now or datetime.now()
        result = {"<1 дня": 0, "1-7 дней": 0, "7-30 дней": 0, "30+ дней": 0, "неизвестно": 0}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT MAX(published_at) AS published_at FROM vacancies_seen GROUP BY vacancy_id"
            ).fetchall()
        for row in rows:
            value = row["published_at"]
            try:
                age = (now - datetime.fromisoformat(value)).total_seconds() / 86400
            except (TypeError, ValueError):
                result["неизвестно"] += 1
                continue
            if age < 1:
                result["<1 дня"] += 1
            elif age < 7:
                result["1-7 дней"] += 1
            elif age < 30:
                result["7-30 дней"] += 1
            else:
                result["30+ дней"] += 1
        return result

    # --- Профиль аккаунта для внешних форм (#282/#284) -----------------------

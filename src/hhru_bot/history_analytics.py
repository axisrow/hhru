"""Аналитика: сводки, воронки, отказы, рыночные зарплаты (#1035).

Выделено из ``history.py`` механически; один report-топик на модуль
(конвенция репортов), read-only запросы поверх actions/responses/skipped.

С #1109 карточки вакансий (vacancies_seen) живут только в общей рыночной базе
data/market.db: методы, которым нужна карточка (воронка по запросам, отказы,
adaptive-отчёт), принимают уже открытый ``MarketStore`` параметром ``market``
и мягко деградируют (пустые карточные поля), когда база недоступна —
``None``. Глобального синглтона нет: открытие — в командах через
``market_store.open_market()``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Только для аннотаций: market_store импортирует этот модуль (AnalyticsMixin),
    # рантайм-импорт на уровне модуля дал бы цикл.
    from .market_store import MarketStore

    # Ленивый импорт внутри estimate_salary разрывает цикл history <-> search.
    from .search import SalaryInfo

logger = logging.getLogger("hhru_bot.history")


def _market_vacancies(market: MarketStore | None) -> list[dict]:
    """Строки vacancies_seen из общей market.db; [] если база недоступна.

    Единственная точка чтения рынка для карточных данных аналитики: ``None``
    (открыть не удалось) и сбой чтения деградируют одинаково — пустой список,
    как будто карточки ещё не собирались (#1109: рыночная база не должна
    ронять личную аналитику, тот же принцип, что у записи в search).
    """
    if market is None:
        return []
    try:
        return market.list_vacancies_seen()
    except Exception as e:  # noqa: BLE001 — рынок не должен валить аналитику
        logger.warning("Не прочитать vacancies_seen из market.db: %s", e)
        return []


class AnalyticsMixin:
    # Конверсия в процентах с защитой от деления на ноль: 0/0 → 0.0.
    # Округление до 1 знака — для читаемого CLI-вывода (воронка — для людей).
    @staticmethod
    def _pct(numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return round(numerator / denominator * 100, 1)

    # Минимум вакансий с указанной ЗП, чтобы считать медиану сферы устойчивой.
    # Ниже порога сфера уходит вниз таблицы и помечается low_sample: на прогоне
    # #67 сфера с n=2 встала НАВЕРХУ как «лидер рынка» — сортировка по одной
    # медиане без учёта размера выборки вводит в заблуждение.
    _LOW_SAMPLE_N = 5

    def summary(self, resume_id: str | None, period: str) -> dict:
        """Срез счётчиков action × status за период.

        Возвращает {"apply": {"success","dry_run","failed","uncertain"},
        "bump": {...}, "total"}. Пустой период → все нули. resume_id=None
        означает «по всем резюме».
        """
        result: dict = {
            "apply": {"success": 0, "dry_run": 0, "failed": 0, "uncertain": 0},
            "bump": {"success": 0, "dry_run": 0, "failed": 0, "uncertain": 0},
            "total": 0,
        }
        where = []
        params: list = []
        if resume_id is not None:
            where.append("resume_id = ?")
            params.append(resume_id)
        since = self._period_since(period)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT action, status, COUNT(*) AS cnt FROM actions{clause} "
                "GROUP BY action, status",
                params,
            ).fetchall()
        for row in rows:
            action = row["action"]
            status = row["status"]
            cnt = row["cnt"]
            if action in result and status in result[action]:
                result[action][status] = cnt
            result["total"] += cnt
        return result

    def reply_summary(self, resume_id: str | None, period: str) -> dict:
        """Сводка наших ответов из локальной таблицы ``replies`` за период.

        Успешные ответы считаются отправленными; ``dry_run`` в отправки не
        входит. ``total`` и ``period``/``letter_variants`` уважают один и тот
        же ``period`` (как ``summary().total`` — #112 review), а не только
        ``resume_id``.
        """
        filters: list[str] = []
        params: list = []
        if resume_id is not None:
            filters.append("resume_id = ?")
            params.append(resume_id)
        since = self._period_since(period)
        period_filters = [*filters]
        period_params = [*params]
        if since is not None:
            period_filters.append("created_at >= ?")
            period_params.append(since)
        period_clause = " WHERE " + " AND ".join(period_filters) if period_filters else ""
        total_filters = [*period_filters, "status = 'success'"]
        total_clause = " WHERE " + " AND ".join(total_filters)
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM replies{total_clause}", period_params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT status, letter_variant, COUNT(*) AS cnt FROM replies{period_clause} "
                "GROUP BY status, letter_variant",
                period_params,
            ).fetchall()
        result = {
            "total": total,
            "period": {"success": 0, "failed": 0, "uncertain": 0},
            "letter_variants": {},
        }
        for row in rows:
            if row["status"] == "success":
                result["period"]["success"] += row["cnt"]
                variant = row["letter_variant"] or "unknown"
                result["letter_variants"][variant] = (
                    result["letter_variants"].get(variant, 0) + row["cnt"]
                )
            elif row["status"] == "failed":
                result["period"]["failed"] += row["cnt"]
            elif row["status"] == "uncertain":
                result["period"]["uncertain"] += row["cnt"]
        return result

    def list_actions(self, resume_id: str | None, period: str, limit: int = 50) -> list[dict]:
        """Последние действия (свежие первыми) для таблицы stats.

        Возвращает список словарей с ключами resume_id/vacancy_id/action/status/
        reason/created_at. resume_id=None — по всем резюме.
        """
        where = []
        params: list = []
        if resume_id is not None:
            where.append("resume_id = ?")
            params.append(resume_id)
        since = self._period_since(period)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT resume_id, vacancy_id, action, status, reason, created_at "
                f"FROM actions{clause} ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def adaptive_report_facts(self, market: MarketStore | None = None) -> dict[str, list[dict]]:
        """Return the read-only rows needed by the adaptive-pool report.

        This deliberately does not reuse ``list_actions``: its default limit is
        a presentation concern and would silently truncate the metric.

        Вакансии читаются из ОБЩЕЙ рыночной базы (``market``, #1109): в
        history.db таблица vacancies_seen больше не создаётся. ``market is
        None`` — мягкая деградация: список вакансий пуст, метрики считаются по
        actions/responses/views как раньше. Строки идут в порядке
        ``last_seen_at DESC`` (порядок list_vacancies_seen): build_adaptive_
        metrics оставляет для вакансии первую встреченную (самую свежую) строку.
        """
        vacancies = [
            {
                "vacancy_id": row["vacancy_id"],
                "title": row["title"],
                "company": row["company"],
                "search_query": row["search_query"],
                "vacancy_text": row["vacancy_text"],
            }
            for row in _market_vacancies(market)
        ]
        with self._connect() as conn:
            actions = [
                dict(row)
                for row in conn.execute(
                    """SELECT resume_id, vacancy_id, action, status
                       FROM actions WHERE action = 'apply'"""
                )
            ]
            responses = [
                dict(row)
                for row in conn.execute("SELECT resume_id, vacancy_id, status FROM responses")
            ]
            views = [
                dict(row) for row in conn.execute("SELECT resume_id, viewed_at FROM resume_views")
            ]
        return {"vacancies": vacancies, "actions": actions, "responses": responses, "views": views}

    # --- Мониторинг ответов работодателей (#12, Этап 2) ------------------------
    # Новые методы в конец файла (паттерн with self._connect(), существующие
    # не трогаем). responses — отдельная таблица (см. SCHEMA), хранит ПОСЛЕДНЕЕ
    # состояние переписки по (vacancy_id, topic), а не журнал переходов.
    # upsert перезаписывает статус только при смене; last_seen_at обновляется
    # всегда (каждый fetch_responses видел эту вакансию в списке).

    def mark_offer(self, vacancy_id: str, resume_id: str) -> bool:
        """Ручная пометка оффера — липкая, per-resume, в отдельной таблице.

        hh.ru не отдаёт оффер как статус переговоров, поэтому верхний шаг
        воронки заполняется вручную командой ``mark --vacancy <id> --status offer``.
        Хранится в ``manual_offers`` (НЕ в responses #12): responses перезаписывается
        каждым scrape'ом #12 и затёр бы ручной offer; manual_offers — липкая пометка,
        survives последующие scrape'ы. Ключ UNIQUE(resume_id, vacancy_id) — per-resume
        (resume_id обязателен). Возвращает True, если пометка создана, False — если
        уже была.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO manual_offers (resume_id, vacancy_id, marked_at) "
                "VALUES (?, ?, ?)",
                (resume_id, vacancy_id, now),
            )
            return cur.rowcount > 0

    def funnel_by_resume(
        self,
        since: str | None = None,
        resume_id: str | None = None,
    ) -> list[dict]:
        """Воронка отправлено → просмотрено → приглашение → оффер по резюме.

        Этапы КУМУЛЯТИВНЫЕ (sent ⊇ viewed ⊇ invited ⊇ replied ⊇ offer): вакансия,
        до которой дошло приглашение, считается и просмотренной; оффер — и
        просмотренным, и приглашённым, и отвеченным нами. Это необходимо, т.к.
        #12 хранит в responses только ТЕКУЩИЙ статус переписки (после
        read→invitation прежний read уже не виден) — некумулятивный подсчёт
        давал бы viewed=0 после перехода. «Просмотрено» = любой ответ
        работодателя (#12: read/response/invitation/discard/offer) — отказ или
        письмо тоже означают, что резюме видели. «Наш ответ» (replied, #112) =
        залогированный успешный ``replies``-ответ на invitation/offer, ИЛИ сам
        факт оффера (responses status='offer' или ручная пометка manual_offers)
        — оффер невозможен без нашего ответа, даже если сам факт ответа не
        попал в локальный журнал (ручной оффер, сбой логирования).

        Ответы берутся из responses (#12, account-scope по vacancy_id) и
        replies (#108, account-scope по topic) плюс липкие ручные пометки из
        manual_offers (per-resume). Группировка по actions.resume_id. Пер-резюме
        точность ограничена account-scope responses/replies (ответ одной
        вакансии зачтётся всем резюме, откликнувшимся в неё) — это ограничение
        источника данных #12/#108 (нет достоверного связывания ответ→резюме).

        Конверсии: view_rate=viewed/sent, invite_rate=invited/viewed, reply_rate=
        replied/invited, offer_rate=offer/invited; 0% при пустом знаменателе.
        Возвращает список словарей (по строке на resume_id, отсортированных по
        убыванию отправленных). Пусто → [].
        """
        where = ["a.action = 'apply'", "a.status = 'success'"]
        params: list = []
        if since is not None:
            where.append("a.created_at >= ?")
            params.append(since)
        if resume_id is not None:
            where.append("a.resume_id = ?")
            params.append(resume_id)
        clause = " WHERE " + " AND ".join(where)

        # EXISTS-подзапросы вместо тройного LEFT JOIN: нет декартова произведения
        # при нескольких responses-строках одной вакансии (разные topic), и этапы
        # кумулятивны по построению (каждый следующий INCLUDE-список шире).
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    a.resume_id AS resume_id,
                    COUNT(DISTINCT a.vacancy_id) AS sent,
                    COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('read', 'response', 'invitation', 'discard', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.vacancy_id END) AS viewed,
                    COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('invitation', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.vacancy_id END) AS invited,
                    COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id AND r.status = 'offer'
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.vacancy_id END) AS offer
                    ,COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        JOIN replies p ON p.topic = r.topic AND p.status = 'success'
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('invitation', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id AND r.status = 'offer'
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.vacancy_id END) AS replied
                FROM actions AS a
                {clause}
                GROUP BY a.resume_id
                ORDER BY sent DESC, a.resume_id
                """,
                params,
            ).fetchall()

        funnel: list[dict] = []
        for row in rows:
            sent, viewed, invited = row["sent"], row["viewed"], row["invited"]
            replied, offer = row["replied"], row["offer"]
            funnel.append(
                {
                    "resume_id": row["resume_id"],
                    "sent": sent,
                    "viewed": viewed,
                    "invited": invited,
                    "replied": replied,
                    "offer": offer,
                    "view_rate": self._pct(viewed, sent),
                    "invite_rate": self._pct(invited, viewed),
                    "reply_rate": self._pct(replied, invited),
                    "offer_rate": self._pct(offer, invited),
                }
            )
        return funnel

    def funnel_by_search_query(
        self,
        since: str | None = None,
        resume_id: str | None = None,
        market: MarketStore | None = None,
    ) -> list[dict]:
        """Воронка отправлено → оффер с группировкой по поисковому запросу.

        Атрибуция запроса (#420, PR #449) — сперва ``actions.search_query``,
        записанный в момент самого отклика (apply/run передают текущий
        ``resume.search.text``, approved-заявки — запрос, сохранённый в
        ``review_queue`` на момент постановки в очередь). Если он ``NULL``
        (строки, созданные до появления колонки — миграционное окно, а не
        дефект — см. #420: "не бэкафилить исторические actions"), запрос
        берётся из собранных карточек ОБЩЕЙ рыночной базы (``market``,
        #1109 — в history.db таблица vacancies_seen больше не создаётся):
        отклик учитывается в каждом запросе, в котором была найдена его
        вакансия (карточка допускает несколько строк на vacancy_id — по одной
        на запрос). Внутри запроса счётчики дедуплицируются по паре
        (resume_id, vacancy_id), а не только по vacancy_id:
        ``idx_resume_vacancy_apply`` — UNIQUE по этой паре, поэтому два разных
        резюме легитимно откликаются на одну и ту же вакансию отдельными
        строками actions (code review #411) — дедуп по одному vacancy_id
        занижал бы sent/viewed/invited/offer/replied и искажал производные
        *_rate при дефолтном resume_id=None (все резюме). Этапы остаются
        кумулятивными, как в :meth:`funnel_by_resume`.

        ``market is None`` (market.db недоступен) — мягкая деградация: отклики
        без собственного query уходят в группу без запроса (``search_query``
        = ``None``), как раньше при пустых карточках; отклики с записанным
        query считаются как обычно.
        """
        where = ["a.action = 'apply'", "a.status = 'success'"]
        params: list = []
        if since is not None:
            where.append("a.created_at >= ?")
            params.append(since)
        if resume_id is not None:
            where.append("a.resume_id = ?")
            params.append(resume_id)
        clause = " WHERE " + " AND ".join(where)

        # Атрибуция откликов без собственного query: vacancy_id → запросы, под
        # которыми карточка найдена в market.db (в порядке свежести).
        seen: dict[str, list[str]] = {}
        for card in _market_vacancies(market):
            seen.setdefault(card["vacancy_id"], []).append(card["search_query"])

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    a.resume_id AS resume_id,
                    a.vacancy_id AS vacancy_id,
                    a.search_query AS search_query,
                    EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('read', 'response', 'invitation', 'discard', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) AS viewed,
                    EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('invitation', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) AS invited,
                    EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id AND r.status = 'offer'
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) AS offer,
                    EXISTS (
                        SELECT 1 FROM responses r
                        JOIN replies p ON p.topic = r.topic AND p.status = 'success'
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('invitation', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id AND r.status = 'offer'
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) AS replied
                FROM actions AS a
                {clause}
                """,
                params,
            ).fetchall()

        # Прежняя семантика SQL-группировки: отклик с собственным query
        # учитывается один раз в нём; без собственного — в КАЖДОМ запросе
        # найденной карточки, ни в одном — в NULL-группе. Дедуп по паре
        # (resume_id, vacancy_id) внутри запроса — аналог COUNT(DISTINCT ...)
        # из #411.
        groups: dict[str | None, dict[str, set[str]]] = {}
        for row in rows:
            own = row["search_query"]
            queries = [own] if own is not None else seen.get(row["vacancy_id"], [None])
            pair = f"{row['resume_id']}:{row['vacancy_id']}"
            for query in queries:
                counts = groups.setdefault(
                    query,
                    {
                        "sent": set(),
                        "viewed": set(),
                        "invited": set(),
                        "offer": set(),
                        "replied": set(),
                    },
                )
                counts["sent"].add(pair)
                for stage in ("viewed", "invited", "offer", "replied"):
                    if row[stage]:
                        counts[stage].add(pair)

        funnel: list[dict] = []
        for query, counts in groups.items():
            sent = len(counts["sent"])
            viewed = len(counts["viewed"])
            invited = len(counts["invited"])
            replied = len(counts["replied"])
            offer = len(counts["offer"])
            funnel.append(
                {
                    "search_query": query,
                    "sent": sent,
                    "viewed": viewed,
                    "invited": invited,
                    "replied": replied,
                    "offer": offer,
                    "view_rate": self._pct(viewed, sent),
                    "invite_rate": self._pct(invited, viewed),
                    "reply_rate": self._pct(replied, invited),
                    "offer_rate": self._pct(offer, invited),
                }
            )
        return sorted(
            funnel,
            key=lambda row: (
                -row["invite_rate"],
                -row["offer_rate"],
                row["search_query"] or "",
            ),
        )

    def rejections_by_employer(
        self,
        since: str | None = None,
        resume_id: str | None = None,
        market: MarketStore | None = None,
    ) -> list[dict]:
        """Агрегат отказов работодателей по поиску и вилке зарплаты.

        Отказ берётся из текущего статуса ``responses`` (``discard``), а
        вакансия считается только если для неё есть успешный отклик в
        ``actions`` — тот же scope, что и у воронки. ``since`` фильтрует дату
        отклика из ``actions``, поэтому ``--period`` имеет одинаковую семантику
        во всех режимах ``funnel``.

        Карточки вакансий (запрос + вилка) читаются из ОБЩЕЙ рыночной базы
        (``market``, #1109 — в history.db таблица vacancies_seen больше не
        создаётся). Карточка хранит по строке на пару (vacancy_id,
        search_query), поэтому одна вакансия может попасть в несколько
        поисковых групп. Для отказов без карточки добавляется отдельная строка
        с пустым поиском и зарплатой вместо тихой потери отказа;
        ``market is None`` деградирует так же — все отказы без карточных
        полей. Дедупликация по response_id не размножает отказ несколькими
        topic или actions.
        """
        from .responses import ResponseStatus

        filters = ["r.status = ?"]
        params: list = [ResponseStatus.DISCARD]
        if resume_id is not None:
            # responses is account-scoped, but an unambiguous SSR mapping may
            # carry resume_id. Do not attribute a known r2 conversation to r1;
            # an unattributed row still falls back to the vacancy-level action.
            filters.append("(r.resume_id IS NULL OR r.resume_id = ?)")
            params.append(resume_id)
        action_filters = [
            "a.action = 'apply'",
            "a.status = 'success'",
        ]
        action_params: list = []
        if since is not None:
            action_filters.append("a.created_at >= ?")
            action_params.append(since)
        if resume_id is not None:
            action_filters.append("a.resume_id = ?")
            action_params.append(resume_id)
        action_where = " AND ".join(action_filters)
        response_where = " AND ".join(filters)
        branch_params = [*params, *action_params]

        with self._connect() as conn:
            matched = conn.execute(
                f"""
                -- A known response belongs only to the application made
                -- with the same resume. Unattributed responses retain the
                -- vacancy-level fallback used by the legacy data.
                SELECT DISTINCT
                    r.id AS response_id,
                    NULLIF(TRIM(r.employer), '') AS employer,
                    r.vacancy_id AS vacancy_id,
                    a.search_query AS search_query
                FROM responses AS r
                JOIN actions AS a
                  ON a.vacancy_id = r.vacancy_id
                 AND (r.resume_id IS NULL OR r.resume_id = a.resume_id)
                WHERE {response_where}
                  AND {action_where}
                """,
                branch_params,
            ).fetchall()

        # Карточки из market.db: (vacancy_id, query) → зарплата и vacancy_id →
        # все его (query, зарплата) в порядке свежести строк. Прежняя
        # SQL-семантика трёх веток: явный query отклика авторитетен (зарплата
        # его собственной карточки, без карточки — пустые поля); legacy-отклик
        # без query приписывается каждой карточке вакансии; карточки нет
        # вовсе — отказ остаётся с пустыми полями.
        cards: dict[tuple[str, str], tuple] = {}
        by_vacancy: dict[str, list[tuple[str, tuple]]] = {}
        for card in _market_vacancies(market):
            key = (card["vacancy_id"], card["search_query"])
            salaries = (card["salary_from"], card["salary_to"], card["salary_currency"])
            if key in cards:
                continue
            cards[key] = salaries
            by_vacancy.setdefault(card["vacancy_id"], []).append((card["search_query"], salaries))

        # Агрегат прежнего GROUP BY: (employer, query, вилка) → set response_id
        # (COUNT(DISTINCT response_id) не размножает отказ по topic/actions).
        aggregates: dict[tuple, set[int]] = {}
        for m in matched:
            own = m["search_query"]
            if own is not None:
                variants = [(own, cards.get((m["vacancy_id"], own), (None, None, None)))]
            else:
                variants = by_vacancy.get(m["vacancy_id"]) or [(None, (None, None, None))]
            for query, salaries in variants:
                key = (m["employer"], query, *salaries)
                aggregates.setdefault(key, set()).add(m["response_id"])

        def _salary_order(value: int | str | None) -> tuple[bool, int | str]:
            # NULL-значения раньше ненулевых — порядок SQLite ASC.
            return (value is not None, value or 0)

        rows = [
            {
                "employer": employer,
                "search_query": query,
                "salary_from": salary_from,
                "salary_to": salary_to,
                "salary_currency": salary_currency,
                "rejections": len(response_ids),
            }
            for (
                employer,
                query,
                salary_from,
                salary_to,
                salary_currency,
            ), response_ids in aggregates.items()
        ]
        rows.sort(
            key=lambda row: (
                -row["rejections"],
                row["employer"] or "",
                row["search_query"] or "",
                _salary_order(row["salary_from"]),
                _salary_order(row["salary_to"]),
                _salary_order(row["salary_currency"]),
            )
        )
        return rows

    def count_unattributed_applies(
        self,
        since: str | None = None,
        resume_id: str | None = None,
        market: MarketStore | None = None,
    ) -> int:
        """Число успешных откликов без карточки в market.db (code review #411).

        ``funnel_by_search_query`` группирует отклик по запросу собранной
        карточки — вакансии без карточки молча выпадают из воронки по запросам.
        Карточки заполняет только команда ``search`` (``upsert_vacancy_seen``
        вызывается из ``commands/search.py`` в общую market.db); `apply`/`run`
        вызывают ``search_vacancies()`` напрямую и карточек не пишут — если
        пользователь откликался через `apply`/`run` без предварительного
        отдельного `search` по тем же вакансиям, эти отклики систематически не
        попадут в `funnel --search-query`. Используется командой `funnel` для
        `[INFO]`-предупреждения вместо тихой потери данных; не влияет на числа
        самой воронки. ``market is None`` — мягкая деградация: все отклики без
        собственного query считаются неатрибутированными.
        """
        where = ["a.action = 'apply'", "a.status = 'success'"]
        params: list = []
        if since is not None:
            where.append("a.created_at >= ?")
            params.append(since)
        if resume_id is not None:
            where.append("a.resume_id = ?")
            params.append(resume_id)
        clause = " WHERE " + " AND ".join(where)

        seen_ids = {card["vacancy_id"] for card in _market_vacancies(market)}
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.vacancy_id AS vacancy_id
                FROM actions AS a
                {clause}
                AND a.search_query IS NULL
                """,
                params,
            ).fetchall()
        return sum(1 for row in rows if row["vacancy_id"] not in seen_ids)

    def dead_responses(self, days: int, resume_id: str | None = None) -> dict:
        """«Мёртвая зона»: доля откликов без ответа старше N дней.

        Кандидат на смену письма/резюме — отклик отправлен, но ответа от
        работодателя нет уже дольше ``days`` дней. «Отвеченный» = есть любая
        responses-строка по вакансии (включая ``read`` — работодатель посмотрел
        резюме, это валидный сигнал; invitation/discard/response — тем более).
        JOIN по vacancy_id (как в воронке, account-scope).

        total_sent здесь = отклики СТАРШЕ N дней (кандидаты стать мёртвыми), НЕ
        все отправленные (как в воронке) — поле переиспользовано, подпись в
        format_dead проясняет semantics. Возвращает {total_sent, dead, dead_rate};
        dead_rate в процентах (0.0 при пустой истории).
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        where = ["a.action = 'apply'", "a.status = 'success'", "a.created_at < ?"]
        params: list = [cutoff]
        if resume_id is not None:
            where.append("a.resume_id = ?")
            params.append(resume_id)
        clause = " WHERE " + " AND ".join(where)

        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(DISTINCT a.vacancy_id) AS total_sent,
                    COUNT(DISTINCT CASE WHEN r.vacancy_id IS NULL
                                        THEN a.vacancy_id END) AS dead
                FROM actions AS a
                LEFT JOIN responses AS r ON r.vacancy_id = a.vacancy_id
                {clause}
                """,
                params,
            ).fetchone()

        total_sent = row["total_sent"] if row else 0
        dead = row["dead"] if row else 0
        return {
            "total_sent": total_sent,
            "dead": dead,
            "dead_rate": self._pct(dead, total_sent),
        }

    # --- Конкуренты: профессиональные снимки резюме (#578) ---------------------

    def market_salary_by_query(self, include_estimates: bool = False) -> list[dict]:
        """Медианы зарплаты по поисковому запросу — сравнение сфер по доходу.

        Главная цель #66: ранжировать сферы по медианной ЗП. #125: считаются ДВЕ
        независимые медианы, потому что вилка на hh.ru часто односторонняя:

        * ``median_from`` / ``with_from`` — медиана нижних границ («от N»);
        * ``median_to`` / ``with_to`` — медиана верхних границ («до N» / фикс.).

        Раньше считалась только вторая, поэтому вакансии «от 350 000» не попадали
        в расчёт ВООБЩЕ (до 28% выборки, смещение до 20% — #125). Границы НЕ
        сливаются в один ряд (``COALESCE``): «от 300» и «до 300» — разные
        величины, их медиана не имеет смысла. Середина вилки не достраивается:
        у односторонних вакансий второй границы не существует, и подставлять её
        значило бы выдумывать данные.

        Медиана отсутствует (ни одной границы такого типа в доминирующей валюте)
        → 0; отчёт рисует «—».

        ``median_from`` может оказаться ВЫШЕ ``median_to`` — это не баг. Медианы
        считаются по разным подмножествам вакансий: если работодатели с высокими
        зарплатами публикуют «от 900 000» без потолка, а вилку целиком указывают
        те, кто платит меньше, нижняя медиана честно окажется выше верхней. Две
        цифры — это два независимых среза рынка, а не границы одного интервала.

        ``count`` = все собранные вакансии сферы, ``with_salary`` = сколько с
        ЛЮБОЙ указанной границей (покрытие: вакансия «от N» — это данные, а не
        пропуск). ``low_sample`` = True, если реальных ЗП меньше
        ``_LOW_SAMPLE_N`` — такие сферы сортируются ниже надёжных.

        Сортировка: сначала надёжные сферы по убыванию медианы, затем ненадёжные
        (тоже по убыванию) — выгодные направления наверху, но не ценой того, что
        лидером станет строка на двух вакансиях. Ранжирует ``median_to``, а при
        её отсутствии — ``median_from`` (см. :meth:`_rank_median`).

        ``include_estimates`` (#93): если True — вакансии без указанной ЗП
        получают эвристическую оценку ``estimate_salary(search_query, tier)``
        (медиана по (query, tier) из данных) и включаются в медиану сферы. Так
        сферы, где большинство без ЗП, получают осмысленную медиану, а не 0/None.
        ВАЖНО (#125): оценка строится на ``salary_to``, т.е. это оценка ВЕРХНЕЙ
        границы — она достраивает только ``median_to``. В ``median_from`` оценки
        не подмешиваются, иначе верхняя граница выдавалась бы за нижнюю — ровно
        то смешение шкал, против которого заведён #125. Сфера, в медиану которой
        вошли оценки, помечается ``estimated=True`` — ``market_summary`` рисует
        перед её медианой ``~``. ``with_salary``/``with_from``/``with_to``
        остаются числами РЕАЛЬНЫХ ЗП (coverage доверия), независимо от оценок.

        Возвращает список словарей. Пусто → [].
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    v.search_query AS search_query,
                    -- #122: медианы считаются ТОЛЬКО по доминирующей валюте сферы.
                    -- salary_currency не нормализована: «от 6000 USD» рядом с
                    -- рублёвыми вилками занижало бы медиану, причём незаметно.
                    -- #125: доминирующая валюта — ОДНА на сферу, считается по
                    -- вакансиям с ЛЮБОЙ границей. Считать её отдельно для каждой
                    -- медианы нельзя: в отчёте одна колонка «Валюта», и медианы
                    -- в разных валютах под общей пометкой врали бы читателю.
                    (
                        SELECT salary_currency FROM vacancies_seen
                        WHERE search_query = v.search_query
                          AND (salary_from IS NOT NULL OR salary_to IS NOT NULL)
                        GROUP BY salary_currency
                        ORDER BY COUNT(*) DESC, salary_currency
                        LIMIT 1
                    ) AS currency,
                    COUNT(*) AS count,
                    -- Покрытие: вакансия с ЛЮБОЙ границей — это данные. До #125
                    -- здесь был COUNT(salary_to), и «от 350 000» считалась
                    -- вакансией без ЗП.
                    SUM(
                        CASE WHEN v.salary_from IS NOT NULL OR v.salary_to IS NOT NULL
                        THEN 1 ELSE 0 END
                    ) AS with_salary,
                    -- Сколько вакансий сферы имеют ЗП в ДРУГОЙ валюте (не вошли
                    -- ни в одну медиану) — чтобы отчёт мог честно об этом сказать.
                    SUM(
                        CASE WHEN (v.salary_from IS NOT NULL OR v.salary_to IS NOT NULL)
                          AND v.salary_currency IS NOT (
                            SELECT salary_currency FROM vacancies_seen
                            WHERE search_query = v.search_query
                              AND (salary_from IS NOT NULL OR salary_to IS NOT NULL)
                            GROUP BY salary_currency
                            ORDER BY COUNT(*) DESC, salary_currency
                            LIMIT 1
                        ) THEN 1 ELSE 0 END
                    ) AS other_currency
                FROM vacancies_seen AS v
                GROUP BY v.search_query
                """
            ).fetchall()
            out: list[dict] = []
            for row in rows:
                query = row["search_query"]
                currency = row["currency"]
                # Обе медианы — одним и тем же хелпером по своей колонке, оба раза
                # с фильтром доминирующей валюты (#122 применяется к ОБЕИМ).
                median_from, with_from = self._median_bound(
                    conn,
                    "salary_from",
                    "search_query = ? AND salary_currency IS ?",
                    [query, currency],
                )
                median_to, with_to = self._median_bound(
                    conn,
                    "salary_to",
                    "search_query = ? AND salary_currency IS ?",
                    [query, currency],
                )
                entry = {
                    "search_query": query,
                    "median_from": median_from or 0,
                    "median_to": median_to or 0,
                    "with_from": with_from,
                    "with_to": with_to,
                    "count": row["count"],
                    "with_salary": row["with_salary"] or 0,
                    "currency": currency,
                    "other_currency": row["other_currency"] or 0,
                    "estimated": False,
                }
                if include_estimates:
                    self._augment_with_estimates(conn, entry)
                # low_sample — про сферу целиком (есть ли вообще на что смотреть),
                # поэтому считается по with_salary (покрытие по любой границе).
                # Надёжность КАЖДОЙ из двух медиан по отдельности этим флагом не
                # передать: у них независимые выборки, и бывает with_from=10 при
                # with_to=1. Для этого случая guard — n прямо в ячейке
                # (report_market._format_median), а не флаг строки.
                entry["low_sample"] = (entry["with_salary"] or 0) < self._LOW_SAMPLE_N
                out.append(entry)
            # Сортировка в Python (а не в SQL): при include_estimates медиана
            # меняется уже после SELECT, а low_sample — производное поле. Ключ:
            # надёжные сферы выше ненадёжных, внутри группы — по убыванию
            # ранжирующей медианы, тай-брейк по count и search_query.
            out.sort(
                key=lambda e: (
                    e["low_sample"],
                    -self._rank_median(e),
                    -e["count"],
                    e["search_query"],
                )
            )
            return out

    @staticmethod
    def _rank_median(entry: dict) -> int:
        """По какой медиане ранжировать сферу: верхняя, иначе нижняя.

        Цель #66 — потолок предложения, поэтому основной ключ ``median_to``. Но
        у сферы, где ВСЕ вакансии «от N», верхней медианы не существует, и голый
        ``median_to`` дал бы ей 0 — она упала бы в конец списка, даже если её
        нижние границы выше чужих верхних. Это инверсия ровно того сравнения,
        ради которого отчёт и считается, поэтому при отсутствии верхней медианы
        ранжируем по нижней. Сравнение «нижняя против верхней» неточное, но
        честнее, чем считать отсутствие данных нулевым доходом.
        """
        return entry["median_to"] or entry["median_from"] or 0

    def _augment_with_estimates(self, conn, entry: dict) -> None:
        """#93: если в сфере есть вакансии БЕЗ верхней границы — достраивает
        ``median_to`` их оценками.

        Берёт все вакансии сферы без salary_to, для каждой считает
        ``estimate_salary``-медиану по её tier (через ``_median_salary_to`` на
        том же соединении — без рекурсивного open), и если оценки есть —
        пересчитывает медиану сферы по РЕАЛЬНЫМ ЗП + оценкам, помечая
        ``estimated=True``. Чистая медиана реальных ЗП (без вакансий без ЗП)
        остаётся ``with_salary``-покрытием; оценка НЕ подменяет реальную, а
        достраивает картину для сфер, где реальных ЗП мало/нет.

        #125: оценка построена на ``salary_to``, т.е. это оценка ВЕРХНЕЙ границы —
        она трогает ТОЛЬКО ``median_to``. ``median_from`` остаётся медианой
        реальных нижних границ: подмешать в неё оценку верхней значило бы
        смешать две разные шкалы, против чего и заведён #125. Следствие: у
        сферы, где есть только вакансии «от N», оценивать верх не из чего, и
        ``median_to`` честно остаётся пустым.
        """
        query = entry["search_query"]
        # Доминирующая валюта сферы (#122) — фильтр для ВСЕХ выборок ниже:
        # и реальных ЗП, и кандидатов на оценку, и самих tier-медиан.
        currency = entry.get("currency")
        # Реальные salary_to сферы (уже есть) + оценки для вакансий без ЗП.
        # #122: только доминирующая валюта — та же, по которой посчитана медиана
        # в market_salary_by_query, иначе оценки вернули бы смешение валют назад.
        real = [
            r["salary_to"]
            for r in conn.execute(
                "SELECT salary_to FROM vacancies_seen "
                "WHERE search_query = ? AND salary_to IS NOT NULL "
                "AND salary_currency IS ?",
                [query, currency],
            ).fetchall()
        ]
        # Кандидаты на оценку — вакансии БЕЗ ОБЕИХ границ (#125). Раньше отбор шёл
        # по `salary_to IS NULL`, из-за чего реальная вакансия «от 900 000»
        # считалась «без ЗП» и получала выдуманный потолок — то самое
        # достраивание вилки, которое запрещено дизайн-решением #125. Побочный
        # эффект был нагляден: нижняя медиана коридора могла оказаться ВЫШЕ
        # верхней. Теперь «от N» — это данные, и оценивать там нечего.
        #
        # #122: вакансия с ЗП в НЕдоминирующей валюте тоже не кандидат. Она уже
        # исключена из медианы и посчитана в other_currency, о чём отчёт прямо
        # пишет «не вошли в медиану»; вернуть её через оценку — сделать сноску
        # ложью. А вот у вакансии совсем без ЗП валюты нет по определению
        # (salary_currency IS NULL), и фильтровать её по валюте нельзя — иначе
        # оценивать будет вообще нечего, ради чего #93 и заводился. Поэтому
        # условие на валюту здесь не нужно: строки без обеих границ по
        # построению вне валютного разделения.
        no_salary_tiers = [
            r["employer_tier"]
            for r in conn.execute(
                "SELECT employer_tier FROM vacancies_seen "
                "WHERE search_query = ? AND salary_to IS NULL AND salary_from IS NULL",
                [query],
            ).fetchall()
        ]
        if not no_salary_tiers:
            return  # все вакансии с ЗП — оценки не нужны, медиана реальная.

        # Оценка одна на tier внутри сферы (медиана по (query, tier)).
        # ВНИМАНИЕ: здесь, в агрегате сферы, НЕ применяется порог _ESTIMATE_TIER_MIN_N
        # (в отличие от точечной estimate_salary): на уровне сферы бёрём любую
        # доступную tier-информацию (медиана по tier, иначе вся сфера), т.к. оценки
        # взвешиваются количеством и сходятся к реальной медиане. estimate_salary —
        # точечная оценка одной вакансии, там порог n>=5 отсекает шумный tier.
        # #122: оценки — тоже ТОЛЬКО в доминирующей валюте сферы. Фильтр по
        # валюте нужен на обоих входах (tier-медиана и fallback на всю сферу):
        # без него рублёвая вилка попадала в медиану, помеченную как USD, и
        # смешение валют возвращалось через путь оценок — ровно тот перекос,
        # ради которого заведён #122. Валюта та же, что в market_salary_by_query.
        tier_estimate: dict[str, int] = {}
        for tier in set(t for t in no_salary_tiers if t):
            med, _ = self._median_salary_to(
                conn,
                "search_query = ? AND employer_tier = ? AND salary_currency IS ?",
                [query, tier, currency],
            )
            if med is None:
                # fallback на всю сферу (в той же валюте).
                med, _ = self._median_salary_to(
                    conn,
                    "search_query = ? AND salary_currency IS ?",
                    [query, currency],
                )
            if med is not None:
                tier_estimate[tier] = med

        if not tier_estimate and not real:
            return  # оценок и реальных ЗП нет — оставляем как есть (0).

        combined = list(real)
        used_estimate = False
        for tier in no_salary_tiers:
            est = tier_estimate.get(tier or "")  # tier может быть NULL
            if est is not None:
                combined.append(est)
                used_estimate = True
            elif tier_estimate:
                # tier NULL/незнакомый, но оценки по др. tier есть → средняя оценка сферы.
                combined.append(sum(tier_estimate.values()) // len(tier_estimate))
                used_estimate = True

        if not combined:
            return
        combined.sort()
        n = len(combined)
        # Медиана тем же приёмом, что SQL-путь (AVG двух центральных, потом int):
        # _median_salary_to делает int(AVG(...)), здесь — int((a+b)/2) с round,
        # чтобы обе ветки считали медиану одинаково (без расхождения на 0.5).
        if n % 2 == 1:
            median = combined[n // 2]
        else:
            median = round((combined[n // 2 - 1] + combined[n // 2]) / 2)
        entry["median_to"] = int(median)
        if used_estimate:
            entry["estimated"] = True

    # --- Эвристическая оценка ЗП для вакансий без указанной (#93, часть B) -----
    #
    # ~50% вакансий на hh.ru РЕАЛЬНО без ЗП. Для рынок-анализа по доходу (#66)
    # нужны оценки. Гипотеза пользователя: «известные компании платят меньше,
    # потому что известные» (бренд-наценка наоборот). Это ГИПОТЕЗА — поэтому
    # коэффициенты tier'ов считаются ИЗ ДАННЫХ (медиана salary_to по
    # (search_query, tier)), а НЕ априорными константами «top_tech × 1.5».
    # Если данные покажут «top_tech < unknown» — эвристика это отразит; если по
    # tier мало данных (n<5) — fallback на медиану по всей сфере.

    # Минимум вакансий с ЗП по (query, tier), чтобы доверять tier-оценке, а не
    # падать на сферу. Мало данных → медиана по tier шумная → честнее сфера.
    _ESTIMATE_TIER_MIN_N = 5

    # Колонки-границы, по которым разрешено считать медиану. Список закрытый:
    # имя колонки подставляется в SQL текстом (параметром колонку не задать), и
    # белый список — граница между «внутренний хелпер» и SQL-инъекцией.
    _BOUND_COLUMNS = ("salary_from", "salary_to")

    def _median_bound(
        self, conn, column: str, where_clause: str, params: list
    ) -> tuple[int | None, int]:
        """Медиана одной границы вилки (``salary_from``/``salary_to``) + её n.

        Возвращает (median, count) — count это число строк, где ЭТА граница
        указана, а не число вакансий: у медиан «от» и «до» выборки разные (#125).
        Нет ни одного значения → (None, 0).

        Медиана — percentile через AVG двух центральных строк (тот же приём, что
        был в market_salary_by_query, вынесенный сюда, чтобы обе границы считались
        одинаково и без дублирования SQL).

        ВАЖНО: count берём из ``COUNT(*) OVER ()`` окна (число строк с ЗП в группе),
        а НЕ внешним ``COUNT(*)`` — внешний работает уже после ``WHERE rn IN (...)``
        (1-2 центральные строки) и давал бы 1/2, а не реальное число значений.
        """
        if column not in self._BOUND_COLUMNS:
            raise ValueError(f"недопустимая колонка границы: {column!r}")
        row = conn.execute(
            f"""
            SELECT AVG({column}) AS median, MAX(total) AS cnt
            FROM (
                SELECT {column}, ROW_NUMBER() OVER (ORDER BY {column}) AS rn,
                       COUNT(*) OVER () AS total
                FROM vacancies_seen
                WHERE {column} IS NOT NULL AND {where_clause}
            )
            WHERE rn IN ((total + 1) / 2, (total + 2) / 2)
            """,
            params,
        ).fetchone()
        if row is None or not row["cnt"]:
            return None, 0
        median = row["median"]
        return (int(median) if median else None, row["cnt"])

    def _median_salary_to(self, conn, where_clause: str, params: list) -> tuple[int | None, int]:
        """Медиана salary_to по произвольному условию + число строк с ЗП.

        Тонкая обёртка над :meth:`_median_bound` — оставлена как точка входа
        эвристических оценок (#93), которые строятся именно на верхней границе.
        """
        return self._median_bound(conn, "salary_to", where_clause, params)

    def estimate_salary(self, search_query: str, employer_tier: str) -> SalaryInfo | None:
        """Эвристическая оценка ЗП для вакансии БЕЗ указанной (#93, часть B).

        Считает медиану ``salary_to`` по собранным вакансиям сферы ``search_query``
        и tier'а ``employer_tier`` (top_tech/big_corp/mid/unknown). Коэффициенты
        tier'ов — ИЗ ДАННЫХ (медиана по tier внутри сферы), не априорные константы:
        если на практике «top_tech платит меньше» — оценка для top_tech будет
        ниже, гипотеза проверяется данными, а не угадывается.

        Fallback по убыванию доверия:
          1. Медиана по (search_query, tier), если по tier достаточно данных
             (n >= ``_ESTIMATE_TIER_MIN_N``). Наиболее точная оценка под конкретный
             tier работодателя.
          2. Иначе (мало данных по tier) — медиана по всей сфере (search_query,
             любой tier). Грубее, но не нулевая.
          3. Иначе — None (данных вообще нет, оценки не существует).

        Возвращает ``SalaryInfo`` (#34) с from=to=медиана (фиксированная оценка),
        в доминирующей валюте сферы. Остальные валюты не участвуют в оценке.
        Если доминирующая группа — вакансии без распознанной валюты
        (``salary_currency IS NULL``), оценка не строится (см. ниже): подписать
        такую медиану конкретной валютой было бы недоказанным допущением.
        ``SalaryInfo``
        импортируется лениво — разрыв цикла history ↔ search (search тянет history
        на верхнем уровне через SKIP_REASONS).

        Это derived-view: оценка честно отличается от реальной ЗП пометкой
        ``~оценка`` в выводе (см. report_market.market_summary).
        """
        from .search import SalaryInfo

        with self._connect() as conn:
            # salary_currency хранится как есть и может различаться внутри одного
            # search_query. Выбираем ту же доминирующую валюту, что и market-отчёт,
            # чтобы точечная оценка не смешивала, например, RUB и USD.
            currency_row = conn.execute(
                """
                SELECT salary_currency
                FROM vacancies_seen
                WHERE search_query = ?
                  AND (salary_from IS NOT NULL OR salary_to IS NOT NULL)
                GROUP BY salary_currency
                ORDER BY COUNT(*) DESC, salary_currency
                LIMIT 1
                """,
                [search_query],
            ).fetchone()
            currency = currency_row["salary_currency"] if currency_row else None

            # 1. Медиана по (query, tier).
            median, n_tier = self._median_salary_to(
                conn,
                "search_query = ? AND employer_tier = ? AND salary_currency IS ?",
                [search_query, employer_tier, currency],
            )
            source_tier = False
            if median is not None and n_tier >= self._ESTIMATE_TIER_MIN_N:
                source_tier = True

            # 2. Fallback на всю сферу, если по tier мало/нет данных.
            if not source_tier:
                median, _ = self._median_salary_to(
                    conn,
                    "search_query = ? AND salary_currency IS ?",
                    [search_query, currency],
                )

            if median is None:
                return None

            # currency=None значит, что ДОМИНИРУЮЩАЯ группа сферы — вакансии без
            # распознанной валюты (salary_currency IS NULL). Подписать такую
            # медиану "RUB" было бы недоказанной ложью (#529): реальных RUB-строк
            # в основе оценки может не быть вовсе. SalaryInfo требует строковую
            # валюту, отдать её не можем — оценки не существует, как и при
            # отсутствии данных вообще.
            if currency is None:
                return None

        return SalaryInfo(
            salary_from=median,
            salary_to=median,
            currency=currency,
            raw=f"~оценка {median} {currency}",
        )

    # --- Журнал отсева skipped (#87) ------------------------------------------
    # Отдельный слой в конец файла (паттерн with self._connect(), существующие
    # методы не трогаем). skipped — append-only кэш отсева filter_candidates:
    # повторный search видит «уже отсеяна» и не дёргает LLM/фильтры повторно
    # (экономия #74/#85). Ключ UNIQUE(resume_id, vacancy_id, reason): разные
    # причины — разные строки, как actions/responses. record_skip идемпотентен
    # по UNIQUE (INSERT OR IGNORE). Координируется с #85 (pre-LLM фильтр пишет
    # свои причины сюда же) — слой общий, точки записи не конфликтуют.

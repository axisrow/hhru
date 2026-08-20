"""Characterization-тесты методов воронки для команды funnel (#13).

Покрывает JOIN actions × responses, КУМУЛЯТИВНЫЕ этапы воронки (read→invitation
не «теряет» просмотр), «мёртвую зону» (отклики без ответа за N дней) и липкую
ручную пометку оффера (mark_offer в отдельной таблице manual_offers, durable
против последующих scrape'ов #12). Без браузера — только SQLite.

Таблица responses — account-scope (#12, миграция 012): ключ UNIQUE(vacancy_id,
topic), хранит ТЕКУЩИЙ статус. Поэтому воронка JOIN'ит по vacancy_id, группируется
по actions.resume_id, а этапы кумулятивны (sent ⊇ viewed ⊇ invited ⊇ offer) —
иначе read→invitation регрессировал бы viewed до 0. Ручной offer живёт в
manual_offers (per-resume, липкая), не в responses. Статусы read/invitation в
тестах сидируем через upsert_response #12 с явным topic.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from hhru_bot.history import History

pytestmark = pytest.mark.unit


def _iso_days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


# --- структура: responses и manual_offers существуют после инициализации ----


def test_responses_and_manual_offers_tables_exist(tmp_path):
    """History создаёт responses (#12) и manual_offers (#13) при инициализации."""
    h = History(tmp_path / "h.db")
    with h._connect() as conn:
        for tbl in ("responses", "manual_offers"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (tbl,),
            ).fetchone()
            assert row is not None, f"таблица {tbl} не создана"


# --- funnel_by_resume ------------------------------------------------------


def test_funnel_empty_returns_zero_conversions(tmp_path):
    """Пустая история: воронка пуста (нет отправленных откликов)."""
    h = History(tmp_path / "h.db")
    assert h.funnel_by_resume(since=None) == []
    assert h.funnel_by_resume(since=None, resume_id="r1") == []


def test_funnel_counts_sent_without_response(tmp_path):
    """Отправленные отклики без записи в responses/manual_offers: sent>0, rest=0."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r1", "v2", "apply", "success")

    funnel = h.funnel_by_resume(since=None)
    assert len(funnel) == 1
    row = funnel[0]
    assert row["resume_id"] == "r1"
    assert row["sent"] == 2
    assert row["viewed"] == 0
    assert row["invited"] == 0
    assert row["offer"] == 0
    assert row["view_rate"] == 0.0


def test_funnel_join_actions_with_responses(tmp_path):
    """JOIN: v1 просмотрен, v2 — приглашение, v3 без ответа. Считается честно."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")  # просмотрен
    h.record_action("r1", "v2", "apply", "success")  # приглашение
    h.record_action("r1", "v3", "apply", "success")  # мёртв
    h.upsert_response("v1", "Acme", "read", "/c", topic="1")
    h.upsert_response("v2", "Acme", "invitation", "/c", topic="2")

    row = h.funnel_by_resume(since=None)[0]
    assert row["sent"] == 3
    assert row["viewed"] == 2  # read (v1) + invitation (v2) — оба «просмотрены» (кумулятивно)
    assert row["invited"] == 1  # только invitation (v2)
    assert row["offer"] == 0


def test_funnel_any_employer_reply_counts_as_viewed(tmp_path):
    """РЕГРЕССИЯ cycle-2: discard/response — работодатель УВИДЕЛ резюме → viewed.

    Любой ответ работодателя (#12: read/response/invitation/discard) означает, что
    резюме просмотрели. Раньше discard/response не включались в viewed → вакансия
    с отказом показывала viewed=0, будто её не видели."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r1", "v2", "apply", "success")
    h.upsert_response("v1", "Acme", "discard", "/c", topic="1")  # отказ
    h.upsert_response("v2", "Acme", "response", "/c", topic="2")  # письмо

    row = h.funnel_by_resume(since=None)[0]
    assert row["viewed"] == 2  # оба просмотрены (отказ и письмо = работодатель видел)
    assert row["invited"] == 0  # ни приглашения, ни оффера
    assert row["offer"] == 0


def test_funnel_cumulative_read_to_invitation_regression(tmp_path):
    """РЕГРЕССИЯ cycle-1: read→invitation не должен обнулять viewed.

    #12 хранит ТЕКУЩИЙ статус. После read→invitation (та же переписка topic=1
    сменила статус) некумулятивный подсчёт давал viewed=0. Кумулятивные этапы:
    invitation включает просмотр → viewed остаётся 1."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.upsert_response("v1", "Acme", "read", "/c", topic="1")
    h.upsert_response("v1", "Acme", "invitation", "/c", topic="1")  # переход статуса

    row = h.funnel_by_resume(since=None)[0]
    assert row["viewed"] == 1  # НЕ 0 — кумулятивно
    assert row["invited"] == 1
    assert row["invite_rate"] == 100.0  # 1/1, а не 0% при viewed=0


def test_funnel_conversions_are_percent(tmp_path):
    """view_rate — процент (0..100) от sent."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r1", "v2", "apply", "success")
    h.upsert_response("v1", "Acme", "read", "/c", topic="1")

    row = h.funnel_by_resume(since=None)[0]
    assert row["viewed"] == 1
    assert row["sent"] == 2
    assert row["view_rate"] == 50.0


def test_funnel_offer_counted_from_mark(tmp_path):
    """Оффер (ручная пометка mark_offer) поднимается в воронку (через manual_offers)."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.mark_offer("v1", "r1")

    row = h.funnel_by_resume(since=None)[0]
    assert row["offer"] == 1
    # кумулятивно: offer → и viewed, и invited тоже
    assert row["viewed"] == 1
    assert row["invited"] == 1
    # РЕГРЕССИЯ (#112 review, cycle 3): offer ⊆ replied тоже, даже когда оффер
    # пришёл через ручную пометку mark_offer без залогированного replies-ответа.
    assert row["replied"] == 1


def test_funnel_offer_via_responses_counts_as_replied_without_logged_reply(tmp_path):
    """РЕГРЕССИЯ (#112 review, cycle 3): responses.status='offer' без replies-строки.

    Оффер физически невозможен без нашего ответа работодателю, даже если сам
    факт ответа не попал в локальный журнал replies (сбой логирования,
    ответили не через бота). offer=1 должен подразумевать replied=1."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.upsert_response("v1", "Acme", "offer", "/chat/1", topic="t1")

    row = h.funnel_by_resume(since=None)[0]
    assert row["offer"] == 1
    assert row["replied"] == 1


def test_funnel_multi_topic_vacancy_no_inflation(tmp_path):
    """РЕГРЕССИЯ cycle-1: вакансия с ответами в разных topic — счётчики не завышаются.

    Реальный production-кейс (#12 кладёт ответы с разными topic): вакансия v1
    имеет read (topic=1) + invitation (topic=2) + ручной offer. Кумулятивно:
    viewed=1 (одна вакансия), invited=1, offer=1, sent=1 — без декартова раздувания."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.upsert_response("v1", "Acme", "read", "/c", topic="1")
    h.upsert_response("v1", "Acme", "invitation", "/c", topic="2")
    h.mark_offer("v1", "r1")

    row = h.funnel_by_resume(since=None)[0]
    assert row["sent"] == 1
    assert row["viewed"] == 1
    assert row["invited"] == 1
    assert row["offer"] == 1


def test_funnel_separates_resumes(tmp_path):
    """Воронка группируется по actions.resume_id: каждое резюме — своя строка."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r2", "v9", "apply", "success")
    h.upsert_response("v1", "Acme", "read", "/c", topic="1")

    funnel = h.funnel_by_resume(since=None)
    by_resume = {r["resume_id"]: r for r in funnel}
    assert set(by_resume) == {"r1", "r2"}
    assert by_resume["r1"]["viewed"] == 1
    assert by_resume["r2"]["viewed"] == 0


def test_funnel_filters_by_resume(tmp_path):
    """resume_id в funnel_by_resume ограничивает срез одним резюме."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r2", "v9", "apply", "success")

    funnel = h.funnel_by_resume(since=None, resume_id="r1")
    assert len(funnel) == 1
    assert funnel[0]["resume_id"] == "r1"


def test_funnel_since_filters_old_actions(tmp_path):
    """since (ISO-отсечка created_at) отсекает старые отклики."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v2','apply','success','', ?)",
            (_iso_days_ago(30),),
        )
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()

    funnel = h.funnel_by_resume(since=cutoff)
    assert funnel[0]["sent"] == 1  # только свежий v1


def test_funnel_by_search_query_joins_seen_vacancies_and_sorts_by_invite_rate(tmp_path):
    """Запросы считаются отдельно, включая одну вакансию, найденную дважды."""
    h = History(tmp_path / "h.db")
    for vacancy in ("v1", "v2", "v3"):
        h.record_action("r1", vacancy, "apply", "success")
    with h._connect() as conn:
        for vacancy, query in (
            ("v1", "python"),
            ("v2", "python"),
            ("v2", "backend"),
            ("v3", "backend"),
        ):
            conn.execute(
                "INSERT INTO vacancies_seen "
                "(vacancy_id, search_query, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                (vacancy, query, "2026-01-01", "2026-01-01"),
            )
    h.upsert_response("v1", "Acme", "invitation", "/c", topic="1")

    funnel = h.funnel_by_search_query()
    assert [row["search_query"] for row in funnel] == ["python", "backend"]
    assert funnel[0]["sent"] == 2
    assert funnel[0]["invited"] == 1
    assert funnel[1]["sent"] == 2
    assert funnel[1]["invited"] == 0
    assert funnel[0]["invite_rate"] == 100.0
    assert funnel[1]["invite_rate"] == 0.0


def test_funnel_by_search_query_filters_resume_and_since(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r2", "v2", "apply", "success")
    with h._connect() as conn:
        for vacancy, _resume, created in (("v1", "r1", "2026-01-01"), ("v2", "r2", "2026-01-02")):
            conn.execute(
                "INSERT INTO vacancies_seen "
                "(vacancy_id, search_query, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                (vacancy, "q", "2026-01-01", "2026-01-01"),
            )
            conn.execute(
                "UPDATE actions SET created_at = ? WHERE vacancy_id = ?", (created, vacancy)
            )

    assert h.funnel_by_search_query(resume_id="r1")[0]["sent"] == 1
    assert h.funnel_by_search_query(since="2026-01-01T12:00:00")[0]["sent"] == 1


# --- dead_responses: отклики без ответа за N дней --------------------------


def test_dead_responses_counts_stale_without_status(tmp_path):
    """dead_responses(days): доля откликов старше N дней без записи в responses.

    total_sent = отклики старше days (знаменатель), dead = из них без ответа.
    """
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")  # свежий — вне зоны
    with h._connect() as conn:  # старый без ответа — мёртвый
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v2','apply','success','', ?)",
            (_iso_days_ago(10),),
        )
    with h._connect() as conn:  # старый с ответом — не мёртвый
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v3','apply','success','', ?)",
            (_iso_days_ago(10),),
        )
    h.upsert_response("v3", "Acme", "read", "/c", topic="3")

    dead = h.dead_responses(days=7)
    assert dead["total_sent"] == 2
    assert dead["dead"] == 1
    assert dead["dead_rate"] == 50.0


def test_dead_responses_empty_is_zero(tmp_path):
    """Пустая история: total_sent=0, dead=0, dead_rate=0.0 (без деления на ноль)."""
    h = History(tmp_path / "h.db")
    dead = h.dead_responses(days=7)
    assert dead["total_sent"] == 0
    assert dead["dead"] == 0
    assert dead["dead_rate"] == 0.0


# --- mark_offer: липкая ручная пометка (manual_offers) ---------------------


def test_mark_offer_inserts_into_manual_offers(tmp_path):
    """mark_offer создаёт запись в manual_offers со связью resume+vacancy."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")

    assert h.mark_offer("v1", "r1") is True

    row = h.funnel_by_resume(since=None)[0]
    assert row["offer"] == 1


def test_mark_offer_idempotent(tmp_path):
    """Повторный mark_offer на ту же (resume, vacancy) — no-op."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")

    assert h.mark_offer("v1", "r1") is True
    assert h.mark_offer("v1", "r1") is False

    row = h.funnel_by_resume(since=None)[0]
    assert row["offer"] == 1


def test_mark_offer_durable_against_subsequent_scrape(tmp_path):
    """РЕГРЕССИЯ cycle-1: ручной offer НЕ затирается следующим scrape'ом #12.

    Прежний баг: mark_offer писал offer в responses (topic=NULL), а следующий
    upsert_response(topic=None) перезаписывал его скрейпнутым статусом → offer
    молча пропадал. Теперь offer в manual_offers (липкая), scrape responses его
    не трогает."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.mark_offer("v1", "r1")
    # Имитация последующего scrape #12: discard без topic по той же вакансии
    h.upsert_response("v1", "Acme", "discard", None, topic=None)

    row = h.funnel_by_resume(since=None)[0]
    assert row["offer"] == 1  # ручной offer уцелел


def test_mark_offer_creates_record_even_without_action(tmp_path):
    """mark_offer работает и без записи в actions: просто создаёт manual_offer."""
    h = History(tmp_path / "h.db")
    h.mark_offer("v1", "r1")  # actions пусто

    with h._connect() as conn:
        row = conn.execute(
            "SELECT resume_id, vacancy_id FROM manual_offers WHERE resume_id=? AND vacancy_id=?",
            ("r1", "v1"),
        ).fetchone()
    assert row is not None


def test_funnel_counts_successful_reply_by_response_topic(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r1", "v2", "apply", "success")
    h.upsert_response("v1", "Acme", "invitation", "/chat/1", topic="t1")
    h.upsert_response("v2", "Acme", "invitation", "/chat/2", topic="t2")
    h.record_reply("t1", "m1", status="success")
    h.record_reply("t2", "m2", status="dry_run")

    row = h.funnel_by_resume(since=None)[0]
    assert row["invited"] == 2
    assert row["replied"] == 1
    assert row["reply_rate"] == 50.0


def test_funnel_reply_rate_cannot_exceed_invited_count(tmp_path):
    """reply_rate не должен превышать 100% (#112 review, cycle 2).

    replied должен быть подмножеством invited: успешный ответ на переписку,
    НЕ дошедшую до приглашения (status='read'/'response'), не должен
    засчитываться в replied — иначе replied может превысить invited и
    reply_rate станет невалидным (>100%)."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "vA", "apply", "success")
    h.record_action("r1", "vB", "apply", "success")
    h.record_action("r1", "vC", "apply", "success")
    h.upsert_response("vA", "Acme", "invitation", "/chat/a", topic="tA")
    h.upsert_response("vB", "Acme", "read", "/chat/b", topic="tB")
    h.upsert_response("vC", "Acme", "response", "/chat/c", topic="tC")
    h.record_reply("tB", "m-b", status="success")
    h.record_reply("tC", "m-c", status="success")

    row = h.funnel_by_resume(since=None)[0]
    assert row["invited"] == 1
    assert row["replied"] == 0
    assert row["reply_rate"] <= 100.0

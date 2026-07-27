"""Characterization-тесты методов воронки для команды funnel (#13).

Покрывает JOIN actions × responses, конверсии между шагами воронки
(деление на ноль → 0%), «мёртвую зону» (отклики без ответа за N дней) и
ручную пометку оффера (mark_offer). Без браузера — только SQLite.

Таблица responses — account-scope (#12, миграция 012): ключ UNIQUE(vacancy_id,
topic), resume_id опционален. Поэтому воронка JOIN'ит по vacancy_id и
группируется по actions.resume_id. Статусы read/invitation/discard наполняет
#12 через upsert_response; offer — ручная пометка mark_offer (hh.ru оффер не
отдаёт). В тестах read/invitation сидируем через upsert_response #12.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hhru_bot.history import History


def _iso_days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


# --- структура: responses существует после инициализации (создаётся #12) ----


def test_responses_table_exists_after_init(tmp_path):
    """History создаёт таблицу responses при инициализации (миграция 012, #12)."""
    h = History(tmp_path / "h.db")
    with h._connect() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='responses'"
        ).fetchone()
    assert row is not None


# --- funnel_by_resume ------------------------------------------------------


def test_funnel_empty_returns_zero_conversions(tmp_path):
    """Пустая история: воронка пуста (нет отправленных откликов)."""
    h = History(tmp_path / "h.db")
    assert h.funnel_by_resume(since=None) == []
    assert h.funnel_by_resume(since=None, resume_id="r1") == []


def test_funnel_counts_sent_without_response(tmp_path):
    """Отправленные отклики без записи в responses: sent>0, viewed/invited/offer=0."""
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
    # конверсия sent→viewed при viewed=0 → 0% (деление на ноль безопасно)
    assert row["view_rate"] == 0.0


def test_funnel_join_actions_with_responses(tmp_path):
    """JOIN: v1 просмотрен, v2 — приглашение, v3 без ответа. Считается честно."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")  # просмотрен
    h.record_action("r1", "v2", "apply", "success")  # приглашение
    h.record_action("r1", "v3", "apply", "success")  # мёртв (нет в responses)
    h.upsert_response("v1", "Acme", "read", "/chat/v1")
    h.upsert_response("v2", "Acme", "invitation", "/chat/v2")

    funnel = h.funnel_by_resume(since=None)
    row = funnel[0]
    assert row["sent"] == 3
    assert row["viewed"] == 1  # только read
    assert row["invited"] == 1  # только invitation
    assert row["offer"] == 0


def test_funnel_conversions_are_percent(tmp_path):
    """view_rate — процент (0..100) от sent."""
    h = History(tmp_path / "h.db")
    # 2 отправлено, 1 просмотрен → view_rate=50%
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r1", "v2", "apply", "success")
    h.upsert_response("v1", "Acme", "read", "/chat/v1")

    row = h.funnel_by_resume(since=None)[0]
    assert row["viewed"] == 1
    assert row["sent"] == 2
    assert row["view_rate"] == 50.0  # 1/2*100


def test_funnel_offer_counted_from_mark(tmp_path):
    """Оффер (status='offer', ручная пометка mark_offer) поднимается в воронку."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.mark_offer("v1")

    row = h.funnel_by_resume(since=None)[0]
    assert row["offer"] == 1
    # offer — отдельный статус (не read/invitation). offer_rate = offer/invited,
    # invited=0 → 0% (деление на ноль → 0%).
    assert row["invited"] == 0
    assert row["offer_rate"] == 0.0


def test_funnel_separates_resumes(tmp_path):
    """Воронка группируется по actions.resume_id: каждое резюме — своя строка."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r2", "v9", "apply", "success")
    h.upsert_response("v1", "Acme", "read", "/chat/v1")

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


# --- dead_responses: отклики без ответа за N дней --------------------------


def test_dead_responses_counts_stale_without_status(tmp_path):
    """dead_responses(days): доля откликов старше N дней без записи в responses.

    total_sent = отклики старше days (знаменатель), dead = из них без ответа.
    """
    h = History(tmp_path / "h.db")
    # свежий отклик (моложе 7 дней) — не участвует в «мёртвой зоне»
    h.record_action("r1", "v1", "apply", "success")
    # старый отклик без ответа — мёртвый
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v2','apply','success','', ?)",
            (_iso_days_ago(10),),
        )
    # старый отклик, но есть ответ — не мёртвый
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v3','apply','success','', ?)",
            (_iso_days_ago(10),),
        )
    h.upsert_response("v3", "Acme", "read", "/chat/v3")

    dead = h.dead_responses(days=7)
    assert dead["total_sent"] == 2  # 2 старых отклика (v2, v3) — знаменатель
    assert dead["dead"] == 1  # только v2 без ответа
    assert dead["dead_rate"] == 50.0  # 1/2*100


def test_dead_responses_empty_is_zero(tmp_path):
    """Пустая история: total_sent=0, dead=0, dead_rate=0.0 (без деления на ноль)."""
    h = History(tmp_path / "h.db")
    dead = h.dead_responses(days=7)
    assert dead["total_sent"] == 0
    assert dead["dead"] == 0
    assert dead["dead_rate"] == 0.0


# --- mark_offer: ручная пометка оффера -------------------------------------


def test_mark_offer_inserts_response_with_offer_status(tmp_path):
    """mark_offer создаёт запись в responses со status='offer' (по topic=NULL)."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")

    h.mark_offer("v1")

    row = h.funnel_by_resume(since=None)[0]
    assert row["offer"] == 1


def test_mark_offer_idempotent_no_duplicate(tmp_path):
    """Повторный mark_offer на ту же вакансию — no-op (offer уже стоит)."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")

    assert h.mark_offer("v1") is True
    assert h.mark_offer("v1") is False  # второй — уже offer

    row = h.funnel_by_resume(since=None)[0]
    assert row["offer"] == 1


def test_mark_offer_overwrites_other_status(tmp_path):
    """mark_offer по вакансии с прежним статусом (read/invitation) — создаёт offer.

    responses — account-scope по (vacancy_id, topic). Прежний ответ (напр.
    invitation через topic=chat_url) остаётся; mark_offer добавляет отдельную
    строку topic=NULL со status='offer'. Воронка считает offer (JOIN по
    vacancy_id)."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.upsert_response("v1", "Acme", "invitation", "/chat/v1")  # был приглашение

    changed = h.mark_offer("v1")
    assert changed is True

    row = h.funnel_by_resume(since=None)[0]
    assert row["offer"] == 1


def test_mark_offer_creates_response_even_without_action(tmp_path):
    """mark_offer работает и без записи в actions: просто создаёт response."""
    h = History(tmp_path / "h.db")
    h.mark_offer("v1")  # actions пусто

    with h._connect() as conn:
        row = conn.execute(
            "SELECT status FROM responses WHERE vacancy_id=? AND topic IS NULL",
            ("v1",),
        ).fetchone()
    assert row is not None
    assert row["status"] == "offer"

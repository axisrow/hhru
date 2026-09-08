"""Ответы работодателей: responses, replies, офферы, тест-задания (#1035).

Выделено из ``history.py`` механически; ``uncertain`` replies НЕ
дедуплицируется (#208) — асимметрия с has_applied намеренная.
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: Ключ настройки-водяного знака ``responses --alert-new`` (см. #176/#13).
RESPONSES_ALERT_CHECKPOINT = "responses.alert_new.last_success_at"

#: Допустимые значения ``replies.status`` (#108, #201). ``uncertain`` означает,
#: что клик состоялся, но позитивный сигнал не был пойман за таймаут. Кортеж, не
#: set: порядок стабилен для сообщений об ошибке.
#:
#: Валидируется в record_reply намеренно (в отличие от record_action): опечатка
#: или синоним (``"SUCCESS"``, ``"sent"``) прошли бы в БД молча, has_replied
#: навсегда вернул бы False, и бот отправил бы работодателю ВТОРОЕ сообщение.
#: В actions такая же ошибка лишь искажает статистику, здесь — видна человеку.
#:
#: Асимметрия с :meth:`History.has_applied` (#176) намеренная: там ``uncertain``
#: дедуплицируется, потому что повторный отклик безопаснее пропустить, чем
#: отправить второй. Для ответа в чате ``uncertain`` пока НЕ дедуплицируется:
#: повтор может показать работодателю дублирующее сообщение, но дедупликация
#: навсегда оставила бы чат без ответа, если первое сообщение не дошло. Вопрос
#: должен быть пересмотрен по продакшен-статистике (#208), а не решён догадкой.
REPLY_STATUS_VALUES = ("success", "failed", "dry_run", "uncertain")


class RepliesMixin:
    def upsert_response(
        self,
        vacancy_id: str,
        employer: str | None,
        status: str,
        chat_url: str | None,
        topic: str | None = None,
        response_date: str | None = None,
        resume_id: str | None = None,
    ) -> str:
        """Записывает/обновляет текущий статус ответа работодателя (account-scope).

        Ключ — ``(vacancy_id, topic)`` (одна строка на переписку). Страница
        /applicant/negotiations общая, поэтому обход остаётся account-scope и
        ответ НЕ клонируется под все resume_id (это фабриковало бы данные).
        Однозначный SSR topic mapping может атрибутировать конкретную строку.
        Одна вакансия может дать
        НЕСКОЛЬКО переписок (разные topic, напр. отклик с разных резюме) — ключ
        по вакансии затирал бы соседние; topic (= id чата из chat_url) их
        различает. topic=None (ответ без чата) группируется по vacancy_id
        (SQLite UNIQUE допускает несколько NULL). ``resume_id`` опционален — под
        будущую достоверную атрибуцию, в ключ UNIQUE не входит.

        Возвращает одно из: ``"inserted"`` (строка заведена впервые),
        ``"updated"`` (статус сменился — это «новый ответ»: прежний status
        копируется в last_status, метка status_changed_at сдвигается),
        ``"unchanged"`` (строка была, статус тот же — обновляем только last_seen_at
        и response_date, как «свежий взгляд без изменений»).
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM responses WHERE vacancy_id = ? AND topic IS ?",
                (vacancy_id, topic),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO responses
                        (resume_id, vacancy_id, topic, employer, status, chat_url,
                         response_date, last_seen_at, status_changed_at, created_at,
                         last_invitation_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resume_id,
                        vacancy_id,
                        topic,
                        employer,
                        status,
                        chat_url,
                        response_date,
                        now,
                        now,
                        now,
                        now if status == "invitation" else None,
                    ),
                )
                return "inserted"
            if row["status"] != status:
                # Статус сменился: прежний → last_status, новый → status, двигаем
                # status_changed_at. employer/chat_url/response_date освежаются тоже
                # (работодатель мог смениться или hh.ru отдал свежую дату ответа).
                conn.execute(
                    """
                    UPDATE responses
                       SET resume_id = COALESCE(?, resume_id), employer = ?,
                           last_status = status, status = ?,
                           chat_url = ?, response_date = ?, last_seen_at = ?,
                           status_changed_at = ?,
                           last_invitation_at = CASE WHEN ? = 'invitation'
                                                     THEN ?
                                                     ELSE last_invitation_at END
                     WHERE vacancy_id = ? AND topic IS ?
                    """,
                    (
                        resume_id,
                        employer,
                        status,
                        chat_url,
                        response_date,
                        now,
                        now,
                        status,
                        now,
                        vacancy_id,
                        topic,
                    ),
                )
                return "updated"
            # Статус не изменился — освежаем только «когда последний раз видели»
            # и дату ответа (hh.ru мог обновить блок даты без смены статуса).
            conn.execute(
                "UPDATE responses SET resume_id = COALESCE(?, resume_id), "
                "employer = ?, chat_url = ?, "
                "response_date = ?, last_seen_at = ? WHERE vacancy_id = ? AND topic IS ?",
                (resume_id, employer, chat_url, response_date, now, vacancy_id, topic),
            )
            return "unchanged"

    def new_responses_since(self, since: datetime, resume_id: str | None = None) -> list[dict]:
        """Ответы работодателей, чей статус сменился после ``since``.

        «Новый ответ» = status_changed_at > since (включает впервые заведённые
        строки: у них status_changed_at == created_at). resume_id=None — по всем
        резюме. Свежие первыми. Возвращает словари с ключами resume_id/vacancy_id/
        topic/employer/status/last_status/last_invitation_at/chat_url/response_date/
        status_changed_at — для вывода команды responses.
        """
        where = ["status_changed_at > ?"]
        params: list = [since.isoformat()]
        if resume_id is not None:
            where.append("resume_id = ?")
            params.append(resume_id)
        clause = " WHERE " + " AND ".join(where)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT resume_id, vacancy_id, topic, employer, status, last_status, chat_url, "
                f"response_date, status_changed_at, last_invitation_at "
                f"FROM responses{clause} ORDER BY status_changed_at DESC, id DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def responses_alert_checkpoint(self) -> datetime | None:
        """Return the last successful ``responses --alert-new`` timestamp."""
        value = self.get_setting(RESPONSES_ALERT_CHECKPOINT)
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"Некорректная метка responses --alert-new в истории: {value!r}"
            ) from exc

    def mark_responses_alert_success(self, at: datetime | None = None) -> None:
        """Persist the upper-bound watermark of a successful alert poll."""
        self.set_setting(RESPONSES_ALERT_CHECKPOINT, (at or datetime.now()).isoformat())

    # --- Воронка и ручная пометка оффера (#13) ----------------------------
    # Воронка JOIN'ит actions × responses. Таблица responses — account-scope
    # (#12): ключ UNIQUE(vacancy_id, topic), resume_id опционален и НЕ в ключе
    # (страница /applicant/negotiations не несёт достоверного признака
    # принадлежности ответа конкретному резюме). Поэтому JOIN идёт по
    # vacancy_id, а группировка воронки — по actions.resume_id (где отклик
    # отправлен). status='offer' — ручная пометка командой mark (hh.ru оффер
    # как статус переговоров не отдаёт); остальных статусов (read/invitation/
    # discard/response) наполняет #12 через upsert_response из живых переговоров.

    def employer_interacted(
        self,
        vacancy_id: str | None = None,
        employer: str | None = None,
        resume_id: str | None = None,
    ) -> bool:
        """Был ли ранее интерес работодателя (приглашение/просмотр) — сигнал pre-фильтра (#85).

        Account-scope (как responses #12): НЕ требует resume_id. Проверяет по
        ``vacancy_id`` (точный матч — работодатель отвечал по ЭТОЙ вакансии) И/ИЛИ
        по ``employer`` (имя компании — работодатель когда-то отвечал по ЛЮБОЙ из
        своих вакансий). resume_id опционален и сужает manual_offers до резюме
        (responses и так account-scope, resume_id в их ключ не входит — #12).

        «Взаимодействие» = есть responses-строка с активным статусом работодателя
        (read/response/invitation/discard/offer — любой ответ = резюме видели) ИЛИ
        липкая ручная пометка оффера в manual_offers. Чистые вакансии без ответа
        (нет строки в responses) → False. Возвращает True при первом совпадении.
        """
        if vacancy_id is None and employer is None:
            return False

        clauses = []
        params: list = []
        # responses: активный статус работодателя (любой ответ). read включаем —
        # работодатель ПОСМОТРЕЛ резюме, это валидный сигнал интереса.
        clauses.append("status IN ('read', 'response', 'invitation', 'discard', 'offer')")
        if vacancy_id is not None:
            clauses.append("vacancy_id = ?")
            params.append(vacancy_id)
        if employer is not None:
            clauses.append("employer = ?")
            params.append(employer)
        responses_where = " AND ".join(clauses)

        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM responses WHERE {responses_where} LIMIT 1",
                params,
            ).fetchone()
            if row is not None:
                return True

            # manual_offers: липкая ручная пометка оффера. resume_id обязателен в
            # таблице, но здесь опционален — без него учитываем все пометки.
            offer_clauses = []
            offer_params: list = []
            if vacancy_id is not None:
                offer_clauses.append("vacancy_id = ?")
                offer_params.append(vacancy_id)
            if resume_id is not None:
                offer_clauses.append("resume_id = ?")
                offer_params.append(resume_id)
            offer_where = (" WHERE " + " AND ".join(offer_clauses)) if offer_clauses else ""
            row = conn.execute(
                f"SELECT 1 FROM manual_offers{offer_where} LIMIT 1",
                offer_params,
            ).fetchone()
            return row is not None

    def record_reply(
        self,
        topic: str,
        inbound_marker: str,
        *,
        vacancy_id: str | None = None,
        resume_id: str | None = None,
        status: str,
        letter_variant: str | None = None,
        note: str | None = None,
    ) -> None:
        """Записывает наш ответ на входящее сообщение (идемпотентно по UNIQUE).

        ``inbound_marker`` — непрозрачный признак входящего: реальный message_id
        либо суррогат (дата + хеш текста), см. комментарий к таблице. ``status``
        — из :data:`REPLY_STATUS_VALUES`; ``uncertain`` означает, что клик был
        выполнен, но подтверждение не поймано за таймаут.

        Идемпотентность — по partial-UNIQUE, то есть только по УСПЕШНЫМ ответам:
        повторный ``success`` на ту же (topic, inbound_marker) — no-op (INSERT OR
        IGNORE), первая успешная запись не перезаписывается. Неуспешные попытки
        (``dry_run``/``failed``/``uncertain``) ключ не занимают: они копятся строками для
        аналитики и НЕ блокируют последующий ``success`` — иначе штатный сценарий
        «сначала --dry-run, потом боевая отправка» терял бы факт отправки. Разные
        входящие в одном чате — разные строки (диалог продолжается).

        :raises ValueError: ``status`` вне :data:`REPLY_STATUS_VALUES`.
        """
        if status not in REPLY_STATUS_VALUES:
            raise ValueError(
                f"недопустимый status={status!r} для replies; "
                f"ожидается одно из {REPLY_STATUS_VALUES}"
            )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO replies
                    (topic, inbound_marker, vacancy_id, resume_id, status,
                     letter_variant, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    topic,
                    inbound_marker,
                    vacancy_id,
                    resume_id,
                    status,
                    letter_variant,
                    note,
                    datetime.now().isoformat(),
                ),
            )

    def has_replied(self, topic: str, inbound_marker: str) -> bool:
        """True, если мы УСПЕШНО ответили на это входящее (для планирования).

        Только ``status='success'``: ``dry_run``, ``failed`` и ``uncertain`` отправкой не
        считаются. В частности, ``uncertain`` намеренно НЕ дедуплицирует ответ,
        в отличие от :meth:`has_applied` (#176): повтор может показать работодателю
        дублирующее сообщение, но дедупликация оставила бы чат без ответа, если
        первое сообщение не дошло. Это компромисс до накопления продакшен-статистики
        (#208). ``dry_run`` также не дедуплицирует отклик: иначе холостой прогон
        навсегда заблокировал бы боевой ответ на живое входящее.

        Тот же ``uncertain``-компромисс относится и к ``--follow-up`` (#710,
        cycle-review PR #761): для напоминаний ``inbound_marker`` — не живой
        marker чата, а синтетический маркер затишья ``follow_up:<status_changed_at>``
        (см. ``reply_employers.py``), но он проходит через тот же ``has_replied``
        и наследует то же поведение — повторный запуск при статусе ``uncertain``
        отправит ещё одно напоминание по тому же затишью, а не будет заблокирован.

        НЕ финальная проверка перед отправкой — см. границу ответственности выше:
        False здесь не значит «в чате нет нашего ответа».
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM replies "
                "WHERE topic = ? AND inbound_marker = ? AND status = 'success' LIMIT 1",
                (topic, inbound_marker),
            ).fetchone()
            return row is not None

    def save_reply_draft(
        self,
        *,
        topic: str,
        inbound_marker: str,
        vacancy_id: str,
        resume_id: str | None,
        message: str,
    ) -> int:
        """Persist a human-reviewable suggestion; this never sends anything."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO reply_drafts
                (topic,inbound_marker,vacancy_id,resume_id,message,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(topic,inbound_marker) DO UPDATE SET
                message=excluded.message, vacancy_id=excluded.vacancy_id,
                resume_id=excluded.resume_id, updated_at=excluded.updated_at,
                status='draft'""",
                (topic, inbound_marker, vacancy_id, resume_id, message, now, now),
            )
            return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def reply_candidates(self, limit: int | None = None) -> list[dict]:
        """Return account-wide chat candidates using only local history.

        The live message marker is intentionally not available here.  It is
        read only after a candidate chat is opened, where ``has_replied`` can
        perform the final duplicate check.
        """
        sql = """
            SELECT r.vacancy_id, r.topic, COALESCE(v.title, r.vacancy_id) AS title,
                   COALESCE(r.employer, '') AS employer
              FROM responses AS r
              LEFT JOIN vacancies_seen AS v ON v.vacancy_id = r.vacancy_id
             WHERE r.topic IS NOT NULL
             GROUP BY r.vacancy_id, r.topic
             ORDER BY MAX(r.last_seen_at) DESC, r.id DESC
        """
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def follow_up_candidates(self, after_days: int, limit: int | None = None) -> list[dict]:
        """Account-wide chats whose status has been stale for at least N days (#710).

        ``status IN ('response', 'read')`` и с тех пор ничего не изменилось:
        ``status_changed_at`` старше ``after_days``. ``read`` здесь — не строго
        «работодатель прочитал»: ``responses.normalize_status(None)`` тоже даёт
        ``read`` (свежий отклик вовсе без бейджа hh.ru), поэтому в выборку
        попадает и «прочитано и молчит», и «реакции не было совсем» — общий
        случай «работодатель ничего не ответил». ``status_changed_at`` (не
        ``last_seen_at``) — момент реальной смены статуса, а не последней
        проверки нашей стороной; иначе частый ``responses`` polling бесконечно
        откладывал бы порог напоминания. ``invitation``/``discard`` сюда не
        попадают: напоминать не о чем — либо работодатель уже ответил, либо
        отказал.

        Как и :meth:`reply_candidates`, живой маркер входящего сообщения здесь
        недоступен — финальную проверку (последнее слово за нами, hh.ru
        явно разрешает напоминание) делает вызывающий код после открытия чата.
        """
        cutoff = (datetime.now() - timedelta(days=after_days)).isoformat()
        sql = """
            SELECT r.vacancy_id, r.topic, COALESCE(v.title, r.vacancy_id) AS title,
                   COALESCE(r.employer, '') AS employer, r.status_changed_at
              FROM responses AS r
              LEFT JOIN vacancies_seen AS v ON v.vacancy_id = r.vacancy_id
             WHERE r.topic IS NOT NULL
               AND r.status IN ('response', 'read')
               AND r.status_changed_at <= ?
             GROUP BY r.vacancy_id, r.topic
             ORDER BY r.status_changed_at ASC, r.id ASC
        """
        params: list[object] = [cutoff]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def record_reply_and_action(
        self,
        topic: str,
        inbound_marker: str,
        *,
        vacancy_id: str,
        resume_id: str | None = None,
        status: str,
        reason: str | None = None,
        letter_variant: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Write the reply journal and action audit in one SQLite transaction.

        ``resume_id`` — резюме, с которого шёл отклик, из SSR ``topicList[].resumeId``
        (#200). Опционален: hh.ru отдаёт его стабильно (проверено 2026-08-16, 7/7
        переписок), но дрейф разметки не должен ронять журналирование ответа —
        отсутствие даёт NULL в ``replies`` и account-wide сентинел в ``actions``,
        как было до #200.
        """
        if status not in REPLY_STATUS_VALUES:
            raise ValueError(f"недопустимый status={status!r} для replies")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO replies
                   (topic, inbound_marker, vacancy_id, resume_id, status,
                    letter_variant, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    topic,
                    inbound_marker,
                    vacancy_id,
                    resume_id,
                    status,
                    letter_variant,
                    reason,
                    now,
                ),
            )
            # actions.resume_id is NOT NULL — пустая строка остаётся сентинелом
            # только когда SSR не отдал resumeId (см. докстринг).
            conn.execute(
                """INSERT INTO actions
                   (resume_id, vacancy_id, action, status, reason, letter_variant,
                    run_id, created_at)
                   VALUES (?, ?, 'reply', ?, ?, ?, ?, ?)""",
                (resume_id or "", vacancy_id, status, reason, letter_variant, run_id, now),
            )

    def finalize_reply_action(
        self,
        action_id: int,
        topic: str,
        inbound_marker: str,
        *,
        vacancy_id: str,
        resume_id: str | None = None,
        status: str,
        reason: str | None = None,
        letter_variant: str | None = None,
    ) -> None:
        """Finalize a pre-click reply reservation and journal the reply atomically.

        Codex adversarial review (cycle-review PR #471, round 3): the pre-click
        durable barrier for reply-employers (``begin_action`` before the send
        click, mirroring apply/withdraw) previously called
        ``finalize_action`` and ``record_reply`` as two separate
        ``self._connect()`` transactions. A crash between them left a
        finalized ``actions`` row with no matching ``replies`` row -- the
        action audit trail survived, but ``has_replied()`` (which reads only
        ``replies``) would return False on the next run, silently reopening
        the duplicate-send guard #12 exists to close. This method commits
        both writes in one transaction, matching ``record_reply_and_action``'s
        atomicity guarantee for the non-reserved (dry-run/pre-click-failed)
        path this pre-click barrier does not cover.
        """
        if status not in REPLY_STATUS_VALUES:
            raise ValueError(f"недопустимый status={status!r} для replies")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE actions
                   SET status = ?, reason = ?, letter_variant = ?, reason_code = ?
                 WHERE id = ?
                """,
                (status, reason, letter_variant, status, action_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Действие истории не найдено: id={action_id}")
            conn.execute(
                """INSERT OR IGNORE INTO replies
                   (topic, inbound_marker, vacancy_id, resume_id, status,
                    letter_variant, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    topic,
                    inbound_marker,
                    vacancy_id,
                    resume_id,
                    status,
                    letter_variant,
                    reason,
                    now,
                ),
            )

    def replies_since(self, since: datetime) -> list[dict]:
        """Наши ответы, записанные после ``since`` — для аналитики и отчётов.

        Свежие первыми. Возвращает ВСЕ статусы (включая ``dry_run``/``failed``/
        ``uncertain``):
        журнал полный, фильтр «успешных» — задача вызывающего. Ключи словарей:
        topic/inbound_marker/vacancy_id/resume_id/status/letter_variant/note/
        created_at.

        ``since`` — НАИВНЫЙ datetime в локальном времени (как ``datetime.now()``,
        которым пишется ``created_at``): сравнение идёт лексикографически по
        ISO-строке, и tz-aware значение (суффикс ``+00:00``) дало бы мусорный
        результат. Граница ИСКЛЮЧАЮЩАЯ (``>``, как в new_responses_since).
        Не курсор: ``isoformat()`` опускает микросекунды, когда они ровно нули,
        поэтому передача ``created_at`` последней строки как ``since`` может
        пропустить строку той же секунды — для дозапроса фильтруй по id.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT topic, inbound_marker, vacancy_id, resume_id, status,
                       letter_variant, note, created_at
                FROM replies
                WHERE created_at > ?
                ORDER BY created_at DESC, id DESC
                """,
                (since.isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Назначения внешних тестов (#180) -----------------------------------

    def record_test_assigned(
        self,
        resume_id: str | None,
        vacancy_id: str,
        topic: str,
        employer: str,
        test_url: str,
        message_text: str,
        *,
        detected_at: datetime | None = None,
    ) -> None:
        """Append a read-only fact discovered in an employer chat.

        resume_id is account-scope metadata, not a verified attribution
        (/applicant/negotiations does not expose which resume a chat belongs
        to) — callers must pass None unless a real mapping exists.

        OR IGNORE: a re-run of ``responses --detect-external-tests`` re-reads
        the same chat message (no message_id cursor), so without dedup it
        would insert a duplicate row every time — see
        ``idx_test_assignments_dedup``.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO test_assignments
                    (resume_id, vacancy_id, topic, employer, test_url, message_text, detected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resume_id,
                    vacancy_id,
                    topic,
                    employer,
                    test_url,
                    message_text,
                    (detected_at or datetime.now()).isoformat(),
                ),
            )

    def test_assignments_since(self, since: datetime) -> list[dict]:
        """Return detected test assignments newer than ``since``, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT resume_id, vacancy_id, topic, employer, test_url, message_text, detected_at
                FROM test_assignments
                WHERE detected_at > ?
                ORDER BY detected_at DESC, id DESC
                """,
                (since.isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]

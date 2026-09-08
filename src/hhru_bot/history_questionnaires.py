"""Анкеты: сканы вопросов, шаблоны, очередь на обучение, аудит (#1035).

Выделено из ``history.py`` механически; вся история ключуется реальным
resume_id (hex-хвост resume_url), а НЕ слагом из config.yaml (#486 п.1).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

from .external_forms.detect import normalize
from .history_skip_reasons import SKIP_REASONS

logger = logging.getLogger("hhru_bot.history")


class QuestionnairesMixin:
    def mark_robot_questionnaire(
        self, topic: str, *, vacancy_id: str | None = None, reason: str = "detected"
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO robot_questionnaires "
                "(topic, vacancy_id, reason, detected_at) VALUES (?, ?, ?, ?)",
                (topic, vacancy_id, reason, datetime.now().isoformat()),
            )

    def record_questionnaire(
        self,
        resume_id: str,
        vacancy_id: str,
        vacancy_url: str,
        title: str,
        company: str,
        questions: list[dict[str, object]],
        *,
        source: str = "probe",
        run_id: str | None = None,
    ) -> None:
        """Append a questionnaire snapshot and its visible questions.

        ``filled`` records only a successful form fill, never an HH.ru submit
        or its later confirmation.  Apply results remain the single source of
        truth in ``actions`` and are joined to this audit by ``run_id``.
        """
        if source not in {"probe", "apply"}:
            raise ValueError(f"unknown questionnaire source: {source!r}")
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO questionnaire_scans
                   (resume_id, vacancy_id, vacancy_url, title, company, source, detected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    resume_id,
                    vacancy_id,
                    vacancy_url,
                    title,
                    company,
                    source,
                    datetime.now().isoformat(),
                ),
            )
            scan_id = cursor.lastrowid
            conn.executemany(
                """INSERT INTO questionnaire_questions
                   (scan_id, body_index, text, kind, is_radio, options_json,
                    answer, answer_source, confidence, filled, run_id,
                    template, cluster, resolver_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        scan_id,
                        int(question["body_index"]),
                        str(question["text"]),
                        str(question["kind"]),
                        int(bool(question["is_radio"])),
                        json.dumps(question["options"], ensure_ascii=False),
                        question.get("answer"),
                        question.get("answer_source"),
                        question.get("confidence"),
                        int(bool(question.get("filled", False))),
                        run_id,
                        question.get("template"),
                        question.get("cluster"),
                        question.get("resolver_source"),
                    )
                    for question in questions
                ],
            )

    def rekey_questionnaire_scans(self, old_resume_id: str, new_resume_id: str) -> int:
        """Переключить накопленные анкеты со слага конфига на реальный resume_id.

        До #486 ``probe --questionnaires-only`` ключевал сканы слагом
        (``resume.id``), тогда как apply-путь и ``questionnaire._scope()``
        используют hex-хвост ``resume_url``. В одной таблице оказались оба вида
        ключей, и ``learn --resume python`` находил единицы вопросов вместо
        сотни — молча, без предупреждения. Тот же перекос задевал scoped
        ``stats``: ``questionnaire_answer_summary`` джойнит эту же таблицу.

        Переносятся ОБЕ таблицы. Очередь — не производная от сканов: обходной
        путь из issue (``learn`` БЕЗ ``--resume``) сеет строки под ключом из
        скана, а не под scope, поэтому в ``questionnaire_pending`` слаг-строк
        накопилось больше, чем в сканах. Перенеся только сканы, следующий
        ``learn`` заново засеял бы те же вопросы уже под hex-ключом: ON CONFLICT
        очереди — ``(resume_id, question_key)``, слаг и hex не сталкиваются, и
        вышел бы дубль, половина которого недостижима навсегда.

        При схлопывании близнецов побеждает строка с более поздним
        ``updated_at``. Равенство времени считается неоднозначностью: обе
        строки остаются на месте, чтобы перенос не выдавал ключ за доказательство
        свежести и не удалял данные без доказательства.

        Идемпотентно и узко: трогает ровно строки со старым ключом. Системы
        миграций в проекте нет намеренно (CLAUDE.md, «Схема SQLite»), поэтому
        разовая нормализация живёт как обычный метод и вызывается командой, у
        которой на руках есть маппинг слаг -> resume_id из конфига.

        Возвращает число перенесённых строк сканов (то, что видит пользователь
        как «сколько анкет вернулось в оборот»).
        """
        if not old_resume_id or old_resume_id == new_resume_id:
            return 0
        with self._connect() as conn:
            # Do not move scans first and discover an unmergeable queue row
            # afterwards: that would split the same legacy key across tables.
            # Equal or malformed timestamps provide no ordering evidence, so
            # the whole rekey is fail-closed before the first mutation.
            pending_pairs = conn.execute(
                """SELECT slug.updated_at AS slug_updated, hex.updated_at AS hex_updated
                     FROM questionnaire_pending AS slug
                     JOIN questionnaire_pending AS hex
                       ON hex.resume_id = ?
                      AND hex.question_key = slug.question_key
                    WHERE slug.resume_id = ?""",
                (new_resume_id, old_resume_id),
            ).fetchall()
            for pair in pending_pairs:
                try:
                    slug_updated = datetime.fromisoformat(pair["slug_updated"])
                    hex_updated = datetime.fromisoformat(pair["hex_updated"])
                except (TypeError, ValueError):
                    return 0
                try:
                    if not (slug_updated < hex_updated or slug_updated > hex_updated):
                        return 0
                except TypeError:
                    return 0
            moved = conn.execute(
                "UPDATE questionnaire_scans SET resume_id = ? WHERE resume_id = ?",
                (new_resume_id, old_resume_id),
            ).rowcount
            # Сначала слить слаг-строку в её hex-близнеца. Ключ строки не
            # доказывает её свежесть: старый probe мог оставить слаг-строку
            # после более нового apply под hex-ключом. Обходим пары в Python,
            # чтобы удалить только ту строку, чья судьба доказана сравнением
            # исходных timestamps: после копирования timestamps стали бы равны
            # и равенство уже нельзя было бы отличить от неоднозначности.
            slug_rows = conn.execute(
                "SELECT * FROM questionnaire_pending WHERE resume_id = ?",
                (old_resume_id,),
            ).fetchall()
            # ``created_at`` намеренно исключён: как и в ``ON CONFLICT DO
            # UPDATE`` у ``record_questionnaire_pending``, это момент первого
            # появления вопроса У ВЫЖИВШЕЙ строки, а не последнего обновления —
            # затирать его временем слаг-строки значило бы терять provenance
            # «когда вопрос впервые встречен» тем же способом, каким остальные
            # поля здесь его сохраняют.
            payload_columns = (
                "vacancy_id",
                "vacancy_url",
                "question_text",
                "kind",
                "is_radio",
                "options_json",
                "template",
                "cluster",
                "reason",
                "status",
                "run_id",
                "updated_at",
            )
            for slug in slug_rows:
                hex_row = conn.execute(
                    """SELECT * FROM questionnaire_pending
                       WHERE resume_id = ? AND question_key = ?""",
                    (new_resume_id, slug["question_key"]),
                ).fetchone()
                if hex_row is None:
                    conn.execute(
                        "UPDATE questionnaire_pending SET resume_id = ? WHERE id = ?",
                        (new_resume_id, slug["id"]),
                    )
                    continue
                try:
                    slug_updated = datetime.fromisoformat(slug["updated_at"])
                    hex_updated = datetime.fromisoformat(hex_row["updated_at"])
                except (TypeError, ValueError):
                    # Legacy/malformed timestamps do not prove ordering.
                    continue
                try:
                    if slug_updated == hex_updated:
                        continue
                    slug_is_newer = slug_updated > hex_updated
                except TypeError:
                    continue
                if slug_is_newer:
                    assignments = ", ".join(f"{column} = ?" for column in payload_columns)
                    conn.execute(
                        f"UPDATE questionnaire_pending SET {assignments} WHERE id = ?",
                        tuple(slug[column] for column in payload_columns) + (hex_row["id"],),
                    )
                conn.execute("DELETE FROM questionnaire_pending WHERE id = ?", (slug["id"],))
            return moved

    def questionnaire_resume_ids(self) -> set[str]:
        """Return every non-account key found in questionnaire history.

        This is used by the legacy rekey preflight. The config is an overlay,
        so a resume removed from it can still have a canonical key in durable
        questionnaire history.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT resume_id FROM questionnaire_scans
                   UNION
                   SELECT resume_id FROM questionnaire_pending"""
            ).fetchall()
        return {str(row[0]) for row in rows if row[0]}

    def questionnaire_answer_summary(
        self, resume_id: str | None = None, period: str = "all"
    ) -> dict[str, int]:
        """Return filled profile/LLM and unfilled counts from apply audits.

        Scoped the same way as ``summary()``/``reply_summary()`` (#473 cycle-review):
        ``resume_id=None`` means all resumes, ``period`` filters on
        ``questionnaire_scans.detected_at`` — otherwise a scoped ``stats
        --resume X --period 7d`` call would silently mix in lifetime,
        all-resume totals for this one line.
        """
        where = ["scan.source = 'apply'"]
        params: list = []
        if resume_id is not None:
            where.append("scan.resume_id = ?")
            params.append(resume_id)
        since = self._period_since(period)
        if since is not None:
            where.append("scan.detected_at >= ?")
            params.append(since)
        clause = " AND ".join(where)
        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT
                       COALESCE(SUM(filled = 1 AND answer_source = 'profile'), 0) AS profile,
                       COALESCE(SUM(filled = 1 AND answer_source = 'llm'), 0) AS llm,
                       COALESCE(SUM(filled = 0), 0) AS unanswered
                     FROM questionnaire_questions AS question
                     JOIN questionnaire_scans AS scan ON scan.id = question.scan_id
                    WHERE {clause}""",
                params,
            ).fetchone()
        return {key: int(row[key]) for key in ("profile", "llm", "unanswered")}

    # --- обучаемые шаблоны ответов на анкеты (#482) ---------------------
    #
    # Скоуп хранится строкой resume_id: '' — уровень аккаунта, непустая —
    # переопределение для конкретного резюме. Приоритет резюме над аккаунтом
    # реализован тем же приёмом, что и manual над hh_ru в get_profile_answers():
    # одна выборка с ORDER BY по признаку скоупа + setdefault, а не два запроса
    # с ручным слиянием.

    @staticmethod
    def _scope(resume_id: str | None) -> str:
        return resume_id or ""

    def set_questionnaire_template(
        self,
        template: str,
        *,
        mode: str,
        cluster: str = "mixed",
        answer: str | None = None,
        instruction: str | None = None,
        resume_id: str | None = None,
    ) -> None:
        """Создать или обновить шаблон в заданном скоупе."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO questionnaire_templates
                    (template, resume_id, cluster, mode, answer, instruction,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(template, resume_id) DO UPDATE SET
                    cluster = excluded.cluster,
                    mode = excluded.mode,
                    answer = excluded.answer,
                    instruction = excluded.instruction,
                    updated_at = excluded.updated_at
                """,
                (template, self._scope(resume_id), cluster, mode, answer, instruction, now, now),
            )

    def unset_questionnaire_template(self, template: str, *, resume_id: str | None = None) -> bool:
        """Удалить шаблон ТОЛЬКО из указанного скоупа.

        Как и ``profile unset``, снятие resume-переопределения не трогает
        account-строку: после него снова начинает действовать общий ответ.
        Подтверждённые формулировки того же скоупа удаляются вместе с шаблоном
        — иначе они продолжали бы направлять вопросы на несуществующий шаблон,
        и каждый такой вопрос падал бы в очередь с невнятной причиной.
        """
        scope = self._scope(resume_id)
        with self._connect() as conn:
            deleted = conn.execute(
                "DELETE FROM questionnaire_templates WHERE template = ? AND resume_id = ?",
                (template, scope),
            ).rowcount
            if deleted:
                conn.execute(
                    "DELETE FROM questionnaire_examples WHERE template = ? AND resume_id = ?",
                    (template, scope),
                )
        return bool(deleted)

    def get_questionnaire_templates(self, resume_id: str | None = None) -> dict[str, dict]:
        """Действующие шаблоны: переопределение резюме поверх ответа аккаунта."""
        scope = self._scope(resume_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT template, resume_id, cluster, mode, answer, instruction
                FROM questionnaire_templates
                WHERE resume_id = ? OR resume_id = ''
                ORDER BY template,
                         CASE WHEN resume_id = ? THEN 0 ELSE 1 END,
                         id
                """,
                (scope, scope),
            ).fetchall()
            examples = conn.execute(
                """
                SELECT template, question_text
                FROM questionnaire_examples
                WHERE resume_id = ? OR resume_id = ''
                ORDER BY id
                """,
                (scope,),
            ).fetchall()
        by_template: dict[str, dict] = {}
        for row in rows:
            by_template.setdefault(row["template"], dict(row))
        for row in examples:
            entry = by_template.get(row["template"])
            if entry is not None:
                entry.setdefault("examples", []).append(row["question_text"])
        return by_template

    def list_questionnaire_templates(self, resume_id: str | None = None) -> list[dict]:
        """Сырые строки обоих скоупов для отчёта ``questionnaire templates``."""
        where, params = "", []
        if resume_id is not None:
            where = "WHERE resume_id = ? OR resume_id = ''"
            params = [self._scope(resume_id)]
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT template, resume_id, cluster, mode, answer, instruction, updated_at
                    FROM questionnaire_templates
                    {where}
                    ORDER BY template, resume_id
                    """,
                    params,
                ).fetchall()
            ]

    def confirm_questionnaire_example(
        self,
        template: str,
        question_text: str,
        *,
        resume_id: str | None = None,
        confirmed_by: str = "user",
    ) -> None:
        """Записать подтверждённое сопоставление «формулировка -> шаблон»."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO questionnaire_examples
                    (template, resume_id, question_key, question_text, confirmed_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    template,
                    self._scope(resume_id),
                    normalize(question_text),
                    question_text,
                    confirmed_by,
                    datetime.now().isoformat(),
                ),
            )

    def get_confirmed_phrases(self, resume_id: str | None = None) -> dict[str, str]:
        """``{нормализованный текст вопроса: шаблон}`` — вход phrase-стратегии."""
        scope = self._scope(resume_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT question_key, template
                FROM questionnaire_examples
                WHERE resume_id = ? OR resume_id = ''
                ORDER BY question_key,
                         CASE WHEN resume_id = ? THEN 0 ELSE 1 END,
                         id
                """,
                (scope, scope),
            ).fetchall()
        phrases: dict[str, str] = {}
        for row in rows:
            phrases.setdefault(row["question_key"], row["template"])
        return phrases

    def record_questionnaire_pending(
        self,
        resume_id: str,
        items: list[dict],
        *,
        vacancy_id: str = "",
        vacancy_url: str = "",
        run_id: str | None = None,
    ) -> bool:
        """Поставить нерешённые вопросы в очередь. False при сбое SQLite.

        Возвращает bool, а не бросает: вызывающий (pipeline) обязан отличать
        «очередь не записана» от исключения, рвущего цикл откликов, — тот же
        контракт, что у ``_record_questionnaire_answers``.
        """
        if not items:
            return True
        now = datetime.now().isoformat()
        try:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO questionnaire_pending
                        (resume_id, vacancy_id, vacancy_url, question_key, question_text,
                         kind, is_radio, options_json, template, cluster, reason,
                         status, run_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    ON CONFLICT(resume_id, question_key) DO UPDATE SET
                        vacancy_id = excluded.vacancy_id,
                        vacancy_url = excluded.vacancy_url,
                        question_text = excluded.question_text,
                        kind = excluded.kind,
                        is_radio = excluded.is_radio,
                        options_json = excluded.options_json,
                        template = excluded.template,
                        cluster = excluded.cluster,
                        reason = excluded.reason,
                        status = 'pending',
                        run_id = excluded.run_id,
                        updated_at = excluded.updated_at
                    """,
                    [
                        (
                            resume_id,
                            vacancy_id,
                            vacancy_url,
                            normalize(str(item["text"])),
                            str(item["text"]),
                            str(item.get("kind", "text")),
                            int(bool(item.get("is_radio", False))),
                            json.dumps(list(item.get("options", ())), ensure_ascii=False),
                            item.get("template"),
                            item.get("cluster"),
                            str(item.get("reason", "")),
                            run_id,
                            now,
                            now,
                        )
                        for item in items
                    ],
                )
        except sqlite3.Error as exc:
            logger.warning("Не удалось записать очередь вопросов анкеты: %s", exc)
            return False
        return True

    def list_questionnaire_pending(
        self,
        resume_id: str | None = None,
        *,
        status: str = "pending",
        limit: int | None = None,
    ) -> list[dict]:
        where = ["status = ?"]
        params: list = [status]
        if resume_id is not None:
            where.append("resume_id = ?")
            params.append(resume_id)
        sql = f"""
            SELECT id, resume_id, vacancy_id, vacancy_url, question_key, question_text,
                   kind, is_radio, options_json, template, cluster, reason, status, updated_at
            FROM questionnaire_pending
            WHERE {" AND ".join(where)}
            ORDER BY id
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def resolve_questionnaire_pending(self, pending_id: int, *, status: str = "resolved") -> bool:
        with self._connect() as conn:
            return bool(
                conn.execute(
                    "UPDATE questionnaire_pending SET status = ?, updated_at = ? WHERE id = ?",
                    (status, datetime.now().isoformat(), pending_id),
                ).rowcount
            )

    def list_scanned_questions(self, resume_id: str | None = None) -> list[dict]:
        """Вопросы анкет из ранее собранных сканов (#482).

        Источник — ``questionnaire_scans``/``questionnaire_questions``, куда
        пишет read-only ``probe --questionnaires-only`` (#456). Нужен, чтобы
        ``questionnaire learn`` мог начаться на уже накопленных данных: без
        этого очередь пуста до первого боевого ``apply``, хотя сотня реальных
        вопросов уже лежит в базе.

        Дедупликация по нормализованному тексту: один и тот же вопрос
        встречается у десятков работодателей, и разбирать его нужно один раз.
        Берётся последняя встреча (``MAX(question.id)``) — у неё свежее
        привязка к вакансии.
        """
        where = []
        params: list = []
        if resume_id is not None:
            where.append("scan.resume_id = ?")
            params.append(resume_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT question.text, question.kind, question.is_radio,
                       question.options_json, scan.resume_id, scan.vacancy_id,
                       scan.vacancy_url, MAX(question.id) AS last_id
                FROM questionnaire_questions AS question
                JOIN questionnaire_scans AS scan ON scan.id = question.scan_id
                {clause}
                GROUP BY scan.resume_id, LOWER(TRIM(question.text))
                ORDER BY last_id
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_questionnaire_audit(
        self,
        resume_id: str | None = None,
        *,
        template: str | None = None,
        low_confidence: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        """Сохранённый аудит ответов на анкеты и исхода отклика (#488, #514).

        Только ``scan.source = 'apply'``: снимки ``probe --questionnaires-only``
        (#456) вопросы собирают, но ни на что не отвечают — колонки аудита у них
        пусты, и в отчёте «насколько верно бот ответил» им места нет. Это не
        недосмотр фильтра: probe-вопросы читаются отдельно
        (``list_scanned_questions``), и расширять выборку сюда не нужно.

        Дедупликации по тексту НЕТ, в отличие от ``list_scanned_questions``: там
        одна и та же формулировка разбирается один раз, здесь же каждый ответ
        привязан к своей вакансии и своему прогону — именно они и оцениваются.

        ``low_confidence=True`` отбирает строки с ``answer = ''`` — ответ,
        который резолвер намеренно НЕ записал (``pipeline.py`` ~246). Причина
        по ЭТОЙ строке не различима: ``answerer._queue`` отдаёт любому
        нерешённому вопросу ``AnswerProposal("", 0.0)``, и так пишется и ответ
        ниже порога, и отказ комплаенс-гейта, и «вопрос не сопоставлен ни с
        одним шаблоном». Отсюда формулировка флага «не стал отвечать», без
        указания причины: назвать здесь порог значило бы отправить оператора
        крутить ``llm_answer_threshold`` там, где порогом ничего не лечится.
        Сама причина в базе есть, но в другой таблице — ``questionnaire_pending
        .reason``; связать её с этой строкой можно текстом вопроса
        (``questionnaire pending``).

        Не ``filled = 0``: этот флаг батчевый, он пишется всему скану сразу,
        поэтому уверенные соседи неуверенного вопроса тоже равны нулю и попали
        бы в выборку ложно. Фильтровать по самому порогу нельзя: он живёт в
        ``AnswerProposal.threshold`` и в базу не пишется.

        Та же батчевость делает ``filled`` верным признаком ДРУГОГО вопроса —
        заполнялась ли форма вообще, — и командный слой печатает по нему
        ``[форма не заполнялась]``: вопрос «дошло ли до формы» тоже решается на
        весь скан. Исход отклика добавляется отдельно из ``actions`` по тройке
        ``run_id + resume_id + vacancy_id``. Поэтому заполненная форма может
        иметь любой из независимых исходов ``success``, ``uncertain`` или
        ``failed``.

        Для старых строк ``actions`` с ``run_id IS NULL`` точного связывания нет:
        при совпадении резюме и вакансии возвращается ``unknown``, а не ложное
        ``no_action``. Если подходящей строки actions вообще нет, возвращается
        ``no_action``. Джойн сворачивает несколько action-строк одной попытки к
        последней по ``id`` и не размножает вопросы одной анкеты.

        COALESCE не нужен, но инвариант держит ВЫЗЫВАЮЩИЙ, а не схема: колонка
        nullable, и ``answer IS NULL`` под этот предикат не попадёт. Пишет
        ``source = 'apply'`` только ``pipeline``, и он всегда подставляет
        ``answer``; ``probe`` ключи аудита опускает, но идёт с ``source =
        'probe'`` и отсекается первым условием. В таблицу такая строка всё
        равно попадёт как «[не заполнено]» — теряется только её выборка флагом.
        """
        where = ["scan.source = 'apply'"]
        params: list = []
        if resume_id is not None:
            where.append("scan.resume_id = ?")
            params.append(resume_id)
        if template is not None:
            where.append("question.template = ?")
            params.append(template)
        if low_confidence:
            where.append("question.answer = ''")
        # Свежие строки, а не первые попавшиеся: ``--last N`` про ПОСЛЕДНИЕ
        # ответы, и восходящий ORDER BY отрезал бы LIMIT-ом не тот конец.
        # Обратно в хронологический порядок разворачиваем уже после среза.
        sql = f"""
            WITH latest_apply AS (
                SELECT action.id, action.resume_id, action.vacancy_id,
                       action.run_id, action.status,
                       ROW_NUMBER() OVER (
                           PARTITION BY action.resume_id, action.vacancy_id, action.run_id
                           ORDER BY action.id DESC
                       ) AS row_number
                  FROM actions AS action
                 WHERE action.action = 'apply'
            ), legacy_apply AS (
                SELECT DISTINCT resume_id, vacancy_id
                  FROM actions
                 WHERE action = 'apply' AND run_id IS NULL
            )
            SELECT question.id, question.text, question.answer, question.answer_source,
                   question.confidence, question.filled, question.template,
                   question.cluster, question.resolver_source, question.run_id,
                   scan.resume_id, scan.vacancy_id, scan.vacancy_url,
                   scan.title, scan.company, scan.detected_at,
                   CASE
                       WHEN action.status IN ('success', 'uncertain', 'failed')
                           THEN action.status
                       WHEN legacy.resume_id IS NOT NULL THEN 'unknown'
                       WHEN action.id IS NOT NULL THEN 'unknown'
                       ELSE 'no_action'
                   END AS delivery_status
            FROM questionnaire_questions AS question
            JOIN questionnaire_scans AS scan ON scan.id = question.scan_id
            LEFT JOIN latest_apply AS action
              ON action.row_number = 1
             AND action.run_id IS NOT NULL
             AND action.run_id = question.run_id
             AND action.resume_id = scan.resume_id
             AND action.vacancy_id = scan.vacancy_id
            LEFT JOIN legacy_apply AS legacy
              ON legacy.resume_id = scan.resume_id
             AND legacy.vacancy_id = scan.vacancy_id
            WHERE {" AND ".join(where)}
            ORDER BY question.id DESC
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in reversed(rows)]

    def resolve_pending_for_templates(
        self, templates: set[str], *, resume_id: str | None = None
    ) -> int:
        """Пометить решёнными вопросы очереди, закреплённые за этими шаблонами.

        Вызывается после ``questionnaire set``: вопрос стоял в очереди с
        пометкой «шаблон найден, но ответа нет» — теперь ответ есть, и держать
        его нерешённым незачем. Помечаются только строки, у которых шаблон
        совпадает: вопросы без сопоставления (``template IS NULL``) остаются в
        очереди — для них по-прежнему неизвестно, что отвечать.
        """
        if not templates:
            return 0
        placeholders = ",".join("?" for _ in templates)
        sql = (
            f"UPDATE questionnaire_pending SET status = 'resolved', updated_at = ? "
            f"WHERE status = 'pending' AND template IN ({placeholders})"
        )
        params: list = [datetime.now().isoformat(), *sorted(templates)]
        if resume_id is not None:
            sql += " AND resume_id = ?"
            params.append(resume_id)
        with self._connect() as conn:
            return conn.execute(sql, params).rowcount

    def resolve_pending_for_questions(
        self, question_texts: list[str], *, resume_id: str | None = None
    ) -> int:
        """Пометить решёнными вопросы очереди с этими формулировками (#486 п.2).

        Дополняет ``resolve_pending_for_templates``, которая матчит по имени
        шаблона: вопрос, не совпавший НИ с одним шаблоном, стоит в очереди с
        ``template IS NULL``, и снять его по имени нечем. Именно так туда
        попадает комплаенс-вопрос, ради которого ``set --example`` и нужен —
        подтверждённая формулировка и есть то, что делает шаблон применимым.

        Сопоставление по ``question_key`` (``normalize(text)``) — тому же ключу,
        которым ``confirm_questionnaire_example`` пишет пример, а
        ``record_questionnaire_pending`` — строку очереди.
        """
        keys = {normalize(text) for text in question_texts if text.strip()}
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        sql = (
            f"UPDATE questionnaire_pending SET status = 'resolved', updated_at = ? "
            f"WHERE status = 'pending' AND question_key IN ({placeholders})"
        )
        params: list = [datetime.now().isoformat(), *sorted(keys)]
        if resume_id is not None:
            sql += " AND resume_id = ?"
            params.append(resume_id)
        with self._connect() as conn:
            return conn.execute(sql, params).rowcount

    def clear_pending_skips(self, resume_id: str | None = None) -> int:
        """Снять skip-записи, поставленные из-за очереди анкет (#482).

        Вызывается после обучения шаблона: вакансия была пропущена только
        потому, что бот не знал ответа, и теперь знает. Удаляются исключительно
        строки с причиной ``questionnaire_pending`` — прочие skip'ы (стоп-слова,
        уже откликались, низкая уверенность LLM) остаются нетронутыми, иначе
        обучение одного шаблона молча воскрешало бы вакансии, отсеянные совсем
        по другим основаниям.

        Разблокируется вакансия, у которой в очереди есть решённые вопросы и не
        осталось нерешённых. Одна анкета часто содержит несколько неизвестных
        вопросов, и обучение одного шаблона не делает её проходимой:
        безусловная разблокировка отправляла бы бота открывать ту же форму
        снова и снова, тратя запросы к hh.ru (а они здесь — троттлинг-бюджет)
        ради заведомо повторного пропуска.

        Требование «есть решённые» — не придирка, а следствие дедупликации
        очереди по ``(resume_id, question_key)``: один и тот же вопрос у десяти
        работодателей держит в очереди ОДНУ строку, с ``vacancy_id`` последней
        встреченной вакансии. Проверка «нет нерешённых» сама по себе выпускала
        бы все девять остальных, хотя их общий вопрос ещё не разобран.
        Вакансии, которых очередь не знает вовсе (запись до #482 или ручная
        чистка), остаются в ``skipped`` и снимаются обычным ``clear-skipped`` —
        автоматика не должна гадать за пределами своих данных.
        """
        sql = """
            DELETE FROM skipped
            WHERE reason = ?
              AND EXISTS (
                  SELECT 1 FROM questionnaire_pending AS q
                  WHERE q.resume_id = skipped.resume_id
                    AND q.vacancy_id = skipped.vacancy_id
                    AND q.status <> 'pending'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM questionnaire_pending AS q
                  WHERE q.resume_id = skipped.resume_id
                    AND q.vacancy_id = skipped.vacancy_id
                    AND q.status = 'pending'
              )
        """
        params: list = [SKIP_REASONS.QUESTIONNAIRE_PENDING]
        if resume_id is not None:
            sql += " AND resume_id = ?"
            params.append(resume_id)
        with self._connect() as conn:
            return conn.execute(sql, params).rowcount

    def robot_questionnaire_row(self, topic: str) -> dict | None:
        """Строка очереди робот-анкет по topic (None — в очереди нет)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT topic, vacancy_id, reason, detected_at, resolved_at, answer "
                "FROM robot_questionnaires WHERE topic = ?",
                (topic,),
            ).fetchone()
        return dict(row) if row else None

    def is_robot_questionnaire(self, topic: str) -> bool:
        """Только НЕрезолвнутые строки: после ``robot-reply`` чат перестаёт
        вечно скипаться в reply-employers (resolved_at IS NULL — в очереди)."""
        with self._connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM robot_questionnaires WHERE topic = ? AND resolved_at IS NULL",
                    (topic,),
                ).fetchone()
                is not None
            )

    def resolve_robot_questionnaire(self, topic: str, *, answer: str) -> None:
        """Пометить робот-анкету отвеченной (колонкой, не DELETE — таблица
        append-only, факт обнаружения хранит аудит).

        Повторный вызов легален: живые анкеты многошаговые — робот задаёт
        следующий вопрос после нашего ответа, и resolve перезаписывается
        свежим ответом (робот-кейс 2026-09-08: вопрос №2 через минуту после
        ответа №1). Дедуп повторного КЛИКА держит не эта таблица, а
        replies/has_replied по inbound-маркеру нового вопроса. Fail-closed
        остаётся для неизвестного topic (rowcount != 1 → ValueError).
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE robot_questionnaires SET resolved_at = ?, answer = ? WHERE topic = ?",
                (now, answer, topic),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"robot_questionnaires: нет строки topic={topic!r}")

    def list_robot_questionnaires(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT topic, vacancy_id, reason, detected_at, resolved_at, answer "
                    "FROM robot_questionnaires "
                    "ORDER BY detected_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]

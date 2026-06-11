"""Своя БД эссеист-бота (pending_drafts + tenant-слой). НИ БАЙТА в базу twidgest.

Ключевая защита (Момент 1): атомарный дедуп публикации через статус-машину.
claim_for_publish переводит pending→publishing ровно один раз; повторное нажатие
получит False и ничего не отправит. Плюс append-only аудит-лог опубликованного.

Tenant-слой (вариант B): допуск к Essayist Pro (essayist_users) и per-channel
конфиг автоподбора (essay_config) живут ЗДЕСЬ; владение каналом читается из
twidgest read-only (candidates). Связующий ключ — Telegram user id.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

import aiosqlite

REJECT_REASONS = ("факт-ошибка", "стиль", "не интересно", "другое")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_plus_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _not_expired(expires_at: str | None) -> bool:
    """True если срок не задан (бессрочно) или ещё не истёк. Битую дату трактуем как активную."""
    if not expires_at:
        return True
    try:
        exp = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


@dataclass
class Draft:
    id: int
    channel_id: int
    owner_user_id: int | None
    tweet_id: str
    tweet_text: str
    author: str | None
    niche: str
    target_chat_id: int | None
    title: str
    brief: str
    draft: str
    violations: list[dict]
    total_searches: int
    status: str
    revision_count: int
    reject_reason: str | None
    published_message_id: int | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    owner_user_id INTEGER,
    tweet_id TEXT,
    tweet_text TEXT,
    author TEXT,
    niche TEXT,
    target_chat_id INTEGER,
    title TEXT,
    brief TEXT DEFAULT '',
    draft TEXT DEFAULT '',
    violations_json TEXT DEFAULT '[]',
    total_searches INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    revision_count INTEGER DEFAULT 0,
    reject_reason TEXT,
    published_message_id INTEGER,
    published_at TEXT,
    created_at TEXT,
    decided_at TEXT
);
"""


_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS channel_flags (channel_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 1);
"""


# Tenant-слой. Создаётся ПОСЛЕ миграции owner_user_id — индекс ниже на неё опирается.
_TENANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_users (
    user_id    INTEGER PRIMARY KEY,          -- нажал /start → боту можно писать первым
    started_at TEXT
);
CREATE TABLE IF NOT EXISTS essayist_users (
    user_id    INTEGER PRIMARY KEY,          -- == twidgest tg_user_id
    enabled    INTEGER NOT NULL DEFAULT 0,   -- допуск к Essayist Pro
    note       TEXT,
    granted_at TEXT,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS essay_costs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    ok         INTEGER NOT NULL,
    searches   INTEGER NOT NULL DEFAULT 0,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_costs_created ON essay_costs(created_at);
CREATE TABLE IF NOT EXISTS essay_config (
    channel_id      INTEGER PRIMARY KEY,      -- id канала в twidgest
    user_id         INTEGER NOT NULL,         -- владелец (кэш из twidgest, для фильтра)
    enabled         INTEGER NOT NULL DEFAULT 0,   -- ОПТ-ИН: по умолчанию выкл
    frequency_hours INTEGER NOT NULL DEFAULT 12,
    mode            TEXT NOT NULL DEFAULT 'hil',  -- hil | auto
    last_run_at     TEXT,
    last_error      TEXT
);
CREATE TABLE IF NOT EXISTS skipped_topics (
    channel_id INTEGER NOT NULL,
    tweet_id   TEXT NOT NULL,
    reason     TEXT,
    ts         TEXT,
    PRIMARY KEY (channel_id, tweet_id)
);
CREATE INDEX IF NOT EXISTS idx_essay_config_user ON essay_config(user_id);
CREATE INDEX IF NOT EXISTS idx_pending_owner ON pending_drafts(owner_user_id, status);
"""


class Store:
    """status: pending | publishing | published | rejected | regenerating"""

    def __init__(self, db_path: str, publish_log: str = "publish_log.jsonl") -> None:
        self.db_path = db_path
        self.publish_log = publish_log

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.executescript(_SETTINGS_SCHEMA)
            await self._migrate_pending(db)  # owner_user_id в старой pending_drafts
            await db.executescript(_TENANT_SCHEMA)
            await self._migrate_tenant(db)   # last_error в старой essay_config
            await db.commit()

    async def _migrate_pending(self, db) -> None:
        """Идемпотентно: owner_user_id в уже существующей pending_drafts."""
        cur = await db.execute("PRAGMA table_info(pending_drafts)")
        cols = {r[1] for r in await cur.fetchall()}
        if "owner_user_id" not in cols:
            await db.execute("ALTER TABLE pending_drafts ADD COLUMN owner_user_id INTEGER")

    async def _migrate_tenant(self, db) -> None:
        """Идемпотентно: last_error в essay_config и expires_at в essayist_users."""
        cur = await db.execute("PRAGMA table_info(essay_config)")
        cols = {r[1] for r in await cur.fetchall()}
        if "last_error" not in cols:
            await db.execute("ALTER TABLE essay_config ADD COLUMN last_error TEXT")
        cur = await db.execute("PRAGMA table_info(essayist_users)")
        ucols = {r[1] for r in await cur.fetchall()}
        if "expires_at" not in ucols:
            await db.execute("ALTER TABLE essayist_users ADD COLUMN expires_at TEXT")
        if "plan" not in ucols:
            await db.execute("ALTER TABLE essayist_users ADD COLUMN plan TEXT DEFAULT 'manual'")
        if "essays_used" not in ucols:
            await db.execute(
                "ALTER TABLE essayist_users ADD COLUMN essays_used INTEGER NOT NULL DEFAULT 0")

    async def create_draft(self, *, channel_id, tweet_id, tweet_text, author, niche,
                           target_chat_id, title, brief, draft, violations,
                           total_searches, owner_user_id=None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO pending_drafts (channel_id, owner_user_id, tweet_id, tweet_text, "
                "author, niche, target_chat_id, title, brief, draft, violations_json, "
                "total_searches, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                (channel_id, owner_user_id, tweet_id, tweet_text, author, niche, target_chat_id,
                 title, brief, draft, json.dumps(violations, ensure_ascii=False),
                 total_searches, _now()),
            )
            await db.commit()
            return cur.lastrowid

    async def get(self, draft_id: int) -> Draft | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM pending_drafts WHERE id = ?", (draft_id,))).fetchone()
        if not row:
            return None
        return Draft(
            id=row["id"], channel_id=row["channel_id"], owner_user_id=row["owner_user_id"],
            tweet_id=row["tweet_id"], tweet_text=row["tweet_text"], author=row["author"],
            niche=row["niche"], target_chat_id=row["target_chat_id"], title=row["title"],
            brief=row["brief"], draft=row["draft"],
            violations=json.loads(row["violations_json"] or "[]"),
            total_searches=row["total_searches"], status=row["status"],
            revision_count=row["revision_count"], reject_reason=row["reject_reason"],
            published_message_id=row["published_message_id"],
        )

    async def claim_for_publish(self, draft_id: int) -> bool:
        """Атомарно pending→publishing. True ровно один раз; повтор → False."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE pending_drafts SET status='publishing' "
                "WHERE id=? AND status='pending'", (draft_id,))
            await db.commit()
            return cur.rowcount == 1

    async def finalize_publish(self, draft_id: int, message_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE pending_drafts SET status='published', published_message_id=?, "
                "published_at=?, decided_at=? WHERE id=?",
                (message_id, _now(), _now(), draft_id))
            await db.commit()
            row = await (await db.execute(
                "SELECT channel_id, tweet_id, target_chat_id, owner_user_id FROM pending_drafts WHERE id=?",
                (draft_id,))).fetchone()
        with open(self.publish_log, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _now(), "draft_id": draft_id, "message_id": message_id,
                "channel_id": row[0], "tweet_id": row[1], "target_chat_id": row[2],
                "owner_user_id": row[3], "sender": "essayist-bot",
            }, ensure_ascii=False) + "\n")

    async def revert_publish(self, draft_id: int) -> None:
        """Откат publishing→pending, если отправка упала."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE pending_drafts SET status='pending' "
                "WHERE id=? AND status='publishing'", (draft_id,))
            await db.commit()

    async def reject(self, draft_id: int, reason: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE pending_drafts SET status='rejected', reject_reason=?, decided_at=? "
                "WHERE id=? AND status IN ('pending','regenerating')",
                (reason, _now(), draft_id))
            await db.commit()
            return cur.rowcount == 1

    async def begin_revision(self, draft_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE pending_drafts SET status='regenerating', "
                "revision_count=revision_count+1 WHERE id=? AND status='pending'",
                (draft_id,))
            await db.commit()
            return cur.rowcount == 1

    async def apply_revision(self, draft_id, new_draft, new_violations) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE pending_drafts SET draft=?, violations_json=?, status='pending' "
                "WHERE id=?",
                (new_draft, json.dumps(new_violations, ensure_ascii=False), draft_id))
            await db.commit()

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT value FROM settings WHERE key=?", (key,))).fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))
            await db.commit()

    async def seen_tweet(self, channel_id: int, tweet_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT 1 FROM pending_drafts WHERE channel_id=? AND tweet_id=? LIMIT 1",
                (channel_id, tweet_id))).fetchone()
        return row is not None

    async def set_channel_enabled(self, channel_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO channel_flags(channel_id, enabled) VALUES(?, ?)",
                (channel_id, 1 if enabled else 0))
            await db.commit()

    async def disabled_channels(self) -> set[int]:
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (await db.execute(
                "SELECT channel_id FROM channel_flags WHERE enabled=0")).fetchall()
        return {r[0] for r in rows}

    async def published_tweets(self, channel_id: int) -> set[str]:
        """tweet_id уже опубликованных разборов канала — для фильтра /essay."""
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (await db.execute(
                "SELECT tweet_id FROM pending_drafts "
                "WHERE channel_id=? AND status='published'", (channel_id,))).fetchall()
        return {r[0] for r in rows}

    # ---------------------------------------------------------------- tenant-слой
    # Допуск к Essayist Pro (entitlement). Управляет суперадмин; позже — биллинг.

    async def user_enabled(self, user_id: int) -> bool:
        """Допущен И срок не истёк. expires_at IS NULL = бессрочно."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT enabled, expires_at FROM essayist_users WHERE user_id=?",
                (user_id,))).fetchone()
        if not row or not row[0]:
            return False
        return _not_expired(row[1])

    # ------------------------------------------------ квоты и триал (этап D)
    # plan: 'trial' (7д/20), 'paid' (30д/30), 'manual' (выдан руками: без квоты).
    PLAN_QUOTA = {"trial": 20, "paid": 20}

    async def ensure_trial(self, user_id: int, days: int = 7) -> bool:
        """Авто-триал при первом контакте. True — триал только что выдан.

        Сам факт строки в essayist_users = триал уже использован: повторный
        триал после истечения НЕ выдаётся (вернёт False, ничего не меняя).
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO essayist_users(user_id, enabled, note, granted_at, "
                "expires_at, plan, essays_used) "
                "VALUES(?,1,'auto-trial',?,?,'trial',0) "
                "ON CONFLICT(user_id) DO NOTHING",
                (user_id, _now(), _now_plus_days(days)))
            await db.commit()
            return cur.rowcount > 0

    async def quota_state(self, user_id: int) -> tuple[str, int, int, str | None]:
        """(plan, used, quota, expires_at). quota=-1 — безлимит (manual/нет строки)."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT plan, essays_used, expires_at FROM essayist_users "
                "WHERE user_id=?", (user_id,))).fetchone()
        if not row:
            return ("none", 0, -1, None)
        plan = row[0] or "manual"
        quota = self.PLAN_QUOTA.get(plan, -1)
        return (plan, int(row[1] or 0), quota, row[2])

    async def consume_essay(self, user_id: int) -> tuple[bool, int, int]:
        """Атомарно списывает 1 разбор. (ok, used_after, quota).

        manual-план (выдан руками) — без квоты, всегда ok. Превышение —
        (False, used, quota), счётчик не растёт.
        """
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT plan, essays_used FROM essayist_users WHERE user_id=?",
                (user_id,))).fetchone()
            if not row:
                return (False, 0, 0)
            plan = row[0] or "manual"
            quota = self.PLAN_QUOTA.get(plan, -1)
            if quota < 0:
                return (True, int(row[1] or 0), -1)
            cur = await db.execute(
                "UPDATE essayist_users SET essays_used = essays_used + 1 "
                "WHERE user_id=? AND essays_used < ?", (user_id, quota))
            await db.commit()
            if cur.rowcount == 0:
                return (False, int(row[1] or 0), quota)
            return (True, int(row[1] or 0) + 1, quota)

    # ------------------------------------------------ себестоимость (учёт)
    async def record_cost(self, channel_id: int, user_id: int, ok: bool,
                          searches: int, tokens_in: int, tokens_out: int) -> None:
        """Одна строка на каждую генерацию (включая неудачные — они тоже стоят денег)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO essay_costs(channel_id, user_id, ok, searches, "
                "tokens_in, tokens_out, created_at) VALUES(?,?,?,?,?,?,?)",
                (channel_id, user_id, 1 if ok else 0, searches,
                 tokens_in, tokens_out, _now()))
            await db.commit()

    async def costs_since(self, since_iso: str) -> list[dict]:
        """Агрегат по каналам с момента since: генерации, успехи, поиски, токены."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT channel_id, COUNT(*) AS runs, SUM(ok) AS ok_runs, "
                "SUM(searches) AS searches, SUM(tokens_in) AS tin, "
                "SUM(tokens_out) AS tout FROM essay_costs "
                "WHERE created_at >= ? GROUP BY channel_id ORDER BY runs DESC",
                (since_iso,))).fetchall()
        return [dict(r) for r in rows]

    async def refund_essay(self, user_id: int) -> None:
        """Возврат списания (генерация не удалась). Безопасно для любых планов."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE essayist_users SET essays_used = MAX(essays_used - 1, 0) "
                "WHERE user_id=?", (user_id,))
            await db.commit()

    async def activate_paid(self, user_id: int, days: int = 30) -> str:
        """Оплата: +days от конца текущего срока (или от now), квота 30, счётчик 0."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT expires_at FROM essayist_users WHERE user_id=?",
                (user_id,))).fetchone()
        base = datetime.now(timezone.utc)
        if row and row[0]:
            try:
                cur_exp = datetime.fromisoformat(row[0])
                if cur_exp.tzinfo is None:
                    cur_exp = cur_exp.replace(tzinfo=timezone.utc)
                base = max(base, cur_exp)
            except ValueError:
                pass
        new_exp = (base + timedelta(days=days)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO essayist_users(user_id, enabled, note, granted_at, "
                "expires_at, plan, essays_used) VALUES(?,1,'paid',?,?,'paid',0) "
                "ON CONFLICT(user_id) DO UPDATE SET enabled=1, plan='paid', "
                "essays_used=0, expires_at=?, note='paid'",
                (user_id, _now(), new_exp, new_exp))
            await db.commit()
        return new_exp

    async def set_user_enabled(self, user_id: int, enabled: bool, note: str | None = None,
                               days: int | None = None) -> None:
        """Выдать/снять допуск. days=N → срок now+N; days=None → бессрочно (expires_at NULL).
        При снятии (enabled=False) срок обнуляется."""
        expires = _now_plus_days(days) if (enabled and days is not None) else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO essayist_users(user_id, enabled, note, granted_at, expires_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled, "
                "note=excluded.note, expires_at=excluded.expires_at",
                (user_id, 1 if enabled else 0, note, _now(), expires))
            await db.commit()

    async def list_entitled(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT user_id, enabled, note, granted_at, expires_at FROM essayist_users "
                "ORDER BY user_id")).fetchall()
        return [dict(r) for r in rows]

    async def get_entitlement(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT user_id, enabled, note, granted_at, expires_at FROM essayist_users "
                "WHERE user_id=?", (user_id,))).fetchone()
        return dict(row) if row else None

    # Реестр /start: кому бот может писать первым (Telegram запрещает иначе).

    async def mark_started(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO bot_users(user_id, started_at) VALUES(?, ?)",
                (user_id, _now()))
            await db.commit()

    async def is_started(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT 1 FROM bot_users WHERE user_id=?", (user_id,))).fetchone()
        return row is not None

    # Per-channel конфиг автоподбора (заменяет глобальные settings/channel_flags).

    async def _ensure_cfg(self, db, channel_id: int, user_id: int) -> None:
        await db.execute(
            "INSERT OR IGNORE INTO essay_config(channel_id, user_id) VALUES(?, ?)",
            (channel_id, user_id))

    async def get_essay_config(self, channel_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM essay_config WHERE channel_id=?", (channel_id,))).fetchone()
        return dict(row) if row else None

    async def set_essay_enabled(self, channel_id: int, user_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_cfg(db, channel_id, user_id)
            await db.execute("UPDATE essay_config SET enabled=?, user_id=? WHERE channel_id=?",
                             (1 if enabled else 0, user_id, channel_id))
            await db.commit()

    async def set_essay_frequency(self, channel_id: int, user_id: int, hours: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_cfg(db, channel_id, user_id)
            await db.execute("UPDATE essay_config SET frequency_hours=? WHERE channel_id=?",
                             (hours, channel_id))
            await db.commit()

    async def set_essay_mode(self, channel_id: int, user_id: int, mode: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_cfg(db, channel_id, user_id)
            await db.execute("UPDATE essay_config SET mode=? WHERE channel_id=?",
                             (mode, channel_id))
            await db.commit()

    async def touch_essay_run(self, channel_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE essay_config SET last_run_at=? WHERE channel_id=?",
                             (_now(), channel_id))
            await db.commit()

    async def set_essay_error(self, channel_id: int, error: str | None) -> None:
        """Отметка последней ошибки доставки (None — очистить). Для /timer-диагностики."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE essay_config SET last_error=? WHERE channel_id=?",
                             (error, channel_id))
            await db.commit()

    async def enabled_essay_channels(self) -> list[dict]:
        """Каналы с включённым автоподбором — для таймера (с владельцем и расписанием)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT channel_id, user_id, frequency_hours, mode, last_run_at "
                "FROM essay_config WHERE enabled=1")).fetchall()
        return [dict(r) for r in rows]

    # Пропущенные темы автоподбора (напр. непоисковые мемы) — чтобы таймер
    # не возвращался к ним в следующих тиках.

    async def mark_skipped(self, channel_id: int, tweet_id: str, reason: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO skipped_topics(channel_id, tweet_id, reason, ts) "
                "VALUES(?,?,?,?)", (channel_id, tweet_id, reason, _now()))
            await db.commit()

    async def is_skipped(self, channel_id: int, tweet_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT 1 FROM skipped_topics WHERE channel_id=? AND tweet_id=? LIMIT 1",
                (channel_id, tweet_id))).fetchone()
        return row is not None

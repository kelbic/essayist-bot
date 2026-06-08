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
from datetime import datetime, timezone
from dataclasses import dataclass

import aiosqlite

REJECT_REASONS = ("факт-ошибка", "стиль", "не интересно", "другое")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
CREATE TABLE IF NOT EXISTS essayist_users (
    user_id    INTEGER PRIMARY KEY,          -- == twidgest tg_user_id
    enabled    INTEGER NOT NULL DEFAULT 0,   -- допуск к Essayist Pro
    note       TEXT,
    granted_at TEXT
);
CREATE TABLE IF NOT EXISTS essay_config (
    channel_id      INTEGER PRIMARY KEY,      -- id канала в twidgest
    user_id         INTEGER NOT NULL,         -- владелец (кэш из twidgest, для фильтра)
    enabled         INTEGER NOT NULL DEFAULT 0,   -- ОПТ-ИН: по умолчанию выкл
    frequency_hours INTEGER NOT NULL DEFAULT 12,
    mode            TEXT NOT NULL DEFAULT 'hil',  -- hil | auto
    last_run_at     TEXT,
    last_error      TEXT
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
        """Идемпотентно: last_error в уже существующей essay_config."""
        cur = await db.execute("PRAGMA table_info(essay_config)")
        cols = {r[1] for r in await cur.fetchall()}
        if "last_error" not in cols:
            await db.execute("ALTER TABLE essay_config ADD COLUMN last_error TEXT")

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
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT enabled FROM essayist_users WHERE user_id=?", (user_id,))).fetchone()
        return bool(row and row[0])

    async def set_user_enabled(self, user_id: int, enabled: bool, note: str | None = None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO essayist_users(user_id, enabled, note, granted_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled, note=excluded.note",
                (user_id, 1 if enabled else 0, note, _now()))
            await db.commit()

    async def list_entitled(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT user_id, enabled, note, granted_at FROM essayist_users "
                "ORDER BY user_id")).fetchall()
        return [dict(r) for r in rows]

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

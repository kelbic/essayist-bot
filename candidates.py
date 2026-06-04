"""Чтение кандидатов из базы Twidgest — ТОЛЬКО НА ЧТЕНИЕ."""
from __future__ import annotations

from dataclasses import dataclass

import aiosqlite


@dataclass
class Candidate:
    channel_id: int
    title: str
    niche: str
    target_chat_id: int | None
    tweet_id: str
    author: str
    text: str
    url: str
    likes: int
    retweets: int


@dataclass
class ChannelInfo:
    channel_id: int
    title: str
    niche: str
    target_chat_id: int | None


def _ro_uri(path: str) -> str:
    return f"file:{path}?mode=ro"


_SELECT = (
    "SELECT c.id, c.title, c.niche, c.target_chat_id, "
    "       q.tweet_id, q.twitter_username, q.text, q.url, q.likes, q.retweets "
    "FROM digest_queue q JOIN channels c ON c.id = q.channel_id "
)


def _row_to_candidate(r) -> Candidate:
    return Candidate(channel_id=r[0], title=r[1], niche=r[2], target_chat_id=r[3],
                     tweet_id=r[4], author=r[5], text=r[6], url=r[7], likes=r[8], retweets=r[9])


async def list_channels(db_path: str) -> list[ChannelInfo]:
    sql = ("SELECT id, title, niche, target_chat_id FROM channels "
           "WHERE is_active = 1 AND target_chat_id IS NOT NULL ORDER BY id")
    async with aiosqlite.connect(_ro_uri(db_path), uri=True) as db:
        rows = await (await db.execute(sql)).fetchall()
    return [ChannelInfo(r[0], r[1], r[2], r[3]) for r in rows]


async def top_candidates(db_path: str, channel_id: int, limit: int = 5) -> list[Candidate]:
    """Топ-N виральных твитов канала по (likes + retweets*3) — для ручного выбора."""
    sql = _SELECT + ("WHERE q.channel_id = ? "
                     "ORDER BY (q.likes + q.retweets * 3) DESC LIMIT ?")
    async with aiosqlite.connect(_ro_uri(db_path), uri=True) as db:
        rows = await (await db.execute(sql, (channel_id, limit))).fetchall()
    return [_row_to_candidate(r) for r in rows]


async def top_candidate(db_path: str, channel_id: int) -> Candidate | None:
    res = await top_candidates(db_path, channel_id, limit=1)
    return res[0] if res else None


async def get_by_tweet(db_path: str, channel_id: int, tweet_id: str) -> Candidate | None:
    """Конкретный твит по id — для генерации после выбора кнопкой."""
    sql = _SELECT + "WHERE q.channel_id = ? AND q.tweet_id = ? LIMIT 1"
    async with aiosqlite.connect(_ro_uri(db_path), uri=True) as db:
        row = await (await db.execute(sql, (channel_id, tweet_id))).fetchone()
    return _row_to_candidate(row) if row else None


async def get_channel(db_path: str, channel_id: int) -> ChannelInfo | None:
    """Метаданные одного канала по id — для /essay со своей темой."""
    sql = "SELECT id, title, niche, target_chat_id FROM channels WHERE id = ? LIMIT 1"
    async with aiosqlite.connect(_ro_uri(db_path), uri=True) as db:
        row = await (await db.execute(sql, (channel_id,))).fetchone()
    return ChannelInfo(row[0], row[1], row[2], row[3]) if row else None

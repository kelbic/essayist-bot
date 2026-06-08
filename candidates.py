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
    user_id: int            # владелец канала в twidgest (tg_user_id) — для проверки прав
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


# Колонки канала в едином порядке — один _row_to_channel на все запросы.
_CH_COLS = "id, user_id, title, niche, target_chat_id"


def _row_to_channel(r) -> ChannelInfo:
    return ChannelInfo(channel_id=r[0], user_id=r[1], title=r[2], niche=r[3], target_chat_id=r[4])


async def list_channels(db_path: str) -> list[ChannelInfo]:
    """Все активные каналы (полный список — только для суперадмина)."""
    sql = (f"SELECT {_CH_COLS} FROM channels "
           "WHERE is_active = 1 AND target_chat_id IS NOT NULL ORDER BY id")
    async with aiosqlite.connect(_ro_uri(db_path), uri=True) as db:
        rows = await (await db.execute(sql)).fetchall()
    return [_row_to_channel(r) for r in rows]


async def channels_for_user(db_path: str, user_id: int) -> list[ChannelInfo]:
    """Только каналы, которыми владеет конкретный пользователь twidgest."""
    sql = (f"SELECT {_CH_COLS} FROM channels "
           "WHERE user_id = ? AND is_active = 1 AND target_chat_id IS NOT NULL ORDER BY id")
    async with aiosqlite.connect(_ro_uri(db_path), uri=True) as db:
        rows = await (await db.execute(sql, (user_id,))).fetchall()
    return [_row_to_channel(r) for r in rows]


async def top_candidates(db_path: str, channel_id: int, limit: int = 5,
                         max_age_hours: int | None = None) -> list[Candidate]:
    """Топ-N виральных твитов канала по (likes + retweets*3).

    max_age_hours: окно свежести по queued_at. None = без окна (любой возраст) —
    так работает ручной /essay. Автоподбор передаёт окно (берёт только свежее),
    чтобы залайканное старьё не всплывало само. int() защищает datetime-модификатор
    от инъекции (его нельзя забиндить плейсхолдером).
    """
    where = "WHERE q.channel_id = ? "
    params: list = [channel_id]
    if max_age_hours is not None:
        where += f"AND q.queued_at >= datetime('now', '-{int(max_age_hours)} hours') "
    sql = _SELECT + where + "ORDER BY (q.likes + q.retweets * 3) DESC LIMIT ?"
    params.append(limit)
    async with aiosqlite.connect(_ro_uri(db_path), uri=True) as db:
        rows = await (await db.execute(sql, params)).fetchall()
    return [_row_to_candidate(r) for r in rows]


async def top_candidate(db_path: str, channel_id: int,
                        max_age_hours: int | None = None) -> Candidate | None:
    res = await top_candidates(db_path, channel_id, limit=1, max_age_hours=max_age_hours)
    return res[0] if res else None


async def get_by_tweet(db_path: str, channel_id: int, tweet_id: str) -> Candidate | None:
    """Конкретный твит по id — для генерации после выбора кнопкой."""
    sql = _SELECT + "WHERE q.channel_id = ? AND q.tweet_id = ? LIMIT 1"
    async with aiosqlite.connect(_ro_uri(db_path), uri=True) as db:
        row = await (await db.execute(sql, (channel_id, tweet_id))).fetchone()
    return _row_to_candidate(row) if row else None


async def get_channel(db_path: str, channel_id: int) -> ChannelInfo | None:
    """Метаданные канала по id — для /essay со своей темой и проверки владения."""
    sql = f"SELECT {_CH_COLS} FROM channels WHERE id = ? LIMIT 1"
    async with aiosqlite.connect(_ro_uri(db_path), uri=True) as db:
        row = await (await db.execute(sql, (channel_id,))).fetchone()
    return _row_to_channel(row) if row else None

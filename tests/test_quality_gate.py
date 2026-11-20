"""Quality Gate v1 в essayist: Substance Gate (Фикс 1) и атрибуция витрины (Фикс 3).

Зеркало правил twidgest (core/quality_gate, core/attribution). База twidgest — временная
SQLite в двух вариантах: после миграции (есть substance/attribution_mode) и до неё.
Сети нет: Telegram и Anthropic не вызываются.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="essayist-qg-")
os.environ.setdefault("ESSBOT_DB", os.path.join(_TMP, "essayist.db"))
os.environ.setdefault("ADMIN_USER_ID", "1")
os.environ.setdefault("TWIDGEST_BOT_USERNAME", "TwidgestBot")

import bot  # noqa: E402
import candidates  # noqa: E402
from essayist import find_teaser_admission  # noqa: E402


def _twidgest_db(path: str, migrated: bool) -> str:
    db = sqlite3.connect(path)
    extra_ch = ", attribution_mode VARCHAR(16)" if migrated else ""
    extra_q = ", substance VARCHAR(16)" if migrated else ""
    db.executescript(f"""
        CREATE TABLE channels (id INTEGER PRIMARY KEY, user_id BIGINT, title TEXT, niche TEXT,
            target_chat_id BIGINT, is_active BOOLEAN, min_interest INTEGER,
            paid_until DATETIME, trial_until DATETIME{extra_ch});
        CREATE TABLE digest_queue (id INTEGER PRIMARY KEY, channel_id INTEGER, tweet_id TEXT,
            twitter_username TEXT, text TEXT, url TEXT, likes INTEGER, retweets INTEGER,
            interest_score INTEGER, queued_at DATETIME DEFAULT CURRENT_TIMESTAMP{extra_q});
    """)
    db.execute("INSERT INTO channels (id, user_id, title, niche, target_chat_id, is_active, "
               "min_interest) VALUES (6, 1, 'AI', 'tech_ai', -100, 1, 0)")
    rows = [("ok", 100), ("teaser", 90), ("unnamed", 80), (None, 70)]
    for i, (sub, likes) in enumerate(rows):
        cols = "channel_id, tweet_id, twitter_username, text, url, likes, retweets, interest_score"
        vals = [6, f"t{i}", "u", "x", "u", likes, 0, 7]
        if migrated:
            cols += ", substance"
            vals.append(sub)
        db.execute(f"INSERT INTO digest_queue ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    db.commit()
    db.close()
    return path


def _draft(**kw):
    base = dict(channel_id=6, author="OpenAI", tweet_id="123")
    base.update(kw)
    return SimpleNamespace(**base)


# ── Фикс 1 ───────────────────────────────────────────────────────────────────────────

def test_teaser_admission_mirror():
    assert find_teaser_admission("Вышла новая модель. Подробностей о самой модели в твите нет.")
    assert find_teaser_admission("Автор не уточнил, о чём речь.")
    assert find_teaser_admission("OpenAI выпустила GPT-5.5: контекст 1M, цена $2.") is None
    assert find_teaser_admission("Сроки пока неизвестны, но бенчмарк — 92%.") is None


def test_autopick_skips_teaser_and_unnamed_after_migration(tmp_path):
    db = _twidgest_db(str(tmp_path / "tw.db"), migrated=True)
    got = asyncio.run(candidates.top_candidates(db, 6, limit=10, strict_topic=True))
    assert [c.tweet_id for c in got] == ["t0", "t3"]          # ok + NULL (оценён до гейта)
    manual = asyncio.run(candidates.top_candidates(db, 6, limit=10))
    assert len(manual) == 4                                   # ручной /essay — любая тема


def test_autopick_works_before_twidgest_migration(tmp_path):
    db = _twidgest_db(str(tmp_path / "tw_old.db"), migrated=False)
    got = asyncio.run(candidates.top_candidates(db, 6, limit=10, strict_topic=True))
    assert len(got) == 4


# ── Фикс 3 ───────────────────────────────────────────────────────────────────────────

def test_channel_attribution_read(tmp_path):
    new = _twidgest_db(str(tmp_path / "a.db"), migrated=True)
    old = _twidgest_db(str(tmp_path / "b.db"), migrated=False)
    assert asyncio.run(candidates.channel_attribution(new, 6))["user_id"] == 1
    assert asyncio.run(candidates.channel_attribution(old, 6))["attribution_mode"] is None
    assert asyncio.run(candidates.channel_attribution(new, 999)) is None


def test_attr_mode_auto_and_explicit():
    future = (datetime.utcnow() + timedelta(days=3)).isoformat(sep=" ")
    assert bot._attr_mode({"user_id": 1}) == "all_posts"                       # витрина админа
    assert bot._attr_mode({"user_id": 77, "paid_until": future}) == "off"      # оплачен
    assert bot._attr_mode({"user_id": 77, "paid_until": None}) == "all_posts"  # триал/прочее
    assert bot._attr_mode({"user_id": 1, "attribution_mode": "digest_only"}) == "digest_only"
    assert bot._attr_mode(None) == "off"


def test_footer_for_tweet_and_custom_topic():
    f = bot._footer_html(_draft(), {"user_id": 1})
    assert f == ('<a href="https://x.com/OpenAI/status/123">→ Источник</a> · '
                 '<a href="https://t.me/TwidgestBot?start=src_c6">Собрано @TwidgestBot</a>')
    custom = bot._footer_html(_draft(author=None, tweet_id="custom:1"), {"user_id": 1})
    assert custom == '<a href="https://t.me/TwidgestBot?start=src_c6">Собрано @TwidgestBot</a>'
    assert bot._footer_html(_draft(), {"user_id": 1, "attribution_mode": "digest_only"}) == ""


def test_publish_parts_escape_and_overflow():
    foot = bot._footer_html(_draft(), {"user_id": 1})
    parts = bot._publish_parts("Абзац 1.\n\nЦена < $5 & рост > 10%.", foot)
    assert parts == [("Абзац 1.\n\nЦена &lt; $5 &amp; рост &gt; 10%.\n\n" + foot, "HTML")]
    long_last = "х" * (bot.TG_LIMIT - 10)
    parts = bot._publish_parts(long_last, foot)
    assert parts[-1] == (foot, "HTML") and parts[0] == (long_last, None)
    assert bot._publish_parts("текст", "") == [("текст", None)]

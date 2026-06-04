"""Essayist HIL-бот: /essay <channel_id> → разбор → карточка с кнопками → публикация.

Отдельный процесс, свой токен. База twidgest — только на чтение (candidates.py).
Своя БД (store.py). Постит своим токеном в target_chat_id канала-витрины.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

import candidates
import store
from essayist import generate_essay

load_dotenv()
ADMIN = int(os.environ.get("ADMIN_USER_ID", "0"))
TWIDGEST_DB = os.environ.get("TWIDGEST_DB", os.path.expanduser("~/twidgest-bot/twidgest.db"))
ESSBOT_DB = os.environ.get("ESSBOT_DB", "essayist.db")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("essayist-bot")

st = store.Store(ESSBOT_DB)
dp = Dispatcher()

TG_LIMIT = 4000
REASON_LABELS = {"fact": "факт-ошибка", "style": "стиль", "boring": "не интересно", "other": "другое"}
DEFAULT_TIMER_HOURS = 6
INTERVAL_CHOICES = (3, 6, 12, 24)


def _is_admin(uid: int) -> bool:
    return ADMIN != 0 and uid == ADMIN


def _chunks(text: str, size: int = TG_LIMIT) -> list[str]:
    """Бьём по абзацам, не разрывая слова; каждый кусок <= size."""
    out, cur = [], ""
    for para in text.split("\n\n"):
        piece = (cur + "\n\n" + para) if cur else para
        if len(piece) <= size:
            cur = piece
            continue
        if cur:
            out.append(cur)
        while len(para) > size:
            out.append(para[:size]); para = para[size:]
        cur = para
    if cur:
        out.append(cur)
    return out or [""]


def _kb_main(did: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"pub:{did}"),
        InlineKeyboardButton(text="✍️ Сменить угол", callback_data=f"angle:{did}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej:{did}"),
    ]])


def _kb_reasons(did: int) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=lbl, callback_data=f"reason:{did}:{code}")
           for code, lbl in REASON_LABELS.items()]
    back = [InlineKeyboardButton(text="« назад", callback_data=f"back:{did}")]
    return InlineKeyboardMarkup(inline_keyboard=[row[:2], row[2:], back])


def _card(cand_title: str, author: str | None, niche: str, res) -> str:
    head = (f"📝 Разбор готов\nКанал: {cand_title} (ниша: {niche})\n"
            f"Источник: {('@' + author) if author else 'своя тема'}\nВеб-поисков: {res.total_searches}")
    if res.violations:
        v = "\n".join(f"• [{x.get('type')}] {x.get('quote','')}" for x in res.violations)
        head += f"\n\n⚠️ Редактор отметил ({len(res.violations)}):\n{v}"
    else:
        head += "\n\n✅ Редактор: замечаний нет"
    head += "\n\nЧерновик — выше. Решение:"
    return head[:TG_LIMIT]


@dp.message(Command("essay"))
async def cmd_essay(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg:
        chans = await candidates.list_channels(TWIDGEST_DB)
        if not chans:
            await message.answer("Каналов с заданным target_chat_id не найдено.")
            return
        lines = "\n".join(f"  /essay {c.channel_id} — {c.title} ({c.niche})" for c in chans)
        await message.answer("Укажи канал:\n" + lines)
        return
    parts = arg.split(maxsplit=1)
    try:
        channel_id = int(parts[0])
    except ValueError:
        await message.answer("Формат: /essay <channel_id> [all | своя тема или ссылка]")
        return
    rest = parts[1].strip() if len(parts) > 1 else ""
    show_all = rest.lower() == "all"
    custom = "" if (not rest or show_all) else rest.strip(' "«»')

    if custom:
        ch = await candidates.get_channel(TWIDGEST_DB, channel_id)
        if not ch or not ch.target_chat_id:
            await message.answer("Канал не найден или без target_chat_id.")
            return
        await message.answer(f"🔎 Генерирую разбор по своей теме (ниша: {ch.niche})… ~минуту.")
        res = await generate_essay(tweet_text=custom, author=None, niche=ch.niche,
                                   channel=ch.title, api_key=ANTHROPIC_KEY)
        if not res.ok:
            await message.answer(f"⚠️ Не получилось: {res.error}")
            return
        did = await st.create_draft(
            channel_id=channel_id, tweet_id=f"custom:{int(time.time())}", tweet_text=custom,
            author=None, niche=ch.niche, target_chat_id=ch.target_chat_id, title=ch.title,
            brief=res.brief, draft=res.draft, violations=res.violations,
            total_searches=res.total_searches)
        for chunk in _chunks(res.draft):
            await message.answer(chunk)
        await message.answer(_card(ch.title, None, ch.niche, res), reply_markup=_kb_main(did))
        return

    cands = await candidates.top_candidates(TWIDGEST_DB, channel_id, limit=15)
    if not cands:
        await message.answer(f"Для канала {channel_id} нет кандидатов в digest_queue.")
        return
    if not show_all:
        published = await st.published_tweets(channel_id)
        cands = [c for c in cands if c.tweet_id not in published]
        if not cands:
            await message.answer(
                f"Свежих тем нет — все топовые уже опубликованы.\n"
                f"Показать полный список: /essay {channel_id} all")
            return
    cands = cands[:5]
    lines = ["Выбери тему разбора (топ по виральности):\n"]
    buttons = []
    for i, c in enumerate(cands, 1):
        text = (c.text or "").replace("\n", " ").strip()
        if len(text) > 350:
            text = text[:350] + "…"
        lines.append(f"{i}. @{c.author} · {c.likes}♥ · {c.retweets}🔁\n{text}\n")
        buttons.append(InlineKeyboardButton(text=str(i),
                                            callback_data=f"pick:{channel_id}:{c.tweet_id}"))
    await message.answer("\n".join(lines),
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]))


@dp.callback_query(F.data.startswith("pick:"))
async def cb_pick(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        return await cq.answer()
    _, sch, tweet_id = cq.data.split(":", 2)
    cand = await candidates.get_by_tweet(TWIDGEST_DB, int(sch), tweet_id)
    if not cand:
        return await cq.answer("Кандидат не найден (очередь обновилась).", show_alert=True)
    if not cand.target_chat_id:
        return await cq.answer("У канала нет target_chat_id.", show_alert=True)
    await cq.answer("Запускаю генерацию…")
    await cq.message.edit_text(f"🔎 Генерирую разбор по @{cand.author} (ниша: {cand.niche})… ~минуту.")
    res = await generate_essay(tweet_text=cand.text, author=cand.author, niche=cand.niche,
                               channel=cand.title, api_key=ANTHROPIC_KEY)
    if not res.ok:
        await cq.message.answer(f"⚠️ Не получилось: {res.error}")
        return
    did = await st.create_draft(
        channel_id=cand.channel_id, tweet_id=cand.tweet_id, tweet_text=cand.text,
        author=cand.author, niche=cand.niche, target_chat_id=cand.target_chat_id,
        title=cand.title, brief=res.brief, draft=res.draft, violations=res.violations,
        total_searches=res.total_searches)
    for ch in _chunks(res.draft):
        await cq.message.answer(ch)
    await cq.message.answer(_card(cand.title, cand.author, cand.niche, res),
                            reply_markup=_kb_main(did))


@dp.callback_query(F.data.startswith("pub:"))
async def cb_pub(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        return await cq.answer()
    did = int(cq.data.split(":")[1])
    d = await st.get(did)
    if not d or not d.target_chat_id:
        return await cq.answer("Нет таргета для публикации.", show_alert=True)
    if not await st.claim_for_publish(did):
        return await cq.answer("Уже опубликовано или в процессе.", show_alert=True)
    try:
        first_id = None
        for ch in _chunks(d.draft):
            m = await cq.bot.send_message(d.target_chat_id, ch)
            first_id = first_id or m.message_id
        await st.finalize_publish(did, first_id)
        await cq.message.edit_reply_markup(reply_markup=None)
        await cq.message.answer(f"✅ Опубликовано в «{d.title}» (msg {first_id})")
        await cq.answer("Опубликовано")
    except Exception as exc:
        await st.revert_publish(did)
        log.exception("publish failed for draft %s", did)
        await cq.answer(f"Ошибка отправки: {exc}", show_alert=True)


@dp.callback_query(F.data.startswith("angle:"))
async def cb_angle(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        return await cq.answer()
    did = int(cq.data.split(":")[1])
    if not await st.begin_revision(did):
        return await cq.answer("Сейчас нельзя (уже опубликовано/отклонено).", show_alert=True)
    d = await st.get(did)
    await cq.answer("Перегенерирую…")
    res = await generate_essay(tweet_text=d.tweet_text, author=d.author, niche=d.niche,
                               channel=d.title, api_key=ANTHROPIC_KEY)
    if not res.ok:
        await st.apply_revision(did, d.draft, d.violations)  # вернуть как было, статус pending
        return await cq.message.answer(f"⚠️ Перегенерация не вышла: {res.error}")
    await st.apply_revision(did, res.draft, res.violations)
    await cq.message.answer(f"🔁 Заход №{d.revision_count + 1}:")
    for ch in _chunks(res.draft):
        await cq.message.answer(ch)
    await cq.message.answer(_card(d.title, d.author, d.niche, res), reply_markup=_kb_main(did))


@dp.callback_query(F.data.startswith("rej:"))
async def cb_rej(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        return await cq.answer()
    did = int(cq.data.split(":")[1])
    await cq.message.edit_reply_markup(reply_markup=_kb_reasons(did))
    await cq.answer("Почему отклоняешь?")


@dp.callback_query(F.data.startswith("back:"))
async def cb_back(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        return await cq.answer()
    did = int(cq.data.split(":")[1])
    await cq.message.edit_reply_markup(reply_markup=_kb_main(did))
    await cq.answer()


@dp.callback_query(F.data.startswith("reason:"))
async def cb_reason(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        return await cq.answer()
    _, sid, code = cq.data.split(":", 2)
    reason = REASON_LABELS.get(code, "другое")
    if not await st.reject(int(sid), reason):
        return await cq.answer("Сейчас нельзя.", show_alert=True)
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.message.answer(f"❌ Отклонено: {reason}")
    await cq.answer("Записал")


async def _settings_panel() -> tuple[str, InlineKeyboardMarkup]:
    hours = await st.get_setting("timer_hours", str(DEFAULT_TIMER_HOURS))
    chans = await candidates.list_channels(TWIDGEST_DB)
    disabled = await st.disabled_channels()
    rows = []
    for c in chans:
        on = c.channel_id not in disabled
        rows.append([InlineKeyboardButton(text=f"{'\u2705' if on else '\U0001f6ab'} {c.title}",
                                          callback_data=f"chtoggle:{c.channel_id}")])
    rows.append([InlineKeyboardButton(text=f"{'\u2022 ' if str(h) == str(hours) else ''}{h}\u0447",
                                      callback_data=f"setint:{h}") for h in INTERVAL_CHOICES])
    text = (f"\u23f1 \u0410\u0432\u0442\u043e\u043f\u043e\u0434\u0431\u043e\u0440 \u0442\u0435\u043c\n"
            f"\u0418\u043d\u0442\u0435\u0440\u0432\u0430\u043b: \u043a\u0430\u0436\u0434\u044b\u0435 {hours} \u0447\n\n"
            "\u041a\u0430\u043d\u0430\u043b\u044b (\u2705 \u0432\u043a\u043b / \U0001f6ab \u0432\u044b\u043a\u043b) \u2014 \u043d\u0430\u0436\u043c\u0438, \u0447\u0442\u043e\u0431\u044b \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0438\u0442\u044c:")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@dp.message(Command("timer"))
async def cmd_timer(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    text, kb = await _settings_panel()
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("chtoggle:"))
async def cb_chtoggle(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        return await cq.answer()
    cid = int(cq.data.split(":")[1])
    disabled = await st.disabled_channels()
    await st.set_channel_enabled(cid, cid in disabled)
    text, kb = await _settings_panel()
    await _safe_edit(cq.message, text, kb)
    await cq.answer()


@dp.callback_query(F.data.startswith("setint:"))
async def cb_setint(cq: CallbackQuery) -> None:
    if not _is_admin(cq.from_user.id):
        return await cq.answer()
    hours = cq.data.split(":")[1]
    await st.set_setting("timer_hours", hours)
    text, kb = await _settings_panel()
    await _safe_edit(cq.message, text, kb)
    await cq.answer(f"\u0418\u043d\u0442\u0435\u0440\u0432\u0430\u043b: {hours} \u0447")


async def _timer_send(bot: Bot, chat_id: int, cand) -> None:
    res = await generate_essay(tweet_text=cand.text, author=cand.author, niche=cand.niche,
                               channel=cand.title, api_key=ANTHROPIC_KEY)
    if not res.ok:
        await bot.send_message(chat_id, f"\u26a0\ufe0f \u0410\u0432\u0442\u043e\u043f\u043e\u0434\u0431\u043e\u0440 \u00ab{cand.title}\u00bb: {res.error}")
        return
    did = await st.create_draft(
        channel_id=cand.channel_id, tweet_id=cand.tweet_id, tweet_text=cand.text,
        author=cand.author, niche=cand.niche, target_chat_id=cand.target_chat_id,
        title=cand.title, brief=res.brief, draft=res.draft, violations=res.violations,
        total_searches=res.total_searches)
    await bot.send_message(chat_id, f"\u23f1 \u0410\u0432\u0442\u043e\u043f\u043e\u0434\u0431\u043e\u0440 \u00b7 \u00ab{cand.title}\u00bb \u00b7 \u0442\u0435\u043c\u0430 @{cand.author}")
    for ch in _chunks(res.draft):
        await bot.send_message(chat_id, ch)
    await bot.send_message(chat_id, _card(cand.title, cand.author, cand.niche, res),
                           reply_markup=_kb_main(did))


async def run_timer_tick(bot: Bot) -> None:
    try:
        chans = await candidates.list_channels(TWIDGEST_DB)
    except Exception:
        log.exception("timer: read channels failed")
        return
    disabled = await st.disabled_channels()
    for ch in chans:
        if ch.channel_id in disabled:
            continue
        try:
            cands = await candidates.top_candidates(TWIDGEST_DB, ch.channel_id, limit=10)
        except Exception:
            log.exception("timer: candidates failed for %s", ch.channel_id)
            continue
        pick = None
        for c in cands:
            if c.target_chat_id and not await st.seen_tweet(c.channel_id, c.tweet_id):
                pick = c
                break
        if pick:
            log.info("timer: channel %s topic @%s", ch.channel_id, pick.author)
            await _timer_send(bot, ADMIN, pick)


async def timer_loop(bot: Bot) -> None:
    while True:
        try:
            hours = float(await st.get_setting("timer_hours", str(DEFAULT_TIMER_HOURS)))
        except (TypeError, ValueError):
            hours = float(DEFAULT_TIMER_HOURS)
        await asyncio.sleep(max(0.5, hours) * 3600)
        try:
            await run_timer_tick(bot)
        except Exception:
            log.exception("timer tick failed")


async def main() -> None:
    if ADMIN == 0:
        raise SystemExit("ADMIN_USER_ID не задан в .env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env")
    await st.init()
    bot = Bot(token)
    asyncio.create_task(timer_loop(bot))
    await bot.set_my_commands([
        BotCommand(command="essay", description="Разбор: выбрать тему канала — /essay <id> [all]"),
        BotCommand(command="timer", description="Автоподбор: интервал и каналы вкл/выкл"),
    ])
    log.info("essayist-bot запущен; админ=%s, twidgest_db=%s, таймер=%s ч",
             ADMIN, TWIDGEST_DB, DEFAULT_TIMER_HOURS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

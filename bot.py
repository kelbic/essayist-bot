"""Essayist HIL-бот: /essay <channel_id> → разбор → карточка с кнопками → публикация.

Отдельный процесс, свой токен. База twidgest — только на чтение (candidates.py).
Своя БД (store.py). Постит своим токеном в target_chat_id канала владельца.

Мультитенант (вариант B): доступ через auth.py — суперадмин → допуск (Essayist Pro)
→ владение каналом. Таймер ходит по per-channel essay_config (opt-in, по умолчанию
выкл), уважает frequency_hours/last_run_at и шлёт карточку ВЛАДЕЛЬЦУ канала.
Онбординг: владелец жмёт /start (реестр bot_users), суперадмин выдаёт доступ
/grant, бот проверяет права в канале (getChat) перед включением автоподбора.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

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

import auth
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
TICK_MINUTES = 30          # внутренний пульс таймера (не пользовательская настройка)
DEFAULT_FREQ_HOURS = 12    # частота разбора по умолчанию для канала
FRESH_HOURS = 24           # автоподбор берёт твиты не старше N часов (по queued_at)
MAX_PICK_ATTEMPTS = 3      # сколько кандидатов пробовать за тик, прежде чем сдаться
TRIAL_DAYS = 7             # срок триала Essayist Pro по умолчанию (/grant без аргумента)
INTERVAL_CHOICES = (3, 6, 9, 12, 24)

NO_ACCESS_CHANNEL = "Нет доступа к этому каналу — он не твой."
NO_ACCESS_DRAFT = "Нет доступа к этому черновику."
NO_PRO = "Доступ к Essayist Pro не активирован. Обратись к администратору."
NEED_ADMIN_RIGHTS = ("Бот не админ этого канала (или без права постинга). "
                     "Добавь @essayist_bot админом с правом Post Messages — потом включи.")


def _is_admin(uid: int) -> bool:
    return ADMIN != 0 and uid == ADMIN


def _bot_can_post(member) -> bool:
    """Может ли бот постить в канал по его членству: создатель или админ с can_post_messages."""
    status = getattr(member, "status", None)
    if status == "creator":
        return True
    if status == "administrator":
        return bool(getattr(member, "can_post_messages", False))
    return False


def _days_left(expires_at) -> int | None:
    """Сколько дней доступа осталось. None = бессрочно/нет даты; 0 = истёк."""
    if not expires_at:
        return None
    try:
        exp = datetime.fromisoformat(expires_at)
    except ValueError:
        return None
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    delta = exp - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return 0
    return delta.days + (1 if delta.seconds > 0 else 0)


def _is_unsearchable(res) -> bool:
    """Сработал ли предохранитель «0 веб-поисков» (тему стоит пропустить навсегда),
    в отличие от транзиентного сбоя API (тему можно ретраить)."""
    return bool(res and res.error and "поиск" in res.error.lower())


def _is_due(last_run_at: str | None, frequency_hours: int, now: datetime | None = None) -> bool:
    """Пора ли каналу новый разбор: никогда не запускался ИЛИ прошло >= frequency_hours."""
    if not last_run_at:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(last_run_at)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= frequency_hours * 3600


async def _resolve_channel(uid: int, channel_id: int):
    """(ch, allowed): ChannelInfo из twidgest + право доступа.

    ch=None если канала нет. allowed=True если суперадмин ИЛИ (допущен И владелец).
    """
    ch = await candidates.get_channel(TWIDGEST_DB, channel_id)
    if not ch:
        return None, False
    allowed = auth.is_superadmin(uid, ADMIN) or (
        await st.user_enabled(uid) and ch.user_id == uid)
    return ch, allowed


async def _owns_draft(uid: int, did: int):
    """(d, allowed): черновик + право им управлять.

    Суперадмин — всегда. Иначе только владелец черновика. Старые черновики с
    owner_user_id=NULL (до миграции) доступны только суперадмину.
    """
    d = await st.get(did)
    if not d:
        return None, False
    allowed = auth.is_superadmin(uid, ADMIN) or (
        d.owner_user_id is not None and d.owner_user_id == uid)
    return d, allowed


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


@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject) -> None:
    uid = message.from_user.id
    await st.mark_started(uid)  # теперь таймер сможет писать этому владельцу первым
    if await auth.is_entitled(st, uid, ADMIN):
        suffix = ""
        if not _is_admin(uid):
            ent = await st.get_entitlement(uid)
            dleft = _days_left(ent["expires_at"]) if ent else None
            if dleft is not None:
                suffix = f"\n\nДоступ (бета): осталось {dleft} дн. Продлить — напиши @kelbic."
        await message.answer(
            "Привет! Essayist Pro активирован.\n\n"
            "• /essay <id> — собрать разбор по теме канала (или своей)\n"
            "• /timer — автоподбор: вкл/выкл и частота по каналам\n\n"
            "Чтобы автоподбор постил, бот должен быть админом твоего канала с правом Post Messages."
            + suffix)
    else:
        await message.answer(
            "Привет! Это Essayist — бот авторских разборов для каналов.\n\n"
            "Доступ к Essayist Pro пока не активирован. Когда администратор выдаст доступ, "
            "тебе станут доступны /essay и /timer.")


@dp.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    tokens = (command.args or "").split()
    if not tokens:
        await message.answer(f"Формат: /grant <user_id> [дни|forever] [заметка]\n"
                             f"Без срока — триал на {TRIAL_DAYS} дн.")
        return
    try:
        uid = int(tokens[0])
    except ValueError:
        await message.answer("user_id должен быть числом. Формат: /grant <user_id> [дни|forever] [заметка]")
        return
    rest = tokens[1:]
    days: int | None = TRIAL_DAYS
    if rest and rest[0].lower() == "forever":
        days, rest = None, rest[1:]
    elif rest and rest[0].isdigit():
        days, rest = int(rest[0]), rest[1:]
    note = " ".join(rest) if rest else None
    await st.set_user_enabled(uid, True, note, days=days)
    period = "бессрочно" if days is None else f"{days} дн."
    started = await st.is_started(uid)
    tail = "" if started else "\n⚠️ Пользователь ещё не нажимал Start — попроси открыть бота и нажать /start."
    await message.answer(f"✅ Доступ выдан: {uid} ({period})" + (f" — {note}" if note else "") + tail)


@dp.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    try:
        uid = int(arg)
    except ValueError:
        await message.answer("Формат: /revoke <user_id>")
        return
    await st.set_user_enabled(uid, False)
    await message.answer(f"🚫 Доступ отозван: {uid}")


@dp.message(Command("grants"))
async def cmd_grants(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    rows = await st.list_entitled()
    if not rows:
        await message.answer("Список допущенных пуст.")
        return
    lines = []
    for r in rows:
        mark = "✅" if r["enabled"] else "🚫"
        started = "" if await st.is_started(r["user_id"]) else " · не нажал Start"
        note = f" — {r['note']}" if r.get("note") else ""
        if not r["enabled"]:
            per = ""
        elif r["expires_at"] is None:
            per = " · бессрочно"
        else:
            dl = _days_left(r["expires_at"])
            per = " · истёк" if dl == 0 else f" · {dl} дн."
        lines.append(f"{mark} {r['user_id']}{note}{per}{started}")
    await message.answer("Допуски Essayist Pro:\n" + "\n".join(lines))


@dp.message(Command("help"))
async def cmd_help(message: Message, command: CommandObject) -> None:
    uid = message.from_user.id
    base = (
        "Essayist — авторские разборы для каналов поверх TwidgestBot.\n\n"
        "• /start — регистрация у бота\n"
        "• /essay <id> — выбрать тему канала и собрать разбор\n"
        "   /essay <id> all — показать все темы (включая опубликованные)\n"
        "   /essay <id> <своя тема или ссылка> — разбор по своему тексту\n"
        "• /timer — автоподбор: вкл/выкл и частота по каналам\n\n"
        "В карточке разбора: ✅ Опубликовать · ✍️ Сменить угол · ❌ Отклонить.")
    if _is_admin(uid):
        base += (
            "\n\nАдмин:\n"
            f"• /grant <user_id> [дни|forever] [заметка] — выдать доступ (по умолчанию {TRIAL_DAYS} дн.)\n"
            "• /revoke <user_id> — снять доступ\n"
            "• /grants — список допусков со сроками")
    await message.answer(base)


@dp.message(Command("essay"))
async def cmd_essay(message: Message, command: CommandObject) -> None:
    uid = message.from_user.id
    if not await auth.is_entitled(st, uid, ADMIN):
        await message.answer(NO_PRO)
        return
    arg = (command.args or "").strip()
    if not arg:
        chans = await auth.visible_channels(st, TWIDGEST_DB, uid, ADMIN)
        if not chans:
            await message.answer("У тебя нет подключённых каналов.")
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

    ch, allowed = await _resolve_channel(uid, channel_id)
    if not ch or not ch.target_chat_id:
        await message.answer("Канал не найден или без target_chat_id.")
        return
    if not allowed:
        await message.answer(NO_ACCESS_CHANNEL)
        return
    owner = ch.user_id

    rest = parts[1].strip() if len(parts) > 1 else ""
    show_all = rest.lower() == "all"
    custom = "" if (not rest or show_all) else rest.strip(' "«»')

    if custom:
        await message.answer(f"🔎 Генерирую разбор по своей теме (ниша: {ch.niche})… ~минуту.")
        res = await generate_essay(tweet_text=custom, author=None, niche=ch.niche,
                                   channel=ch.title, api_key=ANTHROPIC_KEY)
        if not res.ok:
            await message.answer(f"⚠️ Не получилось: {res.error}")
            return
        did = await st.create_draft(
            channel_id=channel_id, owner_user_id=owner,
            tweet_id=f"custom:{int(time.time())}", tweet_text=custom,
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
    uid = cq.from_user.id
    _, sch, tweet_id = cq.data.split(":", 2)
    channel_id = int(sch)
    ch, allowed = await _resolve_channel(uid, channel_id)
    if not allowed:
        return await cq.answer(NO_ACCESS_CHANNEL, show_alert=True)
    cand = await candidates.get_by_tweet(TWIDGEST_DB, channel_id, tweet_id)
    if not cand:
        return await cq.answer("Кандидат не найден (очередь обновилась).", show_alert=True)
    if not cand.target_chat_id:
        return await cq.answer("У канала нет target_chat_id.", show_alert=True)
    owner = ch.user_id
    await cq.answer("Запускаю генерацию…")
    await cq.message.edit_text(f"🔎 Генерирую разбор по @{cand.author} (ниша: {cand.niche})… ~минуту.")
    res = await generate_essay(tweet_text=cand.text, author=cand.author, niche=cand.niche,
                               channel=cand.title, api_key=ANTHROPIC_KEY)
    if not res.ok:
        await cq.message.answer(f"⚠️ Не получилось: {res.error}")
        return
    did = await st.create_draft(
        channel_id=cand.channel_id, owner_user_id=owner, tweet_id=cand.tweet_id,
        tweet_text=cand.text, author=cand.author, niche=cand.niche,
        target_chat_id=cand.target_chat_id, title=cand.title, brief=res.brief,
        draft=res.draft, violations=res.violations, total_searches=res.total_searches)
    for ch_text in _chunks(res.draft):
        await cq.message.answer(ch_text)
    await cq.message.answer(_card(cand.title, cand.author, cand.niche, res),
                            reply_markup=_kb_main(did))


@dp.callback_query(F.data.startswith("pub:"))
async def cb_pub(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    did = int(cq.data.split(":")[1])
    d, allowed = await _owns_draft(uid, did)
    if not allowed:
        return await cq.answer(NO_ACCESS_DRAFT, show_alert=True)
    if not d.target_chat_id:
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
    uid = cq.from_user.id
    did = int(cq.data.split(":")[1])
    _, allowed = await _owns_draft(uid, did)
    if not allowed:
        return await cq.answer(NO_ACCESS_DRAFT, show_alert=True)
    if not await st.begin_revision(did):
        return await cq.answer("Сейчас нельзя (уже опубликовано/отклонено).", show_alert=True)
    d = await st.get(did)
    await cq.answer("Перегенерирую…")
    res = await generate_essay(tweet_text=d.tweet_text, author=d.author, niche=d.niche,
                               channel=d.title, api_key=ANTHROPIC_KEY)
    if not res.ok:
        await st.apply_revision(did, d.draft, d.violations)
        return await cq.message.answer(f"⚠️ Перегенерация не вышла: {res.error}")
    await st.apply_revision(did, res.draft, res.violations)
    await cq.message.answer(f"🔁 Заход №{d.revision_count + 1}:")
    for ch in _chunks(res.draft):
        await cq.message.answer(ch)
    await cq.message.answer(_card(d.title, d.author, d.niche, res), reply_markup=_kb_main(did))


@dp.callback_query(F.data.startswith("rej:"))
async def cb_rej(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    did = int(cq.data.split(":")[1])
    _, allowed = await _owns_draft(uid, did)
    if not allowed:
        return await cq.answer(NO_ACCESS_DRAFT, show_alert=True)
    await cq.message.edit_reply_markup(reply_markup=_kb_reasons(did))
    await cq.answer("Почему отклоняешь?")


@dp.callback_query(F.data.startswith("back:"))
async def cb_back(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    did = int(cq.data.split(":")[1])
    _, allowed = await _owns_draft(uid, did)
    if not allowed:
        return await cq.answer(NO_ACCESS_DRAFT, show_alert=True)
    await cq.message.edit_reply_markup(reply_markup=_kb_main(did))
    await cq.answer()


@dp.callback_query(F.data.startswith("reason:"))
async def cb_reason(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    _, sid, code = cq.data.split(":", 2)
    did = int(sid)
    _, allowed = await _owns_draft(uid, did)
    if not allowed:
        return await cq.answer(NO_ACCESS_DRAFT, show_alert=True)
    reason = REASON_LABELS.get(code, "другое")
    if not await st.reject(did, reason):
        return await cq.answer("Сейчас нельзя.", show_alert=True)
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.message.answer(f"❌ Отклонено: {reason}")
    await cq.answer("Записал")


async def _timer_panel(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Панель автоподбора: суперадмин видит все каналы, юзер — свои.

    Вкл/выкл и частота — per-channel (essay_config). По умолчанию канал выключен.
    """
    chans = await auth.visible_channels(st, TWIDGEST_DB, uid, ADMIN)
    rows, notes = [], []
    for c in chans:
        cfg = await st.get_essay_config(c.channel_id)
        on = bool(cfg and cfg["enabled"])
        freq = cfg["frequency_hours"] if cfg else DEFAULT_FREQ_HOURS
        mark = "✅" if on else "🚫"
        rows.append([InlineKeyboardButton(text=f"{mark} {c.title}",
                                          callback_data=f"estog:{c.channel_id}")])
        rows.append([InlineKeyboardButton(text=f"{'• ' if h == freq else ''}{h}ч",
                                          callback_data=f"esfreq:{c.channel_id}:{h}")
                     for h in INTERVAL_CHOICES])
        if on and cfg and cfg.get("last_error"):
            notes.append(f"⚠️ «{c.title}»: {cfg['last_error']}")
    if not rows:
        return ("У тебя нет подключённых каналов для Essayist Pro.",
                InlineKeyboardMarkup(inline_keyboard=[]))
    text = ("⏱ Автоподбор разборов\n\n"
            f"Проверка каждые {TICK_MINUTES} мин; частота ниже — как часто канал получает разбор.\n"
            "✅ вкл / 🚫 выкл — нажми на название канала.")
    if notes:
        text += "\n\n" + "\n".join(notes)
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@dp.message(Command("timer"))
async def cmd_timer(message: Message, command: CommandObject) -> None:
    uid = message.from_user.id
    if not await auth.is_entitled(st, uid, ADMIN):
        await message.answer(NO_PRO)
        return
    text, kb = await _timer_panel(uid)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("estog:"))
async def cb_estog(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    cid = int(cq.data.split(":")[1])
    ch, allowed = await _resolve_channel(uid, cid)
    if not ch or not allowed:
        return await cq.answer(NO_ACCESS_CHANNEL, show_alert=True)
    cfg = await st.get_essay_config(cid)
    now_on = bool(cfg and cfg["enabled"])
    if not now_on:  # включаем — проверим, что бот реально может постить в этот канал
        try:
            me = await cq.bot.get_me()
            member = await cq.bot.get_chat_member(ch.target_chat_id, me.id)
        except Exception as exc:
            return await cq.answer(f"Не вижу канал ({exc}). Добавь бота админом.", show_alert=True)
        if not _bot_can_post(member):
            return await cq.answer(NEED_ADMIN_RIGHTS, show_alert=True)
    await st.set_essay_enabled(cid, ch.user_id, not now_on)
    if not now_on:
        await st.set_essay_error(cid, None)  # включили с правами — чистим старую ошибку
    text, kb = await _timer_panel(uid)
    await _safe_edit(cq.message, text, kb)
    await cq.answer("Включён" if not now_on else "Выключен")


@dp.callback_query(F.data.startswith("esfreq:"))
async def cb_esfreq(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    _, scid, sh = cq.data.split(":", 2)
    cid, hours = int(scid), int(sh)
    ch, allowed = await _resolve_channel(uid, cid)
    if not ch or not allowed:
        return await cq.answer(NO_ACCESS_CHANNEL, show_alert=True)
    await st.set_essay_frequency(cid, ch.user_id, hours)
    text, kb = await _timer_panel(uid)
    await _safe_edit(cq.message, text, kb)
    await cq.answer(f"Частота: {hours} ч")


async def _timer_send(bot: Bot, chat_id: int, cand, owner_user_id: int) -> bool:
    """Сгенерировать разбор и отправить карточку владельцу. True если карточка ушла."""
    res = await generate_essay(tweet_text=cand.text, author=cand.author, niche=cand.niche,
                               channel=cand.title, api_key=ANTHROPIC_KEY)
    if not res.ok:
        log.warning("timer: генерация не удалась «%s»: %s", cand.title, res.error)
        return False, res
    did = await st.create_draft(
        channel_id=cand.channel_id, owner_user_id=owner_user_id, tweet_id=cand.tweet_id,
        tweet_text=cand.text, author=cand.author, niche=cand.niche,
        target_chat_id=cand.target_chat_id, title=cand.title, brief=res.brief,
        draft=res.draft, violations=res.violations, total_searches=res.total_searches)
    await bot.send_message(chat_id, f"⏱ Автоподбор · «{cand.title}» · тема @{cand.author}")
    for ch in _chunks(res.draft):
        await bot.send_message(chat_id, ch)
    await bot.send_message(chat_id, _card(cand.title, cand.author, cand.niche, res),
                           reply_markup=_kb_main(did))
    return True, res


async def run_timer_tick(bot: Bot) -> None:
    try:
        rows = await st.enabled_essay_channels()
    except Exception:
        log.exception("timer: чтение essay_config не удалось")
        return
    now = datetime.now(timezone.utc)
    for cfg in rows:
        cid = cfg["channel_id"]
        owner = cfg["user_id"]
        if not _is_due(cfg.get("last_run_at"), cfg.get("frequency_hours", DEFAULT_FREQ_HOURS), now):
            continue
        try:
            ch = await candidates.get_channel(TWIDGEST_DB, cid)
            if not ch or not ch.target_chat_id:
                await st.set_essay_error(cid, "канал пропал из twidgest или без target_chat_id")
                continue
            cands = await candidates.top_candidates(TWIDGEST_DB, cid, limit=10,
                                                    max_age_hours=FRESH_HOURS)
        except Exception:
            log.exception("timer: кандидаты не прочитались для %s", cid)
            continue
        eligible = []
        for c in cands:
            if not c.target_chat_id:
                continue
            if await st.seen_tweet(c.channel_id, c.tweet_id):
                continue
            if await st.is_skipped(c.channel_id, c.tweet_id):
                continue
            eligible.append(c)
        if not eligible:
            continue  # свежих новых тем нет — ждём следующего тика, ничего не тратим
        # предохранитель доставки (один раз на канал, до генерации)
        try:
            await bot.send_chat_action(owner, "typing")
        except Exception as exc:
            if not await st.is_started(owner):
                msg = "владелец не нажал Start у бота (нет диалога)"
            else:
                msg = f"не доставить владельцу: {exc}"
            log.warning("timer: владелец %s недоступен (канал %s): %s", owner, cid, msg)
            await st.set_essay_error(cid, msg)
            continue
        # пробуем до MAX_PICK_ATTEMPTS кандидатов: непоисковые метим skip и идём дальше
        delivered = False
        stop_transient = False
        for c in eligible[:MAX_PICK_ATTEMPTS]:
            ok, res = await _timer_send(bot, owner, c, owner)
            if ok:
                await st.touch_essay_run(cid)
                await st.set_essay_error(cid, None)
                log.info("timer: канал %s, владелец %s, тема @%s", cid, owner, c.author)
                delivered = True
                break
            if _is_unsearchable(res):
                await st.mark_skipped(cid, c.tweet_id, "0 веб-поисков (непоисковая тема)")
                log.info("timer: канал %s — @%s непоисковая (0 поисков), пробую следующую", cid, c.author)
                continue
            # транзиентная ошибка генерации — тему НЕ метим, прекращаем тик, ретрай позже
            await st.touch_essay_run(cid)
            await st.set_essay_error(cid, f"генерация: {(res.error if res else 'сбой')[:80]}")
            log.warning("timer: канал %s — транзиентный сбой генерации, ретрай позже", cid)
            stop_transient = True
            break
        if not delivered and not stop_transient:
            await st.touch_essay_run(cid)
            await st.set_essay_error(cid, "подряд непоисковые темы — пропуск тика")


async def timer_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(TICK_MINUTES * 60)
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
        BotCommand(command="start", description="Начать / зарегистрироваться у бота"),
        BotCommand(command="essay", description="Разбор: выбрать тему канала — /essay <id> [all]"),
        BotCommand(command="timer", description="Автоподбор: вкл/выкл и частота по каналам"),
        BotCommand(command="help", description="Справка по командам"),
    ])
    log.info("essayist-bot запущен; админ=%s, twidgest_db=%s, тик=%s мин",
             ADMIN, TWIDGEST_DB, TICK_MINUTES)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

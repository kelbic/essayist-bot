"""Слой авторизации essayist (вариант B).

Три уровня доступа:
    суперадмин → допуск к Essayist Pro (entitlement) → владение каналом.

Разделение источников истины:
    • КТО ЧЕМ ВЛАДЕЕТ — read-only из БД twidgest (candidates.get_channel.user_id).
    • КТО ОПЛАТИЛ Essayist Pro — собственная БД essayist (store.user_enabled).
Связующий ключ — Telegram user id: один и тот же аккаунт пишет обоим ботам,
поэтому message.from_user.id в essayist == users.tg_user_id в twidgest.

admin_id передаётся аргументом (из конфига бота), а не читается тут из env —
так модуль остаётся чистым и тестируемым.
"""
from __future__ import annotations

import candidates


def is_superadmin(uid: int, admin_id: int) -> bool:
    return admin_id != 0 and uid == admin_id


async def is_entitled(store, uid: int, admin_id: int) -> bool:
    """Допущен ли пользователь к Essayist Pro (суперадмин — всегда)."""
    if is_superadmin(uid, admin_id):
        return True
    return await store.user_enabled(uid)


async def owns_channel(twidgest_db: str, uid: int, channel_id: int) -> bool:
    """Владеет ли пользователь этим каналом (по данным twidgest, read-only)."""
    ch = await candidates.get_channel(twidgest_db, channel_id)
    return ch is not None and ch.user_id == uid


async def can_use_channel(store, twidgest_db: str, uid: int,
                          channel_id: int, admin_id: int) -> bool:
    """Главная проверка для хендлеров: можно ли этому юзеру трогать этот канал.

    Суперадмин — всё. Остальные — только если допущены И владеют каналом.
    """
    if is_superadmin(uid, admin_id):
        return True
    if not await store.user_enabled(uid):
        return False
    return await owns_channel(twidgest_db, uid, channel_id)


async def visible_channels(store, twidgest_db: str, uid: int, admin_id: int):
    """Какие каналы показывать в /essay и /timer: суперадмину — все, юзеру — свои."""
    if is_superadmin(uid, admin_id):
        return await candidates.list_channels(twidgest_db)
    if not await store.user_enabled(uid):
        return []
    return await candidates.channels_for_user(twidgest_db, uid)

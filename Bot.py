import os
import sys
import logging
import asyncio
import aiosqlite
import re
import time
from html import escape
from typing import Callable, Dict, Any, Awaitable
from aiohttp import web

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, TelegramObject, ChatPermissions,
    ChatMemberUpdated
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.exceptions import TelegramAPIError

# ================= КОНФИГ =================
BOT_TOKEN = "8823945629:AAHfN3LN7lFahjV7kSC5I8f8SXfM4mvCbKQ"
WEBHOOK_URL = "https://telegram-bot-qxtd.onrender.com/webhook"
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"

ALLOWED_USERNAMES = ["Woozinoid", "roman3801", "durovgar"]
MAIN_ADMINS = ["Woozinoid"]

RANK_MAX = 3
RANK_STARS = {1: "⭐️", 2: "⭐️⭐️", 3: "⭐️⭐️⭐️"}

CMD = "$"

# ================= ПАРСЕР ПЕРИОДА =================
UNITS = [
    ("месяц", 2592000), ("мес", 2592000),
    ("недел", 604800), ("нед", 604800),
    ("день", 86400), ("дня", 86400), ("дней", 86400), ("д", 86400),
    ("час", 3600), ("часа", 3600), ("часов", 3600), ("ч", 3600),
    ("минут", 60), ("мин", 60), ("м", 60),
    ("секунд", 1), ("сек", 1), ("с", 1),
]

def parse_period(s):
    if not s:
        return None
    m = re.match(r"^(\d+)\s*([а-яa-z]+)\.?$", s.strip().lower())
    if not m:
        return None
    n = int(m.group(1))
    u = m.group(2)
    for key, secs in UNITS:
        if u.startswith(key):
            return n * secs
    return None

def fmt_period(s):
    if s >= 2592000: return f"{s // 2592000} мес."
    if s >= 604800:  return f"{s // 604800} нед."
    if s >= 86400:   return f"{s // 86400} дн."
    if s >= 3600:    return f"{s // 3600} ч."
    if s >= 60:      return f"{s // 60} мин."
    return f"{s} сек."

# ================= БАЗА =================
async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS ranks (
            user_id INTEGER PRIMARY KEY, rank INTEGER DEFAULT 0)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS warns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, chat_id INTEGER, reason TEXT,
            warned_by INTEGER, warned_at INTEGER)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER, chat_id INTEGER, until INTEGER,
            reason TEXT, banned_by INTEGER,
            PRIMARY KEY (user_id, chat_id))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER, chat_id INTEGER,
            added_by INTEGER, added_at INTEGER,
            PRIMARY KEY (user_id, chat_id))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS invite_links (
            chat_id INTEGER PRIMARY KEY, link TEXT, updated_at INTEGER)""")
        await db.commit()

async def save_user(uid, username, full_name):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
            (uid, username, full_name))
        await db.commit()

async def get_username(uid):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute("SELECT username FROM users WHERE user_id=?", (uid,))
        r = await cur.fetchone()
        return r[0] if r else None

async def get_display_name(uid):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT username, full_name FROM users WHERE user_id=?", (uid,))
        r = await cur.fetchone()
        if r:
            return f"@{r[0]}" if r[0] else (r[1] or str(uid))
    return str(uid)

def user_mention(uid, name):
    return f'<a href="tg://user?id={uid}">{escape(name)}</a>'

async def get_rank(uid):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute("SELECT rank FROM ranks WHERE user_id=?", (uid,))
        r = await cur.fetchone()
        return r[0] if r else 0

async def set_rank(uid, rank):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO ranks (user_id, rank) VALUES (?, ?)",
            (uid, rank))
        await db.commit()

async def find_uid_by_username(username):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT user_id FROM users WHERE username=? COLLATE NOCASE",
            (username,))
        r = await cur.fetchone()
        return r[0] if r else None

# ---- WHITELIST ----
async def wl_add(uid, chat_id, by_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO whitelist (user_id, chat_id, added_by, added_at) VALUES (?, ?, ?, ?)",
            (uid, chat_id, by_id, int(time.time())))
        await db.commit()

async def wl_remove(uid, chat_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "DELETE FROM whitelist WHERE user_id=? AND chat_id=?", (uid, chat_id))
        await db.commit()

async def wl_is(uid, chat_id):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT 1 FROM whitelist WHERE user_id=? AND chat_id=?", (uid, chat_id))
        return await cur.fetchone() is not None

async def wl_list(chat_id):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT user_id, added_by, added_at FROM whitelist WHERE chat_id=? ORDER BY added_at",
            (chat_id,))
        return await cur.fetchall()

async def wl_all_ids(chat_id):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT user_id FROM whitelist WHERE chat_id=?", (chat_id,))
        return [r[0] for r in await cur.fetchall()]

# ---- INVITE CACHE ----
async def get_cached_link(chat_id):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute("SELECT link FROM invite_links WHERE chat_id=?", (chat_id,))
        r = await cur.fetchone()
        return r[0] if r else None

async def cache_link(chat_id, link):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO invite_links (chat_id, link, updated_at) VALUES (?, ?, ?)",
            (chat_id, link, int(time.time())))
        await db.commit()

# ---- WARNS ----
async def add_warn(uid, chat_id, reason, by_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT INTO warns (user_id, chat_id, reason, warned_by, warned_at) VALUES (?, ?, ?, ?, ?)",
            (uid, chat_id, reason, by_id, int(time.time())))
        await db.commit()
        cur = await db.execute(
            "SELECT COUNT(*) FROM warns WHERE user_id=? AND chat_id=?", (uid, chat_id))
        return (await cur.fetchone())[0]

async def clear_warns(uid, chat_id, count=None):
    async with aiosqlite.connect("bot.db") as db:
        if count is None:
            await db.execute("DELETE FROM warns WHERE user_id=? AND chat_id=?", (uid, chat_id))
        else:
            await db.execute("""DELETE FROM warns WHERE id IN (
                SELECT id FROM warns WHERE user_id=? AND chat_id=? ORDER BY id DESC LIMIT ?
            )""", (uid, chat_id, count))
        await db.commit()

async def get_warns(uid, chat_id):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT reason FROM warns WHERE user_id=? AND chat_id=?", (uid, chat_id))
        return await cur.fetchall()

# ---- BANS ----
async def add_ban(uid, chat_id, until, reason, by_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO bans (user_id, chat_id, until, reason, banned_by) VALUES (?, ?, ?, ?, ?)",
            (uid, chat_id, until, reason, by_id))
        await db.commit()

async def remove_ban(uid, chat_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("DELETE FROM bans WHERE user_id=? AND chat_id=?", (uid, chat_id))
        await db.commit()

async def get_banlist(chat_id):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT user_id, reason FROM bans WHERE chat_id=? ORDER BY rowid DESC LIMIT 20",
            (chat_id,))
        return await cur.fetchall()

# ================= MIDDLEWARE =================
class UserCache(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: Dict[str, Any]):
        user = data.get("event_from_user")
        if user and user.username:
            await save_user(user.id, user.username, user.full_name)
        return await handler(event, data)

# ================= ПРАВА =================
def is_allowed(username):
    if not username:
        return False
    return username.lower() in [u.lower() for u in ALLOWED_USERNAMES]

def is_main_admin(username):
    if not username:
        return False
    return username.lower() in [u.lower() for u in MAIN_ADMINS]

async def check_actor(message):
    u = message.from_user
    if not u or not is_allowed(u.username):
        return False
    if await get_rank(u.id) == 0 and not is_main_admin(u.username):
        await set_rank(u.id, 1)
    return True

async def get_actor_rank(message):
    if is_main_admin(message.from_user.username):
        return RANK_MAX
    return await get_rank(message.from_user.id)

async def check_target(message, target_uid):
    if target_uid == message.from_user.id:
        return False, "🤔 На себя нельзя."
    if target_uid == message.bot.id:
        return False, "🤖 На меня нельзя."

    if await wl_is(target_uid, message.chat.id):
        return False, "🛡 Пользователь в белом списке — его нельзя тронуть."

    actor_rank = await get_actor_rank(message)
    target_rank = await get_rank(target_uid)

    if target_rank >= actor_rank and target_rank > 0:
        return False, "🔒 Цель имеет равный или высший ранг."

    return True, ""

async def target_suffix(target_uid, chat_id):
    is_privileged = False
    try:
        member = await bot.get_chat_member(chat_id, target_uid)
        if member.status in ("administrator", "creator"):
            is_privileged = True
    except TelegramAPIError:
        pass

    if not is_privileged:
        r = await get_rank(target_uid)
        if r > 0:
            is_privileged = True

    if is_privileged:
        return "\n\n<i>увы, но права Iris тут ничего не решают</i>"
    return ""

# ================= ВСПОМОГАТЕЛЬНОЕ =================
async def resolve_target(message, args):
    if message.reply_to_message:
        rt = message.reply_to_message
        if rt.from_user:
            u = rt.from_user
            await save_user(u.id, u.username or "", u.full_name)
            return u.id, u.full_name
    if args:
        arg = args.strip().split()[0]
        arg = arg.lstrip("$")
        if arg.isdigit():
            uid = int(arg)
            return uid, await get_display_name(uid)
        if arg.startswith("@"):
            uid = await find_uid_by_username(arg[1:])
            if uid:
                return uid, await get_display_name(uid)
    return None, None

def full_mute_perms():
    return ChatPermissions(
        can_send_messages=False, can_send_audios=False,
        can_send_documents=False, can_send_photos=False,
        can_send_videos=False, can_send_video_notes=False,
        can_send_voice_notes=False, can_send_polls=False,
        can_send_other_messages=False, can_add_web_page_previews=False)

def full_unmute_perms():
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True,
        can_send_documents=True, can_send_photos=True,
        can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True)

async def get_or_create_invite(chat_id):
    """Возвращает ссылку-приглашение в чат (из кэша или создаёт новую)."""
    cached = await get_cached_link(chat_id)
    if cached:
        return cached
    try:
        chat = await bot.get_chat(chat_id)
        if chat.invite_link:
            await cache_link(chat_id, chat.invite_link)
            return chat.invite_link
    except TelegramAPIError:
        pass
    try:
        link_obj = await bot.create_chat_invite_link(
            chat_id=chat_id,
            name="Whitelist return",
        )
        await cache_link(chat_id, link_obj.invite_link)
        return link_obj.invite_link
    except TelegramAPIError as e:
        logging.warning(f"Не удалось создать ссылку для {chat_id}: {e}")
        return None

async def send_wl_dm(user_id, chat_title, chat_id, reason: str = "бан"):
    """Пишет пользователю в ЛС: его вернули, вот ссылка."""
    link = await get_or_create_invite(chat_id)
    text = (
        f"🛡 <b>Вас попытались наказать в чате «{escape(chat_title)}»</b>\n\n"
        f"Причина: <i>{escape(reason)}</i>\n"
        f"Но вы в белом списке — наказание снято."
    )
    if link:
        text += f"\n\n🔗 <b>Вернуться в чат:</b> {link}"
    else:
        text += f"\n\n🔗 Ссылку создать не удалось — попросите админов."
    try:
        await bot.send_message(user_id, text, disable_web_page_preview=True)
        return True
    except TelegramAPIError as e:
        logging.warning(f"Не смог написать в ЛС {user_id}: {e}")
        return False

# ================= РОУТЕРЫ =================
pm_router = Router()
pm_router.message.filter(F.chat.type == "private")

group_router = Router()
group_router.message.filter(F.chat.type.in_({"group", "supergroup"}))

# ================= ЛИЧКА =================
@pm_router.message(CommandStart())
async def start_pm(message: Message, bot: Bot):
    await message.answer(
        f"👋 <b>Привет, {escape(message.from_user.first_name)}!</b>\n\n"
        f"Я — модератор-бот с рангами и белым списком.\n"
        f"<b>Все команды начинаются с <code>$</code></b> — чтобы не пересекаться с Iris.\n\n"
        f"<b>Команды:</b>\n"
        f"🔨 <code>$бан [срок] @user</code>\n"
        f"🔇 <code>$мут [срок] @user</code>\n"
        f"👞 <code>$кик @user</code>\n"
        f"⚠️ <code>$варн @user</code>\n"
        f"✅ <code>$разбан @user</code>\n"
        f"📋 <code>$банлист</code>\n"
        f"📋 <code>$варны @user</code>\n"
        f"➕ <code>$ранг 1|2|3 @user</code>\n"
        f"➖ <code>$разжаловать @user</code>\n"
        f"🛡 <code>$бс добавить @user</code>\n"
        f"🛡 <code>$бс убрать @user</code>\n"
        f"🛡 <code>$белый список</code>\n"
        f"👥 <code>$админы</code>\n"
        f"🔄 <code>$проверить бс</code>"
    )

# ================= РАНГИ =================
SETRANK_PATTERN = re.compile(rf"^{re.escape(CMD)}ранг\s+(\d+)\b", re.IGNORECASE)
UNRANK_PATTERN = re.compile(rf"^{re.escape(CMD)}разжаловать\b", re.IGNORECASE)

@group_router.message(F.text.regexp(SETRANK_PATTERN))
async def cmd_set_rank(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    if not is_main_admin(message.from_user.username):
        return await message.reply("⛔ Только создатель может назначать ранги.")
    m = SETRANK_PATTERN.match(message.text)
    rank = int(m.group(1))
    if rank < 1 or rank > RANK_MAX:
        return await message.reply(f"❌ Ранг от 1 до {RANK_MAX}.")
    rest = SETRANK_PATTERN.sub("", message.text, count=1).strip()
    uid, name = await resolve_target(message, rest)
    if not uid:
        return await message.reply("🔍 Укажи @username, ID или ответь на сообщение.")
    await set_rank(uid, rank)
    await message.answer(
        f"{RANK_STARS[rank]} {user_mention(uid, name)} — ранг <b>{rank}</b> установлен."
    )

@group_router.message(F.text.regexp(UNRANK_PATTERN))
async def cmd_unrank(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    if not is_main_admin(message.from_user.username):
        return await message.reply("⛔ Только создатель.")
    rest = UNRANK_PATTERN.sub("", message.text, count=1).strip()
    uid, name = await resolve_target(message, rest)
    if not uid:
        return await message.reply("🔍 Кого?")
    await set_rank(uid, 0)
    await message.answer(f"❌ {user_mention(uid, name)} лишён ранга.")

# ================= БЕЛЫЙ СПИСОК =================
WL_ADD_PATTERN = re.compile(rf"^{re.escape(CMD)}бс\s+(?:добавить|доб|add)\b", re.IGNORECASE)
WL_REM_PATTERN = re.compile(rf"^{re.escape(CMD)}бс\s+(?:убрать|убр|del|remove)\b", re.IGNORECASE)
WL_LIST_PATTERN = re.compile(rf"^{re.escape(CMD)}(?:белый список|бс)\s*$", re.IGNORECASE)
WL_CHECK_PATTERN = re.compile(rf"^{re.escape(CMD)}проверить бс\b", re.IGNORECASE)

@group_router.message(F.text.regexp(WL_ADD_PATTERN))
async def cmd_wl_add(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    if await get_actor_rank(message) < 3:
        return await message.reply("⛔ Только ранг 3 может добавлять в белый список.")
    rest = WL_ADD_PATTERN.sub("", message.text, count=1).strip()
    uid, name = await resolve_target(message, rest)
    if not uid:
        return await message.reply("🔍 Кого добавить? Укажи @username, ID или ответь.")
    await wl_add(uid, message.chat.id, message.from_user.id)
    await message.answer(
        f"🛡 {user_mention(uid, name)} добавлен в белый список.\n"
        f"<i>Любые наказания от Iris будут автоматически сняты, "
        f"а пользователю придёт ссылка в ЛС.</i>"
    )

@group_router.message(F.text.regexp(WL_REM_PATTERN))
async def cmd_wl_remove(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    if await get_actor_rank(message) < 3:
        return await message.reply("⛔ Только ранг 3.")
    rest = WL_REM_PATTERN.sub("", message.text, count=1).strip()
    uid, name = await resolve_target(message, rest)
    if not uid:
        return await message.reply("🔍 Кого?")
    await wl_remove(uid, message.chat.id)
    await message.answer(f"✅ {user_mention(uid, name)} убран из белого списка.")

@group_router.message(F.text.regexp(WL_LIST_PATTERN))
async def cmd_wl_list(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    rows = await wl_list(message.chat.id)
    if not rows:
        return await message.reply("📭 Белый список пуст.")
    text = "🛡 <b>Белый список:</b>\n\n"
    for i, (uid, by_id, at) in enumerate(rows, 1):
        name = await get_display_name(uid)
        by_name = await get_display_name(by_id)
        date = time.strftime("%d.%m.%Y", time.localtime(at))
        text += f"{i}. {user_mention(uid, name)} — добавил {escape(by_name)} ({date})\n"
    await message.answer(text)

@group_router.message(F.text.regexp(WL_CHECK_PATTERN))
async def cmd_wl_check(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    ids = await wl_all_ids(message.chat.id)
    if not ids:
        return await message.reply("📭 Белый список пуст.")
    unbanned = 0
    unmuted = 0
    for uid in ids:
        try:
            member = await bot.get_chat_member(message.chat.id, uid)
            if member.status == "kicked":
                await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
                await send_wl_dm(uid, message.chat.title or "чат", message.chat.id, "бан от Iris")
                unbanned += 1
            elif member.status == "restricted" and not member.can_send_messages:
                await bot.restrict_chat_member(
                    message.chat.id, uid, permissions=full_unmute_perms())
                unmuted += 1
        except TelegramAPIError:
            pass
    parts = []
    if unbanned: parts.append(f"разбанено: {unbanned}")
    if unmuted: parts.append(f"размучено: {unmuted}")
    if parts:
        await message.answer("🔄 " + ", ".join(parts))
    else:
        await message.answer("✅ Все из белого списка в порядке.")

# ================= АДМИНЫ =================
ADMINS_PATTERN = re.compile(rf"^{re.escape(CMD)}админы\b", re.IGNORECASE)

@group_router.message(F.text.regexp(ADMINS_PATTERN))
async def cmd_admins(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
    except TelegramAPIError:
        return await message.reply("❌ Не могу получить список.")
    creator = []
    tg_admins = []
    for a in admins:
        if a.user.is_bot:
            continue
        name = await get_display_name(a.user.id)
        link = user_mention(a.user.id, name)
        if a.status == "creator":
            creator.append(link)
        else:
            tg_admins.append(link)

    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT user_id, rank FROM ranks WHERE rank>0 ORDER BY rank DESC")
        custom = await cur.fetchall()

    text = "👥 <b>Администрация:</b>\n\n"
    if creator:
        text += "👑 <b>Создатель чата:</b>\n" + "\n".join(f"• {u}" for u in creator) + "\n\n"
    if custom:
        text += "🎖 <b>Ранги бота:</b>\n"
        for uid, rank in custom:
            name = await get_display_name(uid)
            text += f"{RANK_STARS.get(rank, '⭐️')} {user_mention(uid, name)}\n"
        text += "\n"
    if tg_admins:
        text += "🤖 <b>Telegram-админы:</b>\n" + "\n".join(f"• {u}" for u in tg_admins)
    await message.answer(text)

# ================= БАН =================
BAN_PATTERN = re.compile(rf"^{re.escape(CMD)}бан\b", re.IGNORECASE)
UNBAN_PATTERN = re.compile(rf"^{re.escape(CMD)}(?:разбан|unban)\b", re.IGNORECASE)
BANLIST_PATTERN = re.compile(rf"^{re.escape(CMD)}банлист\b", re.IGNORECASE)

@group_router.message(F.text.regexp(BAN_PATTERN))
async def cmd_ban(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    rest = BAN_PATTERN.sub("", message.text, count=1).strip()
    lines = rest.split("\n", 1)
    first = lines[0].strip()
    reason = lines[1].strip() if len(lines) > 1 else "Без причины"

    period = None
    parts = first.split(maxsplit=1)
    if parts and parse_period(parts[0]):
        period = parse_period(parts[0])
        target_arg = parts[1] if len(parts) > 1 else ""
    else:
        target_arg = first

    uid, name = await resolve_target(message, target_arg)
    if not uid:
        return await message.reply("🔍 Кого банить? Укажи @username или ответь на сообщение.")
    ok, err = await check_target(message, uid)
    if not ok:
        return await message.reply(err)

    until = int(time.time()) + period if period else 0
    try:
        await bot.ban_chat_member(message.chat.id, uid)
        await add_ban(uid, message.chat.id, until, reason, message.from_user.id)
        suffix = await target_suffix(uid, message.chat.id)
        mod_mention = user_mention(message.from_user.id, message.from_user.full_name)
        await message.answer(
            f"🔨 {user_mention(uid, name)} забанен\n"
            f"📝 Причина: <i>{escape(reason)}</i>\n"
            f"⏳ Срок: <b>{fmt_period(period) if period else 'навсегда'}</b>\n"
            f"👤 Модератор: {mod_mention}{suffix}"
        )
    except TelegramAPIError as e:
        logging.error(f"ban error: {e}")
        await message.reply("❌ Не удалось. Возможно, цель — создатель чата.")

@group_router.message(F.text.regexp(UNBAN_PATTERN))
async def cmd_unban(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    rest = UNBAN_PATTERN.sub("", message.text, count=1).strip()
    uid, name = await resolve_target(message, rest)
    if not uid:
        return await message.reply("🔍 Кого?")
    try:
        await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
        await remove_ban(uid, message.chat.id)
        await message.answer(f"✅ {user_mention(uid, name)} разбанен.")
    except TelegramAPIError:
        await message.reply("❌ Не удалось.")

@group_router.message(F.text.regexp(BANLIST_PATTERN))
async def cmd_banlist(message: Message):
    if not await check_actor(message):
        return
    rows = await get_banlist(message.chat.id)
    if not rows:
        return await message.reply("📭 Банлист пуст.")
    text = "📋 <b>Забаненные:</b>\n\n"
    for i, (uid, reason) in enumerate(rows, 1):
        name = await get_display_name(uid)
        text += f"{i}. {user_mention(uid, name)} — <i>{escape(reason or '—')}</i>\n"
    await message.answer(text)

# ================= КИК =================
KICK_PATTERN = re.compile(rf"^{re.escape(CMD)}кик\b", re.IGNORECASE)

@group_router.message(F.text.regexp(KICK_PATTERN))
async def cmd_kick(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    rest = KICK_PATTERN.sub("", message.text, count=1).strip()
    uid, name = await resolve_target(message, rest)
    if not uid:
        return await message.reply("🔍 Кого кикнуть?")
    ok, err = await check_target(message, uid)
    if not ok:
        return await message.reply(err)
    try:
        await bot.ban_chat_member(message.chat.id, uid)
        await bot.unban_chat_member(message.chat.id, uid, only_if_banned=True)
        suffix = await target_suffix(uid, message.chat.id)
        mod_mention = user_mention(message.from_user.id, message.from_user.full_name)
        await message.answer(
            f"👞 {user_mention(uid, name)} кикнут\n"
            f"👤 Модератор: {mod_mention}{suffix}"
        )
    except TelegramAPIError:
        await message.reply("❌ Не удалось.")

# ================= МУТ =================
MUTE_PATTERN = re.compile(rf"^{re.escape(CMD)}мут\b", re.IGNORECASE)
UNMUTE_PATTERN = re.compile(rf"^{re.escape(CMD)}(?:размут|unmute)\b", re.IGNORECASE)

@group_router.message(F.text.regexp(MUTE_PATTERN))
async def cmd_mute(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    rest = MUTE_PATTERN.sub("", message.text, count=1).strip()
    lines = rest.split("\n", 1)
    first = lines[0].strip()
    reason = lines[1].strip() if len(lines) > 1 else "Без причины"

    period = None
    parts = first.split(maxsplit=1)
    if parts and parse_period(parts[0]):
        period = parse_period(parts[0])
        target_arg = parts[1] if len(parts) > 1 else ""
    else:
        target_arg = first

    uid, name = await resolve_target(message, target_arg)
    if not uid:
        return await message.reply("🔍 Кого мутить?")
    ok, err = await check_target(message, uid)
    if not ok:
        return await message.reply(err)

    if not period:
        period = 604800
    until = int(time.time()) + period
    try:
        await bot.restrict_chat_member(
            message.chat.id, uid,
            permissions=full_mute_perms(),
            until_date=until)
        suffix = await target_suffix(uid, message.chat.id)
        mod_mention = user_mention(message.from_user.id, message.from_user.full_name)
        await message.answer(
            f"🔇 {user_mention(uid, name)} в муте\n"
            f"📝 Причина: <i>{escape(reason)}</i>\n"
            f"⏳ Срок: <b>{fmt_period(period)}</b>\n"
            f"👤 Модератор: {mod_mention}{suffix}"
        )
    except TelegramAPIError as e:
        logging.error(f"mute error: {e}")
        await message.reply("❌ Не удалось. Проверь мои права.")

@group_router.message(F.text.regexp(UNMUTE_PATTERN))
async def cmd_unmute(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    rest = UNMUTE_PATTERN.sub("", message.text, count=1).strip()
    uid, name = await resolve_target(message, rest)
    if not uid:
        return await message.reply("🔍 Кого?")
    try:
        await bot.restrict_chat_member(
            message.chat.id, uid,
            permissions=full_unmute_perms())
        await message.answer(f"🔊 {user_mention(uid, name)} размучен.")
    except TelegramAPIError:
        await message.reply("❌ Не удалось.")

# ================= ВАРН =================
WARN_PATTERN = re.compile(rf"^{re.escape(CMD)}варн\b", re.IGNORECASE)
WARNS_PATTERN = re.compile(rf"^{re.escape(CMD)}варны\b", re.IGNORECASE)
UNWARN_PATTERN = re.compile(rf"^{re.escape(CMD)}(?:-варн|снять варн)\b", re.IGNORECASE)

@group_router.message(F.text.regexp(WARN_PATTERN))
async def cmd_warn(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    rest = WARN_PATTERN.sub("", message.text, count=1).strip()
    lines = rest.split("\n", 1)
    first = lines[0].strip()
    reason = lines[1].strip() if len(lines) > 1 else "Без причины"

    uid, name = await resolve_target(message, first)
    if not uid:
        return await message.reply("🔍 Кому варн?")
    ok, err = await check_target(message, uid)
    if not ok:
        return await message.reply(err)

    count = await add_warn(uid, message.chat.id, reason, message.from_user.id)

    if count >= 3:
        try:
            await bot.ban_chat_member(message.chat.id, uid)
            await clear_warns(uid, message.chat.id)
            suffix = await target_suffix(uid, message.chat.id)
            await message.answer(
                f"🔴 {user_mention(uid, name)} получил 3/3 варна и забанен.\n"
                f"📝 Причина: <i>{escape(reason)}</i>{suffix}"
            )
        except TelegramAPIError:
            await message.reply("❌ Не удалось забанить за варны.")
    else:
        mod_mention = user_mention(message.from_user.id, message.from_user.full_name)
        await message.answer(
            f"⚠️ {user_mention(uid, name)} — предупреждение <b>{count}/3</b>\n"
            f"📝 Причина: <i>{escape(reason)}</i>\n"
            f"👤 Модератор: {mod_mention}"
        )

@group_router.message(F.text.regexp(WARNS_PATTERN))
async def cmd_warns(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    rest = WARNS_PATTERN.sub("", message.text, count=1).strip()
    uid, name = await resolve_target(message, rest)
    if not uid:
        return await message.reply("🔍 Кому?")
    rows = await get_warns(uid, message.chat.id)
    if not rows:
        return await message.answer(f"✅ У {user_mention(uid, name)} нет варнов.")
    text = f"⚠️ <b>Варны {escape(name)}:</b>\n\n"
    for i, (reason,) in enumerate(rows, 1):
        text += f"{i}. <i>{escape(reason)}</i>\n"
    await message.answer(text)

@group_router.message(F.text.regexp(UNWARN_PATTERN))
async def cmd_unwarn(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    rest = UNWARN_PATTERN.sub("", message.text, count=1).strip()
    uid, name = await resolve_target(message, rest)
    if not uid:
        return await message.reply("🔍 Кому?")
    await clear_warns(uid, message.chat.id, count=1)
    await message.answer(f"✅ С {user_mention(uid, name)} снят один варн.")

# ================= ЗАЩИТА БЕЛОГО СПИСКА ОТ IRIS =================
@group_router.chat_member()
async def wl_guard(event: ChatMemberUpdated, bot: Bot):
    """Ловит любые действия Iris над пользователями из БС и откатывает."""
    if event.chat.type not in ("group", "supergroup"):
        return

    target_uid = event.new_chat_member.user.id

    # Нас и ботов игнорируем
    if target_uid == bot.id or event.new_chat_member.user.is_bot:
        return

    # Только для БС
    if not await wl_is(target_uid, event.chat.id):
        return

    # Проверяем права бота
    try:
        me = await bot.get_chat_member(event.chat.id, bot.id)
        if me.status != "administrator":
            return
    except TelegramAPIError:
        return

    new = event.new_chat_member
    chat_title = event.chat.title or "чат"

    # --- БАН (kicked) ---
    if new.status == "kicked":
        if not getattr(me, "can_restrict_members", False):
            return
        await asyncio.sleep(2)  # даём Iris закончить
        try:
            await bot.unban_chat_member(event.chat.id, target_uid, only_if_banned=True)
            logging.info(f"WL: разбанен {target_uid} в {event.chat.id}")
        except TelegramAPIError as e:
            logging.error(f"WL unban failed: {e}")
            return

        # Пишем в ЛС со ссылкой
        await send_wl_dm(target_uid, chat_title, event.chat.id, "бан от Iris")

        # Уведомление в чат
        try:
            name = await get_display_name(target_uid)
            await bot.send_message(
                event.chat.id,
                f"🛡 {user_mention(target_uid, name)} в белом списке — "
                f"разбанен автоматически.\n"
                f"<i>увы, но права Iris тут ничего не решают</i>"
            )
        except TelegramAPIError:
            pass

    # --- МУТ (restricted с can_send_messages=False) ---
    elif new.status == "restricted":
        if not getattr(me, "can_restrict_members", False):
            return
        # Проверяем, что реально ограничен
        if getattr(new, "can_send_messages", True):
            return
        await asyncio.sleep(2)
        try:
            await bot.restrict_chat_member(
                event.chat.id, target_uid,
                permissions=full_unmute_perms())
            logging.info(f"WL: размучен {target_uid} в {event.chat.id}")
        except TelegramAPIError as e:
            logging.error(f"WL unmute failed: {e}")
            return

        try:
            name = await get_display_name(target_uid)
            await bot.send_message(
                event.chat.id,
                f"🛡 {user_mention(target_uid, name)} в белом списке — "
                f"мут снят автоматически.\n"
                f"<i>увы, но права Iris тут ничего не решают</i>"
            )
        except TelegramAPIError:
            pass

# ================= ЗАПУСК =================
async def on_startup(bot: Bot):
    await init_db()
    for uname in MAIN_ADMINS:
        uid = await find_uid_by_username(uname)
        if uid:
            await set_rank(uid, RANK_MAX)
    await bot.set_webhook(
        WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=["message", "chat_member", "my_chat_member", "callback_query"]
    )
    logging.info(f"Вебхук установлен: {WEBHOOK_URL}")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook(drop_pending_updates=True)

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s")
    global bot
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.update.middleware(UserCache())
    dp.include_routers(pm_router, group_router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info(f"Сервер на порту {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Остановлен.")

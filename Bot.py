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
from aiogram.types import Message, TelegramObject, ChatPermissions
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.exceptions import TelegramAPIError

# ================= КОНФИГ =================
BOT_TOKEN = "8823945629:AAHfN3LN7lFahjV7kSC5I8f8SXfM4mvCbKQ"
WEBHOOK_URL = "https://telegram-bot-qxtd.onrender.com/webhook"
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"

# Кто может пользоваться ботом (без @)
ALLOWED_USERNAMES = ["Woozinoid", "roman3801", "durovgar"]

# Кто может назначать ранги (без @)
MAIN_ADMINS = ["Woozinoid"]

RANK_MAX = 3
RANK_STARS = {1: "⭐️", 2: "⭐️⭐️", 3: "⭐️⭐️⭐️"}

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
    if s >= 2592000:
        return f"{s // 2592000} мес."
    if s >= 604800:
        return f"{s // 604800} нед."
    if s >= 86400:
        return f"{s // 86400} дн."
    if s >= 3600:
        return f"{s // 3600} ч."
    if s >= 60:
        return f"{s // 60} мин."
    return f"{s} сек."

# ================= БАЗА ДАННЫХ =================
async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ranks (
                user_id INTEGER PRIMARY KEY,
                rank INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                reason TEXT,
                warned_by INTEGER,
                warned_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                user_id INTEGER,
                chat_id INTEGER,
                until INTEGER,
                reason TEXT,
                banned_by INTEGER,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        await db.commit()

async def save_user(uid, username, full_name):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
            (uid, username, full_name)
        )
        await db.commit()

async def get_username(uid):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute("SELECT username FROM users WHERE user_id=?", (uid,))
        r = await cur.fetchone()
        return r[0] if r else None

async def get_display_name(uid):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT username, full_name FROM users WHERE user_id=?", (uid,)
        )
        r = await cur.fetchone()
        if r:
            return f"@{r[0]}" if r[0] else (r[1] or str(uid))
    return str(uid)

async def get_rank(uid):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute("SELECT rank FROM ranks WHERE user_id=?", (uid,))
        r = await cur.fetchone()
        return r[0] if r else 0

async def set_rank(uid, rank):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO ranks (user_id, rank) VALUES (?, ?)",
            (uid, rank)
        )
        await db.commit()

async def find_uid_by_username(username):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT user_id FROM users WHERE username=? COLLATE NOCASE",
            (username,)
        )
        r = await cur.fetchone()
        return r[0] if r else None

async def add_warn(uid, chat_id, reason, by_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT INTO warns (user_id, chat_id, reason, warned_by, warned_at) VALUES (?, ?, ?, ?, ?)",
            (uid, chat_id, reason, by_id, int(time.time()))
        )
        await db.commit()
        cur = await db.execute(
            "SELECT COUNT(*) FROM warns WHERE user_id=? AND chat_id=?",
            (uid, chat_id)
        )
        row = await cur.fetchone()
        return row[0]

async def clear_warns(uid, chat_id, count=None):
    async with aiosqlite.connect("bot.db") as db:
        if count is None:
            await db.execute(
                "DELETE FROM warns WHERE user_id=? AND chat_id=?",
                (uid, chat_id)
            )
        else:
            await db.execute("""
                DELETE FROM warns WHERE id IN (
                    SELECT id FROM warns WHERE user_id=? AND chat_id=? ORDER BY id DESC LIMIT ?
                )
            """, (uid, chat_id, count))
        await db.commit()

async def add_ban(uid, chat_id, until, reason, by_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO bans (user_id, chat_id, until, reason, banned_by) VALUES (?, ?, ?, ?, ?)",
            (uid, chat_id, until, reason, by_id)
        )
        await db.commit()

async def remove_ban(uid, chat_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "DELETE FROM bans WHERE user_id=? AND chat_id=?",
            (uid, chat_id)
        )
        await db.commit()

async def get_banlist(chat_id):
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT user_id, reason FROM bans WHERE chat_id=? ORDER BY rowid DESC LIMIT 20",
            (chat_id,)
        )
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

    actor_rank = await get_actor_rank(message)
    target_uname = await get_username(target_uid)
    target_rank = await get_rank(target_uid)

    if is_allowed(target_uname) and target_rank >= actor_rank:
        return False, "🔒 Пользователь защищён рангом."
    return True, ""

# ================= ВСПОМОГАТЕЛЬНОЕ =================
async def resolve_target(message, args):
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        await save_user(u.id, u.username or "", u.full_name)
        return u.id, escape(u.full_name)
    if args:
        arg = args.strip().split()[0]
        if arg.isdigit():
            uid = int(arg)
            return uid, escape(await get_display_name(uid))
        if arg.startswith("@"):
            uid = await find_uid_by_username(arg[1:])
            if uid:
                return uid, escape(await get_display_name(uid))
    return None, None

def full_mute_perms():
    return ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )

def full_unmute_perms():
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )

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
        f"Я — модератор-бот с ранговой системой.\n\n"
        f"<b>Команды:</b>\n"
        f"🔨 <code>бан [срок] @user</code>\n"
        f"🔇 <code>мут [срок] @user</code>\n"
        f"👞 <code>кик @user</code>\n"
        f"⚠️ <code>варн @user</code>\n"
        f"➕ <code>+модер 1|2|3 @user</code>\n"
        f"➖ <code>-модер @user</code>\n\n"
        f"<i>Срок: 2ч, 3 дня, 1 нед, 30 мин</i>"
    )

# ================= +МОДЕР =================
SETRANK_PATTERN = re.compile(r"^\+модер\s+(\d+)\b", re.IGNORECASE)
UNRANK_PATTERN = re.compile(r"^(?:-модер|снять модер)\b", re.IGNORECASE)

@group_router.message(F.text.regexp(SETRANK_PATTERN))
async def cmd_set_rank(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    if not is_main_admin(message.from_user.username):
        return await message.reply("⛔ Назначать ранги может только создатель.")
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
        f"{RANK_STARS[rank]} <b>{name}</b> — ранг <b>{rank}</b> установлен."
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
    await message.answer(f"❌ {name} лишён ранга.")

# ================= БАН =================
BAN_PATTERN = re.compile(r"^(?:/|!)?бан\b", re.IGNORECASE)
UNBAN_PATTERN = re.compile(r"^(?:разбан|unban)\b", re.IGNORECASE)
BANLIST_PATTERN = re.compile(r"^банлист\b", re.IGNORECASE)

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
        return await message.reply("🔍 Кого банить? Укажи @username или ответь.")

    ok, err = await check_target(message, uid)
    if not ok:
        return await message.reply(err)

    until = int(time.time()) + period if period else 0
    try:
        await bot.ban_chat_member(message.chat.id, uid)
        await add_ban(uid, message.chat.id, until, reason, message.from_user.id)
        mod_name = escape(message.from_user.full_name)
        await message.answer(
            f"🔨 <b>{name}</b> забанен\n"
            f"📝 Причина: <i>{escape(reason)}</i>\n"
            f"⏳ Срок: <b>{fmt_period(period) if period else 'навсегда'}</b>\n"
            f"👤 Модератор: {mod_name}"
        )
    except TelegramAPIError as e:
        logging.error(f"ban error: {e}")
        await message.reply("❌ Не удалось. Возможно, цель — создатель или у меня нет прав.")

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
        await message.answer(f"✅ <b>{name}</b> разбанен.")
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
        text += f"{i}. {escape(await get_display_name(uid))} — <i>{escape(reason or '—')}</i>\n"
    await message.answer(text)

# ================= КИК =================
KICK_PATTERN = re.compile(r"^кик\b", re.IGNORECASE)

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
        await message.answer(
            f"👞 <b>{name}</b> кикнут\n"
            f"👤 Модератор: {escape(message.from_user.full_name)}"
        )
    except TelegramAPIError:
        await message.reply("❌ Не удалось.")

# ================= МУТ =================
MUTE_PATTERN = re.compile(r"^мут\b", re.IGNORECASE)
UNMUTE_PATTERN = re.compile(r"^(?:размут|говори)\b", re.IGNORECASE)

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
            message.chat.id,
            uid,
            permissions=full_mute_perms(),
            until_date=until
        )
        await message.answer(
            f"🔇 <b>{name}</b> в муте\n"
            f"📝 Причина: <i>{escape(reason)}</i>\n"
            f"⏳ Срок: <b>{fmt_period(period)}</b>\n"
            f"👤 Модератор: {escape(message.from_user.full_name)}"
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
            message.chat.id,
            uid,
            permissions=full_unmute_perms()
        )
        await message.answer(f"🔊 <b>{name}</b> размучен.")
    except TelegramAPIError:
        await message.reply("❌ Не удалось.")

# ================= ВАРН =================
WARN_PATTERN = re.compile(r"^варн\b", re.IGNORECASE)
WARNS_PATTERN = re.compile(r"^варны\b", re.IGNORECASE)
UNWARN_PATTERN = re.compile(r"^(?:-варн|снять варн)\b", re.IGNORECASE)

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
            await message.answer(
                f"🔴 <b>{name}</b> получил 3/3 варна и забанен.\n"
                f"📝 Причина: <i>{escape(reason)}</i>"
            )
        except TelegramAPIError:
            await message.reply("❌ Не удалось забанить за варны.")
    else:
        await message.answer(
            f"⚠️ <b>{name}</b> — предупреждение <b>{count}/3</b>\n"
            f"📝 Причина: <i>{escape(reason)}</i>\n"
            f"👤 Модератор: {escape(message.from_user.full_name)}"
        )

@group_router.message(F.text.regexp(WARNS_PATTERN))
async def cmd_warns(message: Message, bot: Bot):
    if not await check_actor(message):
        return
    rest = WARNS_PATTERN.sub("", message.text, count=1).strip()
    uid, name = await resolve_target(message, rest)
    if not uid:
        return await message.reply("🔍 Кому?")
    async with aiosqlite.connect("bot.db") as db:
        cur = await db.execute(
            "SELECT reason FROM warns WHERE user_id=? AND chat_id=?",
            (uid, message.chat.id)
        )
        rows = await cur.fetchall()
    if not rows:
        return await message.answer(f"✅ У <b>{name}</b> нет варнов.")
    text = f"⚠️ <b>Варны {name}:</b>\n\n"
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
    await message.answer(f"✅ С <b>{name}</b> снят один варн.")

# ================= ЗАПУСК =================
async def on_startup(bot: Bot):
    await init_db()
    for uname in MAIN_ADMINS:
        uid = await find_uid_by_username(uname)
        if uid:
            await set_rank(uid, RANK_MAX)
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logging.info(f"Вебхук установлен: {WEBHOOK_URL}")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook(drop_pending_updates=True)

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
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

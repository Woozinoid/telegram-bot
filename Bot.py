import os
import sys
import logging
import asyncio
import aiosqlite
import re
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Callable, Dict, Any, Awaitable
from aiohttp import web

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, TelegramObject, InlineKeyboardMarkup,
    InlineKeyboardButton, ChatPermissions
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.exceptions import TelegramAPIError

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = "8823945629:AAHfN3LN7lFahjV7kSC5I8f8SXfM4mvCbKQ"
WEBHOOK_URL = "https://telegram-bot-qxtd.onrender.com/webhook"
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"

# ================= РЕГУЛЯРНЫЕ ВЫРАЖЕНИЯ =================
BAN_PATTERN     = re.compile(r"^(?:/|!)?(?:бан|ban)\b", re.IGNORECASE)
UNBAN_PATTERN   = re.compile(r"^(?:/|!)?(?:разбан|unban)\b", re.IGNORECASE)
KICK_PATTERN    = re.compile(r"^(?:/|!)?(?:кик|kick)\b", re.IGNORECASE)
MUTE_PATTERN    = re.compile(r"^(?:/|!)?(?:мут|mute)\b", re.IGNORECASE)
UNMUTE_PATTERN  = re.compile(r"^(?:/|!)?(?:размут|unmute)\b", re.IGNORECASE)
WARN_PATTERN    = re.compile(r"^(?:/|!)?(?:варн|warn)\b", re.IGNORECASE)
UNWARN_PATTERN  = re.compile(r"^(?:/|!)?(?:-варн|снять варн|unwarn)\b", re.IGNORECASE)
ADMINS_PATTERN  = re.compile(r"^(?:кто админ|админы|/admins)\b", re.IGNORECASE)
BANLIST_PATTERN = re.compile(r"^(?:бан лист|банлист|/banlist)\b", re.IGNORECASE)

# ================= БАЗА ДАННЫХ =================
async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                admin_id INTEGER,
                target_name TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warns (
                user_id INTEGER,
                chat_id INTEGER,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        await db.commit()

# ================= MIDDLEWARE КЭША ПОЛЬЗОВАТЕЛЕЙ =================
class UserCacheMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            async with aiosqlite.connect("bot.db") as db:
                await db.execute(
                    "INSERT OR REPLACE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
                    (user.id, user.username, user.full_name)
                )
                await db.commit()
        return await handler(event, data)

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
async def get_user_from_args(args: str) -> tuple[int, str] | None:
    if not args:
        return None
    arg = args.strip()
    if arg.isdigit():
        return int(arg), f"ID: {arg}"
    if arg.startswith("@"):
        username = arg[1:]
        async with aiosqlite.connect("bot.db") as db:
            async with db.execute(
                "SELECT user_id, full_name FROM users WHERE username = ? COLLATE NOCASE",
                (username,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return row[0], row[1]
    return None


async def check_admin_rights(
    message: Message,
    bot: Bot,
    target_id: int | None = None,
    check_target: bool = True,
    need_restrict: bool = False,
) -> bool:
    try:
        bot_member = await message.chat.get_member(bot.id)
        if bot_member.status not in ("administrator", "creator"):
            await message.reply("❌ У меня нет прав администратора в этом чате.")
            return False
        if need_restrict and not getattr(bot_member, "can_restrict_members", False):
            await message.reply("❌ У меня нет права ограничивать участников.")
            return False

        user_member = await message.chat.get_member(message.from_user.id)
        if user_member.status not in ("administrator", "creator"):
            await message.reply("⛔ Эта команда доступна только администраторам.")
            return False

        if check_target and target_id:
            try:
                target_member = await message.chat.get_member(target_id)
                if target_member.status in ("administrator", "creator"):
                    await message.reply("⚠️ Нельзя применить это к администратору.")
                    return False
            except TelegramAPIError:
                pass

        return True
    except TelegramAPIError as e:
        logging.warning(f"Ошибка проверки прав: {e}")
        return True


async def resolve_target(message: Message, args: str) -> tuple[int, str] | tuple[None, None]:
    if message.reply_to_message and message.reply_to_message.from_user:
        return (
            message.reply_to_message.from_user.id,
            message.reply_to_message.from_user.full_name,
        )
    elif args:
        user_data = await get_user_from_args(args)
        if user_data:
            return user_data[0], user_data[1]
    return None, None


def full_mute_permissions() -> ChatPermissions:
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


def full_unmute_permissions() -> ChatPermissions:
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
async def cmd_start_pm(message: Message, bot: Bot):
    me = await bot.get_me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="➕ Добавить в свою группу",
            url=f"https://t.me/{me.username}?startgroup=true"
        )
    ]])
    await message.answer(
        f"Привет, <b>{escape(message.from_user.first_name)}</b>! 👋\n\n"
        "Я — бот-менеджер для защиты ваших чатов.\n"
        "Добавь меня в свою группу и назначь администратором.",
        reply_markup=kb
    )

# ================= ИНФОРМАЦИОННЫЕ КОМАНДЫ =================
@group_router.message(F.text.regexp(ADMINS_PATTERN))
async def cmd_admins(message: Message, bot: Bot):
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        creators, senior_admins = [], []

        for admin in admins:
            if admin.user.is_bot:
                continue
            name = f"🏐 <a href='tg://user?id={admin.user.id}'>{escape(admin.user.full_name)}</a>"
            if admin.status == "creator":
                creators.append(name)
            else:
                senior_admins.append(name)

        text = ""
        if creators:
            text += "⭐️⭐️⭐️⭐️ <b>Создатели</b>\n" + "\n".join(creators) + "\n\n"
        if senior_admins:
            text += "⭐️⭐️⭐️ <b>Админы</b>\n" + "\n".join(senior_admins)
        if not text:
            text = "Администраторов не найдено (или они скрыты)."

        await message.answer(text)
    except TelegramAPIError:
        await message.reply("❌ Ошибка при получении списка администраторов.")


@group_router.message(F.text.regexp(BANLIST_PATTERN))
async def cmd_banlist(message: Message):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT target_name FROM bans WHERE chat_id = ? ORDER BY id DESC LIMIT 15",
            (message.chat.id,)
        ) as cursor:
            bans = await cursor.fetchall()

    if not bans:
        return await message.reply("📭 Бан-лист этого чата пуст.")

    text = "📋 <b>Последние забаненные:</b>\n\n"
    for idx, (name,) in enumerate(bans, 1):
        text += f"{idx}. <b>{escape(name)}</b>\n"

    await message.answer(text)

# ================= БАН =================
@group_router.message(F.text.regexp(BAN_PATTERN))
async def cmd_ban_text(message: Message, bot: Bot):
    args = BAN_PATTERN.sub("", message.text, count=1).strip()
    target_id, target_name = await resolve_target(message, args)

    if not target_id:
        return await message.reply(
            "🔍 Пользователь не найден. Ответьте на сообщение или укажите @username / ID."
        )
    if target_id == bot.id:
        return await message.reply("Я не могу заблокировать сам себя.")
    if not await check_admin_rights(message, bot, target_id):
        return

    target_name = escape(target_name)
    moderator = escape(message.from_user.full_name)

    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=target_id)
        async with aiosqlite.connect("bot.db") as db:
            await db.execute(
                "INSERT INTO bans (user_id, chat_id, admin_id, target_name) VALUES (?, ?, ?, ?)",
                (target_id, message.chat.id, message.from_user.id, target_name)
            )
            await db.commit()

        await message.answer(
            f"🔴 <b>{target_name}</b> получает бан навсегда\n"
            f"👺 Модератор: <b>{moderator}</b>"
        )
    except TelegramAPIError as e:
        logging.error(f"Ban error: {e}")
        await message.reply("❌ Не удалось заблокировать пользователя.")

# ================= РАЗБАН =================
@group_router.message(F.text.regexp(UNBAN_PATTERN))
async def cmd_unban_text(message: Message, bot: Bot):
    args = UNBAN_PATTERN.sub("", message.text, count=1).strip()
    target_id, target_name = await resolve_target(message, args)

    if not target_id:
        return await message.reply("🔍 Пользователь не найден.")
    if not await check_admin_rights(message, bot, check_target=False):
        return

    target_name = escape(target_name)
    moderator = escape(message.from_user.full_name)

    try:
        await bot.unban_chat_member(
            chat_id=message.chat.id, user_id=target_id, only_if_banned=True
        )
        async with aiosqlite.connect("bot.db") as db:
            await db.execute(
                "DELETE FROM bans WHERE user_id = ? AND chat_id = ?",
                (target_id, message.chat.id)
            )
            await db.commit()

        await message.answer(
            f"🟢 <b>{target_name}</b> разблокирован\n"
            f"🛡 Модератор: <b>{moderator}</b>"
        )
    except TelegramAPIError as e:
        logging.error(f"Unban error: {e}")
        await message.reply("❌ Не удалось разблокировать.")

# ================= КИК =================
@group_router.message(F.text.regexp(KICK_PATTERN))
async def cmd_kick(message: Message, bot: Bot):
    args = KICK_PATTERN.sub("", message.text, count=1).strip()
    target_id, target_name = await resolve_target(message, args)

    if not target_id:
        return await message.reply("🔍 Кого кикаем? Нужен реплай или @username.")
    if target_id == bot.id:
        return await message.reply("Я не могу кикнуть сам себя.")
    if not await check_admin_rights(message, bot, target_id):
        return

    target_name = escape(target_name)
    moderator = escape(message.from_user.full_name)

    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=target_id)
        await bot.unban_chat_member(
            chat_id=message.chat.id, user_id=target_id, only_if_banned=True
        )
        await message.answer(
            f"👞 <b>{target_name}</b> изгнан из чата (может сразу вернуться)\n"
            f"👺 Модератор: <b>{moderator}</b>"
        )
    except TelegramAPIError as e:
        logging.error(f"Kick error: {e}")
        await message.reply("❌ Не удалось кикнуть пользователя.")

# ================= МУТ =================
@group_router.message(F.text.regexp(MUTE_PATTERN))
async def cmd_mute(message: Message, bot: Bot):
    args = MUTE_PATTERN.sub("", message.text, count=1).strip()
    target_id, target_name = await resolve_target(message, args)

    if not target_id:
        return await message.reply("🔍 Кому даём мут? Нужен реплай или @username.")
    if target_id == bot.id:
        return
    if not await check_admin_rights(message, bot, target_id, need_restrict=True):
        return

    target_name = escape(target_name)
    moderator = escape(message.from_user.full_name)

    until = datetime.now(timezone.utc) + timedelta(days=7)

    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_id,
            permissions=full_mute_permissions(),
            until_date=until,
        )
        await message.answer(
            f"🔇 <b>{target_name}</b> лишается права слова на 7 дней\n"
            f"👺 Модератор: <b>{moderator}</b>"
        )
    except TelegramAPIError as e:
        logging.error(f"Mute error: {e}")
        await message.reply("❌ Не удалось замутить. Проверьте мои права.")

# ================= РАЗМУТ =================
@group_router.message(F.text.regexp(UNMUTE_PATTERN))
async def cmd_unmute(message: Message, bot: Bot):
    args = UNMUTE_PATTERN.sub("", message.text, count=1).strip()
    target_id, target_name = await resolve_target(message, args)

    if not target_id:
        return await message.reply("🔍 Кому возвращаем голос? Нужен реплай или @username.")
    if not await check_admin_rights(message, bot, check_target=False, need_restrict=True):
        return

    target_name = escape(target_name)

    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_id,
            permissions=full_unmute_permissions(),
        )
        await message.answer(
            f"✅ Пользователю <b>{target_name}</b> вернули право слова. "
            f"Следите за языком 😉"
        )
    except TelegramAPIError as e:
        logging.error(f"Unmute error: {e}")
        await message.reply("❌ Не удалось размутить.")

# ================= ВАРН =================
@group_router.message(F.text.regexp(WARN_PATTERN))
async def cmd_warn(message: Message, bot: Bot):
    args = WARN_PATTERN.sub("", message.text, count=1).strip()
    target_id, target_name = await resolve_target(message, args)

    if not target_id:
        return await message.reply("🔍 Кому выдаём варн? Нужен реплай или @username.")
    if target_id == bot.id:
        return
    if not await check_admin_rights(message, bot, target_id):
        return

    target_name = escape(target_name)
    moderator = escape(message.from_user.full_name)

    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT count FROM warns WHERE user_id = ? AND chat_id = ?",
            (target_id, message.chat.id)
        ) as cursor:
            row = await cursor.fetchone()
            current_warns = row[0] if row else 0

        current_warns += 1

        if current_warns >= 3:
            try:
                await bot.ban_chat_member(chat_id=message.chat.id, user_id=target_id)
                await db.execute(
                    "DELETE FROM warns WHERE user_id = ? AND chat_id = ?",
                    (target_id, message.chat.id)
                )
                await db.commit()
                await message.answer(
                    f"🔴 <b>{target_name}</b> превысил лимит предупреждений (3/3) "
                    f"и получает бан навсегда\n"
                    f"👺 Модератор: <b>{moderator}</b>"
                )
            except TelegramAPIError as e:
                logging.error(f"Warn→Ban error: {e}")
                await message.reply("❌ Ошибка при выдаче бана за варны.")
        else:
            await db.execute(
                "INSERT OR REPLACE INTO warns (user_id, chat_id, count) VALUES (?, ?, ?)",
                (target_id, message.chat.id, current_warns)
            )
            await db.commit()
            await message.answer(
                f"❗️ <b>{target_name}</b> получает предупреждение ({current_warns}/3)\n"
                f"👮‍♂️ Модератор: <b>{moderator}</b>"
            )

# ================= СНЯТИЕ ВАРНА =================
@group_router.message(F.text.regexp(UNWARN_PATTERN))
async def cmd_unwarn(message: Message, bot: Bot):
    args = UNWARN_PATTERN.sub("", message.text, count=1).strip()
    target_id, target_name = await resolve_target(message, args)

    if not target_id:
        return await message.reply("🔍 Кому снимаем варн? Нужен реплай или @username.")
    if not await check_admin_rights(message, bot, check_target=False):
        return

    target_name = escape(target_name)

    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "DELETE FROM warns WHERE user_id = ? AND chat_id = ?",
            (target_id, message.chat.id)
        )
        await db.commit()

    await message.answer(
        f"✅ С пользователя <b>{target_name}</b> сняты все предупреждения."
    )

# ================= ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК =================
@group_router.errors()
@pm_router.errors()
async def global_error_handler(event, **kwargs):
    logging.error(f"Критическая ошибка: {event.exception}")
    return True

# ================= ЖИЗНЕННЫЙ ЦИКЛ =================
async def on_startup(bot: Bot):
    await init_db()
    logging.info(f"Установка вебхука: {WEBHOOK_URL}")
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)

async def on_shutdown(bot: Bot):
    logging.info("Удаление вебхука и остановка...")
    await bot.delete_webhook(drop_pending_updates=True)

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    dp.update.middleware(UserCacheMiddleware())
    dp.include_routers(pm_router, group_router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    logging.info(f"Сервер запущен на порту {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")

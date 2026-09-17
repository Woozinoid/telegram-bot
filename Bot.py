import os
import sys
import logging
import asyncio
import aiosqlite
from typing import Callable, Dict, Any, Awaitable
from aiohttp import web

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import Message, TelegramObject, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.exceptions import TelegramAPIError

# --- Конфигурация ---
# Токен и вебхук вставлены напрямую по твоему запросу. 
# Примечание: порт (PORT) мы оставляем через os.getenv, так как Render назначает его динамически.
BOT_TOKEN = "8823945629:AAHfN3LN7lFahjV7kSC5I8f8SXfM4mvCbKQ"
WEBHOOK_URL = "https://telegram-bot-qxtd.onrender.com/webhook"
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"

# --- Инициализация БД ---
async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                admin_id INTEGER,
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
        await db.commit()

# --- Middleware для кэширования пользователей ---
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

# --- Вспомогательные функции ---
async def get_user_from_args(args: str) -> tuple[int, str] | None:
    if not args:
        return None
        
    arg = args.strip()
    if arg.isdigit():
        return int(arg), f"ID: {arg}"
        
    if arg.startswith("@"):
        username = arg[1:]
        async with aiosqlite.connect("bot.db") as db:
            async with db.execute("SELECT user_id, full_name FROM users WHERE username = ? COLLATE NOCASE", (username,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return row[0], row[1]
    return None

async def check_admin_rights(message: Message, bot: Bot, target_id: int) -> bool:
    try:
        bot_member = await message.chat.get_member(bot.id)
        if bot_member.status not in ("administrator", "creator"):
            await message.reply("❌ У меня нет прав администратора в этом чате.")
            return False
            
        user_member = await message.chat.get_member(message.from_user.id)
        if user_member.status not in ("administrator", "creator"):
            await message.reply("⛔ Эта команда доступна только администраторам.")
            return False
            
        target_member = await message.chat.get_member(target_id)
        if target_member.status in ("administrator", "creator"):
            await message.reply("⚠️ Невозможно применить меру к другому администратору.")
            return False
            
        return True
    except TelegramAPIError as e:
        logging.warning(f"Ошибка проверки прав: {e}")
        return True 

# --- Роутеры ---
pm_router = Router()
pm_router.message.filter(F.chat.type == "private")

group_router = Router()
group_router.message.filter(F.chat.type.in_({"group", "supergroup"}))

# --- Команды Личных Сообщений ---
@pm_router.message(CommandStart())
async def cmd_start_pm(message: Message, bot: Bot):
    me = await bot.get_me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="➕ Добавить в свою группу", 
            url=f"https://t.me/{me.username}?startgroup=true"
        )
    ]])
    
    text = (
        f"Привет, <b>{message.from_user.first_name}</b>! 👋\n\n"
        "Я — бот-менеджер для защиты ваших чатов.\n"
        "Добавь меня в свою группу и назначь администратором, чтобы я мог "
        "удалять нарушителей по команде <code>/ban</code> и <code>/unban</code>."
    )
    await message.answer(text, reply_markup=kb)

# --- Команды Группы ---
@group_router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot, command: CommandObject):
    target_id = None
    target_name = "Пользователь"

    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.full_name
    elif command.args:
        user_data = await get_user_from_args(command.args)
        if user_data:
            target_id, target_name = user_data
        else:
            return await message.reply("🔍 Пользователь не найден в моей базе. Используйте ответ на сообщение (Reply) или точный ID.")
    else:
        return await message.reply("Используйте команду в ответ на сообщение, либо укажите <code>/ban @username</code> или <code>/ban ID</code>.")

    if target_id == bot.id:
        return await message.reply("Я не могу заблокировать сам себя.")

    if not await check_admin_rights(message, bot, target_id):
        return

    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=target_id)
        
        async with aiosqlite.connect("bot.db") as db:
            await db.execute(
                "INSERT INTO bans (user_id, chat_id, admin_id) VALUES (?, ?, ?)",
                (target_id, message.chat.id, message.from_user.id)
            )
            await db.commit()
            
        await message.answer(f"🔨 Пользователь <b>{target_name}</b> заблокирован.")
    except TelegramAPIError as e:
        logging.error(f"Ошибка API при бане: {e}")
        await message.reply("❌ Не удалось заблокировать пользователя.")

@group_router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot, command: CommandObject):
    target_id = None
    target_name = "Пользователь"

    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.full_name
    elif command.args:
        user_data = await get_user_from_args(command.args)
        if user_data:
            target_id, target_name = user_data
        else:
            return await message.reply("🔍 Пользователь не найден. Убедитесь, что он писал в этот чат ранее, или укажите его числовой ID.")
    else:
        return await message.reply("Используйте команду в ответ на сообщение, либо укажите <code>/unban @username</code> или <code>/unban ID</code>.")

    try:
        user_member = await message.chat.get_member(message.from_user.id)
        if user_member.status not in ("administrator", "creator"):
            return await message.reply("⛔ Эта команда доступна только администраторам.")
    except TelegramAPIError:
        pass

    try:
        await bot.unban_chat_member(chat_id=message.chat.id, user_id=target_id, only_if_banned=True)
        
        async with aiosqlite.connect("bot.db") as db:
            await db.execute("DELETE FROM bans WHERE user_id = ? AND chat_id = ?", (target_id, message.chat.id))
            await db.commit()
            
        await message.answer(f"🕊 Пользователь <b>{target_name}</b> разблокирован и может снова присоединиться к чату.")
    except TelegramAPIError as e:
        logging.error(f"Ошибка API при разбане: {e}")
        await message.reply("❌ Не удалось разблокировать. Возможно, он и не был в бане.")

# --- Глобальный обработчик ошибок ---
@group_router.errors()
@pm_router.errors()
async def global_error_handler(event, **kwargs):
    logging.error(f"Критическая ошибка: {event.exception}")
    return True

# --- Жизненный цикл бота ---
async def on_startup(bot: Bot):
    await init_db()
    logging.info(f"Установка вебхука на {WEBHOOK_URL}")
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)

async def on_shutdown(bot: Bot):
    logging.info("Удаление вебхука и остановка...")
    await bot.delete_webhook(drop_pending_updates=True)

async def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    
    dp.update.middleware(UserCacheMiddleware())
    dp.include_routers(pm_router, group_router)
    
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
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

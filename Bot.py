import os
import re
import sys
import logging
import asyncio
import time
from html import escape
from aiohttp import web

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.exceptions import TelegramAPIError

# ================= КОНФИГ =================
BOT_TOKEN = "8641527466:AAGSkaTzMJm5X6ExY3vVYRiMLxkwSxOOpnU"
WEBHOOK_URL = "https://telegram-bot-qxtd.onrender.com/webhook"
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_PATH = "/webhook"

CHANNEL_ID = -1002396609986
SUGGEST_GROUP_ID = -5369865912

ADMIN_USERNAMES = ["Woozinoid", "durovgar"]

PUBLISH_INTERVAL = 10  # 10 секунд

# ================= ХРАНИЛИЩА (в памяти) =================
post_queue = asyncio.Queue()
banned_users = {}      # {user_id: {"reason": str, "date": str, "by": str}}
daily_stats = {"date": None, "sent": 0, "rejected": 0}

# ================= МАТ-ФИЛЬТР =================
BAD_WORDS_PATTERN = re.compile(
    r"\b(ху(й|и|е|я|ё|л[оёе]|йн[её]й|йло|ли)|"
    r"пизд(а|ы|е|у|ой|юк|юл[её]й|обол|острад)|"
    r"еба(ть|л|н|ло|нут|льник|нёт)|"
    r"бля(дь|ть|д|дина|дство|дский|дки)|"
    r"сук(а|и|ой|ин|чка|чька|чьку)|"
    r"залуп(а|ы|е|ой|ушка)|"
    r"жоп(а|ы|е|ой|ушка|олиз)|"
    r"гандон|мудак|пидор|пидр|пидрила|лох|лошара|"
    r"у(е|ё)бок|у(е|ё)бище|мразь|тварь|сволочь|гнида|падла|шлюха|проститутка|"
    r"гомик|лезбиянка|трахать|трах|отсос|минет|ахуеть|ахуенно|охуеть|нихуя|нихера|похер|пофиг)\b",
    re.IGNORECASE
)

def has_bad_words(text: str) -> bool:
    return bool(BAD_WORDS_PATTERN.search(text))

# ================= ГРАММАТИКА =================
async def fix_grammar(text: str) -> str:
    import aiohttp
    url = "https://speller.yandex.net/services/spellservice.json/checkText"
    params = {"text": text, "lang": "ru", "options": 0}
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                result = await resp.json()
        if not result:
            return text
        for error in sorted(result, key=lambda x: x["pos"] + x["len"], reverse=True):
            if error["s"]:
                replacement = error["s"][0]
                s = error["pos"]
                e = error["pos"] + error["len"]
                text = text[:s] + replacement + text[e:]
        return text
    except Exception as e:
        logging.error(f"Speller error: {e}")
        return text

# ================= ПРАВА =================
def is_admin(user) -> bool:
    if not user or not user.username:
        return False
    return user.username.lower() in [u.lower() for u in ADMIN_USERNAMES]

def is_banned(uid: int) -> bool:
    return uid in banned_users

# ================= КЛАВИАТУРА =================
def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Мой пост")],
            [KeyboardButton(text="📨 Предложить новость")]
        ],
        resize_keyboard=True
    )

# ================= РОУТЕР =================
router = Router()

# ================= /start =================
@router.message(CommandStart())
async def cmd_start(message: Message):
    if is_banned(message.from_user.id):
        ban = banned_users[message.from_user.id]
        return await message.answer(
            f"⛔ <b>Вы забанены</b>\n"
            f"📅 {ban['date']}\n"
            f"📝 Причина: {escape(ban['reason'])}\n"
            f"🔓 Обратитесь к @Woozinoid",
            parse_mode="HTML"
        )

    await message.answer(
        "📨 <b>Добро пожаловать в предложку!</b>\n\n"
        "Отправь мне текст, фото или видео — я проверю грамматику "
        "и поставлю в очередь на публикацию в канале.\n\n"
        "⚠️ Мат запрещён.\n"
        "🕒 Посты выходят регулярно.",
        parse_mode="HTML",
        reply_markup=main_kb()
    )

# ================= МОЙ ПОСТ =================
@router.message(F.text == "📊 Мой пост")
async def my_post(message: Message):
    uid = message.from_user.id
    position = None
    for idx, item in enumerate(list(post_queue._queue)):
        if item.get("user_id") == uid:
            position = idx + 1
            break

    if position is None:
        return await message.answer("❌ У вас нет постов в очереди.")

    wait_sec = position * PUBLISH_INTERVAL
    hours = wait_sec // 3600
    minutes = (wait_sec % 3600) // 60
    seconds = wait_sec % 60

    parts = []
    if hours:
        parts.append(f"{hours} ч.")
    if minutes:
        parts.append(f"{minutes} мин.")
    if seconds or not parts:
        parts.append(f"{seconds} сек.")
    time_str = " ".join(parts)

    await message.answer(
        f"📊 Ваш пост на позиции <b>{position}</b>\n"
        f"⏳ Примерно через: <b>{time_str}</b>",
        parse_mode="HTML"
    )

# ================= ПОДСКАЗКА =================
@router.message(F.text == "📨 Предложить новость")
async def suggest_hint(message: Message):
    await message.answer("✏️ Просто отправь мне текст ( photoили фото/видео с подписью).=")

# ================= АДМИН-КОmessageМАНДЫ =================
@router..message(Command("ban"))
async def cmd_ban(message: Message):
    if not is_admin(message.from_user):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("Использование: /ban <user_id> <причина>")
    parts = args[1].split(maxsplit=1)
    if not parts[0].isdigit():
        return await message.reply("Укажи числовой ID.")
    uid = int(parts[0])
    reason = parts[1] if len(parts) > 1 else "Без причины"
    banned_users[uid] =photo {
        "reason": reason,
        "date":[- time.strftime("%d.%m.%Y %H:%M"),
        "by": message.from_user.username or message.from_user.full_name
    }
    try:
        await message.bot.send_message(
            uid,
            f"⛔ Вы забанены в предложке.\n"
            f"📝 Причина: {reason}\n"
            f"🔓 Обратитесь к @Woozinoid"
        )
    except TelegramAPIError:
        pass
    await message.reply(f"✅ {uid} забанен.")

@router.message(Command("unban"))
async def cmd_unban(message: Message):
    if not is_admin(message.from_user):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.reply("Использование: /unban <user_id>")
    uid = int(args[1])
    if uid in banned_users:
        del banned_users[uid]
        await message.reply(f"✅ {uid} разбанен.")
    else:
        await message.reply("❌ Не в бане.")

@router.message(Command("banlist"))
async def cmd_banlist(message: Message):
    if not is_admin(message.from_user):
        return
    if not banned_users:
        return await message.reply("📭 Банлист пуст.")
    text = "📋 <b>Забаненные:</b>\n\n"
    for uid, b in banned_users.items():
        text += f"<code>{uid}</code> — {escape(b['reason'])} ({b['date']})\n"
    await message.reply(text, parse_mode="HTML")

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user):
        return
    today = time.strftime("%d.%m.%Y")
    if daily_stats["date"] != today:
        daily_stats["date"] = today
        daily_stats["sent"] = 0
        daily_stats["rejected"] = 0
    await message.reply(
        f"📊 <b>Статистика за сегодня</b>\n"
        f"✅ Опубликовано: {daily_stats['sent']}\n"
        f"❌ Отклонено: {daily_stats['rejected']}\n"
        f"⏳ В очереди: {post_queue.qsize()}",
        parse_mode="HTML"
    )

# ================= ПРИЁМ ПОСТОВ =================
async def process_post(message: Message, text: str, photo=None, video=None):
    uid = message.from_user.id

    if is_banned(uid):
        ban = banned_users[uid]
        return await message.answer(
            f"⛔ Вы забанены.\n📝 {escape(ban['reason'])}",
            parse_mode="HTML"
        )

    if not text and not photo and not video:
        return

    if text and has_bad_words(text):
        today = time.strftime("%d.%m.%Y")
        if daily_stats["date"] != today:
            daily_stats["date"] = today
            daily_stats["sent"] = 0
            daily_stats["rejected"] = 0
        daily_stats["rejected"] += 1
        return await message.answer(
            "❌ Сообщение отклонено из-за нецензурной лексики.\n"
            "Исправь и отправь снова."
        )

    status = await message.answer("🔍 Проверяю грамматику...")
    fixed = await fix_grammar(text) if text else ""

    author = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name

    try:
        header = f"📨 <b>Новая предложка</b>\n👤 {escape(author)} (<code>{uid}</code>)\n\n"
        if photo:
            await message.bot.send_photo(
                SUGGEST_GROUP_ID,
                photo=photo.file_id,
                caption=header + escape(fixed or "(без текста)"),
                parse_mode="HTML"
            )
        elif video:
            await message.bot.send_video(
                SUGGEST_GROUP_ID,
                video=video.file_id,
                caption=header + escape(fixed or "(без текста)"),
                parse_mode="HTML"
            )
        else:
            await message.bot.send_message(
                SUGGEST_GROUP_ID,
                header + escape(fixed),
                parse_mode="HTML"
            )
    except TelegramAPIError as e:
        logging.error(f"Send to group error: {e}")

    await post_queue.put({
        "user_id": uid,
        "text": fixed,
        "photo": photo.file_id if photo else None,
        "video": video.file_id if video else None,
    })

    pos = post_queue.qsize()
    wait_sec = pos * PUBLISH_INTERVAL
    hours = wait_sec // 3600
    minutes = (wait_sec % 3600) // 60
    seconds = wait_sec % 60

    parts = []
    if hours:
        parts.append(f"{hours} ч.")
    if minutes:
        parts.append(f"{minutes} мин.")
    if seconds or not parts:
        parts.append(f"{seconds} сек.")
    time_str = " ".join(parts)

    try:
        await status.edit_text(
            f"✅ <b>Пост принят!</b>\n"
            f"📌 Позиция в очереди: <b>{pos}</b>\n"
            f"⏳ Примерно через: <b>{time_str}</b>",
            parse_mode="HTML"
        )
    except TelegramAPIError:
        await message.answer(
            f"✅ Пост принят! Позиция: {pos}",
            parse_mode="HTML"
        )

# ================= ОБРАБОТЧИКИ =================
@router.message(F.text & ~F.text.startswith("/") & ~F.text.in_({"📊 Мой пост", "📨 Предложить новость"}))
async def handle_text(message: Message):
    await process_post(message, text=message.text)

@router.message(F.photo)
async def handle_photo(message: Message):
    await process_post(
        message,
        text=message.caption or "",
       1]
    )

@router.message(F.video)
async def handle_video(message: Message):
    await process_post(
        message,
        text=message.caption or "",
        video=message.video
    )

# ================= ОЧЕРЕДЬ ПУБЛИКАЦИИ =================
async def publisher(bot: Bot):
    while True:
        await asyncio.sleep(PUBLISH_INTERVAL)
        if post_queue.empty():
            continue
        item = await post_queue.get()
        try:
            if item["photo"]:
                await bot.send_photo(
                    CHANNEL_ID,
                    photo=item["photo"],
                    caption=item["text"] or None,
                    disable_notification=False
                )
            elif item["video"]:
                await bot.send_video(
                    CHANNEL_ID,
                    video=item["video"],
                    caption=item["text"] or None,
                    disable_notification=False
                )
            else:
                await bot.send_message(
                    CHANNEL_ID,
                    text=item["text"],
                    disable_web_page_preview=True
                )
            today = time.strftime("%d.%m.%Y")
            if daily_stats["date"] != today:
                daily_stats["date"] = today
                daily_stats["sent"] = 0
                daily_stats["rejected"] = 0
            daily_stats["sent"] += 1
            logging.info(f"Опубликован пост от {item['user_id']}")
        except TelegramAPIError as e:
            logging.error(f"Publish error: {e}")

# ================= ЗАПУСК =================
async def on_startup(bot: Bot):
    await bot.set_webhook(
        WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"]
    )
    logging.info(f"Вебхук: {WEBHOOK_URL}")

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
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    asyncio.create_task(publisher(bot))

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info(f"Сервер запущен на порту {PORT}")

    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Остановлен.")

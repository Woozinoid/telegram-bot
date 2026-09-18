import os
import re
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
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery
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

POST_COOLDOWN = 10 * 60  # 10 минут

# ================= ХРАНИЛИЩА =================
banned_users = {}
daily_stats = {"date": None, "sent": 0, "rejected": 0}
user_last_post = {}
pending_posts = {}
processed_messages = {}
DEDUP_WINDOW = 60


def cleanup_dedup():
    now = time.time()
    to_del = [k for k, v in processed_messages.items() if now - v > DEDUP_WINDOW]
    for k in to_del:
        del processed_messages[k]


def is_duplicate(message_id):
    cleanup_dedup()
    if message_id in processed_messages:
        return True
    processed_messages[message_id] = time.time()
    return False


def format_cooldown_left(seconds):
    m = seconds // 60
    s = seconds % 60
    parts = []
    if m:
        parts.append(f"{m} мин.")
    if s or not parts:
        parts.append(f"{s} сек.")
    return " ".join(parts)


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
    r"у(е|ё)бок|у(е|ё)бище|мразь|тварь|сволочь|гнида|падла|"
    r"шлюха|проститутка|гомик|лезбиянка|трахать|трах|отсос|"
    r"минет|ахуеть|ахуенно|охуеть|нихуя|нихера|похер|пофиг)\b",
    re.IGNORECASE
)


def has_bad_words(text):
    return bool(BAD_WORDS_PATTERN.search(text))


# ================= ГРАММАТИКА =================
async def fix_grammar(text):
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
def is_admin(user):
    if not user or not user.username:
        return False
    return user.username.lower() in [u.lower() for u in ADMIN_USERNAMES]


def is_banned(uid):
    return uid in banned_users


# ================= КЛАВИАТУРЫ =================
def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📨 Предложить новость")]
        ],
        resize_keyboard=True
    )


def moderation_kb(post_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"pub:{post_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej:{post_id}")
            ]
        ]
    )


# ================= РОУТЕР =================
router = Router()


@router.message(F.chat.type == "private", CommandStart())
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
        "и отправлю на модерацию.\n\n"
        "⚠️ Мат запрещён.\n"
        "⏳ Один пост раз в 10 минут.",
        parse_mode="HTML",
        reply_markup=main_kb()
    )


@router.message(F.chat.type == "private", F.text == "📨 Предложить новость")
async def suggest_hint(message: Message):
    await message.answer("✏️ Просто отправь мне текст (или фото/видео с подписью).")


# ================= АДМИН-КОМАНДЫ =================
@router.message(F.chat.type == "private", Command("ban"))
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
    banned_users[uid] = {
        "reason": reason,
        "date": time.strftime("%d.%m.%Y %H:%M"),
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


@router.message(F.chat.type == "private", Command("unban"))
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


@router.message(F.chat.type == "private", Command("banlist"))
async def cmd_banlist(message: Message):
    if not is_admin(message.from_user):
        return
    if not banned_users:
        return await message.reply("📭 Банлист пуст.")
    text = "📋 <b>Забаненные:</b>\n\n"
    for uid, b in banned_users.items():
        text += f"<code>{uid}</code> — {escape(b['reason'])} ({b['date']})\n"
    await message.reply(text, parse_mode="HTML")


@router.message(F.chat.type == "private", Command("stats"))
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
        f"⏳ На модерации: {len(pending_posts)}",
        parse_mode="HTML"
    )


# ================= МОДЕРАЦИЯ =================
@router.callback_query(F.data.startswith("pub:"))
async def on_publish(call: CallbackQuery):
    post_id = call.data.split(":")[1]
    if not is_admin(call.from_user):
        return await call.answer("⛔ Нет доступа", show_alert=True)

    data = pending_posts.pop(post_id, None)
    if not data:
        return await call.answer("⚠️ Пост уже обработан", show_alert=True)

    try:
        if data["photo"]:
            await call.bot.send_photo(
                CHANNEL_ID, photo=data["photo"], caption=data["text"] or None
            )
        elif data["video"]:
            await call.bot.send_video(
                CHANNEL_ID, video=data["video"], caption=data["text"] or None
            )
        else:
            await call.bot.send_message(
                CHANNEL_ID, text=data["text"], disable_web_page_preview=True
            )

        today = time.strftime("%d.%m.%Y")
        if daily_stats["date"] != today:
            daily_stats["date"] = today
            daily_stats["sent"] = 0
            daily_stats["rejected"] = 0
        daily_stats["sent"] += 1

        try:
            await call.bot.send_message(
                data["user_id"], "✅ Ваш пост опубликован в канале!"
            )
        except TelegramAPIError:
            pass

        try:
            new_caption = (call.message.caption or "") + f"\n\n✅ Одобрено: {call.from_user.full_name}"
            await call.message.edit_caption(caption=new_caption, reply_markup=None)
        except TelegramAPIError:
            try:
                await call.message.edit_text(
                    (call.message.text or "") + f"\n\n✅ Одобрено: {call.from_user.full_name}",
                    reply_markup=None
                )
            except TelegramAPIError:
                pass

        await call.answer("✅ Опубликовано")
    except TelegramAPIError as e:
        logging.error(f"Publish error: {e}")
        await call.answer("❌ Ошибка публикации", show_alert=True)


@router.callback_query(F.data.startswith("rej:"))
async def on_reject(call: CallbackQuery):
    post_id = call.data.split(":")[1]
    if not is_admin(call.from_user):
        return await call.answer("⛔ Нет доступа", show_alert=True)

    data = pending_posts.pop(post_id, None)
    if not data:
        return await call.answer("⚠️ Пост уже обработан", show_alert=True)

    today = time.strftime("%d.%m.%Y")
    if daily_stats["date"] != today:
        daily_stats["date"] = today
        daily_stats["sent"] = 0
        daily_stats["rejected"] = 0
    daily_stats["rejected"] += 1

    try:
        await call.bot.send_message(
            data["user_id"], "❌ Ваш пост отклонён модератором."
        )
    except TelegramAPIError:
        pass

    try:
        new_caption = (call.message.caption or "") + f"\n\n❌ Отклонено: {call.from_user.full_name}"
        await call.message.edit_caption(caption=new_caption, reply_markup=None)
    except TelegramAPIError:
        try:
            await call.message.edit_text(
                (call.message.text or "") + f"\n\n❌ Отклонено: {call.from_user.full_name}",
                reply_markup=None
            )
        except TelegramAPIError:
            pass

    await call.answer("❌ Отклонено")


# ================= ПРИЁМ ПОСТОВ =================
async def process_post(message: Message, text, photo=None, video=None):
    if is_duplicate(message.message_id):
        return

    uid = message.from_user.id

    if is_banned(uid):
        ban = banned_users[uid]
        return await message.answer(
            f"⛔ Вы забанены.\n📝 {escape(ban['reason'])}",
            parse_mode="HTML"
        )

    if not text and not photo and not video:
        return

    now = time.time()
    last = user_last_post.get(uid, 0)
    if now - last < POST_COOLDOWN:
        left = int(POST_COOLDOWN - (now - last))
        return await message.answer(
            f"⏳ Подождите ещё <b>{format_cooldown_left(left)}</b> перед следующим постом.",
            parse_mode="HTML"
        )

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

    if message.from_user.username:
        author = "@" + message.from_user.username
    else:
        author = message.from_user.full_name

    post_id = f"{uid}_{int(now)}"

    header = "📨 <b>Новая предложка на модерации</b>\n"
    header += f"👤 {escape(author)} (<code>{uid}</code>)\n\n"
    body = header + escape(fixed or "(без текста)")

    try:
        if photo:
            sent = await message.bot.send_photo(
                SUGGEST_GROUP_ID,
                photo=photo.file_id,
                caption=body,
                parse_mode="HTML",
                reply_markup=moderation_kb(post_id)
            )
        elif video:
            sent = await message.bot.send_video(
                SUGGEST_GROUP_ID,
                video=video.file_id,
                caption=body,
                parse_mode="HTML",
                reply_markup=moderation_kb(post_id)
            )
        else:
            sent = await message.bot.send_message(
                SUGGEST_GROUP_ID,
                body,
                parse_mode="HTML",
                reply_markup=moderation_kb(post_id)
            )

        pending_posts[post_id] = {
            "user_id": uid,
            "text": fixed,
            "photo": photo.file_id if photo else None,
            "video": video.file_id if video else None,
            "admin_msg_id": sent.message_id,
        }
    except TelegramAPIError as e:
        logging.error(f"Send to group error: {e}")
        return await status.edit_text("❌ Не удалось отправить на модерацию.")

    user_last_post[uid] = now

    try:
        await status.edit_text(
            "✅ <b>Пост отправлен на модерацию.</b>\n"
            "Следующий пост можно будет отправить через 10 минут.",
            parse_mode="HTML"
        )
    except TelegramAPIError:
        await message.answer(
            "✅ Пост отправлен на модерацию. Следующий — через 10 минут."
        )


# ================= ОБРАБОТЧИКИ ТИПОВ =================
@router.message(F.chat.type == "private", F.photo)
async def handle_photo(message: Message):
    await process_post(message, text=message.caption or "", photo=message.photo[-1])


@router.message(F.chat.type == "private", F.video)
async def handle_video(message: Message):
    await process_post(message, text=message.caption or "", video=message.video)


@router.message(
    F.chat.type == "private",
    F.text & ~F.text.startswith("/") & ~F.text.in_({"📨 Предложить новость"})
)
async def handle_text(message: Message):
    await process_post(message, text=message.text)


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
    except SystemExit:
        logging.info("Остановлен.")

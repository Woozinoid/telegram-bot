import asyncio
import logging
import os
import re
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp
from aiohttp import web

# ================= НАСТРОЙКИ =================
TOKEN = "8641527466:AAGSkaTzMJm5X6ExY3vVYRiMLxkwSxOOpnU"
CHANNEL_ID = -1002313542500        # Канал для публикации
ADMIN_GROUP_ID = -1002688386266    # Группа для просмотра предложки

ADMIN_USERNAMES = ["Woozinoid", "HwangMinw"]

MOSCOW_TZ = timezone(timedelta(hours=3))
EKAT_TZ = timezone(timedelta(hours=5))   # Екатеринбург

PUBLISH_INTERVAL = 30  # секунд (для теста, потом замените на 150*60)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ================= ПАМЯТЬ =================
banned_users = {}
daily_stats = {"sent": 0, "rejected": 0, "date": None}
post_queue = asyncio.Queue()

BAD_WORDS = [
    r"\b(ху(й|и|е|я|ё)|пизд(а|ы|е|у|ой)|еба(ть|л|н)|бля(дь|ть|д)|сук(а|и|ой)|залуп(а|ы|е)|жоп(а|ы|е)|гандон|мудак|пидор|лох)\b"
]
BAD_WORDS_PATTERN = re.compile("|".join(BAD_WORDS), re.IGNORECASE)

def is_admin(user: types.User) -> bool:
    if user.username is None:
        return False
    return user.username.lower() in [u.lower() for u in ADMIN_USERNAMES]

def reset_daily_stats():
    ekat_now = datetime.now(EKAT_TZ).date()
    if daily_stats.get("date") != ekat_now:
        daily_stats["sent"] = 0
        daily_stats["rejected"] = 0
        daily_stats["date"] = ekat_now

# ================= ГЛАВНОЕ МЕНЮ =================
def main_keyboard():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📊 Мой пост")],
            [types.KeyboardButton(text="📨 Предложить новость")]
        ],
        resize_keyboard=True
    )

# ================= ПРОВЕРКА ГРАММАТИКИ (Яндекс.Спеллер) =================
async def check_grammar(text: str) -> str:
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
                start = error["pos"]
                end = error["pos"] + error["len"]
                text = text[:start] + replacement + text[end:]
        return text
    except Exception as e:
        logging.error(f"Yandex.Speller error: {e}")
        return text

# ================= УВЕДОМЛЕНИЕ АДМИНАМ =================
async def notify_admins(text: str, user: types.User = None, status: str = "📨 ПРЕДЛОЖКА"):
    if user:
        author = f"@{user.username}" if user.username else user.first_name
        user_link = f"@{user.username}" if user.username else f"tg://user?id={user.id}"
        header = f"{status}\n👤 <b>От:</b> <a href='{user_link}'>{author}</a>\n🆔 ID: <code>{user.id}</code>\n\n"
    else:
        header = f"{status}\n\n"
    full_text = f"{header}📝 <b>Текст:</b>\n{text}"
    try:
        await bot.send_message(chat_id=ADMIN_GROUP_ID, text=full_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Notify admins error: {e}")

# ================= ПРОВЕРКА МАТА =================
def contains_bad_words(text: str) -> bool:
    return bool(BAD_WORDS_PATTERN.search(text))

# ================= ФОНОВАЯ ПУБЛИКАЦИЯ =================
async def publisher():
    while True:
        await asyncio.sleep(PUBLISH_INTERVAL)
        if post_queue.empty():
            continue
        post_data = await post_queue.get()
        try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=post_data["text"],
                parse_mode="HTML"
            )
            reset_daily_stats()
            daily_stats["sent"] += 1
            logging.info(f"Published post from {post_data.get('user_id', 'unknown')}")
        except Exception as e:
            logging.error(f"Publish error: {e}")
            await notify_admins(f"❌ Ошибка публикации: {e}\nПост: {post_data['text']}")

# ================= АДМИНСКИЕ КОМАНДЫ =================
@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user):
        return await message.reply("⛔ Нет доступа")
    args = command.args
    if not args:
        return await message.reply("Использование: /ban <user_id> <причина>")
    parts = args.split(maxsplit=1)
    if not parts[0].isdigit():
        return await message.reply("Укажите числовой ID пользователя")
    user_id = int(parts[0])
    reason = parts[1] if len(parts) > 1 else "Без причины"
    banned_users[user_id] = {
        "reason": reason,
        "date": datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M"),
        "banned_by": message.from_user.username or message.from_user.first_name
    }
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"⛔ <b>Вы заблокированы</b>\n"
                f"📅 Дата: {banned_users[user_id]['date']} (МСК)\n"
                f"📝 Причина: {reason}\n"
                f"🔓 Для разбана обратитесь к @Woozinoid"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Could not notify banned user {user_id}: {e}")
    await message.reply(f"✅ Пользователь {user_id} забанен. Причина: {reason}")

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user):
        return await message.reply("⛔ Нет доступа")
    args = command.args
    if not args or not args.isdigit():
        return await message.reply("Использование: /unban <user_id>")
    user_id = int(args)
    if user_id in banned_users:
        del banned_users[user_id]
        await message.reply(f"✅ Пользователь {user_id} разбанен")
    else:
        await message.reply("❌ Пользователь не в бане")

@dp.message(Command("banlist"))
async def cmd_banlist(message: types.Message):
    if not is_admin(message.from_user):
        return await message.reply("⛔ Нет доступа")
    if not banned_users:
        return await message.reply("📋 Список забаненных пуст")
    text = "📋 <b>Забаненные пользователи:</b>\n\n"
    for uid, data in banned_users.items():
        text += (
            f"🆔 <code>{uid}</code>\n"
            f"📅 {data['date']}\n"
            f"📝 {data['reason']}\n"
            f"👤 Кто забанил: {data['banned_by']}\n\n"
        )
    await message.reply(text, parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not is_admin(message.from_user):
        return await message.reply("⛔ Нет доступа")
    reset_daily_stats()
    sent = daily_stats["sent"]
    rejected = daily_stats["rejected"]
    in_queue = post_queue.qsize()
    await message.reply(
        f"📊 <b>Статистика за сегодня</b>\n"
        f"✅ Опубликовано: {sent}\n"
        f"❌ Отклонено: {rejected}\n"
        f"⏳ В очереди: {in_queue}",
        parse_mode="HTML"
    )

# ================= ОБРАБОТКА СООБЩЕНИЙ =================
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        "📨 <b>Добро пожаловать в предложку «Ищу тебя Екатеринбург»!</b>\n\n"
        "Здесь вы можете отправить свою анкету или объявление, "
        "которое после проверки грамматики будет опубликовано в канале.\n\n"
        "👨‍💼 Администратор канала: @roman3801\n"
        "🤖 Создатель бота: @Woozinoid\n\n"
        "⚠️ Посты выходят каждые 2.5 часа (сейчас 30 сек для теста).\n"
        "🚫 Мат запрещён!",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

@dp.message(F.text == "📊 Мой пост")
async def my_post_status(message: types.Message):
    uid = message.from_user.id
    found_position = None
    for idx, item in enumerate(post_queue._queue):
        if item.get("user_id") == uid:
            found_position = idx + 1
            break
    if found_position is None:
        await message.answer("❌ У вас нет постов в очереди.")
        return

    remaining_seconds = found_position * PUBLISH_INTERVAL
    days = remaining_seconds // 86400
    hours = (remaining_seconds % 86400) // 3600
    minutes = (remaining_seconds % 3600) // 60
    seconds = remaining_seconds % 60

    publish_time = datetime.now(EKAT_TZ) + timedelta(seconds=remaining_seconds)
    time_str = publish_time.strftime("%d.%m.%Y %H:%M")

    parts = []
    if days: parts.append(f"{days} дн")
    if hours: parts.append(f"{hours} ч")
    if minutes: parts.append(f"{minutes} мин")
    if seconds or not parts: parts.append(f"{seconds} сек")
    duration_str = " ".join(parts)

    await message.answer(
        f"📊 Ваш пост находится на позиции <b>{found_position}</b>\n"
        f"⏳ До публикации осталось: {duration_str}\n"
        f"🕒 Будет опубликован (Екб): {time_str}",
        parse_mode="HTML"
    )

@dp.message(F.text == "📨 Предложить новость")
async def suggest_prompt(message: types.Message):
    await message.answer(
        "✏️ Просто отправьте текст (или фото/видео с подписью) — "
        "я проверю грамматику и поставлю в очередь на публикацию."
    )

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: types.Message):
    # Игнорируем сообщения от каналов (sender_chat) и из каналов
    if message.sender_chat or message.chat.type == "channel":
        return
    user = message.from_user
    if not user:  # если сообщение от анонимного канала
        return
    uid = user.id
    if uid == 777000:  # Telegram
        return

    if uid in banned_users:
        ban_data = banned_users[uid]
        await message.answer(
            f"⛔ <b>Вы заблокированы</b>\n"
            f"📅 Дата: {ban_data['date']}\n"
            f"📝 Причина: {ban_data['reason']}\n"
            f"🔓 Обратитесь к @Woozinoid",
            parse_mode="HTML"
        )
        return

    original_text = message.text
    if original_text in ["📊 Мой пост", "📨 Предложить новость"]:
        return

    await notify_admins(original_text, user, "📨 ПРЕДЛОЖКА")

    if contains_bad_words(original_text):
        reset_daily_stats()
        daily_stats["rejected"] += 1
        await message.answer(
            "❌ <b>Сообщение отклонено</b> из-за нецензурной лексики.\n"
            "Пожалуйста, исправьте текст и отправьте снова.",
            parse_mode="HTML"
        )
        await notify_admins(original_text, user, "❌ ОТКЛОНЕНО (мат)")
        return

    status_msg = await message.answer("🔍 Проверяю грамматику...")
    corrected_text = await check_grammar(original_text)
    if corrected_text != original_text:
        await notify_admins(corrected_text, user, "✅ ИСПРАВЛЕНО")

    author_name = f"@{user.username}" if user.username else user.first_name
    post_text = f"{corrected_text}\n\n✍️ <i>Предложка: {author_name}</i>"

    await post_queue.put({"text": post_text, "user_id": uid})
    queue_len = post_queue.qsize()
    approx_time = datetime.now(EKAT_TZ) + timedelta(seconds=PUBLISH_INTERVAL * queue_len)
    time_str = approx_time.strftime("%H:%M")
    await status_msg.edit_text(
        f"✅ <b>Пост принят!</b>\n"
        f"⏳ Будет опубликован примерно в {time_str} (Екб)\n"
        f"📌 Позиция в очереди: {queue_len}",
        parse_mode="HTML"
    )

# ================= ВЕБ-СЕРВЕР =================
async def home(request):
    return web.Response(text="Bot is running")

async def main():
    asyncio.create_task(publisher())
    app = web.Application()
    app.router.add_get("/", home)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server started on port {port}")
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())

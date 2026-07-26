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

ADMIN_USERNAMES = ["Woozinoid", "HwangMinw"]  # Добавьте нужные юзернеймы

MOSCOW_TZ = timezone(timedelta(hours=3))

PUBLISH_INTERVAL = 150 * 60  # 2.5 часа в секундах

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ================= ПАМЯТЬ =================
# Баны: {user_id: {"reason": str, "date": str, "banned_by": str}}
banned_users = {}
# Статистика за сегодня
daily_stats = {"sent": 0, "rejected": 0, "date": None}
# Очередь постов
post_queue = asyncio.Queue()
# Матерный фильтр (простой список, можно расширить)
BAD_WORDS = [
    r"\b(ху(й|и|е|я|ё)|пизд(а|ы|е|у|ой)|еба(ть|л|н)|бля(дь|ть|д)|сук(а|и|ой)|залуп(а|ы|е)|жоп(а|ы|е)|гандон|мудак|пидор|лох)\b"
]
BAD_WORDS_PATTERN = re.compile("|".join(BAD_WORDS), re.IGNORECASE)

# ================= УТИЛИТЫ =================
def is_admin(user: types.User) -> bool:
    if user.username is None:
        return False
    return user.username.lower() in [u.lower() for u in ADMIN_USERNAMES]

def reset_daily_stats():
    moscow_now = datetime.now(MOSCOW_TZ).date()
    if daily_stats.get("date") != moscow_now:
        daily_stats["sent"] = 0
        daily_stats["rejected"] = 0
        daily_stats["date"] = moscow_now

# ================= ПРОВЕРКА ГРАММАТИКИ =================
async def check_grammar(text: str) -> str:
    url = "https://api.languagetool.org/v2/check"
    data = {"text": text, "language": "ru", "enabledOnly": "false"}
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=data) as resp:
                result = await resp.json()
        if not result.get("matches"):
            return text
        corrected = text
        for match in sorted(result["matches"], key=lambda x: x["offset"], reverse=True):
            if match.get("replacements"):
                replacement = match["replacements"][0]["value"]
                start = match["offset"]
                end = start + match["length"]
                corrected = corrected[:start] + replacement + corrected[end:]
        return corrected
    except Exception as e:
        logging.error(f"LanguageTool error: {e}")
        return text

# ================= ПЕРЕСЫЛКА В ГРУППУ АДМИНА =================
async def notify_admins(text: str, user: types.User = None, status: str = "📨 ПРЕДЛОЖКА"):
    """Отправка уведомления в группу администраторов"""
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

# ================= ФИЛЬТРАЦИЯ МАТА =================
def contains_bad_words(text: str) -> bool:
    return bool(BAD_WORDS_PATTERN.search(text))

# ================= ПУБЛИКАЦИЯ ИЗ ОЧЕРЕДИ =================
async def publisher():
    """Фоновая задача: каждые PUBLISH_INTERVAL секунд публикует один пост из очереди"""
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
            # Можно вернуть в очередь или уведомить админа
            await notify_admins(f"❌ Ошибка публикации: {e}\nПост: {post_data['text']}")

# ================= ОБРАБОТЧИКИ КОМАНД (АДМИНКА) =================
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
    # Уведомляем пользователя, если бот может ему написать
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

# ================= ОБРАБОТЧИКИ СООБЩЕНИЙ =================
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        "📨 <b>Предложка новостей</b>\n\n"
        "Отправьте мне текст, фото или видео — "
        "я проверю грамматику и опубликую в канале.\n\n"
        "⚠️ Посты выходят каждые 2.5 часа.\n"
        "🚫 Мат запрещён!",
        parse_mode="HTML"
    )

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: types.Message):
    user = message.from_user
    uid = user.id
    # Проверка бана
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
    # Уведомление админам (оригинал)
    await notify_admins(original_text, user, "📨 ПРЕДЛОЖКА")

    # Проверка мата
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

    # Проверка грамматики
    status_msg = await message.answer("🔍 Проверяю грамматику...")
    corrected_text = await check_grammar(original_text)
    if corrected_text != original_text:
        await notify_admins(corrected_text, user, "✅ ИСПРАВЛЕНО")

    # Формируем пост с подписью автора
    author_name = f"@{user.username}" if user.username else user.first_name
    post_text = f"{corrected_text}\n\n✍️ <i>Предложка: {author_name}</i>"

    # Добавляем в очередь
    await post_queue.put({"text": post_text, "user_id": uid})
    queue_len = post_queue.qsize()
    approx_time = datetime.now(MOSCOW_TZ) + timedelta(seconds=PUBLISH_INTERVAL * queue_len)
    time_str = approx_time.strftime("%H:%M")
    await status_msg.edit_text(
        f"✅ <b>Пост принят!</b>\n"
        f"⏳ Будет опубликован примерно в {time_str} (МСК)\n"
        f"📌 Позиция в очереди: {queue_len}",
        parse_mode="HTML"
    )

# Аналогично для фото и видео (можно добавить, но оставим только текст для примера)

# ================= ВЕБ-СЕРВЕР =================
async def home(request):
    return web.Response(text="Bot is running")

async def main():
    # Запускаем фонового издателя
    asyncio.create_task(publisher())
    # Веб-сервер
    app = web.Application()
    app.router.add_get("/", home)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server started on port {port}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

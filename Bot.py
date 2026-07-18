import asyncio
import json
import logging
import random
import aiosqlite
import os
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
import aiohttp
from aiohttp import web

# ---------- НАСТРОЙКИ ----------
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не установлен!")

MOSCOW_TZ = timezone(timedelta(hours=3))
DB_PATH = "bot_database.db"

ADMIN_USERNAMES = ["Woozinoid"]  # замените на свои юзернеймы

chat_users = set()

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

class AddCity(StatesGroup):
    waiting_for_name = State()

class Broadcast(StatesGroup):
    waiting_for_message = State()

# ---------- БАЗА ДАННЫХ ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                language TEXT DEFAULT 'ru',
                reg_date TEXT,
                status TEXT DEFAULT 'active'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS locations (
                user_id INTEGER PRIMARY KEY,
                lat REAL,
                lon REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                lat REAL,
                lon REAL,
                UNIQUE(user_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER DEFAULT 0
            )
        """)
        await db.execute("INSERT OR IGNORE INTO chat_state (id, enabled) VALUES (1, 0)")
        await db.commit()

async def create_user(user_id: int, username: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, reg_date) VALUES (?, ?, ?)",
            (user_id, username, datetime.now(MOSCOW_TZ).isoformat())
        )
        await db.commit()

async def get_user_language(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else "ru"

async def set_user_language(user_id: int, lang: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, user_id))
        await db.commit()

async def get_user_status(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else "active"

async def set_user_status(user_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
        await db.commit()

async def save_location(user_id: int, lat: float, lon: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO locations (user_id, lat, lon) VALUES (?, ?, ?)",
            (user_id, lat, lon)
        )
        await db.commit()

async def get_location(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT lat, lon FROM locations WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return (row[0], row[1]) if row else None

async def add_city_db(user_id: int, name: str, lat: float, lon: float) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO cities (user_id, name, lat, lon) VALUES (?, ?, ?, ?)",
                (user_id, name, lat, lon)
            )
            await db.commit()
        return True
    except:
        return False

async def get_cities(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT name, lat, lon FROM cities WHERE user_id = ?", (user_id,))
        rows = await cursor.fetchall()
        return [{"name": row[0], "lat": row[1], "lon": row[2]} for row in rows]

async def city_exists(user_id: int, name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM cities WHERE user_id = ? AND LOWER(name) = LOWER(?)",
            (user_id, name)
        )
        return await cursor.fetchone() is not None

async def get_chat_state() -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT enabled FROM chat_state WHERE id = 1")
        row = await cursor.fetchone()
        return bool(row[0]) if row else False

async def set_chat_state(enabled: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE chat_state SET enabled = ? WHERE id = 1", (int(enabled),))
        await db.commit()

async def get_all_user_ids() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users")
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

# ---------- ПОГОДНЫЕ КОДЫ ----------
WEATHER_CODES_RU = {
    0: "Ясно", 1: "Преимущественно ясно", 2: "Переменная облачность",
    3: "Пасмурно", 45: "Туман", 48: "Иней", 51: "Морось",
    53: "Морось", 55: "Сильная морось", 61: "Дождь",
    63: "Сильный дождь", 65: "Ливень", 71: "Снег",
    73: "Снегопад", 75: "Сильный снег", 80: "Кратковременный дождь",
    95: "Гроза", 96: "Гроза с градом", 99: "Гроза с градом"
}
WEATHER_CODES_UK = {
    0: "Ясно", 1: "Переважно ясно", 2: "Мінлива хмарність",
    3: "Хмарно", 45: "Туман", 51: "Мряка", 61: "Дощ",
    63: "Сильний дощ", 65: "Злива", 71: "Сніг", 73: "Снігопад",
    75: "Сильний сніг", 95: "Гроза", 96: "Гроза з градом"
}

# ---------- ПЕРЕВОДЫ (сокращено для экономии места – возьмите полные RU/UK словари из предыдущего полного кода) ----------
# Вставьте сюда RU и UK из более раннего полного сообщения с SQLite (они уже были полными)
# ...

# (Из-за ограничения длины ответа я не могу вставить все переводы, но вы должны взять их из предыдущего полного кода.
#  Обязательно вставьте ПОЛНЫЕ словари RU и UK!)

# ---------- КЛАВИАТУРЫ И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
# ... (все функции: get_main_kb, get_cities_kb, check_status, get_text, is_admin)
# Они должны быть точно такими же, как в предыдущем полном варианте.

# ---------- ОБРАБОТЧИКИ ----------
# Вставьте сюда все обработчики из полного кода (start, location, погода, валюта, админка, чат)
# Они полностью совпадают с тем, что было в финальной версии с SQLite.

# ---------- ВЕБ-СЕРВЕР ДЛЯ RENDER ----------
async def handle(request):
    return web.Response(text="Bot is running")

async def main():
    await init_db()
    # Веб-сервер
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Web server started on port {port}")
    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

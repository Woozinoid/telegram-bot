import asyncio
import json
import logging
import random
import libsql_experimental as libsql
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

# ---------- НАСТРОЙКИ (вшитые токены) ----------
TOKEN = "8641527466:AAGSkaTzMJm5X6ExY3vVYRiMLxkwSxOOpnU"
TURSO_URL = "libsql://db-bot-woozinoid.aws-us-east-2.turso.io"
TURSO_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODQ0MTU2MDUsImlkIjoiMDE5Zjc3NzQtOTcwMS03OTM1LThiMDAtYzI1Mzk3MGEzYTY4Iiwia2lkIjoiOXdHT2NyTlpPLV9xRk80QkdwMFR1V0lfOWI0Q3FjUUJRRG9JM0V6dEFXUSIsInJpZCI6ImQ3NzhkMWMzLTExNDMtNGNmZC04MTJlLWEyMzBjOTVhNTJhZCJ9.R27WAXi6BvDIO6wYPZzmG2OYsjfiDKM1i1Nz2Zr4giYqsphnLPr8XsO_eaEHHij507-Mz55Q6GJ8V-H2g3feDw"

MOSCOW_TZ = timezone(timedelta(hours=3))
ADMIN_USERNAMES = ["Woozinoid", "HwangMinw"]
chat_users = set()

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

class AddCity(StatesGroup):
    waiting_for_name = State()

class Broadcast(StatesGroup):
    waiting_for_message = State()

class SetNickname(StatesGroup):
    waiting_for_nick = State()

# ---------- БАЗА ДАННЫХ (Turso) ----------
def get_db():
    return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)

async def init_db():
    def _():
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                reg_date TEXT,
                status TEXT DEFAULT 'active'
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS locations (
                user_id INTEGER PRIMARY KEY,
                lat REAL,
                lon REAL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS cities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                lat REAL,
                lon REAL,
                UNIQUE(user_id, name)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS chat_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER DEFAULT 0
            )
        """)
        db.execute("INSERT OR IGNORE INTO chat_state (id, enabled) VALUES (1, 0)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                nickname TEXT
            )
        """)
        db.commit()
    await asyncio.get_event_loop().run_in_executor(None, _)

async def create_user(user_id: int, username: str = None):
    def _():
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, reg_date) VALUES (?, ?, ?)",
            (user_id, username, datetime.now(MOSCOW_TZ).isoformat())
        )
        db.commit()
    await asyncio.get_event_loop().run_in_executor(None, _)

async def get_user_status(user_id: int) -> str:
    def _():
        db = get_db()
        r = db.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return r[0] if r else "active"
    return await asyncio.get_event_loop().run_in_executor(None, _)

async def set_user_status(user_id: int, status: str):
    def _():
        db = get_db()
        db.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
        db.commit()
    await asyncio.get_event_loop().run_in_executor(None, _)

async def save_location(user_id: int, lat: float, lon: float):
    def _():
        db = get_db()
        db.execute(
            "INSERT OR REPLACE INTO locations (user_id, lat, lon) VALUES (?, ?, ?)",
            (user_id, lat, lon)
        )
        db.commit()
    await asyncio.get_event_loop().run_in_executor(None, _)

async def get_location(user_id: int):
    def _():
        db = get_db()
        r = db.execute("SELECT lat, lon FROM locations WHERE user_id = ?", (user_id,)).fetchone()
        return (r[0], r[1]) if r else None
    return await asyncio.get_event_loop().run_in_executor(None, _)

async def add_city_db(user_id: int, name: str, lat: float, lon: float) -> bool:
    def _():
        try:
            db = get_db()
            db.execute(
                "INSERT INTO cities (user_id, name, lat, lon) VALUES (?, ?, ?, ?)",
                (user_id, name, lat, lon)
            )
            db.commit()
            return True
        except:
            return False
    return await asyncio.get_event_loop().run_in_executor(None, _)

async def get_cities(user_id: int) -> list:
    def _():
        db = get_db()
        rows = db.execute("SELECT name, lat, lon FROM cities WHERE user_id = ?", (user_id,)).fetchall()
        return [{"name": r[0], "lat": r[1], "lon": r[2]} for r in rows]
    return await asyncio.get_event_loop().run_in_executor(None, _)

async def city_exists(user_id: int, name: str) -> bool:
    def _():
        db = get_db()
        r = db.execute(
            "SELECT 1 FROM cities WHERE user_id = ? AND LOWER(name) = LOWER(?)",
            (user_id, name)
        ).fetchone()
        return r is not None
    return await asyncio.get_event_loop().run_in_executor(None, _)

async def get_chat_state() -> bool:
    def _():
        db = get_db()
        r = db.execute("SELECT enabled FROM chat_state WHERE id = 1").fetchone()
        return bool(r[0]) if r else False
    return await asyncio.get_event_loop().run_in_executor(None, _)

async def set_chat_state(enabled: bool):
    def _():
        db = get_db()
        db.execute("UPDATE chat_state SET enabled = ? WHERE id = 1", (int(enabled),))
        db.commit()
    await asyncio.get_event_loop().run_in_executor(None, _)

async def get_all_user_ids() -> list:
    def _():
        db = get_db()
        rows = db.execute("SELECT user_id FROM users").fetchall()
        return [r[0] for r in rows]
    return await asyncio.get_event_loop().run_in_executor(None, _)

async def set_nickname(user_id: int, nick: str):
    def _():
        db = get_db()
        db.execute(
            "INSERT OR REPLACE INTO profiles (user_id, nickname) VALUES (?, ?)",
            (user_id, nick)
        )
        db.commit()
    await asyncio.get_event_loop().run_in_executor(None, _)

async def get_nickname(user_id: int) -> str | None:
    def _():
        db = get_db()
        r = db.execute("SELECT nickname FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        return r[0] if r else None
    return await asyncio.get_event_loop().run_in_executor(None, _)

# ---------- ПОГОДНЫЕ КОДЫ (только русские) ----------
WEATHER_CODES = {
    0: "Ясно", 1: "Преимущественно ясно", 2: "Переменная облачность",
    3: "Пасмурно", 45: "Туман", 48: "Иней", 51: "Морось",
    53: "Морось", 55: "Сильная морось", 61: "Дождь",
    63: "Сильный дождь", 65: "Ливень", 71: "Снег",
    73: "Снегопад", 75: "Сильный снег", 80: "Кратковременный дождь",
    95: "Гроза", 96: "Гроза с градом", 99: "Гроза с градом"
}

# ---------- РУССКИЕ ТЕКСТЫ ----------
TEXT = {
    "welcome_back": "🌈 <b>С возвращением!</b> Выберите действие:",
    "welcome_new": "🌈 <b>Привет!</b> Отправьте геолокацию, чтобы открыть все функции.",
    "location_saved": "✅ Геолокация сохранена!",
    "weather_menu": "🌟 Выберите город или добавьте новый:",
    "add_city_prompt": "🏙 Введите название города:",
    "cancel": "↩ Назад",
    "searching_coords": "⏳ Ищу координаты...",
    "city_not_found": "❌ Не удалось найти город «{city}».",
    "city_already_exists": "❗ Город «{city}» уже в списке.",
    "city_added": "✅ Город «{city}» добавлен!",
    "loading_weather": "⏳ Загружаю погоду для «{city}»...",
    "weather_error": "❌ Ошибка: {error}",
    "currency_menu": "💰 Выберите валютную пару:",
    "loading_currency": "⏳ Загружаю данные по {pair}...",
    "banned_msg": "⛔ Вы забанены. Обратитесь к администратору.",
    "muted_msg": "🔇 Вы временно не можете писать.",
    "chat_enter": "💬 Вы вошли в общий чат.",
    "chat_leave": "💬 Вы вышли из чата.",
    "chat_off": "💬 Чат временно отключён.",
    "chat_global_on": "✅ Общий чат включён.",
    "chat_global_off": "🛑 Общий чат выключен.",
    "whitelist_empty": "📋 Список заблокированных пуст.",
    "broadcast_sent": "✅ Сообщение отправлено {count} пользователям.",
    "admin_menu": "🔧 <b>Админ-панель</b>",
    "main_menu": [["🌟 Погода", "💰 Курсы валют"], ["📍 Обновить геолокацию"], ["👤 Профиль"]],
    "weather_frame": "🌍 {city}\n🌤 {desc}\n🌡 Температура: {temp}°C (ощ. {feels}°C)\n☁️ Облачность: {cloudcover}%\n💧 Влажность: {humidity}%\n🔵 Давление: {pressure} мм рт.ст.\n🌅 Восход: {sunrise}\n🌇 Закат: {sunset}",
    "now_in_city": "🌈 Сейчас в {city}: {desc}",
    "fiat_info": "📅 <b>{date}</b> 🕒 {time} (МСК)\n\n<b>{pair}</b>\n💰 Текущий курс: <b>{current:.2f} ₽</b>\n📉 За 24 часа: {arrow_24} {change_24:+.2f} ₽ ({change_24_pct:+.2f}%)\n{week_info}",
    "ton_info": "📅 <b>{date}</b> 🕒 {time} (МСК)\n\n<b>💎 TON/RUB</b>\n💰 Текущий курс: <b>{ton_rub:.2f} ₽</b> (${ton_usd:.4f})\n📉 За 24 часа: {arrow} {change_pct:+.2f}%",
    "profile_menu": "👤 <b>Профиль</b>\nВаш ник: {nick}\nВыберите действие:",
    "set_nick_prompt": "✏️ Введите новый никнейм (только буквы, цифры, без пробелов):",
    "nick_saved": "✅ Никнейм сохранён: {nick}",
    "nick_not_set": "не задан",
    "chat_joined": "💬 Пользователь {name} присоединился к чату."
}

MONTHS_RU = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def is_admin(user: types.User) -> bool:
    return user.username is not None and user.username.lower() in [u.lower() for u in ADMIN_USERNAMES]

def get_text(key, **kwargs):
    text = TEXT[key]
    if kwargs:
        text = text.format(**kwargs)
    return text

async def check_status(message: types.Message):
    user_id = message.from_user.id
    status = await get_user_status(user_id)
    if status == "banned":
        await message.answer(get_text("banned_msg"))
        return False
    elif status == "muted":
        await message.answer(get_text("muted_msg"))
        return False
    return True

async def get_display_name(user_id: int) -> str:
    nick = await get_nickname(user_id)
    if nick:
        return nick
    try:
        user = await bot.get_chat(user_id)
        if user.username:
            return f"@{user.username}"
        return user.first_name or f"id{user_id}"
    except:
        return f"id{user_id}"

# ---------- КЛАВИАТУРЫ ----------
def get_location_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)]],
        resize_keyboard=True
    )

async def get_main_kb(user: types.User = None):
    buttons = [
        [KeyboardButton(text="🌟 Погода"), KeyboardButton(text="💰 Курсы валют")],
        [KeyboardButton(text="📍 Обновить геолокацию", request_location=True)],
        [KeyboardButton(text="👤 Профиль")]
    ]
    chat_enabled = await get_chat_state()
    if chat_enabled:
        buttons.append([KeyboardButton(text="💬 Чат")])
    if user and is_admin(user):
        buttons.append([KeyboardButton(text="🔧 Админ")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

async def get_cities_kb(user_id):
    cities = await get_cities(user_id)
    buttons = []
    for city in cities:
        buttons.append([KeyboardButton(text=f"🏙 {city['name']}")])
    buttons.append([KeyboardButton(text="➕ Добавить город")])
    buttons.append([KeyboardButton(text="↩ Назад")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="↩ Назад")]], resize_keyboard=True)

def get_currency_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🇺🇸 USD/RUB"), KeyboardButton(text="🇪🇺 EUR/RUB")],
            [KeyboardButton(text="💎 TON/RUB")],
            [KeyboardButton(text="↩ Назад")]
        ],
        resize_keyboard=True
    )

def get_profile_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Изменить ник")],
            [KeyboardButton(text="↩ Назад")]
        ],
        resize_keyboard=True
    )

# ---------- API ФУНКЦИИ ----------
async def geocode_city(city_name: str) -> tuple:
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": city_name, "format": "json", "limit": 1, "accept-language": "ru"}
    headers = {"User-Agent": "MyTelegramBot/1.0"}
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                data = await resp.json()
                if data:
                    return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        logging.error(f"Geocode error: {e}")
    return None

async def get_weather_by_coords(lat: float, lon: float, display_name: str):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,cloud_cover,pressure_msl",
        "daily": "sunrise,sunset",
        "timezone": "auto",
        "forecast_days": 1
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            data = await resp.json()
    if "error" in data and data["error"]:
        raise Exception("Ошибка получения погоды")
    curr = data["current"]
    daily = data["daily"]
    tz_offset = data.get("utc_offset_seconds", 0)
    local_tz = timezone(timedelta(seconds=tz_offset))
    now_local = datetime.now(local_tz)

    desc = WEATHER_CODES.get(curr["weather_code"], "Неизвестно")
    month_str = MONTHS_RU[now_local.month]
    weekday_str = WEEKDAYS_RU[now_local.weekday()]
    local_time_str = f"{now_local.day} {month_str} {now_local.year}, {weekday_str} {now_local.strftime('%H:%M:%S')}"

    sunrise_utc = datetime.fromisoformat(daily["sunrise"][0]).replace(tzinfo=timezone.utc).astimezone(local_tz)
    sunset_utc = datetime.fromisoformat(daily["sunset"][0]).replace(tzinfo=timezone.utc).astimezone(local_tz)

    return {
        "city": display_name,
        "country": "",
        "temp": curr["temperature_2m"],
        "feels": curr["apparent_temperature"],
        "desc": desc,
        "humidity": curr["relative_humidity_2m"],
        "pressure": round(curr["pressure_msl"] * 0.75006, 1),
        "cloudcover": curr["cloud_cover"],
        "visibility": "—",
        "uv_index": "—",
        "sunrise": sunrise_utc.strftime("%H:%M"),
        "sunset": sunset_utc.strftime("%H:%M"),
        "local_time": local_time_str
    }

async def get_cbr_currency():
    url = "https://www.cbr-xml-daily.ru/daily_json.js"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return json.loads(await resp.text())

async def get_cbr_historical(date_str: str):
    url = f"https://www.cbr-xml-daily.ru/archive/{date_str}/daily_json.js"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                return json.loads(await resp.text())
            else:
                raise Exception("No data")

async def get_ton_price():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": "the-open-network", "vs_currencies": "usd", "include_24hr_change": "true"}
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    ton = data.get("the-open-network", {})
                    if ton.get("usd"):
                        return {"usd": ton["usd"], "change_24h_pct": ton.get("usd_24h_change", 0)}
    except: pass
    try:
        url = "https://api.coincap.io/v2/assets/the-open-network"
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    asset = data.get("data", {})
                    if asset.get("priceUsd"):
                        return {"usd": float(asset["priceUsd"]), "change_24h_pct": float(asset.get("changePercent24Hr", 0))}
    except: pass
    return None

# ---------- ОБРАБОТЧИКИ ----------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    await create_user(user_id, message.from_user.username)
    loc = await get_location(user_id)
    if loc:
        await message.answer(get_text("welcome_back"), reply_markup=await get_main_kb(message.from_user), parse_mode="HTML")
    else:
        await message.answer(get_text("welcome_new"), reply_markup=get_location_kb(), parse_mode="HTML")

@dp.message(F.location)
async def location_received(message: types.Message, state: FSMContext):
    if not await check_status(message): return
    await state.clear()
    user_id = message.from_user.id
    await create_user(user_id, message.from_user.username)
    await save_location(user_id, message.location.latitude, message.location.longitude)
    await message.answer(get_text("location_saved"), reply_markup=await get_main_kb(message.from_user))

# Профиль
@dp.message(lambda msg: msg.text == "👤 Профиль")
async def profile_menu(message: types.Message, state: FSMContext):
    if not await check_status(message): return
    await state.clear()
    user_id = message.from_user.id
    nick = await get_nickname(user_id) or get_text("nick_not_set")
    text = get_text("profile_menu", nick=nick)
    await message.answer(text, reply_markup=get_profile_kb(), parse_mode="HTML")

@dp.message(lambda msg: msg.text == "✏️ Изменить ник")
async def set_nick_start(message: types.Message, state: FSMContext):
    if not await check_status(message): return
    await state.set_state(SetNickname.waiting_for_nick)
    await message.answer(get_text("set_nick_prompt"), reply_markup=get_cancel_kb())

@dp.message(StateFilter(SetNickname.waiting_for_nick), F.text)
async def set_nick_finish(message: types.Message, state: FSMContext):
    if not await check_status(message): return
    nick = message.text.strip()
    if not nick or not nick.isalnum() or len(nick) > 20:
        await message.answer("❌ Некорректный ник. Используйте буквы/цифры, до 20 символов.")
        return
    user_id = message.from_user.id
    await set_nickname(user_id, nick)
    await state.clear()
    await message.answer(get_text("nick_saved", nick=nick), reply_markup=get_profile_kb())

# --- Погода ---
@dp.message(lambda msg: msg.text == "🌟 Погода")
async def weather_menu(message: types.Message, state: FSMContext):
    if not await check_status(message): return
    await state.clear()
    user_id = message.from_user.id
    await message.answer(get_text("weather_menu"), reply_markup=await get_cities_kb(user_id))

@dp.message(lambda msg: msg.text == "➕ Добавить город")
async def add_city_start(message: types.Message, state: FSMContext):
    if not await check_status(message): return
    await state.set_state(AddCity.waiting_for_name)
    await message.answer(get_text("add_city_prompt"), reply_markup=get_cancel_kb())

@dp.message(StateFilter(AddCity.waiting_for_name), lambda msg: msg.text == "↩ Назад")
async def add_city_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await weather_menu(message, state)

@dp.message(AddCity.waiting_for_name, F.text)
async def add_city_name(message: types.Message, state: FSMContext):
    if not await check_status(message): return
    city_name = message.text.strip()
    user_id = message.from_user.id
    if not city_name:
        await message.answer("Введите название")
        return
    msg = await message.answer(get_text("searching_coords"))
    coords = await geocode_city(city_name)
    if not coords:
        await msg.edit_text(get_text("city_not_found", city=city_name))
        return
    lat, lon = coords
    if await city_exists(user_id, city_name):
        await msg.edit_text(get_text("city_already_exists", city=city_name))
        await state.clear()
        await weather_menu(message, state)
        return
    await add_city_db(user_id, city_name, lat, lon)
    await state.clear()
    await msg.edit_text(get_text("city_added", city=city_name))
    await weather_menu(message, state)

@dp.message(lambda msg: msg.text and msg.text.startswith("🏙 "))
async def show_city_weather(message: types.Message, state: FSMContext):
    if not await check_status(message): return
    await state.clear()
    city_name = message.text[2:].strip()
    user_id = message.from_user.id
    cities = await get_cities(user_id)
    city = next((c for c in cities if c['name'] == city_name), None)
    if not city:
        await message.answer("Город не найден")
        return
    msg = await message.answer(get_text("loading_weather", city=city_name))
    try:
        weather = await get_weather_by_coords(city["lat"], city["lon"], city_name)
    except Exception as e:
        await msg.edit_text(get_text("weather_error", error=str(e)))
        return

    frame = TEXT["weather_frame"].format(**weather)
    now_text = TEXT["now_in_city"].format(city=weather['city'], desc=weather['desc'])
    text = f"📅 <b>{weather['local_time']}</b>\n\n{frame}\n\n<i>{now_text}</i>"
    await msg.edit_text(text, parse_mode="HTML")
    await weather_menu(message, state)

# --- Курсы валют ---
@dp.message(lambda msg: msg.text == "💰 Курсы валют")
async def currency_menu(message: types.Message, state: FSMContext):
    if not await check_status(message): return
    await state.clear()
    await message.answer(get_text("currency_menu"), reply_markup=get_currency_kb())

@dp.message(lambda msg: msg.text in ["🇺🇸 USD/RUB", "🇪🇺 EUR/RUB"])
async def show_fiat_currency(message: types.Message, state: FSMContext):
    if not await check_status(message): return
    pair = message.text.split()[1]
    msg = await message.answer(get_text("loading_currency", pair=pair))
    try:
        cbr_data = await get_cbr_currency()
        valutes = cbr_data["Valute"]
        if pair == "USD/RUB":
            current = valutes["USD"]["Value"]
            prev_day = valutes["USD"]["Previous"]
            valute_key = "USD"
        else:
            current = valutes["EUR"]["Value"]
            prev_day = valutes["EUR"]["Previous"]
            valute_key = "EUR"
        change_24 = current - prev_day
        change_24_pct = (change_24 / prev_day) * 100
        arrow_24 = "🔺" if change_24 > 0 else "🔻" if change_24 < 0 else "▪️"
        week_ago = (datetime.now(MOSCOW_TZ) - timedelta(days=7)).strftime("%Y/%m/%d")
        week_info = ""
        try:
            hist_data = await get_cbr_historical(week_ago)
            week_val = hist_data["Valute"][valute_key]["Value"]
            change_week = current - week_val
            change_week_pct = (change_week / week_val) * 100
            arrow_week = "🔺" if change_week > 0 else "🔻" if change_week < 0 else "▪️"
            week_info = f"📆 За неделю: {arrow_week} {change_week:+.2f} ₽ ({change_week_pct:+.2f}%)"
        except:
            week_info = "📆 За неделю: нет данных"
        now_moscow = datetime.now(MOSCOW_TZ)
        time_str = now_moscow.strftime("%H:%M:%S")
        date_str = f"{now_moscow.day} {MONTHS_RU[now_moscow.month]} {now_moscow.year}, {WEEKDAYS_RU[now_moscow.weekday()]}"
        info = get_text("fiat_info", date=date_str, time=time_str, pair=pair, current=current, arrow_24=arrow_24, change_24=change_24, change_24_pct=change_24_pct, week_info=week_info)
        await msg.edit_text(info, parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

@dp.message(lambda msg: msg.text == "💎 TON/RUB")
async def show_ton(message: types.Message, state: FSMContext):
    if not await check_status(message): return
    msg = await message.answer("⏳ Загружаю данные TON...")
    try:
        ton = await get_ton_price()
        if not ton:
            await msg.edit_text("❌ Не удалось загрузить курс TON")
            return
        cbr_data = await get_cbr_currency()
        usd_rub = cbr_data["Valute"]["USD"]["Value"]
        ton_rub = ton["usd"] * usd_rub
        change_pct = ton["change_24h_pct"]
        arrow = "🔺" if change_pct > 0 else "🔻" if change_pct < 0 else "▪️"
        now_moscow = datetime.now(MOSCOW_TZ)
        time_str = now_moscow.strftime("%H:%M:%S")
        date_str = f"{now_moscow.day} {MONTHS_RU[now_moscow.month]} {now_moscow.year}, {WEEKDAYS_RU[now_moscow.weekday()]}"
        info = get_text("ton_info", date=date_str, time=time_str, ton_rub=ton_rub, ton_usd=ton["usd"], arrow=arrow, change_pct=change_pct)
        await msg.edit_text(info, parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

# --- АДМИН-ПАНЕЛЬ ---
@dp.message(lambda msg: msg.text == "🔧 Админ")
async def admin_menu(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user): return
    await state.clear()
    kb = [
        [KeyboardButton(text="👥 Пользователи")],
        [KeyboardButton(text="📨 Рассылка")],
        [KeyboardButton(text="💬 Управление чатом")],
        [KeyboardButton(text="📋 Белый лист")],
        [KeyboardButton(text="↩ Назад")]
    ]
    await message.answer(get_text("admin_menu"), reply_markup=ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True), parse_mode="HTML")

@dp.message(lambda msg: msg.text == "👥 Пользователи")
async def admin_users_list(message: types.Message):
    if not is_admin(message.from_user): return
    user_ids = await get_all_user_ids()
    if not user_ids:
        await message.answer("Нет пользователей")
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[])
    for uid in user_ids[:50]:
        try:
            user = await bot.get_chat(uid)
            uname = f"@{user.username}" if user.username else f"id{uid}"
        except:
            uname = f"id{uid}"
        markup.inline_keyboard.append([
            InlineKeyboardButton(text=f"{uname}", callback_data=f"user_{uid}")
        ])
    await message.answer("👥 Список пользователей:", reply_markup=markup)

@dp.callback_query(F.data.startswith("user_"))
async def user_actions_menu(call: CallbackQuery):
    if not is_admin(call.from_user): return
    target_id = int(call.data.split("_")[1])
    status = await get_user_status(target_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    if status != "banned":
        kb.inline_keyboard.append([InlineKeyboardButton(text="Забанить", callback_data=f"ban_{target_id}")])
    if status != "muted":
        kb.inline_keyboard.append([InlineKeyboardButton(text="Замутить", callback_data=f"mute_{target_id}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="↩ Назад", callback_data="back_to_users")])
    await call.message.edit_reply_markup(reply_markup=kb)

@dp.callback_query(F.data.startswith("ban_"))
async def ban_user(call: CallbackQuery):
    if not is_admin(call.from_user): return
    target_id = int(call.data.split("_")[1])
    await set_user_status(target_id, "banned")
    await call.answer(f"Пользователь {target_id} забанен")
    await admin_users_list(call.message)

@dp.callback_query(F.data.startswith("mute_"))
async def mute_user(call: CallbackQuery):
    if not is_admin(call.from_user): return
    target_id = int(call.data.split("_")[1])
    await set_user_status(target_id, "muted")
    await call.answer(f"Пользователь {target_id} замучен")
    await admin_users_list(call.message)

@dp.callback_query(F.data == "back_to_users")
async def back_to_users(call: CallbackQuery):
    await admin_users_list(call.message)

@dp.message(lambda msg: msg.text == "📋 Белый лист")
async def whitelist_menu(message: types.Message):
    if not is_admin(message.from_user): return
    user_ids = await get_all_user_ids()
    blocked = []
    for uid in user_ids:
        status = await get_user_status(uid)
        if status != "active":
            blocked.append((uid, status))
    if not blocked:
        await message.answer(get_text("whitelist_empty"))
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[])
    for uid, st in blocked:
        try:
            user = await bot.get_chat(uid)
            uname = f"@{user.username}" if user.username else f"id{uid}"
        except:
            uname = f"id{uid}"
        cb = f"unban_{uid}" if st == "banned" else f"unmute_{uid}"
        btn_text = "Разбанить" if st == "banned" else "Размутить"
        markup.inline_keyboard.append([
            InlineKeyboardButton(text=f"{uname} ({st})", callback_data=f"info_{uid}"),
            InlineKeyboardButton(text=btn_text, callback_data=cb)
        ])
    await message.answer("📋 Белый лист:", reply_markup=markup)

@dp.callback_query(F.data.startswith("unban_"))
async def unban_user(call: CallbackQuery):
    if not is_admin(call.from_user): return
    target_id = int(call.data.split("_")[1])
    await set_user_status(target_id, "active")
    await call.answer(f"Пользователь {target_id} разбанен")
    await whitelist_menu(call.message)

@dp.callback_query(F.data.startswith("unmute_"))
async def unmute_user(call: CallbackQuery):
    if not is_admin(call.from_user): return
    target_id = int(call.data.split("_")[1])
    await set_user_status(target_id, "active")
    await call.answer(f"Пользователь {target_id} размучен")
    await whitelist_menu(call.message)

@dp.message(lambda msg: msg.text == "📨 Рассылка")
async def broadcast_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user): return
    await state.set_state(Broadcast.waiting_for_message)
    await message.answer("📨 Введите сообщение для рассылки:", reply_markup=get_cancel_kb())

@dp.message(StateFilter(Broadcast.waiting_for_message), lambda msg: msg.text == "↩ Назад")
async def broadcast_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await admin_menu(message, state)

@dp.message(Broadcast.waiting_for_message)
async def broadcast_send(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user): return
    await state.clear()
    sent = 0
    user_ids = await get_all_user_ids()
    for uid in user_ids:
        try:
            await bot.send_message(uid, message.text)
            sent += 1
        except Exception as e:
            logging.warning(f"Не удалось отправить сообщение {uid}: {e}")
    await message.answer(get_text("broadcast_sent", count=sent), reply_markup=await get_main_kb(message.from_user))

# Управление чатом
@dp.message(lambda msg: msg.text == "💬 Управление чатом")
async def admin_chat_manage(message: types.Message):
    if not is_admin(message.from_user): return
    chat_enabled = await get_chat_state()
    count = len(chat_users)
    status = "включён" if chat_enabled else "выключен"
    text = f"💬 <b>Управление чатом</b>\nУчастников: {count}\nСтатус: {status}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Выключить чат" if chat_enabled else "Включить чат",
            callback_data="toggle_chat"
        )],
        [InlineKeyboardButton(text="↩ Назад", callback_data="admin_back")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "toggle_chat")
async def toggle_chat_global(call: CallbackQuery):
    if not is_admin(call.from_user): return
    chat_enabled = await get_chat_state()
    await set_chat_state(not chat_enabled)
    if chat_enabled:
        for uid in chat_users.copy():
            try:
                await bot.send_message(uid, "💬 Чат временно отключён.")
            except: pass
        chat_users.clear()
    await call.message.edit_text("✅ Общий чат включён." if not chat_enabled else "🛑 Общий чат выключен.")
    await call.answer()

@dp.callback_query(F.data == "admin_back")
async def admin_back(call: CallbackQuery):
    await call.message.delete()
    await admin_menu(call.message, None)

# Чат для обычных пользователей
@dp.message(lambda msg: msg.text == "💬 Чат")
async def toggle_chat(message: types.Message):
    if not await check_status(message): return
    user_id = message.from_user.id
    chat_enabled = await get_chat_state()
    if not chat_enabled:
        await message.answer(get_text("chat_off"))
        return
    if user_id in chat_users:
        chat_users.discard(user_id)
        await message.answer(get_text("chat_leave"))
    else:
        chat_users.add(user_id)
        await message.answer(get_text("chat_enter"))
        name = await get_display_name(user_id)
        for uid in chat_users:
            if uid != user_id:
                try:
                    await bot.send_message(uid, get_text("chat_joined", name=name))
                except:
                    pass

@dp.message(F.text, ~F.text.in_(["🌟 Погода", "💰 Курсы валют", "↩ Назад",
                                "➕ Добавить город", "💬 Чат", "🔧 Админ",
                                "👥 Пользователи", "📨 Рассылка", "📋 Белый лист",
                                "💬 Управление чатом", "👤 Профиль", "✏️ Изменить ник"]))
async def chat_message_handler(message: types.Message):
    if not await check_status(message): return
    user_id = message.from_user.id
    if user_id not in chat_users or not await get_chat_state():
        return
    name = await get_display_name(user_id)
    for uid in chat_users.copy():
        if uid == user_id: continue
        try:
            await bot.send_message(uid, f"💬 {name}: {message.text}")
        except:
            chat_users.discard(uid)

@dp.message(lambda msg: msg.text == "↩ Назад")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    loc = await get_location(user_id)
    if loc:
        await message.answer(get_text("welcome_back"), reply_markup=await get_main_kb(message.from_user), parse_mode="HTML")
    else:
        await message.answer(get_text("welcome_new"), reply_markup=get_location_kb(), parse_mode="HTML")

# ---------- ВЕБ-СЕРВЕР ----------
async def handle(request):
    return web.Response(text="Bot is running")

async def main():
    await init_db()
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Web server started on port {port}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

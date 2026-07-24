import asyncio
import logging
import os
import time

from datetime import datetime, timezone, timedelta

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)


# ================= НАСТРОЙКИ =================


TOKEN = "8641527466:AAGSkaTzMJm5X6ExY3vVYRiMLxkwSxOOpnU"



ADMIN_USERNAMES = [
    "Woozinoid",
    "HwangMinw"
]


MOSCOW_TZ = timezone(
    timedelta(hours=3)
)



logging.basicConfig(
    level=logging.INFO,
    format=
    "%(asctime)s | %(levelname)s | %(message)s"
)



bot = Bot(
    token=TOKEN
)


dp = Dispatcher(
    storage=MemoryStorage()
)



# ================= ПАМЯТЬ =================


# пользователи

all_user_ids = set()


# города

user_locations = {}

user_cities = {}



# профили

user_nicknames = {}

user_statuses = {}



# чат

chat_users = set()

chat_enabled = False



# статистика

user_activity = {}

message_count = 0



# антиспам

spam_data = {}

SPAM_DELAY = 2



# логи

admin_logs = []



# новости

news_subscribers = {}

favorite_news = {}



# кэш

weather_cache = {}

currency_cache = {}



CACHE_TIME = 300




# ================= FSM =================


class AddCity(StatesGroup):

    waiting_name = State()



class SetNickname(StatesGroup):

    waiting_nick = State()



class Broadcast(StatesGroup):

    waiting_message = State()



class NewsSearch(StatesGroup):

    waiting_text = State()




# ================= ВСПОМОГАТЕЛЬНОЕ =================


def is_admin(user):

    if not user.username:
        return False


    return (
        user.username.lower()
        in
        [
            x.lower()
            for x in ADMIN_USERNAMES
        ]
    )




def log_action(text):

    admin_logs.append(
        {
            "time":
            datetime.now()
            .strftime(
                "%d.%m.%Y %H:%M:%S"
            ),

            "text": text
        }
    )


    if len(admin_logs) > 300:

        admin_logs.pop(0)




def update_activity(uid):

    global message_count


    message_count += 1


    user_activity[uid] = (
        user_activity.get(
            uid,
            0
        )
        +
        1
    )




def anti_spam(uid):

    now = time.time()


    last = spam_data.get(
        uid,
        0
    )


    if now - last < SPAM_DELAY:

        return False



    spam_data[uid] = now


    return True




async def check_user(message):

    uid = message.from_user.id


    status = user_statuses.get(
        uid,
        "active"
    )



    if status == "banned":

        await message.answer(
            "⛔ Вы заблокированы."
        )

        return False



    if status == "muted":

        await message.answer(
            "🔇 Вам запрещено писать."
        )

        return False




    if not anti_spam(uid):

        await message.answer(
            "⏳ Слишком быстро."
        )

        return False




    all_user_ids.add(uid)

    update_activity(uid)


    return True




async def display_name(uid):

    if uid in user_nicknames:

        return user_nicknames[uid]



    try:

        user = await bot.get_chat(uid)


        if user.username:

            return (
                "@"
                +
                user.username
            )


        return (
            user.first_name
            or
            str(uid)
        )


    except:

        return str(uid)





# ================= КЛАВИАТУРЫ =================



async def main_keyboard(user=None):


    buttons = [

        [
            KeyboardButton(
                text="🌟 Погода"
            ),

            KeyboardButton(
                text="💰 Курсы валют"
            )

        ],


        [

            KeyboardButton(
                text="📰 Новости"
            )

        ],


        [

            KeyboardButton(
                text="📍 Обновить геолокацию",
                request_location=True
            )

        ],


        [

            KeyboardButton(
                text="👤 Профиль"
            )

        ]

    ]



    if chat_enabled:

        buttons.append(
            [
                KeyboardButton(
                    text="💬 Чат"
                )
            ]
        )



    if user and is_admin(user):

        buttons.append(
            [
                KeyboardButton(
                    text="🔧 Админ"
                )
            ]
        )



    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True
    )





def back_keyboard():

    return ReplyKeyboardMarkup(

        keyboard=[

            [

                KeyboardButton(
                    text="↩ Назад"
                )

            ]

        ],

        resize_keyboard=True

    )
    # ================= ПОГОДА =================


WEATHER_CODES = {

    0: "☀️ Ясно",
    1: "🌤 Преимущественно ясно",
    2: "⛅ Облачно",
    3: "☁️ Пасмурно",
    45: "🌫 Туман",
    61: "🌧 Дождь",
    63: "🌧 Сильный дождь",
    71: "❄️ Снег",
    80: "🌦 Ливень",
    95: "⛈ Гроза"

}




async def geocode_city(name):

    url = (
        "https://nominatim.openstreetmap.org/search"
    )


    params = {

        "q": name,
        "format": "json",
        "limit": 1,
        "accept-language": "ru"

    }


    headers = {

        "User-Agent":
        "NovostiWooziBot"

    }


    try:

        async with aiohttp.ClientSession() as session:

            async with session.get(
                url,
                params=params,
                headers=headers
            ) as response:


                data = await response.json()



                if data:

                    return (

                        float(data[0]["lat"]),

                        float(data[0]["lon"])

                    )


    except Exception as e:

        logging.error(
            f"Geocode error {e}"
        )


    return None





async def get_weather(lat, lon, city):


    key = f"{lat}:{lon}"


    if key in weather_cache:


        saved, result = weather_cache[key]


        if time.time() - saved < CACHE_TIME:

            return result




    url = (
        "https://api.open-meteo.com/v1/forecast"
    )



    params = {

        "latitude": lat,

        "longitude": lon,

        "current":
        "temperature_2m,"
        "apparent_temperature,"
        "relative_humidity_2m,"
        "weather_code,"
        "cloud_cover,"
        "pressure_msl",

        "timezone":
        "auto"

    }




    async with aiohttp.ClientSession() as session:


        async with session.get(
            url,
            params=params
        ) as response:


            data = await response.json()



    current = data["current"]



    result = {

        "city": city,

        "temp":
        current["temperature_2m"],

        "feels":
        current["apparent_temperature"],

        "humidity":
        current["relative_humidity_2m"],

        "cloud":
        current["cloud_cover"],

        "pressure":
        round(
            current["pressure_msl"]
            *
            0.75006,
            1
        ),

        "desc":
        WEATHER_CODES.get(
            current["weather_code"],
            "Неизвестно"
        )

    }



    weather_cache[key] = (
        time.time(),
        result
    )



    return result





# ================= ВАЛЮТА =================



async def get_currency():


    if "currency" in currency_cache:


        saved, data = currency_cache["currency"]


        if time.time() - saved < 300:

            return data





    url = (
        "https://www.cbr-xml-daily.ru/daily_json.js"
    )



    async with aiohttp.ClientSession() as session:


        async with session.get(url) as response:


            data = await response.json()



    currency_cache["currency"] = (

        time.time(),

        data

    )


    return data







# ================= START =================



@dp.message(Command("start"))
async def start(
        message: types.Message,
        state: FSMContext):


    await state.clear()



    uid = message.from_user.id


    all_user_ids.add(uid)



    await message.answer(

        "🌈 Добро пожаловать в "
        "Новости · Вузи!\n\n"
        "Выберите действие:",

        reply_markup=
        await main_keyboard(
            message.from_user
        )

    )







# ================= ГЕОЛОКАЦИЯ =================



@dp.message(F.location)
async def save_location(
        message: types.Message):


    if not await check_user(message):

        return



    uid = message.from_user.id



    user_locations[uid] = (

        message.location.latitude,

        message.location.longitude

    )



    await message.answer(

        "✅ Геолокация сохранена",

        reply_markup=
        await main_keyboard(
            message.from_user
        )

    )








# ================= ПРОФИЛЬ =================



@dp.message(
    lambda m:
    m.text == "👤 Профиль"
)
async def profile(
        message: types.Message):


    uid = message.from_user.id



    nick = user_nicknames.get(

        uid,

        "не установлен"

    )



    kb = ReplyKeyboardMarkup(

        keyboard=[

            [

                KeyboardButton(
                    text="✏️ Изменить ник"
                )

            ],

            [

                KeyboardButton(
                    text="↩ Назад"
                )

            ]

        ],

        resize_keyboard=True

    )



    await message.answer(

        f"👤 Профиль\n\n"
        f"Ник: {nick}",

        reply_markup=kb

    )






@dp.message(
    lambda m:
    m.text == "✏️ Изменить ник"
)
async def nick_start(
        message: types.Message,
        state: FSMContext):


    await state.set_state(
        SetNickname.waiting_nick
    )



    await message.answer(
        "Введите новый ник:"
    )







@dp.message(
    StateFilter(SetNickname.waiting_nick),
    F.text
)
async def nick_save(
        message: types.Message,
        state: FSMContext):


    nick = message.text.strip()



    if len(nick) > 20:

        await message.answer(
            "❌ Максимум 20 символов"
        )

        return




    user_nicknames[
        message.from_user.id
    ] = nick



    await state.clear()



    await message.answer(

        f"✅ Ник изменён: {nick}",

        reply_markup=
        await main_keyboard(
            message.from_user
        )

    )
            # ================= МЕНЮ ПОГОДЫ =================


@dp.message(
    lambda m:
    m.text == "🌟 Погода"
)
async def weather_menu(
        message: types.Message):


    if not await check_user(message):
        return


    uid = message.from_user.id


    buttons = []



    for city in user_cities.get(uid, []):

        buttons.append(

            [

                KeyboardButton(
                    text=
                    "🏙 " + city["name"]
                )

            ]

        )



    buttons.append(

        [

            KeyboardButton(
                text="➕ Добавить город"
            )

        ]

    )


    buttons.append(

        [

            KeyboardButton(
                text="↩ Назад"
            )

        ]

    )



    await message.answer(

        "🌟 Выберите город:",

        reply_markup=
        ReplyKeyboardMarkup(
            keyboard=buttons,
            resize_keyboard=True
        )

    )






# ================= ДОБАВЛЕНИЕ ГОРОДА =================



@dp.message(
    lambda m:
    m.text == "➕ Добавить город"
)
async def add_city(
        message: types.Message,
        state: FSMContext):


    await state.set_state(
        AddCity.waiting_name
    )


    await message.answer(
        "🏙 Напишите название города:"
    )





@dp.message(
    StateFilter(AddCity.waiting_name),
    F.text
)
async def save_city(
        message: types.Message,
        state: FSMContext):


    city_name = message.text.strip()



    msg = await message.answer(
        "⏳ Ищу город..."
    )



    coords = await geocode_city(
        city_name
    )



    if not coords:


        await msg.edit_text(
            "❌ Город не найден"
        )

        return





    uid = message.from_user.id



    if uid not in user_cities:

        user_cities[uid] = []



    user_cities[uid].append(

        {

            "name":
            city_name,

            "lat":
            coords[0],

            "lon":
            coords[1]

        }

    )



    await state.clear()



    await msg.edit_text(

        f"✅ Город {city_name} добавлен"

    )



    await message.answer(

        "Главное меню:",

        reply_markup=
        await main_keyboard(
            message.from_user
        )

    )







# ================= ПОКАЗ ПОГОДЫ =================



@dp.message(
    lambda m:
    m.text and
    m.text.startswith("🏙 ")
)
async def city_weather(
        message: types.Message):


    uid = message.from_user.id



    city_name = (
        message.text[2:]
        .strip()
    )



    city = None



    for c in user_cities.get(uid, []):

        if c["name"] == city_name:

            city = c
            break



    if not city:

        await message.answer(
            "❌ Город не найден"
        )

        return





    loading = await message.answer(
        "⏳ Загружаю погоду..."
    )



    try:


        weather = await get_weather(

            city["lat"],

            city["lon"],

            city_name

        )



        text = (

            f"🌍 {weather['city']}\n\n"

            f"{weather['desc']}\n"

            f"🌡 Температура: "
            f"{weather['temp']}°C\n"

            f"🤔 Ощущается: "
            f"{weather['feels']}°C\n"

            f"💧 Влажность: "
            f"{weather['humidity']}%\n"

            f"☁ Облачность: "
            f"{weather['cloud']}%\n"

            f"🔵 Давление: "
            f"{weather['pressure']} мм"

        )



        await loading.edit_text(
            text
        )



    except Exception as e:


        await loading.edit_text(

            f"❌ Ошибка: {e}"

        )







# ================= ВАЛЮТА =================



@dp.message(
    lambda m:
    m.text == "💰 Курсы валют"
)
async def currency(
        message: types.Message):


    try:


        data = await get_currency()



        usd = (
            data["Valute"]
            ["USD"]
            ["Value"]
        )


        eur = (
            data["Valute"]
            ["EUR"]
            ["Value"]
        )



        await message.answer(

            "💰 Курсы ЦБ РФ\n\n"

            f"🇺🇸 USD: {usd:.2f} ₽\n"

            f"🇪🇺 EUR: {eur:.2f} ₽"

        )



    except Exception as e:


        await message.answer(

            f"❌ Ошибка: {e}"

        )







# ================= НАЗАД =================



@dp.message(
    lambda m:
    m.text == "↩ Назад"
)
async def back(
        message: types.Message,
        state: FSMContext):


    await state.clear()



    await message.answer(

        "🌈 Главное меню:",

        reply_markup=
        await main_keyboard(
            message.from_user
        )

    )







# ================= ЧАТ =================



@dp.message(
    lambda m:
    m.text == "💬 Чат"
)
async def chat_enter(
        message: types.Message):


    if not chat_enabled:


        await message.answer(
            "💬 Чат сейчас отключён"
        )

        return



    uid = message.from_user.id



    if uid in chat_users:


        chat_users.remove(uid)



        await message.answer(
            "💬 Вы вышли из чата"
        )


        return





    chat_users.add(uid)



    name = await display_name(uid)



    log_action(
        f"{name} вошёл в чат"
    )



    await message.answer(
        "💬 Вы вошли в чат"
    )



    for user in chat_users.copy():

        if user != uid:

            try:

                await bot.send_message(

                    user,

                    f"👋 {name} вошёл в чат"

                )

            except:

                chat_users.discard(user)
                # ================= ЧАТ СООБЩЕНИЯ =================


@dp.message(F.sticker)
async def chat_sticker(
        message: types.Message):


    uid = message.from_user.id


    if (
        uid not in chat_users
        or not chat_enabled
    ):
        return



    name = await display_name(uid)



    for user in chat_users.copy():

        if user == uid:
            continue


        try:

            await bot.send_sticker(

                user,

                message.sticker.file_id

            )


        except:

            chat_users.discard(user)






@dp.message(F.text)
async def chat_text(
        message: types.Message):


    uid = message.from_user.id



    ignored = [

        "🌟 Погода",

        "💰 Курсы валют",

        "📰 Новости",

        "👤 Профиль",

        "💬 Чат",

        "🔧 Админ",

        "↩ Назад",

        "➕ Добавить город",

        "✏️ Изменить ник"

    ]



    if (

        uid not in chat_users

        or not chat_enabled

        or message.text in ignored

    ):

        return




    name = await display_name(uid)



    for user in chat_users.copy():


        if user == uid:
            continue


        try:


            await bot.send_message(

                user,

                f"💬 {name}: {message.text}",

                reply_to_message_id=
                message.message_id

            )



        except:


            chat_users.discard(user)







# ================= НОВОСТИ =================


NEWS_SOURCES = [

    "https://lenta.ru/rss",

    "https://ria.ru/export/rss2/archive/index.xml"

]



news_cache = []







async def load_news():


    global news_cache


    result = []



    try:


        async with aiohttp.ClientSession() as session:


            async with session.get(
                NEWS_SOURCES[0]
            ) as response:


                text = await response.text()



        import xml.etree.ElementTree as ET



        root = ET.fromstring(text)



        for item in root.findall(
            ".//item"
        )[:10]:


            title = item.findtext(
                "title"
            )


            link = item.findtext(
                "link"
            )



            if title:

                result.append(

                    {

                        "title": title,

                        "link": link

                    }

                )



        news_cache = result



    except Exception as e:


        logging.error(
            f"News error {e}"
        )



    return news_cache






@dp.message(
    lambda m:
    m.text == "📰 Новости"
)
async def news_menu(
        message: types.Message):


    news = await load_news()



    if not news:


        await message.answer(
            "❌ Новости недоступны"
        )

        return




    buttons = []



    for i, item in enumerate(news):


        buttons.append(

            [

                InlineKeyboardButton(

                    text=
                    item["title"][:50],

                    url=
                    item["link"]

                )

            ]

        )



    await message.answer(

        "📰 Последние новости:",

        reply_markup=
        InlineKeyboardMarkup(
            inline_keyboard=buttons
        )

    )







# ================= ПОИСК НОВОСТЕЙ =================



@dp.message(
    Command("search")
)
async def search_news_start(
        message: types.Message,
        state: FSMContext):


    await state.set_state(
        NewsSearch.waiting_text
    )


    await message.answer(

        "🔍 Напишите слово для поиска:"

    )





@dp.message(
    StateFilter(
        NewsSearch.waiting_text
    ),
    F.text
)
async def search_news(
        message: types.Message,
        state: FSMContext):


    query = message.text.lower()



    news = await load_news()



    found = []



    for item in news:


        if query in item["title"].lower():

            found.append(item)





    await state.clear()



    if not found:


        await message.answer(
            "❌ Ничего не найдено"
        )

        return





    buttons = []



    for item in found:


        buttons.append(

            [

                InlineKeyboardButton(

                    text=
                    item["title"][:50],

                    url=
                    item["link"]

                )

            ]

        )



    await message.answer(

        "🔍 Результаты:",

        reply_markup=
        InlineKeyboardMarkup(
            inline_keyboard=buttons
        )

    )








# ================= ИЗБРАННОЕ =================



@dp.message(
    Command("favorite")
)
async def add_favorite(
        message: types.Message):


    uid = message.from_user.id



    if uid not in favorite_news:

        favorite_news[uid] = []



    await message.answer(

        "⭐ Используйте кнопку "
        "сохранения новости "
        "(добавим в следующей части)"

    )






# ================= ПОДПИСКИ =================



@dp.message(
    Command("subscribe")
)
async def subscribe_news(
        message: types.Message):


    uid = message.from_user.id



    news_subscribers[uid] = True



    await message.answer(

        "🔔 Вы подписались "
        "на новости"

    )
            # ================= АДМИН ПАНЕЛЬ =================


@dp.message(
    lambda m:
    m.text == "🔧 Админ"
)
async def admin_menu(
        message: types.Message):


    if not is_admin(
        message.from_user
    ):
        return



    kb = ReplyKeyboardMarkup(

        keyboard=[

            [

                KeyboardButton(
                    text="👥 Статистика"
                )

            ],

            [

                KeyboardButton(
                    text="📨 Рассылка"
                )

            ],

            [

                KeyboardButton(
                    text="🚫 Управление"
                )

            ],

            [

                KeyboardButton(
                    text="📋 Логи"
                )

            ],

            [

                KeyboardButton(
                    text="💬 Вкл/Выкл чат"
                )

            ],

            [

                KeyboardButton(
                    text="↩ Назад"
                )

            ]

        ],

        resize_keyboard=True

    )



    await message.answer(

        "🔧 Админ-панель",

        reply_markup=kb

    )






# ================= СТАТИСТИКА =================



@dp.message(
    lambda m:
    m.text == "👥 Статистика"
)
async def statistics(
        message: types.Message):


    if not is_admin(
        message.from_user
    ):
        return



    top = sorted(

        user_activity.items(),

        key=lambda x: x[1],

        reverse=True

    )[:10]



    text = (

        "📊 Статистика\n\n"

        f"👤 Пользователей: "
        f"{len(all_user_ids)}\n"

        f"💬 Сообщений: "
        f"{message_count}\n\n"

        "🏆 Топ:\n"

    )



    for uid, count in top:


        text += (

            f"{uid}: "
            f"{count}\n"

        )



    await message.answer(text)







# ================= РАССЫЛКА =================



@dp.message(
    lambda m:
    m.text == "📨 Рассылка"
)
async def broadcast_start(
        message: types.Message,
        state: FSMContext):


    if not is_admin(
        message.from_user
    ):
        return



    await state.set_state(
        Broadcast.waiting_message
    )



    await message.answer(

        "📨 Введите текст рассылки:"

    )






@dp.message(
    StateFilter(
        Broadcast.waiting_message
    ),
    F.text
)
async def broadcast_send(
        message: types.Message,
        state: FSMContext):


    if not is_admin(
        message.from_user
    ):
        return



    sent = 0



    for uid in all_user_ids.copy():


        try:


            await bot.send_message(

                uid,

                message.text

            )


            sent += 1



        except:


            pass




    await state.clear()



    await message.answer(

        f"✅ Отправлено: {sent}"

    )







# ================= БАН / МУТ =================



@dp.message(
    lambda m:
    m.text == "🚫 Управление"
)
async def manage_users(
        message: types.Message):


    if not is_admin(
        message.from_user
    ):
        return



    await message.answer(

        "Команды:\n\n"

        "/ban ID\n"

        "/mute ID\n"

        "/unban ID\n"

        "/unmute ID"

    )






@dp.message(
    Command("ban")
)
async def ban_user(
        message: types.Message):


    if not is_admin(
        message.from_user
    ):
        return



    try:


        uid = int(
            message.text.split()[1]
        )


        user_statuses[uid] = "banned"


        log_action(
            f"{uid} заблокирован"
        )


        await message.answer(
            "⛔ Пользователь заблокирован"
        )


    except:


        await message.answer(
            "Использование: /ban ID"
        )







@dp.message(
    Command("mute")
)
async def mute_user(
        message: types.Message):


    if not is_admin(
        message.from_user
    ):
        return



    try:


        uid = int(
            message.text.split()[1]
        )


        user_statuses[uid] = "muted"



        await message.answer(
            "🔇 Пользователь замучен"
        )


    except:


        await message.answer(
            "Использование: /mute ID"
        )







@dp.message(
    Command("unban")
)
async def unban_user(
        message: types.Message):


    if not is_admin(
        message.from_user
    ):
        return



    uid = int(
        message.text.split()[1]
    )


    user_statuses[uid] = "active"



    await message.answer(
        "✅ Разблокирован"
    )







@dp.message(
    Command("unmute")
)
async def unmute_user(
        message: types.Message):


    if not is_admin(
        message.from_user
    ):
        return



    uid = int(
        message.text.split()[1]
    )


    user_statuses[uid] = "active"



    await message.answer(
        "✅ Мут снят"
    )








# ================= ЛОГИ =================



@dp.message(
    lambda m:
    m.text == "📋 Логи"
)
async def logs(
        message: types.Message):


    if not is_admin(
        message.from_user
    ):
        return



    text = "📋 Последние действия:\n\n"



    for item in admin_logs[-20:]:


        text += (

            f"{item['time']} "
            f"- "
            f"{item['text']}\n"

        )



    await message.answer(text)








# ================= ЧАТ ПЕРЕКЛЮЧАТЕЛЬ =================



@dp.message(
    lambda m:
    m.text == "💬 Вкл/Выкл чат"
)
async def toggle_chat(
        message: types.Message):


    global chat_enabled



    if not is_admin(
        message.from_user
    ):
        return



    chat_enabled = not chat_enabled



    if not chat_enabled:

        chat_users.clear()



    await message.answer(

        "💬 Чат: "

        +

        (
            "включён"
            if chat_enabled
            else
            "выключен"
        )

    )








# ================= АВТОНОВОСТИ =================



async def news_task():


    while True:


        await asyncio.sleep(
            1800
        )


        if not news_subscribers:

            continue



        news = await load_news()



        if not news:

            continue



        item = news[0]



        text = (

            "📰 Новость:\n\n"

            f"{item['title']}\n\n"

            f"{item['link']}"

        )



        for uid in list(
            news_subscribers.keys()
        ):


            try:


                await bot.send_message(

                    uid,

                    text

                )


            except:


                pass







# ================= RENDER =================



async def home(
        request):


    return web.Response(

        text=
        "Новости · Вузи работает"

    )






async def main():


    app = web.Application()



    app.router.add_get(
        "/",
        home
    )



    runner = web.AppRunner(
        app
    )


    await runner.setup()



    port = int(

        os.getenv(
            "PORT",
            8080
        )

    )



    site = web.TCPSite(

        runner,

        "0.0.0.0",

        port

    )



    await site.start()



    asyncio.create_task(
        news_task()
    )



    logging.info(
        "Bot started"
    )



    await dp.start_polling(
        bot
    )







if __name__ == "__main__":


    asyncio.run(
        main()
    )

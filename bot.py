
import asyncio
import html
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vpn_shop")

BOT_TOKEN = config.BOT_TOKEN
ADMIN_ID = int(config.ADMIN_ID)
DB_PATH = getattr(config, "DATABASE", "vpn_bot.db")
BOT_NAME = getattr(config, "BOT_NAME", "VPN SHOP")
LOCAL_TZ = timezone(timedelta(hours=4), name="Asia/Yerevan")

HAPP_IOS = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
HAPP_ANDROID = "https://play.google.com/store/apps/details?id=com.happproxy"
GAME_BOT_URL = "https://t.me/GameBRxocsShop_Bot"

PAYMENT_DEFAULT = (
    "💳 <b>ОПЛАТА ПОДПИСКИ</b>\n\n"
    "🏦 Банк: Сбер\n"
    "💳 Номер карты: 2202209226426665\n"
    "👤 Получатель: Артем\n\n"
    "💰 К оплате: {price} ₽\n"
    "📦 Тариф: {tariff}\n"
    "📅 Срок: {days} дней\n\n"
    "После оплаты нажмите «✅ Я оплатил» и отправьте чек."
)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")


# ============================================================
# DATABASE
# ============================================================

def db_execute(query, params=()):
    cur = db.execute(query, params)
    db.commit()
    return cur


def db_one(query, params=()):
    return db.execute(query, params).fetchone()


def db_all(query, params=()):
    return db.execute(query, params).fetchall()


def ensure_column(table, column, definition):
    columns = {
        row[1]
        for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        db.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )
        db.commit()


def now_utc():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def parse_dt(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def local_date(value):
    dt = parse_dt(value)
    return (
        dt.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
        if dt else "—"
    )


def setup_database():
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            first_seen TEXT,
            last_seen TEXT,
            orders_count INTEGER NOT NULL DEFAULT 0,
            total_spent REAL NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            tariff TEXT NOT NULL DEFAULT 'VPN',
            price INTEGER NOT NULL DEFAULT 0,
            days INTEGER NOT NULL DEFAULT 30,
            status TEXT NOT NULL DEFAULT 'waiting_admin',
            receipt_file_id TEXT,
            vpn_key TEXT,
            created_at TEXT,
            issued_at TEXT,
            expires_at TEXT,
            receipt_type TEXT DEFAULT 'photo',
            promo_code TEXT,
            discount INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tariffs (
            tariff_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            days INTEGER NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY,
            note TEXT DEFAULT '',
            added_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY COLLATE NOCASE,
            discount INTEGER NOT NULL,
            max_uses INTEGER NOT NULL,
            per_user INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
    """)

    migrations = {
        "users": {
            "username": "TEXT DEFAULT ''",
            "first_name": "TEXT DEFAULT ''",
            "first_seen": "TEXT",
            "last_seen": "TEXT",
            "orders_count": "INTEGER NOT NULL DEFAULT 0",
            "total_spent": "REAL NOT NULL DEFAULT 0",
            "blocked": "INTEGER NOT NULL DEFAULT 0",
        },
        "orders": {
            "username": "TEXT DEFAULT ''",
            "tariff": "TEXT NOT NULL DEFAULT 'VPN'",
            "price": "INTEGER NOT NULL DEFAULT 0",
            "days": "INTEGER NOT NULL DEFAULT 30",
            "status": "TEXT NOT NULL DEFAULT 'waiting_admin'",
            "receipt_file_id": "TEXT",
            "vpn_key": "TEXT",
            "created_at": "TEXT",
            "issued_at": "TEXT",
            "expires_at": "TEXT",
            "receipt_type": "TEXT DEFAULT 'photo'",
            "promo_code": "TEXT",
            "discount": "INTEGER NOT NULL DEFAULT 0",
        },
        "whitelist": {
            "note": "TEXT DEFAULT ''",
            "added_at": "TEXT NOT NULL DEFAULT ''",
        },
    }

    for table, columns in migrations.items():
        for column, definition in columns.items():
            ensure_column(table, column, definition)

    defaults = {
        "welcome": getattr(
            config,
            "WELCOME_TEXT",
            f"👋 Добро пожаловать в {html.escape(BOT_NAME)}!\n\n"
            "Выберите действие ниже.",
        ),
        "payment": PAYMENT_DEFAULT,
        "support": (
            "💬 <b>Поддержка</b>\n\n"
            "По вопросам оплаты и подключения напишите администратору."
        ),
        "instruction": (
            "📖 <b>Как подключиться через HAPP</b>\n\n"
            "1️⃣ Установите HAPP по кнопке для своего устройства.\n"
            "2️⃣ Вернитесь в сообщение с VPN-ключом.\n"
            "3️⃣ Скопируйте ключ целиком.\n"
            "4️⃣ Откройте HAPP и выберите добавление профиля.\n"
            "5️⃣ Выберите импорт из буфера обмена или вставьте ссылку.\n"
            "6️⃣ Выберите профиль и нажмите подключение.\n"
            "7️⃣ На iPhone подтвердите системный запрос на добавление VPN.\n\n"
            "Названия пунктов могут отличаться в зависимости от версии HAPP."
        ),
        "delivery": (
            "🎉 <b>VPN готов!</b>\n\n"
            "📦 Тариф: <b>{tariff}</b>\n"
            "📅 Срок: <b>{days} дней</b>\n"
            "📆 Действует до: <b>{expires}</b>\n"
            "🧾 Заказ: <b>#{order_id}</b>\n\n"
            "🔐 <b>Ваш ключ:</b>\n<code>{key}</code>\n\n"
            "Скопируйте ключ целиком и добавьте его в HAPP."
        ),
        "whitelist_info": (
            "🤍 <b>Белый список</b>\n\n"
            "Пользователи из белого списка могут пользоваться ботом "
            "без проверки обязательной подписки. "
            "Белый список не выдаёт VPN-ключ бесплатно."
        ),
        "required_channel": "",
        "required_channel_name": "",
        "required_channel_url": "",
    }

    for key, value in defaults.items():
        db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
            (key, str(value)),
        )

    # Исправленный текст оплаты и реквизиты применяются один раз.
    if not db.execute(
        "SELECT 1 FROM settings WHERE key='payment_text_v4'"
    ).fetchone():
        db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('payment',?)",
            (PAYMENT_DEFAULT,),
        )
        db.execute(
            "INSERT OR REPLACE INTO settings(key,value) "
            "VALUES('payment_text_v4','1')"
        )

    source_tariffs = getattr(config, "TARIFFS", {}) or {
        "vpn_7": {"name": "VPN на 7 дней", "price": 99, "days": 7},
        "vpn_30": {"name": "VPN на 30 дней", "price": 199, "days": 30},
        "vpn_90": {"name": "VPN на 90 дней", "price": 499, "days": 90},
    }

    for index, (tid, item) in enumerate(source_tariffs.items()):
        db.execute("""
            INSERT OR IGNORE INTO tariffs
                (tariff_id,name,price,days,description,enabled,sort_order)
            VALUES (?,?,?,?,?,1,?)
        """, (
            str(tid),
            str(item.get("name", tid)),
            max(0, int(item.get("price", 0))),
            max(1, int(item.get("days", 30))),
            str(item.get("description", "")),
            index,
        ))

    if not db.execute("SELECT 1 FROM tariffs LIMIT 1").fetchone():
        for index, item in enumerate([
            ("vpn_7", "VPN на 7 дней", 99, 7),
            ("vpn_30", "VPN на 30 дней", 199, 30),
            ("vpn_90", "VPN на 90 дней", 499, 90),
        ]):
            db.execute("""
                INSERT INTO tariffs
                    (tariff_id,name,price,days,description,enabled,sort_order)
                VALUES (?,?,?,?,?,1,?)
            """, (*item, "", index))

    db.commit()


setup_database()


def setting(key):
    row = db_one(
        "SELECT value FROM settings WHERE key=?",
        (key,),
    )
    return str(row["value"]) if row else ""


def set_setting(key, value):
    db_execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
        (key, str(value)),
    )


def get_tariffs(enabled_only=True):
    query = (
        "SELECT * FROM tariffs WHERE enabled=1 ORDER BY sort_order,tariff_id"
        if enabled_only
        else "SELECT * FROM tariffs ORDER BY sort_order,tariff_id"
    )
    return db_all(query)


def get_tariff(tariff_id):
    return db_one(
        "SELECT * FROM tariffs WHERE tariff_id=?",
        (tariff_id,),
    )


def touch_user(user):
    stamp = iso(now_utc())
    row = db_one(
        "SELECT user_id FROM users WHERE user_id=?",
        (user.id,),
    )

    if row:
        db_execute("""
            UPDATE users
            SET username=?,first_name=?,last_seen=?,blocked=0
            WHERE user_id=?
        """, (
            user.username or "",
            user.first_name or "",
            stamp,
            user.id,
        ))
    else:
        db_execute("""
            INSERT INTO users(user_id,username,first_name,first_seen,last_seen)
            VALUES(?,?,?,?,?)
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            stamp,
            stamp,
        ))


def is_admin(user_id):
    return int(user_id) == ADMIN_ID


def is_whitelisted(user_id):
    return db_one(
        "SELECT 1 FROM whitelist WHERE user_id=?",
        (user_id,),
    ) is not None


def esc(value):
    return html.escape(str("" if value is None else value), quote=True)


# ============================================================
# КНОПКИ
# ============================================================

def style_button(text, callback_data=None, url=None, style=None):
    # Защита от ошибки, когда success/primary/danger считались URL.
    if style is None and url in {"primary", "success", "danger"}:
        style, url = url, None

    kwargs = {"text": text}

    if callback_data is not None:
        kwargs["callback_data"] = callback_data

    if url is not None:
        kwargs["url"] = url

    # На старой aiogram не передаём неподдерживаемый параметр style.
    model_fields = getattr(InlineKeyboardButton, "model_fields", {})
    if style in {"primary", "success", "danger"} and "style" in model_fields:
        kwargs["style"] = style

    return InlineKeyboardButton(**kwargs)


def make_kb(rows):
    # Принимает и описание одной кнопки, и строку из нескольких кнопок.
    normalized = []

    for row in rows:
        if (
            isinstance(row, (list, tuple))
            and row
            and isinstance(row[0], str)
        ):
            row = [row]

        normalized.append([
            style_button(*button)
            for button in row
        ])

    return InlineKeyboardMarkup(inline_keyboard=normalized)


def back_kb(destination="menu"):
    return make_kb([["⬅️ Назад", destination]])


def main_kb(user_id):
    rows = [
        [["🛒 Купить VPN", "buy", "success"]],
        [["🔐 Моя подписка", "subscription", "primary"]],
        [["🏆 Рейтинг покупателей", "ranking"]],
        [["📖 Инструкция HAPP", "instruction"]],
        [["💬 Поддержка", "support"]],
    ]

    if is_admin(user_id):
        rows.append([["⚙️ Админ-панель", "admin", "primary"]])

    return make_kb(rows)


def tariffs_kb():
    rows = [
        [f"{row['name']} — {row['price']} ₽",
         f"tariff:{row['tariff_id']}", "success"]
        for row in get_tariffs(True)
    ]
    rows.append(["⬅️ Назад", "menu"])
    return make_kb(rows)


def payment_kb():
    return make_kb([
        ["🎟 Ввести промокод", "promo:enter", "primary"],
        ["✅ Я оплатил", "paid", "success"],
        ["⬅️ К тарифам", "buy"],
    ])


def happ_kb():
    return make_kb([
        ["🍎 HAPP для iPhone", None, HAPP_IOS, "primary"],
        ["🤖 HAPP для Android", None, HAPP_ANDROID, "success"],
        ["📖 Подробная инструкция", "instruction"],
    ])


def admin_kb():
    return make_kb([
        ["📥 Новые заявки", "admin:orders", "success"],
        ["📦 История заказов", "admin:history"],
        ["👥 Пользователи", "admin:users", "primary"],
        ["📊 Статистика", "admin:stats", "primary"],
        ["💰 Тарифы и цены", "admin:tariffs", "success"],
        ["📝 Редактор текстов", "admin:texts"],
        ["🤍 Белый список", "admin:whitelist"],
        ["🎟 Промокоды", "admin:promos", "success"],
        ["📢 Обязательная подписка", "admin:channel"],
        ["🏆 Рейтинг покупателей", "ranking"],
        ["📣 Рассылка", "admin:broadcast", "danger"],
        ["⚙️ Настройки", "admin:settings"],
        ["⬅️ В магазин", "menu"],
    ])


def order_kb(order_id):
    return make_kb([
        ["🔑 Выдать ключ", f"give:{order_id}", "success"],
        ["❌ Отклонить", f"reject:{order_id}", "danger"],
    ])


def text_kb():
    return make_kb([
        ["🏠 Главное сообщение", "edittext:welcome"],
        ["💳 Реквизиты / оплата", "edittext:payment"],
        ["📖 Инструкция HAPP", "edittext:instruction"],
        ["🎁 Сообщение после выдачи", "edittext:delivery"],
        ["💬 Поддержка", "edittext:support"],
        ["🤍 Описание белого списка", "edittext:whitelist_info"],
        ["⬅️ Назад", "admin"],
    ])


def format_template(template, values):
    result = template or ""
    for key, value in values.items():
        result = result.replace("{" + key + "}", esc(value))
    return result


def format_payment(tariff, price, days):
    return format_template(setting("payment"), {
        "tariff": tariff,
        "price": price,
        "days": days,
    })


def format_delivery(order, key, expires):
    return format_template(setting("delivery"), {
        "tariff": order["tariff"],
        "days": order["days"],
        "key": key,
        "expires": expires,
        "order_id": order["id"],
    })


# ============================================================
# СОСТОЯНИЯ
# ============================================================

class UserStates(StatesGroup):
    waiting_receipt = State()
    waiting_promo = State()


class AdminStates(StatesGroup):
    waiting_key = State()
    waiting_edit_text = State()
    waiting_price = State()
    waiting_name = State()
    waiting_days = State()
    waiting_description = State()
    waiting_add_tariff = State()
    waiting_whitelist_user = State()
    waiting_broadcast = State()
    waiting_add_promo = State()
    waiting_channel_name = State()
    waiting_channel_link = State()


async def admin_only(callback):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return False
    return True


async def admin_message_only(message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа.")
        return False
    return True


def status_value(status):
    return str(getattr(status, "value", status))


async def safe_edit(callback, text, reply_markup=None):
    if callback.message is None:
        await callback.answer()
        return

    try:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return

        await callback.message.answer(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


# ============================================================
# ОБЯЗАТЕЛЬНАЯ ПОДПИСКА
# ============================================================

async def user_has_channel(user_id):
    raw_channel = setting("required_channel")

    if (
        not raw_channel
        or is_admin(user_id)
        or is_whitelisted(user_id)
    ):
        return True

    channel = (
        int(raw_channel)
        if raw_channel.lstrip("-").isdigit()
        else raw_channel
    )

    try:
        member = await bot.get_chat_member(channel, user_id)
        status = status_value(member.status)

        return status in {
            "creator", "administrator", "member"
        } or (
            status == "restricted"
            and bool(getattr(member, "is_member", False))
        )

    except Exception as exc:
        logger.warning(
            "Не удалось проверить подписку на %s: %s",
            raw_channel,
            exc,
        )
        return False


def subscription_kb():
    rows = []
    channel_url = setting("required_channel_url")

    if channel_url:
        rows.append([
            "📢 Подписаться на канал",
            None,
            channel_url,
            "primary",
        ])

    # Дополнительный бот не проверяется и не заменяет подписку на канал.
    rows.append([
        "🎮 Запустить @GameBRxocsShop_Bot",
        None,
        GAME_BOT_URL,
        "success",
    ])

    rows.append([
        "✅ Проверить подписку",
        "check_sub",
        "success",
    ])

    return make_kb(rows)


class SubscribeMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)

        if not user:
            return await handler(event, data)

        if isinstance(event, Message) and event.chat.type != "private":
            return await handler(event, data)

        touch_user(user)

        if (
            is_admin(user.id)
            or is_whitelisted(user.id)
            or not setting("required_channel")
        ):
            return await handler(event, data)

        if (
            isinstance(event, CallbackQuery)
            and event.data == "check_sub"
        ):
            return await handler(event, data)

        if await user_has_channel(user.id):
            return await handler(event, data)

        text = (
            "🔒 <b>Обязательная подписка</b>\n\n"
            "Чтобы пользоваться ботом, подпишитесь на наш канал, "
            "затем нажмите «✅ Проверить подписку».\n\n"
            "🎮 Дополнительно можно запустить @GameBRxocsShop_Bot. "
            "Это отдельный бот; его запуск не заменяет подписку."
        )

        if isinstance(event, CallbackQuery):
            await event.answer()
            if event.message:
                await event.message.answer(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=subscription_kb(),
                )
        else:
            await event.answer(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=subscription_kb(),
            )

        return None


dp.message.outer_middleware(SubscribeMiddleware())
dp.callback_query.outer_middleware(SubscribeMiddleware())


# ============================================================
# СТАРТ / ОБЩЕЕ МЕНЮ
# ============================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    touch_user(message.from_user)

    await message.answer(
        setting("welcome"),
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(message.from_user.id),
        disable_web_page_preview=True,
    )


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✅ Действие отменено.",
        reply_markup=main_kb(message.from_user.id),
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа.")
        return

    await state.clear()
    count = db_one(
        "SELECT COUNT(*) AS c FROM orders "
        "WHERE status IN ('waiting_admin','issuing')"
    )["c"]

    await message.answer(
        f"⚙️ <b>Админ-панель</b>\n\n"
        f"📥 Активных заявок: <b>{count}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_kb(),
    )


@dp.callback_query(F.data == "menu")
async def menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    touch_user(callback.from_user)

    await safe_edit(
        callback,
        setting("welcome"),
        main_kb(callback.from_user.id),
    )
    await callback.answer()


# ============================================================
# ПОКУПКА / ПРОМОКОД
# ============================================================

@dp.callback_query(F.data == "buy")
async def buy(callback: CallbackQuery, state: FSMContext):
    await state.clear()

    await safe_edit(
        callback,
        "🛒 <b>Выберите тариф</b>",
        tariffs_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("tariff:"))
async def choose_tariff(callback: CallbackQuery, state: FSMContext):
    tariff_id = callback.data.split(":", 1)[1]
    tariff = get_tariff(tariff_id)

    if not tariff or not tariff["enabled"]:
        await callback.answer("Тариф недоступен.", show_alert=True)
        return

    await state.clear()
    await state.update_data(
        tariff_id=tariff_id,
        tariff_name=tariff["name"],
        base_price=int(tariff["price"]),
        days=int(tariff["days"]),
        promo_code="",
        discount=0,
    )

    description = tariff["description"] or "Без дополнительного описания."
    text = (
        f"🛒 <b>{esc(tariff['name'])}</b>\n\n"
        f"{description}\n\n"
        f"📅 Срок: <b>{tariff['days']} дней</b>\n\n"
        f"{format_payment(tariff['name'], int(tariff['price']), int(tariff['days']))}"
    )

    await safe_edit(callback, text, payment_kb())
    await callback.answer()


def check_promo(code, user_id, base_price):
    row = db_one(
        "SELECT * FROM promocodes WHERE code=?",
        (code.upper(),),
    )

    if not row or not row["enabled"]:
        return False, 0, "Промокод не найден или отключён"

    statuses = ("waiting_admin", "issuing", "completed")
    marks = ",".join("?" for _ in statuses)

    total = db_one(
        f"""
        SELECT COUNT(*) AS c FROM orders
        WHERE promo_code=? AND status IN ({marks})
        """,
        (row["code"], *statuses),
    )["c"]

    if int(total) >= int(row["max_uses"]):
        return False, 0, "Лимит использований промокода исчерпан"

    personal = db_one(
        f"""
        SELECT COUNT(*) AS c FROM orders
        WHERE promo_code=? AND user_id=? AND status IN ({marks})
        """,
        (row["code"], user_id, *statuses),
    )["c"]

    if int(personal) >= int(row["per_user"]):
        return False, 0, "Вы уже использовали этот промокод"

    final_price = max(
        0,
        (base_price * (100 - int(row["discount"])) + 50) // 100,
    )
    return True, base_price - final_price, ""


@dp.callback_query(F.data == "promo:enter")
async def promo_enter(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    if not data.get("tariff_id"):
        await callback.answer("Сначала выберите тариф.", show_alert=True)
        return

    await state.set_state(UserStates.waiting_promo)
    await callback.message.answer(
        "🎟 Отправьте промокод одним сообщением. Для отмены — /cancel."
    )
    await callback.answer()


@dp.message(UserStates.waiting_promo)
async def promo_apply(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()

    if (
        not code
        or len(code) > 32
        or not all(
            c.isascii() and (c.isalnum() or c in "_-")
            for c in code
        )
    ):
        await message.answer(
            "❌ Используйте латинские буквы, цифры, _ или - "
            "(до 32 символов)."
        )
        return

    data = await state.get_data()

    if not data.get("tariff_id") or "base_price" not in data:
        await state.clear()
        await message.answer("Покупка устарела. Начните заново: /start")
        return

    valid, discount, error = check_promo(
        code,
        message.from_user.id,
        int(data["base_price"]),
    )

    if not valid:
        await message.answer(
            f"❌ {error}. Попробуйте другой промокод или /cancel."
        )
        return

    await state.update_data(promo_code=code, discount=discount)
    await state.set_state(None)

    final_price = max(0, int(data["base_price"]) - discount)
    text = (
        f"✅ Промокод <code>{esc(code)}</code> применён!\n\n"
        f"📦 {esc(data['tariff_name'])}\n"
        f"💰 Было: <s>{data['base_price']} ₽</s>\n"
        f"🎁 Скидка: <b>{discount} ₽</b>\n"
        f"✅ <b>К оплате: {final_price} ₽</b>\n\n"
        f"{format_payment(data['tariff_name'], final_price, int(data['days']))}"
    )

    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=payment_kb(),
    )


@dp.callback_query(F.data == "paid")
async def paid(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    if not data.get("tariff_id"):
        await callback.answer(
            "Сначала выберите тариф.",
            show_alert=True,
        )
        return

    code = data.get("promo_code") or ""

    if code:
        valid, discount, error = check_promo(
            code,
            callback.from_user.id,
            int(data["base_price"]),
        )
        if not valid:
            await callback.answer(
                f"Промокод больше недоступен: {error}.",
                show_alert=True,
            )
            return
        await state.update_data(discount=discount)

    await state.set_state(UserStates.waiting_receipt)
    await callback.message.answer(
        "🧾 <b>Отправьте чек об оплате.</b>\n\n"
        "Можно прислать фотографию или документ.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(UserStates.waiting_receipt, F.photo)
async def receive_photo(message: Message, state: FSMContext):
    await create_order(
        message,
        state,
        "photo",
        message.photo[-1].file_id,
    )


@dp.message(UserStates.waiting_receipt, F.document)
async def receive_document(message: Message, state: FSMContext):
    await create_order(
        message,
        state,
        "document",
        message.document.file_id,
    )


async def create_order(message, state, receipt_type, file_id):
    touch_user(message.from_user)
    data = await state.get_data()

    if not data.get("tariff_id") or "base_price" not in data:
        await state.clear()
        await message.answer(
            "❌ Покупка устарела. Начните заново: /start"
        )
        return

    code = (data.get("promo_code") or "").upper()
    base_price = int(data["base_price"])
    discount = int(data.get("discount", 0))

    if code:
        valid, discount, error = check_promo(
            code,
            message.from_user.id,
            base_price,
        )
        if not valid:
            await message.answer(
                f"❌ {error}. Начните покупку заново."
            )
            await state.clear()
            return

    final_price = max(0, base_price - discount)

    cur = db_execute("""
        INSERT INTO orders(
            user_id,username,tariff,price,days,status,receipt_file_id,
            created_at,receipt_type,promo_code,discount
        ) VALUES(?,?,?,?,?,'waiting_admin',?,?,?,?,?)
    """, (
        message.from_user.id,
        message.from_user.username or "",
        data["tariff_name"],
        final_price,
        int(data["days"]),
        file_id,
        iso(now_utc()),
        receipt_type,
        code or None,
        discount,
    ))

    order_id = cur.lastrowid
    await state.clear()

    await message.answer(
        f"✅ <b>Заявка #{order_id} создана!</b>\n\n"
        "Чек отправлен администратору. После проверки оплаты "
        "вам выдадут ключ.",
        parse_mode=ParseMode.HTML,
    )

    username = (
        f"@{esc(message.from_user.username)}"
        if message.from_user.username
        else "без username"
    )

    caption = (
        f"🆕 <b>НОВАЯ ЗАЯВКА #{order_id}</b>\n\n"
        f"👤 {esc(message.from_user.full_name)}\n"
        f"🔗 {username}\n"
        f"🆔 <code>{message.from_user.id}</code>\n\n"
        f"📦 {esc(data['tariff_name'])}\n"
        f"💰 <b>{final_price} ₽</b>\n"
        f"🎁 Скидка: {discount} ₽\n"
        f"🎟 Промокод: {esc(code or 'нет')}\n"
        f"📅 {int(data['days'])} дней\n\n"
        "Проверьте поступление оплаты перед выдачей ключа."
    )

    try:
        if receipt_type == "photo":
            await bot.send_photo(
                ADMIN_ID,
                file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=order_kb(order_id),
            )
        else:
            await bot.send_document(
                ADMIN_ID,
                file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=order_kb(order_id),
            )
    except Exception:
        logger.exception("Не получилось отправить чек админу")

        try:
            await bot.send_message(
                ADMIN_ID,
                caption,
                parse_mode=ParseMode.HTML,
                reply_markup=order_kb(order_id),
            )
            if receipt_type == "photo":
                await bot.send_photo(ADMIN_ID, file_id)
            else:
                await bot.send_document(ADMIN_ID, file_id)
        except Exception:
            logger.exception(
                "Не получилось отправить уведомление по заказу %s",
                order_id,
            )
            await message.answer(
                "⚠️ Заявка сохранена, но уведомление админу "
                "не доставлено. Напишите в поддержку."
            )


@dp.message(UserStates.waiting_receipt)
async def wrong_receipt(message: Message):
    await message.answer("❌ Отправьте фото или файл с чеком.")


# ============================================================
# МОЯ ПОДПИСКА / HAPP / ПОДДЕРЖКА
# ============================================================

@dp.callback_query(F.data == "subscription")
async def subscription(callback: CallbackQuery):
    row = db_one("""
        SELECT * FROM orders
        WHERE user_id=? AND status='completed'
        ORDER BY expires_at DESC,id DESC LIMIT 1
    """, (callback.from_user.id,))

    if not row:
        await safe_edit(
            callback,
            "🔐 <b>Моя подписка</b>\n\n"
            "У вас пока нет выданных VPN-ключей.",
            make_kb([
                ["🛒 Купить VPN", "buy", "success"],
                ["⬅️ Назад", "menu"],
            ]),
        )
        await callback.answer()
        return

    expires = parse_dt(row["expires_at"])
    active = bool(expires and expires > now_utc())

    text = (
        "🔐 <b>Моя подписка</b>\n\n"
        f"Статус: <b>{'🟢 Активна' if active else '🔴 Срок истёк'}</b>\n"
        f"📦 Тариф: <b>{esc(row['tariff'])}</b>\n"
        f"📅 До: <b>{local_date(row['expires_at'])}</b>\n"
        f"🧾 Заказ: <b>#{row['id']}</b>\n\n"
        f"🔑 Ключ:\n<code>{esc(row['vpn_key'] or '—')}</code>"
    )

    await safe_edit(
        callback,
        text,
        make_kb([
            ["🍎 HAPP для iPhone", None, HAPP_IOS, "primary"],
            ["🤖 HAPP для Android", None, HAPP_ANDROID, "success"],
            ["📖 Инструкция", "instruction"],
            ["🛒 Купить ещё", "buy", "success"],
            ["⬅️ Назад", "menu"],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data == "instruction")
async def instruction(callback: CallbackQuery):
    text = (
        setting("instruction")
        + "\n\n📌 <b>Если ключ не добавляется</b>\n"
        "• Скопируйте ключ полностью.\n"
        "• Убедитесь, что ключ совместим с HAPP.\n"
        "• Обновите приложение.\n"
        "• При необходимости обратитесь в поддержку."
    )
    await safe_edit(callback, text, happ_kb())
    await callback.answer()


@dp.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    await safe_edit(
        callback,
        setting("support"),
        back_kb("menu"),
    )
    await callback.answer()


# ============================================================
# АДМИН-ПАНЕЛЬ / СТАТИСТИКА / ЗАЯВКИ
# ============================================================

@dp.callback_query(F.data == "admin")
async def admin_home(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    count = db_one(
        "SELECT COUNT(*) AS c FROM orders "
        "WHERE status IN ('waiting_admin','issuing')"
    )["c"]

    await safe_edit(
        callback,
        f"⚙️ <b>Админ-панель</b>\n\n"
        f"📥 Заявок в работе: <b>{count}</b>",
        admin_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    users = db_one("SELECT COUNT(*) AS c FROM users")["c"]
    total = db_one("SELECT COUNT(*) AS c FROM orders")["c"]
    pending = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE status='waiting_admin'"
    )["c"]
    issuing = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE status='issuing'"
    )["c"]
    completed = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE status='completed'"
    )["c"]
    rejected = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE status='rejected'"
    )["c"]
    revenue = db_one(
        "SELECT COALESCE(SUM(price),0) AS s FROM orders "
        "WHERE status='completed'"
    )["s"]
    active = db_one("""
        SELECT COUNT(DISTINCT user_id) AS c FROM orders
        WHERE status='completed' AND expires_at>?
    """, (iso(now_utc()),))["c"]
    white = db_one("SELECT COUNT(*) AS c FROM whitelist")["c"]

    midnight = (
        now_utc()
        .astimezone(LOCAL_TZ)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )

    today_orders = db_one(
        "SELECT COUNT(*) AS c FROM orders WHERE created_at>=?",
        (iso(midnight),),
    )["c"]

    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"📦 Заказов: <b>{total}</b>\n"
        f"📥 Ожидают: <b>{pending}</b>\n"
        f"🔑 В процессе выдачи: <b>{issuing}</b>\n"
        f"✅ Выдано: <b>{completed}</b>\n"
        f"❌ Отклонено: <b>{rejected}</b>\n"
        f"💰 Выручка: <b>{revenue} ₽</b>\n"
        f"🔐 Активных по сроку: <b>{active}</b>\n"
        f"🤍 Белый список: <b>{white}</b>\n"
        f"📅 Заявок сегодня: <b>{today_orders}</b>"
    )

    await safe_edit(callback, text, back_kb("admin"))
    await callback.answer()


@dp.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    rows = db_all("SELECT * FROM users ORDER BY last_seen DESC LIMIT 20")
    parts = ["👥 <b>Последние пользователи</b>\n"]

    for row in rows:
        name = (
            "@" + esc(row["username"])
            if row["username"]
            else esc(row["first_name"] or row["user_id"])
        )
        flags = (
            (" 🤍" if is_whitelisted(row["user_id"]) else "")
            + (" 🚫" if row["blocked"] else "")
        )

        parts.append(
            f"\n{name}{flags}\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"Покупок: {row['orders_count']} · "
            f"Потрачено: {row['total_spent']:.0f} ₽"
        )

    if not rows:
        parts.append("Пока нет пользователей.")

    await safe_edit(callback, "\n".join(parts), back_kb("admin"))
    await callback.answer()


@dp.callback_query(F.data == "admin:orders")
async def admin_orders(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    rows = db_all("""
        SELECT * FROM orders
        WHERE status IN ('waiting_admin','issuing')
        ORDER BY id DESC LIMIT 20
    """)

    if not rows:
        await safe_edit(
            callback,
            "📥 <b>Новые заявки</b>\n\nЗаявок в работе нет.",
            back_kb("admin"),
        )
        await callback.answer()
        return

    parts = ["📥 <b>Заявки в работе</b>\n"]
    buttons = []

    for row in rows:
        icon = "🔑" if row["status"] == "issuing" else "⏳"
        parts.append(
            f"\n{icon} <b>#{row['id']}</b> · "
            f"{esc(row['username'] or row['user_id'])} · "
            f"{esc(row['tariff'])} · {row['price']} ₽"
        )
        buttons.append([[
            f"Открыть #{row['id']}",
            f"order:view:{row['id']}",
            "primary",
        ]])

    buttons.append([["⬅️ Назад", "admin"]])
    await safe_edit(callback, "\n".join(parts), make_kb(buttons))
    await callback.answer()


@dp.callback_query(F.data == "admin:history")
async def admin_history(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    rows = db_all("SELECT * FROM orders ORDER BY id DESC LIMIT 25")

    if not rows:
        await safe_edit(
            callback,
            "📦 <b>История заказов</b>\n\nЗаказов нет.",
            back_kb("admin"),
        )
        await callback.answer()
        return

    icons = {
        "completed": "✅",
        "waiting_admin": "⏳",
        "issuing": "🔑",
        "rejected": "❌",
    }

    parts = ["📦 <b>Последние заказы</b>\n"]
    buttons = []

    for row in rows:
        parts.append(
            f"\n{icons.get(row['status'], '•')} "
            f"<b>#{row['id']}</b> · {esc(row['tariff'])} · "
            f"{row['price']} ₽"
        )
        buttons.append([[
            f"Открыть #{row['id']}",
            f"order:view:{row['id']}",
        ]])

    buttons.append([["⬅️ Назад", "admin"]])
    await safe_edit(callback, "\n".join(parts), make_kb(buttons))
    await callback.answer()


@dp.callback_query(F.data.startswith("order:view:"))
async def order_view(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный номер заказа.", show_alert=True)
        return

    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))

    if not row:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    text = (
        f"📦 <b>Заказ #{row['id']}</b>\n\n"
        f"👤 {esc(row['username'] or 'без username')}\n"
        f"🆔 <code>{row['user_id']}</code>\n"
        f"📦 {esc(row['tariff'])}\n"
        f"💰 <b>{row['price']} ₽</b>\n"
        f"🎁 Скидка: {row['discount']} ₽\n"
        f"🎟 Промокод: {esc(row['promo_code'] or '—')}\n"
        f"📅 {row['days']} дней\n"
        f"📌 Статус: <b>{esc(row['status'])}</b>\n"
        f"🕒 Создан: {local_date(row['created_at'])}\n"
        f"📆 Истекает: {local_date(row['expires_at'])}"
    )

    buttons = []

    if row["status"] in {"waiting_admin", "issuing"} and not row["vpn_key"]:
        buttons.extend([
            ["🔑 Выдать ключ", f"give:{order_id}", "success"],
            ["❌ Отклонить", f"reject:{order_id}", "danger"],
        ])

    if row["status"] in {"issuing", "completed"} and row["vpn_key"]:
        buttons.append([
            "🔁 Отправить сохранённый ключ",
            f"resend:{order_id}",
            "primary",
        ])

    destination = (
        "admin:orders"
        if row["status"] in {"waiting_admin", "issuing"}
        else "admin:history"
    )
    buttons.append(["⬅️ Назад", destination])

    await safe_edit(callback, text, make_kb(buttons))
    await callback.answer()


# ============================================================
# ВЫДАЧА / ОТКЛОНЕНИЕ / ПОВТОРНАЯ ОТПРАВКА
# ============================================================

def next_expiry(user_id, days):
    current = now_utc()

    row = db_one("""
        SELECT expires_at FROM orders
        WHERE user_id=? AND status='completed'
          AND expires_at IS NOT NULL AND expires_at>?
        ORDER BY expires_at DESC LIMIT 1
    """, (user_id, iso(current)))

    previous = parse_dt(row["expires_at"]) if row else None
    start = previous if previous and previous > current else current

    return start + timedelta(days=days)


def mark_completed(order_id):
    row = db_one(
        "SELECT user_id,price,status FROM orders WHERE id=?",
        (order_id,),
    )

    if not row or row["status"] == "completed":
        return

    db_execute(
        "UPDATE orders SET status='completed' WHERE id=?",
        (order_id,),
    )

    db_execute("""
        UPDATE users
        SET orders_count=orders_count+1,
            total_spent=total_spent+?,
            blocked=0
        WHERE user_id=?
    """, (int(row["price"]), row["user_id"]))


@dp.callback_query(F.data.startswith("give:"))
async def give_key(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    try:
        order_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный номер.", show_alert=True)
        return

    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))

    if not row or row["status"] not in {"waiting_admin", "issuing"}:
        await callback.answer(
            "Заявка уже обработана или не найдена.",
            show_alert=True,
        )
        return

    db_execute(
        "UPDATE orders SET status='issuing' WHERE id=?",
        (order_id,),
    )

    await state.set_state(AdminStates.waiting_key)
    await state.update_data(order_id=order_id)
    await callback.message.answer(
        f"🔑 <b>Выдача ключа для заказа #{order_id}</b>\n\n"
        "Отправьте ключ одним текстовым сообщением.\n"
        "Для отмены — /cancel.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


async def deliver_order_key(order, key, existing=False):
    was_completed = order["status"] == "completed"

    if existing and order["expires_at"]:
        expires = order["expires_at"]
    else:
        expires = iso(next_expiry(order["user_id"], int(order["days"])))

    # Повторная отправка готового заказа не должна увеличивать статистику.
    new_status = "completed" if was_completed else "issuing"
    issued_at = (
        order["issued_at"]
        if existing and order["issued_at"]
        else iso(now_utc())
    )

    db_execute("""
        UPDATE orders
        SET status=?,vpn_key=?,issued_at=?,expires_at=?
        WHERE id=?
    """, (new_status, key, issued_at, expires, order["id"]))

    fresh = db_one("SELECT * FROM orders WHERE id=?", (order["id"],))
    text = format_delivery(fresh, key, local_date(expires))

    try:
        await bot.send_message(
            fresh["user_id"],
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=happ_kb(),
            disable_web_page_preview=True,
        )
    except TelegramForbiddenError:
        db_execute(
            "UPDATE users SET blocked=1 WHERE user_id=?",
            (fresh["user_id"],),
        )
        return False
    except Exception:
        logger.exception(
            "Не удалось доставить ключ заказа #%s",
            order["id"],
        )
        return False

    if not was_completed:
        mark_completed(int(order["id"]))

    return True


@dp.message(AdminStates.waiting_key)
async def receive_key(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    key = (message.text or "").strip()

    if not key:
        await message.answer(
            "❌ Отправьте ключ текстом. Для отмены — /cancel."
        )
        return

    data = await state.get_data()
    order_id = data.get("order_id")
    row = (
        db_one("SELECT * FROM orders WHERE id=?", (order_id,))
        if order_id else None
    )

    if not row or row["status"] not in {"waiting_admin", "issuing"}:
        await state.clear()
        await message.answer("❌ Заявка не найдена или уже обработана.")
        return

    delivered = await deliver_order_key(row, key)
    await state.clear()

    if delivered:
        expiry = db_one(
            "SELECT expires_at FROM orders WHERE id=?",
            (order_id,),
        )["expires_at"]

        await message.answer(
            f"✅ <b>Ключ выдан!</b>\n"
            f"Заказ: #{order_id}\n"
            f"Действует до: {local_date(expiry)}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            f"⚠️ Ключ сохранён для заказа #{order_id}, "
            "но отправка не удалась. Повторите отправку через историю заказов."
        )


@dp.callback_query(F.data.startswith("reject:"))
async def reject_order(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    try:
        order_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный номер.", show_alert=True)
        return

    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))

    if (
        not row
        or row["status"] not in {"waiting_admin", "issuing"}
        or row["vpn_key"]
    ):
        await callback.answer(
            "Нельзя отклонить эту заявку.",
            show_alert=True,
        )
        return

    db_execute(
        "UPDATE orders SET status='rejected' WHERE id=?",
        (order_id,),
    )

    try:
        await bot.send_message(
            row["user_id"],
            f"❌ Заявка #{order_id} отклонена. "
            "Если вы уже оплатили, напишите в поддержку.",
        )
    except Exception:
        pass

    await safe_edit(
        callback,
        f"❌ Заявка #{order_id} отклонена.",
        back_kb("admin:orders"),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("resend:"))
async def resend_key(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    try:
        order_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный номер.", show_alert=True)
        return

    row = db_one("SELECT * FROM orders WHERE id=?", (order_id,))

    if (
        not row
        or row["status"] not in {"issuing", "completed"}
        or not row["vpn_key"]
    ):
        await callback.answer(
            "Сохранённый ключ не найден.",
            show_alert=True,
        )
        return

    ok = await deliver_order_key(row, row["vpn_key"], existing=True)

    await callback.answer(
        "✅ Ключ отправлен." if ok else "❌ Не удалось отправить ключ.",
        show_alert=not ok,
    )


# ============================================================
# ТАРИФЫ
# ============================================================

@dp.callback_query(F.data == "admin:tariffs")
async def admin_tariffs(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    rows = []
    text = (
        "💰 <b>Тарифы и цены</b>\n\n"
        "Нажмите на тариф, чтобы редактировать его.\n"
    )

    for tariff in get_tariffs(False):
        text += (
            f"\n{'🟢' if tariff['enabled'] else '🔴'} "
            f"{esc(tariff['name'])}: {tariff['price']} ₽ / "
            f"{tariff['days']} дн."
        )
        rows.append([
            f"{'🟢' if tariff['enabled'] else '🔴'} {tariff['name']}",
            f"edittariff:{tariff['tariff_id']}",
        ])

    rows.append(["➕ Добавить тариф", "addtariff", "success"])
    rows.append(["⬅️ Назад", "admin"])

    await safe_edit(callback, text, make_kb(rows))
    await callback.answer()


@dp.callback_query(F.data.startswith("edittariff:"))
async def edit_tariff_menu(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    tid = callback.data.split(":", 1)[1]
    tariff = get_tariff(tid)

    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    text = (
        f"💰 <b>{esc(tariff['name'])}</b>\n\n"
        f"ID: <code>{esc(tid)}</code>\n"
        f"Цена: {tariff['price']} ₽\n"
        f"Срок: {tariff['days']} дней\n"
        f"Состояние: {'включён' if tariff['enabled'] else 'выключен'}\n\n"
        f"Описание:\n{tariff['description'] or '—'}"
    )

    await safe_edit(callback, text, make_kb([
        ["💰 Изменить цену", f"price:{tid}", "success"],
        ["📝 Изменить название", f"name:{tid}"],
        ["📅 Изменить срок", f"days:{tid}"],
        ["📖 Изменить описание", f"desc:{tid}"],
        ["🔄 Включить / выключить", f"toggle:{tid}"],
        ["⬅️ Назад", "admin:tariffs"],
    ]))
    await callback.answer()


@dp.callback_query(F.data.startswith("price:"))
async def price_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    tid = callback.data.split(":", 1)[1]

    if not get_tariff(tid):
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_price)
    await state.update_data(tariff_id=tid)
    await callback.message.answer(
        "💰 Отправьте новую цену (целое число от 0 и выше). "
        "Для отмены — /cancel."
    )
    await callback.answer()


@dp.message(AdminStates.waiting_price)
async def price_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    try:
        price = int((message.text or "").strip())
        if not 0 <= price <= 100000000:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число от 0 до 100000000.")
        return

    data = await state.get_data()
    db_execute(
        "UPDATE tariffs SET price=? WHERE tariff_id=?",
        (price, data["tariff_id"]),
    )
    await state.clear()

    await message.answer(
        f"✅ Новая цена: <b>{price} ₽</b>",
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("name:"))
async def name_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    tid = callback.data.split(":", 1)[1]

    if not get_tariff(tid):
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_name)
    await state.update_data(tariff_id=tid)
    await callback.message.answer(
        "📝 Отправьте новое название тарифа (до 80 символов)."
    )
    await callback.answer()


@dp.message(AdminStates.waiting_name)
async def name_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    name = (message.text or "").strip()

    if not name or len(name) > 80:
        await message.answer("❌ Название должно быть от 1 до 80 символов.")
        return

    data = await state.get_data()
    db_execute(
        "UPDATE tariffs SET name=? WHERE tariff_id=?",
        (name, data["tariff_id"]),
    )
    await state.clear()
    await message.answer("✅ Название тарифа обновлено.")


@dp.callback_query(F.data.startswith("days:"))
async def days_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    tid = callback.data.split(":", 1)[1]

    if not get_tariff(tid):
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_days)
    await state.update_data(tariff_id=tid)
    await callback.message.answer("📅 Отправьте новый срок в днях (1–36500).")
    await callback.answer()


@dp.message(AdminStates.waiting_days)
async def days_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    try:
        days = int((message.text or "").strip())
        if not 1 <= days <= 36500:
            raise ValueError
    except ValueError:
        await message.answer("❌ Срок должен быть от 1 до 36500 дней.")
        return

    data = await state.get_data()
    db_execute(
        "UPDATE tariffs SET days=? WHERE tariff_id=?",
        (days, data["tariff_id"]),
    )
    await state.clear()
    await message.answer(f"✅ Срок изменён: {days} дней.")


@dp.callback_query(F.data.startswith("desc:"))
async def desc_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    tid = callback.data.split(":", 1)[1]

    if not get_tariff(tid):
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_description)
    await state.update_data(tariff_id=tid)
    await callback.message.answer(
        "📖 Отправьте описание тарифа. Форматирование сохранится."
    )
    await callback.answer()


@dp.message(AdminStates.waiting_description)
async def desc_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    value = message.html_text if message.text else ""

    if not value.strip():
        await message.answer("❌ Отправьте текстовое описание.")
        return

    data = await state.get_data()
    db_execute(
        "UPDATE tariffs SET description=? WHERE tariff_id=?",
        (value, data["tariff_id"]),
    )
    await state.clear()
    await message.answer("✅ Описание сохранено вместе с форматированием.")


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_tariff(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    tid = callback.data.split(":", 1)[1]
    tariff = get_tariff(tid)

    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    db_execute(
        "UPDATE tariffs SET enabled=? WHERE tariff_id=?",
        (0 if tariff["enabled"] else 1, tid),
    )
    await edit_tariff_menu(callback)


@dp.callback_query(F.data == "addtariff")
async def add_tariff_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    await state.set_state(AdminStates.waiting_add_tariff)
    await callback.message.answer(
        "➕ Отправьте 4 строки:\n"
        "<code>id\nназвание\nцена\nдни</code>\n\n"
        "Пример:\n"
        "<code>vpn_180\nVPN на 180 дней\n799\n180</code>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_add_tariff)
async def add_tariff_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    lines = [
        line.strip()
        for line in (message.text or "").splitlines()
        if line.strip()
    ]

    if len(lines) != 4:
        await message.answer("❌ Нужно ровно 4 строки: ID, название, цена, срок.")
        return

    tid, name, raw_price, raw_days = lines

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", tid):
        await message.answer("❌ ID: латинские буквы, цифры, _ или -, до 24 символов.")
        return

    try:
        price = int(raw_price)
        days = int(raw_days)
        if not 0 <= price <= 100000000 or not 1 <= days <= 36500:
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверная цена или срок.")
        return

    if get_tariff(tid):
        await message.answer("❌ Такой ID уже существует.")
        return

    sort_order = db_one(
        "SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM tariffs"
    )["n"]

    db_execute("""
        INSERT INTO tariffs
            (tariff_id,name,price,days,description,enabled,sort_order)
        VALUES (?,?,?,?,?,1,?)
    """, (tid, name[:80], price, days, "", sort_order))

    await state.clear()
    await message.answer("✅ Тариф создан. Его можно редактировать в разделе тарифов.")


# ============================================================
# РЕДАКТОР ТЕКСТОВ
# ============================================================

@dp.callback_query(F.data == "admin:texts")
async def admin_texts(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    await safe_edit(
        callback,
        "📝 <b>Редактор текстов</b>\n\n"
        "Выберите сообщение и пришлите новую версию. "
        "Форматирование Telegram сохраняется.",
        text_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edittext:"))
async def edit_text_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    key = callback.data.split(":", 1)[1]
    allowed = {
        "welcome", "payment", "instruction",
        "delivery", "support", "whitelist_info",
    }

    if key not in allowed:
        await callback.answer("Неизвестный текст.", show_alert=True)
        return

    hint = ""

    if key == "payment":
        hint = (
            "\nПеременные: <code>{price}</code>, "
            "<code>{tariff}</code>, <code>{days}</code>."
        )
    elif key == "delivery":
        hint = (
            "\nПеременные: <code>{tariff}</code>, "
            "<code>{days}</code>, <code>{key}</code>, "
            "<code>{expires}</code>, <code>{order_id}</code>."
        )

    await state.set_state(AdminStates.waiting_edit_text)
    await state.update_data(edit_key=key)
    await callback.message.answer(
        "📝 Отправьте новый текст. Форматирование сохранится. "
        "Для отмены /cancel." + hint,
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_edit_text)
async def edit_text_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    value = message.html_text if message.text else ""

    if not value.strip():
        await message.answer("❌ Отправьте непустой текст.")
        return

    data = await state.get_data()
    key = data.get("edit_key")

    if key not in {
        "welcome", "payment", "instruction",
        "delivery", "support", "whitelist_info",
    }:
        await state.clear()
        await message.answer("❌ Раздел не найден.")
        return

    set_setting(key, value)
    await state.clear()
    await message.answer("✅ Текст сохранён вместе с форматированием Telegram.")


# ============================================================
# БЕЛЫЙ СПИСОК
# ============================================================

@dp.callback_query(F.data == "admin:whitelist")
async def whitelist_admin(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    rows = db_all(
        "SELECT * FROM whitelist ORDER BY added_at DESC LIMIT 30"
    )

    parts = [
        setting("whitelist_info"),
        f"\n\n👥 В списке: <b>{len(rows)}</b>",
    ]
    buttons = [["➕ Добавить пользователя", "white:add", "success"]]

    for row in rows:
        suffix = f" — {esc(row['note'])}" if row["note"] else ""
        parts.append(f"\n🤍 <code>{row['user_id']}</code>{suffix}")
        buttons.append([[
            f"❌ Удалить {row['user_id']}",
            f"white:del:{row['user_id']}",
            "danger",
        ]])

    buttons.append([["⬅️ Назад", "admin"]])
    await safe_edit(callback, "\n".join(parts), make_kb(buttons))
    await callback.answer()


@dp.callback_query(F.data == "white:add")
async def white_add_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    await state.set_state(AdminStates.waiting_whitelist_user)
    await callback.message.answer(
        "🤍 Отправьте числовой Telegram ID пользователя. Для отмены /cancel."
    )
    await callback.answer()


@dp.message(AdminStates.waiting_whitelist_user)
async def white_add_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    try:
        uid = int((message.text or "").strip())
        if uid <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Нужен положительный числовой Telegram ID.")
        return

    db_execute(
        "INSERT OR REPLACE INTO whitelist(user_id,note,added_at) VALUES(?,?,?)",
        (uid, "", iso(now_utc())),
    )

    await state.clear()
    await message.answer(
        f"✅ Пользователь <code>{uid}</code> добавлен в белый список.",
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("white:del:"))
async def white_delete(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    try:
        uid = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Неверный ID.", show_alert=True)
        return

    db_execute("DELETE FROM whitelist WHERE user_id=?", (uid,))
    await whitelist_admin(callback)


# ============================================================
# ПРОМОКОДЫ
# ============================================================

@dp.callback_query(F.data == "admin:promos")
async def promos_admin(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    promos = db_all(
        "SELECT * FROM promocodes ORDER BY created_at DESC LIMIT 30"
    )
    parts = ["🎟 <b>Промокоды</b>\nСкидка указана в процентах.\n"]
    buttons = [["➕ Создать промокод", "promo:add", "success"]]

    for promo in promos:
        used = db_one("""
            SELECT COUNT(*) AS c FROM orders
            WHERE promo_code=?
              AND status IN ('waiting_admin','issuing','completed')
        """, (promo["code"],))["c"]

        parts.append(
            f"\n<code>{esc(promo['code'])}</code> — "
            f"{promo['discount']}% · {used}/{promo['max_uses']} · "
            f"на пользователя: {promo['per_user']} · "
            f"{'🟢' if promo['enabled'] else '🔴'}"
        )
        buttons.append([[
            f"{'⛔ Выключить' if promo['enabled'] else '✅ Включить'} "
            f"{promo['code']}",
            f"promo:toggle:{promo['code']}",
        ]])

    if not promos:
        parts.append("Промокодов пока нет.")

    buttons.append([["⬅️ Назад", "admin"]])
    await safe_edit(callback, "\n".join(parts), make_kb(buttons))
    await callback.answer()


@dp.callback_query(F.data == "promo:add")
async def promo_add_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    await state.set_state(AdminStates.waiting_add_promo)
    await callback.message.answer(
        "🎟 Отправьте четыре значения через пробел:\n"
        "<code>SALE20 20 100 1</code>\n\n"
        "Код, скидка %, общий лимит, лимит на одного пользователя.\n"
        "Для отмены /cancel.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_add_promo)
async def promo_add_finish(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    items = (message.text or "").strip().upper().split()

    try:
        if len(items) != 4:
            raise ValueError

        code, raw_discount, raw_limit, raw_each = items
        discount = int(raw_discount)
        limit = int(raw_limit)
        each = int(raw_each)

        if (
            not re.fullmatch(r"[A-Z0-9_-]{1,32}", code)
            or not 1 <= discount <= 99
            or not 1 <= each <= limit <= 1000000
        ):
            raise ValueError
    except ValueError:
        await message.answer(
            "❌ Формат неверный. Пример: "
            "<code>SALE20 20 100 1</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if db_one("SELECT 1 FROM promocodes WHERE code=?", (code,)):
        await message.answer("❌ Такой промокод уже существует.")
        return

    db_execute("""
        INSERT INTO promocodes
            (code,discount,max_uses,per_user,enabled,created_at)
        VALUES (?,?,?,?,1,?)
    """, (code, discount, limit, each, iso(now_utc())))

    await state.clear()
    await message.answer(
        f"✅ Промокод <code>{esc(code)}</code> создан. "
        f"Скидка {discount}%, лимит {limit}.",
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(F.data.startswith("promo:toggle:"))
async def promo_toggle(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    code = callback.data.split(":", 2)[2]
    row = db_one(
        "SELECT enabled FROM promocodes WHERE code=?",
        (code,),
    )

    if not row:
        await callback.answer("Промокод не найден.", show_alert=True)
        return

    db_execute(
        "UPDATE promocodes SET enabled=? WHERE code=?",
        (0 if row["enabled"] else 1, code),
    )
    await promos_admin(callback)


# ============================================================
# ОБЯЗАТЕЛЬНАЯ ПОДПИСКА: СНАЧАЛА КАНАЛ, ЗАТЕМ ССЫЛКА
# ============================================================

@dp.callback_query(F.data == "admin:channel")
async def admin_channel(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    channel_id = setting("required_channel")
    channel_name = setting("required_channel_name")
    url = setting("required_channel_url")

    text = (
        "📢 <b>Обязательная подписка</b>\n\n"
        f"Состояние: <b>{'🟢 включена' if channel_id else '🔴 выключена'}</b>\n"
        f"Название: <b>{esc(channel_name or 'не задано')}</b>\n"
        f"ID: <code>{esc(channel_id or 'не задан')}</code>\n"
        f"Ссылка: {esc(url or 'не задана')}\n\n"
        "Для настройки бот должен быть администратором канала."
    )

    await safe_edit(callback, text, make_kb([
        ["⚙️ Установить канал пошагово", "channel:set", "primary"],
        ["🔴 Отключить ОП", "channel:off", "danger"],
        ["⬅️ Назад", "admin"],
    ]))
    await callback.answer()


@dp.callback_query(F.data == "channel:set")
async def channel_set(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    await state.set_state(AdminStates.waiting_channel_name)
    await callback.message.answer(
        "📢 <b>Настройка обязательной подписки — шаг 1 из 2</b>\n\n"
        "Отправьте @username канала (например <code>@my_channel</code>)\n"
        "или ID приватного канала (например <code>-1001234567890</code>).\n\n"
        "Обычного названия без username/ID недостаточно, чтобы Telegram нашёл канал.\n"
        "Для отмены: /cancel.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_channel_name)
async def channel_name_received(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    identifier = (message.text or "").strip()

    if identifier.startswith("@"):
        if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", identifier):
            await message.answer(
                "❌ Неверный username. Пример: @my_channel"
            )
            return
        channel_ref = identifier
    elif re.fullmatch(r"-100\d+", identifier):
        channel_ref = int(identifier)
    else:
        await message.answer(
            "❌ Отправьте @username или числовой ID вида -1001234567890."
        )
        return

    try:
        chat = await bot.get_chat(channel_ref)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)

        if status_value(member.status) not in {"administrator", "creator"}:
            await message.answer(
                "❌ Сначала добавьте бота администратором канала."
            )
            return
    except Exception as exc:
        logger.warning("Проверка канала не удалась: %s", exc)
        await message.answer(
            "❌ Не удалось найти канал или проверить права. "
            "Проверьте username/ID и права бота."
        )
        return

    await state.update_data(
        pending_channel_id=str(chat.id),
        pending_channel_name=chat.title or identifier,
        pending_channel_username=(
            identifier if identifier.startswith("@") else ""
        ),
    )

    await state.set_state(AdminStates.waiting_channel_link)
    await message.answer(
        f"✅ Канал найден: <b>{esc(chat.title or identifier)}</b>\n\n"
        "<b>Шаг 2 из 2.</b> Теперь отправьте ссылку на канал отдельным сообщением.\n"
        "Пример: <code>https://t.me/my_channel</code>\n"
        "Для приватного канала отправьте ссылку-приглашение.\n"
        "Для отмены: /cancel.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(AdminStates.waiting_channel_link)
async def channel_link_received(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    url = (message.text or "").strip()

    if not (
        url.startswith("https://t.me/")
        or url.startswith("https://telegram.me/")
    ):
        await message.answer(
            "❌ Ссылка должна начинаться с https://t.me/ "
            "или https://telegram.me/."
        )
        return

    data = await state.get_data()
    channel_id = data.get("pending_channel_id")
    channel_name = data.get("pending_channel_name", "")
    username = data.get("pending_channel_username", "")

    if not channel_id:
        await state.clear()
        await message.answer(
            "❌ Данные канала потеряны. Начните настройку заново."
        )
        return

    if username:
        remainder = url.split("://", 1)[1]
        first_path = remainder.split("/", 1)[1] if "/" in remainder else ""
        link_name = (
            first_path.split("/", 1)[0]
            .split("?", 1)[0]
            .split("#", 1)[0]
        )

        if link_name.casefold() != username[1:].casefold():
            await message.answer(
                f"❌ Ссылка не соответствует {esc(username)}. "
                f"Отправьте ссылку https://t.me/{esc(username[1:])}"
            )
            return

    set_setting("required_channel", str(channel_id))
    set_setting("required_channel_name", channel_name)
    set_setting("required_channel_url", url)

    await state.clear()
    await message.answer(
        "✅ <b>Обязательная подписка настроена!</b>\n\n"
        f"📢 Канал: <b>{esc(channel_name)}</b>\n"
        f"🆔 ID: <code>{esc(channel_id)}</code>\n"
        f"🔗 Ссылка: {esc(url)}\n\n"
        "Кнопка запуска @GameBRxocsShop_Bot остаётся дополнительной "
        "и не проверяется.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_kb(),
    )


@dp.callback_query(F.data == "channel:off")
async def channel_off(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    set_setting("required_channel", "")
    set_setting("required_channel_name", "")
    set_setting("required_channel_url", "")
    await admin_channel(callback)


@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery):
    if await user_has_channel(callback.from_user.id):
        await safe_edit(
            callback,
            "✅ Подписка подтверждена!",
            main_kb(callback.from_user.id),
        )
        await callback.answer("Подписка подтверждена.")
    else:
        await callback.answer(
            "❌ Подписка пока не найдена. Подпишитесь и проверьте снова.",
            show_alert=True,
        )


# ============================================================
# РАССЫЛКА С ФОРМАТИРОВАНИЕМ / МЕДИА
# ============================================================

@dp.callback_query(F.data == "admin:broadcast")
async def broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    await state.set_state(AdminStates.waiting_broadcast)
    await callback.message.answer(
        "📣 Отправьте текст, фото, видео или документ для рассылки. "
        "Форматирование сохранится. Для отмены /cancel."
    )
    await callback.answer()


@dp.message(AdminStates.waiting_broadcast)
async def broadcast_preview(message: Message, state: FSMContext):
    if not await admin_message_only(message):
        return

    await state.update_data(
        broadcast_chat_id=message.chat.id,
        broadcast_message_id=message.message_id,
    )

    preview_markup = make_kb([
        ["🚀 Отправить всем", "broadcast:send", "danger"],
        ["❌ Отмена", "broadcast:cancel"],
    ])

    try:
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=preview_markup,
        )
    except Exception as exc:
        logger.exception("Preview failed: %s", exc)
        await message.answer(
            "❌ Не удалось показать предпросмотр. Попробуйте другое сообщение."
        )
        return

    await message.answer(
        "Проверьте предпросмотр выше и нажмите кнопку отправки на его копии."
    )


@dp.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    await state.clear()
    await safe_edit(
        callback,
        "❌ Рассылка отменена.",
        back_kb("admin"),
    )
    await callback.answer()


@dp.callback_query(F.data == "broadcast:send")
async def broadcast_send(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return

    data = await state.get_data()
    source_chat = data.get("broadcast_chat_id")
    source_message = data.get("broadcast_message_id")

    if not source_chat or not source_message:
        await state.clear()
        await callback.answer(
            "Рассылка не найдена. Начните заново.",
            show_alert=True,
        )
        return

    users = db_all("SELECT user_id FROM users WHERE blocked=0")
    await callback.message.answer(
        f"📣 Начинаю рассылку на {len(users)} пользователей…"
    )

    sent = failed = 0

    for row in users:
        try:
            await bot.copy_message(
                chat_id=row["user_id"],
                from_chat_id=source_chat,
                message_id=source_message,
            )
            sent += 1
            await asyncio.sleep(0.05)

        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.3)

            try:
                await bot.copy_message(
                    chat_id=row["user_id"],
                    from_chat_id=source_chat,
                    message_id=source_message,
                )
                sent += 1
            except Exception:
                failed += 1

        except TelegramForbiddenError:
            db_execute(
                "UPDATE users SET blocked=1 WHERE user_id=?",
                (row["user_id"],),
            )
            failed += 1

        except Exception:
            failed += 1

    await state.clear()
    await callback.message.answer(
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"📨 Доставлено: <b>{sent}</b>\n"
        f"⚠️ Ошибок: <b>{failed}</b>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


# ============================================================
# НАСТРОЙКИ / РЕЙТИНГ ПОКУПАТЕЛЕЙ
# ============================================================

@dp.callback_query(F.data == "admin:settings")
async def admin_settings(callback: CallbackQuery):
    if not await admin_only(callback):
        return

    pending = db_one(
        "SELECT COUNT(*) AS c FROM orders "
        "WHERE status IN ('waiting_admin','issuing')"
    )["c"]

    await safe_edit(
        callback,
        "⚙️ <b>Настройки</b>\n\n"
        f"🤖 Название: <b>{esc(BOT_NAME)}</b>\n"
        f"🆔 Admin ID: <code>{ADMIN_ID}</code>\n"
        f"📥 Заявок в работе: <b>{pending}</b>\n"
        f"📢 ОП: <b>{'включена' if setting('required_channel') else 'выключена'}</b>",
        back_kb("admin"),
    )
    await callback.answer()


@dp.callback_query(F.data == "ranking")
async def ranking(callback: CallbackQuery):
    rows = db_all("""
        SELECT
            o.user_id,
            COALESCE(
                NULLIF(u.first_name,''),
                NULLIF(u.username,''),
                'Покупатель'
            ) AS name,
            COALESCE(u.username,'') AS username,
            COUNT(*) AS purchases,
            SUM(o.price) AS spent
        FROM orders o
        LEFT JOIN users u ON u.user_id=o.user_id
        WHERE o.status='completed'
        GROUP BY o.user_id
        ORDER BY purchases DESC,spent DESC
        LIMIT 10
    """)

    parts = [
        "🏆 <b>Топ покупателей</b>\n"
        "Рейтинг по количеству успешно выданных заказов.\n"
    ]

    for index, row in enumerate(rows, 1):
        name = (
            "@" + esc(row["username"])
            if row["username"]
            else esc(row["name"])
        )
        parts.append(
            f"{index}. {name[:40]} — <b>{row['purchases']}</b> покупок"
        )

    if not rows:
        parts.append("Пока подтверждённых покупок нет.")

    destination = (
        "admin"
        if is_admin(callback.from_user.id)
        else "menu"
    )

    await safe_edit(
        callback,
        "\n".join(parts),
        back_kb(destination),
    )
    await callback.answer()


# ============================================================
# НЕРАСПОЗНАННЫЕ СООБЩЕНИЯ / ЗАПУСК
# ============================================================

@dp.message()
async def fallback(message: Message):
    touch_user(message.from_user)
    await message.answer(
        "Выберите действие в меню 👇",
        reply_markup=main_kb(message.from_user.id),
    )


# Запуск напрямую — без main() и без if __name__ == "__main__"
asyncio.run(dp.start_polling(bot))
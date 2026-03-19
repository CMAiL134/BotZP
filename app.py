import asyncio
import logging
import os
from typing import Optional, List, Tuple

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
VIP_FOLDER_LINK = os.getenv("VIP_FOLDER_LINK", "").strip()
DB_PATH = "bot.sqlite3"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN пустой. Укажи токен в .env")

logging.basicConfig(level=logging.INFO)


# =========================
# ADMIN FILE
# =========================

def load_admins() -> set[str]:
    try:
        with open("admins.txt", "r", encoding="utf-8") as f:
            return {
                line.strip().lower()
                for line in f
                if line.strip() and line.strip().startswith("@")
            }
    except FileNotFoundError:
        return set()


ADMINS_USERNAMES = load_admins()


def reload_admins():
    global ADMINS_USERNAMES
    ADMINS_USERNAMES = load_admins()


def is_admin_user(user) -> bool:
    if not user:
        return False
    if user.username:
        return f"@{user.username.lower()}" in ADMINS_USERNAMES
    return False


# =========================
# DATABASE
# =========================

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    is_admin_chat INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tariffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    price_per_subscriber REAL NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    owner_username TEXT,
    channel_title TEXT NOT NULL,
    channel_username TEXT,
    channel_link TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price_per_subscriber REAL NOT NULL,
    total_price REAL NOT NULL,
    payment_status TEXT NOT NULL DEFAULT 'waiting',   -- waiting, proof_sent, paid, rejected
    status TEXT NOT NULL DEFAULT 'pending',           -- pending, active, finished, rejected
    proof_type TEXT,
    proof_file_id TEXT,
    proof_note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    paid_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vip_users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    granted_by INTEGER,
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS channel_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    is_required INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES_SQL)

        cur = await db.execute("SELECT COUNT(*) FROM tariffs")
        count = (await cur.fetchone())[0]
        if count == 0:
            await db.execute(
                """
                INSERT INTO tariffs (title, price_per_subscriber, is_active)
                VALUES (?, ?, ?)
                """,
                ("Базовый", 3.0, 1),
            )

        defaults = {
            "vip_folder_link": VIP_FOLDER_LINK or "",
            "payment_title": "Оплата рекламы",
            "payment_details": "💳 Карта: 2200 1234 5678 9999",
            "payment_note": "После перевода отправьте скрин или чек прямо в этот бот.",
            "vip_pool_percent": "60",
        }

        for key, value in defaults.items():
            cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
            if not row:
                await db.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )

        await db.commit()


async def register_user(user, admin_chat: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        current_admin_flag = 1 if admin_chat else 0
        await db.execute(
            """
            INSERT INTO users (user_id, username, full_name, is_admin_chat)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                is_admin_chat=CASE
                    WHEN excluded.is_admin_chat=1 THEN 1
                    ELSE users.is_admin_chat
                END
            """,
            (
                user.id,
                user.username,
                user.full_name,
                current_admin_flag,
            ),
        )
        await db.commit()


async def get_admin_chat_ids() -> List[int]:
    if not ADMINS_USERNAMES:
        return []

    placeholders = ",".join("?" for _ in ADMINS_USERNAMES)
    usernames = [x.lstrip("@") for x in ADMINS_USERNAMES]

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"""
            SELECT user_id
            FROM users
            WHERE is_admin_chat=1
              AND username IS NOT NULL
              AND lower(username) IN ({placeholders})
            """,
            usernames,
        )
        rows = await cur.fetchall()
        return [row[0] for row in rows]


async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def get_active_tariffs():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, title, price_per_subscriber FROM tariffs WHERE is_active=1 ORDER BY id"
        )
        return await cur.fetchall()


async def get_tariff(tariff_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, title, price_per_subscriber FROM tariffs WHERE id=? AND is_active=1",
            (tariff_id,),
        )
        return await cur.fetchone()


async def add_tariff(title: str, price_per_subscriber: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO tariffs (title, price_per_subscriber, is_active) VALUES (?, ?, 1)",
            (title, price_per_subscriber),
        )
        await db.commit()


async def list_tariffs():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, title, price_per_subscriber, is_active FROM tariffs ORDER BY id DESC"
        )
        return await cur.fetchall()


async def create_campaign(
    owner_user_id: int,
    owner_username: Optional[str],
    channel_title: str,
    channel_username: Optional[str],
    channel_link: str,
    quantity: int,
    price_per_subscriber: float,
) -> int:
    total_price = quantity * price_per_subscriber
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO campaigns
            (owner_user_id, owner_username, channel_title, channel_username, channel_link,
             quantity, price_per_subscriber, total_price, payment_status, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting', 'pending')
            """,
            (
                owner_user_id,
                owner_username,
                channel_title,
                channel_username,
                channel_link,
                quantity,
                price_per_subscriber,
                total_price,
            ),
        )
        await db.commit()
        return cur.lastrowid


async def get_campaign(campaign_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, owner_user_id, owner_username, channel_title, channel_username,
                   channel_link, quantity, price_per_subscriber, total_price,
                   payment_status, status, proof_type, proof_file_id, proof_note
            FROM campaigns
            WHERE id=?
            """,
            (campaign_id,),
        )
        return await cur.fetchone()


async def list_pending_campaigns():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, owner_user_id, owner_username, channel_title, channel_link,
                   quantity, price_per_subscriber, total_price, payment_status, status, created_at
            FROM campaigns
            WHERE status='pending'
            ORDER BY id DESC
            """
        )
        return await cur.fetchall()


async def list_active_campaigns():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, channel_title, channel_username, channel_link, quantity, total_price
            FROM campaigns
            WHERE status='active'
            ORDER BY id DESC
            """
        )
        return await cur.fetchall()


async def list_paid_campaigns():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, channel_title, channel_username, channel_link, total_price, paid_at
            FROM campaigns
            WHERE payment_status='paid'
            ORDER BY id DESC
            """
        )
        return await cur.fetchall()


async def set_campaign_status(campaign_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE campaigns SET status=? WHERE id=?",
            (status, campaign_id),
        )
        await db.commit()


async def set_campaign_payment_status(campaign_id: int, payment_status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        if payment_status == "paid":
            await db.execute(
                "UPDATE campaigns SET payment_status=?, paid_at=CURRENT_TIMESTAMP WHERE id=?",
                (payment_status, campaign_id),
            )
        else:
            await db.execute(
                "UPDATE campaigns SET payment_status=? WHERE id=?",
                (payment_status, campaign_id),
            )
        await db.commit()


async def save_payment_proof(
    campaign_id: int,
    proof_type: str,
    proof_file_id: str,
    proof_note: Optional[str],
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE campaigns
            SET payment_status='proof_sent',
                proof_type=?,
                proof_file_id=?,
                proof_note=?
            WHERE id=?
            """,
            (proof_type, proof_file_id, proof_note, campaign_id),
        )
        await db.commit()


async def replace_campaign_checks(campaign_id: int, chat_ids: List[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM channel_checks WHERE campaign_id=?", (campaign_id,))
        for chat_id in chat_ids:
            await db.execute(
                "INSERT INTO channel_checks (campaign_id, chat_id, is_required) VALUES (?, ?, 1)",
                (campaign_id, chat_id),
            )
        await db.commit()


async def get_required_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT DISTINCT cc.chat_id
            FROM channel_checks cc
            JOIN campaigns c ON c.id = cc.campaign_id
            WHERE c.status='active' AND cc.is_required=1
            """
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def grant_vip(user_id: int, username: Optional[str], granted_by: Optional[int] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO vip_users (user_id, username, granted_by, granted_at, is_active)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                granted_by=excluded.granted_by,
                granted_at=CURRENT_TIMESTAMP,
                is_active=1
            """,
            (user_id, username, granted_by),
        )
        await db.commit()


async def revoke_vip(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE vip_users SET is_active=0 WHERE user_id=?",
            (user_id,),
        )
        await db.commit()


async def has_vip(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id FROM vip_users WHERE user_id=? AND is_active=1",
            (user_id,),
        )
        row = await cur.fetchone()
        return bool(row)


async def list_vip_users():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT user_id, username, granted_at, is_active
            FROM vip_users
            ORDER BY granted_at DESC
            """
        )
        return await cur.fetchall()


async def find_user_by_username(username: str):
    username = username.lstrip("@").lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, username FROM users WHERE lower(username)=?",
            (username,),
        )
        return await cur.fetchone()


async def get_finance_summary() -> Tuple[float, float, int, float]:
    """
    total_paid, vip_pool, active_vip_count, equal_share
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(total_price), 0) FROM campaigns WHERE payment_status='paid'"
        )
        total_paid = float((await cur.fetchone())[0] or 0)

        cur = await db.execute(
            "SELECT COUNT(*) FROM vip_users WHERE is_active=1"
        )
        active_vip_count = int((await cur.fetchone())[0] or 0)

    vip_percent = float(await get_setting("vip_pool_percent") or "60")
    vip_pool = total_paid * (vip_percent / 100.0)
    equal_share = vip_pool / active_vip_count if active_vip_count > 0 else 0.0
    return total_paid, vip_pool, active_vip_count, equal_share


# =========================
# STATES
# =========================

class BuyAdState(StatesGroup):
    choosing_tariff = State()
    waiting_channel_title = State()
    waiting_channel_username = State()
    waiting_channel_link = State()
    waiting_quantity = State()


class SendProofState(StatesGroup):
    waiting_proof = State()


class AdminAddTariffState(StatesGroup):
    waiting_title = State()
    waiting_price = State()


class AdminSetFolderState(StatesGroup):
    waiting_link = State()


class AdminSetPaymentTitleState(StatesGroup):
    waiting_value = State()


class AdminSetPaymentDetailsState(StatesGroup):
    waiting_value = State()


class AdminSetPaymentNoteState(StatesGroup):
    waiting_value = State()


class AdminActivateCampaignState(StatesGroup):
    waiting_campaign_id = State()
    waiting_chat_ids = State()


class AdminGrantVipState(StatesGroup):
    waiting_user = State()


# =========================
# HELPERS
# =========================

def username_or_id(user_id: int, username: Optional[str]) -> str:
    return f"@{username}" if username else str(user_id)


async def check_user_subscriptions(bot: Bot, user_id: int) -> tuple[bool, list[str]]:
    required_channels = await get_required_channels()

    if not required_channels:
        return True, []

    missing = []
    for chat_id in required_channels:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                missing.append(chat_id)
        except Exception:
            missing.append(chat_id)

    return len(missing) == 0, missing


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Купить рекламу", callback_data="buy_ads")
    kb.button(text="💎 VIP", callback_data="vip_menu")
    kb.button(text="💸 Заработок VIP", callback_data="vip_income")
    kb.button(text="✅ Проверить подписки", callback_data="check_subs")
    kb.button(text="📣 Активные рекламы", callback_data="active_ads")
    if is_admin:
        kb.button(text="⚙️ Админская панель", callback_data="admin_menu")
    kb.adjust(1)
    return kb.as_markup()


def admin_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить тариф", callback_data="admin_add_tariff")
    kb.button(text="📋 Список тарифов", callback_data="admin_tariffs")
    kb.button(text="📂 Установить VIP-папку", callback_data="admin_set_folder")
    kb.button(text="💳 Заголовок оплаты", callback_data="admin_set_payment_title")
    kb.button(text="💰 Реквизиты оплаты", callback_data="admin_set_payment_details")
    kb.button(text="📝 Примечание к оплате", callback_data="admin_set_payment_note")
    kb.button(text="🕐 Ожидающие заявки", callback_data="admin_pending")
    kb.button(text="✅ Активировать заявку", callback_data="admin_activate_campaign")
    kb.button(text="👑 Список VIP", callback_data="admin_vip_list")
    kb.button(text="➕ Выдать VIP", callback_data="admin_grant_vip")
    kb.button(text="📊 Финансы VIP", callback_data="admin_finance")
    kb.button(text="🔄 Обновить список админов", callback_data="admin_reload_admins")
    kb.button(text="⬅️ Назад", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()


def proof_admin_kb(campaign_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить оплату",
                    callback_data=f"approve_pay:{campaign_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👑 Выдать VIP",
                    callback_data=f"grant_vip_from_campaign:{campaign_id}:{user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отклонить чек",
                    callback_data=f"reject_pay:{campaign_id}"
                )
            ],
        ]
    )


async def send_proof_to_admins(bot: Bot, campaign_id: int):
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return

    admin_chat_ids = await get_admin_chat_ids()
    if not admin_chat_ids:
        return

    (
        _id,
        owner_user_id,
        owner_username,
        channel_title,
        _channel_username,
        channel_link,
        quantity,
        _pps,
        total_price,
        payment_status,
        status,
        proof_type,
        proof_file_id,
        proof_note,
    ) = campaign

    caption = (
        "💳 Новый чек по заявке\n\n"
        f"Заявка: #{campaign_id}\n"
        f"Пользователь: {username_or_id(owner_user_id, owner_username)}\n"
        f"user_id: {owner_user_id}\n"
        f"Канал: {channel_title}\n"
        f"Ссылка: {channel_link}\n"
        f"Количество: {quantity}\n"
        f"Сумма: {total_price:.2f} ₽\n"
        f"Статус оплаты: {payment_status}\n"
        f"Статус заявки: {status}\n"
    )

    if proof_note:
        caption += f"\nКомментарий пользователя:\n{proof_note}\n"

    for admin_id in admin_chat_ids:
        try:
            if proof_type == "photo":
                await bot.send_photo(
                    admin_id,
                    photo=proof_file_id,
                    caption=caption,
                    reply_markup=proof_admin_kb(campaign_id, owner_user_id),
                )
            else:
                await bot.send_document(
                    admin_id,
                    document=proof_file_id,
                    caption=caption,
                    reply_markup=proof_admin_kb(campaign_id, owner_user_id),
                )
        except Exception:
            pass


# =========================
# BOT SETUP
# =========================

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# =========================
# USER HANDLERS
# =========================

@dp.message(CommandStart())
async def start_handler(message: Message):
    reload_admins()
    await register_user(message.from_user, admin_chat=is_admin_user(message.from_user))
    vip = await has_vip(message.from_user.id)

    text = (
        "Привет.\n\n"
        "Этот бот позволяет:\n"
        "— купить рекламу канала;\n"
        "— отправить чек после оплаты;\n"
        "— получить VIP после подписки на рекламные каналы;\n"
        "— смотреть расчётный заработок VIP.\n\n"
        f"VIP статус: {'активен' if vip else 'не активен'}"
    )
    await message.answer(
        text,
        reply_markup=main_menu(is_admin_user(message.from_user))
    )


@dp.message(Command("myid"))
async def myid_command(message: Message):
    await register_user(message.from_user, admin_chat=is_admin_user(message.from_user))
    username = f"@{message.from_user.username}" if message.from_user.username else "нет"
    await message.answer(
        f"Твой user_id: {message.from_user.id}\n"
        f"Твой username: {username}"
    )


@dp.callback_query(F.data == "back_main")
async def back_main_handler(call: CallbackQuery):
    await register_user(call.from_user, admin_chat=is_admin_user(call.from_user))
    await call.message.edit_text(
        "Главное меню:",
        reply_markup=main_menu(is_admin_user(call.from_user))
    )
    await call.answer()


@dp.callback_query(F.data == "buy_ads")
async def buy_ads_handler(call: CallbackQuery, state: FSMContext):
    tariffs = await get_active_tariffs()
    if not tariffs:
        await call.answer("Тарифы пока не настроены.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for tariff_id, title, price_per_subscriber in tariffs:
        kb.button(
            text=f"{title} — {price_per_subscriber:.2f} ₽/подписчик",
            callback_data=f"tariff:{tariff_id}",
        )
    kb.button(text="⬅️ Назад", callback_data="back_main")
    kb.adjust(1)

    await state.set_state(BuyAdState.choosing_tariff)
    await call.message.edit_text("Выбери тариф:", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("tariff:"))
async def tariff_selected(call: CallbackQuery, state: FSMContext):
    tariff_id = int(call.data.split(":")[1])
    tariff = await get_tariff(tariff_id)
    if not tariff:
        await call.answer("Тариф не найден.", show_alert=True)
        return

    await state.update_data(tariff_id=tariff[0], tariff_title=tariff[1], price=tariff[2])
    await state.set_state(BuyAdState.waiting_channel_title)
    await call.message.edit_text("Введи название канала:")
    await call.answer()


@dp.message(BuyAdState.waiting_channel_title)
async def buy_waiting_channel_title(message: Message, state: FSMContext):
    await register_user(message.from_user, admin_chat=is_admin_user(message.from_user))
    await state.update_data(channel_title=message.text.strip())
    await state.set_state(BuyAdState.waiting_channel_username)
    await message.answer("Введи username канала без @. Если username нет — отправь -")


@dp.message(BuyAdState.waiting_channel_username)
async def buy_waiting_channel_username(message: Message, state: FSMContext):
    username = message.text.strip()
    if username == "-":
        username = None
    else:
        username = username.replace("@", "").strip()

    await state.update_data(channel_username=username)
    await state.set_state(BuyAdState.waiting_channel_link)
    await message.answer("Отправь ссылку на канал. Например: https://t.me/my_channel")


@dp.message(BuyAdState.waiting_channel_link)
async def buy_waiting_channel_link(message: Message, state: FSMContext):
    await state.update_data(channel_link=message.text.strip())
    await state.set_state(BuyAdState.waiting_quantity)
    await message.answer("Сколько подписчиков нужно?")


@dp.message(BuyAdState.waiting_quantity)
async def buy_waiting_quantity(message: Message, state: FSMContext):
    try:
        quantity = int(message.text.strip())
        if quantity <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи положительное число.")
        return

    data = await state.get_data()
    campaign_id = await create_campaign(
        owner_user_id=message.from_user.id,
        owner_username=message.from_user.username,
        channel_title=data["channel_title"],
        channel_username=data.get("channel_username"),
        channel_link=data["channel_link"],
        quantity=quantity,
        price_per_subscriber=data["price"],
    )

    total_price = quantity * data["price"]
    await state.clear()

    await message.answer(
        f"Заявка создана.\n\n"
        f"ID заявки: {campaign_id}\n"
        f"Канал: {data['channel_title']}\n"
        f"Ссылка: {data['channel_link']}\n"
        f"Количество: {quantity}\n"
        f"Цена за подписчика: {data['price']:.2f} ₽\n"
        f"Итого: {total_price:.2f} ₽\n\n"
        f"Следующий шаг:\n"
        f"1) напиши /pay {campaign_id}\n"
        f"2) получи реквизиты\n"
        f"3) отправь скрин перевода прямо сюда",
        reply_markup=main_menu(is_admin_user(message.from_user))
    )


@dp.message(Command("pay"))
async def pay_handler(message: Message, state: FSMContext):
    await register_user(message.from_user, admin_chat=is_admin_user(message.from_user))

    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Используй команду так: /pay НОМЕР_ЗАЯВКИ")
        return

    campaign_id = int(parts[1])
    campaign = await get_campaign(campaign_id)

    if not campaign:
        await message.answer("Заявка не найдена.")
        return

    if campaign[1] != message.from_user.id:
        await message.answer("Эта заявка не принадлежит тебе.")
        return

    payment_title = await get_setting("payment_title") or "Оплата рекламы"
    payment_details = await get_setting("payment_details") or "Реквизиты не указаны"
    payment_note = await get_setting("payment_note") or ""

    await state.set_state(SendProofState.waiting_proof)
    await state.update_data(campaign_id=campaign_id)

    await message.answer(
        f"{payment_title}\n\n"
        f"{payment_details}\n\n"
        f"ID заявки: {campaign_id}\n"
        f"Сумма: {campaign[8]:.2f} ₽\n\n"
        f"{payment_note}\n\n"
        f"После оплаты отправь сюда скрин или документ с чеком одним сообщением."
    )


@dp.message(SendProofState.waiting_proof, F.photo)
async def receive_payment_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    if not campaign_id:
        await message.answer("Ошибка состояния. Повтори /pay НОМЕР_ЗАЯВКИ")
        await state.clear()
        return

    file_id = message.photo[-1].file_id
    await save_payment_proof(
        campaign_id=campaign_id,
        proof_type="photo",
        proof_file_id=file_id,
        proof_note=message.caption,
    )
    await send_proof_to_admins(bot, campaign_id)
    await state.clear()

    await message.answer(
        "✅ Чек отправлен администраторам.\n"
        "После проверки тебе придет уведомление."
    )


@dp.message(SendProofState.waiting_proof, F.document)
async def receive_payment_document(message: Message, state: FSMContext):
    data = await state.get_data()
    campaign_id = data.get("campaign_id")
    if not campaign_id:
        await message.answer("Ошибка состояния. Повтори /pay НОМЕР_ЗАЯВКИ")
        await state.clear()
        return

    file_id = message.document.file_id
    await save_payment_proof(
        campaign_id=campaign_id,
        proof_type="document",
        proof_file_id=file_id,
        proof_note=message.caption,
    )
    await send_proof_to_admins(bot, campaign_id)
    await state.clear()

    await message.answer(
        "✅ Чек отправлен администраторам.\n"
        "После проверки тебе придет уведомление."
    )


@dp.message(SendProofState.waiting_proof)
async def receive_payment_invalid(message: Message):
    await message.answer("Отправь именно фото-скрин или документ с чеком.")


@dp.callback_query(F.data == "vip_menu")
async def vip_menu_handler(call: CallbackQuery):
    vip = await has_vip(call.from_user.id)
    folder_link = await get_setting("vip_folder_link") or ""

    text = (
        f"VIP статус: {'активен' if vip else 'не активен'}\n\n"
        "Чтобы получить VIP:\n"
        "1) открой папку с рекламными каналами;\n"
        "2) подпишись на все каналы;\n"
        "3) нажми «Проверить подписки».\n\n"
        "Если админ выдал тебе VIP вручную, статус тоже обновится."
    )

    kb = InlineKeyboardBuilder()
    if folder_link:
        kb.button(text="📂 Открыть папку", url=folder_link)
    kb.button(text="✅ Проверить подписки", callback_data="check_subs")
    kb.button(text="💸 Заработок VIP", callback_data="vip_income")
    kb.button(text="⬅️ Назад", callback_data="back_main")
    kb.adjust(1)

    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data == "check_subs")
async def check_subs_handler(call: CallbackQuery):
    ok, missing = await check_user_subscriptions(bot, call.from_user.id)

    if ok:
        await grant_vip(call.from_user.id, call.from_user.username)
        await call.message.edit_text(
            "✅ Подписки подтверждены.\n\n"
            "Тебе выдан VIP-доступ.",
            reply_markup=main_menu(is_admin_user(call.from_user))
        )
    else:
        missing_text = "\n".join(missing[:20]) if missing else "Не удалось проверить часть каналов"
        await call.message.edit_text(
            "❌ Не все подписки выполнены.\n\n"
            "Подпишись на все активные рекламные каналы и попробуй снова.\n\n"
            f"Не хватает:\n{missing_text}",
            reply_markup=main_menu(is_admin_user(call.from_user))
        )

    await call.answer()


@dp.callback_query(F.data == "active_ads")
async def active_ads_handler(call: CallbackQuery):
    campaigns = await list_active_campaigns()
    if not campaigns:
        await call.message.edit_text(
            "Сейчас нет активных реклам.",
            reply_markup=main_menu(is_admin_user(call.from_user))
        )
        await call.answer()
        return

    lines = ["📣 Активные наборы/каналы:\n"]
    for campaign_id, title, username, link, quantity, total_price in campaigns[:30]:
        channel_ref = f"@{username}" if username else link
        lines.append(
            f"#{campaign_id} — {title}\n"
            f"Канал: {channel_ref}\n"
            f"Объем: {quantity}\n"
            f"Оплата по заявке: {total_price:.2f} ₽\n"
        )

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=main_menu(is_admin_user(call.from_user))
    )
    await call.answer()


@dp.callback_query(F.data == "vip_income")
async def vip_income_handler(call: CallbackQuery):
    vip = await has_vip(call.from_user.id)
    total_paid, vip_pool, active_vip_count, equal_share = await get_finance_summary()
    active_campaigns = await list_active_campaigns()

    lines = [
        "💸 Заработок VIP\n",
        f"Всего оплачено рекламодателями: {total_paid:.2f} ₽",
        f"VIP-пул (60%): {vip_pool:.2f} ₽",
        f"Активных VIP: {active_vip_count}",
        f"Расчётная доля на 1 VIP: {equal_share:.2f} ₽",
        "",
    ]

    if vip:
        lines.append("Твой VIP активен.")
        lines.append(f"Твоя текущая расчётная доля: {equal_share:.2f} ₽")
    else:
        lines.append("Сейчас у тебя нет активного VIP.")
        lines.append("После получения VIP ты будешь видеть ту же расчётную долю.")

    lines.append("")
    lines.append("Активные рекламные наборы/каналы:")
    if active_campaigns:
        for campaign_id, title, username, link, _quantity, total_price in active_campaigns[:20]:
            channel_ref = f"@{username}" if username else link
            lines.append(f"#{campaign_id} — {title} — {channel_ref} — {total_price:.2f} ₽")
    else:
        lines.append("Пока активных наборов нет.")

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=main_menu(is_admin_user(call.from_user))
    )
    await call.answer()


# =========================
# ADMIN HANDLERS
# =========================

@dp.message(Command("admin"))
async def admin_command(message: Message):
    reload_admins()
    await register_user(message.from_user, admin_chat=is_admin_user(message.from_user))
    if not is_admin_user(message.from_user):
        await message.answer("У тебя нет доступа к админской панели.")
        return
    await message.answer("Админская панель:", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "admin_menu")
async def admin_menu_handler(call: CallbackQuery):
    reload_admins()
    await register_user(call.from_user, admin_chat=is_admin_user(call.from_user))
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return
    await call.message.edit_text("Админская панель:", reply_markup=admin_menu_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_reload_admins")
async def admin_reload_admins_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return

    reload_admins()
    admins_text = "\n".join(sorted(ADMINS_USERNAMES)) if ADMINS_USERNAMES else "Список пуст"
    await call.message.edit_text(
        f"✅ Список админов обновлен.\n\nТекущие админы:\n{admins_text}",
        reply_markup=admin_menu_kb()
    )
    await call.answer()


@dp.callback_query(F.data == "admin_add_tariff")
async def admin_add_tariff_handler(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminAddTariffState.waiting_title)
    await call.message.edit_text("Введи название нового тарифа:")
    await call.answer()


@dp.message(AdminAddTariffState.waiting_title)
async def admin_add_tariff_title(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user):
        return
    await state.update_data(title=message.text.strip())
    await state.set_state(AdminAddTariffState.waiting_price)
    await message.answer("Введи цену за подписчика, например 3 или 3.5")


@dp.message(AdminAddTariffState.waiting_price)
async def admin_add_tariff_price(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user):
        return

    try:
        price = float(message.text.strip().replace(",", "."))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Некорректная цена.")
        return

    data = await state.get_data()
    await add_tariff(data["title"], price)
    await state.clear()
    await message.answer(
        f"✅ Тариф «{data['title']}» добавлен: {price:.2f} ₽/подписчик",
        reply_markup=admin_menu_kb()
    )


@dp.callback_query(F.data == "admin_tariffs")
async def admin_tariffs_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return

    tariffs = await list_tariffs()
    if not tariffs:
        await call.message.edit_text("Тарифов нет.", reply_markup=admin_menu_kb())
        await call.answer()
        return

    lines = ["📋 Список тарифов:\n"]
    for tariff_id, title, price, is_active in tariffs:
        status = "активен" if is_active else "выключен"
        lines.append(f"{tariff_id}. {title} — {price:.2f} ₽/подписчик ({status})")

    await call.message.edit_text("\n".join(lines), reply_markup=admin_menu_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_set_folder")
async def admin_set_folder_handler(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminSetFolderState.waiting_link)
    await call.message.edit_text("Отправь новую ссылку на VIP-папку Telegram.")
    await call.answer()


@dp.message(AdminSetFolderState.waiting_link)
async def admin_set_folder_link(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user):
        return
    await set_setting("vip_folder_link", message.text.strip())
    await state.clear()
    await message.answer("✅ Ссылка на VIP-папку обновлена.", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "admin_set_payment_title")
async def admin_set_payment_title_handler(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminSetPaymentTitleState.waiting_value)
    await call.message.edit_text("Отправь новый заголовок оплаты.")
    await call.answer()


@dp.message(AdminSetPaymentTitleState.waiting_value)
async def admin_set_payment_title_value(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user):
        return
    await set_setting("payment_title", message.text.strip())
    await state.clear()
    await message.answer("✅ Заголовок оплаты обновлен.", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "admin_set_payment_details")
async def admin_set_payment_details_handler(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminSetPaymentDetailsState.waiting_value)
    await call.message.edit_text("Отправь новые реквизиты оплаты.")
    await call.answer()


@dp.message(AdminSetPaymentDetailsState.waiting_value)
async def admin_set_payment_details_value(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user):
        return
    await set_setting("payment_details", message.text.strip())
    await state.clear()
    await message.answer("✅ Реквизиты оплаты обновлены.", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "admin_set_payment_note")
async def admin_set_payment_note_handler(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminSetPaymentNoteState.waiting_value)
    await call.message.edit_text("Отправь новый текст примечания к оплате.")
    await call.answer()


@dp.message(AdminSetPaymentNoteState.waiting_value)
async def admin_set_payment_note_value(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user):
        return
    await set_setting("payment_note", message.text.strip())
    await state.clear()
    await message.answer("✅ Примечание к оплате обновлено.", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "admin_pending")
async def admin_pending_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return

    rows = await list_pending_campaigns()
    if not rows:
        await call.message.edit_text("Ожидающих заявок нет.", reply_markup=admin_menu_kb())
        await call.answer()
        return

    text = ["🕐 Ожидающие заявки:\n"]
    for row in rows[:20]:
        (
            campaign_id,
            owner_user_id,
            owner_username,
            channel_title,
            channel_link,
            quantity,
            pps,
            total,
            payment_status,
            status,
            created_at,
        ) = row

        text.append(
            f"ID: {campaign_id}\n"
            f"Пользователь: {username_or_id(owner_user_id, owner_username)}\n"
            f"Канал: {channel_title}\n"
            f"Ссылка: {channel_link}\n"
            f"Количество: {quantity}\n"
            f"Цена: {pps:.2f} ₽\n"
            f"Сумма: {total:.2f} ₽\n"
            f"Оплата: {payment_status}\n"
            f"Статус: {status}\n"
            f"Дата: {created_at}\n"
            f"---"
        )

    await call.message.edit_text("\n".join(text), reply_markup=admin_menu_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_activate_campaign")
async def admin_activate_campaign_handler(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(AdminActivateCampaignState.waiting_campaign_id)
    await call.message.edit_text("Введи ID заявки, которую нужно активировать.")
    await call.answer()


@dp.message(AdminActivateCampaignState.waiting_campaign_id)
async def admin_activate_campaign_id(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user):
        return

    try:
        campaign_id = int(message.text.strip())
    except ValueError:
        await message.answer("Некорректный ID.")
        return

    campaign = await get_campaign(campaign_id)
    if not campaign:
        await message.answer("Заявка не найдена.")
        return

    if campaign[9] != "paid":
        await message.answer(
            "Эта заявка еще не подтверждена как оплаченная.\n"
            "Сначала подтверди чек кнопкой из админского сообщения."
        )
        return

    await state.update_data(campaign_id=campaign_id)
    await state.set_state(AdminActivateCampaignState.waiting_chat_ids)
    await message.answer(
        "Теперь отправь @username канала или несколько через запятую.\n\n"
        "Пример:\n"
        "@channel_one,@channel_two"
    )


@dp.message(AdminActivateCampaignState.waiting_chat_ids)
async def admin_activate_campaign_chat_ids(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user):
        return

    raw = message.text.strip()
    chat_ids = [x.strip() for x in raw.split(",") if x.strip()]
    if not chat_ids:
        await message.answer("Нужно указать хотя бы один chat_id.")
        return

    data = await state.get_data()
    campaign_id = data["campaign_id"]

    await replace_campaign_checks(campaign_id, chat_ids)
    await set_campaign_status(campaign_id, "active")
    campaign = await get_campaign(campaign_id)
    await state.clear()

    if campaign:
        owner_user_id = campaign[1]
        try:
            await bot.send_message(
                owner_user_id,
                f"✅ Ваша заявка #{campaign_id} активирована.\n"
                f"Канал: {campaign[3]}"
            )
        except Exception:
            pass

    await message.answer(
        f"✅ Заявка #{campaign_id} активирована.\n"
        f"Теперь она участвует в VIP-системе.",
        reply_markup=admin_menu_kb()
    )


@dp.callback_query(F.data == "admin_vip_list")
async def admin_vip_list_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return

    users = await list_vip_users()
    if not users:
        await call.message.edit_text("VIP пользователей нет.", reply_markup=admin_menu_kb())
        await call.answer()
        return

    text = ["👑 VIP пользователи:\n"]
    for user_id, username, granted_at, is_active in users[:50]:
        name = f"@{username}" if username else str(user_id)
        status = "активен" if is_active else "выключен"
        text.append(f"{name} — {granted_at} — {status}")

    await call.message.edit_text("\n".join(text), reply_markup=admin_menu_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_grant_vip")
async def admin_grant_vip_handler(call: CallbackQuery, state: FSMContext):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(AdminGrantVipState.waiting_user)
    await call.message.edit_text(
        "Отправь user_id или @username пользователя, которому нужно выдать VIP."
    )
    await call.answer()


@dp.message(AdminGrantVipState.waiting_user)
async def admin_grant_vip_value(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user):
        return

    raw = message.text.strip()
    target_user_id = None
    target_username = None

    if raw.startswith("@"):
        found = await find_user_by_username(raw)
        if not found:
            await message.answer(
                "Пользователь с таким username не найден в базе.\n"
                "Он должен хотя бы один раз написать боту."
            )
            return
        target_user_id, db_username = found
        target_username = db_username
    else:
        try:
            target_user_id = int(raw)
        except ValueError:
            await message.answer("Нужно отправить user_id или @username.")
            return

    await grant_vip(target_user_id, target_username, granted_by=message.from_user.id)
    await state.clear()

    try:
        await bot.send_message(
            target_user_id,
            "👑 Администратор выдал тебе VIP-доступ."
        )
    except Exception:
        pass

    await message.answer(
        f"✅ VIP выдан пользователю {target_user_id}.",
        reply_markup=admin_menu_kb()
    )


@dp.callback_query(F.data == "admin_finance")
async def admin_finance_handler(call: CallbackQuery):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return

    total_paid, vip_pool, active_vip_count, equal_share = await get_finance_summary()
    paid_campaigns = await list_paid_campaigns()

    lines = [
        "📊 Финансы VIP\n",
        f"Сумма всех оплаченных заявок: {total_paid:.2f} ₽",
        f"VIP-пул 60%: {vip_pool:.2f} ₽",
        f"Активных VIP: {active_vip_count}",
        f"Расчётная доля на 1 VIP: {equal_share:.2f} ₽",
        "",
        "Последние оплаченные заявки:",
    ]

    if paid_campaigns:
        for campaign_id, title, username, link, total_price, paid_at in paid_campaigns[:20]:
            channel_ref = f"@{username}" if username else link
            lines.append(
                f"#{campaign_id} — {title} — {channel_ref} — "
                f"{total_price:.2f} ₽ — {paid_at}"
            )
    else:
        lines.append("Оплаченных заявок пока нет.")

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=admin_menu_kb()
    )
    await call.answer()


@dp.callback_query(F.data.startswith("approve_pay:"))
async def approve_pay_callback(call: CallbackQuery):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return

    campaign_id = int(call.data.split(":")[1])
    campaign = await get_campaign(campaign_id)
    if not campaign:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    await set_campaign_payment_status(campaign_id, "paid")

    owner_user_id = campaign[1]
    try:
        await bot.send_message(
            owner_user_id,
            f"✅ Ваш чек по заявке #{campaign_id} подтвержден.\n"
            f"Теперь ожидайте активации рекламы администратором."
        )
    except Exception:
        pass

    await call.answer("Оплата подтверждена.")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("reject_pay:"))
async def reject_pay_callback(call: CallbackQuery):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return

    campaign_id = int(call.data.split(":")[1])
    campaign = await get_campaign(campaign_id)
    if not campaign:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    await set_campaign_payment_status(campaign_id, "rejected")

    owner_user_id = campaign[1]
    try:
        await bot.send_message(
            owner_user_id,
            f"❌ Ваш чек по заявке #{campaign_id} отклонен.\n"
            f"Отправьте корректный чек еще раз через /pay {campaign_id}"
        )
    except Exception:
        pass

    await call.answer("Чек отклонен.")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("grant_vip_from_campaign:"))
async def grant_vip_from_campaign_callback(call: CallbackQuery):
    if not is_admin_user(call.from_user):
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, campaign_id_str, user_id_str = call.data.split(":")
    campaign_id = int(campaign_id_str)
    user_id = int(user_id_str)

    campaign = await get_campaign(campaign_id)
    if not campaign:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    await grant_vip(user_id, campaign[2], granted_by=call.from_user.id)

    try:
        await bot.send_message(
            user_id,
            "👑 Администратор выдал тебе VIP-доступ."
        )
    except Exception:
        pass

    await call.answer("VIP выдан.")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# =========================
# MAIN
# =========================

async def main():
    reload_admins()
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
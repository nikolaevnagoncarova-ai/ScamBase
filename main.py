import asyncio
import os
import html
import logging
from typing import Optional, Dict, List, Tuple

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)

# ==========================================
# 1. КОНФИГУРАЦИЯ И НАСТРОЙКИ
# ==========================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")

# Список ID администраторов (можно перечислить через запятую "1234567,9876543")
raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(admin_id.strip()) for admin_id in raw_admins.split(",") if admin_id.strip().isdigit()]

DB_PATH = os.getenv("DB_PATH", "antiscam.db")

# Настройка корректных абсолютных путей к картинкам
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANNERS = {
    "welcome": os.path.join(BASE_DIR, "banners", "welcome.png"),
    "unknown": os.path.join(BASE_DIR, "banners", "unknown.png"),
    "trusted": os.path.join(BASE_DIR, "banners", "trusted.png"),
    "scam": os.path.join(BASE_DIR, "banners", "scam.png")
}

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# ==========================================
# 2. РАБОТА С БАЗОЙ ДАННЫХ (SQLITE)
# ==========================================
def normalize_id(ident: str) -> str:
    ident = ident.strip()
    if ident.startswith("@"):
        return ident.lower()
    return ident

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scammers (
                identifier TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                proof TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trusted_users (
                identifier TEXT PRIMARY KEY,
                note TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guarantors (
                identifier TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                deposit TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL,
                target TEXT NOT NULL,
                description TEXT NOT NULL,
                proof TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def check_entity(ident: str) -> Tuple[str, Optional[Dict]]:
    ident = normalize_id(ident)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        async with db.execute("SELECT * FROM scammers WHERE identifier = ?", (ident,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return "scammer", dict(row)
                
        async with db.execute("SELECT * FROM guarantors WHERE identifier = ?", (ident,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return "guarantor", dict(row)

        async with db.execute("SELECT * FROM trusted_users WHERE identifier = ?", (ident,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return "trusted", dict(row)

        return "unknown", None

async def add_scammer(identifier: str, reason: str, proof: str = "Не указаны"):
    ident = normalize_id(identifier)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO scammers (identifier, reason, proof) VALUES (?, ?, ?)",
            (ident, reason, proof)
        )
        await db.execute("DELETE FROM trusted_users WHERE identifier = ?", (ident,))
        await db.execute("DELETE FROM guarantors WHERE identifier = ?", (ident,))
        await db.commit()

async def add_trusted_user(identifier: str, note: str = "Проверен"):
    ident = normalize_id(identifier)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO trusted_users (identifier, note) VALUES (?, ?)",
            (ident, note)
        )
        await db.execute("DELETE FROM scammers WHERE identifier = ?", (ident,))
        await db.commit()

async def add_guarantor(identifier: str, name: str, description: str, deposit: str = "Отсутствует"):
    ident = normalize_id(identifier)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO guarantors (identifier, name, description, deposit) VALUES (?, ?, ?, ?)",
            (ident, name, description, deposit)
        )
        await db.execute("DELETE FROM scammers WHERE identifier = ?", (ident,))
        await db.commit()

async def get_guarantors() -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM guarantors ORDER BY added_at DESC") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

async def add_complaint(reporter_id: int, target: str, description: str, proof: str) -> int:
    target = normalize_id(target)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO complaints (reporter_id, target, description, proof) VALUES (?, ?, ?, ?)",
            (reporter_id, target, description, proof)
        )
        await db.commit()
        return cursor.lastrowid

async def get_pending_complaints() -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM complaints WHERE status = 'pending' ORDER BY id ASC") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

async def get_complaint_by_id(complaint_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM complaints WHERE id = ?", (complaint_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def resolve_complaint(complaint_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE complaints SET status = ? WHERE id = ?", (status, complaint_id))
        await db.commit()


# ==========================================
# 3. FSM СОСТОЯНИЯ И КЛАВИАТУРЫ
# ==========================================
class ComplaintState(StatesGroup):
    target = State()
    description = State()
    proof = State()

class AdminState(StatesGroup):
    add_scam_target = State()
    add_scam_reason = State()
    add_scam_proof = State()
    
    add_trust_target = State()
    
    add_guarantor_target = State()
    add_guarantor_name = State()
    add_guarantor_desc = State()
    add_guarantor_deposit = State()

class CheckState(StatesGroup):
    input_entity = State()

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Проверить пользователя"), KeyboardButton(text="🛡 Список гарантов")],
            [KeyboardButton(text="📩 Подать жалобу"), KeyboardButton(text="🔗 Проверка реквизитов")]
        ],
        resize_keyboard=True
    )

def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить скамера", callback_data="admin_add_scam")],
            [InlineKeyboardButton(text="➕ Добавить надежного", callback_data="admin_add_trust")],
            [InlineKeyboardButton(text="➕ Добавить гаранта", callback_data="admin_add_guarantor")],
            [InlineKeyboardButton(text="📑 Жалобы на рассмотрении", callback_data="admin_view_complaints")]
        ]
    )

async def send_banner_response(message: types.Message, banner_key: str, caption_text: str, reply_markup=None):
    file_path = BANNERS.get(banner_key)
    if file_path and os.path.exists(file_path):
        photo = FSInputFile(file_path)
        await message.answer_photo(photo=photo, caption=caption_text, reply_markup=reply_markup)
    else:
        await message.answer(text=caption_text, reply_markup=reply_markup)


# ==========================================
# 4. ОБРАБОТЧИКИ КОМАНД И СООБЩЕНИЙ
# ==========================================

# --- /start ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    text = (
        "<b>ДОБРО ПОЖАЛОВАТЬ В ЕДИНУЮ АНТИ-СКАМ БАЗУ</b>\n\n"
        "<blockquote>Автоматизированный сервис проверки контрагентов, поиска злоумышленников и верифицированных гарантов.</blockquote>\n\n"
        "<u>Доступные возможности:</u>\n"
        "• <b>Проверка пользователей</b> по ID или Username\n"
        "• <b>Авто-защита чатов</b> при отправке сообщений\n"
        "• <b>Реестр проверенных гарантов</b> с депозитами\n"
        "• <b>Подача официальных жалоб</b> с доказательствами\n\n"
        "<i>Используйте нижнее меню для навигации.</i>"
    )
    await send_banner_response(message, "welcome", text, reply_markup=main_keyboard())

# --- /admin ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(
            f"<b>ОТКАЗАНО В ДОСТУПЕ</b>\n\n"
            f"У вас нет прав администратора.\n"
            f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n\n"
            f"Укажите этот ID в переменной <code>ADMIN_IDS</code> в настройках Render или прямо в коде!"
        )
        return
    text = (
        "<b>ПАНЕЛЬ УПРАВЛЕНИЯ АДМИНИСТРАТОРА</b>\n\n"
        "<blockquote>Выберите необходимый раздел для модерации базы данных.</blockquote>"
    )
    await message.answer(text, reply_markup=admin_keyboard())

# --- Проверка контрагента ---
async def process_user_check(message: types.Message, query: str):
    clean_query = html.escape(query.strip())
    status, data = await check_entity(query)
    
    if status == "scammer":
        reason = html.escape(data['reason'])
        proof = html.escape(data['proof'])
        text = (
            f"<b>ВНИМАНИЕ! ПОЛЬЗОВАТЕЛЬ СКАМЕР</b>\n\n"
            f"<b>Идентификатор:</b> <code>{clean_query}</code>\n"
            f"<b>Причина занесения:</b> <i>{reason}</i>\n"
            f"<b>Доказательства:</b> {proof}\n\n"
            f"<blockquote><u>Категорически не рекомендуем совершать любые сделки с данным объектом.</u></blockquote>"
        )
        await send_banner_response(message, "scam", text)

    elif status == "guarantor":
        g_name = html.escape(data['name'])
        g_ident = html.escape(data['identifier'])
        g_deposit = html.escape(data['deposit'])
        g_desc = html.escape(data['description'])
        text = (
            f"<b>ВЕРИФИЦИРОВАННЫЙ ГАРАНТ</b>\n\n"
            f"<b>Имя/Проект:</b> <b>{g_name}</b>\n"
            f"<b>Идентификатор:</b> <code>{g_ident}</code>\n"
            f"<b>Страховой депозит:</b> <u>{g_deposit}</u>\n"
            f"<b>Описание:</b> <i>{g_desc}</i>\n\n"
            f"<blockquote>Сделки с данным лицом подлежат стандартной защите сервиса.</blockquote>"
        )
        await send_banner_response(message, "trusted", text)

    elif status == "trusted":
        note = html.escape(data['note'])
        text = (
            f"<b>НАДЕЖНЫЙ ПОЛЬЗОВАТЕЛЬ</b>\n\n"
            f"<b>Идентификатор:</b> <code>{clean_query}</code>\n"
            f"<b>Примечание:</b> <i>{note}</i>\n\n"
            f"<blockquote>Пользователь прошёл первичную верификацию и не имеет зафиксированных жалоб.</blockquote>"
        )
        await send_banner_response(message, "trusted", text)

    else:
        text = (
            f"<b>НЕИЗВЕСТНЫЙ ПОЛЬЗОВАТЕЛЬ</b>\n\n"
            f"<b>Идентификатор:</b> <code>{clean_query}</code>\n\n"
            f"<blockquote>Данный объект отсутствует в базе данных. Будьте внимательны при проведении финансовых операций и используйте официальных гарантов.</blockquote>"
        )
        await send_banner_response(message, "unknown", text)

# Разделенная обработка нажатия кнопки и команды /check
@dp.message(F.text == "🔎 Проверить пользователя")
async def btn_check_user(message: types.Message, state: FSMContext):
    await state.clear()
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        identifier = f"@{target.username}" if target.username else str(target.id)
        await process_user_check(message, identifier)
        return

    await state.set_state(CheckState.input_entity)
    await message.answer(
        "<b>ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ</b>\n\n"
        "<blockquote>Введите <b>@username</b> или <b>ID пользователя</b> для поиска в базе данных.</blockquote>"
    )

@dp.message(Command("check"))
async def cmd_check_user(message: types.Message, state: FSMContext):
    await state.clear()
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        await process_user_check(message, args[1])
        return

    await state.set_state(CheckState.input_entity)
    await message.answer(
        "<b>ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ</b>\n\n"
        "<blockquote>Введите <b>@username</b> или <b>ID пользователя</b> для поиска в базе данных.</blockquote>"
    )

@dp.message(CheckState.input_entity)
async def process_check_input(message: types.Message, state: FSMContext):
    await state.clear()
    await process_user_check(message, message.text)

# --- Список гарантов ---
@dp.message(F.text == "🛡 Список гарантов")
@dp.message(Command("guarantors"))
async def show_guarantors(message: types.Message, state: FSMContext):
    await state.clear()
    guarantors = await get_guarantors()
    if not guarantors:
        await message.answer("<b>СПИСОК ГАРАНТОВ</b>\n\n<blockquote>На данный момент список проверенных гарантов пуст.</blockquote>")
        return

    text = "<b>РЕЕСТР НАДЕЖНЫХ ГАРАНТОВ</b>\n\n"
    for idx, g in enumerate(guarantors, 1):
        g_name = html.escape(g['name'])
        g_ident = html.escape(g['identifier'])
        g_deposit = html.escape(g['deposit'])
        g_desc = html.escape(g['description'])
        text += (
            f"<b>{idx}. {g_name}</b> (<code>{g_ident}</code>)\n"
            f"• <b>Депозит:</b> <u>{g_deposit}</u>\n"
            f"• <b>Информация:</b> <i>{g_desc}</i>\n\n"
        )
    text += "<blockquote>Совершайте сделки исключительно через официальные контакты гарантов.</blockquote>"
    await message.answer(text)

# --- Проверка реквизитов ---
@dp.message(F.text == "🔗 Проверка реквизитов")
async def check_requisites_info(message: types.Message, state: FSMContext):
    await state.clear()
    text = (
        "<b>АВТОМАТИЗИРОВАННЫЙ АНТИФИШИНГ</b>\n\n"
        "<blockquote>Отправьте в этот чат номер карты, крипто-кошелек или ссылку для мгновенной сверки с черным списком.</blockquote>\n\n"
        "<u>Поддерживаемые форматы:</u>\n"
        "• Банковские карты (16 цифр)\n"
        "• USDT / BTC / ETH кошельки\n"
        "• Домены и фишинг-ссылки"
    )
    await message.answer(text)

# --- Подача жалобы ---
@dp.message(F.text == "📩 Подать жалобу")
async def start_complaint(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(ComplaintState.target)
    await message.answer(
        "<b>ПОДАЧА ЖАЛОБЫ — ШАГ 1/3</b>\n\n"
        "<blockquote>Укажите <b>@username</b> или <b>ID</b> нарушителя.</blockquote>"
    )

@dp.message(ComplaintState.target)
async def complaint_target(message: types.Message, state: FSMContext):
    await state.update_data(target=message.text)
    await state.set_state(ComplaintState.description)
    await message.answer(
        "<b>ПОДАЧА ЖАЛОБЫ — ШАГ 2/3</b>\n\n"
        "<blockquote>Подробно опишите ситуацию и суть мошенничества.</blockquote>"
    )

@dp.message(ComplaintState.description)
async def complaint_desc(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(ComplaintState.proof)
    await message.answer(
        "<b>ПОДАЧА ЖАЛОБЫ — ШАГ 3/3</b>\n\n"
        "<blockquote>Предоставьте ссылки на доказательства (Telegraph, Imgur, скриншоты или переписку).</blockquote>"
    )

@dp.message(ComplaintState.proof)
async def complaint_proof(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    complaint_id = await add_complaint(
        reporter_id=message.from_user.id,
        target=data['target'],
        description=data['description'],
        proof=message.text
    )
    
    await message.answer(
        "<b>ЖАЛОБА УСПЕШНО ЗАРЕГИСТРИРОВАНА</b>\n\n"
        f"<b>Номер заявки:</b> <code>#{complaint_id}</code>\n"
        "<blockquote>Ваша жалоба отправлена на рассмотрение модераторам. В случае подтверждения факта скама объект будет внесён в черную базу.</blockquote>"
    )

# --- Логика Администратора ---

@dp.callback_query(F.data == "admin_add_scam")
async def admin_add_scam_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminState.add_scam_target)
    await call.message.answer("<b>АДМИН-ПАНЕЛЬ: ВНЕСЕНИЕ СКАМЕРА</b>\n\nВведите ID или Username нарушителя:")
    await call.answer()

@dp.message(AdminState.add_scam_target)
async def admin_add_scam_target(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.update_data(target=message.text)
    await state.set_state(AdminState.add_scam_reason)
    await message.answer("Укажите причину внесения в ЧС:")

@dp.message(AdminState.add_scam_reason)
async def admin_add_scam_reason(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.update_data(reason=message.text)
    await state.set_state(AdminState.add_scam_proof)
    await message.answer("Прикрепите ссылку на доказательства (или напишите 'Нет'):")

@dp.message(AdminState.add_scam_proof)
async def admin_add_scam_proof(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    await add_scammer(data['target'], data['reason'], message.text)
    await state.clear()
    clean_target = html.escape(data['target'])
    await message.answer(f"<b>ОБЪЕКТ ЗАНЕСЕН В ЧЕРНЫЙ СПИСОК</b>\n\nИдентификатор: <code>{clean_target}</code>")

@dp.callback_query(F.data == "admin_add_trust")
async def admin_add_trust_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminState.add_trust_target)
    await call.message.answer("<b>АДМИН-ПАНЕЛЬ: НАДЕЖНЫЙ ПОЛЬЗОВАТЕЛЬ</b>\n\nВведите ID или Username:")
    await call.answer()

@dp.message(AdminState.add_trust_target)
async def admin_add_trust_target(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await add_trusted_user(message.text)
    await state.clear()
    clean_target = html.escape(message.text)
    await message.answer(f"<b>ПОЛЬЗОВАТЕЛЬ ВЕРИФИЦИРОВАН</b>\n\nИдентификатор: <code>{clean_target}</code>")

@dp.callback_query(F.data == "admin_add_guarantor")
async def admin_add_guarantor_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminState.add_guarantor_target)
    await call.message.answer("<b>ДОБАВЛЕНИЕ ГАРАНТА</b>\n\nВведите ID или Username гаранта:")
    await call.answer()

@dp.message(AdminState.add_guarantor_target)
async def admin_add_g_target(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.update_data(target=message.text)
    await state.set_state(AdminState.add_guarantor_name)
    await message.answer("Укажите имя или название гарант-сервиса:")

@dp.message(AdminState.add_guarantor_name)
async def admin_add_g_name(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.update_data(name=message.text)
    await state.set_state(AdminState.add_guarantor_desc)
    await message.answer("Введите краткое описание гаранта:")

@dp.message(AdminState.add_guarantor_desc)
async def admin_add_g_desc(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.update_data(desc=message.text)
    await state.set_state(AdminState.add_guarantor_deposit)
    await message.answer("Укажите размер депозита (например, 5,000 $):")

@dp.message(AdminState.add_guarantor_deposit)
async def admin_add_g_deposit(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    await add_guarantor(data['target'], data['name'], data['desc'], message.text)
    await state.clear()
    clean_name = html.escape(data['name'])
    await message.answer(f"<b>ГАРАНТ УСПЕШНО ДОБАВЛЕН В РЕЕСТР</b>\n\nИмя: <b>{clean_name}</b>")

@dp.callback_query(F.data == "admin_view_complaints")
async def admin_view_complaints(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return
    complaints = await get_pending_complaints()
    if not complaints:
        await call.message.answer("<b>НОВЫХ ЖАЛОБ НЕТ</b>")
        await call.answer()
        return

    c = complaints[0]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Забанить (Скамер)", callback_data=f"complaint_ban_{c['id']}"),
                InlineKeyboardButton(text="🗑 Отклонить", callback_data=f"complaint_reject_{c['id']}")
            ]
        ]
    )
    text = (
        f"<b>ЖАЛОБА #{c['id']}</b>\n\n"
        f"• <b>Отправитель:</b> <code>{c['reporter_id']}</code>\n"
        f"• <b>Нарушитель:</b> <code>{html.escape(c['target'])}</code>\n"
        f"• <b>Описание:</b> <i>{html.escape(c['description'])}</i>\n"
        f"• <b>Доказательства:</b> {html.escape(c['proof'])}"
    )
    await call.message.answer(text, reply_markup=keyboard)
    await call.answer()

@dp.callback_query(F.data.startswith("complaint_ban_"))
async def process_complaint_ban(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return
    c_id = int(call.data.split("_")[2])
    c = await get_complaint_by_id(c_id)
    if c:
        await add_scammer(c['target'], f"Жалоба #{c_id}: {c['description']}", c['proof'])
        await resolve_complaint(c_id, "approved")
        await call.message.edit_text(f"<b>ЖАЛОБА #{c_id} ОДОБРЕНА. ОБЪЕКТ ЗАНЕСЕН В ЧС.</b>")
    await call.answer()

@dp.callback_query(F.data.startswith("complaint_reject_"))
async def process_complaint_reject(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return
    c_id = int(call.data.split("_")[2])
    await resolve_complaint(c_id, "rejected")
    await call.message.edit_text(f"<b>ЖАЛОБА #{c_id} ОТКЛОНЕНА.</b>")
    await call.answer()

# --- Авто-проверка сообщений в группах ---
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def auto_chat_shield(message: types.Message):
    if not message.from_user:
        return
    
    user_id = str(message.from_user.id)
    username = f"@{message.from_user.username}" if message.from_user.username else None

    status_id, data_id = await check_entity(user_id)
    status_un, data_un = await check_entity(username) if username else ("unknown", None)

    if status_id == "scammer" or status_un == "scammer":
        data = data_id if status_id == "scammer" else data_un
        reason = html.escape(data['reason'])
        proof = html.escape(data['proof'])
        warn_text = (
            f"<b>ОПАСНОСТЬ! В ЧАТЕ ОБНАРУЖЕН СКАМЕР!</b>\n\n"
            f"<b>Пользователь:</b> {message.from_user.mention_html()}\n"
            f"<b>Причина ЧС:</b> <i>{reason}</i>\n"
            f"<b>Доказательства:</b> {proof}\n\n"
            f"<blockquote>Будьте осторожны! Данный участник находится в реестре мошенников.</blockquote>"
        )
        file_path = BANNERS.get("scam")
        if file_path and os.path.exists(file_path):
            await message.reply_photo(photo=FSInputFile(file_path), caption=warn_text)
        else:
            await message.reply(warn_text)

# ==========================================
# 5. ТОЧКА ВХОДА
# ==========================================
async def main():
    await init_db()
    logging.info("База данных успешно инициализирована.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

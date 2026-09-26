import asyncio
import os
import html
import logging
import re
from typing import Optional, Dict, List, Tuple

import libsql_client
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, BotCommand,
    LinkPreviewOptions, URLInputFile
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiohttp import web

# ==========================================
# 1. КОНФИГУРАЦИЯ И НАСТРОЙКИ
# ==========================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")

# Настройки базы данных Turso
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", os.getenv("DB_PATH", "file:antiscam.db"))
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", None)

def get_turso_client():
    return libsql_client.create_client_async(
        url=TURSO_DATABASE_URL,
        auth_token=TURSO_AUTH_TOKEN if TURSO_AUTH_TOKEN else None
    )

def row_to_dict(rs, row) -> dict:
    """Вспомогательная функция конвертации строки Turso в словарь"""
    return {col: val for col, val in zip(rs.columns, row)}

# Список ID главных администраторов из переменных окружения
raw_admins = os.getenv("ADMIN_IDS", "")
ENV_ADMIN_IDS = [int(admin_id.strip()) for admin_id in raw_admins.split(",") if admin_id.strip().isdigit()]

# Прямые ссылки на баннеры из Postimages
BANNERS = {
    "welcome": "https://i.postimg.cc/1XDvqQFf/Bez-nazvania134-20260924150439.png",
    "unknown": "https://i.postimg.cc/Z55LT3wP/Bez-nazvania132-20260924143850.png",
    "trusted": "https://i.postimg.cc/CMb7JwZR/Bez-nazvania131-20260924142417.png",
    "scam":    "https://i.postimg.cc/HLJ4MmDV/Bez-nazvania130-20260924141235.png"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# ==========================================
# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И КОМАНДЫ
# ==========================================
def normalize_id(ident: str) -> str:
    """Нормализация идентификаторов (юзернеймы, ID, карты, кошельки)"""
    ident = ident.strip()
    if ident.startswith("@"):
        return ident.lower()
    clean_num = re.sub(r'[\s\-]+', '', ident)
    if clean_num.isdigit():
        return clean_num
    return ident

def format_profile_link(identifier: str, label: Optional[str] = None) -> str:
    """Формирует рабочую HTML-ссылку на профиль Telegram"""
    clean_id = identifier.strip()
    if clean_id.startswith("@"):
        username = clean_id[1:]
        text = label if label else clean_id
        return f'<a href="https://t.me/{username}">{html.escape(text)}</a>'
    elif clean_id.isdigit():
        text = label if label else f"ID: {clean_id}"
        return f'<a href="tg://user?id={clean_id}">{html.escape(text)}</a>'
    else:
        text = label if label else clean_id
        return html.escape(text)

async def set_bot_commands(bot_instance: Bot):
    """Установка меню подсказок для команд (при вводе /)"""
    commands = [
        BotCommand(command="start", description="🚀 Запустить бота / Главное меню"),
        BotCommand(command="check", description="🔎 Проверить пользователя"),
        BotCommand(command="guarantors", description="🛡 Реестр проверенных гарантов"),
        BotCommand(command="admin", description="👑 Панель администратора")
    ]
    await bot_instance.set_my_commands(commands)


# ==========================================
# 3. РАБОТА С БАЗОЙ ДАННЫХ (TURSO / LIBSQL)
# ==========================================
async def init_db():
    async with get_turso_client() as client:
        await client.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await client.execute("""
            CREATE TABLE IF NOT EXISTS db_admins (
                user_id INTEGER PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await client.execute("""
            CREATE TABLE IF NOT EXISTS scammers (
                identifier TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                proof TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await client.execute("""
            CREATE TABLE IF NOT EXISTS trusted_users (
                identifier TEXT PRIMARY KEY,
                note TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await client.execute("""
            CREATE TABLE IF NOT EXISTS guarantors (
                identifier TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                deposit TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await client.execute("""
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

async def register_user(user: types.User):
    """Регистрация пользователя для рассылок"""
    async with get_turso_client() as client:
        await client.execute(
            "INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)",
            [user.id, user.username]
        )

async def get_all_users() -> List[int]:
    """Получение списка всех ID пользователей для рассылки"""
    async with get_turso_client() as client:
        rs = await client.execute("SELECT user_id FROM users")
        return [int(row[0]) for row in rs.rows]

async def is_admin(user_id: int) -> bool:
    """Проверка прав администратора (из ENV и из БД)"""
    if user_id in ENV_ADMIN_IDS:
        return True
    async with get_turso_client() as client:
        rs = await client.execute("SELECT 1 FROM db_admins WHERE user_id = ?", [user_id])
        return len(rs.rows) > 0

async def add_admin_db(user_id: int):
    async with get_turso_client() as client:
        await client.execute("INSERT OR IGNORE INTO db_admins (user_id) VALUES (?)", [user_id])

async def remove_admin_db(user_id: int):
    async with get_turso_client() as client:
        await client.execute("DELETE FROM db_admins WHERE user_id = ?", [user_id])

async def get_db_admins() -> List[int]:
    async with get_turso_client() as client:
        rs = await client.execute("SELECT user_id FROM db_admins")
        return [int(row[0]) for row in rs.rows]

async def check_entity(ident: str) -> Tuple[str, Optional[Dict]]:
    ident = normalize_id(ident)
    async with get_turso_client() as client:
        rs = await client.execute("SELECT * FROM scammers WHERE identifier = ?", [ident])
        if rs.rows:
            return "scammer", row_to_dict(rs, rs.rows[0])
            
        rs = await client.execute("SELECT * FROM guarantors WHERE identifier = ?", [ident])
        if rs.rows:
            return "guarantor", row_to_dict(rs, rs.rows[0])

        rs = await client.execute("SELECT * FROM trusted_users WHERE identifier = ?", [ident])
        if rs.rows:
            return "trusted", row_to_dict(rs, rs.rows[0])

        return "unknown", None

async def add_scammer(identifier: str, reason: str, proof: str = "Не указаны"):
    ident = normalize_id(identifier)
    async with get_turso_client() as client:
        await client.execute(
            "INSERT OR REPLACE INTO scammers (identifier, reason, proof) VALUES (?, ?, ?)",
            [ident, reason, proof]
        )
        await client.execute("DELETE FROM trusted_users WHERE identifier = ?", [ident])
        await client.execute("DELETE FROM guarantors WHERE identifier = ?", [ident])

async def add_trusted_user(identifier: str, note: str = "Проверен"):
    ident = normalize_id(identifier)
    async with get_turso_client() as client:
        await client.execute(
            "INSERT OR REPLACE INTO trusted_users (identifier, note) VALUES (?, ?)",
            [ident, note]
        )
        await client.execute("DELETE FROM scammers WHERE identifier = ?", [ident])

async def add_guarantor(identifier: str, name: str, description: str, deposit: str = "Отсутствует"):
    ident = normalize_id(identifier)
    async with get_turso_client() as client:
        await client.execute(
            "INSERT OR REPLACE INTO guarantors (identifier, name, description, deposit) VALUES (?, ?, ?, ?)",
            [ident, name, description, deposit]
        )
        await client.execute("DELETE FROM scammers WHERE identifier = ?", [ident])

async def get_guarantors() -> List[Dict]:
    async with get_turso_client() as client:
        rs = await client.execute("SELECT * FROM guarantors ORDER BY added_at DESC")
        return [row_to_dict(rs, row) for row in rs.rows]

async def add_complaint(reporter_id: int, target: str, description: str, proof: str) -> int:
    target = normalize_id(target)
    async with get_turso_client() as client:
        rs = await client.execute(
            "INSERT INTO complaints (reporter_id, target, description, proof) VALUES (?, ?, ?, ?)",
            [reporter_id, target, description, proof]
        )
        return rs.last_insert_rowid

async def get_pending_complaints() -> List[Dict]:
    async with get_turso_client() as client:
        rs = await client.execute("SELECT * FROM complaints WHERE status = 'pending' ORDER BY id ASC")
        return [row_to_dict(rs, row) for row in rs.rows]

async def get_complaint_by_id(complaint_id: int) -> Optional[Dict]:
    async with get_turso_client() as client:
        rs = await client.execute("SELECT * FROM complaints WHERE id = ?", [complaint_id])
        return row_to_dict(rs, rs.rows[0]) if rs.rows else None

async def resolve_complaint(complaint_id: int, status: str):
    async with get_turso_client() as client:
        await client.execute("UPDATE complaints SET status = ? WHERE id = ?", [status, complaint_id])


# ==========================================
# 4. FSM СОСТОЯНИЯ И КЛАВИАТУРЫ
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

    broadcast_message = State()
    add_admin_id = State()
    remove_admin_id = State()

class CheckState(StatesGroup):
    input_entity = State()

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Проверить пользователя"), KeyboardButton(text="🛡 Список гарантов")],
            [KeyboardButton(text="📩 Подать жалобу")]
        ],
        resize_keyboard=True
    )

def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить скамера", callback_data="admin_add_scam")],
            [InlineKeyboardButton(text="➕ Добавить надежного", callback_data="admin_add_trust")],
            [InlineKeyboardButton(text="➕ Добавить гаранта", callback_data="admin_add_guarantor")],
            [InlineKeyboardButton(text="📑 Жалобы на рассмотрении", callback_data="admin_view_complaints")],
            [InlineKeyboardButton(text="📢 Рассылка пользователям", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="👑 Управление админами", callback_data="admin_manage_admins")]
        ]
    )

def admin_manage_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Назначить админа", callback_data="admin_add_admin_btn")],
            [InlineKeyboardButton(text="❌ Снять админа", callback_data="admin_remove_admin_btn")],
            [InlineKeyboardButton(text="📋 Список всех админов", callback_data="admin_list_admins_btn")]
        ]
    )

# --- ИСПРАВЛЕННАЯ ФУНКЦИЯ ОТПРАВКИ БАННЕРОВ С ИСПОЛЬЗОВАНИЕМ URLInputFile ---
async def send_banner_response(message: types.Message, banner_key: str, caption_text: str, reply_markup=None):
    photo_url = BANNERS.get(banner_key)
    link_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True, show_above_text=True)
    
    if photo_url and photo_url.startswith("http"):
        try:
            photo_file = URLInputFile(photo_url)
            await message.answer_photo(photo=photo_file, caption=caption_text, reply_markup=reply_markup)
            return
        except Exception as e:
            logging.error(f"Ошибка отправки баннера {banner_key}: {e}")
            
    await message.answer(text=caption_text, reply_markup=reply_markup, link_preview_options=link_options)


# ==========================================
# 5. ОБРАБОТЧИКИ КОМАНД И СООБЩЕНИЙ
# ==========================================

# --- /start ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user:
        await register_user(message.from_user)
        
    text = (
        "<b>FraudX Base | ЕДИНАЯ АНТИ-СКАМ БАЗА</b>\n\n"
        "<b>Автоматизированный сервис проверки контрагентов, поиска злоумышленников и реестр верифицированных гарантов.</b>\n\n"
        "<u>Доступные возможности:</u>\n"
        "• <b>Проверка пользователей</b> по ID или Username\n"
        "• <b>Авто-защита чатов</b> при отправке сообщений\n"
        "• <b>Реестр проверенных гарантов</b> с прямыми ссылками\n"
        "• <b>Подача официальных жалоб</b> с доказательствами\n\n"
        "<b>Используйте нижнее меню FraudX Base для навигации.</b>"
    )
    await send_banner_response(message, "welcome", text, reply_markup=main_keyboard())

# --- /admin ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user:
        await register_user(message.from_user)
        
    if not await is_admin(message.from_user.id):
        await message.answer(
            f"<b>FraudX Base | ОТКАЗАНО В ДОСТУПЕ</b>\n\n"
            f"У вас нет прав администратора.\n"
            f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n\n"
            f"Обратитесь к главному администратору для получения доступа."
        )
        return
    text = (
        "<b>FraudX Base | ПАНЕЛЬ УПРАВЛЕНИЯ АДМИНИСТРАТОРА</b>\n\n"
        "<b>Выберите необходимый раздел для модерации базы данных.</b>"
    )
    await message.answer(text, reply_markup=admin_keyboard())

# --- Единая функция проверки объектов ---
async def process_user_check(message: types.Message, query: str):
    clean_query = html.escape(query.strip())
    search_query = normalize_id(query)

    status, data = await check_entity(search_query)
    
    if status == "unknown" and search_query != query.strip():
        status, data = await check_entity(query.strip())

    if status == "scammer":
        reason = html.escape(data['reason'])
        proof = html.escape(data['proof'])
        user_link = format_profile_link(clean_query, clean_query)
        text = (
            f"<b>FraudX Base | ВНИМАНИЕ! ОБЪЕКТ В ЧЕРНОМ СПИСКЕ</b>\n\n"
            f"<b>Идентификатор:</b> {user_link}\n"
            f"<b>Причина занесения:</b> <b>{reason}</b>\n"
            f"<b>Доказательства:</b> {proof}\n\n"
            f"<b>Категорически не рекомендуем совершать любые сделки с данным объектом.</b>"
        )
        await send_banner_response(message, "scam", text)

    elif status == "guarantor":
        g_name = html.escape(data['name'])
        g_ident = data['identifier'].strip()
        g_deposit = html.escape(data['deposit'])
        g_desc = html.escape(data['description'])
        g_link = format_profile_link(g_ident, "Открыть профиль ↗️")
        
        text = (
            f"<b>FraudX Base | ВЕРИФИЦИРОВАННЫЙ ГАРАНТ</b>\n\n"
            f"<b>Имя/Проект:</b> <b>{g_name}</b>\n"
            f"<b>Контакт:</b> {format_profile_link(g_ident, g_ident)}\n"
            f"<b>Страховой депозит:</b> <u>{g_deposit}</u>\n"
            f"<b>Описание:</b> <b>{g_desc}</b>\n"
            f"<b>Ссылка на профиль:</b> {g_link}\n\n"
            f"<b>Сделки с данным лицом подлежат стандартной защите сервиса FraudX Base.</b>"
        )
        await send_banner_response(message, "trusted", text)

    elif status == "trusted":
        note = html.escape(data['note'])
        user_link = format_profile_link(clean_query, clean_query)
        text = (
            f"<b>FraudX Base | НАДЕЖНЫЙ ПОЛЬЗОВАТЕЛЬ</b>\n\n"
            f"<b>Идентификатор:</b> {user_link}\n"
            f"<b>Примечание:</b> <b>{note}</b>\n\n"
            f"<b>Объект прошёл первичную верификацию и не имеет зафиксированных жалоб в системе FraudX Base.</b>"
        )
        await send_banner_response(message, "trusted", text)

    else:
        user_link = format_profile_link(clean_query, clean_query)
        text = (
            f"<b>FraudX Base | НЕИЗВЕСТНЫЙ ОБЪЕКТ</b>\n\n"
            f"<b>Идентификатор:</b> {user_link}\n\n"
            f"<b>Данный объект отсутствует в базе данных FraudX Base. Будьте внимательны при проведении финансовых операций и используйте официальных гарантов.</b>"
        )
        await send_banner_response(message, "unknown", text)

# --- Проверка пользователя ---
@dp.message(F.text == "🔎 Проверить пользователя")
async def btn_check_user(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user:
        await register_user(message.from_user)
        
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        identifier = f"@{target.username}" if target.username else str(target.id)
        await process_user_check(message, identifier)
        return

    await state.set_state(CheckState.input_entity)
    await message.answer(
        "<b>FraudX Base | ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ</b>\n\n"
        "<b>Введите @username или ID пользователя для поиска в базе данных.</b>"
    )

@dp.message(Command("check"))
async def cmd_check_user(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user:
        await register_user(message.from_user)
        
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        await process_user_check(message, args[1])
        return

    await state.set_state(CheckState.input_entity)
    await message.answer(
        "<b>FraudX Base | ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ</b>\n\n"
        "<b>Введите @username или ID пользователя для поиска в базе данных.</b>"
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
    if message.from_user:
        await register_user(message.from_user)
        
    guarantors = await get_guarantors()
    if not guarantors:
        await message.answer("<b>FraudX Base | СПИСОК ГАРАНТОВ</b>\n\n<b>На данный момент список проверенных гарантов пуст.</b>")
        return

    text = "<b>FraudX Base | РЕЕСТР НАДЕЖНЫХ ГАРАНТОВ</b>\n\n"
    for idx, g in enumerate(guarantors, 1):
        g_name = html.escape(g['name'])
        g_ident = g['identifier'].strip()
        g_deposit = html.escape(g['deposit'])
        g_desc = html.escape(g['description'])
        
        contact_link = format_profile_link(g_ident, g_ident)
        profile_btn = format_profile_link(g_ident, "👉 Перейти в профиль")
        
        text += (
            f"<b>{idx}. {g_name}</b> ({contact_link})\n"
            f"• <b>Депозит:</b> <u>{g_deposit}</u>\n"
            f"• <b>Информация:</b> <b>{g_desc}</b>\n"
            f"• <b>Ссылка:</b> {profile_btn}\n\n"
        )
    text += "<b>Совершайте сделки исключительно через официальные контакты гарантов.</b>"
    
    link_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True, show_above_text=True)
    await message.answer(text, link_preview_options=link_options)

# --- Подача жалобы ---
@dp.message(F.text == "📩 Подать жалобу")
async def start_complaint(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user:
        await register_user(message.from_user)
        
    await state.set_state(ComplaintState.target)
    await message.answer(
        "<b>FraudX Base | ПОДАЧА ЖАЛОБЫ — ШАГ 1/3</b>\n\n"
        "<b>Укажите @username или ID нарушителя.</b>"
    )

@dp.message(ComplaintState.target)
async def complaint_target(message: types.Message, state: FSMContext):
    await state.update_data(target=message.text)
    await state.set_state(ComplaintState.description)
    await message.answer(
        "<b>FraudX Base | ПОДАЧА ЖАЛОБЫ — ШАГ 2/3</b>\n\n"
        "<b>Подробно опишите ситуацию и суть мошенничества.</b>"
    )

@dp.message(ComplaintState.description)
async def complaint_desc(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(ComplaintState.proof)
    await message.answer(
        "<b>FraudX Base | ПОДАЧА ЖАЛОБЫ — ШАГ 3/3</b>\n\n"
        "<b>Предоставьте ссылки на доказательства (Telegraph, Imgur) или отправьте скриншот/файл.</b>"
    )

@dp.message(ComplaintState.proof)
async def complaint_proof(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    proof_value = "Не указаны"
    
    if message.photo:
        proof_value = f"photo:{message.photo[-1].file_id}"
    elif message.document:
        proof_value = f"doc:{message.document.file_id}"
    elif message.text:
        proof_value = message.text

    complaint_id = await add_complaint(
        reporter_id=message.from_user.id,
        target=data['target'],
        description=data['description'],
        proof=proof_value
    )
    
    await message.answer(
        "<b>FraudX Base | ЖАЛОБА УСПЕШНО ЗАРЕГИСТРИРОВАНА</b>\n\n"
        f"<b>Номер заявки:</b> <code>#{complaint_id}</code>\n"
        "<b>Ваша жалоба отправлена на рассмотрение модераторам FraudX Base. В случае подтверждения факта скама объект будет внесён в черную базу.</b>"
    )

# --- Авто-обработка любых текстовых сообщений в ЛС (С ЗАЩИТОЙ FSM) ---
@dp.message(F.chat.type == "private", F.text, StateFilter(default_state))
async def default_private_text_check(message: types.Message, state: FSMContext):
    if message.from_user:
        await register_user(message.from_user)
        
    menu_buttons = [
        "🔎 Проверить пользователя", 
        "🛡 Список гарантов", 
        "📩 Подать жалобу"
    ]
    if message.text in menu_buttons:
        return

    await process_user_check(message, message.text)


# ==========================================
# 6. ЛОГИКА АДМИНИСТРАТОРА
# ==========================================

# --- Добавление скамера ---
@dp.callback_query(F.data == "admin_add_scam")
async def admin_add_scam_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AdminState.add_scam_target)
    await call.message.answer("<b>FraudX Base | ВНЕСЕНИЕ СКАМЕРА / ЧЕРНЫЙ СПИСОК</b>\n\nВведите ID, Username, номер карты или кошелек нарушителя:")
    await call.answer()

@dp.message(AdminState.add_scam_target)
async def admin_add_scam_target(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.update_data(target=message.text)
    await state.set_state(AdminState.add_scam_reason)
    await message.answer("Укажите причину внесения в ЧС:")

@dp.message(AdminState.add_scam_reason)
async def admin_add_scam_reason(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.update_data(reason=message.text)
    await state.set_state(AdminState.add_scam_proof)
    await message.answer("Прикрепите ссылку на доказательства (или напишите 'Нет'):")

@dp.message(AdminState.add_scam_proof)
async def admin_add_scam_proof(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    await add_scammer(data['target'], data['reason'], message.text)
    await state.clear()
    clean_target = html.escape(data['target'])
    await message.answer(f"<b>FraudX Base | ОБЪЕКТ ЗАНЕСЕН В ЧЕРНЫЙ СПИСОК</b>\n\nИдентификатор: <code>{clean_target}</code>")

# --- Добавление проверенного ---
@dp.callback_query(F.data == "admin_add_trust")
async def admin_add_trust_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AdminState.add_trust_target)
    await call.message.answer("<b>FraudX Base | НАДЕЖНЫЙ ПОЛЬЗОВАТЕЛЬ</b>\n\nВведите ID, Username или реквизит:")
    await call.answer()

@dp.message(AdminState.add_trust_target)
async def admin_add_trust_target(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await add_trusted_user(message.text)
    await state.clear()
    clean_target = html.escape(message.text)
    await message.answer(f"<b>FraudX Base | ПОЛЬЗОВАТЕЛЬ ВЕРИФИЦИРОВАН</b>\n\nИдентификатор: <code>{clean_target}</code>")

# --- Добавление гаранта ---
@dp.callback_query(F.data == "admin_add_guarantor")
async def admin_add_guarantor_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AdminState.add_guarantor_target)
    await call.message.answer("<b>FraudX Base | ДОБАВЛЕНИЕ ГАРАНТА</b>\n\nВведите ID или Username гаранта (например @username):")
    await call.answer()

@dp.message(AdminState.add_guarantor_target)
async def admin_add_g_target(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.update_data(target=message.text)
    await state.set_state(AdminState.add_guarantor_name)
    await message.answer("Укажите имя или название гарант-сервиса:")

@dp.message(AdminState.add_guarantor_name)
async def admin_add_g_name(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.update_data(name=message.text)
    await state.set_state(AdminState.add_guarantor_desc)
    await message.answer("Введите краткое описание гаранта:")

@dp.message(AdminState.add_guarantor_desc)
async def admin_add_g_desc(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.update_data(desc=message.text)
    await state.set_state(AdminState.add_guarantor_deposit)
    await message.answer("Укажите размер депозита (например, 5,000 $):")

@dp.message(AdminState.add_guarantor_deposit)
async def admin_add_g_deposit(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    await add_guarantor(data['target'], data['name'], data['desc'], message.text)
    await state.clear()
    clean_name = html.escape(data['name'])
    await message.answer(f"<b>FraudX Base | ГАРАНТ УСПЕШНО ДОБАВЛЕН В РЕЕСТР</b>\n\nИмя: <b>{clean_name}</b>")

# --- Рассмотрение жалоб ---
@dp.callback_query(F.data == "admin_view_complaints")
async def admin_view_complaints(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("У вас нет прав!", show_alert=True)
        return

    await call.answer()

    try:
        complaints = await get_pending_complaints()
        if not complaints:
            await call.message.answer("<b>FraudX Base | НОВЫХ ЖАЛОБ НЕТ</b>")
            return

        c = complaints[0]
        
        target_text = html.escape(str(c.get('target') or 'Не указан'))
        desc_text = html.escape(str(c.get('description') or 'Без описания'))
        raw_proof = str(c.get('proof') or '')

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="❌ Забанить (Скамер)", callback_data=f"complaint_ban_{c['id']}"),
                    InlineKeyboardButton(text="🗑 Отклонить", callback_data=f"complaint_reject_{c['id']}")
                ]
            ]
        )

        caption_text = (
            f"<b>FraudX Base | ЖАЛОБА #{c['id']}</b>\n\n"
            f"• <b>Отправитель:</b> <code>{c['reporter_id']}</code>\n"
            f"• <b>Нарушитель:</b> <code>{target_text}</code>\n"
            f"• <b>Описание:</b> <b>{desc_text}</b>\n"
        )

        if raw_proof.startswith("photo:"):
            photo_file_id = raw_proof.replace("photo:", "")
            caption_text += "• <b>Доказательства:</b> <i>Прикреплено фото 🖼</i>"
            await call.message.answer_photo(photo=photo_file_id, caption=caption_text, reply_markup=keyboard)
        elif raw_proof.startswith("doc:"):
            doc_file_id = raw_proof.replace("doc:", "")
            caption_text += "• <b>Доказательства:</b> <i>Прикреплен документ 📁</i>"
            await call.message.answer_document(document=doc_file_id, caption=caption_text, reply_markup=keyboard)
        else:
            proof_text = html.escape(raw_proof if raw_proof else "Не указаны")
            caption_text += f"• <b>Доказательства:</b> {proof_text}"
            await call.message.answer(caption_text, reply_markup=keyboard)

    except Exception as e:
        logging.error(f"Ошибка при просмотре жалоб: {e}")
        await call.message.answer(f"⚠️ <b>Ошибка при загрузке жалобы:</b> <code>{html.escape(str(e))}</code>")

@dp.callback_query(F.data.startswith("complaint_ban_"))
async def process_complaint_ban(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("У вас нет прав!", show_alert=True)
        return

    await call.answer()

    try:
        c_id = int(call.data.split("_")[2])
        c = await get_complaint_by_id(c_id)
        
        if c:
            proof = c.get('proof') or "Не указаны"
            target = c.get('target') or "Неизвестно"
            desc = c.get('description') or "Без описания"

            await add_scammer(target, f"Жалоба #{c_id}: {desc}", proof)
            await resolve_complaint(c_id, "approved")

            result_text = f"<b>FraudX Base | ЖАЛОБА #{c_id} ОДОБРЕНА</b>\n\nОбъект <code>{html.escape(str(target))}</code> занесён в ЧС!"
            
            if call.message.caption:
                await call.message.edit_caption(caption=result_text)
            else:
                await call.message.edit_text(text=result_text)
        else:
            await call.message.answer("⚠️ Жалоба не найдена или уже обработана.")

    except Exception as e:
        logging.error(f"Ошибка при блокировке: {e}")
        await call.message.answer(f"⚠️ <b>Ошибка при обработке:</b> <code>{html.escape(str(e))}</code>")

@dp.callback_query(F.data.startswith("complaint_reject_"))
async def process_complaint_reject(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("У вас нет прав!", show_alert=True)
        return

    await call.answer()

    try:
        c_id = int(call.data.split("_")[2])
        await resolve_complaint(c_id, "rejected")

        result_text = f"<b>FraudX Base | ЖАЛОБА #{c_id} ОТКЛОНЕНА</b>"

        if call.message.caption:
            await call.message.edit_caption(caption=result_text)
        else:
            await call.message.edit_text(text=result_text)

    except Exception as e:
        logging.error(f"Ошибка при отклонении: {e}")
        await call.message.answer(f"⚠️ <b>Ошибка при обработке:</b> <code>{html.escape(str(e))}</code>")

# --- ФУНКЦИЯ РАССЫЛКИ ---
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AdminState.broadcast_message)
    await call.message.answer(
        "<b>FraudX Base | МАССОВАЯ РАССЫЛКА</b>\n\n"
        "Отправьте сообщение (текст, фото, видео или пост), которое хотите разослать всем пользователям бота:"
    )
    await call.answer()

@dp.message(AdminState.broadcast_message)
async def admin_broadcast_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    
    users = await get_all_users()
    if not users:
        await message.answer("В базе нет зарегистрированных пользователей для рассылки.")
        return

    status_msg = await message.answer(f"⏳ Рассылка начата для {len(users)} пользователей...")
    
    success = 0
    blocked = 0
    errors = 0

    for u_id in users:
        try:
            await bot.copy_message(
                chat_id=u_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            success += 1
            await asyncio.sleep(0.04)
        except (TelegramForbiddenError, TelegramBadRequest):
            blocked += 1
        except Exception as e:
            logging.error(f"Ошибка при рассылке пользователю {u_id}: {e}")
            errors += 1

    await status_msg.edit_text(
        f"<b>FraudX Base | РАССЫЛКА ЗАВЕРШЕНА</b>\n\n"
        f"✅ <b>Доставлено:</b> {success}\n"
        f"🚫 <b>Заблокировали бота:</b> {blocked}\n"
        f"⚠️ <b>Ошибки отправки:</b> {errors}\n"
        f"📊 <b>Всего в базе:</b> {len(users)}"
    )

# --- УПРАВЛЕНИЕ АДМИНИСТРАТОРАМИ ---
@dp.callback_query(F.data == "admin_manage_admins")
async def admin_manage_menu(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    await call.message.answer(
        "<b>FraudX Base | УПРАВЛЕНИЕ АДМИНИСТРАТОРАМИ</b>\n\n"
        "Вы можете добавлять и удалять администраторов системы:",
        reply_markup=admin_manage_keyboard()
    )
    await call.answer()

@dp.callback_query(F.data == "admin_add_admin_btn")
async def admin_add_admin_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AdminState.add_admin_id)
    await call.message.answer(
        "<b>НАЗНАЧЕНИЕ АДМИНИСТРАТОРА</b>\n\n"
        "Введите <b>Telegram ID</b> нового администратора (только цифры):"
    )
    await call.answer()

@dp.message(AdminState.add_admin_id)
async def admin_add_admin_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("❌ ID должен состоять только из цифр. Попробуйте еще раз:")
        return
    
    new_admin_id = int(text)
    await add_admin_db(new_admin_id)
    await state.clear()
    await message.answer(f"✅ Пользователь с ID <code>{new_admin_id}</code> успешно назначен администратором FraudX Base!")

@dp.callback_query(F.data == "admin_remove_admin_btn")
async def admin_remove_admin_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return
    await state.set_state(AdminState.remove_admin_id)
    await call.message.answer(
        "<b>СНЯТИЕ АДМИНИСТРАТОРА</b>\n\n"
        "Введите <b>Telegram ID</b> администратора, которого хотите снять:"
    )
    await call.answer()

@dp.message(AdminState.remove_admin_id)
async def admin_remove_admin_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("❌ ID должен состоять только из цифр. Попробуйте еще раз:")
        return
    
    rem_id = int(text)
    await remove_admin_db(rem_id)
    await state.clear()
    await message.answer(f"🗑 Права администратора у пользователя с ID <code>{rem_id}</code> успешно отозваны.")

@dp.callback_query(F.data == "admin_list_admins_btn")
async def admin_list_admins(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    
    db_admins = await get_db_admins()
    all_admins = list(set(ENV_ADMIN_IDS + db_admins))
    
    text = "<b>FraudX Base | СПИСОК АДМИНИСТРАТОРОВ</b>\n\n"
    for idx, a_id in enumerate(all_admins, 1):
        tag = " (Главный)" if a_id in ENV_ADMIN_IDS else ""
        text += f"{idx}. <code>{a_id}</code>{tag}\n"
        
    await call.message.answer(text)
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
            f"<b>FraudX Base | ОПАСНОСТЬ! В ЧАТЕ ОБНАРУЖЕН СКАМЕР!</b>\n\n"
            f"<b>Пользователь:</b> {message.from_user.mention_html()}\n"
            f"<b>Причина ЧС:</b> <b>{reason}</b>\n"
            f"<b>Доказательства:</b> {proof}\n\n"
            f"<b>Будьте осторожны! Данный участник находится в реестре мошенников FraudX Base.</b>"
        )
        photo_url = BANNERS.get("scam")
        if photo_url and photo_url.startswith("http"):
            try:
                photo_file = URLInputFile(photo_url)
                await message.reply_photo(photo=photo_file, caption=warn_text)
                return
            except Exception as e:
                logging.error(f"Ошибка отправки фото скамера: {e}")
                
        await message.reply(warn_text)

# ==========================================
# 7. ТОЧКА ВХОДА И ВЕБ-СЕРВЕР ДЛЯ RENDER
# ==========================================
async def handle_ping(request):
    """Простой обработчик для веб-сервера (чтобы Render видел открытый порт)"""
    return web.Response(text="Bot is running!")

async def start_web_server():
    """Запуск фонового веб-сервера aiohttp"""
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    
    await site.start()
    logging.info(f"Web server started on port {port} to satisfy Render requirements.")

async def main():
    await init_db()
    await set_bot_commands(bot)
    logging.info("База данных FraudX Base (Turso) и меню команд успешно инициализированы.")
    
    asyncio.create_task(start_web_server())
    
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())

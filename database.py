import os
import re
from typing import Optional, Dict, List, Tuple
import libsql_client
from dotenv import load_dotenv

load_dotenv()

TURSO_URL = os.getenv("TURSO_DATABASE_URL", os.getenv("DB_PATH", "file:antiscam.db"))
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", None)

def get_client():
    """Создаёт асинхронный клиент для подключения к Turso"""
    return libsql_client.create_client_async(
        url=TURSO_URL,
        auth_token=TURSO_TOKEN if TURSO_TOKEN else None
    )

def row_to_dict(rs, row) -> dict:
    """Конвертация строки Turso в словарь"""
    return {col: val for col, val in zip(rs.columns, row)}

def normalize_id(ident: str) -> str:
    """Нормализация идентификаторов (юзернеймы, ID, карты, кошельки)"""
    ident = ident.strip()
    if ident.startswith("@"):
        return ident.lower()
    clean_num = re.sub(r'[\s\-]+', '', ident)
    if clean_num.isdigit():
        return clean_num
    return ident


# ==========================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
# ==========================================
async def init_db():
    """Инициализация таблиц базы данных"""
    async with get_client() as client:
        # Пользователи (для рассылок)
        await client.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Администраторы (из БД)
        await client.execute("""
            CREATE TABLE IF NOT EXISTS db_admins (
                user_id INTEGER PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Скамеры
        await client.execute("""
            CREATE TABLE IF NOT EXISTS scammers (
                identifier TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                proof TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Проверенные пользователи
        await client.execute("""
            CREATE TABLE IF NOT EXISTS trusted_users (
                identifier TEXT PRIMARY KEY,
                note TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Гаранты
        await client.execute("""
            CREATE TABLE IF NOT EXISTS guarantors (
                identifier TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                deposit TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Жалобы
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


# ==========================================
# РАБОТА С ПОЛЬЗОВАТЕЛЯМИ И АДМИНАМИ
# ==========================================
async def register_user(user_id: int, username: Optional[str] = None):
    """Регистрация пользователя"""
    async with get_client() as client:
        await client.execute(
            "INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)",
            [user_id, username]
        )

async def get_all_users() -> List[int]:
    """Получение всех ID для рассылки"""
    async with get_client() as client:
        rs = await client.execute("SELECT user_id FROM users")
        return [int(row[0]) for row in rs.rows]

async def is_admin_db(user_id: int) -> bool:
    """Проверка прав админа в БД"""
    async with get_client() as client:
        rs = await client.execute("SELECT 1 FROM db_admins WHERE user_id = ?", [user_id])
        return len(rs.rows) > 0

async def add_admin_db(user_id: int):
    async with get_client() as client:
        await client.execute("INSERT OR IGNORE INTO db_admins (user_id) VALUES (?)", [user_id])

async def remove_admin_db(user_id: int):
    async with get_client() as client:
        await client.execute("DELETE FROM db_admins WHERE user_id = ?", [user_id])

async def get_db_admins() -> List[int]:
    async with get_client() as client:
        rs = await client.execute("SELECT user_id FROM db_admins")
        return [int(row[0]) for row in rs.rows]


# ==========================================
# ПРОВЕРКА И УПРАВЛЕНИЕ СУБЪЕКТАМИ
# ==========================================
async def check_entity(ident: str) -> Tuple[str, Optional[Dict]]:
    """Поиск объекта по всем таблицам (скамер, гарант, проверенный)"""
    ident = normalize_id(ident)
    async with get_client() as client:
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
    async with get_client() as client:
        await client.execute(
            "INSERT OR REPLACE INTO scammers (identifier, reason, proof) VALUES (?, ?, ?)",
            [ident, reason, proof]
        )
        await client.execute("DELETE FROM trusted_users WHERE identifier = ?", [ident])
        await client.execute("DELETE FROM guarantors WHERE identifier = ?", [ident])

async def add_trusted_user(identifier: str, note: str = "Проверен"):
    ident = normalize_id(identifier)
    async with get_client() as client:
        await client.execute(
            "INSERT OR REPLACE INTO trusted_users (identifier, note) VALUES (?, ?)",
            [ident, note]
        )
        await client.execute("DELETE FROM scammers WHERE identifier = ?", [ident])

async def add_guarantor(identifier: str, name: str, description: str, deposit: str = "Отсутствует"):
    ident = normalize_id(identifier)
    async with get_client() as client:
        await client.execute(
            "INSERT OR REPLACE INTO guarantors (identifier, name, description, deposit) VALUES (?, ?, ?, ?)",
            [ident, name, description, deposit]
        )
        await client.execute("DELETE FROM scammers WHERE identifier = ?", [ident])

async def get_guarantors() -> List[Dict]:
    async with get_client() as client:
        rs = await client.execute("SELECT * FROM guarantors ORDER BY added_at DESC")
        return [row_to_dict(rs, row) for row in rs.rows]


# ==========================================
# РАБОТА С ЖАЛОБАМИ
# ==========================================
async def add_complaint(reporter_id: int, target: str, description: str, proof: str) -> int:
    target = normalize_id(target)
    async with get_client() as client:
        rs = await client.execute(
            "INSERT INTO complaints (reporter_id, target, description, proof) VALUES (?, ?, ?, ?)",
            [reporter_id, target, description, proof]
        )
        return rs.last_insert_rowid

async def get_pending_complaints() -> List[Dict]:
    async with get_client() as client:
        rs = await client.execute("SELECT * FROM complaints WHERE status = 'pending' ORDER BY id ASC")
        return [row_to_dict(rs, row) for row in rs.rows]

async def get_complaint_by_id(complaint_id: int) -> Optional[Dict]:
    async with get_client() as client:
        rs = await client.execute("SELECT * FROM complaints WHERE id = ?", [complaint_id])
        return row_to_dict(rs, rs.rows[0]) if rs.rows else None

async def resolve_complaint(complaint_id: int, status: str):
    async with get_client() as client:
        await client.execute("UPDATE complaints SET status = ? WHERE id = ?", [status, complaint_id])

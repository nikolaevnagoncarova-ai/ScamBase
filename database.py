import os
import libsql_client

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

def get_client():
    """Создаёт асинхронный клиент для подключения к Turso"""
    return libsql_client.create_client_async(url=TURSO_URL, auth_token=TURSO_TOKEN)

async def init_db():
    """Инициализация таблиц базы данных"""
    async with get_client() as client:
        # Таблица скамеров
        await client.execute("""
            CREATE TABLE IF NOT EXISTS scammers (
                identifier TEXT PRIMARY KEY,
                reason TEXT,
                proof TEXT
            )
        """)
        # Таблица гарантов
        await client.execute("""
            CREATE TABLE IF NOT EXISTS guarantors (
                identifier TEXT PRIMARY KEY,
                info TEXT
            )
        """)
        # Таблица жалоб
        await client.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                target TEXT,
                text TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)

async def check_scammer(identifier: str):
    """Проверка пользователя в базе скамеров"""
    async with get_client() as client:
        res = await client.execute("SELECT reason, proof FROM scammers WHERE identifier = ?", [identifier])
        if res.rows:
            return res.rows[0]
        return None

async def add_scammer(identifier: str, reason: str = "", proof: str = ""):
    """Добавление скамера в базу"""
    async with get_client() as client:
        await client.execute(
            "INSERT OR REPLACE INTO scammers (identifier, reason, proof) VALUES (?, ?, ?)",
            [identifier, reason, proof]
        )

async def get_all_guarantors():
    """Получение списка всех гарантов"""
    async with get_client() as client:
        res = await client.execute("SELECT identifier, info FROM guarantors")
        return res.rows

async def add_guarantor(identifier: str, info: str = ""):
    """Добавление гаранта"""
    async with get_client() as client:
        await client.execute(
            "INSERT OR REPLACE INTO guarantors (identifier, info) VALUES (?, ?)",
            [identifier, info]
        )

async def add_complaint(user_id: int, target: str, text: str):
    """Сохранение жалобы от пользователя"""
    async with get_client() as client:
        await client.execute(
            "INSERT INTO complaints (user_id, target, text) VALUES (?, ?, ?)",
            [user_id, target, text]
        )

"""
MySQL access layer. Same schema/tables as the original Node app
(accounts, contacts, messages, meetings), just accessed with aiomysql
instead of mysql2.
"""
import os
import aiomysql

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "whatsapp_ai_bot")

_pool: aiomysql.Pool | None = None


async def get_pool() -> aiomysql.Pool:
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            autocommit=True,
            minsize=1,
            maxsize=10,
            charset="utf8mb4",  # so emoji / Hindi / Marathi text never gets
            # silently truncated or mangled on insert (utf8mb3, MySQL's old
            # default, can't store 4-byte characters like most emoji)
        )
    return _pool


async def query(sql: str, params: tuple = ()):
    """Run a query and return rows as a list of dicts."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, params)
            if cur.description:
                return await cur.fetchall()
            return []


async def execute(sql: str, params: tuple = ()) -> int:
    """Run an INSERT/UPDATE/DELETE and return the last inserted id (if any)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return cur.lastrowid


async def init_db():
    """Creates the database (if missing) and all required tables. Safe to
    run every time the app starts."""
    # First connect without selecting a database, to create it if needed.
    root_conn = await aiomysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, charset="utf8mb4"
    )
    async with root_conn.cursor() as cur:
        await cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    root_conn.close()

    await execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id VARCHAR(64) PRIMARY KEY,
            phone_number VARCHAR(32) DEFAULT NULL,
            name VARCHAR(128) DEFAULT NULL,
            status ENUM('pending_qr','connected','disconnected') DEFAULT 'pending_qr',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """
    )

    await execute(
        """
        CREATE TABLE IF NOT EXISTS contacts (
            id INT AUTO_INCREMENT PRIMARY KEY,
            account_id VARCHAR(64) NOT NULL,
            jid VARCHAR(64) NOT NULL,
            name VARCHAR(128) DEFAULT NULL,
            ai_status ENUM('open','closed','paused') DEFAULT 'open',
            last_human_at TIMESTAMP NULL DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_contact (account_id, jid),
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """
    )
    # Upgrading an existing DB that predates ai_status/last_human_at —
    # MySQL has no "ADD COLUMN IF NOT EXISTS" before 8.0.29, so just try
    # and ignore the "column already exists" error on repeat runs.
    for ddl in (
        "ALTER TABLE contacts ADD COLUMN ai_status ENUM('open','closed','paused') DEFAULT 'open'",
        "ALTER TABLE contacts ADD COLUMN last_human_at TIMESTAMP NULL DEFAULT NULL",
    ):
        try:
            await execute(ddl)
        except Exception:  # noqa: BLE001 — 1060 = duplicate column, safe to ignore
            pass

    await execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INT AUTO_INCREMENT PRIMARY KEY,
            account_id VARCHAR(64) NOT NULL,
            contact_id INT NOT NULL,
            direction ENUM('in','out') NOT NULL,
            body TEXT,
            ai_generated TINYINT(1) DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """
    )

    await execute(
        """
        CREATE TABLE IF NOT EXISTS meetings (
            id INT AUTO_INCREMENT PRIMARY KEY,
            account_id VARCHAR(64) NOT NULL,
            contact_id INT NOT NULL,
            title VARCHAR(255),
            start_time DATETIME,
            end_time DATETIME,
            google_event_id VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """
    )

    # The statements above only set the charset for BRAND NEW tables. If
    # these tables already existed (from before this fix) with the wrong
    # charset — which is what caused "Data truncated for column 'body'" —
    # convert them in place too. Safe/idempotent to run on every startup.
    for table in ("accounts", "contacts", "messages", "meetings"):
        try:
            await execute(
                f"ALTER TABLE {table} CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        except Exception as err:  # noqa: BLE001
            print(f"[db] charset upgrade for '{table}' skipped: {err}")

    print("[db] schema ready")


async def upsert_contact(account_id: str, jid: str, name: str | None) -> int:
    """Finds or creates a contact row for a chat, returns its id."""
    await execute(
        """
        INSERT INTO contacts (account_id, jid, name) VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE name = COALESCE(VALUES(name), name)
        """,
        (account_id, jid, name),
    )
    rows = await query(
        "SELECT id FROM contacts WHERE account_id = %s AND jid = %s", (account_id, jid)
    )
    return rows[0]["id"]


async def save_message(
    account_id: str, contact_id: int, direction: str, body: str, ai_generated: bool = False
) -> int:
    """Inserts a message row and returns its new id (used as the
    message_id when the caller — e.g. the /api/send endpoint — needs to
    hand it back to whoever sent the message)."""
    return await execute(
        """
        INSERT INTO messages (account_id, contact_id, direction, body, ai_generated)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (account_id, contact_id, direction, body, 1 if ai_generated else 0),
    )


async def upsert_account_connected(account_id: str, phone: str | None, name: str | None):
    """Creates the accounts row the first time a session actually links
    (mirrors the original 'only exists once WhatsApp confirms pairing'
    behaviour), and just refreshes phone/name/status on every reconnect."""
    await execute(
        """
        INSERT INTO accounts (id, phone_number, name, status)
        VALUES (%s, %s, %s, 'connected')
        ON DUPLICATE KEY UPDATE
            status = 'connected',
            phone_number = COALESCE(%s, phone_number),
            name = COALESCE(%s, name)
        """,
        (account_id, phone, name, phone, name),
    )


async def update_account_status(account_id: str, status: str):
    """No-op if the account doesn't exist yet (e.g. still mid pairing) —
    matches the original 'don't create a phantom row for an unscanned QR'."""
    await execute("UPDATE accounts SET status = %s WHERE id = %s", (status, account_id))


async def delete_account(account_id: str):
    await execute("DELETE FROM accounts WHERE id = %s", (account_id,))


# ---------------------------------------------------------------------------
# Per-contact AI conversation state:
#   'open'   -> AI is actively allowed to auto-reply in this chat
#   'closed' -> a meeting got booked / the ask was resolved; AI stays quiet
#               until a fresh website/software request comes in
#   'paused' -> the account holder replied manually; AI stands down until
#               a fresh website/software request comes in
# ---------------------------------------------------------------------------


async def get_contact_status(contact_id: int) -> str:
    rows = await query("SELECT ai_status FROM contacts WHERE id = %s", (contact_id,))
    return rows[0]["ai_status"] if rows else "open"


async def set_contact_status(contact_id: int, status: str):
    await execute("UPDATE contacts SET ai_status = %s WHERE id = %s", (status, contact_id))


async def mark_human_reply(contact_id: int):
    """Called when the account holder types a message themselves — pauses
    the AI and stamps the time, so a delayed AI reply can check 'did a
    human jump in after I started waiting?' before actually sending."""
    await execute(
        "UPDATE contacts SET ai_status = 'paused', last_human_at = NOW() WHERE id = %s",
        (contact_id,),
    )


async def human_replied_since(contact_id: int, since_ts) -> bool:
    """True if last_human_at is newer than `since_ts` — used to cancel a
    delayed AI reply if the account holder jumped in while we were waiting."""
    rows = await query("SELECT last_human_at FROM contacts WHERE id = %s", (contact_id,))
    if not rows or not rows[0]["last_human_at"]:
        return False
    return rows[0]["last_human_at"] > since_ts

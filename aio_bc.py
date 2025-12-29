# aio_bc_final.py
# Panel Bot (python-telegram-bot) + Ubot Sender (Pyrogram) dalam 1 file
# Fitur:
# - /setmsg simpan teks + entities (termasuk custom/premium emoji)
# - whitelist dengan thread_key (-1 = non-topic, selain itu = topic id)
# - /adddest /unwhitelist /blacklist /unblacklist bisa dipakai langsung di grup/topic (paling akurat)
# - fallback forward kalau command dipakai di private
# - /status + /listdest + /listblack
# - /enable /disable
# - /force (blast sekali semua whitelist) + /forcehere (blast sekali di chat/topic tempat command)
# - safe_send max_retry + auto-remove dest yang error permanen biar gak nyangkut

import asyncio
import json
import sqlite3
import time
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple, Set

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

from pyrogram import Client
from pyrogram.errors import FloodWait, SlowmodeWait, RPCError
from pyrogram.types import MessageEntity

# =======================
# LOAD .env (TARUH DI SINI)
# =======================
def load_env_file(path: str):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE_DIR = Path(__file__).resolve().parent
load_env_file(str(BASE_DIR / ".env"))


# =======================
# CONFIG (DIAMBIL DARI .env / ENV)
# =======================
TOKEN = os.getenv("TOKEN", "ISI_TOKEN_BOTFATHER")
API_ID = int(os.getenv("API_ID", "123456"))
API_HASH = os.getenv("API_HASH", "ISI_API_HASH")
OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))  # id telegram kamu (@userinfobot)

# rekomendasi: taruh DB di folder project biar rapi
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data.db")))

DEFAULT_INTERVAL_HOURS = int(os.getenv("DEFAULT_INTERVAL_HOURS", "12"))
DEFAULT_DELAY_SEC = float(os.getenv("DEFAULT_DELAY_SEC", "5"))

PYRO_SESSION_NAME = os.getenv("PYRO_SESSION_NAME", "userbot")

# optional: validasi biar gak jalan kalau belum diisi beneran
if TOKEN == "ISI_TOKEN_BOTFATHER" or API_HASH == "ISI_API_HASH" or API_ID == 123456 or OWNER_ID == 123456789:
    print("⚠️ CONFIG belum diisi. Isi file .env dulu (lihat .env.example).")

# =======================
# UTIL
# =======================
def now() -> int:
    return int(time.time())

def fmt_ts(ts: int) -> str:
    if not ts:
        return "-"
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

def thread_key_from(thread_id: Optional[int]) -> int:
    return int(thread_id) if thread_id is not None else -1

# =======================
# DB
# =======================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS users(
        owner_id INTEGER PRIMARY KEY,
        interval_hours INTEGER DEFAULT 12,
        delay_sec REAL DEFAULT 5,
        enabled INTEGER DEFAULT 0,
        message_text TEXT,
        message_entities TEXT,
        next_run INTEGER DEFAULT 0
    )""")

    # whitelist pakai thread_key (AMAN di SQLite)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS whitelist(
        owner_id INTEGER,
        chat_id INTEGER,
        thread_id INTEGER,
        thread_key INTEGER DEFAULT -1,
        title TEXT,
        UNIQUE(owner_id, chat_id, thread_key)
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS blacklist(
        owner_id INTEGER,
        chat_id INTEGER,
        UNIQUE(owner_id, chat_id)
    )""")

    # migrate users (kalau DB lama)
    for ddl in [
        "ALTER TABLE users ADD COLUMN message_entities TEXT",
        "ALTER TABLE users ADD COLUMN next_run INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass

    # migrate whitelist: tambah thread_key (kalau DB lama)
    try:
        conn.execute("ALTER TABLE whitelist ADD COLUMN thread_key INTEGER DEFAULT -1")
    except sqlite3.OperationalError:
        pass

    # backfill thread_key untuk row lama yang NULL
    try:
        conn.execute("UPDATE whitelist SET thread_key=COALESCE(thread_id, -1) WHERE thread_key IS NULL")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    return conn

def ensure_user(owner_id: int):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users(owner_id, interval_hours, delay_sec) VALUES(?,?,?)",
        (owner_id, DEFAULT_INTERVAL_HOURS, DEFAULT_DELAY_SEC)
    )
    conn.commit()
    conn.close()

# =======================
# DB HELPERS
# =======================
def upsert_whitelist(owner_id: int, chat_id: int, thread_id: Optional[int], title: str) -> int:
    tkey = thread_key_from(thread_id)
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO whitelist(owner_id, chat_id, thread_id, thread_key, title) VALUES(?,?,?,?,?)",
        (owner_id, chat_id, thread_id, tkey, title)
    )
    conn.commit()
    conn.close()
    return tkey

def delete_whitelist(owner_id: int, chat_id: int, thread_id: Optional[int]) -> int:
    tkey = thread_key_from(thread_id)
    conn = db()
    cur = conn.execute(
        "DELETE FROM whitelist WHERE owner_id=? AND chat_id=? AND thread_key=?",
        (owner_id, chat_id, tkey)
    )
    conn.commit()
    conn.close()
    return cur.rowcount

def add_blacklist(owner_id: int, chat_id: int):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO blacklist(owner_id, chat_id) VALUES(?,?)", (owner_id, chat_id))
    conn.commit()
    conn.close()

def remove_blacklist(owner_id: int, chat_id: int) -> int:
    conn = db()
    cur = conn.execute("DELETE FROM blacklist WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
    conn.commit()
    conn.close()
    return cur.rowcount

def remove_dest(owner_id: int, chat_id: int, thread_id: Optional[int]) -> int:
    # auto-remove dest yang error permanen saat kirim
    return delete_whitelist(owner_id, chat_id, thread_id)

def get_user_config(owner_id: int):
    conn = db()
    u = conn.execute(
        "SELECT interval_hours, delay_sec, enabled, message_text, message_entities, next_run "
        "FROM users WHERE owner_id=?",
        (owner_id,)
    ).fetchone()

    wl = conn.execute(
        "SELECT chat_id, thread_id, title FROM whitelist WHERE owner_id=?",
        (owner_id,)
    ).fetchall()

    bl = set(r[0] for r in conn.execute(
        "SELECT chat_id FROM blacklist WHERE owner_id=?",
        (owner_id,)
    ).fetchall())

    conn.close()
    return u, wl, bl

# =======================
# PANEL COMMANDS
# =======================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "✅ Panel BC siap.\n\n"
        "Flow paling aman (support forum/topics):\n"
        "1) /setmsg → lalu kirim 1 pesan BC\n"
        "2) Masuk grup/topik tujuan → ketik /adddest\n"
        "3) /enable (jadwal) atau /force (langsung kirim sekali)\n\n"
        "Commands:\n"
        "/setmsg, /cancel\n"
        "/adddest, /unwhitelist\n"
        "/blacklist, /unblacklist\n"
        "/setinterval 12, /setdelay 5\n"
        "/enable, /disable, /status\n"
        "/listdest, /listblack\n"
        "/force, /forcehere\n\n"
        "Catatan: untuk forum/topics, ketik /adddest & /unwhitelist langsung di TOPIC-nya."
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ Dibatalkan.")

# ---- setmsg step-by-step (tanpa reply) ----
async def cmd_setmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    context.user_data["awaiting"] = "setmsg"
    await update.message.reply_text(
        "✍️ Silakan kirim PESAN BC sekarang.\n"
        "• 1 pesan saja\n"
        "• Bisa teks + emoji premium\n"
        "• (Untuk batal) ketik /cancel"
    )

async def on_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting") != "setmsg":
        return

    msg = update.message
    if not msg.text:
        context.user_data.clear()
        return await msg.reply_text("❌ Pesan harus berupa teks.")

    text = msg.text
    ents = [e.to_dict() for e in (msg.entities or [])]

    conn = db()
    conn.execute(
        "UPDATE users SET message_text=?, message_entities=? WHERE owner_id=?",
        (text, json.dumps(ents, ensure_ascii=False), update.effective_user.id)
    )
    conn.commit()
    conn.close()

    context.user_data.clear()
    await msg.reply_text("✅ Pesan BC berhasil disimpan (entities/premium emoji ikut).")

# ---- interval/delay ----
async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    parts = update.message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await update.message.reply_text("Pakai: /setinterval 12")

    hours = int(parts[1])
    if hours < 1 or hours > 72:
        return await update.message.reply_text("Biar aman, interval 1–72 jam.")

    conn = db()
    conn.execute("UPDATE users SET interval_hours=? WHERE owner_id=?", (hours, update.effective_user.id))
    row = conn.execute("SELECT enabled FROM users WHERE owner_id=?", (update.effective_user.id,)).fetchone()
    if row and int(row[0]) == 1:
        conn.execute("UPDATE users SET next_run=? WHERE owner_id=?",
                     (now() + hours * 3600, update.effective_user.id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Interval diset: {hours} jam.")

async def cmd_setdelay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    parts = update.message.text.split()
    if len(parts) != 2:
        return await update.message.reply_text("Pakai: /setdelay 5")

    try:
        sec = float(parts[1])
    except ValueError:
        return await update.message.reply_text("Harus angka. Contoh: /setdelay 5")

    if sec < 0 or sec > 60:
        return await update.message.reply_text("Delay 0–60 detik aja ya.")

    conn = db()
    conn.execute("UPDATE users SET delay_sec=? WHERE owner_id=?", (sec, update.effective_user.id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Delay antar grup diset: {sec} detik.")

# ---- whitelist/blacklist smart (langsung di grup/topic) + fallback forward di private ----
async def cmd_adddest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    chat = update.effective_chat
    msg = update.message

    if chat and chat.type in ("group", "supergroup"):
        chat_id = chat.id
        thread_id = getattr(msg, "message_thread_id", None)
        title = chat.title or str(chat_id)
        tkey = upsert_whitelist(update.effective_user.id, chat_id, thread_id, title)
        return await msg.reply_text(
            f"✅ Masuk whitelist: {title}\nchat_id={chat_id}\nthread_id={thread_id}\nthread_key={tkey}\n\n"
            f"Forum: pastiin kamu ketik /adddest di TOPIC yang mau dituju."
        )

    context.user_data["mode"] = "whitelist"
    await msg.reply_text(
        "Forward 1 pesan dari grup/topik tujuan untuk masuk WHITELIST.\n"
        "(Forum/topics paling akurat: ketik /adddest langsung di topiknya.)"
    )

async def cmd_unwhitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    chat = update.effective_chat
    msg = update.message

    if chat and chat.type in ("group", "supergroup"):
        chat_id = chat.id
        thread_id = getattr(msg, "message_thread_id", None)
        title = chat.title or str(chat_id)
        deleted = delete_whitelist(update.effective_user.id, chat_id, thread_id)
        return await msg.reply_text(
            f"🗑️ Unwhitelist: {title}\nchat_id={chat_id}\nthread_id={thread_id}\nTerhapus: {deleted}\n\n"
            f"Forum: ketik /unwhitelist di TOPIC yang sesuai."
        )

    context.user_data["mode"] = "unwhitelist"
    await msg.reply_text(
        "Forward 1 pesan dari grup/topik yang mau dihapus dari WHITELIST.\n"
        "(Forum/topics paling akurat: ketik /unwhitelist langsung di topiknya.)"
    )

async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    chat = update.effective_chat
    msg = update.message

    if chat and chat.type in ("group", "supergroup"):
        chat_id = chat.id
        title = chat.title or str(chat_id)
        add_blacklist(update.effective_user.id, chat_id)
        return await msg.reply_text(f"⛔ Masuk blacklist: {title}\nchat_id={chat_id}")

    context.user_data["mode"] = "blacklist"
    await msg.reply_text("Forward 1 pesan dari grup yang mau diblok (BLACKLIST).")

async def cmd_unblacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    chat = update.effective_chat
    msg = update.message

    if chat and chat.type in ("group", "supergroup"):
        chat_id = chat.id
        title = chat.title or str(chat_id)
        deleted = remove_blacklist(update.effective_user.id, chat_id)
        return await msg.reply_text(f"✅ Dihapus dari blacklist: {title}\nchat_id={chat_id}\nTerhapus: {deleted}")

    context.user_data["mode"] = "unblacklist"
    await msg.reply_text("Forward 1 pesan dari grup yang mau dibuka blokirnya (UNBLACKLIST).")

async def on_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    if not mode:
        return

    msg = update.message
    fchat = msg.forward_from_chat
    if not fchat:
        context.user_data.clear()
        return await msg.reply_text("❌ Forward tidak kebaca asal grupnya. Coba forward ulang dari grup/topik.")

    owner_id = update.effective_user.id
    chat_id = fchat.id
    thread_id = getattr(msg, "message_thread_id", None)
    tkey = thread_key_from(thread_id)
    title = fchat.title or str(chat_id)

    if mode == "whitelist":
        upsert_whitelist(owner_id, chat_id, thread_id, title)
        await msg.reply_text(f"✅ Masuk whitelist: {title}\nchat_id={chat_id}\nthread_id={thread_id}\nthread_key={tkey}")

    elif mode == "blacklist":
        add_blacklist(owner_id, chat_id)
        await msg.reply_text(f"⛔ Masuk blacklist: {title}\nchat_id={chat_id}")

    elif mode == "unwhitelist":
        deleted = delete_whitelist(owner_id, chat_id, thread_id)
        await msg.reply_text(f"🗑️ Dihapus dari whitelist: {title}\nchat_id={chat_id}\nthread_id={thread_id}\nTerhapus: {deleted}")

    elif mode == "unblacklist":
        deleted = remove_blacklist(owner_id, chat_id)
        await msg.reply_text(f"✅ Dihapus dari blacklist: {title}\nchat_id={chat_id}\nTerhapus: {deleted}")

    context.user_data.clear()

# ---- enable/disable/status ----
async def cmd_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    conn = db()

    row = conn.execute(
        "SELECT interval_hours, message_text FROM users WHERE owner_id=?",
        (update.effective_user.id,)
    ).fetchone()
    wcnt = conn.execute("SELECT COUNT(*) FROM whitelist WHERE owner_id=?", (update.effective_user.id,)).fetchone()[0]

    if not row or not row[1]:
        conn.close()
        return await update.message.reply_text("❌ Set dulu pesan: /setmsg lalu kirim pesannya.")
    if wcnt == 0:
        conn.close()
        return await update.message.reply_text("❌ Tambah dulu whitelist: /adddest (langsung di grup/topic)")

    interval_hours = int(row[0])
    conn.execute(
        "UPDATE users SET enabled=1, next_run=? WHERE owner_id=?",
        (now() + interval_hours * 3600, update.effective_user.id)
    )
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ Enabled. Ubot akan BC otomatis sesuai jadwal.")

async def cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    conn = db()
    conn.execute("UPDATE users SET enabled=0, next_run=0 WHERE owner_id=?", (update.effective_user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("⛔ Disabled.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    conn = db()
    row = conn.execute(
        "SELECT interval_hours, delay_sec, enabled, message_text, next_run FROM users WHERE owner_id=?",
        (update.effective_user.id,)
    ).fetchone()
    wcnt = conn.execute("SELECT COUNT(*) FROM whitelist WHERE owner_id=?", (update.effective_user.id,)).fetchone()[0]
    bcnt = conn.execute("SELECT COUNT(*) FROM blacklist WHERE owner_id=?", (update.effective_user.id,)).fetchone()[0]
    sample = conn.execute(
        "SELECT title, chat_id, thread_id FROM whitelist WHERE owner_id=? ORDER BY rowid DESC LIMIT 5",
        (update.effective_user.id,)
    ).fetchall()
    conn.close()

    interval_hours, delay_sec, enabled, message_text, next_run = row
    next_run_human = fmt_ts(int(next_run)) if next_run else "-"

    lines = [
        f"Enabled: {bool(enabled)}",
        f"Interval: {interval_hours} jam",
        f"Delay/grup: {delay_sec} detik",
        f"Whitelist: {wcnt}",
        f"Blacklist: {bcnt}",
        f"Message set: {bool(message_text)}",
        f"Next run: {next_run_human} (epoch={next_run})",
    ]
    if sample:
        lines.append("\nContoh whitelist (max 5):")
        for title, chat_id, thread_id in sample:
            lines.append(f"• {title} | chat_id={chat_id} | thread_id={thread_id}")

    await update.message.reply_text("\n".join(lines))

async def cmd_listdest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    conn = db()
    rows = conn.execute(
        "SELECT title, chat_id, thread_id FROM whitelist WHERE owner_id=? ORDER BY title COLLATE NOCASE",
        (update.effective_user.id,)
    ).fetchall()
    conn.close()

    if not rows:
        return await update.message.reply_text("Whitelist kosong. Pakai /adddest dulu.")

    out = ["📌 WHITELIST:"]
    for title, chat_id, thread_id in rows[:80]:
        out.append(f"• {title} | chat_id={chat_id} | thread_id={thread_id}")
    if len(rows) > 80:
        out.append(f"... dan {len(rows)-80} lainnya")

    await update.message.reply_text("\n".join(out))

async def cmd_listblack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    conn = db()
    rows = conn.execute(
        "SELECT chat_id FROM blacklist WHERE owner_id=? ORDER BY chat_id",
        (update.effective_user.id,)
    ).fetchall()
    conn.close()

    if not rows:
        return await update.message.reply_text("Blacklist kosong.")

    out = ["⛔ BLACKLIST:"]
    for (chat_id,) in rows[:150]:
        out.append(f"• chat_id={chat_id}")
    if len(rows) > 150:
        out.append(f"... dan {len(rows)-150} lainnya")

    await update.message.reply_text("\n".join(out))

# =======================
# ENTITIES BUILDER
# =======================
def build_entities(message_entities_json: Optional[str]):
    if not message_entities_json:
        return None
    try:
        raw = json.loads(message_entities_json)
        entities: List[MessageEntity] = []
        for e in raw:
            entities.append(
                MessageEntity(
                    type=e.get("type"),
                    offset=e.get("offset", 0),
                    length=e.get("length", 0),
                    url=e.get("url"),
                    user=e.get("user"),
                    language=e.get("language"),
                    custom_emoji_id=e.get("custom_emoji_id"),
                )
            )
        return entities
    except Exception:
        return None

# =======================
# UBOT SENDER CORE
# =======================
def fetch_owner_config(owner_id: int):
    conn = db()
    u = conn.execute(
        "SELECT interval_hours, delay_sec, enabled, message_text, message_entities, next_run "
        "FROM users WHERE owner_id=?",
        (owner_id,)
    ).fetchone()

    wl = conn.execute(
        "SELECT chat_id, thread_id FROM whitelist WHERE owner_id=?",
        (owner_id,)
    ).fetchall()

    bl = set(r[0] for r in conn.execute(
        "SELECT chat_id FROM blacklist WHERE owner_id=?",
        (owner_id,)
    ).fetchall())

    conn.close()
    return u, wl, bl

def update_next_run(owner_id: int, next_run: int):
    conn = db()
    conn.execute("UPDATE users SET next_run=? WHERE owner_id=?", (next_run, owner_id))
    conn.commit()
    conn.close()

async def safe_send(
    app: Client,
    owner_id: int,
    chat_id: int,
    thread_id: Optional[int],
    text: str,
    entities,
    max_retry: int = 3
) -> bool:
    attempt = 0
    while True:
        try:
            await app.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities,
                message_thread_id=thread_id
            )
            return True

        except SlowmodeWait as e:
            attempt += 1
            wait_s = int(getattr(e, "value", 0)) or 10
            if attempt > max_retry:
                return False
            await asyncio.sleep(wait_s)

        except FloodWait as e:
            attempt += 1
            wait_s = int(getattr(e, "value", 0)) or 30
            if attempt > max_retry:
                return False
            await asyncio.sleep(wait_s)

        except RPCError as e:
            removed = remove_dest(owner_id, chat_id, thread_id)
            print(f"⚠️ RPCError={type(e).__name__} chat_id={chat_id} thread_id={thread_id} removed={removed}")
            return False

        except Exception as e:
            print(f"⚠️ UnknownError={type(e).__name__} chat_id={chat_id} thread_id={thread_id}")
            return False

async def ubot_loop():
    app = Client(PYRO_SESSION_NAME, api_id=API_ID, api_hash=API_HASH)
    await app.start()
    print("✅ Ubot sender running...")

    while True:
        u, wl, bl = fetch_owner_config(OWNER_ID)
        if not u:
            await asyncio.sleep(10)
            continue

        interval_hours, delay_sec, enabled, message_text, message_entities, next_run = u
        if not enabled or not message_text or not wl:
            await asyncio.sleep(10)
            continue

        t = now()
        if not next_run or t < int(next_run):
            await asyncio.sleep(10)
            continue

        entities = build_entities(message_entities)

        for chat_id, thread_id in wl:
            if chat_id in bl:
                continue
            await safe_send(app, OWNER_ID, chat_id, thread_id, message_text, entities, max_retry=3)
            await asyncio.sleep(float(delay_sec))

        update_next_run(OWNER_ID, now() + int(interval_hours) * 3600)

# =======================
# FORCE SEND COMMANDS
# =======================
async def cmd_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    ensure_user(owner_id)

    u, wl, bl = get_user_config(owner_id)
    if not u:
        return await update.message.reply_text("❌ Config user tidak ketemu.")

    interval_hours, delay_sec, enabled, message_text, message_entities, next_run = u
    if not message_text:
        return await update.message.reply_text("❌ Pesan belum diset. Pakai /setmsg dulu.")
    if not wl:
        return await update.message.reply_text("❌ Whitelist kosong. Pakai /adddest dulu.")

    await update.message.reply_text("🚀 Force BC dimulai...")

    app = Client(PYRO_SESSION_NAME, api_id=API_ID, api_hash=API_HASH)
    try:
        await app.start()
    except Exception as e:
        return await update.message.reply_text(f"❌ Gagal start userbot: {type(e).__name__}")

    entities = build_entities(message_entities)

    sent = 0
    skipped = 0

    for chat_id, thread_id, title in wl:
        if chat_id in bl:
            skipped += 1
            continue

        ok = await safe_send(app, owner_id, chat_id, thread_id, message_text, entities, max_retry=3)
        if ok:
            sent += 1
        else:
            skipped += 1

        await asyncio.sleep(float(delay_sec))

    try:
        await app.stop()
    except Exception:
        pass

    await update.message.reply_text(f"✅ Force selesai.\nTerkirim: {sent}\nSkip/Gagal/Blacklist: {skipped}")

async def cmd_forcehere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    ensure_user(owner_id)

    chat = update.effective_chat
    msg = update.message
    if not chat:
        return await msg.reply_text("❌ Chat tidak kebaca.")

    u, wl, bl = get_user_config(owner_id)
    if not u:
        return await msg.reply_text("❌ Config user tidak ketemu.")
    interval_hours, delay_sec, enabled, message_text, message_entities, next_run = u

    if not message_text:
        return await msg.reply_text("❌ Pesan belum diset. Pakai /setmsg dulu.")

    chat_id = chat.id
    thread_id = getattr(msg, "message_thread_id", None)

    if chat_id in bl:
        return await msg.reply_text("⛔ Chat ini lagi masuk blacklist.")

    await msg.reply_text("🚀 Forcehere dimulai...")

    app = Client(PYRO_SESSION_NAME, api_id=API_ID, api_hash=API_HASH)
    try:
        await app.start()
    except Exception as e:
        return await msg.reply_text(f"❌ Gagal start userbot: {type(e).__name__}")

    entities = build_entities(message_entities)
    ok = await safe_send(app, owner_id, chat_id, thread_id, message_text, entities, max_retry=3)

    try:
        await app.stop()
    except Exception:
        pass

    if ok:
        await msg.reply_text("✅ Forcehere sukses terkirim.")
    else:
        await msg.reply_text("⚠️ Forcehere gagal / di-skip.")

# =======================
# RUNNERS
# =======================
async def run_panel():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("setmsg", cmd_setmsg))

    application.add_handler(CommandHandler("adddest", cmd_adddest))
    application.add_handler(CommandHandler("unwhitelist", cmd_unwhitelist))
    application.add_handler(CommandHandler("blacklist", cmd_blacklist))
    application.add_handler(CommandHandler("unblacklist", cmd_unblacklist))

    application.add_handler(CommandHandler("setinterval", cmd_setinterval))
    application.add_handler(CommandHandler("setdelay", cmd_setdelay))

    application.add_handler(CommandHandler("enable", cmd_enable))
    application.add_handler(CommandHandler("disable", cmd_disable))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("listdest", cmd_listdest))
    application.add_handler(CommandHandler("listblack", cmd_listblack))

    application.add_handler(CommandHandler("force", cmd_force))
    application.add_handler(CommandHandler("forcehere", cmd_forcehere))

    # forward handler (fallback private)
    application.add_handler(MessageHandler(filters.FORWARDED, on_forward))
    # text handler untuk setmsg
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_input))

    await application.run_polling(close_loop=False)

async def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["panel", "ubot", "both"], help="Jalankan panel / ubot / keduanya")
    args = p.parse_args()

    c = db()
    c.close()

    if args.mode == "panel":
        await run_panel()
    elif args.mode == "ubot":
        await ubot_loop()
    else:
        await asyncio.gather(run_panel(), ubot_loop())

if __name__ == "__main__":
    asyncio.run(main())

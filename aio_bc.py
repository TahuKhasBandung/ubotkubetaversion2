import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

from pyrogram import Client
from pyrogram.errors import FloodWait, SlowmodeWait, RPCError
from pyrogram.types import MessageEntity

# =======================
# CONFIG (WAJIB DIISI)
# =======================
TOKEN = "ISI_TOKEN_BOTFATHER"
API_ID = 123456
API_HASH = "ISI_API_HASH"
OWNER_ID = 123456789  # id telegram kamu (lihat @userinfobot)

DB_PATH = Path("data.db")

DEFAULT_INTERVAL_HOURS = 12  # 2x sehari
DEFAULT_DELAY_SEC = 5        # delay antar grup

# =======================
# DB
# =======================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
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
    conn.execute("""
    CREATE TABLE IF NOT EXISTS whitelist(
        owner_id INTEGER,
        chat_id INTEGER,
        thread_id INTEGER,
        title TEXT,
        UNIQUE(owner_id, chat_id, COALESCE(thread_id, -1))
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS blacklist(
        owner_id INTEGER,
        chat_id INTEGER,
        UNIQUE(owner_id, chat_id)
    )""")

    # migrate kalau DB lama belum punya kolom tertentu
    for ddl in [
        "ALTER TABLE users ADD COLUMN message_entities TEXT",
        "ALTER TABLE users ADD COLUMN next_run INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    return conn

def now() -> int:
    return int(time.time())

def ensure_user(owner_id: int):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO users(owner_id, interval_hours, delay_sec) VALUES(?,?,?)",
                 (owner_id, DEFAULT_INTERVAL_HOURS, DEFAULT_DELAY_SEC))
    conn.commit()
    conn.close()

# =======================
# PANEL BOT COMMANDS
# =======================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "✅ Panel BC siap.\n\n"
        "Flow:\n"
        "1) /setmsg → lalu kirim pesan BC (teks + premium emoji)\n"
        "2) /adddest → forward pesan dari grup/topik tujuan\n"
        "3) /enable\n\n"
        "Commands:\n"
        "/setmsg\n"
        "/adddest | /unwhitelist\n"
        "/blacklist | /unblacklist\n"
        "/setinterval 12 | /setdelay 5\n"
        "/enable | /disable | /status\n\n"
        "Catatan forum/topics: forward pesan dari topik yang kamu mau."
    )

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

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ Dibatalkan.")

async def on_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting") != "setmsg":
        return

    msg = update.message
    if not msg.text:
        context.user_data.clear()
        return await msg.reply_text("❌ Pesan harus berupa teks.")

    text = msg.text
    ents = []
    if msg.entities:
        for e in msg.entities:
            ents.append(e.to_dict())

    conn = db()
    conn.execute(
        "UPDATE users SET message_text=?, message_entities=? WHERE owner_id=?",
        (text, json.dumps(ents, ensure_ascii=False), update.effective_user.id)
    )
    conn.commit()
    conn.close()

    context.user_data.clear()
    await msg.reply_text("✅ Pesan BC berhasil disimpan (premium emoji ikut).")

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
    # kalau enabled, reschedule dari sekarang + interval
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

# ---- add/remove dest via forward ----
async def cmd_adddest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    context.user_data["mode"] = "whitelist"
    await update.message.reply_text("Forward 1 pesan dari grup/topik tujuan untuk masuk WHITELIST.\n(Forum: forward dari topiknya)")

async def cmd_unwhitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    context.user_data["mode"] = "unwhitelist"
    await update.message.reply_text("Forward 1 pesan dari grup/topik yang mau dihapus dari WHITELIST.")

async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    context.user_data["mode"] = "blacklist"
    await update.message.reply_text("Forward 1 pesan dari grup yang mau diblok (BLACKLIST).")

async def cmd_unblacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    context.user_data["mode"] = "unblacklist"
    await update.message.reply_text("Forward 1 pesan dari grup yang mau dibuka blokirnya (UNBLACKLIST).")

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
    thread_id = getattr(msg, "message_thread_id", None)  # ada kalau forum topic
    title = fchat.title or str(chat_id)

    conn = db()

    if mode == "whitelist":
        conn.execute(
            "INSERT OR IGNORE INTO whitelist(owner_id, chat_id, thread_id, title) VALUES(?,?,?,?)",
            (owner_id, chat_id, thread_id, title)
        )
        conn.commit()
        conn.close()
        await msg.reply_text(f"✅ Masuk whitelist: {title}\nchat_id={chat_id}\nthread_id={thread_id}")

    elif mode == "blacklist":
        conn.execute("INSERT OR IGNORE INTO blacklist(owner_id, chat_id) VALUES(?,?)", (owner_id, chat_id))
        conn.commit()
        conn.close()
        await msg.reply_text(f"⛔ Masuk blacklist: {title}\nchat_id={chat_id}")

    elif mode == "unwhitelist":
        if thread_id is None:
            conn.execute(
                "DELETE FROM whitelist WHERE owner_id=? AND chat_id=? AND thread_id IS NULL",
                (owner_id, chat_id)
            )
        else:
            conn.execute(
                "DELETE FROM whitelist WHERE owner_id=? AND chat_id=? AND thread_id=?",
                (owner_id, chat_id, thread_id)
            )
        conn.commit()
        conn.close()
        await msg.reply_text(f"🗑️ Dihapus dari whitelist: {title}\nchat_id={chat_id}\nthread_id={thread_id}")

    elif mode == "unblacklist":
        conn.execute("DELETE FROM blacklist WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
        conn.commit()
        conn.close()
        await msg.reply_text(f"✅ Dihapus dari blacklist: {title}\nchat_id={chat_id}")

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
        return await update.message.reply_text("❌ Tambah dulu whitelist: /adddest")

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
    conn.close()

    await update.message.reply_text(
        f"Enabled: {bool(row[2])}\n"
        f"Interval: {row[0]} jam\n"
        f"Delay/grup: {row[1]} detik\n"
        f"Whitelist: {wcnt}\n"
        f"Blacklist: {bcnt}\n"
        f"Message set: {bool(row[3])}\n"
        f"Next run (epoch): {row[4]}"
    )

# =======================
# UBOT SENDER
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

def build_entities(message_entities_json: Optional[str]):
    if not message_entities_json:
        return None
    try:
        raw = json.loads(message_entities_json)
        entities = []
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

async def safe_send(app: Client, chat_id: int, thread_id: Optional[int], text: str, entities):
    try:
        await app.send_message(
            chat_id=chat_id,
            text=text,
            entities=entities,
            message_thread_id=thread_id
        )
    except SlowmodeWait as e:
        await asyncio.sleep(int(getattr(e, "value", 0)) or 10)
        return await safe_send(app, chat_id, thread_id, text, entities)
    except FloodWait as e:
        await asyncio.sleep(int(getattr(e, "value", 0)) or 30)
        return await safe_send(app, chat_id, thread_id, text, entities)
    except RPCError:
        return

async def ubot_loop():
    app = Client("userbot", api_id=API_ID, api_hash=API_HASH)
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

        # WHITELIST ONLY + BLACKLIST WINS
        for chat_id, thread_id in wl:
            if chat_id in bl:
                continue
            await safe_send(app, chat_id, thread_id, message_text, entities)
            await asyncio.sleep(float(delay_sec))

        update_next_run(OWNER_ID, now() + int(interval_hours) * 3600)

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

    # IMPORTANT: command handler dulu, baru text handler
    application.add_handler(MessageHandler(filters.FORWARDED, on_forward))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_input))

    await application.run_polling(close_loop=False)

async def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["panel", "ubot", "both"], help="Jalankan panel / ubot / keduanya")
    args = p.parse_args()

    # init db
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

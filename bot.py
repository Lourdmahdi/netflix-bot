# -*- coding: utf-8 -*-
import os, csv
from datetime import datetime, date, timedelta
from calendar import monthrange
from zoneinfo import ZoneInfo
from functools import wraps
from typing import Optional, Sequence, Any

from telegram import Update, InputFile, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import openai
openai.api_key = os.getenv("OPENAI_API_KEY")

# ------------ Config ------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
TZ = ZoneInfo("Asia/Baghdad")

USE_PG = bool(os.getenv("DATABASE_URL"))
if USE_PG:
    import psycopg2 as pg
    import psycopg2.extras as pg_extras
else:
    import sqlite3

# ------------ DB Layer ------------
class DB:
    def __init__(self):
        self.is_pg = USE_PG
        self.conn = self._connect()
        self._ensure_schema()

    def _connect(self):
        if self.is_pg:
            return pg.connect(os.getenv("DATABASE_URL"), sslmode=os.getenv("PG_SSLMODE", "require"))
        return sqlite3.connect(os.getenv("DB_PATH", "subs.db"), check_same_thread=False)

    def cursor(self):
        if self.is_pg:
            return self.conn.cursor(cursor_factory=pg_extras.DictCursor)
        return self.conn.cursor()

    def q(self, sql: str) -> str:
        return "%s".join(sql.split("?")) if self.is_pg else sql

    def execute(self, sql: str, params: Sequence[Any] = ()):
        cur = self.cursor()
        cur.execute(self.q(sql), params)
        return cur

    def commit(self): self.conn.commit()

    def _ensure_schema(self):
        # subscribers
        self.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            id SERIAL PRIMARY KEY,
            name TEXT,
            tg_username TEXT,
            tg_user_id BIGINT,
            customer_no TEXT UNIQUE,
            plan TEXT,
            profiles_count INT DEFAULT 1,
            start_date TEXT,
            end_date TEXT,
            amount_paid INT DEFAULT 0,
            status TEXT DEFAULT 'active',
            note TEXT
        )
        """)
        # custom commands
        self.execute("""
        CREATE TABLE IF NOT EXISTS custom_cmds (
            cmd TEXT PRIMARY KEY,
            reply TEXT
        )
        """)
        self.commit()

db = DB()

# ------------ Helpers ------------
def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id) and (user_id in ADMIN_IDS)

def admin_only(fn):
    @wraps(fn)
    async def w(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            return await update.message.reply_text("❌ غير مسموح.")
        return await fn(update, context)
    return w

def parse_kv(text: str):
    import shlex
    out = {}
    for p in shlex.split(text or ""):
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip()] = v.strip()
    return out

def iso_or_none(s: Optional[str]) -> Optional[str]:
    if not s: return None
    s = s.strip()
    fmts = ["%Y-%m-%d","%d-%m-%Y","%d/%m/%Y"]
    for f in fmts:
        try: return datetime.strptime(s,f).date().isoformat()
        except: pass
    try: return datetime.fromisoformat(s).date().isoformat()
    except: return None

def today_iso() -> str: return datetime.now(TZ).date().isoformat()

def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months)//12
    m = (d.month - 1 + months)%12 + 1
    day = min(d.day, monthrange(y,m)[1])
    return date(y,m,day)

def auto_customer_no(update: Update) -> str:
    base = int(update.message.date.timestamp()) % 100000
    return f"C{(update.effective_user.id % 1000):03d}{base:05d}"

# ------------ Menus ------------
def main_menu_keyboard(is_admin_user: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("ℹ️ معلومات عن البوت"), KeyboardButton("⭐ مميزات البوت"), KeyboardButton("📚 الشروحات")],
        [KeyboardButton("➕ إنشاء/إضافة مشترك"), KeyboardButton("🧾 تجديد اشتراك"), KeyboardButton("⏰ قرب الانتهاء")],
        [KeyboardButton("⬅️ استيراد من CSV"), KeyboardButton("➡️ تصدير CSV")],
    ]
    if is_admin_user:
        rows.append([KeyboardButton("🔐 لوحة المشرف")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

# ------------ Basic Commands ------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_adm = is_admin(update.effective_user.id)
    store = "PostgreSQL" if db.is_pg else "SQLite"
    await update.message.reply_text(
        f"مرحباً 👋 (التخزين: {store})\nاختر من القائمة:",
        reply_markup=main_menu_keyboard(is_adm)
    )

# ------------ Custom Commands ------------
@admin_only
async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts=update.message.text.split(maxsplit=2)
    if len(parts)<3: return await update.message.reply_text("📌 /setcommand <الاسم> <الرد>")
    cmd,reply=parts[1],parts[2]
    db.execute("INSERT INTO custom_cmds (cmd,reply) VALUES (?,?) ON CONFLICT(cmd) DO UPDATE SET reply=excluded.reply",(cmd,reply))
    db.commit(); await update.message.reply_text(f"✅ تم حفظ الأمر: /{cmd}")

@admin_only
async def del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts=update.message.text.split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("📌 /delcommand <الاسم>")
    cmd=parts[1]; db.execute("DELETE FROM custom_cmds WHERE cmd=?", (cmd,)); db.commit()
    await update.message.reply_text(f"🗑️ تم حذف الأمر: /{cmd}")

async def custom_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.startswith("/"): return
    cmd=update.message.text[1:].split()[0]
    cur=db.execute("SELECT reply FROM custom_cmds WHERE cmd=?", (cmd,))
    row=cur.fetchone()
    if row:
        return await update.message.reply_text(row[0] if not db.is_pg else row["reply"])

# ------------ AI Handler ------------
async def ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    try:
        response = openai.Completion.create(
            model="text-davinci-003",
            prompt=user_text,
            max_tokens=200,
            temperature=0.7
        )
        await update.message.reply_text(response["choices"][0]["text"].strip())
    except Exception as e:
        await update.message.reply_text(f"⚠️ خطأ في استدعاء الذكاء الصناعي: {e}")

# ------------ Menu Router ------------
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt=update.message.text.strip()
    if txt=="ℹ️ معلومات عن البوت":
        return await update.message.reply_text("بوت إدارة الاشتراكات 📊\n• إضافة/تجديد\n• استيراد/تصدير CSV\n• أوامر مخصصة ✅")
    if txt=="⭐ مميزات البوت":
        return await update.message.reply_text("• أزرار عربية سهلة\n• PostgreSQL/SQLite\n• أوامر مخصصة\n• ذكاء صناعي 🤖")
    if txt=="📚 الشروحات":
        return await update.message.reply_text("الاستخدام:\n/addsub ...\n/renew ...\n/due ...\n/import | /export\n/setcommand ...")
    if txt=="🔐 لوحة المشرف":
        rows = [
            [KeyboardButton("➕ إضافة أمر جديد"), KeyboardButton("🗑️ حذف أمر")],
            [KeyboardButton("📋 عرض الأوامر")]
        ]
        return await update.message.reply_text("لوحة المشرف 🛠️", reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True))
    if txt=="➕ إضافة أمر جديد":
        return await update.message.reply_text("📌 لاستخدام الإضافة:\n/setcommand <الاسم> <الرد>")
    if txt=="🗑️ حذف أمر":
        return await update.message.reply_text("📌 لاستخدام الحذف:\n/delcommand <الاسم>")
    if txt=="📋 عرض الأوامر":
        cur=db.execute("SELECT cmd, reply FROM custom_cmds"); rows=cur.fetchall()
        if not rows: return await update.message.reply_text("❌ لا يوجد أوامر مضافة بعد.")
        msg="📋 الأوامر المضافة:\n" + "\n".join([f"/{r[0]} → {r[1]}" for r in rows])
        return await update.message.reply_text(msg)

# ------------ Main ------------
def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN غير معيّن.")
    app=Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("setcommand",set_command))
    app.add_handler(CommandHandler("delcommand",del_command))

    # أولاً: أوامر ثابتة (لوحة وأزرار)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))
    # ثانياً: أوامر مخصصة
    app.add_handler(MessageHandler(filters.COMMAND, custom_router))
    # ثالثاً: أي نص عادي ➝ AI
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_reply))

    app.run_polling()

if __name__=="__main__":
    main()

# -*- coding: utf-8 -*-
import os, csv
from datetime import datetime, date, timedelta
from calendar import monthrange
from zoneinfo import ZoneInfo
from functools import wraps
from typing import Optional, Sequence, Any

from telegram import Update, InputFile, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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

@admin_only
async def cmd_addsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kv = parse_kv(update.message.text.partition(" ")[2])
    name = kv.get("اسم") or "بدون اسم"
    customer_no = kv.get("رقم") or auto_customer_no(update)
    plan = kv.get("خطة")
    start_date = iso_or_none(kv.get("بداية")) or today_iso()
    end_date = iso_or_none(kv.get("نهاية"))
    amount_paid = int(kv.get("مدفوع", 0))

    db.execute("INSERT INTO subscribers (name,customer_no,plan,start_date,end_date,amount_paid) VALUES (?,?,?,?,?,?) "
               "ON CONFLICT(customer_no) DO UPDATE SET name=excluded.name, plan=excluded.plan, start_date=excluded.start_date, end_date=excluded.end_date, amount_paid=excluded.amount_paid",
               (name, customer_no, plan, start_date, end_date, amount_paid))
    db.commit()
    await update.message.reply_text(f"✅ تم حفظ المشترك: {name} (رقم: {customer_no})")

@admin_only
async def cmd_renew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split(maxsplit=2)
    if len(parts)<2: return await update.message.reply_text("📌 Usage: /renew <customer_no> months=1 paid=0")
    customer_no = parts[1]
    kv = parse_kv(parts[2] if len(parts)>2 else "")
    months = int(kv.get("months",1)); paid = int(kv.get("paid",0))
    cur = db.execute("SELECT end_date,amount_paid FROM subscribers WHERE customer_no=?", (customer_no,))
    row = cur.fetchone()
    if not row: return await update.message.reply_text("⚠️ العميل غير موجود.")
    end_old = row[0]; paid_old=row[1] or 0
    base = datetime.fromisoformat(end_old).date() if end_old else datetime.now(TZ).date()
    new_end=add_months(base,months).isoformat(); new_paid=paid_old+paid
    db.execute("UPDATE subscribers SET end_date=?,amount_paid=? WHERE customer_no=?", (new_end,new_paid,customer_no))
    db.commit()
    await update.message.reply_text(f"✅ تم التجديد حتى: {new_end} | 💵 مدفوع: {paid}")

@admin_only
async def cmd_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kv = parse_kv(update.message.text); days=int(kv.get("days",3))
    cur=db.execute("SELECT name,customer_no,end_date FROM subscribers WHERE end_date IS NOT NULL")
    rows=cur.fetchall(); now=datetime.now(TZ).date(); out=[]
    for n,c,e in rows:
        try:
            ed=datetime.fromisoformat(e).date()
            if ed<=now+timedelta(days=days): out.append((n,c,e))
        except: pass
    if not out: return await update.message.reply_text("لا يوجد مشتركين تنتهي اشتراكاتهم قريباً ✅")
    msg="الموشكون على الانتهاء:\n"+"\n".join([f"• {n} ({c}) — {d}" for n,c,d in out])
    await update.message.reply_text(msg)

# ------------ Import/Export ------------
@admin_only
async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists("subscribers.csv"): return await update.message.reply_text("⚠️ لا يوجد ملف subscribers.csv")
    with open("subscribers.csv",newline='',encoding="utf-8-sig") as f:
        rdr=csv.DictReader(f); count=0
        for r in rdr:
            db.execute("INSERT INTO subscribers (name,customer_no,plan,start_date,end_date,amount_paid) VALUES (?,?,?,?,?,?) "
                       "ON CONFLICT(customer_no) DO UPDATE SET name=excluded.name,plan=excluded.plan,start_date=excluded.start_date,end_date=excluded.end_date,amount_paid=excluded.amount_paid",
                       (r.get("name"),r.get("customer_no"),r.get("plan"),iso_or_none(r.get("start_date")),iso_or_none(r.get("end_date")),int(r.get("amount_paid",0))))
            count+=1
    db.commit(); await update.message.reply_text(f"✅ تم الاستيراد: {count} صف")

@admin_only
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    out="subscribers_export.csv"
    cur=db.execute("SELECT name,customer_no,plan,start_date,end_date,amount_paid FROM subscribers")
    rows=cur.fetchall()
    with open(out,"w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f); w.writerow(["name","customer_no","plan","start_date","end_date","amount_paid"]); w.writerows(rows)
    await update.message.reply_document(InputFile(out),filename=out,caption="⬇️ ملف التصدير")

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
    if row: await update.message.reply_text(row[0] if not db.is_pg else row["reply"])

# ------------ Menu Router ------------
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt=update.message.text.strip()
    if txt=="ℹ️ معلومات عن البوت":
        return await update.message.reply_text("بوت إدارة الاشتراكات 📊\n• إضافة/تجديد\n• استيراد/تصدير CSV\n• أوامر مخصصة ✅")
    if txt=="⭐ مميزات البوت":
        return await update.message.reply_text("• أزرار عربية سهلة\n• PostgreSQL/SQLite\n• أوامر مخصصة\n• لوحة مشرف")
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
    app.add_handler(CommandHandler("menu",start))
    app.add_handler(CommandHandler("addsub",cmd_addsub))
    app.add_handler(CommandHandler("renew",cmd_renew))
    app.add_handler(CommandHandler("due",cmd_due))
    app.add_handler(CommandHandler("import",cmd_import))
    app.add_handler(CommandHandler("export",cmd_export))
    app.add_handler(CommandHandler("setcommand",set_command))
    app.add_handler(CommandHandler("delcommand",del_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,menu_router))
    app.add_handler(MessageHandler(filters.COMMAND,custom_router))
    app.run_polling()

if __name__=="__main__":
    main()

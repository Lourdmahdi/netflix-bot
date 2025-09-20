# -*- coding: utf-8 -*-
import os, csv
from datetime import datetime, date, timedelta
from calendar import monthrange
from zoneinfo import ZoneInfo
from functools import wraps
from typing import Optional, Sequence, Any

from telegram import Update, InputFile, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
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
        # Convert ? placeholders to %s for PostgreSQL
        if self.is_pg:
            return "%s".join(sql.split("?"))
        return sql

    def execute(self, sql: str, params: Sequence[Any] = ()):
        cur = self.cursor()
        cur.execute(self.q(sql), params)
        return cur

    def commit(self):
        self.conn.commit()

    def _ensure_schema(self):
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
    if not s:
        return None
    s = s.strip()
    fmts = ["%Y-%m-%d", "YYYY/%m/%d".replace("YYYY", "%Y"), "%d-%m-%Y", "%d/%m/%Y"]
    for f in fmts:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except:
            pass
    try:
        return datetime.fromisoformat(s).date().isoformat()
    except:
        return None

def today_iso() -> str:
    return datetime.now(TZ).date().isoformat()

def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, monthrange(y, m)[1])
    return date(y, m, day)

def auto_customer_no(update: Update) -> str:
    base = int(update.message.date.timestamp()) % 100000
    return f"C{(update.effective_user.id % 1000):03d}{base:05d}"

# ------------ Menus (Reply/Inline) ------------
def main_menu_keyboard(is_admin_user: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("ℹ️ معلومات عن البوت"), KeyboardButton("⭐ مميزات البوت"), KeyboardButton("📚 الشروحات")],
        [KeyboardButton("➕ إنشاء/إضافة مشترك"), KeyboardButton("🧾 تجديد اشتراك"), KeyboardButton("⏰ قرب الانتهاء")],
        [KeyboardButton("⬅️ استيراد من CSV"), KeyboardButton("➡️ تصدير CSV")],
    ]
    if is_admin_user:
        rows.append([KeyboardButton("🔐 لوحة المشرف")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)

def about_inline_keyboard() -> InlineKeyboardMarkup:
    btns = [
        # ضع روابطك هنا إن أردت
        # [InlineKeyboardButton("قناة التحديثات", url="https://t.me/your_channel")]
    ]
    return InlineKeyboardMarkup(btns) if btns else InlineKeyboardMarkup([])

# ------------ Commands ------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_adm = is_admin(update.effective_user.id)
    store = "PostgreSQL" if db.is_pg else "SQLite"
    await update.message.reply_text(
        "مرحباً 👋\n"
        f"التخزين: {store}\n"
        "اختر من القائمة بالأسفل:",
        reply_markup=main_menu_keyboard(is_adm)
    )

@admin_only
async def cmd_addsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kv = parse_kv(update.message.text.partition(" ")[2])

    name = kv.get("اسم") or kv.get("name") or "بدون اسم"
    tg_username = kv.get("يوزر") or kv.get("tg_username")
    if tg_username and not tg_username.startswith("@"):
        tg_username = "@" + tg_username
    tg_user_id = int(kv.get("معرف") or kv.get("tg", "0")) if (kv.get("معرف") or kv.get("tg", "")).isdigit() else None
    customer_no = kv.get("رقم") or kv.get("customer_no") or auto_customer_no(update)
    plan = kv.get("خطة") or kv.get("plan")
    profiles_count = int(kv.get("بروفايلات") or kv.get("profiles_count") or 1)
    start_date = iso_or_none(kv.get("بداية") or kv.get("start")) or today_iso()
    end_date = iso_or_none(kv.get("نهاية") or kv.get("end"))
    amount_paid = int(float(kv.get("مدفوع") or kv.get("paid") or 0)) if (kv.get("مدفوع") or kv.get("paid")) else 0
    status = kv.get("حالة") or kv.get("status") or "active"
    note = kv.get("ملاحظة") or kv.get("note") or ""

    if db.is_pg:
        db.execute("""
            INSERT INTO subscribers
            (name,tg_username,tg_user_id,customer_no,plan,profiles_count,start_date,end_date,amount_paid,status,note)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (customer_no) DO UPDATE SET
              name=EXCLUDED.name,tg_username=EXCLUDED.tg_username,tg_user_id=EXCLUDED.tg_user_id,
              plan=EXCLUDED.plan,profiles_count=EXCLUDED.profiles_count,start_date=EXCLUDED.start_date,
              end_date=EXCLUDED.end_date,amount_paid=EXCLUDED.amount_paid,status=EXCLUDED.status,note=EXCLUDED.note
        """, (name, tg_username, tg_user_id, customer_no, plan, profiles_count, start_date, end_date, amount_paid, status, note))
    else:
        db.execute("""
            INSERT INTO subscribers
            (name,tg_username,tg_user_id,customer_no,plan,profiles_count,start_date,end_date,amount_paid,status,note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(customer_no) DO UPDATE SET
              name=excluded.name,tg_username=excluded.tg_username,tg_user_id=excluded.tg_user_id,
              plan=excluded.plan,profiles_count=excluded.profiles_count,start_date=excluded.start_date,
              end_date=excluded.end_date,amount_paid=excluded.amount_paid,status=excluded.status,note=excluded.note
        """, (name, tg_username, tg_user_id, customer_no, plan, profiles_count, start_date, end_date, amount_paid, status, note))
    db.commit()
    await update.message.reply_text(f"✅ تم حفظ المشترك: «{name}» (رقم: {customer_no})")

@admin_only
async def cmd_renew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 2:
        return await update.message.reply_text("📌 Usage: /renew <customer_no> months=1 paid=0")
    customer_no = parts[1]
    kv = parse_kv(parts[2] if len(parts) > 2 else "")

    months = int(kv.get("months", 1))
    paid = int(float(kv.get("paid", 0)))

    cur = db.execute("SELECT end_date, amount_paid FROM subscribers WHERE customer_no=?", (customer_no,))
    row = cur.fetchone()
    if not row:
        return await update.message.reply_text("⚠️ العميل غير موجود.")

    end_old = row["end_date"] if db.is_pg else row[0]
    paid_old = (row["amount_paid"] if db.is_pg else row[1]) or 0

    try:
        base = datetime.fromisoformat(end_old).date() if end_old else datetime.now(TZ).date()
    except:
        base = datetime.now(TZ).date()

    new_end = add_months(base, months).isoformat()
    new_paid = paid_old + paid

    db.execute("UPDATE subscribers SET end_date=?, amount_paid=? WHERE customer_no=?", (new_end, new_paid, customer_no))
    db.commit()
    await update.message.reply_text(f"✅ تم التجديد حتى: {new_end}\n💵 إضافة مدفوع: {paid}")

@admin_only
async def cmd_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kv = parse_kv(update.message.text)
    days = int(kv.get("days", 3))

    cur = db.execute("SELECT name, customer_no, end_date FROM subscribers WHERE end_date IS NOT NULL")
    rows = cur.fetchall() or []
    now = datetime.now(TZ).date()
    out = []
    for r in rows:
        name, cust, endd = (r["name"], r["customer_no"], r["end_date"]) if db.is_pg else (r[0], r[1], r[2])
        try:
            ed = datetime.fromisoformat(endd).date()
            if ed <= now + timedelta(days=days):
                out.append((name, cust, endd))
        except:
            pass

    if not out:
        return await update.message.reply_text("لا يوجد مشتركون تنتهي اشتراكاتهم قريبًا ✅")

    out.sort(key=lambda x: x[2])
    await update.message.reply_text(
        "المشتركون الموشكون على الانتهاء:\n" + "\n".join([f"• {n} ({c}) — ينتهي: {d}" for n, c, d in out])
    )

@admin_only
async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = "subscribers.csv"
    if not os.path.exists(path):
        return await update.message.reply_text("⚠️ لم أجد الملف subscribers.csv في مجلد المشروع.")

    count = 0
    with open(path, newline='', encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            name = r.get("name") or "بدون اسم"
            tg_username = r.get("tg_username")
            if tg_username and not tg_username.startswith("@"):
                tg_username = "@" + tg_username
            tg_user_id = int(r["tg_user_id"]) if r.get("tg_user_id") and str(r["tg_user_id"]).isdigit() else None
            customer_no = r.get("customer_no") or f"IMP{count:05d}"
            plan = r.get("plan")
            profiles_count = int(r.get("profiles_count") or 1)
            start_date = iso_or_none(r.get("start_date")) or today_iso()
            end_date = iso_or_none(r.get("end_date"))
            amount_paid = int(float(r.get("amount_paid") or 0))
            status = r.get("status") or "active"
            note = r.get("note") or ""

            if db.is_pg:
                db.execute("""
                    INSERT INTO subscribers
                    (name,tg_username,tg_user_id,customer_no,plan,profiles_count,start_date,end_date,amount_paid,status,note)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (customer_no) DO UPDATE SET
                      name=EXCLUDED.name,tg_username=EXCLUDED.tg_username,tg_user_id=EXCLUDED.tg_user_id,
                      plan=EXCLUDED.plan,profiles_count=EXCLUDED.profiles_count,start_date=EXCLUDED.start_date,
                      end_date=EXCLUDED.end_date,amount_paid=EXCLUDED.amount_paid,status=EXCLUDED.status,note=EXCLUDED.note
                """, (name, tg_username, tg_user_id, customer_no, plan, profiles_count, start_date, end_date, amount_paid, status, note))
            else:
                db.execute("""
                    INSERT INTO subscribers
                    (name,tg_username,tg_user_id,customer_no,plan,profiles_count,start_date,end_date,amount_paid,status,note)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(customer_no) DO UPDATE SET
                      name=excluded.name,tg_username=excluded.tg_username,tg_user_id=excluded.tg_user_id,
                      plan=excluded.plan,profiles_count=excluded.profiles_count,start_date=excluded.start_date,
                      end_date=excluded.end_date,amount_paid=excluded.amount_paid,status=excluded.status,note=excluded.note
                """, (name, tg_username, tg_user_id, customer_no, plan, profiles_count, start_date, end_date, amount_paid, status, note))
            count += 1
    db.commit()
    await update.message.reply_text(f"✅ تم الاستيراد: {count} صف.")

@admin_only
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    out_path = "subscribers_export.csv"
    cur = db.execute("""
        SELECT name,tg_username,tg_user_id,customer_no,plan,profiles_count,start_date,end_date,amount_paid,status,note
        FROM subscribers ORDER BY id DESC
    """)
    rows = cur.fetchall() or []
    headers = ["name","tg_username","tg_user_id","customer_no","plan","profiles_count","start_date","end_date","amount_paid","status","note"]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(headers)
        for r in rows:
            if db.is_pg: w.writerow([r[h] for h in headers])
            else: w.writerow(list(r))
    try:
        await update.message.reply_document(InputFile(out_path), filename=out_path, caption="⬇️ تم إنشاء ملف التصدير")
    except Exception as e:
        await update.message.reply_text(f"تم الحفظ محلياً: {out_path}\n({e})")

# ------------ Menu Router (text buttons) ------------
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()

    if txt == "ℹ️ معلومات عن البوت":
        return await update.message.reply_text(
            "بوت إدارة الاشتراكات (Netflix وغيرها).\n"
            "• إضافة/تجديد/عرض قرب الانتهاء\n"
            "• استيراد/تصدير CSV\n",
            reply_markup=about_inline_keyboard()
        )

    if txt == "⭐ مميزات البوت":
        return await update.message.reply_text(
            "• أزرار عربية سهلة\n"
            "• قاعدة بيانات PostgreSQL/SQLite\n"
            "• استيراد/تصدير CSV\n"
            "• تنبيهات قرب انتهاء (أمر /due)\n"
        )

    if txt == "📚 الشروحات":
        return await update.message.reply_text(
            "طريقة الاستخدام:\n"
            "• /addsub اسم= يوزر= معرف= خطة= بداية= نهاية= مدفوع=\n"
            "• /renew <رقم_العميل> months=1 paid=0\n"
            "• /due days=3\n"
            "• /import  | /export\n"
        )

    if txt == "➕ إنشاء/إضافة مشترك":
        return await update.message.reply_text(
            "أرسل الأمر هكذا:\n"
            '/addsub اسم="أحمد" يوزر=@ahmad معرف=123 خطة="نتفلكس" بداية=2025-10-01 نهاية=2025-11-01 مدفوع=5000'
        )

    if txt == "🧾 تجديد اشتراك":
        return await update.message.reply_text(
            "أرسل الأمر هكذا:\n"
            "/renew C00001 months=1 paid=5000"
        )

    if txt == "⏰ قرب الانتهاء":
        return await update.message.reply_text(
            "أرسل الأمر هكذا:\n"
            "/due days=7"
        )

    if txt == "⬅️ استيراد من CSV":
        return await update.message.reply_text(
            "ضع الملف subscribers.csv بجانب البوت ثم أرسل:\n"
            "/import"
        )

    if txt == "➡️ تصدير CSV":
        return await update.message.reply_text(
            "للحصول على نسخة من المشتركين بصيغة CSV أرسل:\n"
            "/export"
        )

    if txt == "🔐 لوحة المشرف":
        is_adm = is_admin(update.effective_user.id)
        return await update.message.reply_text(
            "لوحة المشرف:\n"
            "• /addsub  • /renew  • /due  • /import  • /export",
            reply_markup=main_menu_keyboard(is_adm)
        )

    # إن كان نص غير معروف
    is_adm = is_admin(update.effective_user.id)
    return await update.message.reply_text("اختر من الأزرار بالأسفل 👇", reply_markup=main_menu_keyboard(is_adm))

# ------------ Bootstrap ------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN غير معيّن.")
    app = Application.builder().token(BOT_TOKEN).build()

    # English command names (Telegram limitation), Arabic replies
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu",  start))      # لفتح القائمة دائمًا
    app.add_handler(CommandHandler("addsub", cmd_addsub))
    app.add_handler(CommandHandler("renew",  cmd_renew))
    app.add_handler(CommandHandler("due",    cmd_due))
    app.add_handler(CommandHandler("import", cmd_import))
    app.add_handler(CommandHandler("export", cmd_export))

    # Router for reply-keyboard text buttons
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    app.run_polling()

if __name__ == "__main__":
    main()

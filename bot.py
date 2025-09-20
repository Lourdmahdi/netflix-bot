# -*- coding: utf-8 -*-
import os
import csv
import sqlite3
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from functools import wraps
from typing import Optional

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, ContextTypes

# ====== الإعدادات ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}  # مثال: "123,456"
DB_PATH = os.getenv("DB_PATH", "subs.db")
TZ = ZoneInfo("Asia/Baghdad")

# ====== قاعدة البيانات ======
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS subscribers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        tg_username TEXT,
        tg_user_id INTEGER,
        customer_no TEXT UNIQUE,
        plan TEXT,
        profiles_count INTEGER DEFAULT 1,
        start_date TEXT,
        end_date TEXT,
        amount_paid INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        note TEXT
    )
    """)
    conn.commit()
    return conn

# ====== أدوات مساعدة ======
def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id) and (user_id in ADMIN_IDS)

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if not is_admin(uid):
            return await update.message.reply_text("❌ غير مسموح.")
        return await func(update, context)
    return wrapper

def parse_kv_ar(text: str):
    """
    يحوّل نصاً يحتوي أزواج مفتاح=قيمة (يدعم الاقتباسات)
    مثال: اسم="أحمد علي" يوزر=@ahmad معرف=123 بداية=2025-10-01
    """
    import shlex
    parts = shlex.split(text or "")
    out = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip()] = v.strip()
    return out

def iso_or_none(s: Optional[str]) -> Optional[str]:
    """يُعيد التاريخ بصيغة ISO YYYY-MM-DD إذا أمكن، أو None."""
    if not s:
        return None
    s = s.strip()
    # السماح بصيغ شائعة
    fmts = ["%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"]
    for f in fmts:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except Exception:
            pass
    # إذا كان أصلاً ISO صحيح
    try:
        return datetime.fromisoformat(s).date().isoformat()
    except Exception:
        return None

def today_iraq_iso() -> str:
    return datetime.now(TZ).date().isoformat()

def auto_customer_no(update: Update) -> str:
    base = int(update.message.date.timestamp()) % 100000
    return f"C{(update.effective_user.id % 1000):03d}{base:05d}"

# ====== أوامر عامة ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً 👋\n"
        "أوامر الإدارة:\n"
        "• /اضافة اسم= يوزر= معرف= رقم= خطة= بروفايلات= بداية= نهاية= مدفوع= حالة= ملاحظة=\n"
        "• /تجديد <رقم_العميل> اشهر= مدفوع=\n"
        "• /قرب_الانتهاء أيام=3\n"
        "• /استيراد  (من subscribers.csv)\n"
        "• /تصدير    (يحفظ subscribers_export.csv)\n"
    )

# ====== أوامر إدارية ======
@admin_only
async def اضافة(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args_text = update.message.text.partition(" ")[2]
    kv = parse_kv_ar(args_text)

    name = kv.get("اسم") or "بدون اسم"
    tg_username = kv.get("يوزر")
    if tg_username and not tg_username.startswith("@"):
        tg_username = "@" + tg_username

    tg_user_id = None
    try:
        tg_user_id = int(kv["معرف"]) if "معرف" in kv and kv["معرف"].isdigit() else None
    except Exception:
        tg_user_id = None

    customer_no = kv.get("رقم") or auto_customer_no(update)
    plan = kv.get("خطة")
    profiles_count = int(kv.get("بروفايلات", 1))

    start_date = iso_or_none(kv.get("بداية")) or today_iraq_iso()
    end_date = iso_or_none(kv.get("نهاية"))

    amount_paid = 0
    try:
        amount_paid = int(float(kv.get("مدفوع", 0)))
    except Exception:
        amount_paid = 0

    status = kv.get("حالة", "active")
    note = kv.get("ملاحظة", "")

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO subscribers
        (name, tg_username, tg_user_id, customer_no, plan, profiles_count, start_date, end_date, amount_paid, status, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (name, tg_username, tg_user_id, customer_no, plan, profiles_count, start_date, end_date, amount_paid, status, note))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ تم حفظ المشترك: «{name}» (رقم: {customer_no})")

@admin_only
async def تجديد(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        return await update.message.reply_text("📌 الصيغة: /تجديد <رقم_العميل> اشهر=1 مدفوع=0")

    customer_no = parts[1]
    kv = parse_kv_ar(parts[2] if len(parts) > 2 else "")

    months = 1
    try:
        months = int(kv.get("اشهر", 1))
    except Exception:
        months = 1

    paid = 0
    try:
        paid = int(float(kv.get("مدفوع", 0)))
    except Exception:
        paid = 0

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT end_date, amount_paid FROM subscribers WHERE customer_no=?", (customer_no,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return await update.message.reply_text("⚠️ العميل غير موجود.")

    end_date_old, amount_paid_old = row
    amount_paid_old = amount_paid_old or 0

    # تحديد تاريخ الأساس: إن لم يوجد/غير صالح → اليوم
    try:
        base = datetime.fromisoformat(end_date_old).date()
    except Exception:
        base = datetime.now(TZ).date()

    # إضافة أشهر
    from dateutil.relativedelta import relativedelta
    new_end = (base + relativedelta(months=months)).isoformat()
    new_paid = amount_paid_old + paid

    cur.execute("UPDATE subscribers SET end_date=?, amount_paid=? WHERE customer_no=?",
                (new_end, new_paid, customer_no))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ تم التجديد حتى: {new_end}\n💵 إضافة مدفوع: {paid}")

@admin_only
async def قرب_الانتهاء(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kv = parse_kv_ar(update.message.text or "")
    days = 3
    try:
        days = int(kv.get("أيام", 3))
    except Exception:
        days = 3

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT name, customer_no, end_date
        FROM subscribers
        WHERE end_date IS NOT NULL
          AND date(end_date) <= date('now', ?)
        ORDER BY date(end_date)
    """, (f'+{days} day',))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return await update.message.reply_text("لا يوجد مشتركون تنتهي اشتراكاتهم قريبًا ✅")

    lines = [f"• {n} ({c}) — ينتهي: {d}" for n, c, d in rows]
    await update.message.reply_text("المشتركون الموشكون على الانتهاء:\n" + "\n".join(lines))

@admin_only
async def استيراد(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    يستورد من subscribers.csv الموجود بجانب البوت.
    الحقول المقبولة: name,tg_username,tg_user_id,customer_no,plan,profiles_count,start_date,end_date,amount_paid,status,note
    """
    path = "subscribers.csv"
    if not os.path.exists(path):
        return await update.message.reply_text("⚠️ لم أجد الملف subscribers.csv في مجلد المشروع.")

    conn = db()
    cur = conn.cursor()
    added, replaced = 0, 0

    with open(path, newline='', encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            # تنظيف وتحويل
            name = r.get("name") or "بدون اسم"
            tg_username = r.get("tg_username")
            if tg_username and not tg_username.startswith("@"):
                tg_username = "@" + tg_username
            try:
                tg_user_id = int(r["tg_user_id"]) if r.get("tg_user_id") else None
            except Exception:
                tg_user_id = None

            customer_no = r.get("customer_no") or f"C{added:05d}"
            plan = r.get("plan")
            try:
                profiles_count = int(r.get("profiles_count") or 1)
            except Exception:
                profiles_count = 1

            start_date = iso_or_none(r.get("start_date")) or today_iraq_iso()
            end_date = iso_or_none(r.get("end_date"))
            try:
                amount_paid = int(float(r.get("amount_paid") or 0))
            except Exception:
                amount_paid = 0
            status = r.get("status") or "active"
            note = r.get("note") or ""

            # INSERT OR REPLACE يحدّث لو وُجد customer_no
            cur.execute("""
                INSERT OR REPLACE INTO subscribers
                (name, tg_username, tg_user_id, customer_no, plan, profiles_count, start_date, end_date, amount_paid, status, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, tg_username, tg_user_id, customer_no, plan, profiles_count, start_date, end_date, amount_paid, status, note))
            # تقدير بسيطة للعدّ
            if cur.rowcount == 1:
                added += 1
            else:
                replaced += 1

    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ تم الاستيراد.\nجديد: {added} | تحديث: {replaced}")

@admin_only
async def تصدير(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    يصدر كل المشتركين إلى subscribers_export.csv ويرسله كملف.
    """
    out_path = "subscribers_export.csv"
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT name,tg_username,tg_user_id,customer_no,plan,profiles_count,start_date,end_date,amount_paid,status,note
        FROM subscribers
        ORDER BY id DESC
    """)
    rows = cur.fetchall()
    conn.close()

    headers = ["name","tg_username","tg_user_id","customer_no","plan","profiles_count","start_date","end_date","amount_paid","status","note"]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for row in rows:
            w.writerow(row)

    try:
        await update.message.reply_document(InputFile(out_path), filename=os.path.basename(out_path),
                                            caption="⬇️ تم إنشاء ملف التصدير")
    except Exception as e:
        await update.message.reply_text(f"تم الحفظ محلياً: {out_path}\n({e})")

# ====== نقطة التشغيل ======
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN غير معيّن في المتغيرات البيئية.")

    app = Application.builder().token(BOT_TOKEN).build()

    # أوامر عامة
    app.add_handler(CommandHandler(["start", "ابدأ"], start))

    # أوامر إدارية
    app.add_handler(CommandHandler("اضافة", اضافة))
    app.add_handler(CommandHandler("تجديد", تجديد))
    app.add_handler(CommandHandler("قرب_الانتهاء", قرب_الانتهاء))
    app.add_handler(CommandHandler("استيراد", استيراد))
    app.add_handler(CommandHandler("تصدير", تصدير))

    app.run_polling()

if __name__ == "__main__":
    main()

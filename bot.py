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
        status TEXT DEFAULT 'active', -- active|expired|suspended
        notes TEXT,
        created_at TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_no TEXT,
        amount INTEGER,
        paid_at TEXT,
        method TEXT,
        reference TEXT
    )
    """)
    conn.commit()
    return conn

# ====== أدوات مساعدة ======
def is_admin(uid: Optional[int]) -> bool:
    return uid in ADMIN_IDS

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if not is_admin(uid):
            await update.effective_message.reply_text("عذرًا، هذا الأمر للأدمن فقط.")
            return
        return await func(update, context)
    return wrapper

def parse_date(s: str) -> datetime:
    # يقبل YYYY-MM-DD
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=TZ)

def fmt_date(d: Optional[str]) -> str:
    if not d:
        return "-"
    try:
        return datetime.fromisoformat(d).date().isoformat()
    except Exception:
        try:
            return parse_date(d).date().isoformat()
        except Exception:
            return d

def today() -> datetime:
    return datetime.now(TZ)

# ====== إنشاء فاتورة PDF (ReportLab) ======
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

def make_invoice_pdf(customer_no: str, name: str, plan: str, amount: int,
                     start_date: str, end_date: str, method: str, reference: str) -> str:
    fn = f"invoice_{customer_no}_{today().strftime('%Y%m%d_%H%M%S')}.pdf"
    c = canvas.Canvas(fn, pagesize=A4)
    w, h = A4

    # رأس بسيط
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, h-60, "فاتورة اشتراك")
    c.setFont("Helvetica", 10)
    c.drawString(40, h-80, f"التاريخ: {today().date().isoformat()}")
    c.drawString(40, h-95, f"رقم الزبون: {customer_no}")
    c.drawString(40, h-110, f"الاسم: {name or '-'}")

    y = h-150
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, "تفاصيل")
    y -= 20
    c.setFont("Helvetica", 11)
    items = [
        ("الخطة", plan or "-"),
        ("عدد البروفايلات", "-"),
        ("فترة الاشتراك", f"{fmt_date(start_date)} → {fmt_date(end_date)}"),
        ("المبلغ المدفوع", f"{amount}"),
        ("طريقة الدفع", method or "-"),
        ("المرجع", reference or "-"),
    ]
    for k, v in items:
        c.drawString(40, y, f"{k}: {v}")
        y -= 18

    # ملاحظة
    y -= 10
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(40, y, "هذه الفاتورة صادرة من نظام إدارة المشتركين (تلغرام بوت).")
    c.showPage()
    c.save()
    return fn

# ====== رسائل جاهزة للمشترك ======
def build_profile_instruction(account_login: str, profile_display_name: str, has_pin: bool) -> str:
    return (
        "تعليمات الوصول للبروفايل:\n"
        f"1) سجّل الدخول إلى نتفلكس باستخدام الحساب: {account_login}\n"
        f"2) اختر البروفايل باسم: {profile_display_name}\n"
        f"3) { 'أدخل رمز PIN المرسل لك.' if has_pin else 'لا يوجد PIN لهذا البروفايل.' }\n"
        "ملاحظة: لا تقم بتغيير اسم البروفايل أو إعداداته."
    )

# ====== أوامر عامة ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if is_admin(uid):
        await update.message.reply_text(
            "أهلًا أدمن 👋\n"
            "الأوامر: /addsub /editsub /setstatus /renew /list /active /due /find /export /invoice /sendmsg\n"
            "صيَغ سريعة: \n"
            "/addsub <name> <customer_no> <plan> <profiles> <start> <end> <amount> [@username]\n"
            "التواريخ بصيغة YYYY-MM-DD"
        )
    else:
        await update.message.reply_text("مرحبًا! هذا بوت إدارة المشتركين.")

# ====== أوامر الأدمن ======
@admin_only
async def addsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /addsub <name> <customer_no> <plan> <profiles> <start> <end> <amount> [@username]
    """
    args = context.args
    if len(args) < 7:
        await update.message.reply_text(
            "استعمال: /addsub <name> <customer_no> <plan> <profiles> <start> <end> <amount> [@username]"
        )
        return

    name = args[0]
    customer_no = args[1]
    plan = args[2]
    try:
        profiles_count = int(args[3])
    except:
        await update.message.reply_text("profiles يجب أن يكون رقمًا.")
        return
    start_date = args[4]
    end_date = args[5]
    try:
        amount = int(args[6])
    except:
        await update.message.reply_text("amount يجب أن يكون رقمًا (بالدنانير مثلًا).")
        return
    tg_username = args[7].lstrip("@") if len(args) >= 8 else None

    conn = db()
    try:
        conn.execute("""
            INSERT INTO subscribers
            (name, tg_username, tg_user_id, customer_no, plan, profiles_count, start_date, end_date, amount_paid, status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (name, tg_username, None, customer_no, plan, profiles_count, start_date, end_date, amount, "active", today().isoformat()))
        conn.commit()
        await update.message.reply_text(f"تمت إضافة المشترك ✅\n#{customer_no} | {name} | {plan} | {profiles_count}P | {start_date}→{end_date} | {amount}")
    except sqlite3.IntegrityError:
        await update.message.reply_text("❗ رقم الزبون موجود مسبقًا. استخدم /editsub أو /setstatus.")
    finally:
        conn.close()

@admin_only
async def editsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /editsub <customer_no> field=value ...
    الحقول: name, plan, profiles, start, end, amount, status, username
    مثال: /editsub 1001 plan=premium profiles=2 end=2025-10-15 amount=8000
    """
    if len(context.args) < 2:
        await update.message.reply_text("استعمال: /editsub <customer_no> field=value ...")
        return
    customer_no = context.args[0]
    fields = {}
    for part in context.args[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip()

    allowed = {"name", "plan", "profiles", "start", "end", "amount", "status", "username"}
    if not set(fields.keys()).issubset(allowed):
        await update.message.reply_text(f"الحقول المسموحة: {', '.join(sorted(allowed))}")
        return

    setters = []
    params = []
    if "name" in fields: setters.append("name=?"); params.append(fields["name"])
    if "plan" in fields: setters.append("plan=?"); params.append(fields["plan"])
    if "profiles" in fields: setters.append("profiles_count=?"); params.append(int(fields["profiles"]))
    if "start" in fields: setters.append("start_date=?"); params.append(fields["start"])
    if "end" in fields: setters.append("end_date=?"); params.append(fields["end"])
    if "amount" in fields: setters.append("amount_paid=?"); params.append(int(fields["amount"]))
    if "status" in fields: setters.append("status=?"); params.append(fields["status"])
    if "username" in fields: setters.append("tg_username=?"); params.append(fields["username"].lstrip("@"))

    if not setters:
        await update.message.reply_text("لم يتم تحديد أي تغييرات.")
        return

    params.append(customer_no)
    conn = db()
    cur = conn.execute(f"UPDATE subscribers SET {', '.join(setters)} WHERE customer_no=?", tuple(params))
    conn.commit()
    cnt = cur.rowcount
    conn.close()
    await update.message.reply_text("تم التعديل ✅" if cnt else "لم يتم العثور على هذا الرقم.")

@admin_only
async def setstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setstatus <customer_no> <active|expired|suspended>
    """
    if len(context.args) != 2 or context.args[1] not in ("active", "expired", "suspended"):
        await update.message.reply_text("استعمال: /setstatus <customer_no> <active|expired|suspended>")
        return
    customer_no, status = context.args
    conn = db()
    cur = conn.execute("UPDATE subscribers SET status=? WHERE customer_no=?", (status, customer_no))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"تم تحديث حالة #{customer_no} إلى {status} ✅" if cur.rowcount else "لم يُعثر على الرقم.")

@admin_only
async def renew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /renew <customer_no> <months> <amount>
    يمدد end_date بعدد الأشهر ويجمع المبلغ إلى amount_paid
    """
    if len(context.args) != 3:
        await update.message.reply_text("استعمال: /renew <customer_no> <months> <amount>")
        return
    customer_no, months_s, amount_s = context.args
    months = int(months_s); amount = int(amount_s)

    conn = db()
    row = conn.execute("SELECT end_date, amount_paid FROM subscribers WHERE customer_no=?", (customer_no,)).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("الرقم غير موجود.")
        return
    end_date_str, amount_paid = row
    base = max(today(), datetime.fromisoformat(end_date_str).replace(tzinfo=TZ))
    new_end = (base + timedelta(days=30*months)).date().isoformat()
    new_amount = (amount_paid or 0) + amount

    conn.execute("UPDATE subscribers SET end_date=?, amount_paid=?, status='active' WHERE customer_no=?",
                 (new_end, new_amount, customer_no))
    # سجل دفعه
    conn.execute("INSERT INTO payments (customer_no, amount, paid_at, method, reference) VALUES (?,?,?,?,?)",
                 (customer_no, amount, today().isoformat(), "cash", f"renew_{today().strftime('%Y%m%d')}"))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"تم التجديد ✅\n#{customer_no} حتى {new_end} | مجموع مدفوع: {new_amount}")

@admin_only
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /list [page]
    """
    page = int(context.args[0]) if context.args else 1
    page = max(page, 1)
    size = 20
    off = (page - 1) * size
    conn = db()
    rows = conn.execute("""
        SELECT name, customer_no, plan, profiles_count, start_date, end_date, amount_paid, status
        FROM subscribers ORDER BY id DESC LIMIT ? OFFSET ?
    """, (size, off)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("لا يوجد مشتركين في هذه الصفحة.")
        return
    lines = [f"📄 صفحة {page}:"]
    for n, c, p, pc, s, e, a, st in rows:
        lines.append(f"- #{c} | {n} | {p} | {pc}P | {fmt_date(s)}→{fmt_date(e)} | {a} | {st}")
    await update.message.reply_text("\n".join(lines))

@admin_only
async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute("""
        SELECT name, customer_no, plan, profiles_count FROM subscribers
        WHERE status='active' ORDER BY id DESC
    """).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("لا يوجد مشتركين نشطين.")
        return
    lines = ["✅ النشطون:"]
    for n, c, p, pc in rows[:200]:
        lines.append(f"- #{c} | {n} | {p} | {pc}P")
    await update.message.reply_text("\n".join(lines))

@admin_only
async def due_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /due [days]
    يعرض من ستنتهي اشتراكاتهم خلال N يوم (افتراضي 7)
    """
    days = int(context.args[0]) if context.args else 7
    now = today()
    up_to = (now + timedelta(days=days)).date().isoformat()
    conn = db()
    rows = conn.execute("""
        SELECT name, customer_no, plan, end_date FROM subscribers
        WHERE status='active' AND date(end_date) <= date(?)
        ORDER BY date(end_date) ASC
    """, (up_to,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text(f"لا أحد ينتهي خلال {days} يوم.")
        return
    lines = [f"⏰ تنتهي خلال {days} يوم:"]
    for n, c, p, e in rows:
        lines.append(f"- #{c} | {n} | {p} | ينتهي: {fmt_date(e)}")
    await update.message.reply_text("\n".join(lines))

@admin_only
async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استعمال: /find <نص>")
        return
    q = " ".join(context.args)
    like = f"%{q}%"
    conn = db()
    rows = conn.execute("""
        SELECT name, customer_no, plan, profiles_count, end_date, status FROM subscribers
        WHERE name LIKE ? OR customer_no LIKE ? OR plan LIKE ? OR tg_username LIKE ?
        ORDER BY id DESC LIMIT 50
    """, (like, like, like, like)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("لا نتائج.")
        return
    lines = [f"🔎 نتائج '{q}':"]
    for n, c, p, pc, e, st in rows:
        lines.append(f"- #{c} | {n} | {p} | {pc}P | حتى {fmt_date(e)} | {st}")
    await update.message.reply_text("\n".join(lines))

@admin_only
async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = f"subscribers_{today().strftime('%Y%m%d_%H%M%S')}.csv"
    conn = db()
    rows = conn.execute("""
        SELECT name, customer_no, plan, profiles_count, start_date, end_date, amount_paid, status, tg_username, created_at
        FROM subscribers ORDER BY id DESC
    """).fetchall()
    conn.close()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name","customer_no","plan","profiles_count","start_date","end_date","amount_paid","status","tg_username","created_at"])
        w.writerows(rows)
    await update.message.reply_document(open(path, "rb"), filename=os.path.basename(path),
                                        caption="تصدير المشتركين (CSV)")

@admin_only
async def invoice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /invoice <customer_no> <amount> [method] [reference]
    ينشئ إيصال دفع + يرفق PDF
    """
    if len(context.args) < 2:
        await update.message.reply_text("استعمال: /invoice <customer_no> <amount> [method] [reference]")
        return
    customer_no = context.args[0]
    amount = int(context.args[1])
    method = context.args[2] if len(context.args) >= 3 else "cash"
    reference = context.args[3] if len(context.args) >= 4 else "-"

    conn = db()
    row = conn.execute("""
        SELECT name, plan, profiles_count, start_date, end_date FROM subscribers WHERE customer_no=?
    """, (customer_no,)).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("الرقم غير موجود.")
        return
    name, plan, profiles_count, s, e = row

    # سجّل الدفع
    conn.execute("INSERT INTO payments (customer_no, amount, paid_at, method, reference) VALUES (?,?,?,?,?)",
                 (customer_no, amount, today().isoformat(), method, reference))
    conn.execute("UPDATE subscribers SET amount_paid = COALESCE(amount_paid,0) + ? WHERE customer_no=?",
                 (amount, customer_no))
    conn.commit()
    conn.close()

    pdf_path = make_invoice_pdf(customer_no, name, plan, amount, s, e, method, reference)
    await update.message.reply_document(open(pdf_path, "rb"), filename=os.path.basename(pdf_path),
                                        caption=f"فاتورة #{customer_no} بقيمة {amount}")

@admin_only
async def sendmsg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sendmsg <customer_no> <message...>
    يرسل تعليمات للمشترك عبر tg_username (لو موجودة)
    """
    if len(context.args) < 2:
        await update.message.reply_text("استعمال: /sendmsg <customer_no> <message...>")
        return
    customer_no = context.args[0]
    message = " ".join(context.args[1:])
    conn = db()
    row = conn.execute("SELECT tg_username FROM subscribers WHERE customer_no=?", (customer_no,)).fetchone()
    conn.close()
    if not row or not row[0]:
        await update.message.reply_text("لا يوجد tg_username مخزون لهذا المشترك.")
        return
    username = row[0]
    try:
        # نرسل عبر username (قد يتطلّب أن يكون البوت تواصل معه سابقًا)
        await update.get_bot().send_message(chat_id=f"@{username}", text=message)
        await update.message.reply_text("تم إرسال الرسالة ✅")
    except Exception as e:
        await update.message.reply_text(f"تعذر الإرسال: {e}")

# ====== التذكيرات التلقائية (Job Queue) ======
async def daily_reminders(context: ContextTypes.DEFAULT_TYPE):
    """
    يُشغّل يوميًا:
    - يرسل تذكير قبل 3 أيام من الانتهاء.
    - يرسل تنبيه يوم الانتهاء.
    * يُرسل للإدمن كقائمة، ويمكنك لاحقًا ربطه برسائل موجهة للمستخدمين عند توفر chat_id.
    """
    conn = db()
    now = today().date()
    d3 = (today() + timedelta(days=3)).date().isoformat()
    today_str = now.isoformat()

    # قبل 3 أيام
    due_soon = conn.execute("""
        SELECT name, customer_no, plan, end_date FROM subscribers
        WHERE status='active' AND date(end_date)=date(?)
    """, (d3,)).fetchall()

    # اليوم
    due_today = conn.execute("""
        SELECT name, customer_no, plan, end_date FROM subscribers
        WHERE status='active' AND date(end_date)=date(?)
    """, (today_str,)).fetchall()

    conn.close()

    if ADMIN_IDS:
        admin_id = next(iter(ADMIN_IDS))
        if due_soon:
            lines = ["⏳ تذكير: تنتهي بعد 3 أيام:"]
            for n, c, p, e in due_soon:
                lines.append(f"- #{c} | {n} | {p} | {fmt_date(e)}")
            await context.bot.send_message(chat_id=admin_id, text="\n".join(lines))

        if due_today:
            lines = ["⚠️ تنتهي اليوم:"]
            for n, c, p, e in due_today:
                lines.append(f"- #{c} | {n} | {p} | {fmt_date(e)}")
            await context.bot.send_message(chat_id=admin_id, text="\n".join(lines))

# >>> النسخة الصحيحة للجدولة مع PTB v20 <<<
def schedule_jobs(application: Application):
    # يشغّل daily_reminders كل يوم الساعة 10:00 صباحًا بتوقيت بغداد
    application.job_queue.run_daily(
        daily_reminders,
        time=time(10, 0, tzinfo=TZ),
        name="daily_reminders"
    )
    # (اختياري) اختبار سريع بعد الإقلاع بـ 20 ثانية:
    # application.job_queue.run_once(daily_reminders, when=20, name="boot_test")

# ====== الإقلاع ======
def main():
    if not BOT_TOKEN:
        raise RuntimeError("ضع BOT_TOKEN في المتغيرات البيئية")
    if not ADMIN_IDS:
        print("تحذير: ADMIN_IDS فارغة! لن يستطيع أحد استخدام أوامر الأدمن.")

    # تأكد من تهيئة قاعدة البيانات
    conn = db(); conn.close()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addsub", addsub))
    application.add_handler(CommandHandler("editsub", editsub))
    application.add_handler(CommandHandler("setstatus", setstatus))
    application.add_handler(CommandHandler("renew", renew))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("active", active_cmd))
    application.add_handler(CommandHandler("due", due_cmd))
    application.add_handler(CommandHandler("find", find_cmd))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("invoice", invoice_cmd))
    application.add_handler(CommandHandler("sendmsg", sendmsg_cmd))

    # جدولة التذكيرات اليومية
    schedule_jobs(application)

    # تشغيل البوت
    application.run_polling()

if __name__ == "__main__":
    main()

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

conn = psycopg2.connect(DATABASE_URL, sslmode='require')
cur = conn.cursor(cursor_factory=RealDictCursor)

cur.execute("""
CREATE TABLE IF NOT EXISTS subscribers (
    user_id BIGINT PRIMARY KEY,
    profile_name TEXT,
    start_date DATE,
    end_date DATE
)
""")
conn.commit()

async def reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(
        job.chat_id,
        text=f"⏰ تذكير: اشتراكك ({job.data['profile']}) ينتهي بتاريخ {job.data['end_date']}."
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 أهلاً! استخدم /register اسم_البروفايل الأيام")

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        profile_name = context.args[0]
        days = int(context.args[1])
        user_id = update.effective_user.id
        start_date = datetime.utcnow().date()
        end_date = start_date + timedelta(days=days)

        cur.execute("""
            INSERT INTO subscribers (user_id, profile_name, start_date, end_date)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE
              SET profile_name = EXCLUDED.profile_name,
                  start_date = EXCLUDED.start_date,
                  end_date = EXCLUDED.end_date
        """, (user_id, profile_name, start_date, end_date))
        conn.commit()

        reminder_time = datetime.combine(end_date - timedelta(days=2), datetime.min.time())
        context.job_queue.run_once(
            reminder,
            when=reminder_time,
            chat_id=user_id,
            name=f"reminder_{user_id}",
            data={"profile": profile_name, "end_date": end_date.isoformat()}
        )

        await update.message.reply_text(f"✅ تم تسجيلك. ينتهي: {end_date.isoformat()}")
    except Exception:
        await update.message.reply_text("⚠️ استخدم الصيغة: /register الاسم الأيام")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cur.execute("SELECT profile_name, start_date, end_date FROM subscribers WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if row:
        await update.message.reply_text(f"👤 بروفايل: {row['profile_name']}\nبداية: {row['start_date']}\nنهاية: {row['end_date']}")
    else:
        await update.message.reply_text("❌ لا يوجد اشتراك. استخدم /register")

async def renew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(context.args[0])
        user_id = update.effective_user.id
        cur.execute("SELECT end_date, profile_name FROM subscribers WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        if not row:
            await update.message.reply_text("❌ لا يوجد اشتراك. استخدم /register")
            return
        old_end = row['end_date']
        new_end = old_end + timedelta(days=days)
        cur.execute("UPDATE subscribers SET end_date=%s WHERE user_id=%s", (new_end, user_id))
        conn.commit()

        reminder_time = datetime.combine(new_end - timedelta(days=2), datetime.min.time())
        context.job_queue.run_once(
            reminder,
            when=reminder_time,
            chat_id=user_id,
            name=f"reminder_{user_id}",
            data={"profile": row['profile_name'], "end_date": new_end.isoformat()}
        )

        await update.message.reply_text(f"🔄 تم التجديد حتى {new_end.isoformat()}")
    except Exception:
        await update.message.reply_text("⚠️ استخدم الصيغة: /renew الأيام")

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📞 الدعم: @YourUserName")

def main():
    if not TOKEN:
        raise RuntimeError("ضع TELEGRAM_BOT_TOKEN في متغيرات Heroku")
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("renew", renew))
    app.add_handler(CommandHandler("support", support))

    print("✅ Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()

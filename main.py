import logging
from telegram import Update, ForceReply
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from datetime import datetime, timedelta
import pytz

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
NAME, DUE_DATE = range(2)

# Initialize scheduler (but don't start it yet)
jobstores = {
    'default': MemoryJobStore()
}
scheduler = AsyncIOScheduler(jobstores=jobstores, timezone=pytz.utc)

# Global bot instance
bot_instance = None

# Helper: Parse job ID to extract user_id and timestamp
def parse_job_id(job_id: str):
    """
    Parses job ID like: "d1_123456789_1712345678"
    Returns: (prefix, user_id, timestamp)
    """
    parts = job_id.split('_')
    if len(parts) != 3:
        return None, None, None
    prefix = parts[0]  # d1, h12, due
    try:
        user_id = int(parts[1])
        timestamp = int(parts[2])
        return prefix, user_id, timestamp
    except ValueError:
        return None, None, None

# Helper: Get due datetime from timestamp
def get_due_from_timestamp(timestamp: int):
    tz = pytz.timezone('UTC')
    return datetime.fromtimestamp(timestamp, tz=tz)

# Send reminder function
async def send_reminder_proper(chat_id: int, assignment_name: str, message_prefix: str):
    try:
        await bot_instance.send_message(
            chat_id=chat_id,
            text=f"{message_prefix}\n📌 Assignment: {assignment_name}"
        )
        logger.info(f"Sent reminder to {chat_id}: {message_prefix} - {assignment_name}")
    except Exception as e:
        logger.error(f"Failed to send reminder to {chat_id}: {e}")

send_reminder = send_reminder_proper

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Hi! I'm your Reminder Bot.\n"
        "Use /makereminder to set a new reminder.\n"
        "Use /listreminders to see your upcoming assignments.\n"
        "I’ll remind you at D-1, H-12, and Due Time!"
    )

# Begin reminder creation
async def makereminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "📝 Please enter the name of the assignment:",
        reply_markup=ForceReply(selective=True),
    )
    return NAME

# Get assignment name
async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assignment_name = update.message.text
    context.user_data['assignment_name'] = assignment_name

    await update.message.reply_text(
        f"📅 Got it! When is '{assignment_name}' due?\n"
        "Please enter date and time in format: YYYY-MM-DD HH:MM\n"
        "Example: 2025-06-15 14:30",
        reply_markup=ForceReply(selective=True),
    )
    return DUE_DATE

# Get due date and schedule reminders
async def get_due_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    due_date_str = update.message.text
    assignment_name = context.user_data['assignment_name']
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id

    try:
        # Parse due date
        due_datetime = datetime.strptime(due_date_str, "%Y-%m-%d %H:%M")
        tz = pytz.timezone('UTC')  # Change if needed
        due_datetime = tz.localize(due_datetime)
        timestamp = int(due_datetime.timestamp())

        # Calculate reminder times
        d1_reminder = due_datetime - timedelta(days=1)
        h12_reminder = due_datetime - timedelta(hours=12)

        # Schedule D-1 Reminder
        scheduler.add_job(
            send_reminder,
            'date',
            run_date=d1_reminder,
            args=[chat_id, assignment_name, "📆 D-1 Reminder: Due tomorrow!"],
            id=f"d1_{user_id}_{timestamp}",
            replace_existing=True,
        )

        # Schedule H-12 Reminder
        scheduler.add_job(
            send_reminder,
            'date',
            run_date=h12_reminder,
            args=[chat_id, assignment_name, "⏰ H-12 Reminder: Due in 12 hours!"],
            id=f"h12_{user_id}_{timestamp}",
            replace_existing=True,
        )

        # Schedule Due Now Reminder
        scheduler.add_job(
            send_reminder,
            'date',
            run_date=due_datetime,
            args=[chat_id, assignment_name, "🚨 DUE NOW: Time to submit!"],
            id=f"due_{user_id}_{timestamp}",
            replace_existing=True,
        )

        await update.message.reply_text(
            f"✅ All reminders set for '{assignment_name}'!\n"
            f"• D-1: {d1_reminder.strftime('%Y-%m-%d %H:%M')}\n"
            f"• H-12: {h12_reminder.strftime('%Y-%m-%d %H:%M')}\n"
            f"• Due: {due_datetime.strftime('%Y-%m-%d %H:%M')}\n"
            f"I’ll notify you here on time!"
        )

        logger.info(f"Reminders scheduled for '{assignment_name}' at {due_date_str}")

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid format. Please use: YYYY-MM-DD HH:MM\n"
            "Example: 2025-06-15 14:30"
        )
        return DUE_DATE  # Stay in this state

    return ConversationHandler.END

# ➕ List Reminders Command
async def listreminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    jobs = scheduler.get_jobs()

    # Filter jobs for this user
    user_jobs = [job for job in jobs if f"_{user_id}_" in job.id]

    if not user_jobs:
        await update.message.reply_text("📭 You have no upcoming reminders.")
        return

    # Group by assignment (using timestamp)
    assignments = {}
    for job in user_jobs:
        prefix, job_user_id, timestamp = parse_job_id(job.id)
        if job_user_id != user_id or timestamp is None:
            continue

        if timestamp not in assignments:
            due_dt = get_due_from_timestamp(timestamp)
            assignment_name = job.args[1] if len(job.args) > 1 else "Unknown Assignment"
            assignments[timestamp] = {
                'name': assignment_name,
                'due': due_dt,
                'reminders': []
            }
        # Add reminder type
        label = {
            'd1': 'D-1',
            'h12': 'H-12',
            'due': 'Due Now'
        }.get(prefix, prefix)
        assignments[timestamp]['reminders'].append({
            'type': label,
            'time': job.next_run_time
        })

    # Build message
    message = "📋 *Your Upcoming Assignments:*\n\n"
    for ts, data in sorted(assignments.items(), key=lambda x: x[1]['due']):
        due_str = data['due'].strftime('%Y-%m-%d %H:%M')
        message += f"📌 *{data['name']}*\n"
        message += f"   🗓️ Due: {due_str}\n"
        for rem in sorted(data['reminders'], key=lambda x: x['time']):
            time_str = rem['time'].strftime('%Y-%m-%d %H:%M')
            message += f"   🔔 {rem['type']} → {time_str}\n"
        message += "\n"

    await update.message.reply_text(message, parse_mode="Markdown")

# Cancel conversation
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Reminder creation cancelled.")
    return ConversationHandler.END

# ✅ START SCHEDULER AFTER EVENT LOOP IS READY
async def start_scheduler(app: Application):
    """Start the APScheduler after bot and event loop are ready."""
    scheduler.start()
    print("✅ Scheduler started successfully!")

# Main function
def main() -> None:
    TOKEN = "7367445467:AAHiqN3V0z2nfAA0MG8-T9qAnio9pqzSPwQ"  # 🔑 Your token

    application = Application.builder().token(TOKEN).build()

    global bot_instance
    bot_instance = application.bot

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("makereminder", makereminder)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            DUE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_due_date)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("listreminders", listreminders))
    application.add_handler(conv_handler)

    # ✅ Register scheduler starter — runs AFTER event loop starts
    application.post_init = start_scheduler

    # Run the bot
    print("🚀 Reminder Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
import asyncio
import json
import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    PollAnswerHandler,
)

# =========================
# Hardcoded config
# =========================
BOT_TOKEN = "8183120153:AAF3k3FZViX33glskyf-CTi2F3LoxulGvV0"
CHAT_ID = -5299275232
API_URL = "https://uat.dmz.finance/stores/tdd/qcdt/new_price"

TZ = ZoneInfo("Asia/Singapore")

# Window: weekdays only, 15:30–20:30 SGT
WINDOW_START = dtime(15, 30)
WINDOW_END = dtime(20, 30)

# Check every 2 minutes (time-based)
ALLOWED_MINUTES = set(range(0, 60, 2))  # 0,2,4,...,58

HTTP_TIMEOUT_SECONDS = 15
ERROR_COOLDOWN = timedelta(minutes=60)

CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =========================
# In-memory state
# =========================
state = {
    "last_seen_update_time": None,
    "done_date": None,               # YYYY-MM-DD (SGT) -> stop for day after poll answered
    "pending_poll": False,
    "pending_payload": None,         # stored until poll answer
    "pending_poll_id": None,
    "pending_poll_msg_id": None,
    "last_error_alert_at": None,
    "last_check_key": None,
}

CHECK_JOB_NAME = "qcdt_check_job"
check_job = None

# =========================
# Time helpers
# =========================
def now_sgt() -> datetime:
    return datetime.now(TZ)

def today_str_sgt() -> str:
    return now_sgt().strftime("%Y-%m-%d")

def is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5

def in_window(dt: datetime) -> bool:
    t = dt.time()
    return WINDOW_START <= t <= WINDOW_END

def is_scheduled_minute(dt: datetime) -> bool:
    return dt.minute in ALLOWED_MINUTES

def parse_update_time_sgt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)

def fmt_price_date(price_date_str: str) -> str:
    d = datetime.strptime(price_date_str, "%Y-%m-%d").date()
    return d.strftime("%d %b %Y")

def should_send_error_alert(dt: datetime) -> bool:
    last = state["last_error_alert_at"]
    return last is None or (dt - last) >= ERROR_COOLDOWN

# =========================
# API fetch
# =========================
def fetch_payload_sync() -> dict:
    r = requests.get(API_URL, timeout=HTTP_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json()

async def fetch_payload() -> dict:
    return await asyncio.to_thread(fetch_payload_sync)

# =========================
# Message builders
# =========================
def build_json_only(payload: dict) -> str:
    # EXACTLY show only the JSON payload
    return f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>"

def build_ack_message(payload: dict) -> str:
    data = payload.get("data", {})
    price_date = data.get("price_date", "")
    price = data.get("price", "")

    try:
        price_date_pretty = fmt_price_date(price_date)
    except Exception:
        price_date_pretty = price_date

    return (
        f"Updated today for {price_date_pretty} price. "
        f"Price of {price} tallies with NAV report.\n\n"
        f"{CC_LINE}"
    )

def build_startup_message() -> str:
    return (
        "✅ <b>QCDT monitor deployed and running</b>\n"
        "Timezone: SGT\n"
        f"Window: Weekdays {WINDOW_START.strftime('%H:%M')}–{WINDOW_END.strftime('%H:%M')}\n"
        "Checks: Every <b>2 minutes</b> (time-based)\n"
        "Flow: JSON first → poll → ack message only after ✅ Acknowledge\n"
        f"Endpoint: {API_URL}"
    )

# =========================
# Telegram helpers
# =========================
async def send_error(context: ContextTypes.DEFAULT_TYPE, err_text: str) -> None:
    msg = (
        "⚠️ QCDT price monitor error while checking API:\n"
        f"<pre>{err_text}</pre>\n\n"
        f"Endpoint: {API_URL}"
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=ParseMode.HTML)

async def stop_poll_if_needed(context: ContextTypes.DEFAULT_TYPE) -> None:
    poll_msg_id = state.get("pending_poll_msg_id")
    if poll_msg_id is None:
        return
    try:
        await context.bot.stop_poll(chat_id=CHAT_ID, message_id=poll_msg_id)
    except Exception:
        pass

def mark_done_for_today() -> None:
    state["done_date"] = today_str_sgt()

def clear_pending() -> None:
    state["pending_poll"] = False
    state["pending_payload"] = None
    state["pending_poll_id"] = None
    state["pending_poll_msg_id"] = None

def stop_checks_for_day() -> None:
    global check_job
    if check_job is not None:
        try:
            check_job.schedule_removal()
        except Exception:
            pass
        check_job = None

async def send_json_and_poll(context: ContextTypes.DEFAULT_TYPE, payload: dict) -> None:
    """
    Stage 1: Send JSON only, then poll.
    """
    # Store payload while we wait for the poll answer
    state["pending_poll"] = True
    state["pending_payload"] = payload

    # 1) JSON only
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=build_json_only(payload),
        parse_mode=ParseMode.HTML
    )

    # 2) Poll (Telegram requires >=2 options)
    poll_msg = await context.bot.send_poll(
        chat_id=CHAT_ID,
        question="QCDT price update detected. Action?",
        options=["✅ Acknowledge", "🕵️ Investigating / Dispute", "🎌 Public holiday"],
        is_anonymous=False,
    )

    state["pending_poll_id"] = poll_msg.poll.id
    state["pending_poll_msg_id"] = poll_msg.message_id

# =========================
# Core logic
# =========================
async def maybe_check(context: ContextTypes.DEFAULT_TYPE, forced: bool = False) -> None:
    dt = now_sgt()
    today = dt.strftime("%Y-%m-%d")

    # Reset daily stop flag when date rolls
    if state["done_date"] is not None and state["done_date"] != today:
        state["done_date"] = None
        clear_pending()
        state["last_error_alert_at"] = None
        state["last_check_key"] = None

    # If poll already answered today, do nothing
    if state["done_date"] == today:
        return

    # If waiting for a poll answer, do nothing
    if state["pending_poll"]:
        return

    if not forced:
        if not (is_weekday(dt) and in_window(dt) and is_scheduled_minute(dt)):
            return

        # avoid duplicate checks in same minute
        check_key = dt.strftime("%Y-%m-%d %H:%M")
        if state["last_check_key"] == check_key:
            return
        state["last_check_key"] = check_key

    try:
        payload = await fetch_payload()
        data = payload.get("data", {})
        update_time = data.get("update_time")
        if not update_time:
            raise ValueError("Missing data.update_time")

        changed = update_time != state["last_seen_update_time"]
        is_today_update = parse_update_time_sgt(update_time).strftime("%Y-%m-%d") == today

        logging.info("Fetched update_time=%s changed=%s is_today_update=%s forced=%s",
                     update_time, changed, is_today_update, forced)

        state["last_seen_update_time"] = update_time

        # Trigger stage-1 only when BOTH conditions satisfied
        if changed and is_today_update:
            await send_json_and_poll(context, payload)

    except Exception as e:
        logging.exception("Error while fetching/parsing.")
        if forced or (is_weekday(dt) and in_window(dt)):
            if should_send_error_alert(dt):
                await send_error(context, str(e))
                state["last_error_alert_at"] = dt

# =========================
# Poll answer handler
# =========================
async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.poll_answer is None:
        return

    poll_id = update.poll_answer.poll_id
    option_ids = update.poll_answer.option_ids or []

    # Only react to our active poll
    if state.get("pending_poll_id") != poll_id:
        return

    # Close poll
    await stop_poll_if_needed(context)

    choice = option_ids[0] if option_ids else None
    payload = state.get("pending_payload")

    # Stop everything for the day after ANY choice
    mark_done_for_today()
    stop_checks_for_day()
    clear_pending()

    if choice == 0:
        # ✅ Acknowledge -> Stage 2: send ack message (price_date + price)
        if payload:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=build_ack_message(payload),
            )
        else:
            await context.bot.send_message(chat_id=CHAT_ID, text="✅ Acknowledged.")
    elif choice == 1:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="🕵️ Marked as Investigating / Dispute. Monitoring stopped for today.",
        )
    elif choice == 2:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="🎌 Marked as Public holiday. Monitoring stopped for today.",
        )
    else:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="Noted. Monitoring stopped for today.",
        )

# =========================
# Commands
# =========================
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # prints raw payload immediately
    try:
        payload = await fetch_payload()
        await update.message.reply_text(build_json_only(payload), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"⚠️ /status: API fetch failed: {e}")

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # alias of /status
    await status_cmd(update, context)

async def start_monitor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # manual restart monitoring in case you stopped it early
    await start_checks_job(context.application)
    state["done_date"] = None
    clear_pending()
    await update.message.reply_text("✅ Monitoring restarted (within window rules).")

# =========================
# Scheduling
# =========================
async def heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    await maybe_check(context, forced=False)

async def start_checks_job(app) -> None:
    global check_job
    if check_job is not None:
        return
    if app.job_queue is None:
        raise RuntimeError('JobQueue missing. Use: python-telegram-bot[job-queue]==21.6')

    # Run every minute; gated by is_scheduled_minute (every 2 mins)
    check_job = app.job_queue.run_repeating(
        heartbeat,
        interval=60,
        first=5,
        name=CHECK_JOB_NAME,
    )
    logging.info("Check job started.")

async def daily_start(context: ContextTypes.DEFAULT_TYPE) -> None:
    # start monitoring at 15:30 on weekdays
    state["done_date"] = None
    clear_pending()
    state["last_error_alert_at"] = None
    state["last_check_key"] = None
    await start_checks_job(context.application)
    await context.bot.send_message(chat_id=CHAT_ID, text="🟢 Monitoring window started (15:30–20:30 SGT).")

async def daily_stop(context: ContextTypes.DEFAULT_TYPE) -> None:
    # stop checks after the window (no background checks outside window)
    global check_job
    if check_job is not None:
        try:
            check_job.schedule_removal()
        except Exception:
            pass
        check_job = None
    await context.bot.send_message(chat_id=CHAT_ID, text="🔴 Monitoring window ended. Checks stopped until next weekday 15:30 SGT.")

async def post_init(app) -> None:
    # startup ping so you know it's deployed
    await app.bot.send_message(chat_id=CHAT_ID, text=build_startup_message(), parse_mode=ParseMode.HTML)

    # If already inside window at boot, start checks
    dt = now_sgt()
    if is_weekday(dt) and in_window(dt):
        await start_checks_job(app)

def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Handlers
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("start_monitor", start_monitor_cmd))

    app.add_handler(PollAnswerHandler(on_poll_answer))

    if app.job_queue is None:
        raise RuntimeError('JobQueue missing. Use requirements: python-telegram-bot[job-queue]==21.6')

    # Schedule daily start/stop in SGT (Mon-Fri)
    weekdays = (0, 1, 2, 3, 4)

    app.job_queue.run_daily(
        daily_start,
        time=WINDOW_START,
        days=weekdays,
        name="qcdt_daily_start",
    )

    # stop right after window ends
    app.job_queue.run_daily(
        daily_stop,
        time=dtime(20, 31),
        days=weekdays,
        name="qcdt_daily_stop",
    )

    logging.info("QCDT monitor bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

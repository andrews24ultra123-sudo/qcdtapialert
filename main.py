import asyncio
import json
import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

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
HEARTBEAT_SECONDS = 60  # wake up every minute

CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza"

HTTP_TIMEOUT_SECONDS = 15
ERROR_COOLDOWN = timedelta(minutes=60)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# In-memory state
state = {
    "last_seen_update_time": None,
    "alerted_date": None,
    "last_error_alert_at": None,
    "last_check_key": None,  # prevent double-run in same minute
}

# =========================
# Time helpers
# =========================
def now_sgt() -> datetime:
    return datetime.now(TZ)

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
def build_alert_message(payload: dict) -> str:
    data = payload.get("data", {})
    price_date = data.get("price_date", "")
    price = data.get("price", "")

    try:
        price_date_pretty = fmt_price_date(price_date)
    except Exception:
        price_date_pretty = price_date

    return (
        f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>\n\n"
        f"Updated today for {price_date_pretty} price. "
        f"Price of {price} tallies with NAV report.\n\n"
        f"{CC_LINE}"
    )

def build_status_message(payload: dict) -> str:
    dt = now_sgt()
    return (
        f"<b>Status</b>\n"
        f"Time (SGT): {dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Weekday: {is_weekday(dt)}\n"
        f"In window: {in_window(dt)}\n"
        f"Scheduled minute (2-min): {is_scheduled_minute(dt)}\n"
        f"Last seen update_time: {state['last_seen_update_time']}\n"
        f"Alerted date: {state['alerted_date']}\n\n"
        f"<b>Current API payload</b>\n"
        f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>"
    )

# =========================
# Telegram senders
# =========================
async def send_error(context: ContextTypes.DEFAULT_TYPE, err_text: str):
    msg = (
        "⚠️ QCDT price monitor error while checking API:\n"
        f"<pre>{err_text}</pre>\n\n"
        f"Endpoint: {API_URL}"
    )
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=msg,
        parse_mode=ParseMode.HTML
    )

async def send_alert(context: ContextTypes.DEFAULT_TYPE, payload: dict):
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=build_alert_message(payload),
        parse_mode=ParseMode.HTML
    )

    await context.bot.send_poll(
        chat_id=CHAT_ID,
        question="Acknowledge QCDT price update?",
        options=["✅ Acknowledge"],
        is_anonymous=False
    )

async def send_startup_message(app):
    msg = (
        "✅ <b>QCDT monitor deployed and running</b>\n"
        "Timezone: SGT\n"
        f"Window: Weekdays {WINDOW_START.strftime('%H:%M')}–{WINDOW_END.strftime('%H:%M')}\n"
        "Checks: Every <b>2 minutes</b> (time-based)\n"
        f"Endpoint: {API_URL}"
    )
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text=msg,
        parse_mode=ParseMode.HTML
    )

# =========================
# Core logic
# =========================
async def check_logic(context: ContextTypes.DEFAULT_TYPE, forced: bool = False):
    dt = now_sgt()
    today = dt.strftime("%Y-%m-%d")

    # Reset on new day
    if state["alerted_date"] and state["alerted_date"] != today:
        state.update({
            "last_seen_update_time": None,
            "last_error_alert_at": None,
            "alerted_date": None,
            "last_check_key": None,
        })

    if not forced:
        if not (is_weekday(dt) and in_window(dt) and is_scheduled_minute(dt)):
            return
        if state["alerted_date"] == today:
            return

        check_key = dt.strftime("%Y-%m-%d %H:%M")
        if state["last_check_key"] == check_key:
            return
        state["last_check_key"] = check_key

    try:
        payload = await fetch_payload()
        data = payload.get("data", {})
        update_time = data.get("update_time")

        if not update_time:
            raise ValueError("Missing update_time")

        changed = update_time != state["last_seen_update_time"]
        is_today = parse_update_time_sgt(update_time).strftime("%Y-%m-%d") == today

        state["last_seen_update_time"] = update_time

        if changed and is_today and state["alerted_date"] != today:
            await send_alert(context, payload)
            state["alerted_date"] = today

        return payload

    except Exception as e:
        if forced or (is_weekday(dt) and in_window(dt)):
            if should_send_error_alert(dt):
                await send_error(context, str(e))
                state["last_error_alert_at"] = dt

# =========================
# Commands
# =========================
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = await check_logic(context, forced=True)
    if payload:
        await update.message.reply_text(
            build_status_message(payload),
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("⚠️ /status: API fetch failed.")

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = await check_logic(context, forced=True)
    if payload:
        await update.message.reply_text(
            f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("⚠️ /check: API fetch failed.")

# =========================
# Heartbeat (runs every minute)
# =========================
async def heartbeat(context: ContextTypes.DEFAULT_TYPE):
    await check_logic(context, forced=False)

async def post_init(app):
    await send_startup_message(app)

def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("check", check_cmd))

    if app.job_queue is None:
        raise RuntimeError(
            'JobQueue missing. Use: python-telegram-bot[job-queue]==21.6'
        )

    app.job_queue.run_repeating(
        heartbeat,
        interval=HEARTBEAT_SECONDS,
        first=5
    )

    logging.info("QCDT monitor bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

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
# Hardcoded config (as requested)
# =========================
BOT_TOKEN = "8183120153:AAF3k3FZViX33glskyf-CTi2F3LoxulGvV0"
CHAT_ID = -5299275232
API_URL = "https://uat.dmz.finance/stores/tdd/qcdt/new_price"

TZ = ZoneInfo("Asia/Singapore")

# Window: weekdays only, 15:30–20:30 SGT (inclusive)
WINDOW_START = dtime(15, 30)
WINDOW_END = dtime(20, 30)

# Run checks at exact half-hour marks: :00 and :30, but starting from 15:30
ALLOWED_MINUTES = {0, 30}

# Heartbeat runs every minute; it only checks API on allowed minutes
HEARTBEAT_SECONDS = 60

CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza"

HTTP_TIMEOUT_SECONDS = 15
ERROR_COOLDOWN = timedelta(minutes=60)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# In-memory state (no persistence as requested)
state = {
    "last_seen_update_time": None,   # str
    "alerted_date": None,            # YYYY-MM-DD (SGT)
    "last_error_alert_at": None,     # datetime (SGT)
    "last_check_key": None,          # "YYYY-MM-DD HH:MM" to avoid double-running within same minute
}

def now_sgt() -> datetime:
    return datetime.now(TZ)

def is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5  # Mon-Fri

def in_window(dt: datetime) -> bool:
    t = dt.time()
    return (t >= WINDOW_START) and (t <= WINDOW_END)

def is_scheduled_minute(dt: datetime) -> bool:
    # only check at :00 and :30
    return dt.minute in ALLOWED_MINUTES

def parse_update_time_sgt(s: str) -> datetime:
    # "YYYY-MM-DD HH:MM:SS" assumed SGT
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)

def fmt_price_date(price_date_str: str) -> str:
    # "2026-01-21" -> "21 Jan 2026"
    d = datetime.strptime(price_date_str, "%Y-%m-%d").date()
    return d.strftime("%d %b %Y")

def should_send_error_alert(dt: datetime) -> bool:
    last = state["last_error_alert_at"]
    if last is None:
        return True
    return (dt - last) >= ERROR_COOLDOWN

def fetch_payload_sync() -> dict:
    r = requests.get(API_URL, timeout=HTTP_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json()

async def fetch_payload() -> dict:
    return await asyncio.to_thread(fetch_payload_sync)

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
        f"Updated today for {price_date_pretty} price. Price of {price} tallies with NAV report.\n\n"
        f"{CC_LINE}"
    )

def build_status_message(payload: dict) -> str:
    dt = now_sgt()
    today_str = dt.strftime("%Y-%m-%d")
    return (
        f"<b>Status</b>\n"
        f"Time (SGT): {dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Today: {today_str}\n"
        f"In window: {in_window(dt)} | Weekday: {is_weekday(dt)}\n"
        f"Scheduled minute now (:00/:30): {is_scheduled_minute(dt)}\n"
        f"Last seen update_time: {state['last_seen_update_time']}\n"
        f"Alerted date: {state['alerted_date']}\n\n"
        f"<b>Current API payload</b>\n"
        f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>"
    )

async def send_error(context: ContextTypes.DEFAULT_TYPE, err_text: str) -> None:
    msg = (
        "⚠️ QCDT price monitor error while checking API:\n"
        f"<pre>{err_text}</pre>\n\n"
        f"Endpoint: {API_URL}"
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=ParseMode.HTML)

async def send_alert(context: ContextTypes.DEFAULT_TYPE, payload: dict) -> None:
    await context.bot.send_message(chat_id=CHAT_ID, text=build_alert_message(payload), parse_mode=ParseMode.HTML)
    await context.bot.send_poll(
        chat_id=CHAT_ID,
        question="Acknowledge QCDT price update?",
        options=["✅ Acknowledge"],
        is_anonymous=False
    )

async def send_startup_message(app) -> None:
    msg = (
        "✅ <b>QCDT monitor deployed and running</b>\n"
        f"Timezone: SGT\n"
        f"Window: Weekdays {WINDOW_START.strftime('%H:%M')}–{WINDOW_END.strftime('%H:%M')}\n"
        "Checks: Every 30 minutes at :00 and :30 (time-based)\n"
        f"Endpoint: {API_URL}"
    )
    await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=ParseMode.HTML)

async def check_logic(context: ContextTypes.DEFAULT_TYPE, forced: bool = False) -> dict | None:
    dt = now_sgt()
    today_str = dt.strftime("%Y-%m-%d")

    # Reset if date rolled over
    if state["alerted_date"] is not None and state["alerted_date"] != today_str:
        state["last_seen_update_time"] = None
        state["last_error_alert_at"] = None
        state["alerted_date"] = None
        state["last_check_key"] = None

    # Scheduled checks only within window
    if not forced:
        if not (is_weekday(dt) and in_window(dt) and is_scheduled_minute(dt)):
            return None
        if state["alerted_date"] == today_str:
            return None

        # Avoid double-running if heartbeat fires twice in same minute
        check_key = dt.strftime("%Y-%m-%d %H:%M")
        if state["last_check_key"] == check_key:
            return None
        state["last_check_key"] = check_key

    try:
        payload = await fetch_payload()
        data = payload.get("data", {})
        update_time_str = data.get("update_time")
        if not update_time_str:
            raise ValueError(f"Missing data.update_time in response: {payload}")

        changed = (state["last_seen_update_time"] != update_time_str)
        ut = parse_update_time_sgt(update_time_str)
        is_today_update = (ut.strftime("%Y-%m-%d") == today_str)

        logging.info(
            "Fetched update_time=%s changed=%s is_today_update=%s forced=%s",
            update_time_str, changed, is_today_update, forced
        )

        state["last_seen_update_time"] = update_time_str

        if state["alerted_date"] != today_str and changed and is_today_update:
            await send_alert(context, payload)
            state["alerted_date"] = today_str
            logging.info("Alert sent for %s. Stop checking for the day.", today_str)

        return payload

    except Exception as e:
        logging.exception("Error while fetching/parsing.")
        if forced or (is_weekday(dt) and in_window(dt)):
            if should_send_error_alert(dt):
                await send_error(context, str(e))
                state["last_error_alert_at"] = dt
        return None

# Commands
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payload = await check_logic(context, forced=True)
    if payload is None:
        await update.message.reply_text("⚠️ /status: API fetch failed.")
        return
    await update.message.reply_text(build_status_message(payload), parse_mode=ParseMode.HTML)

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payload = await check_logic(context, forced=True)
    if payload is None:
        await update.message.reply_text("⚠️ /check: API fetch failed.")
        return
    await update.message.reply_text(
        f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>",
        parse_mode=ParseMode.HTML
    )

# Heartbeat job (runs every minute; only checks API on exact :00/:30 inside window)
async def heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    await check_logic(context, forced=False)

async def post_init(app):
    # Runs once after initialization
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
        raise RuntimeError('JobQueue missing. Use requirements: python-telegram-bot[job-queue]==21.6')

    # Run every minute; logic gates it to :00/:30 in the right window
    app.job_queue.run_repeating(heartbeat, interval=HEARTBEAT_SECONDS, first=5)

    logging.info("QCDT monitor bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

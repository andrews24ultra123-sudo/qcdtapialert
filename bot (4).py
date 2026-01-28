import json
import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# =========================
# Hardcoded config (as you requested)
# =========================
BOT_TOKEN = "8183120153:AAF3k3FZViX33glskyf-CTi2F3LoxulGvV0"
CHAT_ID = -5299275232

API_URL = "https://uat.dmz.finance/stores/tdd/qcdt/new_price"

TZ = ZoneInfo("Asia/Singapore")
WINDOW_START = dtime(15, 30)  # 3:30pm
WINDOW_END = dtime(20, 30)    # 8:30pm
POLL_INTERVAL_SECONDS = 30 * 60  # 30 minutes

CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza"

HTTP_TIMEOUT_SECONDS = 15

# Error alert cooldown (avoid spam if API is down)
ERROR_COOLDOWN = timedelta(minutes=60)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# In-memory state (no persistence)
state = {
    "last_seen_update_time": None,   # str
    "alerted_date": None,            # YYYY-MM-DD (SGT)
    "last_error_alert_at": None      # datetime (SGT)
}

def now_sgt() -> datetime:
    return datetime.now(TZ)

def is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5  # Mon-Fri

def in_window(dt: datetime) -> bool:
    t = dt.time()
    return (t >= WINDOW_START) and (t <= WINDOW_END)

def parse_update_time(s: str) -> datetime:
    # "2026-01-22 17:29:35" (assume SGT)
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
    # run blocking requests in a worker thread
    return await ContextTypes.DEFAULT_TYPE.application.run_in_executor(None, fetch_payload_sync)

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

async def check_logic(context: ContextTypes.DEFAULT_TYPE, forced: bool = False) -> dict | None:
    """
    Returns payload if fetched successfully, else None.
    If forced=True, it fetches and returns payload but DOES NOT change 'alerted_date' logic unless conditions met.
    """
    dt = now_sgt()
    today_str = dt.strftime("%Y-%m-%d")

    # Reset daily flags if day changed
    if state["alerted_date"] is not None and state["alerted_date"] != today_str:
        state["last_seen_update_time"] = None
        state["last_error_alert_at"] = None
        state["alerted_date"] = None

    # Normal scheduled checks only happen inside the window
    if not forced:
        if not (is_weekday(dt) and in_window(dt)):
            return None
        if state["alerted_date"] == today_str:
            # Already alerted today; stop checks for day
            return None

    try:
        payload = await fetch_payload()
        data = payload.get("data", {})
        update_time_str = data.get("update_time")

        if not update_time_str:
            raise ValueError(f"Missing data.update_time in response: {payload}")

        changed = (state["last_seen_update_time"] != update_time_str)
        ut = parse_update_time(update_time_str)
        is_today_update = (ut.strftime("%Y-%m-%d") == today_str)

        logging.info(
            "Fetched update_time=%s changed=%s is_today_update=%s forced=%s",
            update_time_str, changed, is_today_update, forced
        )

        # Always update last seen
        state["last_seen_update_time"] = update_time_str

        # Only alert when BOTH conditions satisfied (and not already alerted today)
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

# =========================
# Telegram commands
# =========================
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Only respond if invoked in the target group (or by admins if you want later)
    payload = await check_logic(context, forced=True)
    if payload is None:
        await update.message.reply_text(
            "⚠️ /status: Could not fetch payload (API down or invalid response). "
            "If within window, an error alert will also be sent (with cooldown)."
        )
        return

    await update.message.reply_text(build_status_message(payload), parse_mode=ParseMode.HTML)

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Same as /status but shorter response
    payload = await check_logic(context, forced=True)
    if payload is None:
        await update.message.reply_text("⚠️ /check: API fetch failed.")
        return

    await update.message.reply_text(
        f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>",
        parse_mode=ParseMode.HTML
    )

# =========================
# Scheduled job
# =========================
async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await check_logic(context, forced=False)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("check", check_cmd))

    # Repeat job every 30 minutes (the logic itself enforces weekday + time window)
    app.job_queue.run_repeating(
        scheduled_check,
        interval=POLL_INTERVAL_SECONDS,
        first=5  # start shortly after boot
    )

    logging.info("QCDT monitor bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

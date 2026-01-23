import os
import time
import json
import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests
from telegram import Bot
from telegram.constants import ParseMode

# =========================
# Hardcoded config (as you requested)
# =========================
BOT_TOKEN = "8183120153:AAF3k3FZViX33glskyf-CTi2F3LoxulGvV0"
CHAT_ID = -5299275232

API_URL = "https://uat.dmz.finance/stores/tdd/qcdt/new_price"

# Schedule: Weekdays only, 3:30pm–8:30pm SGT, check every 30 min
TZ = ZoneInfo("Asia/Singapore")
WINDOW_START = dtime(15, 30)  # 3:30 PM
WINDOW_END = dtime(20, 30)    # 8:30 PM
POLL_INTERVAL_SECONDS = 30 * 60  # 30 minutes

# Mentions & template
CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza"

# Requests
HTTP_TIMEOUT_SECONDS = 15

# Error alert cooldown (avoid spam if API is down)
ERROR_COOLDOWN = timedelta(minutes=60)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

bot = Bot(token=BOT_TOKEN)

# In-memory state (no persistence as per your requirement)
state = {
    "last_seen_update_time": None,   # str
    "alerted_date": None,            # YYYY-MM-DD (SGT) we already alerted for
    "last_error_alert_at": None      # datetime
}

def now_sgt() -> datetime:
    return datetime.now(TZ)

def is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5  # Mon=0 ... Fri=4

def in_window(dt: datetime) -> bool:
    t = dt.time()
    return (t >= WINDOW_START) and (t <= WINDOW_END)

def parse_update_time(s: str) -> datetime:
    # API format: "2026-01-22 17:29:35"
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)

def fmt_price_date(price_date_str: str) -> str:
    # "2026-01-21" -> "21 Jan 2026"
    d = datetime.strptime(price_date_str, "%Y-%m-%d").date()
    return d.strftime("%d %b %Y")

def send_alert(payload: dict) -> None:
    data = payload.get("data", {})
    update_time = data.get("update_time", "")
    price_date = data.get("price_date", "")
    price = data.get("price", "")

    try:
        price_date_pretty = fmt_price_date(price_date)
    except Exception:
        price_date_pretty = price_date

    # Message exactly in your requested structure
    msg = (
        f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>\n\n"
        f"Updated today for {price_date_pretty} price. Price of {price} tallies with NAV report.\n\n"
        f"{CC_LINE}"
    )

    bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=ParseMode.HTML)

    # Acknowledgement poll
    bot.send_poll(
        chat_id=CHAT_ID,
        question="Acknowledge QCDT price update?",
        options=["✅ Acknowledge"],
        is_anonymous=False
    )

def send_error_alert(err_text: str) -> None:
    msg = (
        "⚠️ QCDT price monitor error while checking API:\n"
        f"<pre>{err_text}</pre>\n\n"
        f"Endpoint: {API_URL}"
    )
    bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=ParseMode.HTML)

def should_send_error_alert(dt: datetime) -> bool:
    last = state["last_error_alert_at"]
    if last is None:
        return True
    return (dt - last) >= ERROR_COOLDOWN

def fetch_payload() -> dict:
    r = requests.get(API_URL, timeout=HTTP_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json()

def main_loop():
    logging.info("QCDT monitor started.")

    while True:
        dt = now_sgt()
        today_str = dt.strftime("%Y-%m-%d")

        # Reset daily alert flag if date changed
        if state["alerted_date"] != today_str and state["alerted_date"] is not None:
            # When crossing midnight SGT, allow alerting again
            state["last_seen_update_time"] = None
            state["last_error_alert_at"] = None

        if is_weekday(dt) and in_window(dt):
            # If already alerted today, do nothing until window ends / next day
            if state["alerted_date"] == today_str:
                logging.info("Already alerted today (%s). Sleeping...", today_str)
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            try:
                payload = fetch_payload()
                data = payload.get("data", {})
                update_time_str = data.get("update_time")

                if not update_time_str:
                    raise ValueError(f"Missing data.update_time in response: {payload}")

                # Condition 1: update_time changed vs last seen
                changed = (state["last_seen_update_time"] != update_time_str)

                # Condition 2: update_time date is today (SGT)
                ut = parse_update_time(update_time_str)
                is_today_update = (ut.strftime("%Y-%m-%d") == today_str)

                logging.info(
                    "Fetched update_time=%s changed=%s is_today_update=%s",
                    update_time_str, changed, is_today_update
                )

                # Always track last seen so we can detect change
                state["last_seen_update_time"] = update_time_str

                # Only alert when BOTH conditions satisfied
                if changed and is_today_update:
                    send_alert(payload)
                    state["alerted_date"] = today_str
                    logging.info("Alert sent for %s. Stop checking for the day.", today_str)

            except Exception as e:
                logging.exception("Error while fetching/parsing.")
                if should_send_error_alert(dt):
                    send_error_alert(str(e))
                    state["last_error_alert_at"] = dt

        else:
            logging.info("Outside schedule window. Sleeping...")

        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    main_loop()

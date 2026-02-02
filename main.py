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

# Tag + CC line (updated)
TAG_LINE = "@mrpotato1234 please cross ref QCDT price to NAV pack email"
CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza @Ascentkaiwei"

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

def fmt_date_pretty(date_str_yyyy_mm_dd: str) -> str:
    # "2026-02-02" -> "2 Feb 2026"
    d = datetime.strptime(date_str_yyyy_mm_dd, "%Y-%m-%d").date()
    return d.strftime("%-d %b %Y") if hasattr(d, "strftime") else d.strftime("%d %b %Y")

def fmt_price_date(price_date_str: str) -> str:
    # "2026-01-30" -> "30 Jan 2026"
    d = datetime.strptime(price_date_str, "%Y-%m-%d").date()
    return d.strftime("%-d %b %Y") if hasattr(d, "strftime") else d.strftime("%d %b %Y")

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
    return f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>"

def build_ack_message(payload: dict) -> str:
    data = payload.get("data", {})
    price_date = data.get("price_date", "")
    price = data.get("price", "")

    today_pretty = fmt_date_pretty(today_str_sgt())
    try:
        price_date_pretty = fmt_price_date(price_date)
    except Exception:
        price_date_pretty = price_date

    return (
        f"Updated today on {today_pretty} for {price_date_pretty} QCDT price. "
        f"Price of {price} tallies with NAV report. "
        f"{CC_LINE}"
    )

def build_startup_message() -> str:
    return (
        "✅ <b>QCDT monitor deployed and running</b>\n"
        "Timezone: SGT\n"
        f"Window: Weekdays {WINDOW_START.strftime('%H:%M')}–{WINDOW_END.strftime('%H:%M')}\n"
        "Checks: Every <b>2 minutes</b> (time-based)\n"
        "Flow: JSON + tag → poll → ack message only after ✅ Acknowledge\n"
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

async def send_json_tag_and_poll(context: ContextTypes.DEFAULT_TYPE, payload: dict) -> None:
    """
    Stage 1: Send JSON only, then tag line, then poll.
    """
    state["pending_poll"] = True
    state["pending_payload"] = payload

    # 1) JSON only
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=build_json_only(payload),
        parse_mode=ParseMode.HTML
    )

    # 2) Tag reminder
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=TAG_LINE
    )

    # 3) Poll
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

    # Reset when date rolls
    if state["done_date"] is not None and state["done_date"] != today:
        state["done_date"] = None
        clear_pending()
        state["last_error_alert_at"] = None
        state["last_check_key"] = None

    # If decided today, stop
    if state["done_date"] == today:
        return

    # If waiting for poll answer, stop checking
    if state["pending_poll"]:
        return

    if not forced:
        if not (is_weekday(dt) and in_window(dt) and is_scheduled_minute(dt)):
            return
        check_key = dt.strftime("%Y-%m-%d %H:%M")
        if state["last_check_key"] == check_key:

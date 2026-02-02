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
# Config
# =========================
BOT_TOKEN = "8183120153:AAF3k3FZViX33glskyf-CTi2F3LoxulGvV0"
CHAT_ID = -5299275232
API_URL = "https://uat.dmz.finance/stores/tdd/qcdt/new_price"

TZ = ZoneInfo("Asia/Singapore")

WINDOW_START = dtime(15, 30)
WINDOW_END = dtime(20, 30)

ALLOWED_MINUTES = set(range(0, 60, 2))  # every 2 minutes
HTTP_TIMEOUT_SECONDS = 15
ERROR_COOLDOWN = timedelta(minutes=60)

TAG_LINE = "@mrpotato1234 please cross ref QCDT price to NAV pack email"
CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza @Ascentkaiwei"

logging.basicConfig(level=logging.INFO)

# =========================
# State
# =========================
state = {
    "last_seen_update_time": None,
    "done_date": None,
    "pending_poll": False,
    "pending_payload": None,
    "pending_poll_id": None,
    "pending_poll_msg_id": None,
    "last_error_alert_at": None,
    "last_check_key": None,
}

check_job = None

# =========================
# Helpers
# =========================
def now_sgt():
    return datetime.now(TZ)

def today_str():
    return now_sgt().strftime("%Y-%m-%d")

def is_weekday(dt):
    return dt.weekday() < 5

def in_window(dt):
    return WINDOW_START <= dt.time() <= WINDOW_END

def is_scheduled_minute(dt):
    return dt.minute in ALLOWED_MINUTES

def parse_update_time(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)

def fmt_price_date(s):
    return datetime.strptime(s, "%Y-%m-%d").strftime("%d %b %Y")

def fmt_today():
    return now_sgt().strftime("%d %b %Y")

def should_send_error(dt):
    last = state["last_error_alert_at"]
    return last is None or (dt - last) >= ERROR_COOLDOWN

# =========================
# API
# =========================
def fetch_payload_sync():
    r = requests.get(API_URL, timeout=HTTP_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json()

async def fetch_payload():
    return await asyncio.to_thread(fetch_payload_sync)

# =========================
# Messages
# =========================
def json_only(payload):
    return f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>"

def ack_message(payload):
    d = payload["data"]
    return (
        f"Updated today on {fmt_today()} for {fmt_price_date(d['price_date'])} QCDT price. "
        f"Price of {d['price']} tallies with NAV report. "
        f"{CC_LINE}"
    )

# =========================
# Telegram helpers
# =========================
async def send_error(ctx, text):
    await ctx.bot.send_message(
        chat_id=CHAT_ID,
        text=f"⚠️ QCDT price monitor error:\n<pre>{text}</pre>",
        parse_mode=ParseMode.HTML,
    )

async def stop_poll(ctx):
    if state["pending_poll_msg_id"]:
        try:
            await ctx.bot.stop_poll(CHAT_ID, state["pending_poll_msg_id"])
        except Exception:
            pass

def stop_for_day():
    global check_job
    state["done_date"] = today_str()
    state["pending_poll"] = False
    state["pending_payload"] = None
    if check_job:
        check_job.schedule_removal()
        check_job = None

# =========================
# Core logic
# =========================
async def maybe_check(ctx):
    dt = now_sgt()

    if state["done_date"] == today_str():
        return

    if state["pending_poll"]:
        return

    if not (is_weekday(dt) and in_window(dt) and is_scheduled_minute(dt)):
        return

    check_key = dt.strftime("%Y-%m-%d %H:%M")
    if state["last_check_key"] == check_key:
        return
    state["last_check_key"] = check_key

    try:
        payload = await fetch_payload()
        ut = payload["data"]["update_time"]

        changed = ut != state["last_seen_update_time"]
        is_today = parse_update_time(ut).strftime("%Y-%m-%d") == today_str()

        state["last_seen_update_time"] = ut

        if changed and is_today:
            state["pending_poll"] = True
            state["pending_payload"] = payload

            await ctx.bot.send_message(CHAT_ID, json_only(payload), parse_mode=ParseMode.HTML)
            await ctx.bot.send_message(CHAT_ID, TAG_LINE)

            poll = await ctx.bot.send_poll(
                chat_id=CHAT_ID,
                question="QCDT price update detected. Action?",
                options=["✅ Acknowledge", "🕵️ Investigating / Dispute", "🎌 Public holiday"],
                is_anonymous=False,
            )

            state["pending_poll_id"] = poll.poll.id
            state["pending_poll_msg_id"] = poll.message_id

    except Exception as e:
        if should_send_error(dt):
            state["last_error_alert_at"] = dt
            await send_error(ctx, str(e))

# =========================
# Poll handler
# =========================
async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.poll_answer.poll_id != state["pending_poll_id"]:
        return

    await stop_poll(ctx)

    choice = update.poll_answer.option_ids[0]
    payload = state["pending_payload"]

    stop_for_day()

    if choice == 0 and payload:
        await ctx.bot.send_message(CHAT_ID, ack_message(payload))
    elif choice == 1:
        await ctx.bot.send_message(CHAT_ID, "🕵️ Marked as Investigating / Dispute.")
    elif choice == 2:
        await ctx.bot.send_message(CHAT_ID, "🎌 Marked as Public holiday.")

# =========================
# Scheduler
# =========================
async def heartbeat(ctx):
    await maybe_check(ctx)

async def post_init(app):
    await app.bot.send_message(
        CHAT_ID,
        "✅ QCDT monitor deployed and running",
    )

    global check_job
    check_job = app.job_queue.run_repeating(heartbeat, interval=60, first=5)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.run_polling()

if __name__ == "__main__":
    main()

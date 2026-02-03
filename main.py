import asyncio
import json
import logging
from datetime import datetime, time as dtime, timedelta, date
from zoneinfo import ZoneInfo

import requests
import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    PollAnswerHandler,
    CommandHandler,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = "8591711650:AAHYMbGwiYxCqZm64tKyWiOgl2moiRUvVWM"
CHAT_ID = -4680966417
API_URL = "https://www.dmz.finance/stores/tdd/qcdt/new_price"

TZ = ZoneInfo("Asia/Singapore")

# Times (SGT)
HOLIDAY_TIME = dtime(16, 0)      # 4:00pm
REMINDER_TIME = dtime(17, 30)    # 5:30pm
NAG_START = dtime(18, 0)         # 6:00pm
NAG_END = dtime(21, 0)           # 9:00pm

CHECK_EVERY_MIN = 2
NAG_EVERY_MIN = 15

TAG_LINE = "@mrpotato1234 please cross ref QCDT price to NAV pack email"
CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza @Ascentkaiwei"

DAILY_REMINDER = "📝 Ascent, please remember to update QCDT price on the portal."

HOLIDAY_API = "https://date.nager.at/api/v3/PublicHolidays"

logging.basicConfig(level=logging.INFO)

# =========================
# STATE (daily)
# =========================
state = {
    "last_seen_update_time": None,
    "update_detected": False,
    "stop_all": False,
    "stop_nags": False,
    "pending_update_payload": None,
    "pending_update_poll_id": None,
    "pending_nag_poll_id": None,
    "last_error_at": None,
}

ERROR_COOLDOWN = timedelta(minutes=60)

# =========================
# HELPERS
# =========================
def now_sgt():
    return datetime.now(TZ)

def today_str():
    return now_sgt().strftime("%Y-%m-%d")

def is_weekday():
    return now_sgt().weekday() < 5

def pretty(d: str):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y").lstrip("0")

def should_error():
    return state["last_error_at"] is None or now_sgt() - state["last_error_at"] > ERROR_COOLDOWN

# =========================
# HOLIDAYS (simple & safe)
# =========================
async def holiday_summary():
    today = now_sgt().date()
    year = today.year
    lines = ["📅 Public Holidays (SG / UAE) This Week"]

    async with httpx.AsyncClient() as client:
        for label, code in [("Singapore", "SG"), ("UAE", "AE")]:
            try:
                r = await client.get(f"{HOLIDAY_API}/{year}/{code}", timeout=20)
                data = r.json() if r.status_code == 200 else []
            except Exception:
                data = []

            found = []
            for h in data:
                try:
                    hd = date.fromisoformat(h["date"])
                except Exception:
                    continue
                if abs((hd - today).days) <= 7:
                    found.append(f"  - {hd:%a %d %b}: {h.get('name','Holiday')}")

            if found:
                lines.append(f"\n• {label}:")
                lines.extend(found)
            else:
                lines.append(f"\n• {label}: None")

    return "\n".join(lines)

# =========================
# API
# =========================
async def fetch_payload():
    return await asyncio.to_thread(
        lambda: requests.get(API_URL, timeout=15).json()
    )

def parse_update_time(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)

# =========================
# TELEGRAM
# =========================
async def send(ctx, text, mode=None):
    await ctx.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=mode)

async def send_error(ctx, e):
    await send(ctx, f"⚠️ Error:\n<pre>{e}</pre>", ParseMode.HTML)

# =========================
# PRICE CHECK
# =========================
async def check_price(ctx):
    if state["stop_all"] or not is_weekday():
        return

    try:
        payload = await fetch_payload()
        ut = payload["data"]["update_time"]
        changed = ut != state["last_seen_update_time"]
        today_update = parse_update_time(ut).strftime("%Y-%m-%d") == today_str()

        state["last_seen_update_time"] = ut

        if changed and today_update:
            state["update_detected"] = True
            state["pending_update_payload"] = payload

            await send(ctx, f"<pre>{json.dumps(payload)}</pre>", ParseMode.HTML)
            await send(ctx, TAG_LINE)

            poll = await ctx.bot.send_poll(
                CHAT_ID,
                "QCDT price update detected. Action?",
                ["✅ Acknowledge", "🕵️ Investigating / Dispute", "🎌 Public holiday"],
                is_anonymous=False,
            )
            state["pending_update_poll_id"] = poll.poll.id

    except Exception as e:
        if should_error():
            state["last_error_at"] = now_sgt()
            await send_error(ctx, e)

# =========================
# NAG POLL
# =========================
async def nag_poll(ctx):
    if state["stop_all"] or state["stop_nags"] or state["update_detected"]:
        return

    poll = await ctx.bot.send_poll(
        CHAT_ID,
        "⚠️ QCDT price not updated yet. Action?",
        ["🕵️ Investigating / Dispute", "🎌 Public holiday"],
        is_anonymous=False,
    )
    state["pending_nag_poll_id"] = poll.poll.id

# =========================
# POLL ANSWERS
# =========================
async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pa = update.poll_answer
    if not pa:
        return

    choice = pa.option_ids[0]

    if pa.poll_id == state["pending_update_poll_id"]:
        state["stop_all"] = True
        payload = state["pending_update_payload"]

        if choice == 0:
            d = payload["data"]
            await send(
                ctx,
                f"Updated today on {pretty(today_str())} for {pretty(d['price_date'])} QCDT price. "
                f"Price of {d['price']} tallies with NAV report. {CC_LINE}",
            )
        elif choice == 1:
            await send(ctx, "🕵️ Marked as Investigating / Dispute.")
        else:
            await send(ctx, "🎌 Marked as Public holiday.")

    elif pa.poll_id == state["pending_nag_poll_id"]:
        if choice == 1:
            state["stop_nags"] = True
            await send(ctx, "🎌 Public holiday noted. Nag reminders stopped.")

# =========================
# DAILY RESET
# =========================
async def daily_reset(ctx):
    for k in state:
        state[k] = False if isinstance(state[k], bool) else None
    state["last_seen_update_time"] = None
    await send(ctx, "🔄 QCDT bot daily reset.")

# =========================
# STARTUP
# =========================
async def post_init(app):
    await app.bot.send_message(
        CHAT_ID,
        f"✅ QCDT bot online at {now_sgt():%a %d %b %H:%M} SGT",
    )

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(CommandHandler("status", lambda u, c: send(c, "Bot alive ✅")))

    jq = app.job_queue

    jq.run_repeating(check_price, interval=CHECK_EVERY_MIN * 60, first=10)
    jq.run_repeating(nag_poll, interval=NAG_EVERY_MIN * 60, first=60)

    jq.run_daily(lambda c: send(c, asyncio.run(holiday_summary())), time=HOLIDAY_TIME)
    jq.run_daily(lambda c: send(c, DAILY_REMINDER), time=REMINDER_TIME)
    jq.run_daily(daily_reset, time=dtime(0, 1))

    app.run_polling()

if __name__ == "__main__":
    main()

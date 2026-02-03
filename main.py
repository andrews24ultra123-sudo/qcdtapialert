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
    CommandHandler,
    ContextTypes,
    PollAnswerHandler,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = "8591711650:AAHYMbGwiYxCqZm64tKyWiOgl2moiRUvVWM"
CHAT_ID = -4680966417

API_URL = "https://www.dmz.finance/stores/tdd/qcdt/new_price"

TZ = ZoneInfo("Asia/Singapore")

# Monitoring window (Weekdays)
MONITOR_START = dtime(15, 30)     # 3:30pm
MONITOR_END = dtime(21, 0)        # 9:00pm
CHECK_EVERY_MINUTES = 2           # API check cadence

# Scheduled messages (Weekdays)
HOLIDAY_SUMMARY_TIME = dtime(16, 0)   # 4:00pm
PORTAL_REMINDER_TIME = dtime(17, 30)  # 5:30pm
NAG_START_TIME = dtime(18, 0)         # 6:00pm
NAG_END_TIME = dtime(21, 0)           # 9:00pm
NAG_EVERY_MINUTES = 15

DAILY_REMINDER = "📝 Ascent, please remember to update QCDT price on the portal."
TAG_LINE = "@mrpotato1234 please cross ref QCDT price to NAV pack email"
CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza @Ascentkaiwei"

HTTP_TIMEOUT_SECONDS = 15
ERROR_COOLDOWN = timedelta(minutes=60)

# Holiday API (SG + UAE)
HOLIDAY_API_BASE = "https://date.nager.at/api/v3/PublicHolidays"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =========================
# STATE (in-memory only)
# =========================
state = {
    # price monitoring
    "last_seen_update_time": None,   # str
    "update_detected_today": False,  # bool (update_time changed to today's date)

    # stop switches for the day
    "stop_all_today": False,         # True after update-action poll selection (any option)
    "stop_nags_today": False,        # True if user selects Public holiday on nag polls

    # pending polls
    "pending_update_poll_id": None,  # poll id for "update detected" action
    "pending_update_payload": None,  # payload dict tied to that poll
    "pending_nag_poll_id": None,     # poll id for nag poll

    # spam control
    "last_error_alert_at": None,     # datetime SGT
}

# holiday cache
_holiday_cache: dict[tuple[int, str], list[dict]] = {}

# =========================
# TIME HELPERS
# =========================
def now_sgt() -> datetime:
    return datetime.now(TZ)

def today_str() -> str:
    return now_sgt().strftime("%Y-%m-%d")

def pretty_date_from_yyyy_mm_dd(s: str) -> str:
    # "2026-02-02" -> "2 Feb 2026"
    d = datetime.strptime(s, "%Y-%m-%d").date()
    return d.strftime("%d %b %Y").lstrip("0")

def pretty_today() -> str:
    return now_sgt().strftime("%d %b %Y").lstrip("0")

def is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5

def in_time_window(dt: datetime, start: dtime, end: dtime) -> bool:
    t = dt.time()
    return start <= t <= end

def should_send_error_alert(dt: datetime) -> bool:
    last = state["last_error_alert_at"]
    return last is None or (dt - last) >= ERROR_COOLDOWN

# =========================
# HOLIDAY HELPERS
# =========================
def week_range_monday_to_sunday(d: date):
    monday = d.fromordinal(d.toordinal() - d.weekday())
    sunday = monday.fromordinal(monday.toordinal() + 6)
    return monday, sunday

def fmt_day(d: date) -> str:
    return d.strftime("%a %d %b %Y")

async def fetch_holidays_for_year(country_code: str, year: int) -> list[dict]:
    key = (year, country_code)
    if key in _holiday_cache:
        return _holiday_cache[key]

    url = f"{HOLIDAY_API_BASE}/{year}/{country_code}"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, timeout=20)
        except Exception:
            _holiday_cache[key] = []
            return []

    if r.status_code != 200:
        _holiday_cache[key] = []
        return []

    ctype = (r.headers.get("content-type") or "").lower()
    if "json" not in ctype:
        _holiday_cache[key] = []
        return []

    try:
        data = r.json()
        if not isinstance(data, list):
            data = []
    except Exception:
        data = []

    _holiday_cache[key] = data
    return data

async def holiday_summary_for_this_week() -> str:
    now = now_sgt()
    monday, sunday = week_range_monday_to_sunday(now.date())
    years_needed = {monday.year, sunday.year}

    countries = [("Singapore", "SG"), ("Dubai (UAE)", "AE")]
    lines = [f"📅 Public Holidays This Week ({fmt_day(monday)} → {fmt_day(sunday)})"]

    for label, code in countries:
        hits = []
        for y in years_needed:
            holidays = await fetch_holidays_for_year(code, y)
            for h in holidays:
                try:
                    hd = date.fromisoformat(h.get("date", ""))
                except Exception:
                    continue
                if monday <= hd <= sunday:
                    name = h.get("name") or h.get("localName") or "Holiday"
                    hits.append((hd, name))

        hits.sort(key=lambda x: x[0])
        if not hits:
            lines.append(f"\n• {label}: None")
        else:
            lines.append(f"\n• {label}:")
            for hd, name in hits:

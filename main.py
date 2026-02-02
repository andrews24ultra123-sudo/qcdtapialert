import asyncio
from datetime import datetime, date
from zoneinfo import ZoneInfo
import httpx
import json

# ================= CONFIG =================

TOKEN = "8591711650:AAHYMbGwiYxCqZm64tKyWiOgl2moiRUvVWM"
CHAT_ID = -4680966417

TZ = ZoneInfo("Asia/Singapore")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

DAILY_REMINDER = "📝 Ascent, please remember to update QCDT price on the portal."

# Who to nag until they click
TARGET_USERNAME = "mrpotato1234"       # without @
TARGET_MENTION = "@mrpotato1234"

# Reminder cadence after check-in is posted
NAG_EVERY_MINUTES = 15
NAG_START_HOUR = 18      # 6:00 PM
NAG_START_MIN = 0
NAG_END_HOUR = 21        # 9:00 PM cutoff (adjust if you want)
NAG_END_MIN = 0

HOLIDAY_API_BASE = "https://date.nager.at/api/v3/PublicHolidays"

# ================= TELEGRAM HELPERS =================

async def tg_post(method: str, payload: dict, timeout: int = 20):
    url = f"{BASE_URL}/{method}"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, timeout=timeout)
            print(f"TG {method}: {r.status_code} {r.text[:300]}")
            return r
        except Exception as e:
            print(f"TG {method} EXCEPTION: {type(e).__name__}: {e}")
            return None

async def tg_get(method: str, params: dict, timeout: int = 20):
    url = f"{BASE_URL}/{method}"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, params=params, timeout=timeout)
            print(f"TG {method}: {r.status_code} {r.text[:200]}")
            return r
        except Exception as e:
            print(f"TG {method} EXCEPTION: {type(e).__name__}: {e}")
            return None

async def send_text(text: str):
    await tg_post("sendMessage", {"chat_id": CHAT_ID, "text": text}, timeout=10)

async def pin_message(message_id: int):
    await tg_post(
        "pinChatMessage",
        {"chat_id": CHAT_ID, "message_id": message_id, "disable_notification": True},
        timeout=10,
    )

async def send_checkin_and_pin():
    """
    Sends a message with inline buttons (trackable per user), then pins it.
    Returns message_id if successful else None.
    """
    payload = {
        "chat_id": CHAT_ID,
        "text": "Has QCDT price been updated on portal?",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Yes", "callback_data": "QCDT_YES"},
                 {"text": "❌ No", "callback_data": "QCDT_NO"}],
                [{"text": "🏖️ NA - SG/UAE public holiday", "callback_data": "QCDT_NA"}],
            ]
        }
    }
    r = await tg_post("sendMessage", payload, timeout=10)
    if not r:
        return None
    try:
        js = r.json()
    except Exception:
        return None
    if r.status_code == 200 and js.get("ok"):
        mid = js["result"]["message_id"]
        await pin_message(mid)
        return mid
    return None

async def answer_callback_query(callback_query_id: str, text: str = ""):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = False
    await tg_post("answerCallbackQuery", payload, timeout=10)

# ================= HOLIDAY HELPERS (SG + UAE only) =================

def week_range_monday_to_sunday(d: date):
    monday = d.fromordinal(d.toordinal() - d.weekday())
    sunday = monday.fromordinal(monday.toordinal() + 6)
    return monday, sunday

def fmt_day(d: date) -> str:
    return d.strftime("%a %d %b %Y")

_holiday_cache = {}

async def fetch_holidays_for_year(country_code: str, year: int) -> list[dict]:
    key = (year, country_code)
    if key in _holiday_cache:
        return _holiday_cache[key]

    url = f"{HOLIDAY_API_BASE}/{year}/{country_code}"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, timeout=20)
        except Exception as e:
            print(f"HOLIDAY GET EXCEPTION {country_code} {year}: {e}")
            _holiday_cache[key] = []
            return []

    if r.status_code != 200:
        print(f"HOLIDAY GET non-200 {country_code} {year}: {r.status_code}")
        _holiday_cache[key] = []
        return []

    ctype = (r.headers.get("content-type") or "").lower()
    if "json" not in ctype:
        print(f"HOLIDAY GET non-json {country_code} {year}: {ctype}")
        _holiday_cache[key] = []
        return []

    try:
        data = r.json()
        if not isinstance(data, list):
            data = []
    except json.JSONDecodeError:
        data = []
    except Exception:
        data = []

    _holiday_cache[key] = data
    return data

async def holiday_summary_for_this_week():
    now = datetime.now(TZ)
    monday, sunday = week_range_monday_to_sunday(now.date())
    years_needed = {monday.year, sunday.year}

    countries = [
        ("Singapore", "SG"),
        ("Dubai (UAE)", "AE"),
    ]

    lines = [f"📅 Public Holidays This Week ({fmt_day(monday)} → {fmt_day(sunday)})"]

    for label, code in countries:
        hits = []
        for y in years_needed:
            for h in await fetch_holidays_for_year(code, y):
                try:
                    hd = date.fromisoformat(h["date"])
                except Exception:
                    continue
                if monday <= hd <= sunday:
                    hits.append((hd, h.get("name") or h.get("localName") or "Holiday"))

        hits.sort(key=lambda x: x[0])
        if not hits:
            lines.append(f"\n• {label}: None")
        else:
            lines.append(f"\n• {label}:")
            for hd, name in hits:
                lines.append(f"  - {hd:%a %d %b}: {name}")

    return "\n".join(lines)

# ================= UPDATE POLLING (for button clicks) =================

_last_update_id = 0

async def poll_updates_and_process(today_key: str, state: dict):
    """
    Uses getUpdates to capture inline-button clicks and mark TARGET_USERNAME as responded.
    state contains:
      - responded_today: bool
      - response_value: str|None
    """
    global _last_update_id

    r = await tg_get("getUpdates", {"offset": _last_update_id + 1, "timeout": 0}, timeout=10)
    if not r:
        return
    try:
        js = r.json()
    except Exception:
        return
    if not js.get("ok"):
        return

    updates = js.get("result", [])
    for upd in updates:
        _last_update_id = max(_last_update_id, upd.get("update_id", _last_update_id))

        cq = upd.get("callback_query")
        if not cq:
            continue

        cq_id = cq.get("id")
        from_user = cq.get("from", {})
        username = (from_user.get("username") or "").lower()
        data = cq.get("data", "")

        # Always acknowledge the button click to stop Telegram "loading..."
        await answer_callback_query(cq_id, "Recorded ✅")

        if username == TARGET_USERNAME.lower() and data in {"QCDT_YES", "QCDT_NO", "QCDT_NA"}:
            state["responded_today"] = True
            state["response_value"] = data
            print(f"INFO: {TARGET_USERNAME} responded today with {data}")

# ================= SCHEDULER LOOP =================

async def scheduler():
    print("BOOT: scheduler() starting")
    fired = set()
    last_date = datetime.now(TZ).date()

    # per-day state
    state = {"responded_today": False, "response_value": None}

    await send_text(f"✅ QCDT bot online at {datetime.now(TZ):%a %d %b %Y %H:%M:%S} (SGT)")

    while True:
        now = datetime.now(TZ)

        # Process button clicks (updates) every loop
        await poll_updates_and_process(today_key=str(now.date()), state=state)

        # Reset daily locks & per-day response state
        if now.date() != last_date:
            fired.clear()
            last_date = now.date()
            state["responded_today"] = False
            state["response_value"] = None
            print("INFO: new day -> reset fired + responded_today")

        wd = now.weekday()  # Mon=0 ... Sun=6
        h, m = now.hour, now.minute

        # Mon–Fri 4:00 PM — holiday summary (SG + UAE)
        if wd < 5 and h == 16 and m == 0 and "HOL_SUMMARY" not in fired:
            fired.add("HOL_SUMMARY")
            await send_text(await holiday_summary_for_this_week())

        # Mon–Fri 5:30 PM — reminder
        if wd < 5 and h == 17 and m == 30 and "DAILY_REMINDER" not in fired:
            fired.add("DAILY_REMINDER")
            await send_text(DAILY_REMINDER)

        # Mon–Fri 5:45 PM — send check-in buttons + pin
        if wd < 5 and h == 17 and m == 45 and "DAILY_CHECKIN" not in fired:
            fired.add("DAILY_CHECKIN")
            state["responded_today"] = False
            state["response_value"] = None
            await send_checkin_and_pin()

        # Mon–Fri: if not responded, tag every 15 mins from 6:00 PM to 9:00 PM
        if wd < 5 and "DAILY_CHECKIN" in fired and not state["responded_today"]:
            start_ok = (h > NAG_START_HOUR) or (h == NAG_START_HOUR and m >= NAG_START_MIN)
            end_ok = (h < NAG_END_HOUR) or (h == NAG_END_HOUR and m <= NAG_END_MIN)

            if start_ok and end_ok and (m % NAG_EVERY_MINUTES == 0):
                key = f"NAG_{h:02d}{m:02d}"
                if key not in fired:
                    fired.add(key)
                    await send_text(f"{TARGET_MENTION} reminder: please respond to the QCDT update check-in above ✅")

        await asyncio.sleep(15)

# ================= ENTRY =================

if __name__ == "__main__":
    asyncio.run(scheduler())

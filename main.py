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
HOLIDAY_API_BASE = "https://date.nager.at/api/v3/PublicHolidays"

# Acknowledgement settings
TARGET_USERNAME = "mrpotato1234"  # without @
TARGET_MENTION = "@mrpotato1234"

ACK_COMMANDS = {
    "/qcdt_done": "DONE",
    "/qcdt_no": "NO",
    "/qcdt_na": "NA",
}

# Nag cadence (every 15 minutes)
NAG_EVERY_MINUTES = 15
NAG_START_HOUR = 18  # 6:00 PM
NAG_START_MIN = 0
NAG_END_HOUR = 21    # 9:00 PM cutoff
NAG_END_MIN = 0

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

async def send_poll_and_pin(question: str, options: list[str]):
    r = await tg_post(
        "sendPoll",
        {
            "chat_id": CHAT_ID,
            "question": question,
            "options": options,
            "is_anonymous": False,
            "allows_multiple_answers": False,
        },
        timeout=20,
    )
    if not r:
        return

    try:
        js = r.json()
    except Exception:
        return

    if r.status_code == 200 and js.get("ok"):
        mid = js["result"]["message_id"]
        await tg_post(
            "pinChatMessage",
            {"chat_id": CHAT_ID, "message_id": mid, "disable_notification": True},
            timeout=10,
        )

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

# ================= ACK TRACKING VIA getUpdates =================

_last_update_id = 0

async def process_ack_updates(state: dict):
    """
    Reads group messages via getUpdates and marks state['ack_today']=True
    if TARGET_USERNAME sends /qcdt_done, /qcdt_no, or /qcdt_na.
    """
    global _last_update_id

    # Only request message updates (avoid other update types)
    params = {
        "offset": _last_update_id + 1,
        "limit": 50,
        "timeout": 0,
        "allowed_updates": json.dumps(["message"]),
    }

    r = await tg_get("getUpdates", params=params, timeout=10)
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

        msg = upd.get("message")
        if not msg:
            continue

        chat = msg.get("chat", {})
        if chat.get("id") != CHAT_ID:
            continue  # ignore other chats

        from_user = msg.get("from", {})
        username = (from_user.get("username") or "").lower()
        text = (msg.get("text") or "").strip()

        if username == TARGET_USERNAME.lower():
            cmd = text.split()[0].lower() if text else ""
            if cmd in ACK_COMMANDS:
                state["ack_today"] = True
                state["ack_value"] = ACK_COMMANDS[cmd]
                print(f"ACK: {TARGET_USERNAME} -> {state['ack_value']}")
                await send_text(f"✅ Acknowledged by {TARGET_MENTION}: {state['ack_value']}")

# ================= SCHEDULER LOOP =================

async def scheduler():
    print("BOOT: scheduler() starting")
    fired = set()
    last_date = datetime.now(TZ).date()

    # per-day state
    state = {"ack_today": False, "ack_value": None}

    await send_text(f"✅ QCDT bot online at {datetime.now(TZ):%a %d %b %Y %H:%M:%S} (SGT)")

    while True:
        now = datetime.now(TZ)

        # Read acknowledgements continuously
        await process_ack_updates(state)

        # Reset daily locks & ack state
        if now.date() != last_date:
            fired.clear()
            last_date = now.date()
            state["ack_today"] = False
            state["ack_value"] = None
            print("INFO: new day -> reset fired + ack")

        wd = now.weekday()  # Mon=0 ... Sun=6
        h, m = now.hour, now.minute

        # Mon–Fri 4:00 PM — holiday summary
        if wd < 5 and h == 16 and m == 0 and "HOL_SUMMARY" not in fired:
            fired.add("HOL_SUMMARY")
            await send_text(await holiday_summary_for_this_week())

        # Mon–Fri 5:30 PM — reminder
        if wd < 5 and h == 17 and m == 30 and "DAILY_REMINDER" not in fired:
            fired.add("DAILY_REMINDER")
            await send_text(DAILY_REMINDER)

        # Mon–Fri 5:45 PM — poll (sent + pinned)
        if wd < 5 and h == 17 and m == 45 and "DAILY_POLL" not in fired:
            fired.add("DAILY_POLL")
            state["ack_today"] = False
            state["ack_value"] = None
            await send_poll_and_pin(
                "Has QCDT price been updated on portal?",
                ["Yes", "No", "NA - SG/UAE public holiday"],
            )
            await send_text(
                f"{TARGET_MENTION} please acknowledge with one of: "
                f"/qcdt_done /qcdt_no /qcdt_na"
            )

        # Mon–Fri: if not acked, tag every 15 mins (6:00–9:00 PM)
        if wd < 5 and "DAILY_POLL" in fired and not state["ack_today"]:
            start_ok = (h > NAG_START_HOUR) or (h == NAG_START_HOUR and m >= NAG_START_MIN)
            end_ok = (h < NAG_END_HOUR) or (h == NAG_END_HOUR and m <= NAG_END_MIN)

            if start_ok and end_ok and (m % NAG_EVERY_MINUTES == 0):
                key = f"NAG_{h:02d}{m:02d}"
                if key not in fired:
                    fired.add(key)
                    await send_text(
                        f"{TARGET_MENTION} reminder: please acknowledge "
                        f"/qcdt_done /qcdt_no /qcdt_na"
                    )

        await asyncio.sleep(15)

# ================= ENTRY =================

if __name__ == "__main__":
    asyncio.run(scheduler())

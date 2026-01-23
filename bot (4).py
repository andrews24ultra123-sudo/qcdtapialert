import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

# === CONFIG ===

TOKEN = "8448114982:AAFjVekkgALSK9M3CKc8K7KjrUSTcsvPvIc"
CHAT_ID = -1001819726736

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
TZ = ZoneInfo("Asia/Singapore")


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1:'st', 2:'nd', 3:'rd'}.get(n % 10, 'th')}"


def _format_date_long(d: datetime) -> str:
    return f"{_ordinal(d.day)} {d.strftime('%B %Y')} ({d.strftime('%a')})"


# ===== Telegram helpers =====

async def send_text(text: str):
    payload = {"chat_id": CHAT_ID, "text": text}
    async with httpx.AsyncClient() as client:
        await client.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10)


async def send_poll(question: str, options: list[str], allows_multiple: bool):
    payload = {
        "chat_id": CHAT_ID,
        "question": question,
        "options": options,
        "is_anonymous": False,
        "allows_multiple_answers": allows_multiple,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BASE_URL}/sendPoll", json=payload, timeout=20)
        if resp.status_code != 200:
            return

        data = resp.json()
        if not data.get("ok"):
            return

        msg = data.get("result", {})
        message_id = msg.get("message_id")
        if message_id:
            await client.post(
                f"{BASE_URL}/pinChatMessage",
                json={
                    "chat_id": CHAT_ID,
                    "message_id": message_id,
                    "disable_notification": True,
                },
                timeout=10,
            )


# ===== Poll logic =====

async def send_cg_poll():
    now = datetime.now(TZ)
    days_ahead = (4 - now.weekday()) % 7  # Next Friday
    target = now + timedelta(days=days_ahead)

    question = f"Cell Group – {_format_date_long(target)}"
    options = ["🍽️ Dinner 7.15pm", "⛪ CG 8.15pm", "❌ Cannot make it"]
    await send_poll(question, options, allows_multiple=False)


async def send_service_poll():
    now = datetime.now(TZ)
    days_ahead = (6 - now.weekday()) % 7  # Next Sunday
    target = now + timedelta(days=days_ahead)

    question = f"Sunday Service – {_format_date_long(target)}"
    options = [
        "⏰ 9am",
        "🕚 11.15am",
        "🙋 Serving",
        "🍽️ Lunch",
        "🧑‍🤝‍🧑 Invited a friend",
    ]
    await send_poll(question, options, allows_multiple=True)


# ===== Reminders =====

CG_REMINDER = "📝 Remember to vote for the CG Poll if you have not done so yet!"
SERVICE_REMINDER = "📝 Remember to vote for the Sunday Service Poll if you have not done so yet!"


# ===== Scheduler loop =====

async def scheduler_loop():
    fired_today = set()
    last_date = datetime.now(TZ).date()

    while True:
        now = datetime.now(TZ)
        today = now.date()
        wd = now.weekday()  # Mon=0 ... Sun=6
        h, m = now.hour, now.minute

        if today != last_date:
            fired_today.clear()
            last_date = today

        # Wed 17:30 – CG reminder
        if wd == 2 and h == 17 and m == 30 and "WED_CG_REM" not in fired_today:
            await send_text(CG_REMINDER)
            fired_today.add("WED_CG_REM")

        # Fri 15:00 – CG reminder
        if wd == 4 and h == 15 and m == 0 and "FRI_CG_REM" not in fired_today:
            await send_text(CG_REMINDER)
            fired_today.add("FRI_CG_REM")

        # Fri 22:15 – Sunday Service poll
        if wd == 4 and h == 22 and m == 15 and "FRI_SVC_POLL" not in fired_today:
            await send_service_poll()
            fired_today.add("FRI_SVC_POLL")

        # Sat 17:30 – Service reminder
        if wd == 5 and h == 17 and m == 30 and "SAT_SVC_REM" not in fired_today:
            await send_text(SERVICE_REMINDER)
            fired_today.add("SAT_SVC_REM")

        # Sun 10:45 – CG poll
        if wd == 6 and h == 10 and m == 45 and "SUN_CG_POLL" not in fired_today:
            await send_cg_poll()
            fired_today.add("SUN_CG_POLL")

        await asyncio.sleep(15)


# ===== Main =====

async def main():
    await send_text(f"✅ Scheduler online at {datetime.now(TZ):%a %d %b %Y %H:%M:%S} (SGT)")
    await scheduler_loop()


if __name__ == "__main__":
    asyncio.run(main())

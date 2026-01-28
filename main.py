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
    level=logging

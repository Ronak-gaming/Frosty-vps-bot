"""Shared config, constants, and the bot instance itself."""
import logging
import shutil
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

# ─── Config ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("vps_bot")

if not shutil.which("docker"):
    raise SystemExit("Docker command not found. Please ensure Docker is installed and on PATH.")

MAIN_ADMIN_ID = 1520903362667876523          # your Discord user ID
VPS_USER_ROLE_ID = None                       # filled in automatically once a role is found/created
DOCKER_IMAGE = "ubuntu:22.04"

CPU_THRESHOLD = 90        # % — pause all VPS if host CPU stays above this
CPU_CHECK_INTERVAL = 60   # seconds between CPU checks

DATA_DIR = Path(__file__).resolve().parent
USER_DATA_FILE = DATA_DIR / "user_data.json"
VPS_DATA_FILE = DATA_DIR / "vps_data.json"
ADMIN_DATA_FILE = DATA_DIR / "admin_data.json"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

BOT_START_TIME = datetime.utcnow()
maintenance_mode = False
cpu_monitor_active = True

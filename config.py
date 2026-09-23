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
DOCKER_IMAGE = "ubuntu:22.04"                 # default/fallback image if no OS picked

# label shown in dropdown -> docker image tag. Debian/Ubuntu only — both apt-based,
# same setup_script works on all of them. Ubuntu 26.04 is a newer tag; if Docker Hub
# doesn't have it yet for some reason, pulling it will just fail with a clear error.
OS_CHOICES = [
    ("Ubuntu 22.04 LTS", "ubuntu:22.04"),
    ("Ubuntu 24.04 LTS", "ubuntu:24.04"),
    ("Ubuntu 26.04 LTS", "ubuntu:26.04"),
    ("Debian 11 (Bullseye)", "debian:11"),
    ("Debian 12 (Bookworm)", "debian:12"),
    ("Debian 13 (Trixie)", "debian:13"),
]

# Real SSH (not just sshx) needs a host port mapped to container:22.
SERVER_PUBLIC_IP = "YOUR_SERVER_IP"   # <-- set this to your VPS's public IP
SSH_PORT_START = 20000

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

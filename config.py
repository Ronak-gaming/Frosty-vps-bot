"""Shared config, constants, and the bot instance itself."""
import logging
import shutil
import urllib.request
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

# ─── Logging & Pre-flight ───────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("vps_bot")

if not shutil.which("docker"):
    raise SystemExit("Docker command not found. Please ensure Docker is installed and on PATH.")


def _detect_public_ip() -> str:
    for url in ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                ip = resp.read().decode().strip()
                if ip and "." in ip:
                    return ip
        except Exception:
            pass
    return "127.0.0.1"


# ─── Core Settings ──────────────────────────────────────────────────────────

MAIN_ADMIN_ID = 1520903362667876523          # your Discord user ID (updated by installer if needed)
VPS_USER_ROLE_ID = None                       # filled in automatically once a role is found/created
DOCKER_IMAGE = "ubuntu:22.04"                 # default/fallback image if no OS picked

# Default name for the primary host node (can be customized at setup or via !node-rename)
LOCAL_NODE_NAME = "Node-1"

OS_CHOICES = [
    ("Ubuntu 22.04 LTS", "ubuntu:22.04"),
    ("Ubuntu 24.04 LTS", "ubuntu:24.04"),
    ("Ubuntu 26.04 LTS", "ubuntu:26.04"),
    ("Debian 11 (Bullseye)", "debian:11"),
    ("Debian 12 (Bookworm)", "debian:12"),
    ("Debian 13 (Trixie)", "debian:13"),
]

SERVER_PUBLIC_IP = _detect_public_ip()
SSH_PORT_START = 20000

CPU_THRESHOLD = 90        # % — pause VPS on a node if its host CPU stays above this
CPU_CHECK_INTERVAL = 60   # seconds between CPU checks

DATA_DIR = Path(__file__).resolve().parent
USER_DATA_FILE = DATA_DIR / "user_data.json"
VPS_DATA_FILE = DATA_DIR / "vps_data.json"
ADMIN_DATA_FILE = DATA_DIR / "admin_data.json"
INVITE_DATA_FILE = DATA_DIR / "invite_data.json"
NODES_DATA_FILE = DATA_DIR / "nodes_data.json"
NODE_SSH_KEY = DATA_DIR / "frosty_node_key"

EXPIRY_CHOICES = [
    ("1 Month", 30),
    ("2 Months", 60),
    ("3 Months", 90),
    ("Custom...", "custom"),
    ("No Expiry", None),
]
RENEW_EXTEND_DAYS = 30
EXPIRY_WARN_DAYS = (5, 4, 3, 2, 1)

INVITE_TIERS = [
    (6,  {"ram": 6,  "cpu": 2,  "storage": 64}),
    (12, {"ram": 12, "cpu": 3,  "storage": 128}),
    (16, {"ram": 16, "cpu": 4,  "storage": 192}),
    (20, {"ram": 24, "cpu": 4,  "storage": 60}),
    (30, {"ram": 32, "cpu": 8,  "storage": 280}),
    (60, {"ram": 64, "cpu": 16, "storage": 300}),
]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

BOT_START_TIME = datetime.utcnow()
maintenance_mode = False
cpu_monitor_active = True

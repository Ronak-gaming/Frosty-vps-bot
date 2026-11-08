"""Central configuration. Everything can be set via environment or a .env file."""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.request
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ[key] = val  # .env is managed by the installer and always wins over stale process env


_load_env_file(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = _env(name, "1" if default else "0").lower()
    return val in ("1", "true", "yes", "on")


BOT_VERSION = "4.0.0"
BOT_BRAND = _env("BOT_BRAND", "Apex Hosting")

# ── Discord ──────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = _env("DISCORD_BOT_TOKEN")
MAIN_ADMIN_ID = _env_int("MAIN_ADMIN_ID", 0)
COMMAND_PREFIX = _env("COMMAND_PREFIX", "!")
# Slash commands need NO privileged intents. Turn this on only if you also want
# "!command" prefix commands AND enabled "Message Content Intent" in the dev portal.
ENABLE_MESSAGE_CONTENT = _env_bool("ENABLE_MESSAGE_CONTENT", False)
SYNC_GUILD_ID = _env_int("SYNC_GUILD_ID", 0)  # optional: instant slash sync in one server

# ── Paths ────────────────────────────────────────────────────────────────
DATA_DIR = Path(_env("DATA_DIR", str(BASE_DIR)))
DB_PATH = DATA_DIR / "frosty.db"
SSH_KEY_PATH = Path(_env("SSH_KEY_PATH", str(DATA_DIR / "frosty_node_key")))
SSH_PUB_KEY_PATH = Path(str(SSH_KEY_PATH) + ".pub")
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_PATH = BASE_DIR / "terminal.html"

# ── Web terminal / pairing API (single HTTP server) ──────────────────────
WEB_BIND = _env("WEB_BIND", "0.0.0.0")
WEB_PORT = _env_int("WEB_PORT", _env_int("PAIR_API_PORT", 8765))
PAIR_API_PORT = WEB_PORT  # backwards compatible name
WEB_BASE_URL = _env("WEB_BASE_URL").rstrip("/")  # e.g. https://term.example.com
WEB_TOKEN_TTL = _env_int("WEB_TOKEN_TTL", 600)  # link must be opened within N seconds
WEB_IDLE_TIMEOUT = _env_int("WEB_IDLE_TIMEOUT", 1800)
WEB_MAX_SESSION = _env_int("WEB_MAX_SESSION", 4 * 3600)
PAIR_SECRET = _env("PAIR_SECRET")  # empty -> auto-generated into DATA_DIR/pair_secret

# ── Cluster ──────────────────────────────────────────────────────────────
LOCAL_NODE_NAME = _env("LOCAL_NODE_NAME", "Node-1")
SERVER_PUBLIC_IP = _env("SERVER_PUBLIC_IP")  # empty -> auto-detect (public, else LAN)

# ── Provisioning ─────────────────────────────────────────────────────────
MAX_VPS_PER_USER = _env_int("MAX_VPS_PER_USER", 3)
PORT_RANGE = (_env_int("PORT_RANGE_START", 20000), _env_int("PORT_RANGE_END", 40000))
AUTO_UPGRADE = _env_bool("AUTO_UPGRADE", True)  # apt upgrade after create
ENABLE_SSHX = _env_bool("ENABLE_SSHX", True)
DEPLOY_TIMEOUT = _env_int("DEPLOY_TIMEOUT", 900)
DAILY_CREDITS = _env_int("DAILY_CREDITS", 25)  # 0 disables /daily

OS_IMAGES = {
    "ubuntu-22.04": {"label": "Ubuntu 22.04 LTS (Jammy)", "image": "ubuntu:22.04"},
    "ubuntu-24.04": {"label": "Ubuntu 24.04 LTS (Noble)", "image": "ubuntu:24.04"},
    "ubuntu-26.04": {"label": "Ubuntu 26.04 LTS (Resolute)", "image": "ubuntu:26.04"},
    "debian-11": {"label": "Debian 11 (Bullseye)", "image": "debian:11"},
    "debian-12": {"label": "Debian 12 (Bookworm)", "image": "debian:12"},
    "debian-13": {"label": "Debian 13 (Trixie)", "image": "debian:13"},
}

VPS_PLANS = {
    "Starter": {"ram": 2, "cpu": 1, "disk": 15, "price": 100},
    "Basic": {"ram": 4, "cpu": 2, "disk": 30, "price": 200},
    "Pro": {"ram": 8, "cpu": 4, "disk": 60, "price": 400},
    "Ultra": {"ram": 16, "cpu": 6, "disk": 120, "price": 800},
}

# ── Network helpers ──────────────────────────────────────────────────────
def is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved


def _lan_ip() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect(("10.255.255.255", 1))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return None


def _public_ip() -> Optional[str]:
    for url in ("https://api.ipify.org", "https://ipv4.icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                ip = r.read().decode().strip()
                ipaddress.ip_address(ip)
                return ip
        except Exception:
            continue
    return None


_resolved_ip: Optional[str] = None


def resolve_server_ip() -> str:
    """Public IP if we have one, otherwise the LAN/private IPv4. Never returns an empty string."""
    global _resolved_ip
    if _resolved_ip:
        return _resolved_ip
    _resolved_ip = SERVER_PUBLIC_IP or _public_ip() or _lan_ip() or "127.0.0.1"
    return _resolved_ip


def web_base_url_source() -> str:
    """Where the console link base comes from: 'admin' (/admin seturl), 'env' (.env) or 'auto'."""
    try:
        import storage
        if storage.get_meta("web_base_url"):
            return "admin"
    except Exception:
        pass
    return "env" if WEB_BASE_URL.startswith("http") else "auto"


def web_base_url() -> str:
    """Priority: /admin seturl (database)  >  WEB_BASE_URL in .env  >  http://<server ip>:<port>."""
    try:
        import storage
        override = storage.get_meta("web_base_url")
        if override:
            return override.rstrip("/")
    except Exception:
        pass
    if WEB_BASE_URL.startswith("http"):
        return WEB_BASE_URL
    return f"http://{resolve_server_ip()}:{WEB_PORT}"

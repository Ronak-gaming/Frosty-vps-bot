import os

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN")
MAIN_ADMIN_ID = 123456789012345678  # Replaced by installer
LOCAL_NODE_NAME = "Node-1"

# Supports both Private IPv4 (10.x / 172.16.x / 192.168.x / 127.0.0.1) and Public IPv4
SERVER_PUBLIC_IP = os.getenv("SERVER_PUBLIC_IP", "127.0.0.1")

# Cloudflare Tunnel / Custom Domain URL for WebSSH (no trailing slash needed, handled automatically)
WEBSSH_BASE_URL = os.getenv("WEBSSH_BASE_URL", "https://freevps.apex1hosting.qzz.io").rstrip("/")

BOT_VERSION = "3.0.0"
BOT_DIR = "/opt/Frosty-vps-bot"
SSH_KEY_PATH = os.path.join(BOT_DIR, "frosty_node_key")
SSH_PUB_KEY_PATH = os.path.join(BOT_DIR, "frosty_node_key.pub")
WEBSSH_HTML_PATH = os.path.join(BOT_DIR, "webssh.html")

PAIR_API_PORT = int(os.getenv("PAIR_API_PORT", "8765"))
WEBSSH_PORT = PAIR_API_PORT

DEFAULT_RAM_GB = 2
DEFAULT_CPU_CORES = 1
DEFAULT_DISK_GB = 15
DOCKER_IMAGE = "ubuntu:22.04"

VPS_PLANS = {
    "Starter": {"ram": 2, "cpu": 1, "disk": 15, "price": 100},
    "Basic":   {"ram": 4, "cpu": 2, "disk": 30, "price": 200},
    "Pro":     {"ram": 8, "cpu": 4, "disk": 60, "price": 400},
    "Ultra":   {"ram": 16, "cpu": 6, "disk": 120, "price": 800}
}

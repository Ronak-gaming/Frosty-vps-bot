import os

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN")
MAIN_ADMIN_ID = 123456789012345678
LOCAL_NODE_NAME = "Node-1"
SERVER_PUBLIC_IP = os.getenv("SERVER_PUBLIC_IP", "127.0.0.1")

BOT_VERSION = "3.2.0"
BOT_DIR = "/opt/Frosty-vps-bot"
SSH_KEY_PATH = os.path.join(BOT_DIR, "frosty_node_key")
SSH_PUB_KEY_PATH = os.path.join(BOT_DIR, "frosty_node_key.pub")

PAIR_API_PORT = int(os.getenv("PAIR_API_PORT", "8765"))

DEFAULT_RAM_GB = 2
DEFAULT_CPU_CORES = 1
DEFAULT_DISK_GB = 15

# Supported Operating Systems mapping to Docker base images
OS_IMAGES = {
    "ubuntu-22.04": {"label": "Ubuntu 22.04 LTS (Jammy)", "image": "ubuntu:22.04"},
    "ubuntu-24.04": {"label": "Ubuntu 24.04 LTS (Noble)", "image": "ubuntu:24.04"},
    "ubuntu-26.04": {"label": "Ubuntu 26.04 LTS (Resolute)", "image": "ubuntu:26.04"},
    "debian-11":    {"label": "Debian 11 (Bullseye)", "image": "debian:11"},
    "debian-12":    {"label": "Debian 12 (Bookworm)", "image": "debian:12"},
    "debian-13":    {"label": "Debian 13 (Trixie / Latest)", "image": "debian:13"}
}

VPS_PLANS = {
    "Starter": {"ram": 2, "cpu": 1, "disk": 15, "price": 100},
    "Basic":   {"ram": 4, "cpu": 2, "disk": 30, "price": 200},
    "Pro":     {"ram": 8, "cpu": 4, "disk": 60, "price": 400},
    "Ultra":   {"ram": 16, "cpu": 6, "disk": 120, "price": 800}
}

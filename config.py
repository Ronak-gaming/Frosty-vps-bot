import os

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN")
MAIN_ADMIN_ID = 123456789012345678  # Replaced by installer
LOCAL_NODE_NAME = "Node-1"
SERVER_PUBLIC_IP = "YOUR_SERVER_IP"

BOT_DIR = "/opt/Frosty-vps-bot"
SSH_KEY_PATH = os.path.join(BOT_DIR, "frosty_node_key")
SSH_PUB_KEY_PATH = os.path.join(BOT_DIR, "frosty_node_key.pub")
PAIR_API_PORT = int(os.getenv("PAIR_API_PORT", "8765"))

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

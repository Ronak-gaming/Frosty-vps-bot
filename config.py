import os

# Auto-populated by installer or environment
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN")
MAIN_ADMIN_ID = 123456789012345678  # Replaced by installer
LOCAL_NODE_NAME = "Node-1"          # Replaced by installer
SERVER_PUBLIC_IP = "YOUR_SERVER_IP" # Replaced by installer

# Directory & Key paths
BOT_DIR = "/opt/Frosty-vps-bot"
SSH_KEY_PATH = os.path.join(BOT_DIR, "frosty_node_key")
SSH_PUB_KEY_PATH = os.path.join(BOT_DIR, "frosty_node_key.pub")

# Auto-Pairing HTTP API Port (used when Worker Nodes pair via Bot Token)
PAIR_API_PORT = int(os.getenv("PAIR_API_PORT", "8765"))

# Default Container Limits
DEFAULT_RAM_GB = 2
DEFAULT_CPU_CORES = 1
DEFAULT_DISK_GB = 15
DOCKER_IMAGE = "ubuntu:22.04"

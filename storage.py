"""Persistence (JSON-backed), node registry, small helpers, admin checks, and embed builders."""
import json
import random
import string
import threading
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

from config import (
    MAIN_ADMIN_ID, DEFAULT_NODE_NAME, SERVER_PUBLIC_IP,
    USER_DATA_FILE, VPS_DATA_FILE, ADMIN_DATA_FILE, INVITE_DATA_FILE, NODES_DATA_FILE, logger,
)

# ─── Persistence ────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_all():
    users = _load_json(USER_DATA_FILE, {})

    raw_vps = _load_json(VPS_DATA_FILE, {})
    vps = {}
    for uid, v in raw_vps.items():
        if isinstance(v, list):
            vps[uid] = v
        elif isinstance(v, dict):
            vps[uid] = [v] if "container_name" in v else list(v.values())
        else:
            logger.warning("Skipping unrecognised VPS data for user %s", uid)

    admins = _load_json(ADMIN_DATA_FILE, {"admins": [str(MAIN_ADMIN_ID)]})
    admins.setdefault("admins", [])
    if str(MAIN_ADMIN_ID) not in admins["admins"]:
        admins["admins"].append(str(MAIN_ADMIN_ID))

    invites = _load_json(INVITE_DATA_FILE, {})

    # nodes_data schema:
    # {
    #   "NodeName": {
    #       "name": "NodeName",
    #       "ip": "1.2.3.4",
    #       "ssh_port": 22,
    #       "user": "root",
    #       "is_local": True/False,
    #       "added_at": "..."
    #   }
    # }
    nodes = _load_json(NODES_DATA_FILE, {})
    if not nodes:
        nodes[DEFAULT_NODE_NAME] = {
            "name": DEFAULT_NODE_NAME,
            "ip": SERVER_PUBLIC_IP,
            "ssh_port": 22,
            "user": "root",
            "is_local": True,
            "added_at": datetime.now().isoformat(),
        }
    else:
        # Keep local node IP synced if it changed or was placeholder
        for n_name, n_info in nodes.items():
            if n_info.get("is_local") and (not n_info.get("ip") or n_info.get("ip") in ("YOUR_SERVER_IP", "127.0.0.1")):
                n_info["ip"] = SERVER_PUBLIC_IP

    # Backfill existing VPS entries that don't have a "node" field yet
    first_node_name = next(iter(nodes.keys()))
    for uid, vlist in vps.items():
        for v in vlist:
            if not v.get("node") or v.get("node") not in nodes:
                v["node"] = first_node_name

    return users, vps, admins, invites, nodes


user_data, vps_data, admin_data, invite_data, nodes_data = load_all()
_save_lock = threading.Lock()


def save_data():
    with _save_lock:
        try:
            with open(USER_DATA_FILE, "w") as f:
                json.dump(user_data, f, indent=4)
            with open(VPS_DATA_FILE, "w") as f:
                json.dump(vps_data, f, indent=4)
            with open(ADMIN_DATA_FILE, "w") as f:
                json.dump(admin_data, f, indent=4)
            with open(INVITE_DATA_FILE, "w") as f:
                json.dump(invite_data, f, indent=4)
            with open(NODES_DATA_FILE, "w") as f:
                json.dump(nodes_data, f, indent=4)
        except Exception:
            logger.exception("Failed to save data")


save_data()


def get_default_node() -> str:
    for name, info in nodes_data.items():
        if info.get("is_local"):
            return name
    return next(iter(nodes_data.keys()), DEFAULT_NODE_NAME)


def get_node_info(node_name: str = None) -> dict:
    if not node_name or node_name not in nodes_data:
        node_name = get_default_node()
    return nodes_data.get(node_name, {
        "name": DEFAULT_NODE_NAME,
        "ip": SERVER_PUBLIC_IP,
        "ssh_port": 22,
        "user": "root",
        "is_local": True,
    })


def find_vps_by_container(container_name: str, node_name: str = None):
    """Return (owner_id, index, vps_dict) matching container_name (and optional node_name)."""
    for uid, vlist in vps_data.items():
        for idx, v in enumerate(vlist):
            if v.get("container_name") == container_name:
                if node_name is None or v.get("node") == node_name:
                    return uid, idx, v
    return None, None, None


def is_admin_id(user_id) -> bool:
    user_id = str(user_id)
    return user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])


# ─── Small helpers ──────────────────────────────────────────────────────────

def generate_password(length=16) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def next_container_id(user_id: str) -> int:
    used = set()
    for v in vps_data.get(user_id, []):
        name = v.get("container_name", "")
        suffix = name.rsplit("-", 1)[-1]
        if suffix.isdigit():
            used.add(int(suffix))
    n = 1
    while n in used:
        n += 1
    return n


# ─── Admin check decorators ─────────────────────────────────────────────────

def is_admin():
    async def predicate(ctx):
        if is_admin_id(ctx.author.id):
            return True
        await ctx.send(embed=error_embed("Access Denied", "You don't have permission to use this command."))
        return False
    return commands.check(predicate)


def is_main_admin():
    async def predicate(ctx):
        if str(ctx.author.id) == str(MAIN_ADMIN_ID):
            return True
        await ctx.send(embed=error_embed("Access Denied", "Only the main admin can use this command."))
        return False
    return commands.check(predicate)


# ─── Embeds ─────────────────────────────────────────────────────────────────

def base_embed(title, description="", color=0x1A1A1A):
    e = discord.Embed(title=f"▌ {title}", description=description, color=color)
    e.set_footer(text=f"ApexHosting | Multi-Node VPS Manager • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return e


def success_embed(title, description=""):
    return base_embed(title, description, 0x00FF88)


def error_embed(title, description=""):
    return base_embed(title, description, 0xFF3366)


def info_embed(title, description=""):
    return base_embed(title, description, 0x00CCFF)


def warning_embed(title, description=""):
    return base_embed(title, description, 0xFFAA00)

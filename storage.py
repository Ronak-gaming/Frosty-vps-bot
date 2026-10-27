"""Persistence (atomic JSON-backed), multi-node registry, helpers, admin checks, and embed builders."""
import json
import os
import random
import string
import threading
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

from config import (
    MAIN_ADMIN_ID, USER_DATA_FILE, VPS_DATA_FILE, ADMIN_DATA_FILE,
    INVITE_DATA_FILE, NODES_DATA_FILE, LOCAL_NODE_NAME, SERVER_PUBLIC_IP, logger,
)

# ─── Persistence ────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _atomic_write_json(path: Path, data):
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp_path, path)


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

    nodes = _load_json(NODES_DATA_FILE, {})
    if not nodes:
        nodes[LOCAL_NODE_NAME] = {
            "name": LOCAL_NODE_NAME,
            "is_local": True,
            "ip": SERVER_PUBLIC_IP,
            "ssh_port": 22,
            "user": "root",
        }
    else:
        has_local = False
        for n_name, n_cfg in nodes.items():
            n_cfg["name"] = n_name
            if n_cfg.get("is_local"):
                has_local = True
                if not n_cfg.get("ip") or n_cfg.get("ip") in ("YOUR_SERVER_IP", "127.0.0.1"):
                    n_cfg["ip"] = SERVER_PUBLIC_IP
        if not has_local:
            nodes[LOCAL_NODE_NAME] = {
                "name": LOCAL_NODE_NAME,
                "is_local": True,
                "ip": SERVER_PUBLIC_IP,
                "ssh_port": 22,
                "user": "root",
            }

    # Backfill missing "node" field on any legacy VPS entries
    default_local = next((k for k, v in nodes.items() if v.get("is_local")), LOCAL_NODE_NAME)
    for uid, vlist in vps.items():
        for item in vlist:
            if not item.get("node"):
                item["node"] = default_local

    return users, vps, admins, invites, nodes


user_data, vps_data, admin_data, invite_data, nodes_data = load_all()
_save_lock = threading.Lock()


def save_data():
    with _save_lock:
        try:
            _atomic_write_json(USER_DATA_FILE, user_data)
            _atomic_write_json(VPS_DATA_FILE, vps_data)
            _atomic_write_json(ADMIN_DATA_FILE, admin_data)
            _atomic_write_json(INVITE_DATA_FILE, invite_data)
            _atomic_write_json(NODES_DATA_FILE, nodes_data)
        except Exception:
            logger.exception("Failed to save data")


# Save initial state so nodes_data.json exists on disk immediately
save_data()


def default_node_name() -> str:
    for name, cfg in nodes_data.items():
        if cfg.get("is_local"):
            return name
    return next(iter(nodes_data.keys()), LOCAL_NODE_NAME)


def get_node(node_name: str = None) -> dict:
    if node_name and node_name in nodes_data:
        return nodes_data[node_name]
    fallback = default_node_name()
    return nodes_data.get(fallback, {
        "name": fallback,
        "is_local": True,
        "ip": SERVER_PUBLIC_IP,
        "ssh_port": 22,
        "user": "root",
    })


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

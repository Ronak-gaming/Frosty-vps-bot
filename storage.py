"""Persistence (JSON-backed), small helpers, admin checks, and embed builders."""
import json
import random
import string
import threading
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

from config import MAIN_ADMIN_ID, USER_DATA_FILE, VPS_DATA_FILE, ADMIN_DATA_FILE

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

    return users, vps, admins


user_data, vps_data, admin_data = load_all()
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
        except Exception:
            logger.exception("Failed to save data")


def is_admin_id(user_id) -> bool:
    user_id = str(user_id)
    return user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])


# ─── Small helpers ──────────────────────────────────────────────────────────

def generate_password(length=16) -> str:
    # Deliberately avoid shell-special characters beyond what's safe inside
    # a single-quoted bash string (still fine, but keeps things boring).
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def next_container_id(user_id: str) -> int:
    """
    Smallest positive integer not currently used as a container-name suffix
    for this user. Using this (instead of len(list)+1) means deleting a VPS
    can never cause a new one to collide with a surviving container's name.
    """
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
    e.set_footer(text=f"PrimeCloud | VPS Manager • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return e


def success_embed(title, description=""):
    return base_embed(title, description, 0x00FF88)


def error_embed(title, description=""):
    return base_embed(title, description, 0xFF3366)


def info_embed(title, description=""):
    return base_embed(title, description, 0x00CCFF)


def warning_embed(title, description=""):
    return base_embed(title, description, 0xFFAA00)



"""SQLite persistence (atomic credit operations, no race conditions, no corrupted JSON)."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from config import DATA_DIR, DB_PATH, LOCAL_NODE_NAME, PAIR_SECRET, resolve_server_ip

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, credits INTEGER NOT NULL DEFAULT 0, last_daily REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admins (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS nodes (
    name TEXT PRIMARY KEY, ip TEXT NOT NULL, ssh_port INTEGER NOT NULL DEFAULT 22,
    user TEXT NOT NULL DEFAULT 'root', is_local INTEGER NOT NULL DEFAULT 0, added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vps (
    container_name TEXT PRIMARY KEY, owner_id TEXT NOT NULL, node TEXT NOT NULL, ip TEXT NOT NULL,
    ssh_port INTEGER NOT NULL, root_password TEXT NOT NULL, ram_gb INTEGER NOT NULL, cores INTEGER NOT NULL,
    disk_gb INTEGER NOT NULL, os_key TEXT, os_label TEXT, fetch_output TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vps_owner ON vps(owner_id);
CREATE TABLE IF NOT EXISTS tx (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, delta INTEGER NOT NULL,
    reason TEXT NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        try:
            os.chmod(DB_PATH, 0o600)  # contains root passwords
        except OSError:
            pass
        _import_legacy_json()
    return _conn


@contextmanager
def _tx():
    with _lock:
        c = db()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def _q(sql: str, args: tuple = ()) -> List[sqlite3.Row]:
    with _lock:
        return db().execute(sql, args).fetchall()


def _x(sql: str, args: tuple = ()) -> None:
    with _lock:
        db().execute(sql, args)


# ── legacy JSON import (v3.x -> v4) ──────────────────────────────────────
def _read_json(name: str, default: Any) -> Any:
    path = DATA_DIR / name
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _import_legacy_json() -> None:
    c = _conn
    assert c is not None
    if c.execute("SELECT 1 FROM meta WHERE key='legacy_imported'").fetchone():
        return
    now = time.time()
    for name, cfg in (_read_json("nodes_data.json", {}) or {}).items():
        if isinstance(cfg, dict):
            c.execute(
                "INSERT OR IGNORE INTO nodes(name,ip,ssh_port,user,is_local,added_at) VALUES(?,?,?,?,?,?)",
                (name, cfg.get("ip", "127.0.0.1"), int(cfg.get("ssh_port", 22)), cfg.get("user", "root"),
                 1 if cfg.get("is_local") else 0, now),
            )
    for uid, ud in (_read_json("user_data.json", {}) or {}).items():
        if isinstance(ud, dict):
            c.execute("INSERT OR IGNORE INTO users(id,credits) VALUES(?,?)", (str(uid), int(ud.get("credits", 0))))
    adm = _read_json("admin_data.json", {"admins": []})
    for a in (adm if isinstance(adm, list) else adm.get("admins", [])):
        c.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)", (str(a),))
    for uid, vlist in (_read_json("vps_data.json", {}) or {}).items():
        for v in vlist if isinstance(vlist, list) else []:
            try:
                c.execute(
                    "INSERT OR IGNORE INTO vps(container_name,owner_id,node,ip,ssh_port,root_password,ram_gb,cores,"
                    "disk_gb,os_key,os_label,fetch_output,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (v["container_name"], str(uid), v.get("node", LOCAL_NODE_NAME), v.get("ip", "127.0.0.1"),
                     int(v.get("ssh_port", 22)), v.get("root_password", ""), int(v.get("ram_gb", 2)),
                     int(v.get("cores", 1)), int(v.get("disk_gb", 15)), v.get("os_key"),
                     v.get("os_label", "Linux"), v.get("fetch_output", ""), now),
                )
            except Exception:
                continue
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('legacy_imported','1')")


# ── meta / secrets ───────────────────────────────────────────────────────
def get_meta(key: str) -> str:
    rows = _q("SELECT value FROM meta WHERE key=?", (key,))
    return rows[0]["value"] if rows else ""


def set_meta(key: str, value: str) -> None:
    if value:
        _x("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
    else:
        _x("DELETE FROM meta WHERE key=?", (key,))


def get_pair_secret() -> str:
    if PAIR_SECRET:
        return PAIR_SECRET
    rows = _q("SELECT value FROM meta WHERE key='pair_secret'")
    if rows:
        return rows[0]["value"]
    secret = secrets.token_urlsafe(24)
    _x("INSERT INTO meta(key,value) VALUES('pair_secret',?)", (secret,))
    return secret


# ── nodes ────────────────────────────────────────────────────────────────
def get_nodes() -> Dict[str, Dict[str, Any]]:
    rows = _q("SELECT * FROM nodes ORDER BY is_local DESC, name")
    if not any(r["is_local"] for r in rows):
        _x("INSERT OR REPLACE INTO nodes(name,ip,ssh_port,user,is_local,added_at) VALUES(?,?,?,?,1,?)",
           (LOCAL_NODE_NAME, resolve_server_ip(), 22, "root", time.time()))
        rows = _q("SELECT * FROM nodes ORDER BY is_local DESC, name")
    return {r["name"]: {**dict(r), "is_local": bool(r["is_local"])} for r in rows}


def get_node(name: str) -> Optional[Dict[str, Any]]:
    nodes = get_nodes()
    if name in nodes:
        return nodes[name]
    for k, v in nodes.items():
        if k.lower() == (name or "").lower():
            return v
    return None


def local_node() -> Dict[str, Any]:
    return next(n for n in get_nodes().values() if n["is_local"])


def register_node(name: str, ip: str, ssh_port: int = 22, user: str = "root") -> None:
    _x("INSERT OR REPLACE INTO nodes(name,ip,ssh_port,user,is_local,added_at) VALUES(?,?,?,?,0,?)",
       (name, ip, int(ssh_port), user, time.time()))


def set_node_ip(name: str, ip: str) -> bool:
    node = get_node(name)
    if not node:
        return False
    _x("UPDATE nodes SET ip=? WHERE name=?", (ip, node["name"]))
    _x("UPDATE vps SET ip=? WHERE node=?", (ip, node["name"]))
    return True


def remove_node(name: str) -> str:
    """Returns '' on success, otherwise an error string."""
    node = get_node(name)
    if not node:
        return "Node not found."
    if node["is_local"]:
        return "The primary local node cannot be removed."
    if _q("SELECT 1 FROM vps WHERE node=? LIMIT 1", (node["name"],)):
        return "Node still hosts VPS containers. Delete them first."
    _x("DELETE FROM nodes WHERE name=?", (node["name"],))
    return ""


def rename_node(old: str, new: str) -> str:
    node = get_node(old)
    if not node:
        return "Node not found."
    if get_node(new):
        return "A node with that name already exists."
    with _tx() as c:
        c.execute("UPDATE nodes SET name=? WHERE name=?", (new, node["name"]))
        c.execute("UPDATE vps SET node=? WHERE node=?", (new, node["name"]))
    return ""


# ── vps ──────────────────────────────────────────────────────────────────
def add_vps(owner_id: int, info: Dict[str, Any]) -> None:
    _x(
        "INSERT INTO vps(container_name,owner_id,node,ip,ssh_port,root_password,ram_gb,cores,disk_gb,os_key,"
        "os_label,fetch_output,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (info["container_name"], str(owner_id), info["node"], info["ip"], info["ssh_port"], info["root_password"],
         info["ram_gb"], info["cores"], info["disk_gb"], info.get("os_key"), info.get("os_label"),
         info.get("fetch_output", ""), time.time()),
    )


def get_vps(container_name: str) -> Optional[Dict[str, Any]]:
    rows = _q("SELECT * FROM vps WHERE container_name=?", (container_name,))
    return dict(rows[0]) if rows else None


def get_user_vps(owner_id: int) -> List[Dict[str, Any]]:
    return [dict(r) for r in _q("SELECT * FROM vps WHERE owner_id=? ORDER BY created_at", (str(owner_id),))]


def get_all_vps() -> List[Dict[str, Any]]:
    return [dict(r) for r in _q("SELECT * FROM vps ORDER BY created_at")]


def update_vps(container_name: str, **fields: Any) -> None:
    allowed = {"root_password", "ip", "ssh_port", "node", "fetch_output"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if sets:
        _x(f"UPDATE vps SET {', '.join(k + '=?' for k in sets)} WHERE container_name=?",
           (*sets.values(), container_name))


def delete_vps(container_name: str) -> None:
    _x("DELETE FROM vps WHERE container_name=?", (container_name,))


def used_ports(node: str) -> set:
    return {r["ssh_port"] for r in _q("SELECT ssh_port FROM vps WHERE node=?", (node,))}


# ── admins ───────────────────────────────────────────────────────────────
def get_admins() -> List[int]:
    return [int(r["id"]) for r in _q("SELECT id FROM admins")]


def add_admin(user_id: int) -> None:
    _x("INSERT OR IGNORE INTO admins(id) VALUES(?)", (str(user_id),))


def remove_admin(user_id: int) -> None:
    _x("DELETE FROM admins WHERE id=?", (str(user_id),))


# ── credits (atomic) ─────────────────────────────────────────────────────
def get_credits(user_id: int) -> int:
    rows = _q("SELECT credits FROM users WHERE id=?", (str(user_id),))
    return int(rows[0]["credits"]) if rows else 0


def change_credits(user_id: int, delta: int, reason: str, allow_negative: bool = False) -> Optional[int]:
    """Atomically add/remove credits. Returns the new balance, or None if it would go below zero."""
    uid = str(user_id)
    with _tx() as c:
        c.execute("INSERT OR IGNORE INTO users(id,credits) VALUES(?,0)", (uid,))
        bal = c.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()["credits"]
        new = bal + int(delta)
        if new < 0 and not allow_negative:
            return None
        c.execute("UPDATE users SET credits=? WHERE id=?", (new, uid))
        c.execute("INSERT INTO tx(user_id,delta,reason,ts) VALUES(?,?,?,?)", (uid, int(delta), reason, time.time()))
        return new


def set_credits(user_id: int, amount: int, reason: str) -> int:
    uid = str(user_id)
    with _tx() as c:
        c.execute("INSERT OR IGNORE INTO users(id,credits) VALUES(?,0)", (uid,))
        old = c.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()["credits"]
        c.execute("UPDATE users SET credits=? WHERE id=?", (max(0, amount), uid))
        c.execute("INSERT INTO tx(user_id,delta,reason,ts) VALUES(?,?,?,?)", (uid, max(0, amount) - old, reason, time.time()))
    return max(0, amount)


def transfer_credits(from_id: int, to_id: int, amount: int) -> bool:
    with _tx() as c:
        c.execute("INSERT OR IGNORE INTO users(id,credits) VALUES(?,0)", (str(from_id),))
        c.execute("INSERT OR IGNORE INTO users(id,credits) VALUES(?,0)", (str(to_id),))
        bal = c.execute("SELECT credits FROM users WHERE id=?", (str(from_id),)).fetchone()["credits"]
        if bal < amount:
            return False
        now = time.time()
        c.execute("UPDATE users SET credits=credits-? WHERE id=?", (amount, str(from_id)))
        c.execute("UPDATE users SET credits=credits+? WHERE id=?", (amount, str(to_id)))
        c.execute("INSERT INTO tx(user_id,delta,reason,ts) VALUES(?,?,?,?)", (str(from_id), -amount, f"pay:{to_id}", now))
        c.execute("INSERT INTO tx(user_id,delta,reason,ts) VALUES(?,?,?,?)", (str(to_id), amount, f"pay-from:{from_id}", now))
    return True


def claim_daily(user_id: int, amount: int, cooldown: int = 86400) -> Optional[float]:
    """Returns None if credited, otherwise seconds remaining until the next claim."""
    uid = str(user_id)
    now = time.time()
    with _tx() as c:
        c.execute("INSERT OR IGNORE INTO users(id,credits) VALUES(?,0)", (uid,))
        last = c.execute("SELECT last_daily FROM users WHERE id=?", (uid,)).fetchone()["last_daily"]
        if now - last < cooldown:
            return cooldown - (now - last)
        c.execute("UPDATE users SET credits=credits+?, last_daily=? WHERE id=?", (amount, now, uid))
        c.execute("INSERT INTO tx(user_id,delta,reason,ts) VALUES(?,?,?,?)", (uid, amount, "daily", now))
    return None


def leaderboard(limit: int = 10) -> List[sqlite3.Row]:
    return _q("SELECT id, credits FROM users ORDER BY credits DESC LIMIT ?", (limit,))

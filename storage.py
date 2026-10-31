import json
import os
from typing import Dict, Any, List
from config import BOT_DIR, LOCAL_NODE_NAME, SERVER_PUBLIC_IP

USER_FILE = os.path.join(BOT_DIR, "user_data.json")
VPS_FILE = os.path.join(BOT_DIR, "vps_data.json")
ADMIN_FILE = os.path.join(BOT_DIR, "admin_data.json")
INVITE_FILE = os.path.join(BOT_DIR, "invite_data.json")
NODES_FILE = os.path.join(BOT_DIR, "nodes_data.json")


def _load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


# --- Nodes ---
def get_nodes() -> Dict[str, Dict[str, Any]]:
    nodes = _load_json(NODES_FILE, {})
    has_local = any(cfg.get("is_local") for cfg in nodes.values() if isinstance(cfg, dict))
    if not nodes or not has_local:
        default_name = LOCAL_NODE_NAME or "Node-1"
        nodes[default_name] = {
            "name": default_name,
            "is_local": True,
            "ip": SERVER_PUBLIC_IP,
            "ssh_port": 22,
            "user": "root"
        }
        save_nodes(nodes)
    return nodes


def save_nodes(nodes: Dict[str, Dict[str, Any]]) -> None:
    _save_json(NODES_FILE, nodes)


def register_node(name: str, ip: str, ssh_port: int = 22, is_local: bool = False, user: str = "root") -> Dict[str, Any]:
    nodes = get_nodes()
    if is_local:
        for k in list(nodes.keys()):
            if nodes[k].get("is_local"):
                del nodes[k]
    nodes[name] = {
        "name": name,
        "is_local": is_local,
        "ip": ip,
        "ssh_port": int(ssh_port),
        "user": user
    }
    save_nodes(nodes)
    return nodes[name]


def remove_node(name: str) -> bool:
    nodes = get_nodes()
    if name not in nodes or nodes[name].get("is_local"):
        return False
    del nodes[name]
    save_nodes(nodes)
    return True


def rename_node(old_name: str, new_name: str) -> bool:
    nodes = get_nodes()
    if old_name not in nodes:
        return False
    cfg = nodes.pop(old_name)
    cfg["name"] = new_name
    nodes[new_name] = cfg
    save_nodes(nodes)

    vps_all = get_all_vps()
    for uid, vlist in vps_all.items():
        if isinstance(vlist, list):
            for v in vlist:
                if v.get("node") == old_name:
                    v["node"] = new_name
    save_all_vps(vps_all)
    return True


# --- VPS Containers ---
def get_all_vps() -> Dict[str, List[Dict[str, Any]]]:
    return _load_json(VPS_FILE, {})


def save_all_vps(data: Dict[str, List[Dict[str, Any]]]) -> None:
    _save_json(VPS_FILE, data)


def get_user_vps(user_id: int) -> List[Dict[str, Any]]:
    data = get_all_vps()
    return data.get(str(user_id), [])


def add_user_vps(user_id: int, vps_info: Dict[str, Any]) -> None:
    data = get_all_vps()
    uid = str(user_id)
    if uid not in data:
        data[uid] = []
    data[uid].append(vps_info)
    save_all_vps(data)


def update_user_vps(user_id: int, container_name: str, updates: Dict[str, Any]) -> bool:
    data = get_all_vps()
    uid = str(user_id)
    if uid not in data:
        return False
    for v in data[uid]:
        if v.get("container_name") == container_name:
            v.update(updates)
            save_all_vps(data)
            return True
    return False


def delete_user_vps(user_id: int, container_name: str) -> bool:
    data = get_all_vps()
    uid = str(user_id)
    if uid not in data:
        return False
    before = len(data[uid])
    data[uid] = [v for v in data[uid] if v.get("container_name") != container_name]
    save_all_vps(data)
    return len(data[uid]) < before


# --- Admins ---
def get_admins() -> List[int]:
    data = _load_json(ADMIN_FILE, {"admins": []})
    if isinstance(data, list):
        return data
    return data.get("admins", [])


def add_admin(user_id: int) -> None:
    admins = get_admins()
    if user_id not in admins:
        admins.append(user_id)
        _save_json(ADMIN_FILE, {"admins": admins})


def remove_admin(user_id: int) -> None:
    admins = get_admins()
    if user_id in admins:
        admins.remove(user_id)
        _save_json(ADMIN_FILE, {"admins": admins})


# --- Credits & Users ---
def get_user_data(user_id: int) -> Dict[str, Any]:
    users = _load_json(USER_FILE, {})
    return users.get(str(user_id), {"credits": 0, "invites": 0})


def get_credits(user_id: int) -> int:
    return int(get_user_data(user_id).get("credits", 0))


def add_credits(user_id: int, amount: int) -> int:
    users = _load_json(USER_FILE, {})
    uid = str(user_id)
    if uid not in users:
        users[uid] = {"credits": 0, "invites": 0}
    users[uid]["credits"] = max(0, int(users[uid].get("credits", 0)) + int(amount))
    _save_json(USER_FILE, users)
    return users[uid]["credits"]


def set_user_credits(user_id: int, credits: int) -> None:
    users = _load_json(USER_FILE, {})
    uid = str(user_id)
    if uid not in users:
        users[uid] = {"credits": 0, "invites": 0}
    users[uid]["credits"] = max(0, int(credits))
    _save_json(USER_FILE, users)

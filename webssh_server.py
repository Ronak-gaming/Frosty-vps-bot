import asyncio
import fcntl
import json
import os
import pty
import random
import secrets
import signal
import struct
import termios
import time
from urllib.parse import parse_qs, urlparse

from config import (
    BOT_DIR,
    BOT_VERSION,
    DISCORD_BOT_TOKEN,
    SERVER_PUBLIC_IP,
    WEBSSH_BASE_URL,
    WEBSSH_HTML_PATH,
    WEBSSH_PORT
)
from docker_utils import ensure_ssh_key
from storage import get_nodes, register_node

WEBSSH_TOKENS = {}
SSH_SESSIONS = {}


def get_public_webssh_base() -> str:
    if WEBSSH_BASE_URL and WEBSSH_BASE_URL.startswith("http"):
        return WEBSSH_BASE_URL.rstrip("/")
    return f"http://{SERVER_PUBLIC_IP}:{WEBSSH_PORT}"


def create_webssh_link(bot_name: str, vps_info: dict, require_pin: bool = True) -> dict:
    token = secrets.token_urlsafe(20)
    pin = f"{random.randint(100000, 999999)}" if require_pin else ""
    unlock_key = secrets.token_hex(16)

    # Determine internal/private connection IP so NAT / Private IPv4 nodes always connect
    node_name = vps_info.get("node", "Node-1")
    nodes = get_nodes()
    node_cfg = nodes.get(node_name, {})
    if node_cfg.get("is_local", True):
        connect_host = "127.0.0.1"
    else:
        connect_host = node_cfg.get("ip") or vps_info.get("ip", "127.0.0.1")

    WEBSSH_TOKENS[token] = {
        "bot_name": bot_name,
        "host": connect_host,
        "display_host": vps_info.get("ip", connect_host),
        "port": int(vps_info.get("ssh_port", 22)),
        "username": "root",
        "password": vps_info.get("root_password", ""),
        "label": vps_info.get("container_name", "VPS"),
        "pin": pin,
        "unlock_key": unlock_key,
        "require_pin": require_pin,
        "attempts": 5,
        "created_at": time.time()
    }

    base_url = get_public_webssh_base()
    url = f"{base_url}/ssh/{token}"
    return {"url": url, "pin": pin, "token": token, "base_url": base_url}


def _start_pty_ssh(host: str, port: int, username: str, password: str) -> str:
    session_id = secrets.token_hex(16)
    master_fd, slave_fd = pty.openpty()

    winsize = struct.pack("HHHH", 30, 100, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    pid = os.fork()
    if pid == 0:
        os.setsid()
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        if master_fd > 2:
            os.close(master_fd)

        os.environ["TERM"] = "xterm-256color"
        os.environ["SSHPASS"] = password
        cmd = [
            "sshpass", "-e", "ssh",
            "-p", str(int(port)),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "ConnectTimeout=10",
            f"{username}@{host}"
        ]
        os.execvp("sshpass", cmd)
        os._exit(1)

    os.close(slave_fd)
    fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    session = {
        "master_fd": master_fd,
        "pid": pid,
        "buffer": "",
        "closed": False,
        "created_at": time.time()
    }
    SSH_SESSIONS[session_id] = session

    loop = asyncio.get_running_loop()

    def _on_readable():
        try:
            data = os.read(master_fd, 4096)
            if not data:
                session["closed"] = True
                loop.remove_reader(master_fd)
                return
            session["buffer"] += data.decode("utf-8", errors="replace")
            if len(session["buffer"]) > 500000:
                session["buffer"] = session["buffer"][-250000:]
        except OSError:
            session["closed"] = True
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass

    loop.add_reader(master_fd, _on_readable)
    return session_id


def _close_pty_session(session_id: str):
    sess = SSH_SESSIONS.pop(session_id, None)
    if not sess:
        return
    sess["closed"] = True
    try:
        asyncio.get_running_loop().remove_reader(sess["master_fd"])
    except Exception:
        pass
    try:
        os.close(sess["master_fd"])
    except Exception:
        pass
    try:
        os.kill(sess["pid"], signal.SIGTERM)
        os.waitpid(sess["pid"], os.WNOHANG)
    except Exception:
        pass


def _render_webssh_html(token: str, bot_name: str) -> str:
    if not os.path.exists(WEBSSH_HTML_PATH):
        return "<h3>Error: /opt/Frosty-vps-bot/webssh.html not found!</h3>"

    with open(WEBSSH_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    t_info = WEBSSH_TOKENS.get(token)
    if t_info:
        bot_name = t_info.get("bot_name", bot_name)
        autoconnect_obj = {
            "host": t_info["host"],
            "port": t_info["port"],
            "username": t_info["username"],
            "password": t_info["password"],
            "label": t_info["label"]
        }
        autoconnect_json = json.dumps(autoconnect_obj)
        unlock_key = "" if t_info.get("require_pin") else t_info.get("unlock_key", "")
    else:
        token = "none"
        autoconnect_json = "null"
        unlock_key = ""

    html = html.replace("__BOT_NAME__", bot_name)
    html = html.replace("__BOT_VERSION__", BOT_VERSION)
    html = html.replace("__TOKEN__", token)
    html = html.replace("__UNLOCK_KEY__", unlock_key)
    html = html.replace("__AUTOCONNECT__", autoconnect_json)
    return html


def _send_http(writer: asyncio.StreamWriter, status: str, content_type: str, body_str: str):
    body_bytes = body_str.encode("utf-8", errors="replace")
    headers = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}; charset=utf-8\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("utf-8")
    writer.write(headers + body_bytes)


async def handle_http_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, bot_ref):
    try:
        raw = await asyncio.wait_for(reader.read(16384), timeout=8.0)
        if not raw:
            writer.close()
            return
        text = raw.decode("utf-8", errors="ignore")
        if "\r\n\r\n" not in text:
            writer.close()
            return

        header_part, body = text.split("\r\n\r\n", 1)
        lines = header_part.split("\r\n")
        req_line = lines[0].split(" ")
        if len(req_line) < 2:
            writer.close()
            return

        method, full_path = req_line[0].upper(), req_line[1]
        parsed = urlparse(full_path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        bot_name = bot_ref.user.name if getattr(bot_ref, "user", None) else "ApexHosting"

        # Serve static xterm.js assets from /opt/Frosty-vps-bot/static/
        if method == "GET" and path.startswith("/static/"):
            fname = os.path.basename(path)
            fpath = os.path.join(BOT_DIR, "static", fname)
            if os.path.isfile(fpath):
                ctype = "text/css" if fname.endswith(".css") else "application/javascript"
                with open(fpath, "r", encoding="utf-8", errors="ignore") as sf:
                    _send_http(writer, "200 OK", ctype, sf.read())
                await writer.drain()
                return

        # Serve WebSSH HTML UI
        if method == "GET" and (path == "/" or path == "/ssh" or path.startswith("/ssh/")):
            token = ""
            if path.startswith("/ssh/"):
                token = path.split("/ssh/", 1)[1].strip("/")
            elif "token" in qs:
                token = qs["token"][0]
            html = _render_webssh_html(token, bot_name)
            _send_http(writer, "200 OK", "text/html", html)
            await writer.drain()
            return

        payload = {}
        if method == "POST" and body.strip():
            try:
                payload = json.loads(body.strip())
            except Exception:
                payload = {}

        if method == "POST" and path == "/pair":
            req_token = payload.get("token", "").strip()
            node_name = payload.get("node_name", "Node-2").strip()
            node_ip = payload.get("ip", "").strip()
            ssh_port = int(payload.get("ssh_port", 22))
            step = payload.get("step", "init")

            if req_token != DISCORD_BOT_TOKEN.strip():
                _send_http(writer, "401 Unauthorized", "application/json", json.dumps({"ok": False, "error": "Token mismatch"}))
            else:
                pub_key = ensure_ssh_key()
                if step == "confirm" and node_ip:
                    register_node(node_name, node_ip, ssh_port, is_local=False)
                _send_http(writer, "200 OK", "application/json", json.dumps({
                    "ok": True,
                    "bot_name": bot_name,
                    "bot_id": str(bot_ref.user.id) if getattr(bot_ref, "user", None) else "0",
                    "pubkey": pub_key,
                    "node_name": node_name
                }))
            await writer.drain()
            return

        if method == "POST" and path == "/api/ssh/unlock":
            token = payload.get("token", "")
            pin = str(payload.get("pin", "")).strip()
            t_info = WEBSSH_TOKENS.get(token)
            if not t_info:
                _send_http(writer, "200 OK", "application/json", json.dumps({"success": False, "code": "token_used", "error": "Session link expired."}))
            elif t_info["attempts"] <= 0:
                _send_http(writer, "200 OK", "application/json", json.dumps({"success": False, "code": "too_many_pins", "error": "Too many failed PIN attempts."}))
            elif pin != t_info["pin"]:
                t_info["attempts"] -= 1
                _send_http(writer, "200 OK", "application/json", json.dumps({"success": False, "error": f"Incorrect PIN ({t_info['attempts']} attempts left)."}))
            else:
                _send_http(writer, "200 OK", "application/json", json.dumps({"success": True, "key": t_info["unlock_key"]}))
            await writer.drain()
            return

        if method == "POST" and path == "/api/ssh/connect":
            token = payload.get("token", "")
            unlock_key = payload.get("unlock_key", "")
            host = payload.get("host", "127.0.0.1")
            port = int(payload.get("port", 22))
            username = payload.get("username", "root")
            password = payload.get("password", "")

            if token and token in WEBSSH_TOKENS:
                t_info = WEBSSH_TOKENS[token]
                if t_info.get("require_pin") and unlock_key != t_info.get("unlock_key"):
                    _send_http(writer, "200 OK", "application/json", json.dumps({"success": False, "code": "pin_required"}))
                    await writer.drain()
                    return
                # Always use the token's verified internal/private routing host
                host = t_info["host"]
                port = int(t_info["port"])
                username = t_info["username"]
                password = t_info["password"]

            session_id = _start_pty_ssh(host, port, username, password)
            await asyncio.sleep(0.4)
            _send_http(writer, "200 OK", "application/json", json.dumps({"success": True, "session_id": session_id}))
            await writer.drain()
            return

        if method == "POST" and path == "/api/ssh/read":
            session_id = payload.get("session_id", "")
            last_idx = int(payload.get("last_index", 0))
            sess = SSH_SESSIONS.get(session_id)
            if not sess:
                _send_http(writer, "200 OK", "application/json", json.dumps({"success": False, "error": "Session not found or ended."}))
            else:
                buf = sess["buffer"]
                new_data = buf[last_idx:] if last_idx < len(buf) else ""
                _send_http(writer, "200 OK", "application/json", json.dumps({
                    "success": True,
                    "data": new_data,
                    "last_index": len(buf),
                    "closed": sess["closed"]
                }))
            await writer.drain()
            return

        if method == "POST" and path == "/api/ssh/write":
            session_id = payload.get("session_id", "")
            data = payload.get("data", "")
            sess = SSH_SESSIONS.get(session_id)
            if sess and not sess["closed"]:
                try:
                    os.write(sess["master_fd"], data.encode("utf-8"))
                except OSError:
                    sess["closed"] = True
            _send_http(writer, "200 OK", "application/json", json.dumps({"success": True}))
            await writer.drain()
            return

        if method == "POST" and path == "/api/ssh/resize":
            session_id = payload.get("session_id", "")
            cols = int(payload.get("cols", 100))
            rows = int(payload.get("rows", 30))
            sess = SSH_SESSIONS.get(session_id)
            if sess and not sess["closed"]:
                try:
                    winsize = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(sess["master_fd"], termios.TIOCSWINSZ, winsize)
                except Exception:
                    pass
            _send_http(writer, "200 OK", "application/json", json.dumps({"success": True}))
            await writer.drain()
            return

        if method == "POST" and path == "/api/ssh/disconnect":
            session_id = payload.get("session_id", "")
            _close_pty_session(session_id)
            _send_http(writer, "200 OK", "application/json", json.dumps({"success": True}))
            await writer.drain()
            return

        _send_http(writer, "404 Not Found", "application/json", json.dumps({"error": "Not found"}))
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

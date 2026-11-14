"""Web terminal + node-pairing HTTP API (one aiohttp server, WebSocket based).

The terminal runs `docker exec -it` on the node that hosts the container (locally or over the bot's SSH key),
so it needs no container SSH port, no password and works on private/NAT'd IPv4 addresses.
"""
from __future__ import annotations

import asyncio
import fcntl
import hmac
import json
import os
import pty
import secrets
import signal
import struct
import termios
import time
from typing import Any, Callable, Dict, List, Optional

from aiohttp import WSMsgType, web

import storage
from config import (
    BOT_VERSION, STATIC_DIR, TEMPLATE_PATH, WEB_BIND, WEB_IDLE_TIMEOUT, WEB_MAX_SESSION,
    WEB_PORT, WEB_TOKEN_TTL, DISCORD_BOT_TOKEN, is_private_ip, resolve_server_ip, web_base_url,
)
from docker_utils import console_argv, ensure_ssh_key

TOKENS: Dict[str, Dict[str, Any]] = {}
BOT_INFO: Callable[[], Dict[str, str]] = lambda: {"name": "Apex Hosting", "id": "0"}


def create_link(owner_id: int, vps: Dict[str, Any], bot_name: str) -> str:
    _purge()
    token = secrets.token_urlsafe(32)
    TOKENS[token] = {
        "owner": owner_id, "vps": vps, "bot_name": bot_name, "expires": time.time() + WEB_TOKEN_TTL,
    }
    return f"{web_base_url()}/t/{token}"


def _purge() -> None:
    now = time.time()
    for t in [t for t, v in TOKENS.items() if v["expires"] < now]:
        TOKENS.pop(t, None)


def link_hint() -> str:
    base = web_base_url()
    host = base.split("://", 1)[-1].split(":")[0].split("/")[0]
    if is_private_ip(host):
        return ("ℹ️ This link uses a **private/LAN address** — it only opens from the same network or VPN. "
                "Admins: set `WEB_BASE_URL` (e.g. a Cloudflare Tunnel → `http://localhost:%d`) for access from anywhere." % WEB_PORT)
    return ""


def set_ws_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
    except OSError:
        pass


def _page(title: str, msg: str, status: int = 200) -> web.Response:
    html = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><body style=\"font-family:system-ui;background:#0b0e14;color:#c9d1d9;display:flex;"
            f"align-items:center;justify-content:center;height:100vh;margin:0\"><div style='text-align:center;max-width:420px;padding:24px'>"
            f"<h2 style='color:#7c6aff'>{title}</h2><p>{msg}</p></div>")
    return web.Response(text=html, content_type="text/html", status=status)


# ── handlers ─────────────────────────────────────────────────────────────
async def index(request: web.Request) -> web.Response:
    info = BOT_INFO()
    return _page(f"{info['name']} Web Terminal", f"Online · v{BOT_VERSION}. Use <b>/console</b> in Discord to get a session link.")


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "version": BOT_VERSION, "sessions": len(TOKENS)})


async def terminal_page(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    _purge()
    entry = TOKENS.get(token)
    if not entry:
        return _page("Link expired", "This console link is invalid, expired or was already used. Run <b>/console</b> again in Discord.", 410)
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = (html.replace("__BOT_NAME__", entry["bot_name"]).replace("__LABEL__", entry["vps"]["container_name"])
            .replace("__TOKEN__", token).replace("__VERSION__", BOT_VERSION))
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def ws_terminal(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    entry = TOKENS.pop(token, None)  # one-time use: link cannot be shared after it is opened
    if not entry or entry["expires"] < time.time():
        return web.Response(status=410, text="expired")
    origin = request.headers.get("Origin")
    if origin and origin.split("://", 1)[-1].split("/")[0] != request.headers.get("Host", ""):
        return web.Response(status=403, text="bad origin")

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await ws.prepare(request)

    vps = entry["vps"]
    argv = console_argv(vps)
    master, slave = pty.openpty()
    set_ws_winsize(slave, 30, 100)

    def _ctty():
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    env = {**os.environ, "TERM": "xterm-256color"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=slave, stdout=slave, stderr=slave, env=env,
            start_new_session=True, preexec_fn=_ctty, close_fds=True)
    except FileNotFoundError as e:
        os.close(master); os.close(slave)
        await ws.send_str(json.dumps({"t": "err", "d": f"Missing binary on bot host: {e.filename}"}))
        await ws.close()
        return ws
    os.close(slave)
    os.set_blocking(master, False)

    loop = asyncio.get_running_loop()
    out_q: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
    last_input = time.time()
    started = time.time()

    def _readable():
        try:
            data = os.read(master, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            try:
                loop.remove_reader(master)
            except Exception:
                pass
        out_q.put_nowait(data or None)

    loop.add_reader(master, _readable)

    async def pump_out():
        while True:
            data = await out_q.get()
            if data is None:
                break
            await ws.send_bytes(data)

    async def watchdog():
        while not ws.closed:
            await asyncio.sleep(5)
            now = time.time()
            if now - last_input > WEB_IDLE_TIMEOUT:
                await ws.send_str(json.dumps({"t": "err", "d": "Session closed: idle timeout."}))
                break
            if now - started > WEB_MAX_SESSION:
                await ws.send_str(json.dumps({"t": "err", "d": "Session closed: maximum duration reached."}))
                break
        out_q.put_nowait(None)

    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(watchdog())]
    waiter = asyncio.create_task(proc.wait())

    async def reader():
        nonlocal last_input
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                if m.get("t") == "i":
                    last_input = time.time()
                    try:
                        os.write(master, str(m.get("d", "")).encode())
                    except OSError:
                        break
                elif m.get("t") == "r":
                    set_ws_winsize(master, int(m.get("r", 30)), int(m.get("c", 100)))
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break

    read_task = asyncio.create_task(reader())
    try:
        await asyncio.wait({tasks[0], waiter, read_task}, return_when=asyncio.FIRST_COMPLETED)
        await asyncio.sleep(0.2)  # let final output flush
        while not out_q.empty():
            data = out_q.get_nowait()
            if data:
                await ws.send_bytes(data)
    finally:
        try:
            loop.remove_reader(master)
        except Exception:
            pass
        for t in (*tasks, waiter, read_task):
            t.cancel()
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        try:
            os.close(master)
        except OSError:
            pass
        if not ws.closed:
            await ws.send_str(json.dumps({"t": "end"}))
            await ws.close()
    return ws


_pair_fail: Dict[str, List[float]] = {}


async def pair(request: web.Request) -> web.Response:
    """Worker-node auto pairing (used by the Apex installer). Authenticated by PAIR_SECRET (or, for old
    installers, the bot token). Constant-time compare + simple per-IP rate limit."""
    ip = request.remote or "?"
    now = time.time()
    fails = [t for t in _pair_fail.get(ip, []) if now - t < 300]
    if len(fails) >= 8:
        return web.json_response({"ok": False, "error": "Too many attempts"}, status=429)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Bad JSON"}, status=400)
    given = str(payload.get("token", "")).strip().encode()
    valid = [storage.get_pair_secret().encode()]
    if DISCORD_BOT_TOKEN:
        valid.append(DISCORD_BOT_TOKEN.encode())  # legacy installers
    if not any(hmac.compare_digest(given, v) for v in valid):
        fails.append(now)
        _pair_fail[ip] = fails
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
    node_name = str(payload.get("node_name", "Node-2")).strip()[:40] or "Node-2"
    node_ip = str(payload.get("ip", "")).strip()
    if payload.get("step") == "confirm" and node_ip:
        storage.register_node(node_name, node_ip, int(payload.get("ssh_port", 22)))
    info = BOT_INFO()
    return web.json_response({"ok": True, "bot_name": info["name"], "bot_id": info["id"],
                              "pubkey": ensure_ssh_key(), "node_name": node_name})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/t/{token}", terminal_page)
    app.router.add_get("/ws/{token}", ws_terminal)
    app.router.add_post("/pair", pair)
    app.router.add_static("/static/", str(STATIC_DIR), follow_symlinks=False)
    return app


async def start_server() -> web.AppRunner:
    runner = web.AppRunner(make_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, WEB_BIND, WEB_PORT)
    await site.start()
    return runner

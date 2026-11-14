"""Reverse SSH tunnel support for worker nodes that the bot cannot reach directly
(NAT, another private network, firewalled hosts).

The WORKER dials OUT to the primary and exposes its own sshd on a loopback port of the primary
(ssh -R 127.0.0.1:<port>:127.0.0.1:22). The bot then treats the node as 127.0.0.1:<port>.
The tunnel account is locked down: it can only create that single loopback forward, nothing else."""
from __future__ import annotations

import os
import pwd
import re
import shutil
import socket
import subprocess
from typing import Optional, Tuple

import storage
from config import TUNNEL_HOME, TUNNEL_PORT_RANGE, TUNNEL_USER

KEY_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/=]+( [^\r\n]*)?$")


class TunnelError(Exception):
    pass


def _home_and_ids() -> Tuple[str, Optional[int], Optional[int]]:
    """Returns (home_dir, uid, gid). With TUNNEL_HOME set (tests) no system user is touched."""
    if TUNNEL_HOME:
        os.makedirs(TUNNEL_HOME, exist_ok=True)
        return TUNNEL_HOME, None, None
    if os.geteuid() != 0:
        raise TunnelError("The bot must run as root to manage the tunnel account.")
    try:
        pw = pwd.getpwnam(TUNNEL_USER)
    except KeyError:
        nologin = shutil.which("nologin") or "/usr/sbin/nologin"
        # -p '*' = no password but NOT locked, so key logins are accepted even with UsePAM
        r = subprocess.run(["useradd", "-m", "-s", nologin, "-p", "*", TUNNEL_USER], capture_output=True, text=True)
        if r.returncode != 0:
            raise TunnelError(f"Could not create user {TUNNEL_USER}: {r.stderr.strip()[:200]}")
        pw = pwd.getpwnam(TUNNEL_USER)
    return pw.pw_dir, pw.pw_uid, pw.pw_gid


def allocate_port(node_name: str) -> int:
    """Re-use the node's existing tunnel port if it has one, otherwise pick a free loopback port."""
    existing = storage.get_node(node_name)
    if existing and existing["ip"] == "127.0.0.1" and not existing["is_local"]:
        return int(existing["ssh_port"])
    used = {int(n["ssh_port"]) for n in storage.get_nodes().values()}
    for port in range(TUNNEL_PORT_RANGE[0], TUNNEL_PORT_RANGE[1] + 1):
        if port in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise TunnelError("No free tunnel ports left in the configured range.")


def authorize(node_name: str, pubkey: str, port: int) -> None:
    """Add (or replace) the worker's tunnel key, restricted to exactly one loopback listen port."""
    pubkey = " ".join((pubkey or "").split())
    if not KEY_RE.match(pubkey):
        raise TunnelError("That is not a valid SSH public key.")
    key_type, key_b64 = pubkey.split()[:2]
    home, uid, gid = _home_and_ids()
    ssh_dir = os.path.join(home, ".ssh")
    os.makedirs(ssh_dir, exist_ok=True)
    os.chmod(ssh_dir, 0o700)
    path = os.path.join(ssh_dir, "authorized_keys")
    tag = f"frosty-tunnel-{node_name}"
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.rstrip("\n") for l in f if l.strip() and not l.rstrip().endswith(" " + tag)]
    lines.append(f'restrict,port-forwarding,permitlisten="127.0.0.1:{port}" {key_type} {key_b64} {tag}')
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    if uid is not None:
        for p in (ssh_dir, path):
            os.chown(p, uid, gid)


def revoke(node_name: str) -> None:
    try:
        home, _, _ = _home_and_ids()
    except TunnelError:
        return
    path = os.path.join(home, ".ssh", "authorized_keys")
    if not os.path.exists(path):
        return
    tag = f"frosty-tunnel-{node_name}"
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        keep = [l for l in f if l.strip() and not l.rstrip().endswith(" " + tag)]
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(keep)

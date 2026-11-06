"""Node + container operations. All commands are argv lists (no shell injection)."""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import shlex
import string
from typing import Any, Dict, List, Optional, Tuple

import storage
from config import (
    AUTO_UPGRADE, DEPLOY_TIMEOUT, ENABLE_SSHX, ENABLE_TMATE, OS_IMAGES, PORT_RANGE,
    SSH_KEY_PATH, SSH_PUB_KEY_PATH,
)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_ALPHABET = string.ascii_letters + string.digits
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def gen_password(n: int = 16) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


# ── ssh key ──────────────────────────────────────────────────────────────
def ensure_ssh_key() -> str:
    SSH_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not SSH_KEY_PATH.exists():
        os.system(f'ssh-keygen -t ed25519 -f {shlex.quote(str(SSH_KEY_PATH))} -N "" -C frosty-bot-node >/dev/null 2>&1')
    try:
        os.chmod(SSH_KEY_PATH, 0o600)
    except OSError:
        pass
    try:
        return SSH_PUB_KEY_PATH.read_text().strip()
    except OSError:
        return ""


# ── command runner ───────────────────────────────────────────────────────
def ssh_base(node: Dict[str, Any], tty: bool = False) -> List[str]:
    args = [
        "ssh", "-i", str(SSH_KEY_PATH), "-p", str(int(node.get("ssh_port", 22))),
        "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={SSH_KEY_PATH.parent / 'known_hosts'}",
        "-o", "IdentitiesOnly=yes", "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
        "-o", "ServerAliveInterval=15",
    ]
    if tty:
        args.append("-tt")
    args.append(f"{node.get('user', 'root')}@{node['ip']}")
    return args


def wrap_for_node(node: Dict[str, Any], argv: List[str], tty: bool = False) -> List[str]:
    """argv that runs `argv` on the node (locally, or through ssh)."""
    if node.get("is_local"):
        return argv
    ensure_ssh_key()
    return ssh_base(node, tty=tty) + [shlex.join(argv)]


async def run(node_name: str, argv: List[str], timeout: int = 60, stdin: Optional[str] = None) -> Tuple[int, str, str]:
    node = storage.get_node(node_name) or storage.local_node()
    full = wrap_for_node(node, argv)
    try:
        proc = await asyncio.create_subprocess_exec(
            *full,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return 127, "", f"Missing binary on bot host: {e.filename}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 124, "", f"Timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="ignore").strip(), err.decode(errors="ignore").strip()


# ── node pairing / stats ─────────────────────────────────────────────────
async def pair_remote_node(name: str, ip: str, port: int, secret: str = "") -> Tuple[bool, str]:
    """Install the bot key on a worker. `secret` = root password (needs sshpass on the bot host) or empty if the
    key is already authorised. Only authorized_keys is modified; sshd config is never touched."""
    pub = ensure_ssh_key()
    if not pub:
        return False, "Could not create the bot SSH key (is openssh-client installed?)"
    secret = (secret or "").strip()
    if secret and secret.upper() != "KEY_ONLY" and not secret.startswith("ssh-"):
        script = (
            "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
            f"(grep -qxF {shlex.quote(pub)} ~/.ssh/authorized_keys || echo {shlex.quote(pub)} >> ~/.ssh/authorized_keys)"
        )
        env = {**os.environ, "SSHPASS": secret}
        try:
            proc = await asyncio.create_subprocess_exec(
                "sshpass", "-e", "ssh", "-p", str(int(port)), "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={SSH_KEY_PATH.parent / 'known_hosts'}",
                "-o", "PubkeyAuthentication=no", "-o", "ConnectTimeout=15", "-o", "LogLevel=ERROR",
                f"root@{ip}", script, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), 40)
        except FileNotFoundError:
            return False, "sshpass is not installed on the bot host (apt install sshpass), or add the key manually."
        except asyncio.TimeoutError:
            return False, "SSH password login timed out."
        if proc.returncode != 0:
            return False, err.decode(errors="ignore").strip() or "SSH password login failed (is root login by password allowed?)"
    node = {"name": name, "ip": ip, "ssh_port": int(port), "user": "root", "is_local": False}
    try:
        proc = await asyncio.create_subprocess_exec(
            *wrap_for_node(node, ["docker", "info", "--format", "{{.ServerVersion}}"]),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 30)
    except asyncio.TimeoutError:
        return False, "Key login timed out."
    if proc.returncode != 0:
        msg = err.decode(errors="ignore").strip()
        return False, msg or "Key login failed - add the key shown by /node key to the worker's authorized_keys."
    return True, out.decode().strip() or "unknown"


async def node_stats(node_name: str) -> Dict[str, Any]:
    script = (
        "echo \"$(nproc)|$(free -m | awk '/Mem:/ {print $3\"/\"$2\" MB\"}')|"
        "$(uptime -p 2>/dev/null || echo up)|$(docker ps -q 2>/dev/null | wc -l)|"
        "$(df -h / | awk 'NR==2 {print $3\"/\"$2}')\""
    )
    rc, out, err = await run(node_name, ["bash", "-c", script], timeout=15)
    if rc != 0 or "|" not in out:
        return {"online": False, "error": err or out or "Unreachable"}
    p = out.splitlines()[-1].split("|")
    p += ["?"] * (5 - len(p))
    return {"online": True, "cores": p[0], "ram": p[1], "uptime": p[2], "containers": p[3], "disk": p[4]}


# ── provisioning ─────────────────────────────────────────────────────────
def _provision_script(root_pass: str) -> str:
    sshx = (
        "(curl -fsSL https://sshx.io/get | sh >/dev/null 2>&1 || true)\n" if ENABLE_SSHX else ""
    )
    tmate = "apt-get install -y --no-install-recommends tmate >/dev/null 2>&1 || true\n" if ENABLE_TMATE else ""
    upgrade = "apt-get upgrade -y >/dev/null 2>&1 || true\n" if AUTO_UPGRADE else ""
    return f"""set -e
export DEBIAN_FRONTEND=noninteractive
mkdir -p /run/sshd
apt-get update -y >/dev/null
{upgrade}apt-get install -y --no-install-recommends openssh-server sudo curl wget nano ca-certificates procps iproute2 iputils-ping >/dev/null
{tmate}(apt-get install -y --no-install-recommends neofetch >/dev/null 2>&1 || apt-get install -y --no-install-recommends screenfetch >/dev/null 2>&1 || true)
{sshx}echo 'root:'{shlex.quote(root_pass)} | chpasswd
sed -i '1i PermitRootLogin yes\\nPasswordAuthentication yes' /etc/ssh/sshd_config
cat > /usr/local/bin/frosty-init <<'EOF_INIT'
#!/bin/bash
mkdir -p /run/sshd
ssh-keygen -A >/dev/null 2>&1
pgrep -x sshd >/dev/null || /usr/sbin/sshd
EOF_INIT
chmod +x /usr/local/bin/frosty-init
/usr/local/bin/frosty-init
"""


async def _ensure_image(node_name: str, image: str) -> Tuple[bool, str]:
    rc, _, _ = await run(node_name, ["docker", "image", "inspect", image], timeout=20)
    if rc == 0:
        return True, ""
    last = ""
    for src in (image, f"mirror.gcr.io/library/{image}", f"docker.m.daocloud.io/library/{image}"):
        rc, _, err = await run(node_name, ["docker", "pull", src], timeout=600)
        if rc == 0:
            if src != image:
                await run(node_name, ["docker", "tag", src, image], timeout=20)
            return True, ""
        last = err
    return False, f"Could not pull {image}: {last[-300:]}"


async def create_vps(node_name: str, owner_id: int, ram_gb: int, cores: int, disk_gb: int,
                     os_key: str, progress=None) -> Tuple[bool, Dict[str, Any], str]:
    node = storage.get_node(node_name)
    if not node:
        return False, {}, f"Unknown node '{node_name}'"
    node_name = node["name"]
    os_info = OS_IMAGES.get(os_key) or OS_IMAGES["ubuntu-22.04"]
    image = os_info["image"]

    async def step(msg: str):
        if progress:
            try:
                await progress(msg)
            except Exception:
                pass

    rc, _, err = await run(node_name, ["docker", "version", "--format", "{{.Server.Version}}"], timeout=20)
    if rc != 0:
        return False, {}, f"Docker not available on {node_name}: {err[-200:] or 'daemon not running'}"

    await step(f"📥 Pulling `{image}` (first time only)…")
    ok, err = await _ensure_image(node_name, image)
    if not ok:
        return False, {}, err

    # Cap CPUs to what the host really has (otherwise docker refuses to start).
    _, nproc, _ = await run(node_name, ["nproc"], timeout=10)
    host_cpus = int(nproc) if nproc.isdigit() else 1
    use_cpus = max(1, min(cores, host_cpus))

    root_pass = gen_password()
    container = f"vps-{owner_id}-{secrets.token_hex(2)}"
    boot = "[ -x /usr/local/bin/frosty-init ] && /usr/local/bin/frosty-init; exec sleep infinity"

    taken = storage.used_ports(node_name)
    created = False
    ssh_port = 0
    disk_enforced = True
    last_err = ""
    await step("🧱 Creating container…")
    for attempt in range(8):
        ssh_port = secrets.randbelow(PORT_RANGE[1] - PORT_RANGE[0]) + PORT_RANGE[0]
        if ssh_port in taken:
            continue
        argv = [
            "docker", "run", "-d", "--name", container, "--hostname", container.replace("_", "-")[:63],
            "--memory", f"{ram_gb}g", "--memory-swap", f"{ram_gb}g", "--cpus", str(use_cpus),
            "--pids-limit", "4096", "--dns", "1.1.1.1", "--dns", "8.8.8.8",
            "-p", f"{ssh_port}:22", "--restart", "unless-stopped",
            "--label", "frosty.vps=1", "--label", f"frosty.owner={owner_id}",
        ]
        if disk_enforced:
            argv += ["--storage-opt", f"size={disk_gb}g"]
        argv += [image, "bash", "-c", boot]
        rc, _, err = await run(node_name, argv, timeout=120)
        if rc == 0:
            created = True
            break
        last_err = err
        low = err.lower()
        if "storage-opt" in low or "quota" in low or "backing filesystem" in low:
            disk_enforced = False  # overlay2 without pquota: run without a hard disk limit
            await run(node_name, ["docker", "rm", "-f", container], timeout=30)
            continue
        if "already allocated" in low or "address already in use" in low:
            taken.add(ssh_port)
            await run(node_name, ["docker", "rm", "-f", container], timeout=30)
            continue
        break
    if not created:
        await run(node_name, ["docker", "rm", "-f", container], timeout=30)
        return False, {}, last_err[-400:] or "docker run failed"

    await step("⚙️ Installing SSH, tools & updates (this takes a minute)…")
    rc, out, err = await run(node_name, ["docker", "exec", "-i", container, "bash", "-s"],
                             timeout=DEPLOY_TIMEOUT, stdin=_provision_script(root_pass))
    if rc != 0:
        await run(node_name, ["docker", "rm", "-f", "-v", container], timeout=30)
        return False, {}, f"Provisioning failed: {(err or out)[-400:]}"

    _, fetch, _ = await run(
        node_name,
        ["docker", "exec", container, "bash", "-c", "(neofetch --stdout || screenfetch -N || uname -a) 2>/dev/null | head -n 18"],
        timeout=25)
    fetch = _ANSI.sub("", fetch).strip() or f"{os_info['label']} | {ram_gb}GB RAM | {cores} vCPU"
    if not disk_enforced:
        fetch += f"\n(note: {disk_gb}GB disk quota not enforced on this host's filesystem)"

    info = {
        "container_name": container, "node": node_name, "ip": node["ip"], "ssh_port": ssh_port,
        "root_password": root_pass, "ram_gb": ram_gb, "cores": cores, "disk_gb": disk_gb,
        "os_key": os_key, "os_label": os_info["label"], "fetch_output": fetch,
    }
    return True, info, ""


# ── container actions ────────────────────────────────────────────────────
async def container_status(node: str, name: str) -> str:
    rc, out, _ = await run(node, ["docker", "inspect", "-f", "{{.State.Status}}", name], timeout=15)
    return out if rc == 0 else "missing"


async def container_stats(node: str, name: str) -> Optional[Dict[str, str]]:
    rc, out, _ = await run(
        node, ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.PIDs}}", name],
        timeout=25)
    if rc != 0 or out.count("|") < 4:
        return None
    cpu, mem, memp, net, pids = out.splitlines()[-1].split("|")[:5]
    return {"cpu": cpu, "mem": mem, "mem_pct": memp, "net": net, "pids": pids}


async def power(node: str, name: str, action: str) -> Tuple[bool, str]:
    if action not in ("start", "stop", "restart", "delete"):
        return False, "Invalid action"
    if action == "delete":
        rc, out, err = await run(node, ["docker", "rm", "-f", "-v", name], timeout=60)
    else:
        rc, out, err = await run(node, ["docker", action, name], timeout=60)
        if rc == 0 and action in ("start", "restart"):
            await run(node, ["docker", "exec", name, "/usr/local/bin/frosty-init"], timeout=15)
    return rc == 0, out if rc == 0 else (err or out or "failed")


async def reset_password(node: str, name: str) -> Tuple[bool, str]:
    new = gen_password()
    await run(node, ["docker", "start", name], timeout=30)
    rc, out, err = await run(node, ["docker", "exec", "-i", name, "chpasswd"], timeout=20, stdin=f"root:{new}\n")
    if rc != 0:
        return False, err or out or "Failed to reset password"
    await run(node, ["docker", "exec", name, "/usr/local/bin/frosty-init"], timeout=15)
    return True, new


async def get_sshx(node: str, name: str) -> Tuple[bool, str]:
    if not ENABLE_SSHX:
        return False, "SSHX is disabled by the administrator."
    script = r"""
export PATH=$PATH:/usr/local/bin:/root/.local/bin
command -v sshx >/dev/null 2>&1 || { apt-get update -y >/dev/null 2>&1; apt-get install -y --no-install-recommends curl ca-certificates >/dev/null 2>&1; curl -fsSL https://sshx.io/get | sh >/dev/null 2>&1; }
pkill -f '^sshx' 2>/dev/null; rm -f /tmp/sshx.log
nohup sshx --quiet >/tmp/sshx.log 2>&1 &
for i in $(seq 1 12); do
  sed 's/\x1b\[[0-9;]*[A-Za-z]//g' /tmp/sshx.log 2>/dev/null | grep -oE 'https://sshx\.io/s/[A-Za-z0-9_#/-]+' | head -n1 && exit 0
  sleep 1
done
exit 1
"""
    await run(node, ["docker", "start", name], timeout=30)
    rc, out, err = await run(node, ["docker", "exec", name, "bash", "-c", script], timeout=60)
    m = re.search(r"https://sshx\.io/s/\S+", _ANSI.sub("", out))
    if m:
        return True, m.group(0)
    return False, err or "Could not create an SSHX link (does the container have internet access?)"


async def get_tmate(node: str, name: str) -> Tuple[bool, str]:
    if not ENABLE_TMATE:
        return False, "Tmate is disabled by the administrator."
    script = (
        "command -v tmate >/dev/null || { apt-get update -y >/dev/null 2>&1; apt-get install -y tmate >/dev/null 2>&1; }; "
        "rm -f /tmp/tmate.sock; tmate -S /tmp/tmate.sock new-session -d && tmate -S /tmp/tmate.sock wait tmate-ready && "
        "tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'"
    )
    await run(node, ["docker", "start", name], timeout=30)
    rc, out, err = await run(node, ["docker", "exec", name, "bash", "-c", script], timeout=60)
    if rc == 0 and "ssh " in out:
        return True, out.splitlines()[-1].strip()
    return False, err or "Could not create a tmate session"


def console_argv(vps: Dict[str, Any]) -> List[str]:
    """argv for an interactive shell inside the container, executed on whichever node hosts it.
    Goes through `docker exec` on the node, so it works with private IPs / NAT / closed SSH ports."""
    node = storage.get_node(vps["node"]) or storage.local_node()
    inner = ["docker", "exec", "-it", "-e", "TERM=xterm-256color", vps["container_name"],
             "/bin/sh", "-c", "command -v bash >/dev/null 2>&1 && exec bash -l || exec sh"]
    return wrap_for_node(node, inner, tty=True)

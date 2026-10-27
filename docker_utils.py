"""Multi-node Docker container lifecycle, remote SSH bridge, live stats, sshx/tmate, and CPU guard."""
import asyncio
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time

import discord

import config
from config import DOCKER_IMAGE, CPU_THRESHOLD, CPU_CHECK_INTERVAL, SSH_PORT_START, NODE_SSH_KEY, logger
import storage
from storage import vps_data, nodes_data, get_node, default_node_name, save_data


# ─── Multi-Node SSH Keypair Setup ───────────────────────────────────────────

def ensure_node_ssh_key() -> str:
    """Ensure the bot has an ed25519 SSH keypair for connecting to remote nodes."""
    pub_path = str(NODE_SSH_KEY) + ".pub"
    if not NODE_SSH_KEY.exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(NODE_SSH_KEY), "-N", "", "-C", "frosty-bot-node"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            os.chmod(NODE_SSH_KEY, 0o600)
        except Exception:
            pass
    try:
        with open(pub_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _remote_ssh_base_args(node_cfg: dict) -> list:
    return [
        "ssh",
        "-i", str(NODE_SSH_KEY),
        "-p", str(node_cfg.get("ssh_port", 22)),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=8",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=15",
        f"{node_cfg.get('user', 'root')}@{node_cfg['ip']}",
    ]


async def run_host_shell(script: str, timeout=30, node_name: str = None) -> tuple[str, str, int]:
    """Run a raw bash snippet on a node (local or remote via SSH)."""
    node = get_node(node_name)
    if node.get("is_local", True):
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        ssh_args = _remote_ssh_base_args(node) + [f"bash -c {shlex.quote(script)}"]
        proc = await asyncio.create_subprocess_exec(
            *ssh_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "", f"Timed out after {timeout}s", 124
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode


async def pair_remote_node(ip: str, ssh_port: int, root_password: str, user: str = "root") -> tuple[bool, str]:
    """
    Copies the bot's SSH public key to a remote VPS node using sshpass (if password given)
    and verifies that Docker + container networking are ready on that remote node.
    """
    pubkey = ensure_node_ssh_key()
    if not pubkey:
        return False, "Failed to generate bot SSH keypair on main host."

    if root_password and root_password != "KEY_ONLY" and not shutil.which("sshpass"):
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c",
            "DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y sshpass -qq",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    if root_password and root_password != "KEY_ONLY":
        remote_setup = (
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"grep -qxF {shlex.quote(pubkey)} ~/.ssh/authorized_keys 2>/dev/null || echo {shlex.quote(pubkey)} >> ~/.ssh/authorized_keys && "
            f"chmod 600 ~/.ssh/authorized_keys && "
            f"(command -v docker >/dev/null 2>&1 || (curl -fsSL https://get.docker.com | sh)) && "
            f"sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true && "
            f"(systemctl enable --now docker >/dev/null 2>&1 || (pgrep -x dockerd >/dev/null || (nohup dockerd >/tmp/dockerd.log 2>&1 & sleep 4))) && "
            f"docker info >/dev/null 2>&1"
        )
        cmd = [
            "sshpass", "-p", root_password,
            "ssh", "-p", str(ssh_port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=12",
            f"{user}@{ip}", remote_setup,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=240)
        except asyncio.TimeoutError:
            proc.kill()
            return False, "Timed out connecting or installing Docker on remote node."
        if proc.returncode != 0:
            return False, stderr.decode().strip() or "SSH password login or Docker setup failed."

    test_node = {"is_local": False, "ip": ip, "ssh_port": ssh_port, "user": user}
    try:
        proc = await asyncio.create_subprocess_exec(
            *_remote_ssh_base_args(test_node), "docker info --format '{{.ServerVersion}}'",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            return True, f"Connected (Docker v{stdout.decode().strip()})"
        return False, stderr.decode().strip() or "SSH key authentication failed."
    except Exception as e:
        return False, str(e)


async def check_node_online(node_name: str = None) -> tuple[bool, str]:
    """Check if a local or remote node is reachable and return Docker version + RAM/CPU summary."""
    node = get_node(node_name)
    try:
        script = (
            "ver=$(docker info --format '{{.ServerVersion}}' 2>/dev/null) || exit 1; "
            "mem=$(free -m | awk '/^Mem:/{printf \"%d/%d MB\", $3, $2}'); "
            "cores=$(nproc 2>/dev/null || echo '?'); "
            "echo \"Docker v${ver} | ${cores} Cores | RAM: ${mem}\""
        )
        out, err, rc = await run_host_shell(script, timeout=10, node_name=node["name"])
        if rc == 0 and out:
            return True, out
        return False, err or "Docker daemon not responding"
    except Exception as e:
        return False, str(e)


# ─── Docker execution (Local & Remote Node Aware) ───────────────────────────

async def run_docker(command: str, timeout=120, node_name: str = None) -> str:
    """Run a docker CLI command on the specified node (local or remote via SSH)."""
    node = get_node(node_name)
    args = shlex.split(command)

    if node.get("is_local", True):
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        remote_cmd = " ".join(shlex.quote(a) for a in args)
        ssh_args = _remote_ssh_base_args(node) + [remote_cmd]
        proc = await asyncio.create_subprocess_exec(
            *ssh_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise Exception(f"[{node.get('name', 'Node')}] Command timed out after {timeout}s: {command}")
    if proc.returncode != 0:
        raise Exception(stderr.decode().strip() or f"[{node.get('name', 'Node')}] command failed")
    return stdout.decode().strip()


async def docker_exec(container: str, command: str, timeout=60, node_name: str = None):
    """Run a bash command inside a container on the specified node."""
    node = get_node(node_name)
    exec_args = ["docker", "exec", container, "bash", "-c", command]

    if node.get("is_local", True):
        proc = await asyncio.create_subprocess_exec(
            *exec_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        remote_cmd = " ".join(shlex.quote(a) for a in exec_args)
        ssh_args = _remote_ssh_base_args(node) + [remote_cmd]
        proc = await asyncio.create_subprocess_exec(
            *ssh_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise Exception(f"[{node.get('name', 'Node')}] docker exec timed out after {timeout}s")
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode


async def get_container_live_stats(container: str, node_name: str = None) -> dict:
    """Fetch live CPU, Memory, NetIO, and PIDs for a container on its node."""
    try:
        out = await run_docker(
            f"docker stats --no-stream --format '{{{{.CPUPerc}}}}|{{{{.MemUsage}}}}|{{{{.MemPerc}}}}|{{{{.NetIO}}}}|{{{{.PIDs}}}}' {container}",
            timeout=12, node_name=node_name,
        )
        parts = out.strip().strip("'").split("|")
        if len(parts) >= 5:
            return {
                "cpu": parts[0],
                "mem_usage": parts[1],
                "mem_perc": parts[2],
                "net_io": parts[3],
                "pids": parts[4],
            }
    except Exception:
        pass
    return {}


async def next_ssh_port(node_name: str = None) -> int:
    """Find a guaranteed free host port on the target node (checks both vps_data and live listening ports)."""
    target_node = node_name or default_node_name()
    used = {
        int(v["ssh_port"])
        for vl in vps_data.values()
        for v in vl
        if v.get("ssh_port") and v.get("node", default_node_name()) == target_node
    }
    try:
        out, _, rc = await run_host_shell(
            "ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | awk -F: '{print $NF}'",
            timeout=8, node_name=target_node,
        )
        if rc == 0 and out:
            for line in out.splitlines():
                if line.strip().isdigit():
                    used.add(int(line.strip()))
    except Exception:
        pass

    port = SSH_PORT_START
    while port in used:
        port += 1
    return port


async def create_container(container_name: str, ram_mb: int, cpu_count, password: str,
                            disk_gb: int = 30, os_image: str = None, ssh_port: int = None,
                            node_name: str = None):
    """Create and provision a Docker container as a VPS on the selected node."""
    target_node = node_name or default_node_name()
    image = os_image or DOCKER_IMAGE
    try:
        await run_docker(f"docker pull {image}", timeout=300, node_name=target_node)
    except Exception:
        pass

    try:
        await run_docker(f"docker rm -f {container_name}", timeout=30, node_name=target_node)
    except Exception:
        pass

    port_flag = f"-p {ssh_port}:22 " if ssh_port else ""
    keepalive_loop = (
        "while true; do "
        "if [ -x /usr/sbin/sshd ] && ! pgrep -x sshd >/dev/null 2>&1; then "
        "mkdir -p /run/sshd /var/run/sshd && /usr/sbin/sshd; "
        "fi; sleep 5; done"
    )
    run_cmd = (
        f"docker run -d --name {container_name} --hostname {container_name} {port_flag}"
        f"--memory={ram_mb}m --cpus={cpu_count} --restart=unless-stopped "
        f"{image} bash -c {shlex.quote(keepalive_loop)}"
    )
    await run_docker(run_cmd, timeout=60, node_name=target_node)

    disk_quota_applied = False
    try:
        await run_docker(
            f"docker update --storage-opt size={disk_gb}G {container_name}",
            timeout=15, node_name=target_node,
        )
        disk_quota_applied = True
    except Exception:
        pass

    safe_pw = shlex.quote(f"root:{password}")
    setup_script = (
        "export DEBIAN_FRONTEND=noninteractive && "
        "apt-get update -qq && "
        "apt-get upgrade -y -qq && "
        "apt-get install -y -qq openssh-server curl sudo ca-certificates && "
        "(apt-get install -y -qq neofetch || apt-get install -y -qq screenfetch || apt-get install -y -qq fastfetch || true) && "
        "(apt-get install -y -qq tmate || true) && "
        "if ! command -v neofetch >/dev/null 2>&1; then "
        "  if command -v fastfetch >/dev/null 2>&1; then ln -sf $(command -v fastfetch) /usr/local/bin/neofetch; "
        "  elif command -v screenfetch >/dev/null 2>&1; then ln -sf $(command -v screenfetch) /usr/local/bin/neofetch; fi; "
        "fi && "
        "mkdir -p /var/run/sshd /run/sshd && "
        "sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && "
        "sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config && "
        "grep -q '^PermitRootLogin' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config && "
        "grep -q '^PasswordAuthentication' /etc/ssh/sshd_config || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config && "
        f"echo {safe_pw} | chpasswd && "
        "ssh-keygen -A >/dev/null 2>&1 || true && "
        "/usr/sbin/sshd && "
        "(curl -sSf https://sshx.io/get | sh || true) && "
        "grep -q 'FROSTY_AUTO_FETCH' /root/.bashrc 2>/dev/null || "
        "printf '\\n# FROSTY_AUTO_FETCH\\nif [[ $- == *i* ]]; then (command -v neofetch >/dev/null 2>&1 && neofetch) || (command -v screenfetch >/dev/null 2>&1 && screenfetch) || (command -v fastfetch >/dev/null 2>&1 && fastfetch) || true; fi\\n' >> /root/.bashrc"
    )
    stdout, stderr, rc = await docker_exec(container_name, setup_script, timeout=300, node_name=target_node)
    if rc != 0 and "already" not in stderr.lower():
        raise Exception(f"SSH setup failed on {target_node}: {stderr}")

    return disk_quota_applied


async def get_sshx_session(container_name: str, node_name: str = None) -> str:
    """sshx: browser-based terminal link, no SSH client needed."""
    script = (
        "pkill sshx 2>/dev/null || true && sleep 1 && "
        "(command -v sshx >/dev/null 2>&1 || curl -sSf https://sshx.io/get | sh) >/dev/null 2>&1 && "
        "setsid sshx > /tmp/sshx.log 2>&1 < /dev/null & "
        "for i in 1 2 3 4 5 6 7 8 9 10; do "
        "  link=$(sed -r 's/\\x1b\\[[0-9;]*[a-zA-Z]//g' /tmp/sshx.log 2>/dev/null | grep -oE 'https://sshx\\.io/s/[A-Za-z0-9#,._-]+' | head -1); "
        "  if [ -n \"$link\" ]; then echo \"$link\"; break; fi; "
        "  sleep 1; "
        "done"
    )
    stdout, stderr, rc = await docker_exec(container_name, script, timeout=35, node_name=node_name)
    if rc != 0 or not stdout:
        raise Exception(stderr or "sshx failed to start")
    return stdout.splitlines()[-1].strip()


def classic_ssh_command(ssh_port: int, node_name: str = None) -> str:
    node = get_node(node_name)
    ip = node.get("ip") or config.SERVER_PUBLIC_IP
    return f"ssh root@{ip} -p {ssh_port}"


async def get_ssh_access(container_name: str, node_name: str = None) -> dict:
    """Return every SSH method that worked on the container's node."""
    methods = {}
    try:
        methods["sshx"] = await get_sshx_session(container_name, node_name=node_name)
    except Exception:
        logger.warning("sshx failed for %s on node %s", container_name, node_name)
    return methods


async def get_or_create_vps_role(guild):
    if config.VPS_USER_ROLE_ID:
        role = guild.get_role(config.VPS_USER_ROLE_ID)
        if role:
            return role
    role = discord.utils.get(guild.roles, name="VPS User")
    if role:
        config.VPS_USER_ROLE_ID = role.id
        return role
    try:
        role = await guild.create_role(
            name="VPS User", color=discord.Color.dark_purple(),
            reason="VPS User role", permissions=discord.Permissions.none(),
        )
        config.VPS_USER_ROLE_ID = role.id
        return role
    except Exception:
        logger.exception("Failed to create VPS User role")
        return None


# ─── Multi-Node CPU Guard (Host Protection Across All Nodes) ────────────────

def _parse_top_cpu(out: str) -> float:
    for line in out.splitlines():
        if "%Cpu(s):" in line or "Cpu(s):" in line:
            for part in line.split(","):
                part = part.strip()
                if part.endswith("id"):
                    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", part)
                    if m:
                        return 100.0 - float(m.group(1))
    return 0.0


def get_node_cpu_percent_sync(node_cfg: dict) -> float:
    try:
        if node_cfg.get("is_local", True):
            out = subprocess.run(["top", "-bn1"], capture_output=True, text=True, timeout=8).stdout
        else:
            cmd = _remote_ssh_base_args(node_cfg) + ["top -bn1"]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        return _parse_top_cpu(out)
    except Exception:
        return 0.0


_cpu_monitor_started = False


def start_cpu_monitor():
    global _cpu_monitor_started
    if _cpu_monitor_started:
        return
    _cpu_monitor_started = True
    threading.Thread(target=cpu_monitor_loop, daemon=True).start()


def cpu_monitor_loop():
    while True:
        if config.cpu_monitor_active:
            try:
                for n_name, n_cfg in list(nodes_data.items()):
                    usage = get_node_cpu_percent_sync(n_cfg)
                    if usage > CPU_THRESHOLD:
                        running = [
                            v["container_name"]
                            for vlist in vps_data.values()
                            for v in vlist
                            if v.get("status") == "running" and v.get("node", default_node_name()) == n_name
                        ]
                        if running:
                            logger.warning("Node '%s' CPU %.1f%% > %d%% — stopping %d containers", n_name, usage, CPU_THRESHOLD, len(running))
                            if n_cfg.get("is_local", True):
                                subprocess.run(["docker", "stop", "--time=5", *running], check=False)
                            else:
                                remote_stop = "docker stop --time=5 " + " ".join(shlex.quote(c) for c in running)
                                subprocess.run(_remote_ssh_base_args(n_cfg) + [remote_stop], check=False)
                            for vlist in vps_data.values():
                                for v in vlist:
                                    if v.get("status") == "running" and v.get("node", default_node_name()) == n_name:
                                        v["status"] = "stopped"
                            save_data()
            except Exception:
                logger.exception("Error in multi-node CPU monitor loop")
        time.sleep(CPU_CHECK_INTERVAL)


# ─── VPS plans ──────────────────────────────────────────────────────────────

PLANS = {
    "Starter":  {"ram": "4GB",  "cpu": "1", "storage": "10GB", "price": {"Intel": 42,  "AMD": 83}},
    "Basic":    {"ram": "8GB",  "cpu": "1", "storage": "10GB", "price": {"Intel": 96,  "AMD": 164}},
    "Standard": {"ram": "12GB", "cpu": "2", "storage": "10GB", "price": {"Intel": 192, "AMD": 320}},
    "Pro":      {"ram": "16GB", "cpu": "2", "storage": "10GB", "price": {"Intel": 220, "AMD": 340}},
}

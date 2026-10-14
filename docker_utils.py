"""Multi-node Docker container lifecycle, remote SSH node bridge, sshx/tmate, and CPU guard."""
import asyncio
import shlex
import subprocess
import threading
import time

import discord

import config
from config import (
    DOCKER_IMAGE, CPU_THRESHOLD, CPU_CHECK_INTERVAL,
    SSH_PORT_START, NODE_SSH_KEY_PATH, logger,
)
from storage import vps_data, nodes_data, get_node_info, save_data

# ─── SSH Key & Multi-Node Execution Bridge ──────────────────────────────────

def ensure_bot_ssh_key():
    """Ensure the bot has an Ed25519 SSH keypair for communicating with remote nodes."""
    NODE_SSH_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not NODE_SSH_KEY_PATH.exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(NODE_SSH_KEY_PATH), "-N", "", "-C", "frosty-bot-master"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            NODE_SSH_KEY_PATH.chmod(0o600)
        except Exception:
            pass


ensure_bot_ssh_key()


def _ssh_base_args(node_info: dict) -> list[str]:
    return [
        "ssh",
        "-i", str(NODE_SSH_KEY_PATH),
        "-p", str(node_info.get("ssh_port", 22)),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        f"{node_info.get('user', 'root')}@{node_info['ip']}",
    ]


async def pair_remote_node(ip: str, ssh_port: int, password: str, user: str = "root") -> tuple[bool, str]:
    """
    Push the bot's SSH public key to a remote server using sshpass, verify SSH key login,
    and ensure Docker is installed and running on the remote node.
    """
    ensure_bot_ssh_key()
    pub_key_file = f"{NODE_SSH_KEY_PATH}.pub"
    try:
        with open(pub_key_file, "r") as f:
            pubkey = f.read().strip()
    except Exception as e:
        return False, f"Failed to read bot public key: {e}"

    # Ensure sshpass is installed locally on the bot host
    if not subprocess.run(["which", "sshpass"], capture_output=True).returncode == 0:
        proc_install = await asyncio.create_subprocess_exec(
            "apt-get", "update", "-qq",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc_install.communicate()
        proc_install2 = await asyncio.create_subprocess_exec(
            "apt-get", "install", "-y", "sshpass",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc_install2.communicate()

    remote_bootstrap = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"grep -qxF {shlex.quote(pubkey)} ~/.ssh/authorized_keys 2>/dev/null || echo {shlex.quote(pubkey)} >> ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys && "
        "(command -v docker >/dev/null 2>&1 || (curl -fsSL https://get.docker.com | sh)) && "
        "(docker info >/dev/null 2>&1 || systemctl start docker 2>/dev/null || (dockerd >/tmp/dockerd.log 2>&1 & sleep 4)) && "
        "docker info >/dev/null 2>&1 && echo NODE_READY"
    )

    cmd = [
        "sshpass", "-p", password,
        "ssh",
        "-p", str(ssh_port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=15",
        f"{user}@{ip}",
        remote_bootstrap,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "Connection or Docker installation on remote node timed out (180s)."

    out_str = stdout.decode(errors="ignore").strip()
    err_str = stderr.decode(errors="ignore").strip()
    if proc.returncode != 0 or "NODE_READY" not in out_str:
        return False, err_str or out_str or "Could not authenticate or start Docker on remote node."

    return True, "Connected, SSH key installed, and Docker verified online."


async def run_docker(command: str, timeout=120, node: str = None) -> str:
    """Run a docker CLI command on the specified node (local or remote SSH), raise on nonzero exit."""
    node_info = get_node_info(node)
    if node_info.get("is_local", True):
        exec_args = shlex.split(command)
    else:
        exec_args = _ssh_base_args(node_info) + [command]

    proc = await asyncio.create_subprocess_exec(
        *exec_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise Exception(f"Command timed out after {timeout}s on node '{node_info['name']}': {command}")
    if proc.returncode != 0:
        raise Exception(stderr.decode(errors="ignore").strip() or f"Command failed on node '{node_info['name']}'")
    return stdout.decode(errors="ignore").strip()


async def docker_exec(container: str, command: str, timeout=60, node: str = None):
    """Run a shell command inside a container on the specified node. Returns (stdout, stderr, returncode)."""
    node_info = get_node_info(node)
    if node_info.get("is_local", True):
        exec_args = ["docker", "exec", container, "bash", "-c", command]
    else:
        remote_cmd = f"docker exec {shlex.quote(container)} bash -c {shlex.quote(command)}"
        exec_args = _ssh_base_args(node_info) + [remote_cmd]

    proc = await asyncio.create_subprocess_exec(
        *exec_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise Exception(f"docker exec timed out after {timeout}s on node '{node_info['name']}'")
    return stdout.decode(errors="ignore").strip(), stderr.decode(errors="ignore").strip(), proc.returncode


async def get_node_live_stats(node_name: str) -> dict:
    """Query a node (local or remote) for live CPU, RAM, and running container count."""
    node_info = get_node_info(node_name)
    stat_cmd = (
        "free -m | awk '/^Mem:/{printf \"%s/%s MB\", $3, $2}'; echo -n ' | '; "
        "nproc 2>/dev/null || echo 1; echo -n ' | '; "
        "docker ps -q 2>/dev/null | wc -l"
    )
    if node_info.get("is_local", True):
        exec_args = ["bash", "-c", stat_cmd]
    else:
        exec_args = _ssh_base_args(node_info) + [stat_cmd]

    try:
        proc = await asyncio.create_subprocess_exec(
            *exec_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
        if proc.returncode != 0:
            return {"online": False, "ram": "Offline", "cpus": "?", "containers": "?"}
        parts = [p.strip() for p in stdout.decode(errors="ignore").replace("\n", " ").split("|")]
        return {
            "online": True,
            "ram": parts[0] if len(parts) > 0 else "?",
            "cpus": parts[1] if len(parts) > 1 else "?",
            "containers": parts[2] if len(parts) > 2 else "0",
        }
    except Exception:
        return {"online": False, "ram": "Offline", "cpus": "?", "containers": "?"}


def next_ssh_port(node: str = None) -> int:
    """Smallest free host port on the target node for -p mapping."""
    target_node = get_node_info(node)["name"]
    used = {
        v.get("ssh_port")
        for vl in vps_data.values()
        for v in vl
        if v.get("ssh_port") and v.get("node", target_node) == target_node
    }
    port = SSH_PORT_START
    while port in used:
        port += 1
    return port


async def create_container(container_name: str, ram_mb: int, cpu_count, password: str,
                           disk_gb: int = 30, os_image: str = None, ssh_port: int = None,
                           node: str = None):
    """
    Create and provision a Docker container as a VPS on the selected node.
    """
    image = os_image or DOCKER_IMAGE
    try:
        await run_docker(f"docker pull {image}", timeout=300, node=node)
    except Exception:
        pass

    try:
        await run_docker(f"docker rm -f {container_name}", timeout=30, node=node)
    except Exception:
        pass

    port_flag = f"-p {ssh_port}:22 " if ssh_port else ""
    keepalive_cmd = (
        "bash -c 'while true; do "
        "if [ -x /usr/sbin/sshd ] && ! pgrep -x sshd >/dev/null 2>&1; then "
        "mkdir -p /run/sshd /var/run/sshd && /usr/sbin/sshd; "
        "fi; sleep 5; done'"
    )
    run_cmd = (
        f"docker run -d --name {container_name} --hostname {container_name} {port_flag}"
        f"--memory={ram_mb}m --cpus={cpu_count} --restart=unless-stopped "
        f"{image} {keepalive_cmd}"
    )
    await run_docker(run_cmd, timeout=60, node=node)

    disk_quota_applied = False
    try:
        await run_docker(
            f"docker update --storage-opt size={disk_gb}G {container_name}", timeout=15, node=node
        )
        disk_quota_applied = True
    except Exception:
        pass

    # Install critical SSH & curl packages first; install fetch/tmate tools as best-effort
    # so missing universe packages on Ubuntu 24.04/26.04 or Debian 13 never abort SSH setup.
    safe_pass = shlex.quote(f"root:{password}")
    setup_script = (
        "export DEBIAN_FRONTEND=noninteractive && "
        "apt-get update -qq && "
        "apt-get upgrade -y -qq && "
        "apt-get install -y -qq openssh-server curl sudo ca-certificates && "
        "(apt-get install -y -qq neofetch || apt-get install -y -qq screenfetch || apt-get install -y -qq fastfetch || true) && "
        "(apt-get install -y -qq tmate || true) && "
        "mkdir -p /var/run/sshd /run/sshd && "
        "sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && "
        "sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config && "
        "grep -q '^PermitRootLogin yes' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config && "
        "grep -q '^PasswordAuthentication yes' /etc/ssh/sshd_config || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config && "
        f"echo {safe_pass} | chpasswd && "
        "ssh-keygen -A >/dev/null 2>&1 && "
        "/usr/sbin/sshd && "
        "(curl -sSf https://sshx.io/get | sh >/dev/null 2>&1 || true) && "
        "grep -q 'FROSTY_FETCH' /root/.bashrc || "
        "echo '# FROSTY_FETCH\ncommand -v neofetch >/dev/null 2>&1 && neofetch || (command -v screenfetch >/dev/null 2>&1 && screenfetch || (command -v fastfetch >/dev/null 2>&1 && fastfetch))' >> /root/.bashrc"
    )
    stdout, stderr, rc = await docker_exec(container_name, setup_script, timeout=300, node=node)
    if rc != 0 and "already" not in stderr.lower():
        raise Exception(f"SSH setup failed on node '{get_node_info(node)['name']}': {stderr}")

    return disk_quota_applied


async def get_sshx_session(container_name: str, node: str = None) -> str:
    """sshx: browser-based terminal link, no SSH client needed."""
    script = (
        "command -v sshx >/dev/null 2>&1 || (curl -sSf https://sshx.io/get | sh >/dev/null 2>&1); "
        "pkill sshx 2>/dev/null || true; sleep 1; "
        "setsid sshx > /tmp/sshx.log 2>&1 < /dev/null & "
        "for i in 1 2 3 4 5 6 7 8; do "
        "  link=$(sed -r 's/\\x1b\\[[0-9;]*[a-zA-Z]//g' /tmp/sshx.log 2>/dev/null | grep -oE 'https://sshx\\.io/s/[A-Za-z0-9#,._-]+' | head -1); "
        "  if [ -n \"$link\" ]; then echo \"$link\"; exit 0; fi; "
        "  sleep 1; "
        "done; exit 1"
    )
    stdout, stderr, rc = await docker_exec(container_name, script, timeout=30, node=node)
    if rc != 0 or not stdout:
        raise Exception(stderr or "sshx failed to start")
    return stdout.splitlines()[-1].strip()


def classic_ssh_command(ssh_port: int, node: str = None) -> str:
    node_ip = get_node_info(node).get("ip", config.SERVER_PUBLIC_IP)
    return f"ssh root@{node_ip} -p {ssh_port}"


async def get_ssh_access(container_name: str, node: str = None) -> dict:
    """Return every SSH method that worked."""
    methods = {}
    try:
        methods["sshx"] = await get_sshx_session(container_name, node=node)
    except Exception:
        logger.warning("sshx failed for %s on node %s", container_name, node)
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


# ─── CPU guard (host protection) ────────────────────────────────────────────

def get_host_cpu_percent() -> float:
    try:
        out = subprocess.run(["top", "-bn1"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "%Cpu(s):" in line:
                for part in line.split(","):
                    part = part.strip()
                    if part.endswith("id"):
                        return 100.0 - float(part.split()[0])
    except Exception:
        logger.exception("Failed to read host CPU usage")
    return 0.0


_cpu_thread_started = False


def start_cpu_monitor_once():
    global _cpu_thread_started
    if _cpu_thread_started:
        return
    _cpu_thread_started = True
    threading.Thread(target=cpu_monitor_loop, daemon=True).start()


def cpu_monitor_loop():
    while True:
        if config.cpu_monitor_active:
            try:
                usage = get_host_cpu_percent()
                if usage > CPU_THRESHOLD:
                    local_nodes = {n for n, info in nodes_data.items() if info.get("is_local", True)}
                    running = [
                        v["container_name"]
                        for vlist in vps_data.values()
                        for v in vlist
                        if v.get("status") == "running" and v.get("node") in local_nodes
                    ]
                    if running:
                        logger.warning("Local Host CPU %.1f%% > %d%% — stopping %d containers", usage, CPU_THRESHOLD, len(running))
                        subprocess.run(["docker", "stop", "--time=5", *running], check=False)
                        for vlist in vps_data.values():
                            for v in vlist:
                                if v.get("status") == "running" and v.get("node") in local_nodes:
                                    v["status"] = "stopped"
                        save_data()
            except Exception:
                logger.exception("Error in CPU monitor loop")
        time.sleep(CPU_CHECK_INTERVAL)


start_cpu_monitor_once()

# ─── VPS plans ──────────────────────────────────────────────────────────────

PLANS = {
    "Starter":  {"ram": "4GB",  "cpu": "1", "storage": "10GB", "price": {"Intel": 42,  "AMD": 83}},
    "Basic":    {"ram": "8GB",  "cpu": "1", "storage": "10GB", "price": {"Intel": 96,  "AMD": 164}},
    "Standard": {"ram": "12GB", "cpu": "2", "storage": "10GB", "price": {"Intel": 192, "AMD": 320}},
    "Pro":      {"ram": "16GB", "cpu": "2", "storage": "10GB", "price": {"Intel": 220, "AMD": 340}},
}

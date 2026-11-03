import asyncio
import os
import random
import re
import shlex
from typing import Tuple, Dict, Any
from config import SSH_KEY_PATH, SSH_PUB_KEY_PATH, OS_IMAGES
from storage import get_nodes


def ensure_ssh_key() -> str:
    os.makedirs(os.path.dirname(SSH_KEY_PATH), exist_ok=True)
    if not os.path.exists(SSH_KEY_PATH):
        os.system(f'ssh-keygen -t ed25519 -f "{SSH_KEY_PATH}" -N "" -C "frosty-bot-node" >/dev/null 2>&1')
    os.chmod(SSH_KEY_PATH, 0o600)
    if os.path.exists(SSH_PUB_KEY_PATH):
        with open(SSH_PUB_KEY_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


async def run_node_cmd(node_name: str, command: str, timeout: int = 120) -> Tuple[int, str, str]:
    nodes = get_nodes()
    node = nodes.get(node_name)
    if not node:
        return 1, "", f"Node '{node_name}' not found in nodes_data.json"

    if node.get("is_local"):
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
    else:
        ensure_ssh_key()
        ip = node.get("ip")
        port = node.get("ssh_port", 22)
        user = node.get("user", "root")
        ssh_cmd = (
            f"ssh -i {shlex.quote(SSH_KEY_PATH)} -p {int(port)} "
            f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o IdentitiesOnly=yes -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=12 "
            f"{shlex.quote(user)}@{shlex.quote(ip)} {shlex.quote(command)}"
        )
        proc = await asyncio.create_subprocess_shell(
            ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode(errors="ignore").strip(), stderr.decode(errors="ignore").strip()
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "Command timed out"


async def pair_remote_node(node_name: str, ip: str, port: int, password_or_mode: str = "KEY_ONLY") -> Tuple[bool, str]:
    pub_key = ensure_ssh_key()
    clean_arg = (password_or_mode or "KEY_ONLY").strip()
    is_key_mode = clean_arg.upper() == "KEY_ONLY" or clean_arg.startswith("ssh-ed25519") or clean_arg == ""

    remote_script = (
        f"mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
        f"echo '{pub_key}' >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys && "
        f"docker info --format '{{{{.ServerVersion}}}}'"
    )
    if not is_key_mode:
        env = os.environ.copy()
        env["SSHPASS"] = clean_arg
        cmd = f"sshpass -e ssh -p {int(port)} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{shlex.quote(ip)} {shlex.quote(remote_script)}"
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return False, stderr.decode(errors="ignore").strip() or "Auth failed"

    verify_cmd = f"ssh -i {shlex.quote(SSH_KEY_PATH)} -p {int(port)} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o IdentitiesOnly=yes root@{shlex.quote(ip)} \"docker info --format '{{{{.ServerVersion}}}}'\""
    proc = await asyncio.create_subprocess_shell(verify_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return False, stderr.decode(errors="ignore").strip() or "Key check failed"
    return True, stdout.decode(errors="ignore").strip() or "unknown"


async def get_node_stats(node_name: str) -> Dict[str, Any]:
    cmd = "echo \"$(nproc)|$(free -m | awk '/Mem:/ {print $3\"/\"$2\" MB\"}')|$(uptime -p 2>/dev/null || echo 'up')|$(docker ps -q 2>/dev/null | wc -l)\""
    rc, out, err = await run_node_cmd(node_name, cmd, timeout=12)
    if rc != 0 or "|" not in out:
        return {"online": False, "error": err or "Unreachable"}
    parts = out.splitlines()[-1].split("|")
    return {
        "online": True,
        "cores": parts[0] if len(parts) > 0 else "1",
        "ram": parts[1] if len(parts) > 1 else "?",
        "uptime": parts[2] if len(parts) > 2 else "up",
        "containers": parts[3] if len(parts) > 3 else "0"
    }


async def create_vps_container(node_name: str, user_id: int, ram_gb: int, cores: int, disk_gb: int, os_key: str = "ubuntu-22.04") -> Tuple[bool, Dict[str, Any], str]:
    nodes = get_nodes()
    node = nodes.get(node_name)
    if not node:
        return False, {}, f"Node `{node_name}` does not exist."

    os_info = OS_IMAGES.get(os_key, OS_IMAGES["ubuntu-22.04"])
    docker_image = os_info["image"]

    ssh_port = random.randint(10000, 60000)
    root_pass = f"Frosty@{random.randint(100000, 999999)}"
    container_name = f"vps-{user_id}-{random.randint(1000, 9999)}"

    # Post setup script: apt update && apt upgrade + neofetch (fallback screenfetch) + sshx
    setup_cmd = (
        f"HOST_CPUS=$(nproc 2>/dev/null || echo 1); "
        f"REQ_CPUS={cores}; "
        f"if [ \"$REQ_CPUS\" -gt \"$HOST_CPUS\" ]; then USE_CPUS=\"$HOST_CPUS\"; else USE_CPUS=\"$REQ_CPUS\"; fi; "
        f"docker run -d --name {container_name} --hostname {container_name} "
        f"--memory={ram_gb}g --cpus=\"$USE_CPUS\" -p 0.0.0.0:{ssh_port}:22 --restart unless-stopped {docker_image} "
        f"bash -c \"export DEBIAN_FRONTEND=noninteractive; "
        f"mkdir -p /var/cache/apt/tmp; "
        f"apt-get update -y && apt-get upgrade -y && "
        f"apt-get install -y --no-install-recommends openssh-server sudo curl wget nano ca-certificates || true; "
        f"(apt-get install -y neofetch || apt-get install -y screenfetch || true); "
        f"(curl -sSf https://sshx.io/get | sh >/dev/null 2>&1 || true); "
        f"mkdir -p /run/sshd && echo 'root:{root_pass}' | chpasswd && "
        f"rm -f /etc/ssh/sshd_config.d/*.conf 2>/dev/null || true && "
        f"sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && "
        f"sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config && "
        f"ssh-keygen -A && /usr/sbin/sshd -D\""
    )

    rc, out, err = await run_node_cmd(node_name, setup_cmd, timeout=240)
    if rc != 0:
        return False, {}, err or "Failed to create container"

    node_ip = node.get("ip", "127.0.0.1")
    vps_info = {
        "container_name": container_name,
        "node": node_name,
        "ip": node_ip,
        "ssh_port": ssh_port,
        "root_password": root_pass,
        "ram_gb": ram_gb,
        "cores": cores,
        "disk_gb": disk_gb,
        "os_label": os_info["label"]
    }
    return True, vps_info, ""


async def get_sshx_session(node_name: str, container_name: str) -> Tuple[bool, str]:
    cmd = (
        f"docker exec {shlex.quote(container_name)} bash -c \""
        f"if ! command -v sshx >/dev/null 2>&1; then curl -sSf https://sshx.io/get | sh >/dev/null 2>&1; fi; "
        f"pkill -f 'sshx' 2>/dev/null || true; "
        f"nohup sshx > /tmp/sshx.log 2>&1 & "
        f"for i in 1 2 3 4 5 6 7 8; do "
        f"  grep -oE 'https://sshx\\.io/s/[A-Za-z0-9_#-]+' /tmp/sshx.log | head -n 1 && break; "
        f"  sleep 1; "
        f"done\""
    )
    rc, out, err = await run_node_cmd(node_name, cmd, timeout=30)
    match = re.search(r"https://sshx\.io/s/[A-Za-z0-9_#-]+", out)
    if match:
        return True, match.group(0)
    return False, err or out or "Failed to generate SSHX link"


async def get_tmate_session(node_name: str, container_name: str) -> Tuple[bool, str]:
    cmd = (
        f"docker exec {shlex.quote(container_name)} bash -c "
        f"\"tmate -S /tmp/tmate.sock new-session -d && tmate -S /tmp/tmate.sock wait tmate-ready && "
        f"tmate -S /tmp/tmate.sock display -p '#{{tmate_ssh}}'\""
    )
    rc, out, err = await run_node_cmd(node_name, cmd, timeout=25)
    if rc == 0 and "ssh " in out:
        return True, out.splitlines()[-1].strip()
    return False, err or "Could not generate tmate session"


async def reset_container_password(node_name: str, container_name: str) -> Tuple[bool, str]:
    new_pass = f"Frosty@{random.randint(100000, 999999)}"
    cmd = f"docker exec {shlex.quote(container_name)} bash -c \"echo 'root:{new_pass}' | chpasswd\""
    rc, out, err = await run_node_cmd(node_name, cmd, timeout=20)
    return (rc == 0), (new_pass if rc == 0 else (err or "Failed to reset password"))


async def manage_container_power(node_name: str, container_name: str, action: str) -> Tuple[bool, str]:
    if action not in ("start", "stop", "restart", "rm -f"):
        return False, "Invalid action"
    rc, out, err = await run_node_cmd(node_name, f"docker {action} {shlex.quote(container_name)}", timeout=30)
    return (rc == 0), (out if rc == 0 else err)

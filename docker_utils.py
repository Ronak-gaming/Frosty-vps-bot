import asyncio
import os
import random
import shlex
from typing import Tuple, Dict, Any
from config import SSH_KEY_PATH, SSH_PUB_KEY_PATH, DOCKER_IMAGE
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
            f"-o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=12 "
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


async def pair_remote_node(node_name: str, ip: str, port: int, password_or_mode: str) -> Tuple[bool, str]:
    pub_key = ensure_ssh_key()
    remote_setup_script = (
        f"mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
        f"(grep -qxF '{pub_key}' /root/.ssh/authorized_keys 2>/dev/null || echo '{pub_key}' >> /root/.ssh/authorized_keys) && "
        f"chmod 600 /root/.ssh/authorized_keys && "
        f"sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config 2>/dev/null || true && "
        f"sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null || true && "
        f"(systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true) && "
        f"if ! command -v docker >/dev/null 2>&1; then curl -fsSL https://get.docker.com | sh >/dev/null 2>&1; fi && "
        f"docker info --format '{{{{.ServerVersion}}}}'"
    )

    if password_or_mode.upper() != "KEY_ONLY":
        env = os.environ.copy()
        env["SSHPASS"] = password_or_mode
        cmd = (
            f"sshpass -e ssh -p {int(port)} "
            f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive "
            f"-o LogLevel=ERROR -o ConnectTimeout=15 "
            f"root@{shlex.quote(ip)} {shlex.quote(remote_setup_script)}"
        )
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="ignore").strip() or "SSH password login failed"
            return False, err_msg

    # Verify passwordless SSH key authentication works
    verify_cmd = (
        f"ssh -i {shlex.quote(SSH_KEY_PATH)} -p {int(port)} "
        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=12 "
        f"root@{shlex.quote(ip)} \"docker info --format '{{{{.ServerVersion}}}}'\""
    )
    proc = await asyncio.create_subprocess_shell(
        verify_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="ignore").strip() or "Permission denied (publickey)"
        return False, err

    docker_ver = stdout.decode(errors="ignore").strip() or "unknown"
    return True, docker_ver


async def get_node_stats(node_name: str) -> Dict[str, Any]:
    cmd = (
        "echo \"$(nproc)|$(free -m | awk '/Mem:/ {print $3\"/\"$2\" MB\"}')|"
        "$(uptime -p 2>/dev/null || echo 'up')|$(docker ps -q 2>/dev/null | wc -l)\""
    )
    rc, out, err = await run_node_cmd(node_name, cmd, timeout=12)
    if rc != 0 or "|" not in out:
        return {"online": False, "error": err or "Unreachable"}
    parts = out.splitlines()[-1].split("|")
    return {
        "online": True,
        "cores": parts[0] if len(parts) > 0 else "?",
        "ram": parts[1] if len(parts) > 1 else "?",
        "uptime": parts[2] if len(parts) > 2 else "up",
        "containers": parts[3] if len(parts) > 3 else "0"
    }


async def create_vps_container(node_name: str, user_id: int, ram_gb: int, cores: int, disk_gb: int) -> Tuple[bool, Dict[str, Any], str]:
    nodes = get_nodes()
    node = nodes.get(node_name)
    if not node:
        return False, {}, f"Node `{node_name}` does not exist."

    ssh_port = random.randint(10000, 60000)
    root_pass = f"Frosty@{random.randint(100000, 999999)}"
    container_name = f"vps-{user_id}-{random.randint(1000, 9999)}"

    setup_cmd = (
        f"docker run -d --name {container_name} --hostname {container_name} "
        f"--memory={ram_gb}g --cpus={cores} -p {ssh_port}:22 --restart unless-stopped {DOCKER_IMAGE} "
        f"bash -c \"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server sudo curl nano tmate && "
        f"mkdir -p /run/sshd && echo 'root:{root_pass}' | chpasswd && "
        f"sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && "
        f"sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config && "
        f"/usr/sbin/sshd -D\""
    )

    rc, out, err = await run_node_cmd(node_name, setup_cmd, timeout=180)
    if rc != 0:
        return False, {}, err or "Failed to create container"

    vps_info = {
        "container_name": container_name,
        "node": node_name,
        "ip": node.get("ip"),
        "ssh_port": ssh_port,
        "root_password": root_pass,
        "ram_gb": ram_gb,
        "cores": cores,
        "disk_gb": disk_gb
    }
    return True, vps_info, ""


async def manage_container_power(node_name: str, container_name: str, action: str) -> Tuple[bool, str]:
    if action not in ("start", "stop", "restart", "rm -f"):
        return False, "Invalid action"
    rc, out, err = await run_node_cmd(node_name, f"docker {action} {shlex.quote(container_name)}", timeout=30)
    return (rc == 0), (out if rc == 0 else err)

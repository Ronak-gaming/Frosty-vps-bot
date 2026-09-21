"""Docker container lifecycle, tmate SSH sessions, and the host CPU guard."""
import asyncio
import shlex
import subprocess
import threading
import time

import discord

import config
from config import DOCKER_IMAGE, CPU_THRESHOLD, CPU_CHECK_INTERVAL, logger
import storage
from storage import vps_data, save_data

# ─── Docker execution ───────────────────────────────────────────────────────

async def run_docker(command: str, timeout=120) -> str:
    """Run a docker CLI command, raise on nonzero exit."""
    proc = await asyncio.create_subprocess_exec(
        *shlex.split(command),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise Exception(f"Command timed out after {timeout}s: {command}")
    if proc.returncode != 0:
        raise Exception(stderr.decode().strip() or "command failed with no stderr")
    return stdout.decode().strip()


async def docker_exec(container: str, command: str, timeout=60):
    """Run a shell command inside a container. Returns (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", container, "bash", "-c", command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode


async def create_container(container_name: str, ram_mb: int, cpu_count, password: str, disk_gb: int = 30):
    """
    Create and provision a Docker container as a VPS.
    SSH access is via tmate (no port needs to be exposed on the host).
    """
    try:
        await run_docker(f"docker pull {DOCKER_IMAGE}", timeout=300)
    except Exception:
        pass  # already present, or a transient registry hiccup — the run below will surface real problems

    run_cmd = (
        f"docker run -d --name {container_name} "
        f"--memory={ram_mb}m --cpus={cpu_count} --restart=unless-stopped "
        f"{DOCKER_IMAGE} sleep infinity"
    )
    await run_docker(run_cmd, timeout=60)

    # Best-effort disk quota. Only works with certain storage drivers
    # (overlay2 on xfs w/ pquota, or zfs) — if unsupported we just skip it
    # rather than silently pretending there's a hard limit.
    disk_quota_applied = False
    try:
        await run_docker(
            f"docker update --storage-opt size={disk_gb}G {container_name}", timeout=15
        )
        disk_quota_applied = True
    except Exception:
        pass

    setup_script = (
        "apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server tmate curl -qq && "
        "mkdir -p /var/run/sshd && "
        "echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config && "
        "echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config && "
        f"echo 'root:{password}' | chpasswd && "
        "/usr/sbin/sshd"
    )
    stdout, stderr, rc = await docker_exec(container_name, setup_script, timeout=180)
    if rc != 0 and "already" not in stderr.lower():
        raise Exception(f"SSH setup failed: {stderr}")

    return disk_quota_applied


async def get_tmate_session(container_name: str) -> str:
    script = (
        "pkill tmate 2>/dev/null || true && sleep 1 && "
        "tmate -S /tmp/tmate.sock new-session -d && "
        "tmate -S /tmp/tmate.sock wait tmate-ready && "
        "tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'"
    )
    stdout, stderr, rc = await docker_exec(container_name, script, timeout=30)
    if rc != 0 or not stdout:
        raise Exception(stderr or "tmate failed to start")
    return stdout


async def get_or_create_vps_role(guild):
    global VPS_USER_ROLE_ID
    if VPS_USER_ROLE_ID:
        role = guild.get_role(VPS_USER_ROLE_ID)
        if role:
            return role
    role = discord.utils.get(guild.roles, name="VPS User")
    if role:
        VPS_USER_ROLE_ID = role.id
        return role
    try:
        role = await guild.create_role(
            name="VPS User", color=discord.Color.dark_purple(),
            reason="VPS User role", permissions=discord.Permissions.none(),
        )
        VPS_USER_ROLE_ID = role.id
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
                        # e.g. "99.3 id" -> take the number BEFORE "id", not the word itself
                        return 100.0 - float(part.split()[0])
    except Exception:
        logger.exception("Failed to read host CPU usage")
    return 0.0


def cpu_monitor_loop():
    while True:
        if config.cpu_monitor_active:
            try:
                usage = get_host_cpu_percent()
                if usage > CPU_THRESHOLD:
                    running = [
                        v["container_name"]
                        for vlist in vps_data.values()
                        for v in vlist
                        if v.get("status") == "running"
                    ]
                    if running:  # never call `docker stop` with zero args
                        logger.warning("Host CPU %.1f%% > %d%% — stopping %d containers", usage, CPU_THRESHOLD, len(running))
                        subprocess.run(["docker", "stop", "--time=5", *running], check=False)
                        for vlist in vps_data.values():
                            for v in vlist:
                                if v.get("status") == "running":
                                    v["status"] = "stopped"
                        save_data()
            except Exception:
                logger.exception("Error in CPU monitor loop")
        time.sleep(CPU_CHECK_INTERVAL)


threading.Thread(target=cpu_monitor_loop, daemon=True).start()

# ─── VPS plans ──────────────────────────────────────────────────────────────

PLANS = {
    "Starter":  {"ram": "4GB",  "cpu": "1", "storage": "10GB", "price": {"Intel": 42,  "AMD": 83}},
    "Basic":    {"ram": "8GB",  "cpu": "1", "storage": "10GB", "price": {"Intel": 96,  "AMD": 164}},
    "Standard": {"ram": "12GB", "cpu": "2", "storage": "10GB", "price": {"Intel": 192, "AMD": 320}},
    "Pro":      {"ram": "16GB", "cpu": "2", "storage": "10GB", "price": {"Intel": 220, "AMD": 340}},
}

# ⚡ Apex Hosting VPS Bot v4

Docker VPS deployer for Discord. Deploy Ubuntu 22.04–26.04 / Debian 11–13 containers with SSH, a **browser
terminal**, SSHX and a persistent button control panel. Multi-node (workers over SSH key).

## Quick start
```bash
sudo ./install.sh          # installs deps, venv, systemd service
nano .env                  # DISCORD_BOT_TOKEN + MAIN_ADMIN_ID
sudo systemctl restart apex-vps-bot
```
Invite the bot with scopes **`bot` + `applications.commands`**. **No privileged intents are required** (slash commands).

## Web terminal (works on private IPv4 / NAT)
`/console` (or the 🖥️ button) gives a single-use, 10-minute link. The terminal runs `docker exec -it` on the node
that hosts the container (locally or via the bot's SSH key), so it does **not** need the container's SSH port,
password, or a public IP on the container.

The *link itself* must be reachable from the user's browser:
| Situation | What to do |
|---|---|
| Public IP | works out of the box (`http://IP:8765`) — open port 8765 |
| Private IP / behind NAT | set `WEB_BASE_URL` to a **Cloudflare Tunnel** hostname → `http://localhost:8765` (recommended, gives HTTPS) |
| LAN / VPN only | works as-is; link uses the LAN IP |
Run `/admin webterm` to see what the bot detected.

## Commands (all slash commands; `!` works if ENABLE_MESSAGE_CONTENT=1)
**Users:** `/help` `/plans` `/buy` `/myvps` `/console` `/sshx` `/credits` `/daily` `/pay` `/leaderboard` `/ping` `/about`
**Admins:** `/admin create|vpslist|deletevps|credits|webterm|seturl|sync|addadmin|removeadmin` · `/node list|add|remove|rename|setip|key`

## Post-boot setup (every new VPS)
1. `apt update` + `apt upgrade` (set `AUTO_UPGRADE=0` to skip the upgrade)
2. Installs the first available of **fastfetch → neofetch → screenfetch**
3. System info + your plan limits are shown automatically on every interactive login: **web console, SSH and SSHX**
   (`/etc/profile.d/99-apex-fetch.sh`, also hooked into bashrc for non-login shells; runs once per session)

## Private vs public IP
The node IP is checked automatically (`SSH_DISPLAY=auto`). On a **private / NAT / CGNAT** IP the bot does **not** show
`ssh root@ip -p port` or the root password, hides *Reset Password*, binds the container's SSH port to `127.0.0.1`, and users
connect only through the **Web Console** and **SSHX**. Public-IP nodes show SSH as before. Force a mode with
`SSH_DISPLAY=always` or `never`. VPS created before this update keep their old behaviour for the fetch banner.

## Adding worker nodes
`/node add name:Node-2 ip:10.0.0.7 password:<root pw>` (needs `sshpass`), or leave password empty after adding the key
from `/node key` to the worker's `~/.ssh/authorized_keys`. Workers need Docker. Only `authorized_keys` is touched —
sshd config is never modified.

## Upgrading from v3
Put the new files in place; on first start `nodes_data.json`, `user_data.json`, `vps_data.json` and `admin_data.json`
are imported into `frosty.db` automatically. Commands changed from `node-add` → `/node add`, `vps-list` → `/admin vpslist`.

## Files
`main.py` bot + commands · `views.py` UI · `docker_utils.py` containers/nodes · `webterm.py` web terminal + pairing API ·
`storage.py` SQLite · `config.py` settings · `terminal.html` + `static/` (xterm.js, bundled offline).

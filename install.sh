#!/usr/bin/env bash
# Frosty VPS Bot — installer (systemd). Run as root from the bot directory.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root"; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"
apt-get update -y >/dev/null
apt-get install -y python3 python3-venv python3-pip openssh-client sshpass curl >/dev/null
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q -U pip -r "$DIR/requirements.txt"
[[ -f "$DIR/.env" ]] || { cp "$DIR/.env.example" "$DIR/.env"; chmod 600 "$DIR/.env"; echo ">> Edit $DIR/.env (token + admin id), then: systemctl restart frosty-vps-bot"; }
cat > /etc/systemd/system/frosty-vps-bot.service <<UNIT
[Unit]
Description=Frosty VPS Bot
After=network-online.target docker.service
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python $DIR/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now frosty-vps-bot
echo "Installed. Logs: journalctl -u frosty-vps-bot -f"

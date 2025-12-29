#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "[1/6] Setup venv..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

echo "[2/6] Install deps..."
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "[3/6] Ensure .env..."
if [ ! -f ".env" ]; then
  echo "❌ .env belum ada"
  echo "cp .env.example .env && nano .env"
  exit 1
fi

echo "[4/6] Install systemd service..."
sudo cp ubot.service /etc/systemd/system/ubot.service
sudo systemctl daemon-reload
sudo systemctl enable ubot.service

echo "[5/6] Restart service..."
sudo systemctl restart ubot.service

echo "[6/6] Done"
echo "Log: journalctl -u ubot.service -f"

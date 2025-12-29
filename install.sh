#!/bin/bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== Update & install packages =="
sudo apt update -y
sudo apt install -y python3 python3-venv python3-pip git sqlite3

echo "== Create venv =="
cd "$APP_DIR"
if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

echo "== Install python deps =="
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt

echo
echo "== Set config in aio_bc.py =="
read -rp "BOT TOKEN (BotFather): " TOKEN
read -rp "API_ID (my.telegram.org): " API_ID
read -rp "API_HASH (my.telegram.org): " API_HASH
read -rp "OWNER_ID (dari @userinfobot): " OWNER_ID

python3 - <<PY
import re, pathlib
p = pathlib.Path("aio_bc.py")
s = p.read_text(encoding="utf-8")

s = re.sub(r'^TOKEN\\s*=\\s*".*?"\\s*$', f'TOKEN = "{TOKEN}"', s, flags=re.M)
s = re.sub(r'^API_ID\\s*=\\s*\\d+\\s*$', f'API_ID = {API_ID}', s, flags=re.M)
s = re.sub(r'^API_HASH\\s*=\\s*".*?"\\s*$', f'API_HASH = "{API_HASH}"', s, flags=re.M)
s = re.sub(r'^OWNER_ID\\s*=\\s*\\d+\\s*$', f'OWNER_ID = {OWNER_ID}', s, flags=re.M)

p.write_text(s, encoding="utf-8")
print("OK: aio_bc.py updated")
PY

echo
echo "== Test run (both) =="
echo "Userbot pertama kali akan minta login (nomor + OTP)."
echo "Tekan CTRL+C setelah login sukses."
python3 aio_bc.py both

#!/bin/bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

echo "== Make scripts executable =="
chmod +x "$APP_DIR/install.sh" || true

echo "== Create runner scripts =="

cat > "$APP_DIR/run_panel.sh" <<EOF
#!/bin/bash
set -e
cd "$APP_DIR"
source venv/bin/activate
exec python3 aio_bc.py panel
EOF

cat > "$APP_DIR/run_ubot.sh" <<EOF
#!/bin/bash
set -e
cd "$APP_DIR"
source venv/bin/activate
exec python3 aio_bc.py ubot
EOF

chmod +x "$APP_DIR/run_panel.sh" "$APP_DIR/run_ubot.sh"

echo "== Create backup script =="
mkdir -p "$APP_DIR/backups"

cat > "$APP_DIR/backup_db.sh" <<EOF
#!/bin/bash
set -euo pipefail
APP="$APP_DIR"
DB="\$APP/data.db"
OUT="\$APP/backups"
mkdir -p "\$OUT"

if [ ! -f "\$DB" ]; then
  exit 0
fi

TS="\$(date +'%Y%m%d-%H%M%S')"
DEST="\$OUT/data.db.\$TS.sqlite"

sqlite3 "\$DB" ".backup '\$DEST'"
gzip -9 "\$DEST"

find "\$OUT" -type f -name "data.db.*.sqlite.gz" -mtime +14 -delete
EOF

chmod +x "$APP_DIR/backup_db.sh"

echo "== Write systemd services =="

sudo tee /etc/systemd/system/panelbc.service > /dev/null <<EOF
[Unit]
Description=BC Panel Bot (BotFather)
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/run_panel.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/ubotbc.service > /dev/null <<EOF
[Unit]
Description=BC Ubot Sender (Pyrogram)
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/run_ubot.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "== Write systemd backup service + timer =="

sudo tee /etc/systemd/system/bcdb-backup.service > /dev/null <<EOF
[Unit]
Description=Backup SQLite DB for BC Bot

[Service]
Type=oneshot
User=$USER_NAME
ExecStart=$APP_DIR/backup_db.sh
EOF

sudo tee /etc/systemd/system/bcdb-backup.timer > /dev/null <<EOF
[Unit]
Description=Daily backup timer for BC DB

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now panelbc ubotbc
sudo systemctl enable --now bcdb-backup.timer

echo
echo "DONE ✅"
echo "Cek status: systemctl status panelbc ubotbc --no-pager"
echo "Log ubot : journalctl -u ubotbc -f"
echo "Timer    : systemctl list-timers --all | grep bcdb"

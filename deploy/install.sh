#!/usr/bin/env bash
# Установка или обновление МедФильтра на сервере Ubuntu 22.04/24.04.
# Запуск: bash install.sh   (повторный запуск обновляет бота из GitHub)
set -euo pipefail

APP=/opt/medfilter
REPO=https://github.com/plyut/medfilter-bot.git
RUN_USER="$(id -un)"

echo "== Пакеты: git, Python, ffmpeg"
sudo apt-get update -y -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q git python3-venv python3-pip ffmpeg

echo "== Код бота"
if [ -d "$APP/.git" ]; then
  git -C "$APP" pull --ff-only
else
  sudo mkdir -p "$APP"
  sudo chown "$RUN_USER" "$APP"
  git clone "$REPO" "$APP"
fi
cd "$APP"

echo "== Зависимости"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "== Создан $APP/.env — в него нужно вписать токены"
fi

echo "== Тесты"
.venv/bin/python -m unittest discover tests

echo "== Автозапуск (systemd)"
sudo tee /etc/systemd/system/medfilter.service >/dev/null <<EOF
[Unit]
Description=MedFilter Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable -q medfilter

if grep -qE '^TELEGRAM_TOKEN=.+' .env && grep -qE '^GIGACHAT_CREDENTIALS=.+' .env; then
  sudo systemctl restart medfilter
  sleep 3
  sudo systemctl --no-pager --lines=15 status medfilter || true
  echo "== Готово: бот запущен"
else
  echo "== Почти готово: впишите TELEGRAM_TOKEN и GIGACHAT_CREDENTIALS в $APP/.env"
  echo "   nano $APP/.env"
  echo "   затем: sudo systemctl restart medfilter"
fi

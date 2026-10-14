#!/usr/bin/env bash
# Первичная настройка сервера Ubuntu 22.04/24.04 для бота диспетчерской.
# Запускать один раз от пользователя с sudo:  bash deploy/setup-server.sh
# Можно запускать повторно — уже сделанное просто пропустится.
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Запустите от обычного пользователя с sudo, не от root." >&2
  exit 1
fi

echo "== Пакеты: Docker (из репозитория Ubuntu), git, firewall"
sudo apt-get update -q
sudo apt-get install -y -q docker.io docker-compose-v2 git ufw sqlite3

echo "== Docker без sudo для $USER"
sudo usermod -aG docker "$USER"
sudo systemctl enable --now docker

echo "== Firewall: SSH, HTTP, HTTPS — остальное закрыто"
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

echo "== Папка данных (владелец — пользователь контейнера, UID 1000)"
cd "$(dirname "$0")/.."
mkdir -p data
sudo chown 1000:1000 data
sudo chmod 750 data
if [[ "$(id -u)" != "1000" ]]; then
  echo "  Ваш UID не 1000 — к файлам в data/ обращайтесь через sudo."
fi

echo
echo "Готово. Перелогиньтесь (чтобы заработал docker без sudo) и продолжайте по deploy/README.md, шаг «Настройки»."

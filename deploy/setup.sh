#!/usr/bin/env bash
# Базовая подготовка свежего сервера Ubuntu 24.04 под «карточка Ai».
# Запускать от root:  bash setup.sh
set -euo pipefail

echo "==> 1/5 Обновление системы и базовые пакеты"
apt update && apt -y upgrade
apt -y install python3 python3-venv python3-pip nginx git curl ufw ca-certificates gnupg

echo "==> 2/5 Google Chrome (нужен парсеру WB: SeleniumBase UC, headless)"
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
  | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
  > /etc/apt/sources.list.d/google-chrome.list
apt update && apt -y install google-chrome-stable

echo "==> 3/5 Системный пользователь kartochka"
id kartochka >/dev/null 2>&1 || useradd -m -s /bin/bash kartochka
# /opt/kartochka заранее НЕ создаём — его создаст scp при заливке кода
# (DEPLOY.md, шаг 4), иначе папка вложится сама в себя.

echo "==> 4/5 Firewall (SSH + HTTP/HTTPS)"
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

echo "==> 5/5 Базовая подготовка готова."
echo "Дальше вручную (см. deploy/DEPLOY.md, начиная с шага 4):"
echo "  • залить код проекта в /opt/kartochka (папки site/ и пр.)"
echo "  • python3 -m venv /opt/kartochka/venv"
echo "  • /opt/kartochka/venv/bin/pip install -r /opt/kartochka/site/requirements.txt"
echo "  • настроить systemd-юнит, nginx, домен (reg.ru DNS) и SSL (certbot)"

#!/usr/bin/env bash
# «карточка Ai» — единый бронебойный деплой на свежую Ubuntu 24.04.
#
#   sudo bash /opt/kartochka/deploy/deploy.sh
#
# Делает ВСЁ: система, Google Chrome (для парсера WB), Python-окружение,
# зависимости, автозапуск (systemd), nginx, firewall, пароль почты.
# Идемпотентно — можно запускать повторно без вреда.
set -Eeuo pipefail

BASE="/opt/kartochka"
DOMAIN_PUNY="xn--your-domain.example"
SVC_USER="kartochka"

c()    { printf '\033[%sm' "$1"; }
log()  { echo; echo "$(c '1;36')==> $*$(c 0)"; }
ok()   { echo "  $(c '1;32')OK$(c 0) $*"; }
warn() { echo "  $(c '1;33')! $(c 0) $*"; }
die()  { echo; echo "$(c '1;31')ОШИБКА: $*$(c 0)" >&2; exit 1; }
trap 'die "оборвалось на строке $LINENO. Прочитай вывод выше и просто запусти скрипт снова — он идемпотентный."' ERR

[ "$(id -u)" -eq 0 ] || die "нужны root-права. Запусти:  sudo bash $0"
[ -f "$BASE/site/server.py" ] || die "нет $BASE/site/server.py — сперва залей проект в $BASE (scp), потом запускай меня."

export DEBIAN_FRONTEND=noninteractive
apt_get() { local n; for n in 1 2 3 4 5; do apt-get "$@" && return 0; warn "apt занят/сбой — повтор $n/5 через 10с…"; sleep 10; done; die "apt не выполнил: $*"; }

log "1/8 Жду, пока Ubuntu закончит свои фоновые авто-обновления…"
for _ in $(seq 1 60); do
  if pgrep -x apt >/dev/null || pgrep -x apt-get >/dev/null || pgrep -x dpkg >/dev/null || pgrep -f unattended-upgrade >/dev/null; then
    sleep 5
  else
    break
  fi
done
ok "apt свободен"

log "2/8 Система и базовые пакеты…"
apt_get update -y
apt_get install -y python3 python3-venv python3-pip nginx git curl ufw ca-certificates gnupg
ok "пакеты установлены"

log "3/8 Google Chrome (движок парсера Wildberries)…"
if ! command -v google-chrome >/dev/null 2>&1; then
  install -d -m 0755 /usr/share/keyrings
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
  apt_get update -y
  apt_get install -y google-chrome-stable
fi
ok "Chrome: $(google-chrome --version 2>/dev/null || echo установлен)"

log "4/8 Пользователь $SVC_USER и права на файлы…"
id "$SVC_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$SVC_USER"
chown -R "$SVC_USER:$SVC_USER" "$BASE"
[ -f "$BASE/site/config.json" ] && chmod 600 "$BASE/site/config.json"
ok "права выставлены (config.json закрыт от чужих)"

log "5/8 Python-окружение и зависимости (пара минут)…"
[ -d "$BASE/venv" ] || sudo -u "$SVC_USER" python3 -m venv "$BASE/venv"
sudo -u "$SVC_USER" "$BASE/venv/bin/pip" install --upgrade pip >/dev/null
sudo -u "$SVC_USER" "$BASE/venv/bin/pip" install -r "$BASE/site/requirements.txt"
ok "зависимости установлены"

log "6/8 Автозапуск (systemd) + почта…"
cp "$BASE/deploy/kartochka.service" /etc/systemd/system/kartochka.service
printf "  Пароль почтового ящика reg.ru (Enter — пропустить, настроишь позже): "
read -rs SMTP_PW || true
echo
if [ -n "${SMTP_PW:-}" ]; then
  install -d -m 0700 /etc/systemd/system/kartochka.service.d
  printf '[Service]\nEnvironment=SMTP_PASSWORD=%s\n' "$SMTP_PW" > /etc/systemd/system/kartochka.service.d/secret.conf
  chmod 600 /etc/systemd/system/kartochka.service.d/secret.conf
  ok "пароль почты сохранён отдельно (не в коде проекта)"
else
  warn "почта без пароля — письма пока только в лог (включишь позже)"
fi
systemctl daemon-reload
systemctl enable kartochka >/dev/null 2>&1 || true
systemctl restart kartochka
sleep 4
if systemctl is-active --quiet kartochka; then
  ok "сервис запущен"
else
  journalctl -u kartochka -n 40 --no-pager || true
  die "сервис не поднялся — смотри лог выше"
fi

log "7/8 nginx (реверс-прокси на 127.0.0.1:8000)…"
cp "$BASE/deploy/nginx-kartochka.conf" /etc/nginx/sites-available/kartochka
ln -sf /etc/nginx/sites-available/kartochka /etc/nginx/sites-enabled/kartochka
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
ok "nginx настроен"

log "8/8 Firewall…"
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 'Nginx Full' >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true
ok "firewall включён (SSH + HTTP/HTTPS)"

IP="$(curl -s --max-time 5 https://api.ipify.org || true)"
[ -n "$IP" ] || IP="$(hostname -I | awk '{print $1}')"

echo
echo "================================================================"
echo " ГОТОВО. «карточка Ai» поднята и работает на этом сервере."
echo "================================================================"
echo
echo " Сайт уже отвечает по IP (http). Осталось 2 РУЧНЫХ шага:"
echo
echo " 1) reg.ru -> домен example.com -> DNS -> две A-записи:"
echo "        @     ->  $IP"
echo "        www   ->  $IP"
echo "    Проверка через 10-60 мин:   ping $DOMAIN_PUNY"
echo
echo " 2) Когда ping домена отвечает адресом $IP — включи HTTPS:"
echo "        sudo apt -y install certbot python3-certbot-nginx"
echo "        sudo certbot --nginx -d $DOMAIN_PUNY -d www.$DOMAIN_PUNY"
echo
echo " После этого сайт открывается по:  https://example.com"
echo
echo " Управление сервером:"
echo "   лог в реальном времени:   journalctl -u kartochka -f"
echo "   перезапуск:               systemctl restart kartochka"
echo "   статус:                   systemctl status kartochka"
echo "================================================================"

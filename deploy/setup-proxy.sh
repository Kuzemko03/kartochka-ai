#!/usr/bin/env bash
# Личный HTTP-прокси (tinyproxy) для доступа к OpenAI/Anthropic с РФ-сервера.
# Ставить на ЗАГРАНИЧНЫЙ VPS (Амстердам/Франкфурт), Ubuntu 24.04:
#   sudo bash setup-proxy.sh
# Можно задать параметры заранее:  ALLOWED_IP=.. PROXY_USER=.. PROXY_PASS=.. sudo -E bash setup-proxy.sh
set -Eeuo pipefail

c(){ printf '\033[%sm' "$1"; }
log(){ echo; echo "$(c '1;36')==> $*$(c 0)"; }
ok(){ echo "  $(c '1;32')OK$(c 0) $*"; }
die(){ echo; echo "$(c '1;31')ОШИБКА: $*$(c 0)" >&2; exit 1; }
trap 'die "строка $LINENO — прочитай вывод выше и запусти снова (скрипт идемпотентный)"' ERR
[ "$(id -u)" -eq 0 ] || die "нужны root-права: sudo bash $0"

ALLOWED_IP="${ALLOWED_IP:-}"
PROXY_USER="${PROXY_USER:-}"
PROXY_PASS="${PROXY_PASS:-}"
PORT="${PORT:-3128}"

[ -n "$ALLOWED_IP" ] || { printf "  IP основного сервера (кому разрешить прокси) [203.0.113.10]: "; read -r ALLOWED_IP; ALLOWED_IP="${ALLOWED_IP:-203.0.113.10}"; }
[ -n "$PROXY_USER" ]  || { printf "  Логин прокси [kartochka]: "; read -r PROXY_USER; PROXY_USER="${PROXY_USER:-kartochka}"; }
[ -n "$PROXY_PASS" ]  || { printf "  Пароль прокси (придумай сложный): "; read -rs PROXY_PASS; echo; }
[ -n "$PROXY_PASS" ]  || die "пароль не может быть пустым"

export DEBIAN_FRONTEND=noninteractive

log "1/4 Установка tinyproxy…"
apt-get update -y
apt-get install -y tinyproxy ufw curl
ok "tinyproxy установлен"

log "2/4 Конфигурация (порт $PORT, доступ только с $ALLOWED_IP + логин/пароль)…"
cat > /etc/tinyproxy/tinyproxy.conf <<EOF
User tinyproxy
Group tinyproxy
Port $PORT
Timeout 600
# Доступ ТОЛЬКО с твоего основного сервера…
Allow $ALLOWED_IP
# …и только с верным логином/паролем
BasicAuth $PROXY_USER $PROXY_PASS
# HTTPS-туннель к API нейронок (api.openai.com / api.anthropic.com и т.п.)
ConnectPort 443
ConnectPort 563
LogLevel Info
EOF
ok "конфиг записан"

log "3/4 Firewall (SSH + порт $PORT только с $ALLOWED_IP)…"
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow from "$ALLOWED_IP" to any port "$PORT" proto tcp >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true
ok "firewall настроен"

log "4/4 Запуск…"
systemctl enable tinyproxy >/dev/null 2>&1 || true
systemctl restart tinyproxy
sleep 2
if systemctl is-active --quiet tinyproxy; then ok "прокси работает"; else journalctl -u tinyproxy -n 20 --no-pager || true; die "не запустился"; fi

IP="$(curl -s --max-time 5 https://api.ipify.org || true)"
[ -n "$IP" ] || IP="$(hostname -I | awk '{print $1}')"

echo
echo "================================================================"
echo " ГОТОВО. Личный HTTP-прокси поднят."
echo "================================================================"
echo
echo " URL прокси (впиши в config.json основного сервера):"
echo
echo "   http://$PROXY_USER:$PROXY_PASS@$IP:$PORT"
echo
echo " В /opt/kartochka/site/config.json → блок ai_proxy:"
echo "   \"ai_proxy\": { \"enabled\": true, \"url\": \"http://$PROXY_USER:$PROXY_PASS@$IP:$PORT\" }"
echo
echo " Проверка С ОСНОВНОГО сервера (должен вернуть $IP — значит трафик идёт через прокси):"
echo "   curl -x http://$PROXY_USER:$PROXY_PASS@$IP:$PORT https://api.ipify.org"
echo "================================================================"

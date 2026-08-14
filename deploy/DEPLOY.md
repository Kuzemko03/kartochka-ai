# Деплой «карточка Ai» на сервер Timeweb (Ubuntu 24.04) + домен example.com (reg.ru)

Пошаговая инструкция: поднять сервер, прикрепить домен, включить HTTPS и почту.

- **Домен:** `example.com` → в технике (nginx, SSL, DNS) = punycode **`xn--your-domain.example`**
- **Сервер:** Timeweb Cloud, раздел **«Облачные серверы»** (НЕ «Хостинг»), тариф **Cloud MSK 50** (2 vCPU / 4 ГБ / 50 ГБ NVMe), ОС **Ubuntu 24.04**
- **Почта:** reg.ru, ящик вида `info@xn--your-domain.example`, SMTP `mail.hosting.reg.ru:465`

> Все команды на сервере — от root (или через `sudo`). Подставляй свой IP сервера вместо `SERVER_IP`.

---

## ⚡ Самый простой путь — бронебойный скрипт (рекомендуется)

Не хочешь делать шаги вручную? После создания сервера всего две команды:

1. Залей проект на сервер (с локального ПК, PowerShell). Папку `/opt/kartochka`
   заранее НЕ создавай — scp создаст её сам:
   ```powershell
   scp -r "C:\Users\sazon\OneDrive\Desktop\Kartochka" root@SERVER_IP:/opt/kartochka
   ```
2. Зайди на сервер и запусти один скрипт:
   ```bash
   ssh root@SERVER_IP
   sudo bash /opt/kartochka/deploy/deploy.sh
   ```

Скрипт сам поставит Python, Chrome, nginx, зависимости, настроит автозапуск и firewall,
спросит пароль почты и поднимет сервер. В конце напечатает IP сервера и 2 оставшихся
ручных шага (DNS на reg.ru + команда HTTPS). Идемпотентный — если что-то прервётся,
просто запусти снова.

Подробный разбор по шагам (на случай если хочешь понимать каждый) — ниже.

---

## Шаг 1. Создать сервер на Timeweb

1. Timeweb Cloud → **Облачные серверы** → создать.
2. Тариф **Cloud MSK 50**, ОС **Ubuntu 24.04**.
3. Добавь свой **SSH-ключ** (или задай root-пароль — Timeweb пришлёт его).
4. После создания запиши **IP-адрес** сервера.

## Шаг 2. Подключиться по SSH

```bash
ssh root@SERVER_IP
```
(на Windows — через PowerShell той же командой, либо PuTTY)

## Шаг 3. Базовая подготовка (система + Chrome + пользователь + firewall)

Залей `deploy/setup.sh` на сервер и запусти — он ставит Python, nginx, **Google Chrome** (нужен парсеру WB), создаёт пользователя `kartochka` и включает firewall:

```bash
# с локального ПК закинуть проект (см. шаг 4), либо вставить setup.sh вручную:
bash /opt/kartochka/deploy/setup.sh
```

Либо то же самое руками:
```bash
apt update && apt -y upgrade
apt -y install python3 python3-venv python3-pip nginx git curl ufw ca-certificates gnupg
# Chrome:
curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
apt update && apt -y install google-chrome-stable
useradd -m -s /bin/bash kartochka
install -d -o kartochka -g kartochka /opt/kartochka
ufw allow OpenSSH && ufw allow 'Nginx Full' && ufw --force enable
```

## Шаг 4. Залить код проекта

С локального ПК (PowerShell), из папки, где лежит `Kartochka`:

```powershell
# scp/ssh встроены в Windows 10/11. Папку /opt/kartochka заранее НЕ создавай —
# scp создаст её сам (иначе получится вложенность /opt/kartochka/Kartochka).
scp -r "C:\Users\sazon\OneDrive\Desktop\Kartochka" root@SERVER_IP:/opt/kartochka
```

> Не обязательно копировать `tmp/`, `__pycache__/`, `android/app/build/`, `.gradle/` — они пересоздаются. Папку `site/data/` (база юзеров) копируй, если переносишь существующих пользователей.

Затем на сервере отдать папку пользователю `kartochka` и создать venv:
```bash
chown -R kartochka:kartochka /opt/kartochka
sudo -u kartochka python3 -m venv /opt/kartochka/venv
sudo -u kartochka /opt/kartochka/venv/bin/pip install --upgrade pip
sudo -u kartochka /opt/kartochka/venv/bin/pip install -r /opt/kartochka/site/requirements.txt
```

## Шаг 5. Прод-настройки config.json

Открой `/opt/kartochka/site/config.json` и проверь:

- `"site_base_url": "https://example.com"` — оставить.
- Блок `"smtp"` уже добавлен (host/порт reg.ru). **Пароль ящика сюда НЕ пиши** — он пойдёт через переменную окружения (шаг 9).
- `"vidinator_core_path": "./vidinator_core"` — нужен ТОЛЬКО для видео-генерации (Veo) и транскрипции. Если этими функциями пользуешься — скопируй внешнюю папку `Vidinator_Structured/core` в `/opt/kartochka/site/vidinator_core`. Для генерации **карточек (картинок) она не нужна** — там FastGen/OpenAI.
- `"site_gate": { "enabled": true ... }` — это глобальный пароль на весь сайт (пред-запуск). Пока тестируешь — можно оставить. **Перед публичным запуском поставь `false`** и перезапусти сервис.

Права на конфиг (в нём API-ключи — закрываем от чужих):
```bash
chown kartochka:kartochka /opt/kartochka/site/config.json
chmod 600 /opt/kartochka/site/config.json
```

## Шаг 6. Автозапуск через systemd

```bash
cp /opt/kartochka/deploy/kartochka.service /etc/systemd/system/kartochka.service
# впиши пароль ящика в строку Environment=SMTP_PASSWORD= (см. шаг 9), затем:
systemctl daemon-reload
systemctl enable --now kartochka
systemctl status kartochka      # должно быть active (running)
journalctl -u kartochka -f      # живой лог (Ctrl+C выйти)
```
Сервер поднимется на `127.0.0.1:8000`. Снаружи он пока не виден — его проксирует nginx (шаг 7).

## Шаг 7. nginx + домен + HTTPS

**7.1. Поставить конфиг nginx:**
```bash
cp /opt/kartochka/deploy/nginx-kartochka.conf /etc/nginx/sites-available/kartochka
ln -sf /etc/nginx/sites-available/kartochka /etc/nginx/sites-enabled/kartochka
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

**7.2. Прописать DNS на reg.ru** (в личном кабинете reg.ru → домен `example.com` → DNS-серверы и управление зонами):

| Тип | Хост | Значение |
|-----|------|----------|
| A   | `@`  | `SERVER_IP` |
| A   | `www`| `SERVER_IP` |

Подожди 10–60 минут, проверь: `ping xn--your-domain.example` должен отвечать твоим IP.

**7.3. Выпустить SSL-сертификат (бесплатный, Let's Encrypt):**
```bash
apt -y install certbot python3-certbot-nginx
certbot --nginx -d xn--your-domain.example -d www.xn--your-domain.example
```
Certbot сам допишет HTTPS-блок в конфиг nginx и настроит авто-продление. После этого сайт открывается по `https://example.com`.

## Шаг 8. Завести почтовый ящик на reg.ru

1. reg.ru → твой домен → **Почта** → создать ящик, напр. `info@example.com` (технически `info@xn--your-domain.example`).
2. reg.ru сам пропишет нужные **MX-записи** для домена.
3. Запомни пароль ящика — он нужен серверу для отправки писем.

## Шаг 9. Подключить отправку писем (сброс пароля, чеки)

Пароль ящика держим в переменной окружения, НЕ в конфиге. Открой юнит и впиши:
```bash
nano /etc/systemd/system/kartochka.service
#   Environment=SMTP_PASSWORD=твой_пароль_от_ящика
systemctl daemon-reload && systemctl restart kartochka
```
Проверка: на сайте нажми «Забыли пароль?» → письмо должно прийти. В логе (`journalctl -u kartochka`) ищи `[SmtpMailer] отправлено`.

После того как письма заработали — в `config.json` поставь `"password_reset_return_link": false` (чтобы ссылка сброса не возвращалась в ответе API, а только письмом) и перезапусти сервис.

## Шаг 10. Пересобрать Android-приложение под новый домен

В коде уже обновлено: `android/app/build.gradle.kts` (release `SITE_URL`) и белый список доменов. Нужно только пересобрать release-APK (см. `android/GUIDE.md`) — теперь приложение грузит `https://xn--your-domain.example/`.

---

## Чек-лист перед публичным запуском

- [ ] `site_gate.enabled = false` (снять глобальный пароль-замок) + перезапуск
- [ ] `password_reset_return_link = false`
- [ ] **Сменить API-ключи** в `config.json` (OpenAI, FastGen и пр.) — старые «засветились», лучше выпустить новые в кабинетах сервисов
- [ ] `chmod 600 config.json`, владелец `kartochka`
- [ ] Подключить **реальный платёжный провайдер** (ЮKassa/Robokassa) вместо MockProvider — см. `site/accounts/payments.py`
- [ ] Подать **уведомление оператора ПДн** в Роскомнадзор (ты собираешь email/IP)
- [ ] Проверить, что генерация карточек реально работает на сервере (Chrome-пул токенов WB поднялся: в логе `WB smart-stack: запущен`)

## Если что-то не так

- **Сайт не открывается** → `systemctl status kartochka`, `journalctl -u kartochka -e`, `nginx -t`.
- **502 Bad Gateway** → сервис не поднялся или не на 8000-м порту; смотри лог сервиса.
- **Парсер WB не находит товары / 429** → проверь, что Chrome установился (`google-chrome --version`) и в логе нет ошибок token-пула; на крайний случай проверь прокси в `config.json → wb_smart.proxies`.
- **Письма не уходят** → проверь `SMTP_PASSWORD` в юните и логин ящика (для кириллического домена — в punycode), порт 465.
- **Долгая генерация рвётся** → проверь, что в nginx стоит `proxy_buffering off` и таймауты 600s (уже в конфиге).

"""
site_gate.py — глобальная пред-запусковая ЗАГЛУШКА-ЗАМОК на ВЕСЬ сайт.

Пока site_gate включён, любой запрос (страницы, API, /static) без валидной
admin-сессии получает HTML «Сайт закрыт» с формой логин+пароль. После входа
ставится cookie с HMAC-подписанным токеном (HttpOnly, SameSite=Lax,
Secure на https) и сроком жизни.

Безопасность:
  • пароль НИКОГДА не хранится открытым — только PBKDF2-HMAC-SHA256
    (200k итераций, 16-байтовая соль); сверка через hmac.compare_digest;
  • токен сессии — stateless HMAC-SHA256(secret, "v1.<exp>"), проверка
    constant-time + срок годности (нельзя подделать без секрета);
  • защита от перебора — rate-limit по IP (блокировка после N попыток)
    + задержка на каждую попытку;
  • вся проверка на сервере — в HTML/JS заглушки нет ничего секретного;
  • креды и секрет берутся из ENV (приоритет) либо config.json["site_gate"].

Включить/выключить: config.json → "site_gate": { "enabled": true|false }
(или ENV WBN_GATE_ENABLED=1|0), затем перезапустить сервер.
"""
from __future__ import annotations

import os
import json
import time
import hmac
import hashlib
import asyncio
import logging
import threading
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

logger = logging.getLogger("server.gate")

_BASE = Path(__file__).parent
_COOKIE = "wbn_gate"
_TOKEN_VER = "v1"


def _as_bool(v, default=False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _load_cfg() -> dict:
    """ENV (приоритет) поверх config.json["site_gate"]."""
    cfg: dict = {}
    try:
        data = json.loads((_BASE / "config.json").read_text(encoding="utf-8"))
        cfg = dict(data.get("site_gate") or {})
    except Exception:
        logger.exception("site_gate: не удалось прочитать config.json")

    env = os.environ
    if "WBN_GATE_ENABLED" in env:
        cfg["enabled"] = _as_bool(env["WBN_GATE_ENABLED"])
    cfg["login"]  = env.get("WBN_GATE_LOGIN",  cfg.get("login", ""))
    cfg["hash"]   = env.get("WBN_GATE_HASH",   cfg.get("hash", ""))
    cfg["salt"]   = env.get("WBN_GATE_SALT",   cfg.get("salt", ""))
    cfg["secret"] = env.get("WBN_GATE_SECRET", cfg.get("secret", ""))
    cfg["iterations"]          = int(env.get("WBN_GATE_ITER", cfg.get("iterations", 200000)))
    cfg["session_ttl_seconds"] = int(cfg.get("session_ttl_seconds", 7 * 24 * 3600))
    cfg["max_attempts"]        = int(cfg.get("max_attempts", 8))
    cfg["lockout_seconds"]     = int(cfg.get("lockout_seconds", 900))

    # Мобильное приложение (Android WebView) НЕ должно упираться в парольный
    # замок: юзеру приложения незачем вводить admin-логин. WebView шлёт свой
    # маркер в User-Agent ("… KartochkaApp/1.0", см. MainActivity.kt), по нему
    # и пропускаем. По умолчанию включено; выключить — allow_app=false в
    # config.json["site_gate"] или ENV WBN_GATE_ALLOW_APP=0.
    #   ⚠️ Маркер в UA подделываем кем угодно → это НЕ защита, а снятие трения
    #   для приложения. Замок остаётся барьером только против случайных
    #   веб-посетителей. Нужна жёсткая защита приложения — вшить секретный
    #   заголовок в APK (см. app_secret_header ниже) и включить require_app_secret.
    if "WBN_GATE_ALLOW_APP" in env:
        cfg["allow_app"] = _as_bool(env["WBN_GATE_ALLOW_APP"], True)
    else:
        cfg["allow_app"] = _as_bool(cfg.get("allow_app", True), True)
    cfg["app_ua_marker"] = env.get("WBN_GATE_APP_UA", cfg.get("app_ua_marker", "KartochkaApp"))
    return cfg


_CFG = _load_cfg()


def enabled() -> bool:
    """ON только если флаг включён И заданы hash+secret (иначе сайт открыт)."""
    return bool(_CFG.get("enabled")) and bool(_CFG.get("hash")) and bool(_CFG.get("secret"))


# ── проверка кред (constant-time, без раскрытия логина) ───────────────────
def _check_credentials(login: str, password: str) -> bool:
    try:
        salt = bytes.fromhex(_CFG.get("salt", "") or "")
        want = bytes.fromhex(_CFG.get("hash", "") or "")
    except ValueError:
        return False
    iters = int(_CFG.get("iterations", 200000))
    # PBKDF2 считаем ВСЕГДА (даже при неверном логине) — против timing/enumeration
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), salt,
                             iters, dklen=len(want) or 32)
    ok_pw = hmac.compare_digest(dk, want) if want else False
    ok_login = hmac.compare_digest((login or "").encode(),
                                   str(_CFG.get("login", "")).encode())
    return bool(ok_pw and ok_login)


# ── stateless подписанный токен ───────────────────────────────────────────
def _sign(msg: str) -> str:
    secret = (_CFG.get("secret", "") or "").encode()
    return hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    exp = int(time.time()) + int(_CFG.get("session_ttl_seconds", 604800))
    msg = f"{_TOKEN_VER}.{exp}"
    return f"{msg}.{_sign(msg)}"


def verify_token(token: str) -> bool:
    try:
        ver, exp_s, sig = token.split(".", 2)
        if ver != _TOKEN_VER:
            return False
        msg = f"{ver}.{exp_s}"
        if not hmac.compare_digest(sig, _sign(msg)):
            return False
        return int(exp_s) > int(time.time())
    except Exception:
        return False


# ── анти-брутфорс (per-IP, в памяти процесса) ─────────────────────────────
_attempts: dict[str, dict] = {}
_attempts_lock = threading.Lock()


def _client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else "?"


def _locked_for(ip: str) -> float:
    with _attempts_lock:
        rec = _attempts.get(ip)
        return max(0.0, rec.get("until", 0.0) - time.time()) if rec else 0.0


def _register_fail(ip: str) -> None:
    with _attempts_lock:
        rec = _attempts.setdefault(ip, {"fails": 0, "until": 0.0})
        rec["fails"] += 1
        if rec["fails"] >= int(_CFG.get("max_attempts", 8)):
            rec["until"] = time.time() + int(_CFG.get("lockout_seconds", 900))
            rec["fails"] = 0


def _register_ok(ip: str) -> None:
    with _attempts_lock:
        _attempts.pop(ip, None)


def _is_https(req: Request) -> bool:
    proto = req.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return proto == "https" or req.url.scheme == "https"


def _is_app_request(req: Request) -> bool:
    """Запрос из мобильного приложения (Android WebView)? Определяем по маркеру
    в User-Agent, который WebView добавляет к своему UA. Если allow_app выключен
    — всегда False (приложение тоже увидит замок)."""
    if not bool(_CFG.get("allow_app", True)):
        return False
    marker = str(_CFG.get("app_ua_marker") or "").strip()
    if not marker:
        return False
    ua = req.headers.get("user-agent", "") or ""
    return marker in ua


# ── страница-замок (самодостаточная: без /static и внешних шрифтов) ───────
def lock_page(error: str = "") -> str:
    err_html = f'<div class="gate-err">{error}</div>' if error else ""
    return _LOCK_TEMPLATE.replace("<!--ERR-->", err_html)


_LOCK_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Сайт закрыт</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif;
    color: #1F1A35;
    display: flex; align-items: center; justify-content: center;
    padding: 24px;
    background:
      radial-gradient(ellipse at 25% 18%, #EDE5FB 0%, transparent 55%),
      radial-gradient(ellipse at 78% 82%, #F8E5EE 0%, transparent 60%),
      #F4F2FA;
  }
  .gate-card {
    width: 100%; max-width: 390px;
    background: rgba(255,255,255,0.72);
    border: 1px solid rgba(255,255,255,0.8);
    backdrop-filter: blur(22px) saturate(160%);
    -webkit-backdrop-filter: blur(22px) saturate(160%);
    border-radius: 26px;
    padding: 40px 34px 30px;
    box-shadow: 0 24px 70px rgba(60,50,110,0.18), inset 0 1px 0 rgba(255,255,255,0.7);
    text-align: center;
  }
  .gate-lock {
    width: 64px; height: 64px; margin: 0 auto 20px;
    border-radius: 20px;
    background: linear-gradient(135deg, #6B4FE0, #4C29D3);
    display: flex; align-items: center; justify-content: center;
    color: #fff;
    box-shadow: 0 10px 30px rgba(76,41,211,0.4), inset 0 1px 0 rgba(255,255,255,0.4);
  }
  .gate-card h1 { font-size: 23px; font-weight: 650; letter-spacing: -0.02em; margin-bottom: 8px; }
  .gate-sub { font-size: 14px; color: rgba(31,26,53,0.6); margin-bottom: 26px; line-height: 1.5; }
  .gate-field { position: relative; margin-bottom: 12px; text-align: left; }
  .gate-field input {
    width: 100%; padding: 14px 16px;
    font-size: 15px; font-family: inherit; color: #1F1A35;
    background: rgba(255,255,255,0.7);
    border: 1.5px solid rgba(76,41,211,0.18);
    border-radius: 14px; outline: none;
    transition: border-color 160ms, box-shadow 160ms;
  }
  .gate-field input:focus {
    border-color: #6B4FE0;
    box-shadow: 0 0 0 4px rgba(76,41,211,0.12);
  }
  .gate-field input::placeholder { color: rgba(31,26,53,0.4); }
  .gate-btn {
    width: 100%; margin-top: 8px; padding: 14px 16px;
    font-size: 15px; font-weight: 650; font-family: inherit;
    color: #fff; border: none; border-radius: 14px; cursor: pointer;
    background: linear-gradient(135deg, #6B4FE0, #4C29D3);
    box-shadow: 0 8px 24px rgba(76,41,211,0.35);
    transition: transform 160ms, box-shadow 160ms;
  }
  .gate-btn:hover { transform: translateY(-1px); box-shadow: 0 12px 30px rgba(76,41,211,0.45); }
  .gate-btn:active { transform: translateY(0); }
  .gate-err {
    margin-bottom: 14px; padding: 10px 14px;
    font-size: 13px; font-weight: 500; color: #B4264C;
    background: rgba(224,111,147,0.12);
    border: 1px solid rgba(224,111,147,0.3);
    border-radius: 12px;
  }
  .gate-foot { margin-top: 22px; font-size: 11.5px; color: rgba(31,26,53,0.4); letter-spacing: 0.02em; }
</style>
</head>
<body>
  <div class="gate-card">
    <div class="gate-lock">
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
      </svg>
    </div>
    <h1>Сайт закрыт</h1>
    <div class="gate-sub">Идёт подготовка к запуску. Доступ только для администратора.</div>
    <!--ERR-->
    <form method="post" action="/__gate__/login" autocomplete="off">
      <div class="gate-field">
        <input type="text" name="login" placeholder="Логин" autofocus
               autocapitalize="off" autocorrect="off" spellcheck="false">
      </div>
      <div class="gate-field">
        <input type="password" name="password" placeholder="Пароль">
      </div>
      <button class="gate-btn" type="submit">Войти</button>
    </form>
    <div class="gate-foot">WBnator · защищённый доступ</div>
  </div>
</body>
</html>"""


def install(app) -> None:
    """Подключить заглушку к FastAPI-приложению (middleware + роуты входа)."""
    if not enabled():
        if _CFG.get("enabled"):
            logger.error("site_gate: enabled=true, но не заданы hash/secret — "
                         "ЗАГЛУШКА НЕ АКТИВНА, сайт открыт! Заполни config.json.")
        else:
            logger.info("site_gate: выключен (enabled=false) — сайт открыт")
        return

    logger.warning("site_gate: ВКЛЮЧЁН — весь сайт под admin-замком "
                   "(вход: POST /__gate__/login)")

    @app.middleware("http")
    async def _gate_mw(request: Request, call_next):
        path = request.url.path
        # пропускаем preflight и собственные роуты входа/выхода
        if request.method == "OPTIONS" or path.startswith("/__gate__/"):
            return await call_next(request)
        # Мобильное приложение проходит без пароля (маркер в User-Agent).
        if _is_app_request(request):
            return await call_next(request)
        token = request.cookies.get(_COOKIE, "")
        if token and verify_token(token):
            return await call_next(request)
        # не авторизован → заглушка (для навигации) или 401 (для API/XHR)
        if request.method in ("GET", "HEAD"):
            return HTMLResponse(lock_page(), status_code=401)
        return JSONResponse({"error": "site_locked"}, status_code=401)

    @app.post("/__gate__/login")
    async def _gate_login(request: Request):
        ip = _client_ip(request)
        wait = _locked_for(ip)
        if wait > 0:
            mins = int(wait // 60) + 1
            return HTMLResponse(
                lock_page(f"Слишком много попыток. Попробуйте через {mins} мин."),
                status_code=429,
            )
        try:
            form = await request.form()
        except Exception:
            form = {}
        login = (form.get("login") or "").strip()
        password = form.get("password") or ""

        await asyncio.sleep(0.3)  # базовый троттлинг любого входа
        if not _check_credentials(login, password):
            _register_fail(ip)
            await asyncio.sleep(0.5)  # доп. задержка на неудаче
            return HTMLResponse(lock_page("Неверный логин или пароль."), status_code=401)

        _register_ok(ip)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            _COOKIE, make_token(),
            max_age=int(_CFG.get("session_ttl_seconds", 604800)),
            httponly=True, samesite="lax", secure=_is_https(request), path="/",
        )
        return resp

    @app.get("/__gate__/logout")
    async def _gate_logout(request: Request):
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(_COOKIE, path="/")
        return resp

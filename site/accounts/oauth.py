"""
OAuth 2.0 social login — Google, Yandex.

Config-driven: provider client_id / client_secret are read from ``config.json``
under the ``oauth`` block. A provider with empty credentials is simply
*disabled* — its button never appears on the frontend and its endpoints return
404. So shipping this module without keys breaks nothing; the moment you paste
real credentials and restart, the buttons light up.

Flow (standard Authorization Code):
  1. /api/account/oauth/{provider}/start  → 302 to provider's consent screen.
     We mint a signed ``state`` (HMAC) that also carries the post-login ``next``
     path and bind it to a one-time httponly cookie (CSRF defence).
  2. provider redirects back to
     /api/account/oauth/{provider}/callback?code=…&state=…
  3. we verify state, exchange ``code`` → access token, fetch the user profile,
     normalise it to ``{provider, sub, email, name, avatar}`` and hand it to
     ``service.oauth_login_or_register`` which creates / links the local user.

Network calls go out **directly** (no LLM proxy) with a tight timeout. We never
trust anything from the client except the opaque ``code`` and a ``state`` we
ourselves signed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

_BASE_DIR = Path(__file__).resolve().parent.parent
_HTTP_TIMEOUT = 15            # seconds — token / userinfo calls
_STATE_TTL = 600             # signed state valid for 10 minutes


class OAuthError(Exception):
    """Raised on any OAuth failure; router maps it to a friendly redirect."""


# ── provider catalogue ──────────────────────────────────────────────────────
# Each entry is static protocol metadata; secrets come from config at runtime.
_PROVIDERS = {
    "google": {
        "label": "Google",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
        "extra_authorize": {"access_type": "online", "prompt": "select_account"},
    },
    "yandex": {
        "label": "Яндекс",
        "authorize_url": "https://oauth.yandex.ru/authorize",
        "token_url": "https://oauth.yandex.ru/token",
        "userinfo_url": "https://login.yandex.ru/info",
        # У Яндекса scope в authorize-запросе НЕОБЯЗАТЕЛЕН: пустой → выдаются ровно
        # те права, что отмечены в приложении (почта/портрет/инфо). Так исключён
        # рассинхрон «запросили право, которого у приложения нет» (invalid_scope),
        # а профиль (email/имя/аватар) парсится из ответа best-effort.
        "scope": "",
        "extra_authorize": {},
    },
}

# Brand order in the UI (Google first — most familiar to RU SMB sellers too).
PROVIDER_ORDER = ["google", "yandex"]


# ── config access ────────────────────────────────────────────────────────────
def _load_cfg() -> dict:
    try:
        return json.loads((_BASE_DIR / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _provider_creds(provider: str) -> Optional[dict]:
    """Return {client_id, client_secret} if the provider is fully configured.

    ENV overrides config.json (секрет лучше не держать в файле):
      OAUTH_GOOGLE_CLIENT_ID / OAUTH_GOOGLE_SECRET, OAUTH_YANDEX_*.
    """
    import os
    cfg = _load_cfg()
    block = (cfg.get("oauth") or {}).get(provider) or {}
    env_id = os.environ.get(f"OAUTH_{provider.upper()}_CLIENT_ID")
    env_secret = os.environ.get(f"OAUTH_{provider.upper()}_SECRET")
    cid = (env_id or block.get("client_id") or "").strip()
    secret = (env_secret or block.get("client_secret") or "").strip()
    if not cid or not secret:
        return None
    return {"client_id": cid, "client_secret": secret}


def is_configured(provider: str) -> bool:
    return provider in _PROVIDERS and _provider_creds(provider) is not None


def _hide_unconfigured() -> bool:
    """Строгий режим для прода: спрятать соц-кнопки без ключей. По умолчанию
    OFF — кнопки видны ВСЕГДА (так их сразу видно в приложении/на сайте).
    Включается config.json → oauth.hide_unconfigured=true ИЛИ ENV OAUTH_HIDE_UNCONFIGURED=1."""
    import os
    env = os.environ.get("OAUTH_HIDE_UNCONFIGURED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    cfg = _load_cfg()
    return bool((cfg.get("oauth") or {}).get("hide_unconfigured"))


def enabled_providers() -> list[dict]:
    """Public list for the frontend.

    По умолчанию возвращаем ВСЕ провайдеры (кнопки видны сразу), помечая каждый
    флагом configured: настроенный → реальный OAuth, ненастроенный → фронт по
    клику подсказывает добавить ключи (без редиректа на провайдера).
    Строгий режим (oauth.hide_unconfigured=true) прячет ненастроенные."""
    hide = _hide_unconfigured()
    out = []
    for p in PROVIDER_ORDER:
        conf = is_configured(p)
        if conf or not hide:
            out.append({"id": p, "label": _PROVIDERS[p]["label"], "configured": conf})
    return out


def _site_base_url() -> str:
    cfg = _load_cfg()
    return (cfg.get("site_base_url") or "").rstrip("/")


def _idna_base(base: str) -> str:
    """Хост → punycode (IDNA). Провайдеры (Google и др.) требуют ASCII-домен в
    redirect_uri, а у нас кириллический example.com (= xn--your-domain.example).
    Без этого redirect_uri не совпал бы с зарегистрированным → invalid_redirect."""
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(base)
        host = parts.hostname
        if not host:
            return base.rstrip("/")
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except Exception:
            ascii_host = host
        netloc = ascii_host + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, "", "")).rstrip("/")
    except Exception:
        return (base or "").rstrip("/")


def redirect_uri(provider: str, request_base: Optional[str] = None) -> str:
    """Callback URL registered with the provider.

    Prefers config.site_base_url (stable, must match what you registered in the
    provider console). Falls back to the live request base for local dev. Host is
    normalised to punycode so IDN-домены совпадают с регистрацией у провайдера.
    """
    base = _site_base_url() or (request_base or "").rstrip("/")
    return f"{_idna_base(base)}/api/account/oauth/{provider}/callback"


# ── signed state (CSRF + carries `next`) ─────────────────────────────────────
def _state_key() -> bytes:
    # Ключ HMAC должен быть СЕКРЕТНЫМ и стабильным. Берём site_gate.secret (его
    # знает только сервер), иначе — другие серверные секреты (claude/fastgen ключ).
    # НЕ откатываемся на site_base_url (он публичный → атакующий подделает state).
    import os
    cfg = _load_cfg()
    secret = (
        os.environ.get("WBN_GATE_SECRET")
        or (cfg.get("site_gate") or {}).get("secret")
        or cfg.get("claude_api_key")
        or cfg.get("fastgen_api_key")
        or "wbnator-oauth-fallback-change-me"
    )
    return hashlib.sha256(("oauth-state::" + str(secret)).encode("utf-8")).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_state(provider: str, nonce: str, next_path: str, ref: Optional[str] = None,
               client: str = "web") -> str:
    # client="app" → вход начат из Android-приложения: callback вернёт сессию
    # приложению через deep-link, а не cookie+redirect (см. router.oauth_callback).
    payload = {"p": provider, "n": nonce, "x": _safe_next(next_path),
               "r": (ref or "").strip().upper()[:16],
               "c": "app" if str(client).lower() == "app" else "web",
               "t": int(time.time())}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(_state_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def parse_state(state: str) -> Optional[dict]:
    try:
        body, sig = state.split(".", 1)
        expected = _b64e(hmac.new(_state_key(), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64d(body))
        if int(time.time()) - int(payload.get("t", 0)) > _STATE_TTL:
            return None
        return payload
    except Exception:
        return None


def _safe_next(next_path: Optional[str]) -> str:
    """Only allow same-site absolute paths — blocks open-redirect via `next`.

    Strict: must start with a single '/', no backslashes anywhere (browsers treat
    '\\' as '/'), no control/whitespace chars, no scheme. Anything off → '/'.
    """
    np = (next_path or "/").strip()
    if not np.startswith("/"):
        return "/"
    if np.startswith("//") or np.startswith("/\\") or np.startswith("/%2f") or np.startswith("/%2F"):
        return "/"
    if "\\" in np or "\t" in np or "\n" in np or "\r" in np:
        return "/"
    # запрет любых управляющих символов (защита от парсер-трюков прокси/браузера)
    if any(ord(c) < 0x20 for c in np):
        return "/"
    return np[:300]


# ── authorize URL ────────────────────────────────────────────────────────────
def build_authorize_url(provider: str, state: str, request_base: Optional[str] = None) -> str:
    if not is_configured(provider):
        raise OAuthError(f"provider {provider} not configured")
    meta = _PROVIDERS[provider]
    creds = _provider_creds(provider)
    params = {
        "response_type": "code",
        "client_id": creds["client_id"],
        "redirect_uri": redirect_uri(provider, request_base),
        "state": state,
    }
    # scope шлём, только если задан (у Google он обязателен; у Яндекса пустой —
    # права берутся из настроек приложения, см. _PROVIDERS["yandex"]).
    if meta.get("scope"):
        params["scope"] = meta["scope"]
    params.update(meta.get("extra_authorize") or {})
    return f"{meta['authorize_url']}?{urlencode(params)}"


# ── code → token → normalised profile ────────────────────────────────────────
def _http_json(method: str, url: str, **kw) -> dict:
    """GET/POST → JSON с жёсткой обработкой: timeout, проверка 200, не-JSON →
    OAuthError (а не голый ValueError, который бы протёк мимо обработчиков)."""
    kw.setdefault("timeout", _HTTP_TIMEOUT)
    try:
        resp = requests.request(method, url, **kw)
    except requests.RequestException as e:
        raise OAuthError(f"request to {url} failed: {e}")
    if resp.status_code != 200:
        raise OAuthError(f"{url} → HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError:
        raise OAuthError(f"{url} → non-JSON response: {resp.text[:120]}")
    if not isinstance(data, dict):
        raise OAuthError(f"{url} → unexpected JSON shape")
    return data


def exchange_and_fetch(provider: str, code: str, request_base: Optional[str] = None) -> dict:
    """Full server-side leg. Returns {provider, sub, email, email_verified, name, avatar}."""
    if not is_configured(provider):
        raise OAuthError(f"provider {provider} not configured")
    meta = _PROVIDERS[provider]
    creds = _provider_creds(provider)
    ruri = redirect_uri(provider, request_base)

    # Google / Yandex — RFC 6749 form POST → bearer token → userinfo.
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "redirect_uri": ruri,
    }
    tok = _http_json("POST", meta["token_url"], data=token_data,
                     headers={"Accept": "application/json"})
    access_token = tok.get("access_token")
    if not access_token:
        raise OAuthError("no access_token in response")

    if provider == "google":
        ui = _http_json("GET", meta["userinfo_url"],
                        headers={"Authorization": f"Bearer {access_token}"})
        # Google отдаёт email_verified — НЕ доверяем email без него (могут быть
        # неподтверждённые Google Workspace-аккаунты → защита от account-takeover
        # через привязку по чужому email).
        email = (ui.get("email") or "").strip().lower()
        email_verified = bool(ui.get("email_verified")) and bool(email)
        return {
            "provider": "google",
            "sub": str(ui.get("sub") or ""),
            "email": email,
            "email_verified": email_verified,
            "name": (ui.get("name") or ui.get("given_name") or "").strip(),
            "avatar": ui.get("picture") or "",
        }

    if provider == "yandex":
        ui = _http_json("GET", meta["userinfo_url"], params={"format": "json"},
                        headers={"Authorization": f"OAuth {access_token}"})
        avatar = ""
        aid = ui.get("default_avatar_id")
        if aid and not ui.get("is_avatar_empty"):
            avatar = f"https://avatars.yandex.net/get-yapic/{aid}/islands-200"
        email = (ui.get("default_email")
                 or (ui.get("emails") or [None])[0] or "").strip().lower()
        return {
            "provider": "yandex",
            "sub": str(ui.get("id") or ""),
            "email": email,
            # У Яндекса default_email — это подтверждённый адрес аккаунта.
            "email_verified": bool(email),
            "name": (ui.get("real_name") or ui.get("display_name")
                     or ui.get("login") or "").strip(),
            "avatar": avatar,
        }

    raise OAuthError(f"unhandled provider {provider}")

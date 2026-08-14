"""
FastAPI router for /api/account/* — thin HTTP layer over service.py.

Identity is an httponly session cookie holding an opaque token looked up in the
``sessions`` table (no client-trusted state). All prices/credits are resolved
server-side; the client only ever sends intent (which pack, how many tokens).
"""

from __future__ import annotations

import time as _time
import threading as _threading
from typing import Optional

from fastapi import APIRouter, Request, Response, Depends, Cookie, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import service, pricing, oauth
from .service import ServiceError


# ── Auth rate-limit (per-IP sliding window) ─────────────────────────────────
# Локальный мини-лимитер для auth-эндпоинтов чтобы не тащить зависимость на
# server._rate_limit (циркулярный импорт). Цель — глушить:
#   - brute-force паролей (login/register)
#   - спам password-reset → SMTP-bombing на email-аккаунт юзера
#   - перебор reset-токенов (256-bit, но всё равно)
# Окно 60 сек, скользящее. Лимиты подобраны так, чтобы НЕ мешать живому юзеру
# (он не делает 10 login'ов за минуту), но блокировать ботов.
_AUTH_RL_LOCK = _threading.Lock()
_AUTH_RL_HITS: dict[str, list[float]] = {}

def _auth_rate_limit(scope: str, per_minute: int):
    def _dep(req: Request):
        ip = _client_ip(req) or "unknown"
        key = f"{scope}:{ip}"
        now = _time.monotonic()
        cutoff = now - 60.0
        with _AUTH_RL_LOCK:
            hits = _AUTH_RL_HITS.setdefault(key, [])
            while hits and hits[0] < cutoff:
                hits.pop(0)
            # лёгкий GC заброшенных ключей чтоб dict не рос вечно
            if len(_AUTH_RL_HITS) > 10000:
                for k in list(_AUTH_RL_HITS.keys()):
                    if not _AUTH_RL_HITS[k] or _AUTH_RL_HITS[k][-1] < cutoff:
                        _AUTH_RL_HITS.pop(k, None)
            if len(hits) >= per_minute:
                retry = max(1, int(hits[0] + 60 - now) + 1)
                raise HTTPException(
                    status_code=429,
                    detail=f"Слишком много попыток. Подожди {retry} сек.",
                    headers={"Retry-After": str(retry)},
                )
            hits.append(now)
    return _dep

router = APIRouter(prefix="/api/account", tags=["account"])

COOKIE_NAME = "wbn_session"
_COOKIE_MAX_AGE = service.SESSION_TTL_DAYS * 24 * 3600


# ── helpers ──────────────────────────────────────────────────────────────────
def _client_ip(req: Request) -> Optional[str]:
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else None


def _is_secure_request(req: Request) -> bool:
    """https? Учитываем reverse-proxy (nginx ставит X-Forwarded-Proto)."""
    xf = req.headers.get("x-forwarded-proto", "")
    if xf:
        return xf.split(",")[0].strip().lower() == "https"
    return req.url.scheme == "https"


def _set_session_cookie(resp: Response, token: str, secure: bool = False) -> None:
    # Secure только на https (иначе локальная http-разработка не получит cookie).
    resp.set_cookie(
        COOKIE_NAME, token, max_age=_COOKIE_MAX_AGE, httponly=True,
        samesite="lax", path="/", secure=secure,
    )


def current_user(wbn_session: Optional[str] = Cookie(default=None)):
    """Optional auth — returns a user Row or None."""
    return service.user_for_session(wbn_session)


def require_user(user=Depends(current_user)):
    if user is None:
        raise ServiceError("Нужно войти в аккаунт", "unauthorized", 401)
    return user


# ── request models ───────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    email: str
    password: str = Field(min_length=6, max_length=200)
    name: Optional[str] = None
    consent_pdn: bool = False
    consent_offer: bool = False
    ref_code: Optional[str] = None


class LoginIn(BaseModel):
    email: str
    password: str


class ResetReqIn(BaseModel):
    email: str


class ResetConfirmIn(BaseModel):
    token: str
    password: str = Field(min_length=6, max_length=200)


class PromoIn(BaseModel):
    code: str


class QuoteIn(BaseModel):
    cards: int


class PurchaseIn(BaseModel):
    # Единственная модель оплаты — произвольное число карточек (ползунок).
    # kind/pack_key приняты опционально только для обратной совместимости со
    # старыми вкладками; сервер всегда трактует покупку как «cards».
    cards: Optional[int] = None
    email: str
    consent_offer: bool = False
    idempotency_key: Optional[str] = None
    kind: Optional[str] = None
    pack_key: Optional[str] = None


class ConfirmIn(BaseModel):
    success: bool = True


class AccountUpdateIn(BaseModel):
    name: Optional[str] = None
    notif_email: Optional[bool] = None
    new_password: Optional[str] = None
    current_password: Optional[str] = None


class ReserveIn(BaseModel):
    card_count: int = 1


class RefundIn(BaseModel):
    charged: int = 0
    mode: str = "cards"


# ── auth ─────────────────────────────────────────────────────────────────────
@router.post("/register",
             dependencies=[Depends(_auth_rate_limit("register", 5))])
def register(body: RegisterIn, req: Request, resp: Response):
    out = service.register(
        email=body.email, password=body.password, name=body.name,
        consent_pdn=body.consent_pdn, consent_offer=body.consent_offer,
        ref_code=body.ref_code, ip=_client_ip(req),
    )
    _set_session_cookie(resp, out["session"], secure=_is_secure_request(req))
    return {"user": out["user"]}


@router.post("/login",
             dependencies=[Depends(_auth_rate_limit("login", 10))])
def login(body: LoginIn, req: Request, resp: Response):
    out = service.login(body.email, body.password, _client_ip(req))
    _set_session_cookie(resp, out["session"], secure=_is_secure_request(req))
    return {"user": out["user"]}


@router.post("/logout")
def logout(resp: Response, wbn_session: Optional[str] = Cookie(default=None)):
    service.logout(wbn_session)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/password-reset/request",
             dependencies=[Depends(_auth_rate_limit("pwreset_req", 5))])
def password_reset_request(body: ResetReqIn, req: Request):
    base = str(req.base_url).rstrip("/")
    return service.request_password_reset(body.email, _client_ip(req), base)


@router.post("/password-reset/confirm",
             dependencies=[Depends(_auth_rate_limit("pwreset_cfm", 10))])
def password_reset_confirm(body: ResetConfirmIn):
    return service.confirm_password_reset(body.token, body.password)


@router.get("/me")
def me(user=Depends(current_user)):
    if user is None:
        return {"user": None}
    return {"user": service._public_user(user)}


# ── OAuth (Google / Yandex) ───────────────────────────────────────────────────
# Кнопки на фронте появляются ТОЛЬКО для настроенных провайдеров (есть client_id
# + client_secret в config.json → oauth). Незаполненный провайдер = выключен:
# /providers его не отдаёт, /start и /callback возвращают 404. Поэтому деплой без
# ключей ничего не ломает.
_OAUTH_NONCE_COOKIE = "wbn_oauth"
_OAUTH_NONCE_MAXAGE = 600


@router.get("/oauth/providers")
def oauth_providers():
    return {"providers": oauth.enabled_providers()}


@router.get("/oauth/{provider}/start",
            dependencies=[Depends(_auth_rate_limit("oauth_start", 20))])
def oauth_start(provider: str, req: Request, next: str = "/", ref: Optional[str] = None,
                client: str = "web"):
    if not oauth.is_configured(provider):
        raise HTTPException(status_code=404, detail="Провайдер не подключён")
    import secrets as _secrets
    nonce = _secrets.token_urlsafe(16)
    # client=app → вход из Android-приложения (см. /oauth/app/finish).
    state = oauth.make_state(provider, nonce, next, ref, client)
    try:
        url = oauth.build_authorize_url(provider, state, str(req.base_url).rstrip("/"))
    except oauth.OAuthError:
        raise HTTPException(status_code=404, detail="Провайдер не подключён")
    resp = RedirectResponse(url, status_code=302)
    # nonce живёт в httponly-cookie → CSRF невозможен (атакующий не выставит
    # жертве cookie, а без совпадения nonce↔state callback отвергается).
    resp.set_cookie(_OAUTH_NONCE_COOKIE, nonce, max_age=_OAUTH_NONCE_MAXAGE,
                    httponly=True, samesite="lax", path="/api/account/oauth",
                    secure=_is_secure_request(req))
    return resp


def _oauth_fail_redirect(reason: str) -> RedirectResponse:
    base = service.site_base_url() or ""
    return RedirectResponse(f"{base}/?auth_error={reason}", status_code=302)


@router.get("/oauth/{provider}/callback")
def oauth_callback(provider: str, req: Request,
                   code: Optional[str] = None, state: Optional[str] = None,
                   error: Optional[str] = None,
                   wbn_oauth: Optional[str] = Cookie(default=None)):
    if not oauth.is_configured(provider):
        raise HTTPException(status_code=404, detail="Провайдер не подключён")
    if error or not code or not state:
        return _oauth_fail_redirect("cancelled")

    payload = oauth.parse_state(state)
    if (not payload or payload.get("p") != provider
            or not wbn_oauth or payload.get("n") != wbn_oauth):
        return _oauth_fail_redirect("state")

    try:
        profile = oauth.exchange_and_fetch(provider, code, str(req.base_url).rstrip("/"))
    except oauth.OAuthError:
        return _oauth_fail_redirect("exchange")
    except Exception:
        return _oauth_fail_redirect("exchange")

    if not profile.get("sub"):
        return _oauth_fail_redirect("noprofile")

    try:
        out = service.oauth_login_or_register(
            provider=profile["provider"], sub=profile["sub"],
            email=profile.get("email"), name=profile.get("name"),
            avatar=profile.get("avatar"), ref_code=payload.get("r") or None,
            ip=_client_ip(req), email_verified=bool(profile.get("email_verified")))
    except ServiceError:
        return _oauth_fail_redirect("account")

    next_path = oauth._safe_next(payload.get("x") or "/")

    # Вход из Android-приложения: callback выполняется во ВНЕШНЕМ браузере, ставить
    # cookie там бессмысленно. Кладём сессию в одноразовый код и возвращаем её в
    # приложение через deep-link kartochka://oauth?code=... (см. /oauth/app/finish).
    if payload.get("c") == "app":
        try:
            code_h = service.create_oauth_handoff(out["session"], next_path)
        except ServiceError:
            return _oauth_fail_redirect("account")
        return _oauth_app_return_page(code_h)

    base = service.site_base_url() or ""
    resp = RedirectResponse(f"{base}{next_path}", status_code=302)
    _set_session_cookie(resp, out["session"], secure=_is_secure_request(req))
    resp.delete_cookie(_OAUTH_NONCE_COOKIE, path="/api/account/oauth")
    return resp


# Deep-link схема приложения (совпадает с intent-filter в AndroidManifest.xml).
_APP_OAUTH_SCHEME = "kartochka://oauth"


def _oauth_app_return_page(code: str) -> HTMLResponse:
    """Мини-страница во внешнем браузере: сразу уводит обратно в приложение по
    deep-link kartochka://oauth?code=..., с кнопкой-фолбэком на случай, если
    авто-редирект на кастомную схему заблокирован."""
    deep = f"{_APP_OAUTH_SCHEME}?code={code}"   # code — url-safe токен, экранирование не нужно
    html = (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Возврат в приложение…</title><style>"
        "html,body{height:100%;margin:0}body{font-family:-apple-system,system-ui,'Segoe UI',Roboto,sans-serif;"
        "background:#F4F2FA;color:#1F1A35;display:flex;align-items:center;justify-content:center;padding:24px}"
        ".c{text-align:center;max-width:340px}.s{width:42px;height:42px;margin:0 auto 18px;"
        "border:3px solid rgba(76,41,211,.25);border-top-color:#4C29D3;border-radius:50%;animation:r .8s linear infinite}"
        "@keyframes r{to{transform:rotate(360deg)}}a.b{display:inline-block;margin-top:18px;padding:13px 22px;"
        "background:linear-gradient(135deg,#6B4FE0,#4C29D3);color:#fff;border-radius:13px;text-decoration:none;font-weight:700}"
        "</style></head><body><div class=\"c\"><div class=\"s\"></div><div>Завершаем вход…</div>"
        f"<a class=\"b\" href=\"{deep}\">Вернуться в приложение</a></div>"
        f"<script>location.replace(\"{deep}\");</script></body></html>"
    )
    return HTMLResponse(html)


@router.get("/oauth/app/finish")
def oauth_app_finish(req: Request, code: Optional[str] = None):
    """Финал входа в приложении: WebView грузит этот URL по deep-link. Обмениваем
    одноразовый код на сессию и ставим cookie ИМЕННО в WebView, затем редиректим."""
    data = service.consume_oauth_handoff(code or "")
    if not data:
        return _oauth_fail_redirect("state")
    next_path = oauth._safe_next(data.get("next_path") or "/")
    base = service.site_base_url() or ""
    resp = RedirectResponse(f"{base}{next_path}", status_code=302)
    _set_session_cookie(resp, data["session_token"], secure=_is_secure_request(req))
    return resp


# ── pricing (public) ─────────────────────────────────────────────────────────
@router.get("/pricing")
def get_pricing():
    data = pricing.public_pricing()
    data["site_base_url"] = service.site_base_url()
    return data


@router.post("/quote")
def quote(body: QuoteIn):
    return pricing.quote_cards(body.cards)


# ── promo ────────────────────────────────────────────────────────────────────
@router.post("/promo")
def promo(body: PromoIn, user=Depends(require_user)):
    return service.apply_promo(user["id"], body.code)


# ── purchases ────────────────────────────────────────────────────────────────
@router.post("/purchase")
def purchase(body: PurchaseIn, req: Request, user=Depends(require_user)):
    base = str(req.base_url).rstrip("/")
    return service.create_purchase(
        user_id=user["id"], cards=body.cards, email=body.email,
        idempotency_key=body.idempotency_key,
        return_url=f"{base}/profile",
    )


def _require_own_purchase(purchase_id: str, user) -> None:
    owner = service.purchase_owner(purchase_id)
    if owner is None or owner != user["id"]:
        raise ServiceError("Покупка не найдена", "not_found", 404)


@router.post("/purchase/{purchase_id}/confirm")
def confirm(purchase_id: str, body: ConfirmIn, user=Depends(require_user)):
    _require_own_purchase(purchase_id, user)
    return service.confirm_purchase(purchase_id, success=body.success)


@router.get("/purchase/{purchase_id}")
def purchase_status(purchase_id: str, user=Depends(require_user)):
    _require_own_purchase(purchase_id, user)
    out = service.get_purchase(purchase_id)
    if not out:
        raise ServiceError("Покупка не найдена", "not_found", 404)
    return out


# ── history / referral / account ─────────────────────────────────────────────
@router.get("/history")
def history(user=Depends(require_user)):
    return service.history(user["id"])


@router.get("/referral")
def referral(user=Depends(require_user)):
    return service.referral_stats(user["id"])


@router.post("/account",
             dependencies=[Depends(_auth_rate_limit("acc_update", 20))])
def account_update(body: AccountUpdateIn, user=Depends(require_user)):
    return service.update_account(
        user["id"], name=body.name, notif_email=body.notif_email,
        new_password=body.new_password, current_password=body.current_password)


@router.delete("/account",
               dependencies=[Depends(_auth_rate_limit("acc_delete", 3))])
def account_delete(resp: Response, user=Depends(require_user),
                   wbn_session: Optional[str] = Cookie(default=None)):
    service.delete_account(user["id"])
    resp.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


# ── generation spend (server-authoritative) ──────────────────────────────────
# ВНИМАНИЕ: публичных HTTP-эндпоинтов reserve/refund НЕТ намеренно.
# Списание/возврат бесплатной (пробной) карточки делает только сервер изнутри
# (server.py: reserve_generation при старте джобы, refund_generation при 0 файлов).
# Если их выставить наружу — юзер сможет дёргать refund(mode="free") и бесконечно
# сбрасывать свою единственную бесплатную генерацию. Поэтому это in-process API,
# а не REST. Модели ReserveIn/RefundIn оставлены для внутренней типизации.

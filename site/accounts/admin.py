"""
Admin API — /api/admin/* — gated by ``is_admin``.

Gives an operator: dashboard metrics, user search + management (balance
adjustments, grant admin, delete), promo-code management, recent purchases,
generation monitoring, and editing of provider API keys (config.json). Admins
are configured via ``config.json:admin_emails`` (synced to the ``is_admin`` flag
on register/login).
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from . import db, service
from .router import current_user
from .service import ServiceError

admin_router = APIRouter(prefix="/api/admin", tags=["admin"])
_BASE = Path(__file__).resolve().parent.parent
_CONFIG = _BASE / "config.json"


def require_admin(req: Request, user=Depends(current_user)):
    # Скрываем админку: для не-админов отдаём 404 (как будто эндпоинта нет),
    # чтобы не подтверждать его существование. + опциональный IP-allowlist.
    if user is None or not user["is_admin"]:
        raise ServiceError("Not found", "not_found", 404)
    allow = service.admin_ip_allowlist()
    if allow:
        fwd = req.headers.get("x-forwarded-for")
        ip = fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "")
        if ip not in allow:
            raise ServiceError("Not found", "not_found", 404)
    return user


def _today_iso() -> str:
    return dt.date.today().isoformat()


# ── dashboard ────────────────────────────────────────────────────────────────
@admin_router.get("/stats")
def stats(user=Depends(require_admin)):
    today = _today_iso()
    q1 = db.query_one
    users_total = q1("SELECT COUNT(*) c FROM users")["c"]
    users_today = q1("SELECT COUNT(*) c FROM users WHERE substr(created_at,1,10)=?", (today,))["c"]
    paid = q1("""SELECT COUNT(*) c, COALESCE(SUM(amount_kopecks),0) s, COALESCE(SUM(cards),0) cd
                 FROM purchases WHERE status='paid'""")
    paid_today = q1("""SELECT COALESCE(SUM(amount_kopecks),0) s FROM purchases
                       WHERE status='paid' AND substr(paid_at,1,10)=?""", (today,))
    gens_total = q1("SELECT COUNT(*) c FROM generations")["c"]
    gens_today = q1("SELECT COUNT(*) c FROM generations WHERE substr(created_at,1,10)=?", (today,))["c"]
    cards_spent = q1("""SELECT COALESCE(SUM(-delta),0) s FROM card_ledger
                        WHERE reason='spend' AND delta<0""")["s"]
    promo_used = q1("SELECT COUNT(*) c FROM promo_redemptions")["c"]
    referrals = q1("SELECT COUNT(*) c FROM referrals WHERE status='rewarded'")["c"]
    balance_outstanding = q1("SELECT COALESCE(SUM(balance_cards),0) s FROM users")["s"]
    return {
        "users_total": users_total, "users_today": users_today,
        "revenue_rub": round(paid["s"] / 100, 2), "revenue_today_rub": round(paid_today["s"] / 100, 2),
        "purchases_paid": paid["c"], "cards_sold": paid["cd"],
        "generations_total": gens_total, "generations_today": gens_today,
        "cards_spent": cards_spent, "balance_outstanding": balance_outstanding,
        "promo_redemptions": promo_used, "referrals_rewarded": referrals,
    }


@admin_router.get("/timeseries")
def timeseries(days: int = 14, user=Depends(require_admin)):
    """Дневные ряды за N дней — для графиков на дашборде."""
    days = max(7, min(90, days))
    today = dt.date.today()
    dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]

    def by_day(sql):
        return {r["d"]: r["v"] for r in db.query_all(sql)}

    users = by_day("SELECT substr(created_at,1,10) d, COUNT(*) v FROM users GROUP BY d")
    revenue = by_day("SELECT substr(paid_at,1,10) d, COALESCE(SUM(amount_kopecks),0) v "
                     "FROM purchases WHERE status='paid' AND paid_at IS NOT NULL GROUP BY d")
    gens = by_day("SELECT substr(created_at,1,10) d, COUNT(*) v FROM generations GROUP BY d")
    spent = by_day("SELECT substr(created_at,1,10) d, COALESCE(SUM(-delta),0) v "
                   "FROM card_ledger WHERE reason='spend' AND delta<0 GROUP BY d")
    return {
        "dates": dates,
        "users": [users.get(d, 0) for d in dates],
        "revenue_rub": [round(revenue.get(d, 0) / 100, 2) for d in dates],
        "generations": [gens.get(d, 0) for d in dates],
        "cards_spent": [spent.get(d, 0) for d in dates],
    }


# ── users ────────────────────────────────────────────────────────────────────
def _user_brief(r) -> dict:
    return {
        "id": r["id"], "email": r["email"], "name": r["name"],
        "balance_cards": r["balance_cards"], "is_admin": bool(r["is_admin"]),
        "referral_code": r["referral_code"], "created_at": r["created_at"],
    }


@admin_router.get("/users")
def list_users(q: str = "", limit: int = 50, offset: int = 0, user=Depends(require_admin)):
    limit = max(1, min(200, limit))
    if q:
        like = f"%{q.strip().lower()}%"
        rows = db.query_all(
            """SELECT * FROM users WHERE lower(email) LIKE ? OR lower(COALESCE(name,'')) LIKE ?
               ORDER BY id DESC LIMIT ? OFFSET ?""", (like, like, limit, offset))
        total = db.query_one(
            "SELECT COUNT(*) c FROM users WHERE lower(email) LIKE ? OR lower(COALESCE(name,'')) LIKE ?",
            (like, like))["c"]
    else:
        rows = db.query_all("SELECT * FROM users ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
        total = db.query_one("SELECT COUNT(*) c FROM users")["c"]
    return {"users": [_user_brief(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@admin_router.get("/users/{user_id}")
def user_detail(user_id: int, user=Depends(require_admin)):
    row = service.get_user_row(user_id)
    if not row:
        raise ServiceError("Пользователь не найден", "not_found", 404)
    hist = service.history(user_id, limit=100)
    ref = service.referral_stats(user_id)
    return {"user": service._public_user(row), "history": hist, "referral": ref}


class AdjustIn(BaseModel):
    delta: int
    note: Optional[str] = None


@admin_router.post("/users/{user_id}/adjust")
def adjust(user_id: int, body: AdjustIn, user=Depends(require_admin)):
    bal = service.admin_adjust_balance(user_id, body.delta, body.note or f"admin:{user['email']}")
    return {"balance_cards": bal}


class AdminFlagIn(BaseModel):
    is_admin: bool


@admin_router.post("/users/{user_id}/admin")
def set_admin(user_id: int, body: AdminFlagIn, user=Depends(require_admin)):
    service.set_admin_flag(user_id, body.is_admin)
    return {"ok": True}


@admin_router.delete("/users/{user_id}")
def delete_user(user_id: int, user=Depends(require_admin)):
    if user_id == user["id"]:
        raise ServiceError("Нельзя удалить себя", "self", 400)
    service.delete_account(user_id)
    return {"ok": True}


# ── promos ───────────────────────────────────────────────────────────────────
@admin_router.get("/promos")
def list_promos(user=Depends(require_admin)):
    rows = db.query_all("SELECT * FROM promo_codes ORDER BY id DESC")
    return {"promos": [{
        "id": r["id"], "code": r["code"], "cards": r["cards"],
        "max_total_uses": r["max_total_uses"], "per_user_limit": r["per_user_limit"],
        "used_count": r["used_count"], "active": bool(r["active"]), "created_at": r["created_at"],
    } for r in rows]}


class PromoCreateIn(BaseModel):
    code: str
    cards: int
    max_total_uses: Optional[int] = None
    per_user_limit: int = 1


@admin_router.post("/promos")
def create_promo(body: PromoCreateIn, user=Depends(require_admin)):
    service.create_promo(body.code, body.cards, body.max_total_uses, body.per_user_limit)
    return {"ok": True}


class PromoToggleIn(BaseModel):
    active: bool


@admin_router.post("/promos/{promo_id}/toggle")
def toggle_promo(promo_id: int, body: PromoToggleIn, user=Depends(require_admin)):
    service.toggle_promo(promo_id, body.active)
    return {"ok": True}


# ── purchases & generations (monitoring) ─────────────────────────────────────
@admin_router.get("/purchases")
def purchases(limit: int = 50, user=Depends(require_admin)):
    limit = max(1, min(200, limit))
    rows = db.query_all(
        """SELECT p.*, u.email FROM purchases p JOIN users u ON u.id=p.user_id
           ORDER BY p.created_at DESC LIMIT ?""", (limit,))
    return {"purchases": [{
        "id": r["id"], "email": r["email"], "kind": r["kind"], "pack_key": r["pack_key"],
        "cards": r["cards"], "amount_rub": round(r["amount_kopecks"] / 100, 2),
        "status": r["status"], "provider": r["provider"],
        "created_at": r["created_at"], "paid_at": r["paid_at"],
    } for r in rows]}


@admin_router.get("/generations")
def generations(limit: int = 50, user=Depends(require_admin)):
    limit = max(1, min(200, limit))
    rows = db.query_all(
        """SELECT g.id, g.user_id, g.title, g.status, g.progress, g.card_count,
                  g.created_at, g.updated_at, u.email
           FROM generations g JOIN users u ON u.id=g.user_id
           ORDER BY g.created_at DESC LIMIT ?""", (limit,))
    return {"generations": [dict(r) for r in rows]}


# ── settings / API keys (config.json) ────────────────────────────────────────
def _mask(key: str) -> str:
    if not key:
        return ""
    return key[:6] + "…" + key[-4:] if len(key) > 12 else "***"


@admin_router.get("/settings")
def get_settings(user=Depends(require_admin)):
    cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
    endpoints = []
    for ep in cfg.get("claude_endpoints", []):
        endpoints.append({"name": ep.get("name"), "base_url": ep.get("base_url"),
                          "api_key_masked": _mask(ep.get("api_key", "")),
                          "models": ep.get("models", [])})
    return {
        "default_provider": cfg.get("default_provider"),
        # Тир-политика картинок (free→low/микс, paid→gpt medium).
        "tier_policy_enabled": bool(cfg.get("tier_policy_enabled", True)),
        "new_user_provider": cfg.get("new_user_provider", "mix"),
        "site_base_url": cfg.get("site_base_url", ""),
        "admin_emails": cfg.get("admin_emails", []),
        "fastgen_api_key_masked": _mask(cfg.get("fastgen_api_key", "")),
        "fastgen_base_url": cfg.get("fastgen_base_url", ""),
        "veo_api_key_masked": _mask(cfg.get("veo_api_key", "")),
        # Прямой OpenAI gpt-image-2 — рычаги для админа.
        "openai_image_api_key_masked":  _mask(cfg.get("openai_image_api_key", "")),
        "openai_image_base_url":        cfg.get("openai_image_base_url", "https://api.openai.com/v1"),
        "openai_image_model":           cfg.get("openai_image_model", "gpt-image-2"),
        "openai_image_quality":         cfg.get("openai_image_quality", "high"),
        "openai_image_size":            cfg.get("openai_image_size", "auto"),
        # Глобальный рубильник прокси нейронки (тумблер + поле в админке).
        "ai_proxy": {
            "enabled":    bool((cfg.get("ai_proxy") or {}).get("enabled")),
            "url_masked": _mask_proxy((cfg.get("ai_proxy") or {}).get("url", "")),
            "has_url":    bool((cfg.get("ai_proxy") or {}).get("url")),
        },
        "claude_endpoints": endpoints,
    }


class SettingsIn(BaseModel):
    default_provider: Optional[str] = None
    tier_policy_enabled: Optional[bool] = None
    new_user_provider: Optional[str] = None       # mix / fastgen_nanopro / openai_low
    site_base_url: Optional[str] = None
    admin_emails: Optional[list[str]] = None
    fastgen_api_key: Optional[str] = None
    fastgen_base_url: Optional[str] = None
    veo_api_key: Optional[str] = None
    # OpenAI Image (прямой gpt-image-2)
    openai_image_api_key: Optional[str] = None
    openai_image_base_url: Optional[str] = None
    openai_image_model: Optional[str] = None
    openai_image_quality: Optional[str] = None   # low/medium/high/auto
    openai_image_size: Optional[str] = None      # 1024x1024 / 1024x1536 / 1536x1024 / auto
    # per-endpoint updates: [{name, base_url?, api_key?, models?}]
    claude_endpoints: Optional[list[dict]] = None
    # глобальный рубильник прокси нейронки: {enabled: bool, url?: str}
    ai_proxy: Optional[dict] = None


_ALLOWED_OPENAI_QUALITY = {"low", "medium", "high", "auto"}
_ALLOWED_OPENAI_SIZE    = {"1024x1024", "1024x1536", "1536x1024", "auto"}


@admin_router.put("/settings")
def put_settings(body: SettingsIn, user=Depends(require_admin)):
    cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
    if body.default_provider is not None:
        cfg["default_provider"] = body.default_provider
    if body.tier_policy_enabled is not None:
        cfg["tier_policy_enabled"] = bool(body.tier_policy_enabled)
    if body.new_user_provider is not None:
        v = body.new_user_provider.strip().lower()
        if v in ("mix", "fastgen_nanopro", "openai_low"):
            cfg["new_user_provider"] = v
    if body.site_base_url is not None:
        cfg["site_base_url"] = body.site_base_url
    if body.admin_emails is not None:
        cfg["admin_emails"] = [e.strip() for e in body.admin_emails if e.strip()]
    if body.fastgen_api_key and not body.fastgen_api_key.endswith("…") and "***" not in body.fastgen_api_key:
        cfg["fastgen_api_key"] = body.fastgen_api_key
    if body.fastgen_base_url is not None:
        cfg["fastgen_base_url"] = body.fastgen_base_url
    if body.veo_api_key and not body.veo_api_key.endswith("…") and "***" not in body.veo_api_key:
        cfg["veo_api_key"] = body.veo_api_key
    # OpenAI Image (прямой gpt-image-2) — quality/size валидируем по списку
    # (если фронт пришлёт мусор — не записываем, чтобы потом не упасть в рендере).
    if (body.openai_image_api_key
            and not body.openai_image_api_key.endswith("…")
            and "***" not in body.openai_image_api_key):
        cfg["openai_image_api_key"] = body.openai_image_api_key
    if body.openai_image_base_url is not None:
        cfg["openai_image_base_url"] = body.openai_image_base_url.strip() or "https://api.openai.com/v1"
    if body.openai_image_model is not None:
        cfg["openai_image_model"] = body.openai_image_model.strip() or "gpt-image-2"
    if body.openai_image_quality is not None:
        q = body.openai_image_quality.strip().lower()
        if q in _ALLOWED_OPENAI_QUALITY:
            cfg["openai_image_quality"] = q
    if body.openai_image_size is not None:
        s = body.openai_image_size.strip().lower().replace("х", "x")  # кириллический «х»
        if s in _ALLOWED_OPENAI_SIZE:
            cfg["openai_image_size"] = s
    if body.claude_endpoints:
        for upd in body.claude_endpoints:
            name = upd.get("name")
            if not name:
                continue
            for ep in cfg.setdefault("claude_endpoints", []):
                if ep.get("name") == name:
                    if upd.get("base_url"):
                        ep["base_url"] = upd["base_url"]
                    k = upd.get("api_key")
                    if k and not k.endswith("…") and "***" not in k:
                        ep["api_key"] = k
                    if isinstance(upd.get("models"), list):
                        ep["models"] = upd["models"]
                    break
    # Глобальный прокси нейронки: тумблер + (опц.) новый url.
    # url с маской (*** / …) игнорируем — это неизменённое отображение.
    if body.ai_proxy is not None:
        ap = cfg.get("ai_proxy") or {}
        if "enabled" in body.ai_proxy:
            ap["enabled"] = bool(body.ai_proxy["enabled"])
        new_url = body.ai_proxy.get("url")
        if new_url is not None:
            new_url = str(new_url).strip()
            if "***" not in new_url and "…" not in new_url:
                ap["url"] = new_url
        cfg["ai_proxy"] = ap
    _CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    # Реал-тайм: после записи перезаливаем CFG в server.py и сбрасываем
    # singleton-клиенты, чтобы провайдер картинок / quality / size / ключ
    # подхватились без рестарта (зеркало того, как живёт reorder LLM endpoints).
    _reload_server_cfg()
    return {"ok": True}


# ── LLM endpoints: live reorder / toggle / test ──────────────────────────────
# Зачем: модель-прокси Stepanovikov/Wellflow и OpenAI имеют разные простои.
# Раньше порядок цепочки менялся только редактированием config.json + рестартом.
# Теперь админ тащит блок в UI → PUT → config переписан → in-memory CFG в
# server.py перезаливается G.load_config() через хук → следующий вызов идёт по
# новому порядку. Никаких рестартов.

def _mask_proxy(proxy: str) -> str:
    if not proxy:
        return ""
    if "@" in proxy:
        scheme, _, rest = proxy.partition("://")
        creds, _, host = rest.partition("@")
        u = creds.split(":", 1)[0]
        return f"{scheme}://{u}:***@{host}"
    return proxy


def _reload_server_cfg() -> None:
    """Горячая перезагрузка CFG во ВСЕХ живых модулях + сброс LLM-blacklist'ов +
    сброс singleton-клиентов, у которых конструктор зависит от cfg.

    ВАЖНО про мутацию vs присваивание:
      Раньше делали `_srv.CFG = load_config()` — это создаёт НОВЫЙ dict.
      Но пайплайн рендера получает cfg как параметр функции _stream_render_…
      и держит ссылку на СТАРЫЙ dict. После присваивания server.CFG смотрит на
      новый dict, а уже-запущенная задача — на старый, и админка кажется
      «не применилась в реал-тайме» (видели на openai_image_quality: ставил
      low, рендер шёл high).
      Лекарство: in-place — clear()+update() на текущий dict. Все, кто хранят
      ссылку, моментально видят новые значения.

    ВАЖНО про дубль модуля server (фикс «настройки применяются только после
    рестарта»):
      Сервер запускается как `python server.py` → его модуль в sys.modules — это
      `__main__`, и route-хендлеры (`/api/generate-cards` и т.п.) читают
      `__main__.CFG`. А `import server as _srv` импортирует server.py ВТОРОЙ раз
      под именем `server` (отдельный объект со своим `server.CFG`). Раньше мы
      мутировали ТОЛЬКО `server.CFG` → роуты в `__main__` продолжали видеть
      стартовый конфиг, пока сервер не перезапустят. Поэтому обновляем CFG во
      ВСЕХ модулях, где он есть (и `__main__`, и `server`), дедуп по id объекта.
    """
    try:
        import sys
        import generator as _G  # type: ignore
        new_cfg = _G.load_config()
        try:
            import server as _srv  # noqa: F401 — гарантируем наличие модуля 'server'
        except Exception:
            pass
        # Обновляем ТОЛЬКО те модули, у которых уже есть dict CFG (т.е. это
        # реально инстанс server.py — будь то __main__ от `python server.py`
        # или модуль `server` от `uvicorn server:app`). Не выдумываем CFG там,
        # где его нет (например, в uvicorn-CLI __main__).
        seen_ids = set()
        for modname in ("server", "__main__"):
            mod = sys.modules.get(modname)
            if mod is None:
                continue
            existing = getattr(mod, "CFG", None)
            if isinstance(existing, dict) and id(existing) not in seen_ids:
                seen_ids.add(id(existing))
                existing.clear()
                existing.update(new_cfg)   # in-place: все держатели ссылки видят свежее
        try:
            _G.reset_claude_health()
        except Exception:
            pass
        try:
            _G.reset_openai_image_client()
        except Exception:
            pass
        try:
            # FastGen-синглтоны тоже держат proxy_url из конфига на момент
            # создания — без сброса смена ai_proxy к картинкам не применялась
            # до рестарта («прокси слетает» из админки).
            _G.reset_fastgen_clients()
        except Exception:
            pass
    except Exception:
        # Server-side reload опционален — даже без него следующий рестарт
        # подхватит новый файл. Падать тут нельзя.
        pass


@admin_router.get("/llm-endpoints")
def llm_endpoints_list(user=Depends(require_admin)):
    """Маскированный список endpoints с реальным порядком и enabled-флагом."""
    cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
    order = cfg.get("claude_endpoint_order") or []
    out = []
    for ep in cfg.get("claude_endpoints", []):
        out.append({
            "name":           ep.get("name", "?"),
            "base_url":       ep.get("base_url", ""),
            "api_key_masked": _mask(ep.get("api_key", "")),
            "proxy_masked":   _mask_proxy(ep.get("proxy_url") or ""),
            "has_proxy":      bool(ep.get("proxy_url")),
            "enabled":        ep.get("enabled") is not False,
            "models":         ep.get("models", []),
        })
    # Сортируем как в реальной цепочке: сохранённый порядок впереди.
    if order:
        rank = {n: i for i, n in enumerate(order)}
        out.sort(key=lambda e: (rank.get(e["name"], 9999),))
    return {"endpoints": out, "order": order}


class LlmOrderIn(BaseModel):
    order: list[str]
    enabled: Optional[dict] = None  # {name: bool}


@admin_router.put("/llm-endpoints/order")
def llm_endpoints_set_order(body: LlmOrderIn, user=Depends(require_admin)):
    """Сохраняет порядок и enabled-флаги, горячо перезагружает CFG."""
    cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
    known = {ep.get("name") for ep in cfg.get("claude_endpoints", []) if ep.get("name")}
    cleaned_order = [n for n in body.order if isinstance(n, str) and n in known]
    cfg["claude_endpoint_order"] = cleaned_order
    if body.enabled:
        for ep in cfg.get("claude_endpoints", []):
            name = ep.get("name")
            if name in body.enabled:
                ep["enabled"] = bool(body.enabled[name])
    _CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    _reload_server_cfg()
    return {"ok": True, "order": cleaned_order}


# ── LLM token usage (для админки) ────────────────────────────────────────────
# Раньше у нас не было ни цента видимости куда уходят токены. Теперь generator.py
# на каждый успешный 200 пишет строку в `llm_usage` — здесь читаем агрегатами:
#   • totals (день / месяц / всё время)
#   • разрез по моделям (топ-N)
#   • разрез по юзерам (топ-N)
#   • разрез по purpose ('design' / 'detect_category' / 'copy_fix' / ...)
#   • дневной ряд за N дней для графика
# Фильтры: ?days=N (default 14) и ?user_id=ID для срезов.

@admin_router.get("/llm-usage/summary")
def llm_usage_summary(days: int = 14, user_id: Optional[int] = None,
                      _user=Depends(require_admin)):
    days = max(1, min(180, days))
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=days)).isoformat()
    where = "WHERE created_at >= ?"
    params: list = [cutoff]
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)

    totals = db.query_one(
        f"""SELECT COUNT(*)              AS calls,
                   COALESCE(SUM(prompt_tokens),0)     AS p_in,
                   COALESCE(SUM(completion_tokens),0) AS p_out,
                   COALESCE(SUM(total_tokens),0)      AS p_total,
                   COALESCE(SUM(cached_tokens),0)     AS p_cached,
                   COALESCE(SUM(cost_usd),0)          AS cost
            FROM llm_usage {where}""",
        tuple(params),
    )

    # Разрез по типу провайдера: 'priced' (OpenAI/OpenRouter — есть $) и 'claude'
    # (прокси stepanovikov/wellflow — цену НЕ считаем, только расход токенов).
    split_rows = db.query_all(
        f"""SELECT CASE WHEN lower(model) LIKE 'claude%' THEN 'claude' ELSE 'priced' END AS grp,
                   COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens),0)     AS p_in,
                   COALESCE(SUM(completion_tokens),0) AS p_out,
                   COALESCE(SUM(total_tokens),0)      AS p_total,
                   COALESCE(SUM(cached_tokens),0)     AS p_cached,
                   COALESCE(SUM(cost_usd),0)          AS cost
            FROM llm_usage {where}
            GROUP BY grp""",
        tuple(params),
    )
    by_provider_group = {r["grp"]: dict(r) for r in split_rows}

    # UTC: строки llm_usage/image_usage пишутся с created_at=utcnow().isoformat(),
    # и cutoff окна тоже UTC. Брали date.today() (локальная) → на проде (UTC+3)
    # между 00:00–03:00 «сегодня» съезжало на день и показывало 0. Берём UTC-дату.
    today_iso = dt.datetime.utcnow().strftime("%Y-%m-%d")
    today = db.query_one(
        f"""SELECT COUNT(*) AS calls,
                   COALESCE(SUM(total_tokens),0) AS p_total,
                   COALESCE(SUM(cost_usd),0)     AS cost
            FROM llm_usage WHERE substr(created_at,1,10)=?""",
        (today_iso,),
    )

    by_model = db.query_all(
        f"""SELECT model,
                   COUNT(*) AS calls,
                   SUM(prompt_tokens) AS p_in,
                   SUM(completion_tokens) AS p_out,
                   SUM(total_tokens) AS p_total,
                   SUM(cached_tokens) AS p_cached,
                   SUM(cost_usd) AS cost
            FROM llm_usage {where}
            GROUP BY model ORDER BY p_total DESC LIMIT 20""",
        tuple(params),
    )

    by_purpose = db.query_all(
        f"""SELECT COALESCE(purpose,'unknown') AS purpose,
                   COUNT(*) AS calls,
                   SUM(total_tokens) AS p_total,
                   SUM(cost_usd) AS cost
            FROM llm_usage {where}
            GROUP BY purpose ORDER BY p_total DESC""",
        tuple(params),
    )

    by_endpoint = db.query_all(
        f"""SELECT endpoint,
                   COUNT(*) AS calls,
                   SUM(total_tokens) AS p_total,
                   SUM(cost_usd) AS cost
            FROM llm_usage {where}
            GROUP BY endpoint ORDER BY p_total DESC""",
        tuple(params),
    )

    # ── Генерации картинок (image_usage) — фото + деньги на них ──────────────
    # Отдельная таблица: у картинок нет токенов, но есть провайдер/кредиты/$.
    # openai_direct (gpt-image-2) даёт реальную оценку $; fastgen — кредиты.
    img_totals = db.query_one(
        f"""SELECT COALESCE(SUM(images),0)   AS images,
                   COUNT(*)                  AS rows,
                   COALESCE(SUM(credits),0)  AS credits,
                   COALESCE(SUM(cost_usd),0) AS cost
            FROM image_usage {where}""",
        tuple(params),
    )
    img_today = db.query_one(
        """SELECT COALESCE(SUM(images),0) AS images,
                  COALESCE(SUM(cost_usd),0) AS cost
           FROM image_usage WHERE substr(created_at,1,10)=?""",
        (today_iso,),
    )
    img_by_provider = db.query_all(
        f"""SELECT provider,
                   COALESCE(SUM(images),0)   AS images,
                   COALESCE(SUM(credits),0)  AS credits,
                   COALESCE(SUM(cost_usd),0) AS cost
            FROM image_usage {where}
            GROUP BY provider ORDER BY images DESC""",
        tuple(params),
    )

    return {
        "window_days":      days,
        "user_id":          user_id,
        "totals":           dict(totals) if totals else {},
        "today":            dict(today) if today else {},
        "by_provider_group": by_provider_group,   # {'priced': {...$...}, 'claude': {...токены...}}
        "by_model":         [dict(r) for r in by_model],
        "by_purpose":       [dict(r) for r in by_purpose],
        "by_endpoint":      [dict(r) for r in by_endpoint],
        "images": {
            "totals":      dict(img_totals) if img_totals else {},
            "today":       dict(img_today) if img_today else {},
            "by_provider": [dict(r) for r in img_by_provider],
        },
    }


@admin_router.get("/llm-usage/by-day")
def llm_usage_by_day(days: int = 14, user_id: Optional[int] = None,
                     _user=Depends(require_admin)):
    days = max(1, min(180, days))
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=days)).isoformat()
    where = "WHERE created_at >= ?"
    params: list = [cutoff]
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)

    rows = db.query_all(
        f"""SELECT substr(created_at,1,10) AS day,
                   COUNT(*) AS calls,
                   SUM(prompt_tokens) AS p_in,
                   SUM(completion_tokens) AS p_out,
                   SUM(total_tokens) AS p_total,
                   SUM(cost_usd) AS cost
            FROM llm_usage {where}
            GROUP BY day ORDER BY day""",
        tuple(params),
    )
    # Картинки по дням (для второго графика «Фото по дням»).
    img_rows = db.query_all(
        f"""SELECT substr(created_at,1,10) AS day,
                   COALESCE(SUM(images),0)   AS images,
                   COALESCE(SUM(cost_usd),0) AS img_cost
            FROM image_usage {where}
            GROUP BY day ORDER BY day""",
        tuple(params),
    )
    img_seen = {r["day"]: dict(r) for r in img_rows}
    # Дозаполняем пропущенные дни нулями — фронт удобнее строить график.
    seen = {r["day"]: dict(r) for r in rows}
    out = []
    _utc_today = dt.datetime.utcnow().date()  # UTC — совпадает с substr(created_at) строк
    for i in range(days - 1, -1, -1):
        d = (_utc_today - dt.timedelta(days=i)).isoformat()
        rec = seen.get(d) or {"day": d, "calls": 0, "p_in": 0, "p_out": 0, "p_total": 0, "cost": 0}
        im = img_seen.get(d) or {}
        rec["images"] = im.get("images", 0)
        rec["img_cost"] = im.get("img_cost", 0)
        out.append(rec)
    return {"days": out}


@admin_router.get("/llm-usage/by-user")
def llm_usage_by_user(days: int = 14, limit: int = 25, _user=Depends(require_admin)):
    days = max(1, min(180, days))
    limit = max(1, min(200, limit))
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=days)).isoformat()
    tok_rows = db.query_all(
        """SELECT lu.user_id,
                  u.email,
                  COUNT(*)               AS calls,
                  SUM(lu.prompt_tokens)  AS p_in,
                  SUM(lu.completion_tokens) AS p_out,
                  SUM(lu.total_tokens)   AS p_total,
                  SUM(lu.cost_usd)       AS cost
           FROM llm_usage lu
           LEFT JOIN users u ON u.id = lu.user_id
           WHERE lu.created_at >= ?
           GROUP BY lu.user_id, u.email""",
        (cutoff,),
    )
    # Фото по юзерам — отдельная таблица; мерджим, чтобы видеть «скок фоток генерил».
    img_rows = db.query_all(
        """SELECT iu.user_id,
                  u.email,
                  COALESCE(SUM(iu.images),0)   AS img_count,
                  COALESCE(SUM(iu.cost_usd),0) AS img_cost
           FROM image_usage iu
           LEFT JOIN users u ON u.id = iu.user_id
           WHERE iu.created_at >= ?
           GROUP BY iu.user_id, u.email""",
        (cutoff,),
    )
    merged: dict = {}
    for r in tok_rows:
        d = dict(r)
        d["img_count"] = 0
        d["img_cost"] = 0
        merged[r["user_id"]] = d
    for r in img_rows:
        uid = r["user_id"]
        if uid in merged:
            merged[uid]["img_count"] = r["img_count"]
            merged[uid]["img_cost"] = r["img_cost"]
        else:
            merged[uid] = {
                "user_id": uid, "email": r["email"], "calls": 0,
                "p_in": 0, "p_out": 0, "p_total": 0, "cost": 0,
                "img_count": r["img_count"], "img_cost": r["img_cost"],
            }
    # Сортируем по токенам, фото — вторичный ключ (чтобы фото-активные не терялись).
    users = sorted(
        merged.values(),
        key=lambda x: ((x.get("p_total") or 0), (x.get("img_count") or 0)),
        reverse=True,
    )[:limit]
    return {"users": users}


class LlmTestIn(BaseModel):
    name: str


@admin_router.post("/llm-endpoints/test")
def llm_endpoints_test(body: LlmTestIn, user=Depends(require_admin)):
    """Тычок endpoint мини-запросом — показывает живой он или нет.
    Игнорирует enabled (можно тестить выключенный, чтобы решить включать ли)."""
    import generator as _G  # late import: избегаем circular
    name = (body.name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    cfg = _G.load_config()
    # ищем endpoint по имени даже если он disabled
    raw = [ep for ep in cfg.get("claude_endpoints", []) if ep.get("name") == name]
    if not raw:
        return {"ok": False, "error": f"endpoint {name!r} not found"}
    ep_raw = raw[0]
    # форсим enabled на время теста (delete disabled чтобы _build_claude_endpoints
    # вернул его), копируем cfg чтобы не мутировать оригинал
    test_cfg = dict(cfg)
    test_cfg["claude_endpoints"] = [dict(e) for e in cfg.get("claude_endpoints", [])]
    for e in test_cfg["claude_endpoints"]:
        if e.get("name") == name:
            e["enabled"] = True
    model = (ep_raw.get("models") or ["?"])[0]
    t0 = time.time()
    try:
        text = _G._claude_chat(
            test_cfg,
            system_prompt="You are a ping responder.",
            user_prompt="reply with the single word OK",
            images=None, max_tokens=4, temperature=0.0,
            retries_per_model=0, dead_endpoint_threshold=1,
            only_endpoints=[name],
        )
        dt_ms = int((time.time() - t0) * 1000)
        return {"ok": True, "ms": dt_ms, "model": model, "reply": (text or "").strip()[:80]}
    except Exception as e:
        dt_ms = int((time.time() - t0) * 1000)
        return {"ok": False, "ms": dt_ms, "model": model, "error": str(e)[:240]}

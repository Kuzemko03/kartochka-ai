"""
WBnator API Server — FastAPI backend for карточка Ai frontend.
Wraps the competitor analysis pipeline as REST endpoints.

Run:  python server.py   (opens http://127.0.0.1:8000)
"""

import os
import sys
import json
import time
import uuid
import shutil
import asyncio
import logging
from pathlib import Path
from typing import Optional
from dataclasses import asdict

# Fix Windows UTF-8
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"

from fastapi import FastAPI, UploadFile, File, Form, Request, Response, Depends
from fastapi.responses import (
    HTMLResponse, FileResponse, JSONResponse, StreamingResponse, PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import generator as G
import wb_analyzer as A
import prompts as P
from wb_parser import WBParser, Product
from wb_token_pool import WBTokenPool, get_global_pool, shutdown_global_pool
from wb_smart_client import make_smart_client_from_config

import site_gate  # глобальная заглушка-замок (пред-запусковый admin-доступ)

# Account / billing subsystem (sqlite, auth, cards, promo, referral, payments)
from accounts import (
    router as account_router,
    admin_router as account_admin_router,
    init_db as accounts_init_db,
    install_error_handler as accounts_install_error_handler,
    service as account_service,
    backup as accounts_backup,
    mailer as accounts_mailer,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("server")

app = FastAPI(title="WBnator API")

# NOTE: cookies (session auth) require credentialed CORS. With allow_credentials
# нельзя использовать "*" origin wildcard, иначе любой сайт получит CSRF-доступ
# на /api/account/*, может сменить пароль / списать средства / удалить аккаунт.
# Поэтому разрешаем ТОЛЬКО:
#   - localhost / 127.0.0.1 (dev)
#   - наш собственный домен (prod) — задаётся через config.site_base_url
#     (если он есть — добавляем его явно)
# Раньше тут была вторая wildcard-middleware (allow_origin_regex match-all с
# allow_credentials=True), которая echo-back текущий Origin и полностью
# обходила защиту от CSRF на /api/account/*. Снёс.
from fastapi.middleware.cors import CORSMiddleware

# Собираем regex с явным перечислением допустимых origin'ов
_allowed_origins_re_parts = [r"https?://(localhost|127\.0\.0\.1)(:\d+)?"]
try:
    _prod_base = (G.load_config() or {}).get("site_base_url") or ""
    if _prod_base:
        from urllib.parse import urlparse as _urlparse
        _h = _urlparse(_prod_base).hostname
        if _h:
            # экранируем точки в hostname для regex
            _allowed_origins_re_parts.append(
                rf"https?://{_h.replace('.', r'\.')}(:\d+)?"
            )
except Exception:
    pass

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex="|".join(_allowed_origins_re_parts),
    allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# Пред-запусковая заглушка-замок: регистрируется ПОСЛЕ CORS → это внешний слой
# middleware, срабатывает первым. Весь сайт закрыт паролем админа, пока в
# config.json site_gate.enabled = true (после запуска переключить в false).
site_gate.install(app)

# Account / billing API + admin API + ServiceError → JSON handler
app.include_router(account_router.router)
app.include_router(account_admin_router)
accounts_install_error_handler(app)

CFG = G.load_config()
BASE = Path(__file__).parent
UPLOAD_DIR = BASE / "tmp" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ─── WB Smart Stack: token pool + smart client + singleton parser ─────────
#
# Стек поднимается лениво при первом обращении (чтобы dev-режим без
# seleniumbase не падал на импорте). В проде — на startup'е, чтобы pool
# был прогрет к первому пользователю.

_WB_PARSER: Optional[WBParser] = None
_WB_PARSER_LOCK = __import__("threading").Lock()


def get_wb_parser() -> WBParser:
    """Singleton WBParser с подключённым TokenPool + SmartClient."""
    global _WB_PARSER
    with _WB_PARSER_LOCK:
        if _WB_PARSER is not None:
            return _WB_PARSER

        smart_cfg = (CFG or {}).get("wb_smart") or {}
        enabled = smart_cfg.get("enabled", True)

        if not enabled:
            logger.info("WB smart-stack: ОТКЛЮЧЁН в config — fallback на старый парсер")
            _WB_PARSER = WBParser(photo_limit=0)
            return _WB_PARSER

        try:
            pool = get_global_pool(
                min_size=smart_cfg.get("token_pool_min", 2),
                max_size=smart_cfg.get("token_pool_max", 4),
                ttl_seconds=smart_cfg.get("token_ttl_seconds", 4 * 3600),
                refresh_interval=smart_cfg.get("token_refresh_interval", 30 * 60),
            )
            smart_client = make_smart_client_from_config(CFG, token_pool=pool)
            _WB_PARSER = WBParser(photo_limit=0, smart_client=smart_client)
            logger.info(f"WB smart-stack: запущен (proxies={len(smart_client.proxies)}, "
                        f"token pool min/max={pool.min_size}/{pool.max_size})")
        except Exception as e:
            logger.exception(f"WB smart-stack: не удалось запустить ({e}) → fallback на старый")
            _WB_PARSER = WBParser(photo_limit=0)
        return _WB_PARSER


# ─── Авточистка старых генераций ────────────────────────────────────────────
#
# output/product_*-папки накапливаются быстро (каждая = несколько JPG ~500 KB +
# JSON-промпты + _design.json), на сервере диск 50 ГБ → за пару недель активного
# использования забивается. Чистим директории старше N дней (default 30) и
# временные слепки tmp/ (uploads/raw_dir) старше 1 дня. Cache-Control на
# /api/output/* immutable=30d синхронизирован с retention — кэш браузера и файл
# на диске истекают одновременно, broken-images не накапливаются.

def _cleanup_retention_days() -> int:
    try:
        return int((CFG or {}).get("output_retention_days") or 30)
    except Exception:
        return 30


def _cleanup_old_outputs(retention_days: int) -> dict:
    """Удаляет старые product_*-папки и временные tmp-слепки. Идемпотентно.

    Args:
        retention_days: возраст в днях, старше — удаляем.
    Returns:
        {"deleted_packs": int, "deleted_tmp": int, "freed_bytes": int}
    """
    stats = {"deleted_packs": 0, "deleted_tmp": 0, "freed_bytes": 0}

    def _dir_size(root: Path) -> int:
        total = 0
        try:
            for f in root.rglob("*"):
                if f.is_file():
                    try: total += f.stat().st_size
                    except OSError: pass
        except OSError:
            pass
        return total

    def _max_child_mtime(root: Path) -> float:
        """Самый свежий mtime среди файлов внутри. Защищает от удаления
        ACTIVE-папок (если генерация ещё пишет → один из файлов будет свежим).
        Папки 30-дневной давности с детьми-30-дневной-давности — спокойно
        удалятся, а вот pack который чудом всё ещё дописывается — выживет.
        """
        latest = 0.0
        try:
            for f in root.rglob("*"):
                if f.is_file():
                    try:
                        m = f.stat().st_mtime
                        if m > latest: latest = m
                    except OSError:
                        pass
        except OSError:
            pass
        return latest

    now = time.time()
    cutoff_packs = now - retention_days * 86400
    # Жёсткий guard: pack чьи файлы обновлялись за последние active_window — НЕ
    # трогаем, даже если корневой mtime старый. active_window=24h — даже самая
    # длинная генерация заведомо короче. Прикрывает race cleanup vs writer:
    # shutil.rmtree(running_dir) → потеря свежих карточек у юзера.
    active_window = now - 24 * 3600
    out_root = BASE / "output"
    if out_root.is_dir():
        for p in out_root.iterdir():
            if not p.is_dir() or not p.name.startswith("product_"):
                continue
            try:
                if p.stat().st_mtime > cutoff_packs:
                    continue
                if _max_child_mtime(p) > active_window:
                    # папка старая, но внутри активно пишут → активная генерация
                    continue
                size = _dir_size(p)
                shutil.rmtree(p, ignore_errors=True)
                if not p.exists():
                    stats["deleted_packs"] += 1
                    stats["freed_bytes"] += size
            except OSError:
                logger.exception(f"cleanup: не удалил {p}")

    # tmp-слепки (uploads/raw_dir генерации) — короче TTL, 1 день
    cutoff_tmp = time.time() - 86400
    tmp_root = BASE / "tmp"
    if tmp_root.is_dir():
        for p in tmp_root.iterdir():
            # uploads — папка-контейнер, её саму не удаляем (только содержимое)
            if p.name == "uploads" and p.is_dir():
                for f in p.iterdir():
                    try:
                        if f.is_file() and f.stat().st_mtime < cutoff_tmp:
                            size = f.stat().st_size
                            f.unlink()
                            stats["deleted_tmp"] += 1
                            stats["freed_bytes"] += size
                    except OSError:
                        pass
                continue
            # папки-слепки tmp/YYYYMMDD_*
            if not p.is_dir():
                continue
            try:
                if p.stat().st_mtime > cutoff_tmp:
                    continue
                size = _dir_size(p)
                shutil.rmtree(p, ignore_errors=True)
                if not p.exists():
                    stats["deleted_tmp"] += 1
                    stats["freed_bytes"] += size
            except OSError:
                logger.exception(f"cleanup: не удалил tmp {p}")

    return stats


async def _cleanup_loop():
    """Периодическая чистка — раз в 6 часов в фоне."""
    interval = 6 * 3600
    while True:
        try:
            await asyncio.sleep(interval)
            days = _cleanup_retention_days()
            stats = await asyncio.to_thread(_cleanup_old_outputs, days)
            if stats["deleted_packs"] or stats["deleted_tmp"]:
                freed_mb = stats["freed_bytes"] / (1024 * 1024)
                logger.info(
                    f"cleanup: −{stats['deleted_packs']} packs, "
                    f"−{stats['deleted_tmp']} tmp items, −{freed_mb:.1f} MB"
                )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("cleanup loop tick failed — продолжаем")


@app.on_event("startup")
async def _startup_warmup():
    # Снимаем бэкап БДО ДО любых миграций / init — если init вдруг что-то
    # поломает, всегда можно откатиться через `python manage.py restore …`.
    try:
        paths = accounts_backup.backup_now("startup")
        if paths:
            logger.info(f"DB backup (startup) → {paths[0]}")
    except Exception:
        logger.exception("startup backup упал — продолжаем без него")

    # инициализируем БД аккаунтов (создаёт таблицы идемпотентно + сидит промокоды)
    try:
        accounts_init_db()
        account_service.gen_mark_stale_running()   # незавершённые джобы прошлого процесса → error
        logger.info("Accounts DB initialised")
    except Exception:
        logger.exception("Accounts DB init упал")

    # Почта (reg.ru SMTP). Если не настроена (нет пароля) — останется StubMailer
    # и письма просто пишутся в лог, сервер при этом поднимается нормально.
    try:
        accounts_mailer.init_mailer_from_config(CFG)
    except Exception:
        logger.exception("init_mailer_from_config упал — письма остаются в логе")

    # Фоновые бэкапы каждый час. Защита от потери юзеров если рабочую папку
    # случайно перезапишут / сотрут — копии живут в %USERPROFILE%\.wbnator\backups.
    try:
        accounts_backup.start_periodic(3600)
        logger.info("DB backup loop started (every 60 min)")
    except Exception:
        logger.exception("periodic backup loop не стартанул")

    # прогреваем стек заранее
    import asyncio
    try:
        asyncio.create_task(asyncio.to_thread(get_wb_parser))  # patched: non-blocking startup
    except Exception:
        logger.exception("WB smart-stack warmup упал — продолжаем с lazy init")

    # Авточистка output/ + tmp/ — раз сразу при старте, потом каждые 6 часов.
    # Освобождает диск (50 ГБ на Timeweb-сервере забивается за пару недель
    # активного использования: каждая генерация = пара JPG ~500 KB + JSON-блобы).
    try:
        days = _cleanup_retention_days()
        stats = await asyncio.to_thread(_cleanup_old_outputs, days)
        if stats["deleted_packs"] or stats["deleted_tmp"]:
            freed_mb = stats["freed_bytes"] / (1024 * 1024)
            logger.info(
                f"cleanup (startup): −{stats['deleted_packs']} packs, "
                f"−{stats['deleted_tmp']} tmp items, −{freed_mb:.1f} MB"
            )
        asyncio.create_task(_cleanup_loop())
        logger.info(f"cleanup loop запущен (retention={days}d, каждые 6h)")
    except Exception:
        logger.exception("output cleanup на startup упал — продолжаем без чистки")


@app.on_event("shutdown")
async def _shutdown_pool():
    try:
        accounts_backup.stop_periodic()
        accounts_backup.backup_now("shutdown")
    except Exception:
        pass
    try:
        shutdown_global_pool()
    except Exception:
        pass


# ─── Static files ───────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# WebView в Android-обёртке (и любой mobile browser) тащит ассеты при каждом
# скролле / навигации, если у них нет HTTP-кеша. На /static/assets/* лежат
# иконки/лого/демо-фото (которые меняются раз в месяц) — ставим immutable
# на 30 дней, WebView будет брать из локального кеша мгновенно.
# Для index.html, account.js, landing.html — даём no-cache (revalidation
# через ETag/If-Modified-Since), чтобы свежие правки сразу подхватывались.
@app.middleware("http")
async def _static_cache_headers(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/static/assets/"):
        resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    elif path.startswith("/api/output/") or path.startswith("/api/tmp/"):
        # Сгенерированные карточки и временные превью — уникальный путь
        # (product_TIMESTAMP_HASH/wb_main__card_N.jpg), содержимое не меняется
        # после генерации. immutable на 30 дней = WebView и браузер берут из
        # локального кеша при возврате к чату (без сетевого raundтрипа), скролл
        # и переключение между чатами становятся мгновенными. Retention выше
        # синхронизирован: cleanup удаляет файлы старше 30 дней — кэш и диск
        # истекают одновременно, broken-images не накапливаются.
        resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    elif path in ("/", "/landing", "/checkout") or path == "/api/example":
        # HTML/демо — ВСЕГДА ревалидируем, но разрешаем кэш с ETag: повторный
        # заход отдаёт 304 (пустое тело) вместо повторной выкачки ~670КБ SPA →
        # быстрый старт приложения и сайта. Свежие правки подхватываются сразу:
        # ETag меняется при изменении файла → сервер отдаёт новый 200.
        # (раньше тут стоял no-store — он запрещал кэш вовсе и заставлял
        #  перекачивать весь index.html при каждом запуске → «долгая загрузка».)
        resp.headers["Cache-Control"] = "no-cache"
    elif path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # ETag-ревалидация: повторный заход с If-None-Match получает 304 (без тела)
    # вместо повторной выкачки ~670КБ. ETag = mtime+size → меняется при деплое,
    # поэтому свежий index.html отдаётся сразу новым 200.
    html_path = BASE / "static" / "index.html"
    etag = None
    try:
        st = html_path.stat()
        etag = f'W/"{int(st.st_mtime)}-{st.st_size}"'
    except OSError:
        pass
    if etag:
        inm = request.headers.get("if-none-match", "")
        if inm and any(etag == tok.strip() for tok in inm.split(",")):
            return Response(status_code=304, headers={"ETag": etag})
    html = html_path.read_text(encoding="utf-8")
    return HTMLResponse(html, headers=({"ETag": etag} if etag else None))


@app.get("/landing", response_class=HTMLResponse)
async def landing():
    html_path = BASE / "static" / "landing.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/checkout", response_class=HTMLResponse)
async def checkout():
    html_path = BASE / "static" / "checkout.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/profile", response_class=HTMLResponse)
async def profile():
    html_path = BASE / "static" / "profile.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/pay", response_class=HTMLResponse)
async def pay_page():
    """Mock hosted payment page. A real PSP would host this; the confirm flow on
    the backend is identical, so swapping in ЮKassa/Robokassa is a config change."""
    html_path = BASE / "static" / "pay.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/legal", response_class=HTMLResponse)
async def legal_page():
    """Политика конфиденциальности, согласие на обработку ПДн (152-ФЗ), оферта."""
    html_path = BASE / "static" / "legal.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


def _request_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.client.host if req.client else ""


# ─────────────────────────────────────────────────────────────────────────────
# Rate-limiter + anti-DDoS
# ─────────────────────────────────────────────────────────────────────────────
# In-memory sliding window per (ip, endpoint). Без внешних либ — для масштаба
# до нескольких десятков RPS этого достаточно. При переходе на multi-worker
# деплой (uvicorn --workers N) — заменить на Redis.
#
# Лимиты подобраны по стоимости:
#   detect-category    → vision-вызов модели (дорогой)     →  6/мин
#   refine-search      → text-only LLM (дешёвый)            → 20/мин
#   search-competitors → парсинг WB (наши прокси, дорого) → 12/мин
#   analyze-competitors→ vision-batch + LLM (самый дорогой)→  4/мин
#
# Глобальный flood guard: > 120 req/мин c одного IP → бан на 5 минут
# (защита от ботов / скриптов в while-true).
import threading as _threading_rl
from collections import deque as _deque_rl
from fastapi import HTTPException as _HTTPException_rl


class _RateLimiter:
    def __init__(self):
        self._buckets: dict[str, _deque_rl] = {}
        self._bans: dict[str, float] = {}   # ip → ban_until_monotonic
        self._lock = _threading_rl.Lock()

    def allow(self, key: str, max_count: int, window_seconds: float) -> tuple[bool, float]:
        """True если можно, иначе False + сколько секунд до следующего слота."""
        now = time.monotonic()
        with self._lock:
            dq = self._buckets.setdefault(key, _deque_rl())
            while dq and now - dq[0] > window_seconds:
                dq.popleft()
            if len(dq) >= max_count:
                retry_in = max(0.0, window_seconds - (now - dq[0]))
                return False, retry_in
            dq.append(now)
            return True, 0.0

    def is_banned(self, ip: str) -> tuple[bool, float]:
        with self._lock:
            until = self._bans.get(ip, 0.0)
            now = time.monotonic()
            if until > now:
                return True, until - now
            if until and until <= now:
                self._bans.pop(ip, None)
            return False, 0.0

    def ban(self, ip: str, duration_seconds: float):
        with self._lock:
            self._bans[ip] = time.monotonic() + duration_seconds


_rate_limiter = _RateLimiter()

# IP-whitelist (локальная разработка). На проде расширь через env или config.
_RL_WHITELIST = {"127.0.0.1", "::1", "localhost"}


def _rate_limit(endpoint: str, per_minute: int):
    """Возвращает FastAPI dependency, которая поднимает 429 при превышении.
    Использование: dependencies=[Depends(_rate_limit('detect_category', 6))]"""
    async def _dep(req: Request):
        ip = _request_ip(req) or "unknown"
        if ip in _RL_WHITELIST:
            return
        # 1) ban-check (юзер уже наглотался флуд-лимита)
        banned, ban_left = _rate_limiter.is_banned(ip)
        if banned:
            raise _HTTPException_rl(
                status_code=429,
                detail=f"Слишком много запросов. Подожди {int(ban_left) + 1} сек.",
                headers={"Retry-After": str(int(ban_left) + 1)},
            )
        # 2) global flood guard: 120 req/мин на ВСЁ → 5-минутный бан
        flood_ok, _ = _rate_limiter.allow(f"flood:{ip}", 120, 60)
        if not flood_ok:
            _rate_limiter.ban(ip, 300)
            logger.warning(f"[rate-limit] IP {ip} banned for 5 min (flood)")
            raise _HTTPException_rl(
                status_code=429,
                detail="Подозрительный флуд. Бан на 5 минут.",
                headers={"Retry-After": "300"},
            )
        # 3) endpoint-specific
        ok, retry_in = _rate_limiter.allow(f"{endpoint}:{ip}", per_minute, 60)
        if not ok:
            raise _HTTPException_rl(
                status_code=429,
                detail=f"Слишком быстро. Попробуй через {int(retry_in) + 1} сек.",
                headers={"Retry-After": str(int(retry_in) + 1)},
            )
    return _dep


def _is_admin_request(req: Request) -> bool:
    """Серверная проверка: вошедший админ + (опц.) IP из allowlist."""
    user = account_service.user_for_session(req.cookies.get(account_router.COOKIE_NAME))
    if user is None or not user["is_admin"]:
        return False
    allow = account_service.admin_ip_allowlist()
    if allow and _request_ip(req) not in allow:
        return False
    return True


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(req: Request):
    """Админ-панель СПРЯТАНА: для всех, кроме вошедшего администратора, отдаём
    обычный 404 (страница даже не раскрывает своё существование). Админ сначала
    входит на основном сайте, затем открывает /admin (или ссылку из профиля)."""
    if not _is_admin_request(req):
        return HTMLResponse(
            "<!doctype html><html lang=ru><head><meta charset=utf-8>"
            "<title>404 — страница не найдена</title></head>"
            "<body style='font-family:system-ui;text-align:center;padding:80px;color:#444'>"
            "<h1 style='font-size:48px;margin:0'>404</h1><p>Страница не найдена.</p>"
            "<p><a href='/' style='color:#4C29D3'>На главную</a></p></body></html>",
            status_code=404)
    return HTMLResponse((BASE / "static" / "admin.html").read_text(encoding="utf-8"))


@app.get("/reset", response_class=HTMLResponse)
async def reset_page():
    """Страница установки нового пароля по ссылке из письма (?token=…)."""
    html_path = BASE / "static" / "reset.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ─── SEO: robots.txt + sitemap.xml (домен берём из config.site_base_url) ─────

@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    base = account_service.site_base_url()
    sitemap = (f"{base}/sitemap.xml" if base else "/sitemap.xml")
    return (
        "User-agent: *\n"
        "Disallow: /profile\n"
        "Disallow: /checkout\n"
        "Disallow: /pay\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "Allow: /\n"
        f"Sitemap: {sitemap}\n"
    )


@app.get("/sitemap.xml")
async def sitemap_xml():
    base = account_service.site_base_url()
    pages = [("/", "1.0", "daily"), ("/landing", "0.9", "weekly")]
    items = "".join(
        f"<url><loc>{base}{p}</loc><changefreq>{cf}</changefreq><priority>{pr}</priority></url>"
        for p, pr, cf in pages
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f'{items}</urlset>')
    return Response(content=xml, media_type="application/xml")


# ─── Serve generated output files ───────────────────────────────────────────
#
# Defence in depth: require_user блокирует анонимный доступ, ownership-check
# по `gen_owner(pack_dir)` блокирует enumerate-атаку (16M комбинаций
# product_TIMESTAMP_xxxxxx brute-forceable за часы при открытом endpoint'е).
# Если файл принадлежит ДРУГОМУ юзеру → 404 (не 403, чтобы не палить факт
# существования). Для старых файлов без записи в БД (cleanup стёр запись,
# но не файл) — отдаём — это безопасно, т.к. файл вообще не известен снаружи.

def _pack_dir_from_output_path(path: str) -> Optional[str]:
    """'product_20260620_155311_c74a4e/wb_main__card_1.jpg' -> 'product_..._c74a4e'."""
    parts = (path or "").split("/", 1)
    head = parts[0] if parts else ""
    return head if head.startswith("product_") else None


def _make_thumb(src: Path, dst: Path, width: int) -> None:
    """Уменьшенная JPEG-копия src шириной width (если оригинал шире). Пишется
    один раз рядом с оригиналом и переиспользуется. Синхронная — вызывать через
    asyncio.to_thread, чтобы не блокировать event loop.

    АТОМАРНО: пишем во временный файл и os.replace в финальный путь. Иначе
    обрыв процесса (OOM/SIGTERM/диск) посреди save() оставлял бы битый .wN.jpg,
    который потом отдавался бы вечно (serve_output регенерит только если файла
    нет). При любой ошибке подчищаем temp."""
    from PIL import Image
    tmp = dst.with_name(dst.name + f".tmp{os.getpid()}")
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            if im.width > width:
                h = max(1, round(im.height * width / im.width))
                im = im.resize((width, h), Image.LANCZOS)
            im.save(tmp, "JPEG", quality=82, optimize=True)
        os.replace(tmp, dst)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


@app.get("/api/output/{path:path}")
async def serve_output(path: str, request: Request, user=Depends(account_router.require_user)):
    full = (BASE / "output" / path).resolve()
    if not str(full).startswith(str((BASE / "output").resolve())):
        return JSONResponse({"error": "forbidden"}, 403)
    if not full.is_file():
        return JSONResponse({"error": "not found"}, 404)
    # Ownership: pack_dir → gen_id (одно и то же — product_TS_HASH)
    pack = _pack_dir_from_output_path(path)
    if pack:
        owner = account_service.gen_owner(pack)
        if owner is not None and owner != user["id"]:
            return JSONResponse({"error": "not found"}, 404)
    # ?w=N → отдаём кэшированную миниатюру (карточки полноразмерные 2K, а в
    # сайдбаре/гриде мелкие — грузить целиком долго). Миниатюра кладётся рядом с
    # оригиналом (та же папка пака → та же ownership-проверка и авточистка).
    # Любая ошибка генерации → молча отдаём полный файл (выдача не ломается).
    w = request.query_params.get("w")
    if w and full.suffix.lower() in (".jpg", ".jpeg", ".png"):
        try:
            wi = max(64, min(1024, int(w)))
            thumb = full.parent / f"{full.stem}.w{wi}.jpg"
            if not thumb.is_file():
                await asyncio.to_thread(_make_thumb, full, thumb, wi)
            if thumb.is_file():
                return FileResponse(str(thumb))
        except Exception as e:
            logger.debug(f"thumb {path}?w={w} failed → full file: {e}")
    return FileResponse(str(full))


@app.get("/api/tmp/{path:path}")
async def serve_tmp(path: str, user=Depends(account_router.require_user)):
    full = (BASE / "tmp" / path).resolve()
    if not str(full).startswith(str((BASE / "tmp").resolve())):
        return JSONResponse({"error": "forbidden"}, 403)
    if not full.is_file():
        return JSONResponse({"error": "not found"}, 404)
    return FileResponse(str(full))


# ─── Upload (for style refs and product photos) ────────────────────────────
#
# Защиты:
#   - require_user → блокирует анонимный DoS-аплоад
#   - rate-limit → 20/мин (выше чем detect_category — юзер может загружать
#     несколько фоток сразу при первом контакте, но не сотни)
#   - whitelist расширений + per-file size cap (7 MB как у detect-category) +
#     лимит на количество файлов в одном запросе (10 шт)
#   - проверка через .read(_MAX_PHOTO_BYTES+1) → не грузим в RAM гигабайт
_MAX_UPLOAD_FILES = 10

@app.post("/api/upload",
          dependencies=[Depends(_rate_limit("upload", 20))])
async def upload_files(
    files: list[UploadFile] = File(...),
    user=Depends(account_router.require_user),
):
    if len(files) > _MAX_UPLOAD_FILES:
        return JSONResponse(
            {"error": f"too_many_files (max {_MAX_UPLOAD_FILES})"}, 413
        )
    saved = []
    rejected = []
    for f in files:
        raw_name = f.filename or "img.jpg"
        ext = Path(raw_name).suffix.lower() or ".jpg"
        if ext not in _ALLOWED_PHOTO_EXT:
            rejected.append({"name": raw_name, "reason": f"формат «{ext}» не поддерживается"})
            continue
        # читаем с capped размером — на 1 байт больше лимита, чтобы детектить overflow
        data = await f.read(_MAX_PHOTO_BYTES + 1)
        if not data:
            rejected.append({"name": raw_name, "reason": "пустой файл"})
            continue
        if len(data) > _MAX_PHOTO_BYTES:
            rejected.append({
                "name": raw_name,
                "reason": f"больше {_MAX_PHOTO_BYTES // (1024*1024)} MB",
            })
            continue
        dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
        with open(dest, "wb") as out:
            out.write(data)
        saved.append(str(dest))
    return {"paths": saved, "rejected": rejected}


# ─── 1. Detect category from photos ────────────────────────────────────────

_ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".heif"}
_MAX_PHOTO_BYTES = 7 * 1024 * 1024  # 7MB — старое 20MB забивало память/трафик; image-API всё равно даунскейлит

@app.post("/api/detect-category",
          dependencies=[Depends(_rate_limit("detect_category", 6))])
async def detect_category(
    req: Request,
    photos: list[UploadFile] = File(...),
    brief: str = Form(""),
):
    saved = []
    rejected = []   # (filename, reason) — то что не приняли, юзеру в ответ
    for photo in photos:
        raw_name = (photo.filename or "img.jpg")
        ext = Path(raw_name).suffix.lower() or ".jpg"
        # Серверная сторона страхует фронт: даже если фронт пропустил битый
        # файл, бэкенд НЕ молча его сохраняет, а возвращает ошибку.
        if ext not in _ALLOWED_PHOTO_EXT:
            rejected.append((raw_name, f"формат «{ext or '?'}» не поддерживается"))
            continue
        data = await photo.read()
        if not data:
            rejected.append((raw_name, "пустой файл"))
            continue
        if len(data) > _MAX_PHOTO_BYTES:
            mb = len(data) / (1024 * 1024)
            limit_mb = _MAX_PHOTO_BYTES // (1024 * 1024)
            rejected.append((raw_name, f"{mb:.1f} МБ — больше лимита {limit_mb} МБ"))
            continue
        if len(data) < 1024:
            rejected.append((raw_name, f"подозрительно мал ({len(data)} байт)"))
            continue
        dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
        try:
            with open(dest, "wb") as f:
                f.write(data)
        except Exception as e:
            rejected.append((raw_name, f"не удалось сохранить ({type(e).__name__})"))
            continue
        saved.append(str(dest))

    # Жёсткий потолок: не более 4 фото товара в детект. Фронт уже ограничивает,
    # это страховка от прямых POST — бережёт латентность и токены vision-вызова.
    if len(saved) > 4:
        saved = saved[:4]

    if not saved:
        reasons = "; ".join(f"{n}: {r}" for n, r in rejected) or "не передано ни одного файла"
        return {
            "error": f"Не удалось принять фото — {reasons}",
            "category": "", "category_wb": "",
            "search_query_specific": "", "search_query_broad": "",
            "product_description": "", "product_title_short": "",
            "brand_detected": "", "brand_is_product": False,
            "confidence": "", "quick_facts": [], "photo_paths": [],
        }

    # Привязка вызовов LLM к юзеру для статистики токенов в админке.
    user = account_service.user_for_session(req.cookies.get(account_router.COOKIE_NAME))
    user_id = user["id"] if user else None
    # Раньше слали 1 фото ради экономии: на СТАРОМ gpt-4o-mini одно high-detail фото
    # = ~37k токенов (×33-множитель), 4 фото вообще пробивали лимит 128k. Сейчас
    # vision на gpt-4.1-mini (~2.5k токенов/фото) — все ≤4 фото стоят копейки и не
    # тормозят, поэтому отдаём ВСЕ. Это важно для качества: вторичные кадры (оборот
    # этикетки, доп. ракурсы, мелкие спеки) дают факты, которых НЕТ на главном фото,
    # иначе детект переспрашивал бы в quick_facts то, что видно на обороте.
    detect_photos = saved          # saved уже ограничен 4 фото выше
    def _detect_with_ctx():
        G.set_llm_context(user_id=user_id, purpose="detect_category")
        try:
            return A.detect_category_from_photos(detect_photos, brief, CFG)
        finally:
            G.clear_llm_context()
    result = await asyncio.to_thread(_detect_with_ctx)
    result["photo_paths"] = saved
    if rejected:
        # Часть приняли, часть нет — сообщаем в notes, но не блокируем
        result["rejected_photos"] = [{"file": n, "reason": r} for n, r in rejected]
    return result


# ─── 1b. Refine search query (после ответов на quick_facts) ────────────────

@app.post("/api/refine-search-query",
          dependencies=[Depends(_rate_limit("refine_query", 20))])
async def refine_search_query(req: Request):
    """
    Принимает контекст (title, brand, brand_is_important, answers, quick_facts,
    user_free_text) и возвращает {query_specific, query_broad} — естественный
    WB-запрос, собранный LLM как живой человек.
    """
    body = await req.json()
    result = await asyncio.to_thread(
        A.refine_search_query,
        body.get("title", ""),
        body.get("brand", ""),
        bool(body.get("brand_is_important", False)),
        body.get("answers") or {},
        body.get("quick_facts") or [],
        body.get("user_free_text", ""),
        CFG,
    )
    return result


# ─── 2. Search competitors on WB ───────────────────────────────────────────

# Кэш РЕЗУЛЬТАТА поиска конкурентов ПО ЗАПРОСУ (не по юзеру!). Парсинг WB — самый
# медленный и флапающий шаг; для одного запроса («механическая клавиатура RGB»)
# выдача у всех одинаковая и стабильна часами. Кэшируем ТОЛЬКО публичную выдачу WB
# по нормализованному запросу+регионе. Синтез под конкретный товар
# (/analyze-competitors, с user_facts) НЕ кэшируется никогда → перепутать данные
# разных юзеров физически нельзя. Потокобезопасно, с TTL и потолком; любой сбой
# кэша проглатывается и на запрос не влияет.
import threading as _threading_cc
_COMP_CACHE: dict = {}
_COMP_CACHE_LOCK = _threading_cc.Lock()
_COMP_CACHE_TTL = 6 * 3600        # 6 часов
_COMP_CACHE_MAX = 500             # потолок числа закэшированных запросов


def _comp_cache_key(q_specific: str, q_broad: str, limit, dest) -> tuple:
    def _norm(s):
        return " ".join((s or "").strip().lower().split())
    try:
        lim = int(limit or 5)
    except Exception:
        lim = 5
    return (_norm(q_specific), _norm(q_broad), lim, str(dest or ""))


def _comp_cache_get(key):
    try:
        now = time.monotonic()
        with _COMP_CACHE_LOCK:
            ent = _COMP_CACHE.get(key)
            if ent and ent[0] > now:
                return ent[1]
            if ent:
                _COMP_CACHE.pop(key, None)
    except Exception:
        pass
    return None


def _comp_cache_put(key, value):
    try:
        now = time.monotonic()
        with _COMP_CACHE_LOCK:
            for k in [k for k, v in _COMP_CACHE.items() if v[0] <= now]:
                _COMP_CACHE.pop(k, None)
            if len(_COMP_CACHE) >= _COMP_CACHE_MAX:
                for k, _ in sorted(_COMP_CACHE.items(), key=lambda kv: kv[1][0])[:100]:
                    _COMP_CACHE.pop(k, None)
            _COMP_CACHE[key] = (now + _COMP_CACHE_TTL, value)
    except Exception:
        pass


@app.post("/api/search-competitors",
          dependencies=[Depends(_rate_limit("search_competitors", 12))])
async def search_competitors(req: Request):
    """
    Ответ всегда HTTP 200. Поля:
      success: bool
      error_type: null | "WB_UNAVAILABLE" | "NO_RESULTS"
      error_message: str (если success=false)
      products: [...]   (всегда массив, м.б. пустой)
      can_continue_without_wb: bool — UI решает показывать модалку

    UI обрабатывает success=false + error_type="WB_UNAVAILABLE":
      → показать модалку "Парсинг WB временно не работает. Продолжить
        генерацию карточки без анализа конкурентов?" [Да] [Нет — подождать]
    """
    body = await req.json()
    q_specific = body.get("query_specific", "")
    q_broad = body.get("query_broad", "")
    limit = body.get("limit", 5)

    parser = get_wb_parser()

    # Кэш по запросу: одинаковая выдача WB → мгновенный ответ без повторного
    # парсинга (быстрее + не долбим WB). Только публичные данные, без юзер-контекста.
    ckey = _comp_cache_key(q_specific, q_broad, limit, parser.dest)
    cached = _comp_cache_get(ckey)
    if cached is not None:
        return {**cached, "cached": True}

    result = await asyncio.to_thread(
        A.smart_search_competitors, q_specific, q_broad, limit, parser.dest, parser
    )

    products_json = []
    for p in result.get("products", []):
        d = asdict(p) if hasattr(p, "__dataclass_fields__") else p.__dict__.copy()
        d["photos"] = d.get("photos", [])[:6]
        products_json.append(d)

    error_type = result.get("error_type")
    success = bool(products_json) and error_type is None

    resp = {
        "success": success,
        "error_type": error_type,
        "error_message": result.get("error") if error_type else None,
        "can_continue_without_wb": error_type == "WB_UNAVAILABLE",
        "products": products_json,
        "query_used": result.get("query_used", q_specific),
        "fallback_used": result.get("fallback_used", False),
        "raw_count": result.get("raw_count", len(products_json)),
    }
    # Кэшируем ТОЛЬКО успешную непустую выдачу — транзиентные сбои WB (пустота/
    # WB_UNAVAILABLE) не кэшируются, чтобы не «залипла» ошибка для всех.
    if success and products_json:
        _comp_cache_put(ckey, resp)
    return resp


@app.get("/api/wb-status")
async def wb_status():
    """Health-check для WB smart-stack. Удобно для мониторинга."""
    try:
        parser = get_wb_parser()
        snap = parser.smart_client.state_snapshot() if parser.smart_client else None
        return {
            "smart_enabled": parser.smart_client is not None,
            "state": snap,
        }
    except Exception as e:
        return {"smart_enabled": False, "error": str(e)}


# ─── WB image proxy ─────────────────────────────────────────────────────────
# Превью-фото конкурентов (basket-XX.wbbasket.ru) браузер тянет КРОСС-доменно с
# нашего origin. WB CDN без Referer https://www.wildberries.ru/ периодически
# отдаёт 403 / пустоту → в карточке пустой квадрат. Плюс номер basket иногда
# «уезжает» (sharding) и прямой URL даёт 404. Прокси качает фото на сервере с
# правильными заголовками и, если basket не совпал, перебирает соседние хосты —
# поэтому превью грузятся стабильно. Кэш у клиента — неделя.
import re as _re_wbimg
from urllib.parse import urlsplit as _urlsplit_wbimg, urlunsplit as _urlunsplit_wbimg

_WB_IMG_HOST_RE = _re_wbimg.compile(r"^basket-(\d{1,2})\.wbbasket\.ru$", _re_wbimg.IGNORECASE)
_WB_IMG_SCAN_CAP = 10             # максимум хостов-кандидатов при 404 (bounded latency)
_WB_IMG_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}


def _is_wb_img_url(url: str):
    """SSRF-guard: вернуть (scheme, netloc, path, query) только для https
    basket-NN.wbbasket.ru, иначе None."""
    try:
        p = _urlsplit_wbimg(url)
    except Exception:
        return None
    if p.scheme != "https" or not _WB_IMG_HOST_RE.match(p.netloc):
        return None
    return p


def _fetch_wb_image(url: str):
    """Синхронно (в to_thread): тянет фото WB c правильным Referer; при 404
    перебирает basket-хосты. Возвращает (bytes, content_type) или None."""
    import requests
    p = _is_wb_img_url(url)
    if p is None:
        return None
    m = _WB_IMG_HOST_RE.match(p.netloc)
    cur = int(m.group(1))
    # nearest-first: вычисленный basket, затем соседи (cur±1, cur±2 …). WB при
    # шардинге перемещает товар в БЛИЗКИЙ basket, поэтому соседей хватает, а cap
    # держит worst-case латентность для реально отсутствующих фото ограниченной.
    order = [cur]
    for d in range(1, _WB_IMG_SCAN_CAP):
        for nb in (cur - d, cur + d):
            if 1 <= nb <= 60 and nb not in order:
                order.append(nb)
    order = order[:_WB_IMG_SCAN_CAP]
    for b in order:
        host = f"basket-{b:02d}.wbbasket.ru"
        cand = _urlunsplit_wbimg((p.scheme, host, p.path, p.query, ""))
        try:
            r = requests.get(cand, headers=_WB_IMG_HEADERS, timeout=(4, 12))
        except requests.RequestException:
            continue
        if r.status_code == 200 and r.content:
            ct = r.headers.get("Content-Type", "") or ""
            if not ct.startswith("image/"):
                ct = "image/webp"
            return (r.content, ct)
        if r.status_code in (403, 429):
            # хост есть, но режет — перебор соседей не поможет
            break
        # 404 → возможно basket «уехал», пробуем следующий хост
    return None


@app.get("/api/wb-img")
async def wb_image_proxy(u: str = ""):
    """Прокси превью-фото конкурентов WB. SSRF-guard: только *.wbbasket.ru."""
    url = (u or "").strip()
    if _is_wb_img_url(url) is None:
        return Response(status_code=400)            # чужой хост — не проксируем
    try:
        res = await asyncio.to_thread(_fetch_wb_image, url)
    except Exception:
        res = None
    if res is None:
        # graceful: пусть браузер попробует исходный URL напрямую
        return RedirectResponse(url, status_code=302)
    content, ct = res
    return Response(
        content=content,
        media_type=ct,
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


# ─── 3. Analyze competitors ────────────────────────────────────────────────

@app.post("/api/analyze-competitors",
          dependencies=[Depends(_rate_limit("analyze_competitors", 4))])
async def analyze_competitors(req: Request):
    body = await req.json()
    products_raw = body.get("products", [])
    card_count = body.get("card_count", 6)
    # Контекст пользователя: фронт собирает quick_facts ответы + бренд +
    # свободный текст. Без этого card_recipe строил план «по типичной нише»
    # и подставлял выдуманные числа (например «4.3 л», «срок 36 мес»).
    user_facts = body.get("user_facts") or {}
    user_brief = (body.get("brief") or "").strip()
    user_title = (body.get("title") or "").strip()
    user_category = (body.get("category") or "").strip()

    products = []
    for p in products_raw:
        products.append(Product(
            nm_id=p.get("nm_id", 0),
            name=p.get("name", ""),
            brand=p.get("brand"),
            price=p.get("price"),
            rating=p.get("rating"),
            feedbacks=p.get("feedbacks"),
            description=p.get("description"),
            characteristics=p.get("characteristics", []),
            photos=p.get("photos", []),
            photo_count=p.get("photo_count", 0),
            url=p.get("url", ""),
            supplier=p.get("supplier"),
        ))

    # Джанитор: папки competitors_* раньше копились бессрочно (каждый анализ —
    # до 50 фото на диск). Сносим старше 24ч — фронту фото нужны только в рамках
    # текущей сессии подбора. Best-effort, ошибка уборки не мешает анализу.
    def _sweep_old_competitor_dirs(max_age_h: int = 24):
        import shutil, time as _t
        cutoff = _t.time() - max_age_h * 3600
        try:
            for d in (BASE / "tmp").glob("competitors_*"):
                try:
                    if d.is_dir() and d.stat().st_mtime < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    continue
        except Exception:
            pass
    await asyncio.to_thread(_sweep_old_competitor_dirs)

    save_dir = str(BASE / "tmp" / f"competitors_{uuid.uuid4().hex[:8]}")
    result = await asyncio.to_thread(
        A.analyze_competitors, products, CFG,
        card_count=card_count, include_photos=True,
        save_photos_dir=save_dir,
        user_facts=user_facts, user_brief=user_brief,
        user_title=user_title, user_category=user_category,
    )

    if "competitors_summary" in result:
        for cs in result["competitors_summary"]:
            paths = cs.get("photo_paths", [])
            cs["photo_urls"] = [
                f"/api/tmp/{Path(p).relative_to(BASE / 'tmp').as_posix()}"
                for p in paths if Path(p).is_file()
            ]

    return result


# ─── 3.25. Buyer questions (dynamic quick_facts after competitor analysis) ──
#
# После того как юзер прошёл первый круг quick_facts (по фото) и спарсились
# конкуренты + analyze — модель уже видит нишу и знает что в ней решает покупку.
# Этот эндпоинт возвращает 3-5 ДОПОЛНИТЕЛЬНЫХ категорийно-специфичных вопросов
# в формате, совместимом с quick_facts от /api/detect-product (key/label/options/
# multi_select/allow_custom). Фронт мёрджит их в state._detectResult.quick_facts
# и рендерит теми же ud-section чипсами, что и базовые вопросы. Нового шага в
# UX не плодим — это вторая панель внутри шага 2.

@app.post("/api/buyer-questions",
          dependencies=[Depends(_rate_limit("buyer_questions", 8))])
async def buyer_questions_endpoint(req: Request):
    body = await req.json()
    category = (body.get("category") or "").strip()
    brief = (body.get("brief") or "").strip()
    competitor_analysis = body.get("competitor_analysis") or {}
    already_answered = body.get("already_answered") or {}
    # existing_questions = всё, о чём юзер уже спрашивается на UI (даже без ответа).
    # Нужно чтобы модель не дублировала тематически: «ОБЪЕМ» (детект) + «Какой
    # объём кружки?» (наш) = баг. Промпт делает thematic dedup по этим заголовкам.
    existing_questions = body.get("existing_questions") or []
    if not isinstance(existing_questions, list):
        existing_questions = []

    system_p = P.BUYER_QUESTIONS_SYSTEM_PROMPT
    user_p = P.build_buyer_questions_user_prompt(
        category, brief, competitor_analysis, already_answered,
        existing_questions=existing_questions,
    )

    try:
        text = await asyncio.to_thread(
            G._claude_chat, CFG, system_p, user_p,
            max_tokens=900, temperature=0.5,
            retries_per_model=1,
            # Свой purpose: (1) честный разрез в llm_usage (раньше маскировался под
            # "design"), (2) тир: ключа "buyer_questions" в purpose_tier обычно нет →
            # _endpoints_for_purpose падает на llm_default_tier (balanced, gpt-4.1-mini)
            # вместо smart gpt-4.1 — для 2-4 вопросов этого более чем достаточно.
            purpose="buyer_questions",
        )
        data = P.extract_json(text)
    except Exception as e:
        logger.exception("buyer_questions: LLM call failed")
        return JSONResponse(status_code=200, content={
            "success": False, "error": str(e)[:200], "buyer_questions": []
        })

    raw = (data or {}).get("buyer_questions") or []
    if not isinstance(raw, list):
        return {"success": True, "buyer_questions": []}

    # Нормализация под фронтовый формат quick_facts. Любая невалидная запись →
    # пропуск, чтобы фронт не упал на render и ничего не сбило state.
    seen_keys = set(str(k).lower() for k in (already_answered or {}).keys())
    clean = []
    for q in raw[:6]:
        if not isinstance(q, dict):
            continue
        key = str(q.get("key") or "").strip()
        label = str(q.get("label") or "").strip()
        if not key or not label:
            continue
        if key.lower() in seen_keys or label.lower() in seen_keys:
            continue  # модель умудрилась продублировать уже отвеченное
        opts_in = q.get("options")
        opts = []
        if isinstance(opts_in, list):
            for o in opts_in[:6]:
                s = str(o).strip()
                if s:
                    opts.append(s[:60])
        clean.append({
            "key": key[:40],
            "label": label[:90],
            "options": opts,
            "multi_select": bool(q.get("multi_select", False)),
            "allow_custom": bool(q.get("allow_custom", True)),
        })

    return {"success": True, "buyer_questions": clean}


# ─── 3.5. Plan cards (text-preview before image render) ──────────────────
# Bug 3 + 6: новый этап. Юзер видит ТЕКСТЫ карточек (headline / sub / badges)
# ДО того как тратятся генерации фоток. Может одобрить, поправить inline или
# попросить пересоздать план.

_PLAN_CACHE: dict[str, dict] = {}   # plan_id → prepared (от generator._prepare_design)
_PLAN_CACHE_LOCK = __import__("threading").Lock()
_PLAN_CACHE_TTL = 30 * 60           # 30 минут — потом дроп

def _evict_old_plans():
    """Удаляет планы старше _PLAN_CACHE_TTL. Вызывается перед каждой записью."""
    import time as _t
    now = _t.monotonic()
    with _PLAN_CACHE_LOCK:
        dead = [pid for pid, p in _PLAN_CACHE.items()
                if now - p.get("_cached_at", now) > _PLAN_CACHE_TTL]
        for pid in dead:
            _PLAN_CACHE.pop(pid, None)


@app.post("/api/plan-cards",
          dependencies=[Depends(_rate_limit("plan_cards", 6))])
async def plan_cards(req: Request):
    """
    Делает только design phase (Claude). Возвращает текст карточек:
      {plan_id, cards: [{id, type, headline_ru, sub_ru, badges_ru, format_key}],
       concept, palette, inferred_product}
    Прокинуть plan_id в /api/generate-cards чтобы рендерить ИМЕННО этот план
    (опционально с правками юзера в card_overrides).
    """
    body = await req.json()
    ref_paths = body.get("ref_paths", [])
    brief = body.get("brief", "")
    competitor_analysis = body.get("competitor_analysis", {})
    formats_selected = body.get("formats_selected", ["wb_main"])
    # Кламп 1..10: план — это полноценный design-вызов на дорогой модели; раньше
    # эндпоинт был без rate-limit и без потолка card_count — прямой POST мог
    # заказать план на 50 карточек бесплатно (биллинга на этапе плана нет).
    try:
        card_count = int(body.get("card_count", 6))
    except (TypeError, ValueError):
        card_count = 6
    card_count = max(1, min(card_count, 10))
    # На бесплатной пробе план содержит ровно 1 карточку (как и сама генерация).
    # Условие зеркалит /api/generate-cards: форсим 1 ТОЛЬКО без платного баланса —
    # иначе платник с непотраченной пробой видел план из 1 карточки, а рендер из N.
    plan_user = account_service.user_for_session(req.cookies.get(account_router.COOKIE_NAME))
    if (plan_user is not None and account_service._free_available(plan_user)
            and int(plan_user["balance_cards"] or 0) <= 0):
        card_count = 1
    style_key = body.get("style_key", "premium")
    category = body.get("category_confirmed", "")
    style_ref_paths = body.get("style_ref_paths", [])
    image_provider = body.get("image_provider", CFG.get("default_provider", "veononstop"))
    user_facts = body.get("user_facts") or {}

    logger.info(f"/api/plan-cards: brief={brief[:60]!r} card_count={card_count} "
                f"facts={list((user_facts or {}).keys())}")

    try:
        prepared = await asyncio.to_thread(
            G._prepare_design,
            CFG,
            "competitor",
            (brief or "", formats_selected, card_count,
             competitor_analysis or {}, category or ""),
            style_key,
            ref_paths,
            style_ref_paths,
            None,           # palette_override
            image_provider,
            CFG.get("default_provider", "fastgen_flower"),  # video_provider
            False,          # write_description (на этапе плана не нужно)
            False,          # generate_videos
            formats_selected,
            "auto",         # video_effect
            user_facts,
        )
    except Exception as e:
        logger.exception("plan_cards: design phase failed")
        return JSONResponse(status_code=200, content={
            "success": False,
            "error": str(e),
            "cards": [],
        })

    import time as _t, uuid as _uuid
    plan_id = _uuid.uuid4().hex
    prepared["_cached_at"] = _t.monotonic()
    _evict_old_plans()
    with _PLAN_CACHE_LOCK:
        _PLAN_CACHE[plan_id] = prepared

    design = prepared.get("design") or {}
    cards = []
    for c in (design.get("cards") or []):
        cards.append({
            "id": c.get("id", ""),
            "type": c.get("type", "main"),
            "format_key": c.get("format_key", ""),
            "headline_ru": c.get("headline_ru") or "",
            "sub_ru": c.get("sub_ru") or "",
            "badges_ru": c.get("badges_ru") or [],
        })

    # ── Маркетинговые сигналы из ШАГА 0 промпта (mind_state / buyer_questions /
    #    niche_voice / facts_translated / missing_critical_facts). Прокидываем на
    #    фронт, чтобы UI мог мягко подсветить пробелы по фактам и помочь юзеру
    #    дополнить бриф без отдельного шага.
    def _clean_list(val, limit_each=120, max_items=8):
        out = []
        if isinstance(val, list):
            for x in val[:max_items]:
                if x is None:
                    continue
                s = str(x).strip()
                if not s:
                    continue
                out.append(s[:limit_each])
        return out

    return {
        "success": True,
        "plan_id": plan_id,
        "concept": design.get("concept") or "",
        "palette": prepared.get("palette") or {},
        "inferred_product": prepared.get("inferred") or None,
        "cards": cards,
        "mind_state": (design.get("_mind_state") or "").strip() or None,
        "buyer_questions": _clean_list(design.get("_buyer_questions"), limit_each=140, max_items=6),
        "niche_voice": (design.get("_niche_voice") or "").strip() or None,
        "missing_critical_facts": _clean_list(design.get("missing_critical_facts"), limit_each=40, max_items=6),
    }


# ─── 4. Generate cards (server-side background JOB, resumable) ──────────────
#
# Генерация выполняется в фоновом потоке на сервере и переживает уход юзера со
# страницы / перезагрузку: состояние пишется в БД (таблица generations), а живые
# обновления раздаются подписчикам по SSE. Юзер может вернуться в чат и забрать
# результат (см. /api/generation/*). Биллинг: 1 карточка за генерацию, возврат
# при 0 доставленных дизайнов.

import threading as _threading
import queue as _queue
import time as _time

_GEN_JOBS: dict[str, "GenJob"] = {}
_GEN_JOBS_LOCK = _threading.Lock()
_GEN_JOB_TTL = 30 * 60   # держим завершённую джобу в памяти 30 мин для мгновенного reconnect


class GenJob:
    def __init__(self, gid: str, user_id: int, card_count: int, title: str):
        self.id = gid
        self.user_id = user_id
        self.card_count = max(1, int(card_count))
        self.title = title
        self.status = "running"          # running | done | error
        self.progress = 4
        self.caption = "Запускаю…"
        self.files: list[str] = []
        self.file_set: set[str] = set()
        self.description = ""
        self.design = None
        self.error = None
        self.charged = 0
        self.charge_mode = "free"
        self.subs: list = []
        self.lock = _threading.Lock()
        self.finished_at = None

    def snapshot_frame(self) -> dict:
        return {"files": list(self.files), "description": self.description,
                "design": self.design, "progress": self.progress, "caption": self.caption,
                "status": self.status, "done": self.status != "running", "error": self.error}

    def broadcast(self, item) -> None:
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(item)
                except Exception:
                    pass

    def subscribe(self):
        q = _queue.Queue()
        with self.lock:
            self.subs.append(q)
            q.put(("data", self.snapshot_frame()))   # сразу отдаём текущее состояние (replay)
            if self.status != "running":
                q.put(("end", None))
        return q

    def unsubscribe(self, q) -> None:
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)


def _evict_gen_jobs() -> None:
    now = _time.monotonic()
    with _GEN_JOBS_LOCK:
        dead = [g for g, j in _GEN_JOBS.items()
                if j.finished_at and now - j.finished_at > _GEN_JOB_TTL]
        for g in dead:
            _GEN_JOBS.pop(g, None)


def _run_generation_job(job: "GenJob", params: dict) -> None:
    plan_id = params.get("plan_id")
    card_overrides = params.get("card_overrides") or {}

    # Привязываем все Claude/OpenAI вызовы этого потока к конкретному юзеру и
    # generation_id, чтобы в админ-статистике токенов видно было кто их потратил
    # и в рамках какой генерации.
    G.set_llm_context(user_id=job.user_id, purpose="design", generation_id=job.id,
                      img_policy=params.get("image_policy"))

    def gen_sync():
        prepared_cached = None
        if plan_id:
            with _PLAN_CACHE_LOCK:
                prepared_cached = _PLAN_CACHE.get(plan_id)
        if prepared_cached and card_overrides:
            cards = (prepared_cached.get("design") or {}).get("cards") or []
            for c in cards:
                ov = card_overrides.get(c.get("id"))
                if not ov:
                    continue
                if "headline_ru" in ov: c["headline_ru"] = ov["headline_ru"]
                if "sub_ru" in ov:      c["sub_ru"] = ov["sub_ru"]
                if "badges_ru" in ov and isinstance(ov["badges_ru"], list):
                    c["badges_ru"] = ov["badges_ru"]
                ip = c.get("image_prompt") or ""
                orig_head = ov.get("_orig_head"); orig_sub = ov.get("_orig_sub")
                if orig_head and ov.get("headline_ru") and orig_head in ip:
                    ip = ip.replace(orig_head, ov["headline_ru"])
                if orig_sub and ov.get("sub_ru") and orig_sub in ip:
                    ip = ip.replace(orig_sub, ov["sub_ru"])
                c["image_prompt"] = ip
        if prepared_cached:
            for partial in G._stream_render_from_prepared(CFG, prepared_cached):
                yield partial
            return
        for partial in G.generate_pack_competitor_streaming(
            ref_paths=params["ref_paths"], brief=params["brief"],
            competitor_analysis=params["competitor_analysis"],
            formats_selected=params["formats_selected"], card_count=job.card_count,
            style_key=params["style_key"], category_confirmed=params["category"],
            image_provider=params["image_provider"], style_ref_paths=params["style_ref_paths"],
            write_description=params["write_description"], generate_videos=False,
            user_facts=params["user_facts"], cfg=CFG,
        ):
            yield partial

    try:
        for partial in gen_sync():
            if partial.get("heartbeat"):
                if partial.get("info"):
                    job.caption = partial["info"]
                job.broadcast(("data", {"info": partial.get("info") or "", "heartbeat": True,
                                        "progress": job.progress, "caption": job.caption}))
                continue
            info = partial.get("info", "")
            if "проектирует" in info or "Claude" in info:
                job.progress = max(job.progress, 12); job.caption = "Проектирую пак карточек…"
            new_files = []
            for f in partial.get("files", []):
                fp = Path(f)
                if fp.is_file():
                    try:
                        rel = fp.relative_to(BASE / "output")
                        url = f"/api/output/{rel.as_posix()}"
                    except ValueError:
                        url = str(fp)
                    if url not in job.file_set:
                        job.file_set.add(url); job.files.append(url); new_files.append(url)
            if job.files:
                frac = min(1.0, len(job.files) / job.card_count)
                job.progress = max(job.progress, int(18 + frac * 70))
                job.caption = "Рисую карточки…"
            if partial.get("design"):
                job.design = partial["design"]
            if partial.get("description"):
                job.description = partial["description"]
                job.progress = max(job.progress, 92); job.caption = "Пишу описание…"
            job.broadcast(("data", {
                "files": new_files, "description": partial.get("description", ""),
                "info": info, "design": partial.get("design"),
                "progress": job.progress, "caption": job.caption, "done": False,
            }))
            try:
                account_service.gen_update(job.id, progress=job.progress, caption=job.caption,
                                           files=job.files, description=job.description or None,
                                           design=job.design)
            except Exception:
                logger.exception("gen_update failed")

        job.status = "done"; job.progress = 100; job.caption = "Готово"
        account_service.gen_update(job.id, status="done", progress=100, caption="Готово",
                                   files=job.files, description=job.description or None,
                                   design=job.design)
        job.broadcast(("data", {"files": [], "progress": 100, "caption": "Готово",
                                "done": True, "description": job.description}))
        job.broadcast(("end", None))
    except Exception as e:
        logger.exception("generation job failed")
        job.status = "error"; job.error = str(e)
        try:
            account_service.gen_update(job.id, status="error", error=str(e))
        except Exception:
            pass
        job.broadcast(("error", str(e))); job.broadcast(("end", None))
    finally:
        job.finished_at = _time.monotonic()
        try:
            if len(job.files) == 0:   # ничего не отдали — возвращаем карточку
                account_service.refund_generation(job.user_id, job.charged, job.charge_mode)
                logger.info(f"generation {job.id}: карточка возвращена (0 дизайнов)")
        except Exception:
            logger.exception("refund failed")


@app.post("/api/generate-cards")
async def generate_cards(req: Request):
    """Создаёт фоновую генерацию и сразу возвращает {generation_id}. Прогресс/
    результат забираются через /api/generation/{id}/stream (SSE) или /snapshot."""
    body = await req.json()

    session_token = req.cookies.get(account_router.COOKIE_NAME)
    biller_user = account_service.user_for_session(session_token)
    if biller_user is None:
        return JSONResponse(status_code=401, content={
            "error": "Войдите в аккаунт, чтобы создавать карточки", "code": "unauthorized"})

    try:
        card_count = int(body.get("card_count", 6))
    except (TypeError, ValueError):
        card_count = 6
    # Кламп 1..10 — страховка от прямого POST с card_count=100 (биллинг спишет,
    # но пак на 100 карточек положит воркеры; UI и так даёт максимум 10).
    card_count = max(1, min(card_count, 10))
    # Раньше тут стояло `if _free_available: card_count = 1` без проверки баланса.
    # → Юзер с купленными карточками И доступной пробой видел в UI 10, но сервер
    # пожинал 1. Теперь форсим 1 только когда у юзера НЕТ платного баланса:
    # на платном балансе он имеет право заказывать сколько угодно (1..10),
    # пробу при этом сохраним для следующего раза (см. reserve_generation).
    # biller_user — это sqlite3.Row, у него НЕТ .get(): только [] / индексирование.
    # Раньше тут стояло biller_user.get("balance_cards") → AttributeError при каждом
    # вызове /api/generate-cards. Теперь читаем по индексу с дефолтом-через-or.
    if account_service._free_available(biller_user) and int(biller_user["balance_cards"] or 0) <= 0:
        card_count = 1
    try:
        reservation = account_service.reserve_generation(biller_user["id"], card_count)
    except account_service.ServiceError as e:
        return JSONResponse(status_code=e.status, content={"error": e.message, "code": e.code})

    gid = uuid.uuid4().hex
    title = (body.get("title") or body.get("brief") or "Карточки").strip()[:80] or "Карточки"
    # Тир-политика картинок: free (проба) → low/нано-про-микс, cards (оплачено) →
    # gpt-image medium. reservation["mode"] — точный признак этой генерации.
    # ВАЖНО: читаем конфиг СВЕЖИМ с диска (load_config), а НЕ из модульного CFG.
    # Причина: при запуске `python server.py` роуты живут в модуле __main__, а
    # админ-хук горячей перезагрузки исторически обновлял только дубль-модуль
    # `server` → выбор провайдера/тира брался из стартового конфига до рестарта
    # («поставил GPT low — рендерит fastgen»). Свежее чтение делает решение
    # тира независимым от того, в какой модуль попал reload. Диск-чтение ~12 КБ
    # на фоне многосекундной генерации — пренебрежимо.
    try:
        live_cfg = G.load_config()
    except Exception:
        live_cfg = CFG
    tier = "paid" if reservation["mode"] == "cards" else "free"
    img_policy = G.resolve_image_policy(live_cfg, tier)
    requested_provider = body.get("image_provider", live_cfg.get("default_provider", "veononstop"))
    # Политика (если включена) перебивает выбор провайдера: primary идёт в
    # prepared (для размера пула воркеров), пер-карточная раскладка — в context.
    effective_provider = (img_policy.get("primary") if img_policy else None) or requested_provider
    params = {
        "plan_id": body.get("plan_id"),
        "card_overrides": body.get("card_overrides") or {},
        "ref_paths": body.get("ref_paths", []),
        "brief": body.get("brief", ""),
        "competitor_analysis": body.get("competitor_analysis", {}),
        "formats_selected": body.get("formats_selected", ["wb_main"]),
        "style_key": body.get("style_key", "premium"),
        "category": body.get("category_confirmed", ""),
        "style_ref_paths": body.get("style_ref_paths", []),
        "image_provider": effective_provider,
        "image_policy": img_policy,
        "write_description": body.get("write_description", True),
        "user_facts": body.get("user_facts") or {},
    }

    job = GenJob(gid, biller_user["id"], card_count, title)
    job.charged = reservation["charged"]; job.charge_mode = reservation["mode"]
    account_service.gen_create(gid, biller_user["id"], body.get("chat_id"), title,
                               card_count, {}, reservation["charged"], reservation["mode"])
    _evict_gen_jobs()
    with _GEN_JOBS_LOCK:
        _GEN_JOBS[gid] = job
    _threading.Thread(target=_run_generation_job, args=(job, params), daemon=True).start()
    logger.info(f"/api/generate-cards: job {gid} user={biller_user['id']} "
                f"mode={reservation['mode']} charged={reservation['charged']} designs={card_count}")
    return {"generation_id": gid, "status": "running"}


def _gen_require_owner(req: Request, gid: str):
    user = account_service.user_for_session(req.cookies.get(account_router.COOKIE_NAME))
    if user is None:
        return None, JSONResponse(status_code=401, content={"error": "unauthorized", "code": "unauthorized"})
    owner = account_service.gen_owner(gid)
    if owner is None or owner != user["id"]:
        return None, JSONResponse(status_code=404, content={"error": "not found"})
    return user, None


@app.get("/api/generation/active")
async def generation_active(req: Request, limit: int = 50):
    """Серверная история чатов юзера. Дефолт 50 — этого хватает сайдбару при
    переустановке приложения / входе с нового устройства, чтобы восстановить
    весь список. ?limit=N перебивает дефолт."""
    user = account_service.user_for_session(req.cookies.get(account_router.COOKIE_NAME))
    if user is None:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return {"generations": account_service.gen_active(user["id"], limit=limit)}


@app.get("/api/generation/{gid}")
async def generation_snapshot(gid: str, req: Request):
    user, err = _gen_require_owner(req, gid)
    if err:
        return err
    with _GEN_JOBS_LOCK:
        job = _GEN_JOBS.get(gid)
    if job:
        snap = job.snapshot_frame(); snap["id"] = gid; snap["title"] = job.title
        snap["card_count"] = job.card_count
        return snap
    snap = account_service.gen_get(gid, user["id"])
    if not snap:
        return JSONResponse(status_code=404, content={"error": "not found"})
    snap["done"] = snap["status"] != "running"
    return snap


# Soft-cancel: помечает генерацию cancelled в БД, чтобы UI не показывал
# resume-banner после удаления чата. Поток продолжит до естественного
# завершения (не убить threading.Thread безопасно), но result не появится
# в /api/generation/active → юзер не получит призрак-чат на удалённый pack.
@app.post("/api/generation/{gid}/cancel")
async def cancel_generation(gid: str, req: Request):
    user, err = _gen_require_owner(req, gid)
    if err:
        return err
    snap = account_service.gen_get(gid, user["id"])
    if snap and snap.get("status") == "running":
        try:
            account_service.gen_update(gid, status="cancelled",
                                       error="отменено пользователем")
        except Exception:
            logger.exception(f"gen cancel failed for {gid}")
            return JSONResponse({"error": "internal"}, 500)
    return {"ok": True, "status": "cancelled"}


@app.get("/api/generation/{gid}/stream")
async def generation_stream(gid: str, req: Request):
    user, err = _gen_require_owner(req, gid)
    if err:
        return err

    with _GEN_JOBS_LOCK:
        job = _GEN_JOBS.get(gid)

    async def stream():
        if job is None:
            # нет в памяти → отдаём финальный снапшот из БД одним кадром
            snap = account_service.gen_get(gid, user["id"])
            if snap:
                yield f"data: {json.dumps({'files': snap['files'], 'description': snap['description'], 'design': snap['design'], 'progress': snap['progress'], 'caption': snap['caption'] or '', 'done': snap['status'] != 'running', 'error': snap['error']}, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
            return
        q = job.subscribe()
        try:
            yield ": warmup\n\n"
            while True:
                try:
                    kind, data = await asyncio.to_thread(q.get, True, 10)
                except Exception:
                    try:
                        if await req.is_disconnected():
                            break
                    except Exception:
                        pass
                    yield ": ping\n\n"
                    continue
                if kind == "end":
                    yield f"data: {json.dumps({'done': True}, ensure_ascii=False)}\n\n"
                    break
                if kind == "error":
                    yield f"data: {json.dumps({'error': data}, ensure_ascii=False)}\n\n"
                    continue
                if kind == "ping":
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            job.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


# ─── 4b. Regenerate ONE card with user feedback ─────────────────────────────

@app.post("/api/regenerate-card")
async def regenerate_card(req: Request):
    """
    Перегенерирует ОДНУ карточку с учётом фидбека пользователя.
    Не SSE: блокирующий вызов, фронт ждёт один файл и подменяет картинку.

    Body:
      ref_paths, brief, competitor_analysis, formats_selected, style_key,
      category_confirmed, style_ref_paths, image_provider, user_facts —
      такие же, как у /api/generate-cards.
      card_index: int — индекс карточки которую перегенерируем (0-based)
      user_feedback: str — что юзеру не нравится / что должно стать
      original_card_type: str — main / feature / lifestyle / comparison (для
        подсказки модели чтобы новая карточка осталась той же роли)

    Returns:
      { success: bool, file_url: str | None, error: str | None,
        card_index: int }
    """
    body = await req.json()
    card_index = int(body.get("card_index", 0))
    user_feedback = (body.get("user_feedback") or "").strip()
    original_card_type = (body.get("original_card_type") or "").strip()

    if not user_feedback:
        return {"success": False, "error": "user_feedback пуст", "card_index": card_index}

    # Правка карточки стоит 1 карточку С БАЛАНСА (бесплатная проба НЕ годится —
    # правки только за купленные карточки). Фри-юзер без баланса → 402, фронт
    # зовёт пополнить. Тир правки = paid (списываем с баланса) → то же качество,
    # что у платной генерации (resolve_image_policy('paid')).
    session_token = req.cookies.get(account_router.COOKIE_NAME)
    biller_user = account_service.user_for_session(session_token)
    if biller_user is None:
        return JSONResponse(status_code=401, content={
            "success": False, "error": "Войдите в аккаунт", "code": "unauthorized",
            "card_index": card_index})
    try:
        account_service.reserve_edit(biller_user["id"])
    except account_service.ServiceError as e:
        return JSONResponse(status_code=e.status, content={
            "success": False, "error": e.message, "code": e.code, "card_index": card_index})
    # Тир правки берём как у генерации: фреш-конфиг → политика 'paid'. Если
    # тир-политика выключена — None, тогда рендер пойдёт по default_provider.
    try:
        edit_policy = G.resolve_image_policy(G.load_config(), "paid")
    except Exception:
        edit_policy = None

    ref_paths = body.get("ref_paths", [])
    brief = body.get("brief", "")
    competitor_analysis = body.get("competitor_analysis", {})
    formats_selected = body.get("formats_selected", ["wb_main"])
    style_key = body.get("style_key", "premium")
    category = body.get("category_confirmed", "")
    style_ref_paths = body.get("style_ref_paths", [])
    image_provider = body.get("image_provider", CFG.get("default_provider", "veononstop"))
    if edit_policy and edit_policy.get("primary"):
        image_provider = edit_policy["primary"]   # prepared-провайдер = primary тира
    user_facts = body.get("user_facts") or {}

    # Брифу даём очень явный сигнал: это правка конкретной карточки.
    # Модель должна понять что текст ниже — пожелания юзера, не описание товара.
    role_hint = f"карточки типа «{original_card_type}»" if original_card_type else "этой карточки"
    fixed_brief = (
        f"{brief}\n\n"
        f"━━━ ПРАВКА ОТ ЮЗЕРА ДЛЯ {role_hint.upper()} ━━━\n"
        f"Пользователь хочет ИЗМЕНИТЬ {role_hint}. "
        f"Что именно поменять: «{user_feedback}». "
        f"Учти это в композиции/тексте/цвете/стиле, остальное оставь как было."
    )

    logger.info(
        f"/api/regenerate-card: index={card_index} feedback={user_feedback[:60]!r} "
        f"type={original_card_type!r}"
    )

    def run_single():
        # Воркер to_thread: ставим контекст (user_id для учёта + тир-политику
        # правки) и ОБЯЗАТЕЛЬНО чистим в finally — пул потоков переиспользуется,
        # иначе политика «протекла» бы в следующую задачу пула (detect_category).
        G.set_llm_context(user_id=biller_user["id"], purpose="design",
                          generation_id=None, img_policy=edit_policy)
        try:
            last = None
            for partial in G.generate_pack_competitor_streaming(
                ref_paths=ref_paths,
                brief=fixed_brief,
                competitor_analysis=competitor_analysis,
                formats_selected=formats_selected,
                card_count=1,                 # ровно одна новая карточка
                style_key=style_key,
                category_confirmed=category,
                image_provider=image_provider,
                style_ref_paths=style_ref_paths,
                write_description=False,      # описание не трогаем
                generate_videos=False,
                user_facts=user_facts,
                cfg=CFG,
            ):
                last = partial
            return last or {}
        finally:
            G.clear_llm_context()

    def _refund():
        # Списанную за правку карточку возвращаем при любой неудаче.
        try:
            account_service.refund_edit(biller_user["id"])
        except Exception:
            logger.exception("refund_edit failed")

    try:
        result = await asyncio.to_thread(run_single)
    except Exception as e:
        logger.exception("regenerate-card failed")
        _refund()
        return {"success": False, "error": str(e), "card_index": card_index}

    files = result.get("files") or []
    if not files:
        _refund()
        return {"success": False, "error": "Не получили файл от генератора", "card_index": card_index}

    # Конвертируем абсолютный путь в /api/output/... URL (как в /api/generate-cards)
    fp = Path(files[0])
    if fp.is_file():
        try:
            rel = fp.relative_to(BASE / "output")
            file_url = f"/api/output/{rel.as_posix()}"
        except ValueError:
            file_url = str(fp)
    else:
        _refund()
        return {"success": False, "error": "Файл не найден на диске", "card_index": card_index}

    return {
        "success": True,
        "file_url": file_url,
        "card_index": card_index,
    }


# ─── 4b2. Regenerate SEO description with user feedback ─────────────────────
#
# Зеркало правки картинки, но для описания. Биллинг (service.reserve_description_edit):
#   • генерация была бесплатной пробой → 402 code='free_mode' → фронт зовёт покупку;
#   • платная генерация, первая правка описания → БЕСПЛАТНО (1 фри на генерацию);
#   • далее → 1 карточка с баланса (возврат при сбое).

@app.post("/api/regenerate-description",
          dependencies=[Depends(_rate_limit("regenerate_description", 8))])
async def regenerate_description_endpoint(req: Request):
    body = await req.json()
    gen_id = (body.get("generation_id") or "").strip()
    user_feedback = (body.get("user_feedback") or "").strip()

    session_token = req.cookies.get(account_router.COOKIE_NAME)
    biller_user = account_service.user_for_session(session_token)
    if biller_user is None:
        return JSONResponse(status_code=401, content={
            "success": False, "error": "Войдите в аккаунт", "code": "unauthorized"})
    if not gen_id:
        return JSONResponse(status_code=400, content={
            "success": False, "error": "generation_id обязателен", "code": "bad_request"})
    if not user_feedback:
        return JSONResponse(status_code=400, content={
            "success": False, "error": "Опишите, что изменить в описании", "code": "bad_request"})

    # Биллинг + гейтинг free/paid — атомарно в сервисе.
    try:
        reservation = account_service.reserve_description_edit(gen_id, biller_user["id"])
    except account_service.ServiceError as e:
        return JSONResponse(status_code=e.status, content={
            "success": False, "error": e.message, "code": e.code})

    snap = account_service.gen_get(gen_id, biller_user["id"])
    current_description = (body.get("current_description")
                           or (snap or {}).get("description") or "").strip()
    competitor_analysis = body.get("competitor_analysis") or {}
    user_facts = body.get("user_facts") or {}

    # Лёгкий product-контекст, чтобы правка осталась предметной (основная фактура
    # и так в current_description; это — страховка от «воды»).
    facts_features = []
    for k, v in (user_facts or {}).items():
        if v is None:
            continue
        sv = ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)
        if sv.strip():
            facts_features.append(f"{k}: {sv}")
    product_ctx = {
        "name": (body.get("title") or body.get("brief") or "").strip() or "—",
        "brand": (body.get("brand") or "").strip() or "—",
        "category": (body.get("category_confirmed") or body.get("category") or "").strip() or "—",
        "usp": "",
        "features": facts_features,
        "audience": "",
        "price_range": "",
    }

    def run_desc():
        G.set_llm_context(user_id=biller_user["id"], purpose="description",
                          generation_id=gen_id)
        try:
            return G.regenerate_description(
                current_description, user_feedback,
                product_or_design=product_ctx,
                competitor_analysis=competitor_analysis,
                user_facts=user_facts, cfg=CFG)
        finally:
            G.clear_llm_context()

    def _refund():
        try:
            account_service.refund_description_edit(gen_id, biller_user["id"],
                                                    reservation["mode"])
        except Exception:
            logger.exception("refund_description_edit failed")

    try:
        new_text = await asyncio.to_thread(run_desc)
    except Exception as e:
        logger.exception("regenerate-description failed")
        _refund()
        return {"success": False, "error": str(e)}

    new_text = (new_text or "").strip()
    if not new_text:
        _refund()
        return {"success": False, "error": "Модель вернула пустое описание"}

    # Сохраняем новое описание в генерацию (переживает reload / возврат в чат).
    try:
        account_service.gen_update(gen_id, description=new_text)
    except Exception:
        logger.exception("gen_update(description) failed — отдаём текст всё равно")

    return {
        "success": True,
        "description": new_text,
        "mode": reservation["mode"],            # free_desc | paid
        "charged": reservation["charged"],
        "balance_cards": reservation.get("balance_cards"),
    }


# ─── 4c. Built-in EXAMPLE (free demo, no WB parse, no token spend) ──────────

@app.get("/api/example")
async def get_example():
    """Возвращает заранее подготовленный пример: фото товара + готовые карточки +
    описание. Никакого парсинга WB и живой генерации — это встроенный showcase.
    Фронт «проигрывает» по нему анимацию процесса. Бесплатно и без авторизации."""
    manifest = BASE / "static" / "assets" / "example" / "manifest.json"
    if not manifest.is_file():
        return JSONResponse({"error": "example not configured"}, status_code=404)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as e:
        logger.exception("example manifest read failed")
        return JSONResponse({"error": str(e)}, status_code=500)
    return data


# ─── 5. Settings ───────────────────────────────────────────────────────────

SAFE_SETTINGS_KEYS = [
    "default_provider", "veononstop_default_model",
]

@app.get("/api/settings")
async def get_settings():
    cfg = G.load_config()
    safe = {}
    safe["default_provider"] = cfg.get("default_provider", "veononstop")
    safe["veononstop_default_model"] = cfg.get("veononstop_default_model", "GEM_PIX_2")

    endpoints = []
    for ep in cfg.get("claude_endpoints", []):
        key = ep.get("api_key", "")
        masked = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"
        endpoints.append({
            "name": ep.get("name", "?"),
            "base_url": ep.get("base_url", ""),
            "api_key_masked": masked,
            "models": ep.get("models", []),
        })
    safe["claude_endpoints"] = endpoints

    return safe


@app.put("/api/settings")
async def update_settings(req: Request):
    # Дубль /admin/settings без auth раньше принимал api_key и base_url Claude
    # для любого анонимного клиента → перезапись config.json, экспорт ключей.
    # Гейтим через ту же admin-проверку что и /admin (login + IP allowlist).
    if not _is_admin_request(req):
        return JSONResponse({"error": "forbidden"}, 403)
    body = await req.json()
    cfg_path = BASE / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    if "default_provider" in body:
        cfg["default_provider"] = body["default_provider"]
    if "veononstop_default_model" in body:
        cfg["veononstop_default_model"] = body["veononstop_default_model"]

    if "claude_endpoints" in body:
        for update_ep in body["claude_endpoints"]:
            name = update_ep.get("name")
            if not name:
                continue
            for ep in cfg.get("claude_endpoints", []):
                if ep.get("name") == name:
                    if "api_key" in update_ep and not update_ep["api_key"].endswith("..."):
                        ep["api_key"] = update_ep["api_key"]
                    if "base_url" in update_ep:
                        ep["base_url"] = update_ep["base_url"]
                    if "models" in update_ep:
                        ep["models"] = update_ep["models"]
                    break

    if "new_endpoint" in body:
        ne = body["new_endpoint"]
        if ne.get("name") and ne.get("base_url") and ne.get("api_key"):
            cfg.setdefault("claude_endpoints", []).append(ne)

    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

    global CFG
    CFG = G.load_config()
    return {"ok": True}


# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    import socket
    import os

    # PORT из окружения (прод: systemd задаёт PORT=8000 → nginx знает, куда
    # проксировать). Без него — авто-поиск свободного порта (dev-режим).
    port = int(os.environ.get("PORT") or 0)
    if not port:
        port = 8000
        for p in range(8000, 8020):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", p)) != 0:
                    port = p
                    break

    logger.info(f"Starting WBnator API on http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")

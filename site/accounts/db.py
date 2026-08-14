"""
SQLite data layer for the WBnator account / billing system.

Pure stdlib (sqlite3) — no ORM, no external deps. A single shared connection
guarded by a re-entrant lock; every write goes through ``tx()`` so it is atomic
(BEGIN … COMMIT / ROLLBACK). At the scale of a single-server app this is both
simple and correct.

Currency is **карточки** (1 карточка = 1 генерация). The schema is created
idempotently on import via ``init_db()`` (called from server startup). New
column later? Use ``_ensure_column`` — a tiny built-in migration helper.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

_BASE = Path(__file__).resolve().parent.parent
_DATA_DIR = _BASE / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
# Боевая БД по умолчанию; можно переопределить env-переменной WBNATOR_DB
# (например, для тестов на отдельной БД, чтобы не трогать прод-данные).
DB_PATH = Path(os.environ.get("WBNATOR_DB") or (_DATA_DIR / "wbnator.db"))

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(DB_PATH),
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def get_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        with _LOCK:
            if _CONN is None:
                _CONN = _connect()
    return _CONN


def close_conn() -> None:
    """Закрыть общее соединение (нужно перед restore из бэкапа)."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
            _CONN = None


@contextmanager
def tx():
    """Atomic write transaction."""
    conn = get_conn()
    with _LOCK:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE;")
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def query_all(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        cur = conn.execute(sql, tuple(params))
        rows = cur.fetchall()
        cur.close()
        return rows


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        cur = conn.execute(sql, tuple(params))
        row = cur.fetchone()
        cur.close()
        return row


def _existing_columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    cur.execute(f"PRAGMA table_info({table});")
    return {r[1] for r in cur.fetchall()}


def _ensure_column(cur: sqlite3.Cursor, table: str, column: str, ddl: str) -> None:
    if column not in _existing_columns(cur, table):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl};")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  email           TEXT    NOT NULL UNIQUE,
  password_hash   TEXT    NOT NULL,
  name            TEXT,
  balance_cards   INTEGER NOT NULL DEFAULT 0,        -- == SUM(card_ledger.delta)
  is_admin        INTEGER NOT NULL DEFAULT 0,
  referral_code   TEXT    NOT NULL UNIQUE,
  referred_by     INTEGER REFERENCES users(id),
  consent_pdn     INTEGER NOT NULL DEFAULT 0,        -- 152-ФЗ
  consent_offer   INTEGER NOT NULL DEFAULT 0,
  consent_version TEXT,
  consent_at      TEXT,
  consent_ip      TEXT,
  notif_email     INTEGER NOT NULL DEFAULT 1,
  free_used_date  TEXT,
  created_at      TEXT    NOT NULL,
  updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS card_ledger (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  delta         INTEGER NOT NULL,
  balance_after INTEGER NOT NULL,
  reason        TEXT    NOT NULL,   -- signup|promo|purchase|referral|spend|refund|adjust
  ref_type      TEXT,
  ref_id        TEXT,
  meta          TEXT,
  created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON card_ledger(user_id, id DESC);

CREATE TABLE IF NOT EXISTS purchases (
  id                  TEXT    PRIMARY KEY,
  user_id             INTEGER NOT NULL REFERENCES users(id),
  kind                TEXT    NOT NULL,    -- pack | cards
  pack_key            TEXT,
  cards               INTEGER NOT NULL,
  amount_kopecks      INTEGER NOT NULL,
  currency            TEXT    NOT NULL DEFAULT 'RUB',
  status              TEXT    NOT NULL,    -- pending | paid | failed | canceled
  provider            TEXT    NOT NULL,
  provider_payment_id TEXT,
  receipt_email       TEXT    NOT NULL,
  receipt_sent        INTEGER NOT NULL DEFAULT 0,
  idempotency_key     TEXT    UNIQUE,
  created_at          TEXT    NOT NULL,
  paid_at             TEXT,
  meta                TEXT
);
CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS promo_codes (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  code           TEXT    NOT NULL UNIQUE,
  kind           TEXT    NOT NULL DEFAULT 'cards',
  cards          INTEGER NOT NULL DEFAULT 0,
  max_total_uses INTEGER,
  per_user_limit INTEGER NOT NULL DEFAULT 1,
  used_count     INTEGER NOT NULL DEFAULT 0,
  active         INTEGER NOT NULL DEFAULT 1,
  starts_at      TEXT,
  expires_at     TEXT,
  created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_redemptions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  promo_id   INTEGER NOT NULL REFERENCES promo_codes(id),
  user_id    INTEGER NOT NULL REFERENCES users(id),
  cards      INTEGER NOT NULL,
  created_at TEXT    NOT NULL,
  UNIQUE(promo_id, user_id)
);

CREATE TABLE IF NOT EXISTS referrals (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  referrer_id           INTEGER NOT NULL REFERENCES users(id),
  referee_id            INTEGER NOT NULL REFERENCES users(id),
  status                TEXT    NOT NULL DEFAULT 'pending',  -- pending | rewarded | void
  signup_reward_cards   INTEGER NOT NULL DEFAULT 0,
  purchase_reward_cards INTEGER NOT NULL DEFAULT 0,
  created_at            TEXT    NOT NULL,
  rewarded_at           TEXT,
  UNIQUE(referee_id)
);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);

CREATE TABLE IF NOT EXISTS sessions (
  id         TEXT    PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT    NOT NULL,
  expires_at TEXT    NOT NULL,
  user_agent TEXT,
  ip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS password_resets (
  token      TEXT    PRIMARY KEY,           -- opaque random token (в ссылке)
  user_id    INTEGER NOT NULL REFERENCES users(id),
  expires_at TEXT    NOT NULL,
  used       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resets_user ON password_resets(user_id);

-- Одноразовый код «передачи сессии» в Android-приложение (WebView). Соц-вход в
-- приложении идёт во ВНЕШНЕМ браузере (Google запрещает OAuth внутри WebView);
-- callback кладёт сюда свежую сессию и отдаёт код приложению через deep-link
-- kartochka://oauth?code=..., а приложение обменивает его в WebView на cookie.
-- Код single-use и короткоживущий (TTL ~2 мин) — даже при перехвате deep-link
-- он бесполезен после первого обмена либо по истечении срока.
CREATE TABLE IF NOT EXISTS oauth_handoff (
  code          TEXT    PRIMARY KEY,
  session_token TEXT    NOT NULL,
  next_path     TEXT,
  expires_at    TEXT    NOT NULL,
  created_at    TEXT    NOT NULL
);

-- Фоновые генерации: переживают навигацию/перезагрузку, можно вернуться и забрать.
CREATE TABLE IF NOT EXISTS generations (
  id            TEXT    PRIMARY KEY,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  chat_id       TEXT,
  title         TEXT,
  status        TEXT    NOT NULL,        -- running | done | error | canceled
  progress      INTEGER NOT NULL DEFAULT 0,
  caption       TEXT,
  card_count    INTEGER NOT NULL DEFAULT 6,
  request_json  TEXT,                    -- параметры запроса (для возможного рестарта)
  files_json    TEXT,                    -- доставленные карточки (URL'ы)
  description   TEXT,
  design_json   TEXT,
  error         TEXT,
  charged       INTEGER NOT NULL DEFAULT 0,   -- списано карточек (для возврата)
  charge_mode   TEXT,                         -- free | cards
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_generations_user ON generations(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS llm_usage (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id             INTEGER REFERENCES users(id),    -- NULL = system call
  endpoint            TEXT    NOT NULL,                -- 'openai-fallback' / 'stepanovikov' / ...
  model               TEXT    NOT NULL,                -- 'gpt-4o-mini' / 'claude-opus-4.6' / ...
  prompt_tokens       INTEGER NOT NULL DEFAULT 0,
  completion_tokens   INTEGER NOT NULL DEFAULT 0,
  total_tokens        INTEGER NOT NULL DEFAULT 0,
  cached_tokens       INTEGER NOT NULL DEFAULT 0,       -- prompt-токены, прочитанные из кеша (дешевле/free)
  cost_usd            REAL    NOT NULL DEFAULT 0,       -- оценка по прайсу; 0 для Claude-прокси (цена неизвестна)
  purpose             TEXT,                             -- 'design' | 'detect_category' | 'copy_fix' | 'analyze' | 'description' | 'ping'
  generation_id       TEXT,                             -- FK to generations.id when applicable
  created_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_usage_user    ON llm_usage(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_model   ON llm_usage(model, created_at DESC);

-- Учёт ГЕНЕРАЦИИ КАРТИНОК (отдельно от llm_usage — у картинок нет токенов, но
-- есть провайдер/качество/кредиты/$). Одна строка = одна сгенерированная картинка.
-- generator.py пишет сюда на каждый успешный рендер карточки; админка читает
-- агрегатами (всего фото / $ / по провайдерам / по дням / по юзерам).
CREATE TABLE IF NOT EXISTS image_usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER REFERENCES users(id),   -- NULL = system / гость
  generation_id TEXT,                            -- FK to generations.id when applicable
  provider      TEXT    NOT NULL,                -- 'openai_direct' / 'fastgen_openai' / 'fastgen_nanopro' / 'legacy' / ...
  model         TEXT,                            -- 'gpt-image-2' / 'nano-banana-pro' / ...
  quality       TEXT,                            -- gpt-image: low/medium/high/auto; иначе ''
  size          TEXT,                            -- gpt-image: 1024x1536 / auto; иначе ''
  images        INTEGER NOT NULL DEFAULT 1,      -- сколько картинок в этой строке (обычно 1)
  credits       INTEGER NOT NULL DEFAULT 0,      -- кредиты FastGen (0 для openai_direct)
  cost_usd      REAL    NOT NULL DEFAULT 0,      -- оценка $ (gpt-image по прайсу токенов; fastgen = credits*ставка)
  purpose       TEXT,                            -- 'design' | 'variant' | ...
  created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_usage_created ON image_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_image_usage_user    ON image_usage(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_image_usage_prov    ON image_usage(provider, created_at DESC);
"""


def init_db() -> None:
    """Create tables idempotently + tiny forward migrations + seed."""
    # Перед любой возможной миграцией снимаем pre-init бэкап. Если БД ещё нет —
    # backup_now тихо вернёт пустой список (это нормально, бэкапить нечего).
    if DB_PATH.exists():
        try:
            from . import backup as _backup
            _backup.backup_now("pre-init")
        except Exception as e:
            print(f"[db] WARN: pre-init backup failed → {e}")

    with tx() as cur:
        cur.executescript(SCHEMA)
        _ensure_column(cur, "users", "free_used_date", "free_used_date TEXT")
        _ensure_column(cur, "users", "is_admin", "is_admin INTEGER NOT NULL DEFAULT 0")
        # OAuth (Google/Yandex): связка соц-аккаунта с локальным юзером.
        # oauth_provider = 'google'|'yandex', oauth_sub = стабильный id у
        # провайдера. Пара (provider, sub) уникальна → один соц-аккаунт = один
        # локальный юзер. Колонки добавляются миграцией без потери старых данных.
        _ensure_column(cur, "users", "oauth_provider", "oauth_provider TEXT")
        _ensure_column(cur, "users", "oauth_sub", "oauth_sub TEXT")
        _ensure_column(cur, "users", "avatar_url", "avatar_url TEXT")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oauth "
            "ON users(oauth_provider, oauth_sub) "
            "WHERE oauth_provider IS NOT NULL AND oauth_sub IS NOT NULL;"
        )
        # Правка SEO-описания: одна бесплатная попытка на каждую ПЛАТНУЮ генерацию
        # (в free-пробе кнопка ведёт на покупку). desc_free_used=1 — фри-правка
        # уже потрачена, дальнейшие правят описание за 1 карточку с баланса.
        _ensure_column(cur, "generations", "desc_free_used",
                       "desc_free_used INTEGER NOT NULL DEFAULT 0")
        # Кеш-токены добавлены позже — догоняем старые БД без потери данных.
        _ensure_column(cur, "llm_usage", "cached_tokens", "cached_tokens INTEGER NOT NULL DEFAULT 0")
        # Гарантия уникальности email БЕЗ учёта регистра — на случай если сервисный
        # слой ошибётся с нормализацией. UNIQUE на колонке уже есть (case-sensitive);
        # этот индекс добавляет COLLATE NOCASE как страховочный пояс на уровне БД.
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_nocase "
            "ON users(email COLLATE NOCASE);"
        )
    _seed_promos()
    _backfill_image_usage()


def _backfill_image_usage() -> None:
    """Однократно переносим ИСТОРИЧЕСКИЕ генерации картинок из ``generations`` в
    ``image_usage``, чтобы в админке сразу были видны прошлые фото-генерации
    (счётчики по юзерам/дням). До этого трекинга провайдер/качество/стоимость на
    строку не сохранялись, поэтому исторические строки идут как provider='legacy'
    с cost_usd=0 — точные ТОЛЬКО счётчики, $ начинает считаться с момента трекинга.

    Идемпотентно: бэкап делается лишь когда image_usage пуста (иначе уже мигрировали
    или уже есть живой трекинг)."""
    try:
        import json as _json
        have = query_one("SELECT COUNT(*) c FROM image_usage")
        if have and have["c"]:
            return  # уже есть данные — ничего не трогаем
        gens = query_all(
            "SELECT id, user_id, card_count, files_json, created_at "
            "FROM generations WHERE status='done'"
        )
        if not gens:
            return
        rows = []
        for g in gens:
            # сколько картинок реально доставлено: длина files_json, иначе card_count
            n = 0
            fj = g["files_json"]
            if fj:
                try:
                    parsed = _json.loads(fj)
                    if isinstance(parsed, (list, tuple)):
                        n = len(parsed)
                    elif isinstance(parsed, dict):
                        n = len(parsed)
                except Exception:
                    n = 0
            if n <= 0:
                n = int(g["card_count"] or 0)
            if n <= 0:
                continue
            rows.append((g["user_id"], g["id"], "legacy", "", "", "",
                         n, 0, 0.0, "design", g["created_at"]))
        if not rows:
            return
        with tx() as cur:
            cur.executemany(
                """INSERT INTO image_usage
                   (user_id, generation_id, provider, model, quality, size,
                    images, credits, cost_usd, purpose, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        print(f"[db] backfilled {len(rows)} legacy image_usage rows from generations")
    except Exception as e:
        print(f"[db] WARN: image_usage backfill skipped → {e}")


def _seed_promos() -> None:
    """Seed launch promo codes once (idempotent)."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    seeds = [
        # code,      cards, max_total_uses, per_user_limit
        ("WELCOME5",  5,  None, 1),
        ("START10",   10, 1000, 1),
    ]
    with tx() as cur:
        for code, cards, max_total, per_user in seeds:
            cur.execute(
                """INSERT OR IGNORE INTO promo_codes
                   (code, kind, cards, max_total_uses, per_user_limit, active, created_at)
                   VALUES (?, 'cards', ?, ?, ?, 1, ?)""",
                (code, cards, max_total, per_user, now),
            )

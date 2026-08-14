"""
Account / billing business logic (currency = карточки).

Every balance change goes through ``_credit`` / ``_debit`` which write an
immutable ``card_ledger`` row AND update the denormalized ``users.balance_cards``
in the SAME transaction — balance can never disagree with its audit trail.

Invariants:
  • promo: one redemption per (code, user) — DB UNIQUE + pre-check;
  • purchase: idempotent create + credit-once on pending→paid;
  • referral: a user can be referred at most once (DB UNIQUE on referee_id);
  • generation: 1 карточка за генерацию (free покрывает); правки бесплатны;
  • spend never goes negative.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

from . import db, pricing, payments, mailer
from .security import (
    hash_password, verify_password, new_session_token, new_referral_code,
    normalize_email, is_valid_email,
)

SESSION_TTL_DAYS = 30
CONSENT_VERSION = "2026-05-31"
_BASE_DIR = Path(__file__).resolve().parent.parent


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "error", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _today() -> str:
    return dt.date.today().isoformat()


# ── admin config ─────────────────────────────────────────────────────────────
def admin_emails() -> set[str]:
    try:
        cfg = json.loads((_BASE_DIR / "config.json").read_text(encoding="utf-8"))
        return {normalize_email(e) for e in cfg.get("admin_emails", []) if e}
    except Exception:
        return set()


def site_base_url() -> str:
    """Домен сайта для реф-ссылок/возвратов (из config.site_base_url)."""
    try:
        cfg = json.loads((_BASE_DIR / "config.json").read_text(encoding="utf-8"))
        return (cfg.get("site_base_url") or "").rstrip("/")
    except Exception:
        return ""


def admin_ip_allowlist() -> set[str]:
    """Опциональный список IP, которым разрешена админка. Пусто = без ограничения по IP."""
    try:
        cfg = json.loads((_BASE_DIR / "config.json").read_text(encoding="utf-8"))
        return {str(ip).strip() for ip in cfg.get("admin_ip_allowlist", []) if str(ip).strip()}
    except Exception:
        return set()


# ── password reset (email-ready, stub mailer) ────────────────────────────────
RESET_TTL_HOURS = 1


def _reset_return_link() -> bool:
    """Stub-режим: вернуть ссылку в ответе, чтобы тестировать без реальной почты.
    Поставьте config.password_reset_return_link=false, когда подключите SMTP."""
    try:
        cfg = json.loads((_BASE_DIR / "config.json").read_text(encoding="utf-8"))
        return bool(cfg.get("password_reset_return_link", True))
    except Exception:
        return True


def request_password_reset(email: str, ip: Optional[str], base_url: Optional[str]) -> dict:
    email = normalize_email(email)
    out = {"sent": True}                       # не раскрываем, есть ли такой email
    row = db.query_one("SELECT * FROM users WHERE email=?", (email,))
    if not row:
        return out
    token = new_session_token()
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=RESET_TTL_HOURS)).isoformat()
    with db.tx() as cur:
        cur.execute("UPDATE password_resets SET used=1 WHERE user_id=? AND used=0", (row["id"],))
        cur.execute(
            """INSERT INTO password_resets (token, user_id, expires_at, used, created_at)
               VALUES (?,?,?,0,?)""", (token, row["id"], expires, _now()))
    base = (base_url or site_base_url() or "").rstrip("/")
    link = f"{base}/reset?token={token}"
    mailer.send_password_reset(email, link)    # сейчас — заглушка (лог)
    if _reset_return_link():
        out["reset_link"] = link               # для теста без почты
    return out


def confirm_password_reset(token: str, new_password: str) -> dict:
    row = db.query_one("SELECT * FROM password_resets WHERE token=?", (token,))
    if not row or row["used"] or row["expires_at"] < _now():
        raise ServiceError("Ссылка недействительна или истекла", "bad_token", 400)
    try:
        pw_hash = hash_password(new_password)
    except ValueError as e:
        raise ServiceError(str(e), "bad_password")
    with db.tx() as cur:
        cur.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                    (pw_hash, _now(), row["user_id"]))
        cur.execute("UPDATE password_resets SET used=1 WHERE token=?", (token,))
        cur.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))  # сбросить сессии
    return {"ok": True}


# ── ledger primitives (must be called inside a tx) ───────────────────────────
def _apply_delta(cur: sqlite3.Cursor, user_id: int, delta: int, reason: str,
                 ref_type: Optional[str] = None, ref_id: Optional[str] = None,
                 meta: Optional[dict] = None) -> int:
    row = cur.execute("SELECT balance_cards FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise ServiceError("Пользователь не найден", "not_found", 404)
    balance = int(row["balance_cards"]) + delta
    if balance < 0:
        raise ServiceError("Недостаточно карточек на балансе", "insufficient_funds", 402)
    cur.execute("UPDATE users SET balance_cards=?, updated_at=? WHERE id=?",
                (balance, _now(), user_id))
    cur.execute(
        """INSERT INTO card_ledger
           (user_id, delta, balance_after, reason, ref_type, ref_id, meta, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (user_id, delta, balance, reason, ref_type, ref_id,
         json.dumps(meta, ensure_ascii=False) if meta else None, _now()),
    )
    return balance


# ── serialization ────────────────────────────────────────────────────────────
def _public_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "balance_cards": int(row["balance_cards"]),
        "is_admin": bool(row["is_admin"]),
        "referral_code": row["referral_code"],
        "notif_email": bool(row["notif_email"]),
        "consent_pdn": bool(row["consent_pdn"]),
        "consent_offer": bool(row["consent_offer"]),
        "free_available": _free_available(row),
        "avatar_url": (row["avatar_url"] if "avatar_url" in row.keys() else None),
        "oauth_provider": (row["oauth_provider"] if "oauth_provider" in row.keys() else None),
        "created_at": row["created_at"],
    }


def _free_available(row: sqlite3.Row) -> bool:
    # Бесплатная карточка даётся НАВСЕГДА (одна на аккаунт), а не «в день».
    # Признак «ещё не использована» — пустой free_used_date.
    if pricing.FREE_GENERATIONS_LIFETIME <= 0:
        return False
    return not row["free_used_date"]


def get_user_row(user_id: int) -> Optional[sqlite3.Row]:
    return db.query_one("SELECT * FROM users WHERE id=?", (user_id,))


# ── auth ─────────────────────────────────────────────────────────────────────
def register(email: str, password: str, name: Optional[str],
             consent_pdn: bool, consent_offer: bool,
             ref_code: Optional[str], ip: Optional[str]) -> dict:
    email = normalize_email(email)
    if not is_valid_email(email):
        raise ServiceError("Некорректный email", "bad_email")
    if not consent_pdn or not consent_offer:
        raise ServiceError("Нужно согласие на обработку ПДн и принятие оферты",
                           "consent_required")
    try:
        pw_hash = hash_password(password)
    except ValueError as e:
        raise ServiceError(str(e), "bad_password")

    referrer = None
    if ref_code:
        referrer = db.query_one("SELECT * FROM users WHERE referral_code=?",
                                (ref_code.strip().upper(),))
    is_admin = 1 if email in admin_emails() else 0

    with db.tx() as cur:
        if cur.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise ServiceError("Пользователь с таким email уже есть", "email_taken", 409)

        for _ in range(10):
            rc = new_referral_code()
            if not cur.execute("SELECT 1 FROM users WHERE referral_code=?", (rc,)).fetchone():
                break
        else:
            rc = new_referral_code() + uuid.uuid4().hex[:3].upper()

        now = _now()
        cur.execute(
            """INSERT INTO users
               (email, password_hash, name, balance_cards, is_admin, referral_code,
                referred_by, consent_pdn, consent_offer, consent_version, consent_at,
                consent_ip, notif_email, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (email, pw_hash, (name or "").strip() or None, 0, is_admin, rc,
             referrer["id"] if referrer else None,
             1, 1, CONSENT_VERSION, now, ip, now, now),
        )
        user_id = cur.lastrowid

        if pricing.SIGNUP_BONUS_CARDS > 0:
            _apply_delta(cur, user_id, pricing.SIGNUP_BONUS_CARDS, "signup", "signup", None)

        if referrer and referrer["id"] != user_id:
            cur.execute(
                """INSERT OR IGNORE INTO referrals
                   (referrer_id, referee_id, status, signup_reward_cards,
                    purchase_reward_cards, created_at)
                   VALUES (?,?, 'pending', ?, ?, ?)""",
                (referrer["id"], user_id, pricing.REFERRAL_SIGNUP_CARDS,
                 pricing.REFERRAL_PURCHASE_CARDS, now),
            )
            if cur.rowcount and pricing.REFERRAL_SIGNUP_CARDS > 0:
                _apply_delta(cur, user_id, pricing.REFERRAL_SIGNUP_CARDS,
                             "referral", "referral", str(referrer["id"]), {"role": "referee"})

    row = get_user_row(user_id)
    token = _create_session(user_id, ip)
    return {"user": _public_user(row), "session": token}


def login(email: str, password: str, ip: Optional[str]) -> dict:
    email = normalize_email(email)
    row = db.query_one("SELECT * FROM users WHERE email=?", (email,))
    if not row or not verify_password(password, row["password_hash"]):
        raise ServiceError("Неверный email или пароль", "bad_credentials", 401)
    # keep admin flag in sync with config (promote/demote on login)
    want_admin = 1 if email in admin_emails() else row["is_admin"]
    if want_admin != row["is_admin"]:
        with db.tx() as cur:
            cur.execute("UPDATE users SET is_admin=? WHERE id=?", (want_admin, row["id"]))
        row = get_user_row(row["id"])
    token = _create_session(row["id"], ip)
    return {"user": _public_user(row), "session": token}


def _unique_referral_code(cur: sqlite3.Cursor) -> str:
    for _ in range(10):
        rc = new_referral_code()
        if not cur.execute("SELECT 1 FROM users WHERE referral_code=?", (rc,)).fetchone():
            return rc
    return new_referral_code() + uuid.uuid4().hex[:3].upper()


def oauth_login_or_register(provider: str, sub: str, email: Optional[str],
                            name: Optional[str], avatar: Optional[str],
                            ref_code: Optional[str], ip: Optional[str],
                            email_verified: bool = False) -> dict:
    """Вход/регистрация через соц-сеть (Google/Yandex).

    Порядок поиска юзера:
      1. по паре (oauth_provider, oauth_sub) — это тот же соц-аккаунт;
      2. по ПОДТВЕРЖДЁННОМУ email — у юзера уже есть локальный аккаунт,
         ПРИВЯЗЫВАЕМ соц-вход (чтобы вход хоть паролем, хоть Google вёл в один
         профиль). Привязка по email только если провайдер подтвердил адрес —
         иначе это account-takeover (соц-вход с чужим неподтверждённым email).
      3. иначе — создаём новый аккаунт. Пароль — случайный непригодный хеш
         (вход только через провайдера либо через сброс пароля по email).

    Согласия (ПДн / оферта) проставляются автоматически — кнопка соц-входа на
    UI снабжена явной правовой пометкой об их принятии (как того требует 152-ФЗ).
    """
    if not provider or not sub:
        raise ServiceError("Неполные данные от провайдера", "oauth_incomplete", 400)
    email = normalize_email(email or "")
    # Доверяем email (для привязки/админки/чеков) ТОЛЬКО если провайдер его
    # подтвердил. Неподтверждённый email не используется для идентификации.
    trusted_email = email if (email and email_verified) else ""
    # Неподтверждённый email → синтезируем стабильный плейсхолдер, чтобы
    # NOT NULL/UNIQUE не падали; для чеков юзер укажет реальный email при оплате.
    synth_email = trusted_email or f"{provider}_{sub}@oauth.local"
    is_admin = 1 if (trusted_email and trusted_email in admin_emails()) else 0
    now = _now()

    referrer = None
    if ref_code:
        referrer = db.query_one("SELECT * FROM users WHERE referral_code=?",
                                (ref_code.strip().upper(),))

    with db.tx() as cur:
        # 1) уже знакомый соц-аккаунт
        row = cur.execute(
            "SELECT * FROM users WHERE oauth_provider=? AND oauth_sub=?",
            (provider, str(sub))).fetchone()

        # 2) тот же ПОДТВЕРЖДЁННЫЙ email → привязываем провайдера к аккаунту
        if row is None and trusted_email:
            existing = cur.execute("SELECT * FROM users WHERE email=?", (trusted_email,)).fetchone()
            if existing is not None:
                cur.execute(
                    "UPDATE users SET oauth_provider=?, oauth_sub=?, "
                    "avatar_url=COALESCE(?, avatar_url), updated_at=? WHERE id=?",
                    (provider, str(sub), avatar or None, now, existing["id"]))
                row = cur.execute("SELECT * FROM users WHERE id=?", (existing["id"],)).fetchone()

        if row is not None:
            # синхронизируем admin-флаг и аватар при каждом входе
            want_admin = 1 if (row["email"] in admin_emails()) else row["is_admin"]
            cur.execute(
                "UPDATE users SET avatar_url=COALESCE(?, avatar_url), is_admin=?, "
                "updated_at=? WHERE id=?",
                (avatar or None, want_admin, now, row["id"]))
            row = cur.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
            user_id = row["id"]
        else:
            # 3) новый аккаунт
            if cur.execute("SELECT 1 FROM users WHERE email=?", (synth_email,)).fetchone():
                # коллизия плейсхолдера (тот же sub без email) — добиваем уникальность
                synth_email = f"{provider}_{sub}_{uuid.uuid4().hex[:6]}@oauth.local"
            pw_hash = hash_password(new_session_token())   # непригодный для входа пароль
            rc = _unique_referral_code(cur)
            cur.execute(
                """INSERT INTO users
                   (email, password_hash, name, balance_cards, is_admin, referral_code,
                    referred_by, consent_pdn, consent_offer, consent_version, consent_at,
                    consent_ip, notif_email, oauth_provider, oauth_sub, avatar_url,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
                (synth_email, pw_hash, (name or "").strip() or None, 0, is_admin, rc,
                 referrer["id"] if referrer else None,
                 1, 1, CONSENT_VERSION, now, ip, provider, str(sub), avatar or None,
                 now, now))
            user_id = cur.lastrowid

            if pricing.SIGNUP_BONUS_CARDS > 0:
                _apply_delta(cur, user_id, pricing.SIGNUP_BONUS_CARDS, "signup", "signup", None)

            if referrer and referrer["id"] != user_id:
                cur.execute(
                    """INSERT OR IGNORE INTO referrals
                       (referrer_id, referee_id, status, signup_reward_cards,
                        purchase_reward_cards, created_at)
                       VALUES (?,?, 'pending', ?, ?, ?)""",
                    (referrer["id"], user_id, pricing.REFERRAL_SIGNUP_CARDS,
                     pricing.REFERRAL_PURCHASE_CARDS, now))
                if cur.rowcount and pricing.REFERRAL_SIGNUP_CARDS > 0:
                    _apply_delta(cur, user_id, pricing.REFERRAL_SIGNUP_CARDS,
                                 "referral", "referral", str(referrer["id"]), {"role": "referee"})

    user_row = get_user_row(user_id)
    token = _create_session(user_id, ip)
    return {"user": _public_user(user_row), "session": token}


# ── OAuth-handoff для Android-приложения (WebView) ───────────────────────────
# Соц-вход в приложении проходит во внешнем браузере, поэтому свежая сессия не
# может быть установлена прямо в WebView. Мост: callback создаёт одноразовый код,
# приложение получает его по deep-link и обменивает в WebView на cookie-сессию.
_OAUTH_HANDOFF_TTL_SECONDS = 120


def create_oauth_handoff(session_token: str, next_path: Optional[str]) -> str:
    """Завести одноразовый код для передачи уже созданной сессии в приложение."""
    code = new_session_token()
    expires = (dt.datetime.now(dt.timezone.utc)
               + dt.timedelta(seconds=_OAUTH_HANDOFF_TTL_SECONDS)).isoformat()
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO oauth_handoff (code, session_token, next_path, expires_at, created_at) "
            "VALUES (?,?,?,?,?)",
            (code, session_token, next_path or "/", expires, _now()))
    return code


def consume_oauth_handoff(code: str) -> Optional[dict]:
    """Разовый обмен кода на сессию: СРАЗУ удаляем (single-use) и проверяем срок.
    Заодно подчищаем протухшие коды. Возвращает {session_token, next_path} или None."""
    if not code:
        return None
    now = _now()
    with db.tx() as cur:
        row = cur.execute("SELECT * FROM oauth_handoff WHERE code=?", (code,)).fetchone()
        if row is not None:
            cur.execute("DELETE FROM oauth_handoff WHERE code=?", (code,))
        cur.execute("DELETE FROM oauth_handoff WHERE expires_at < ?", (now,))
    if row is None or row["expires_at"] < now:
        return None
    return {"session_token": row["session_token"], "next_path": row["next_path"] or "/"}


def _create_session(user_id: int, ip: Optional[str], user_agent: Optional[str] = None) -> str:
    token = new_session_token()
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=SESSION_TTL_DAYS)).isoformat()
    with db.tx() as cur:
        cur.execute(
            """INSERT INTO sessions (id, user_id, created_at, expires_at, user_agent, ip)
               VALUES (?,?,?,?,?,?)""",
            (token, user_id, _now(), expires, user_agent, ip),
        )
    return token


def user_for_session(token: Optional[str]) -> Optional[sqlite3.Row]:
    if not token:
        return None
    return db.query_one(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.id=? AND s.expires_at > ?""",
        (token, _now()),
    )


def logout(token: Optional[str]) -> None:
    if not token:
        return
    with db.tx() as cur:
        cur.execute("DELETE FROM sessions WHERE id=?", (token,))


# ── promo ────────────────────────────────────────────────────────────────────
def apply_promo(user_id: int, code: str) -> dict:
    code = (code or "").strip().upper()
    if not code:
        raise ServiceError("Введите промокод", "empty")
    now = _now()
    with db.tx() as cur:
        promo = cur.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
        if not promo or not promo["active"]:
            raise ServiceError("Промокод не найден", "not_found", 404)
        if promo["starts_at"] and promo["starts_at"] > now:
            raise ServiceError("Промокод ещё не активен", "not_started")
        if promo["expires_at"] and promo["expires_at"] < now:
            raise ServiceError("Срок действия промокода истёк", "expired")
        if (promo["max_total_uses"] is not None
                and promo["used_count"] >= promo["max_total_uses"]):
            raise ServiceError("Лимит активаций промокода исчерпан", "exhausted")
        if cur.execute("SELECT 1 FROM promo_redemptions WHERE promo_id=? AND user_id=?",
                       (promo["id"], user_id)).fetchone():
            raise ServiceError("Вы уже активировали этот промокод", "already_used", 409)
        try:
            cur.execute(
                """INSERT INTO promo_redemptions (promo_id, user_id, cards, created_at)
                   VALUES (?,?,?,?)""",
                (promo["id"], user_id, promo["cards"], now))
        except sqlite3.IntegrityError:
            raise ServiceError("Вы уже активировали этот промокод", "already_used", 409)
        cur.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE id=?",
                    (promo["id"],))
        balance = _apply_delta(cur, user_id, promo["cards"], "promo", "promo", code)
    return {"cards_added": promo["cards"], "balance_cards": balance, "code": code}


# ── purchases (idempotent, mock-settled) ─────────────────────────────────────
def _resolve_purchase_amount(cards: Optional[int]) -> tuple[int, int]:
    """Единственная модель покупки — произвольное число карточек (ползунок)."""
    q = pricing.quote_cards(int(cards or 0))
    return q["cards"], q["amount_kopecks"]


def create_purchase(user_id: int, cards: Optional[int], email: str,
                    idempotency_key: Optional[str], return_url: str) -> dict:
    email = normalize_email(email)
    if not is_valid_email(email):
        raise ServiceError("Некорректный email для чека", "bad_email")
    kind = "cards"          # старые «пакеты» убраны — остался только ползунок
    pack_key = None
    n_cards, amount_kopecks = _resolve_purchase_amount(cards)

    if idempotency_key:
        existing = db.query_one("SELECT * FROM purchases WHERE idempotency_key=?",
                                (idempotency_key,))
        if existing:
            return _purchase_with_confirmation(existing)

    pid = uuid.uuid4().hex
    provider = payments.get_provider()
    with db.tx() as cur:
        cur.execute(
            """INSERT INTO purchases
               (id, user_id, kind, pack_key, cards, amount_kopecks, currency,
                status, provider, receipt_email, idempotency_key, created_at)
               VALUES (?,?,?,?,?,?,'RUB','pending',?,?,?,?)""",
            (pid, user_id, kind, pack_key, n_cards, amount_kopecks,
             provider.name, email, idempotency_key, _now()),
        )

    desc = f"{n_cards} карточек для маркетплейса"
    receipt_items = [{
        "description": desc, "quantity": 1,
        "amount": {"value": f"{amount_kopecks/100:.2f}", "currency": "RUB"},
        "vat_code": 1, "payment_subject": "service", "payment_mode": "full_payment",
    }]
    created = provider.create_payment(
        purchase_id=pid, amount_kopecks=amount_kopecks, description=desc,
        receipt_email=email, return_url=return_url, receipt_items=receipt_items,
    )
    with db.tx() as cur:
        cur.execute("UPDATE purchases SET provider_payment_id=? WHERE id=?",
                    (created["provider_payment_id"], pid))

    out = _public_purchase(db.query_one("SELECT * FROM purchases WHERE id=?", (pid,)))
    out["confirmation_url"] = created["confirmation_url"]
    return out


def confirm_purchase(purchase_id: str, *, success: bool = True) -> dict:
    with db.tx() as cur:
        row = cur.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
        if not row:
            raise ServiceError("Покупка не найдена", "not_found", 404)
        if row["status"] == "paid":
            return _public_purchase(row)
        if not success:
            cur.execute("UPDATE purchases SET status='failed' WHERE id=?", (purchase_id,))
            return _public_purchase(
                cur.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone())
        if row["status"] != "pending":
            raise ServiceError("Покупку нельзя оплатить", "bad_state", 409)
        cur.execute("UPDATE purchases SET status='paid', paid_at=?, receipt_sent=1 WHERE id=?",
                    (_now(), purchase_id))
        _apply_delta(cur, row["user_id"], row["cards"], "purchase", "purchase",
                     purchase_id, {"kind": row["kind"], "pack_key": row["pack_key"]})
        _maybe_reward_referrer(cur, row["user_id"])
        settled = cur.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
    return _public_purchase(settled)


def _maybe_reward_referrer(cur: sqlite3.Cursor, referee_id: int) -> None:
    ref = cur.execute(
        "SELECT * FROM referrals WHERE referee_id=? AND status='pending'",
        (referee_id,)).fetchone()
    if not ref:
        return
    cur.execute("UPDATE referrals SET status='rewarded', rewarded_at=? WHERE id=?",
                (_now(), ref["id"]))
    if ref["purchase_reward_cards"] > 0:
        _apply_delta(cur, ref["referrer_id"], ref["purchase_reward_cards"],
                     "referral", "referral", str(referee_id), {"role": "referrer"})


def get_purchase(purchase_id: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM purchases WHERE id=?", (purchase_id,))
    return _public_purchase(row) if row else None


def purchase_owner(purchase_id: str) -> Optional[int]:
    row = db.query_one("SELECT user_id FROM purchases WHERE id=?", (purchase_id,))
    return row["user_id"] if row else None


def _purchase_with_confirmation(row: sqlite3.Row) -> dict:
    out = _public_purchase(row)
    if row["status"] == "pending":
        out["confirmation_url"] = f"/pay?purchase_id={row['id']}"
    return out


def _public_purchase(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "pack_key": row["pack_key"],
        "cards": row["cards"], "amount_kopecks": row["amount_kopecks"],
        "amount_rub": round(row["amount_kopecks"] / 100, 2), "currency": row["currency"],
        "status": row["status"], "receipt_email": row["receipt_email"],
        "receipt_sent": bool(row["receipt_sent"]),
        "created_at": row["created_at"], "paid_at": row["paid_at"],
    }


# ── generation spend (1 карточка за генерацию; правки бесплатны) ──────────────
def reserve_generation(user_id: int, card_count: int = 1) -> dict:
    """Списать карточки за пак ДО запуска генерации.

    Правила:
      • free доступен И card_count == 1 → используем пробу, баланс не трогаем.
      • free доступен И card_count > 1   → пробу СОХРАНЯЕМ для следующего раза,
        списываем card_count с платного баланса (юзер сознательно отказался от
        пробы ради большого пака — выгоднее ему так).
      • free НЕ доступен                 → списываем card_count с баланса.

    Раньше тут было `_apply_delta(... -1 ...)` — баг: за 10 карточек снималось 1.
    Юзер платил один раз и получал пак из 10 за треть копейки. Теперь — за каждую.
    """
    card_count = max(1, int(card_count))
    with db.tx() as cur:
        row = cur.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise ServiceError("Пользователь не найден", "not_found", 404)
        if _free_available(row) and card_count == 1:
            cur.execute("UPDATE users SET free_used_date=?, updated_at=? WHERE id=?",
                        (_today(), _now(), user_id))
            return {"mode": "free", "charged": 0, "balance_cards": int(row["balance_cards"])}
        balance = _apply_delta(cur, user_id, -card_count, "spend", "generation", None,
                               {"designs": card_count})
    return {"mode": "cards", "charged": card_count, "balance_cards": balance}


def refund_generation(user_id: int, charged: int, mode: str) -> None:
    if mode == "free":
        with db.tx() as cur:
            cur.execute("UPDATE users SET free_used_date=NULL, updated_at=? WHERE id=?",
                        (_now(), user_id))
        return
    if charged > 0:
        with db.tx() as cur:
            _apply_delta(cur, user_id, charged, "refund", "generation", None,
                         {"reason": "generation_failed"})


# ── card edit spend (правка = 1 карточка С БАЛАНСА; пробу НЕ трогаем) ─────────
def reserve_edit(user_id: int) -> dict:
    """Списать 1 карточку с БАЛАНСА за правку карточки. Бесплатную пробу НЕ
    используем — правки только за купленные карточки. Если баланса нет,
    _apply_delta бросит ServiceError 402 (insufficient_funds) → фронт зовёт
    пополнить. (Тир правки = paid, т.к. всегда списываем с баланса.)"""
    with db.tx() as cur:
        balance = _apply_delta(cur, user_id, -1, "spend", "edit", None, {"kind": "card_edit"})
    return {"charged": 1, "balance_cards": balance}


def refund_edit(user_id: int) -> None:
    """Вернуть 1 карточку, если правка не удалась (рендер упал / нет файла)."""
    with db.tx() as cur:
        _apply_delta(cur, user_id, 1, "refund", "edit", None, {"reason": "edit_failed"})


# ── description edit (1 бесплатная правка на ПЛАТНУЮ генерацию; free-проба → апселл) ──
def reserve_description_edit(gen_id: str, user_id: int) -> dict:
    """Зарезервировать правку SEO-описания.

    Правила (зеркалят оффер пользователя):
      • генерация была бесплатной пробой (charge_mode != 'cards') → правка
        описания недоступна: бросаем 402 code='free_mode' → фронт зовёт покупку.
      • платная генерация, фри-правка ещё не использована → БЕСПЛАТНО,
        помечаем desc_free_used=1.
      • платная генерация, фри-правка уже была → списываем 1 карточку с баланса
        (как правка картинки); нет баланса → 402 insufficient_funds.

    Returns: {"mode": "free_desc"|"paid", "charged": int, "balance_cards": int}
    """
    with db.tx() as cur:
        gen = cur.execute(
            "SELECT user_id, charge_mode, desc_free_used FROM generations WHERE id=?",
            (gen_id,)).fetchone()
        if gen is None or gen["user_id"] != user_id:
            raise ServiceError("Генерация не найдена", "not_found", 404)
        if gen["charge_mode"] != "cards":
            raise ServiceError(
                "Правка описания доступна для платных карточек. "
                "Оформите подписку, чтобы редактировать описание.",
                "free_mode", 402)
        bal = cur.execute("SELECT balance_cards FROM users WHERE id=?", (user_id,)).fetchone()
        balance = int(bal["balance_cards"]) if bal else 0
        if not gen["desc_free_used"]:
            # Атомарно: занимаем фри-правку только если она ещё свободна
            # (WHERE desc_free_used=0). Защита от двойного списания/двойной
            # бесплатной правки даже при гонке/мульти-воркере.
            cur.execute(
                "UPDATE generations SET desc_free_used=1, updated_at=? "
                "WHERE id=? AND desc_free_used=0", (_now(), gen_id))
            if cur.rowcount == 1:
                return {"mode": "free_desc", "charged": 0, "balance_cards": balance}
            # гонку проиграли — фри уже занят, идём по платному пути
        balance = _apply_delta(cur, user_id, -1, "spend", "desc_edit", gen_id,
                               {"kind": "description_edit"})
    return {"mode": "paid", "charged": 1, "balance_cards": balance}


def refund_description_edit(gen_id: str, user_id: int, mode: str) -> None:
    """Откат при неудачной правке описания: фри-попытку возвращаем флагом,
    платную — карточкой на баланс."""
    if mode == "free_desc":
        with db.tx() as cur:
            cur.execute("UPDATE generations SET desc_free_used=0, updated_at=? WHERE id=?",
                        (_now(), gen_id))
        return
    if mode == "paid":
        with db.tx() as cur:
            _apply_delta(cur, user_id, 1, "refund", "desc_edit", gen_id,
                         {"reason": "desc_edit_failed"})


# ── account management ───────────────────────────────────────────────────────
def update_account(user_id: int, *, name: Optional[str] = None,
                   notif_email: Optional[bool] = None,
                   new_password: Optional[str] = None,
                   current_password: Optional[str] = None) -> dict:
    with db.tx() as cur:
        row = cur.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise ServiceError("Пользователь не найден", "not_found", 404)
        if name is not None:
            cur.execute("UPDATE users SET name=?, updated_at=? WHERE id=?",
                        ((name or "").strip() or None, _now(), user_id))
        if notif_email is not None:
            cur.execute("UPDATE users SET notif_email=?, updated_at=? WHERE id=?",
                        (1 if notif_email else 0, _now(), user_id))
        if new_password:
            if not verify_password(current_password or "", row["password_hash"]):
                raise ServiceError("Текущий пароль неверный", "bad_credentials", 401)
            cur.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                        (hash_password(new_password), _now(), user_id))
    return _public_user(get_user_row(user_id))


def delete_account(user_id: int) -> None:
    """152-ФЗ право на удаление: ПДн + авторизация удаляются, финансы обезличиваются."""
    with db.tx() as cur:
        cur.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        cur.execute("DELETE FROM promo_redemptions WHERE user_id=?", (user_id,))
        cur.execute("UPDATE referrals SET status='void' WHERE referrer_id=? OR referee_id=?",
                    (user_id, user_id))
        cur.execute("UPDATE purchases SET receipt_email='deleted@anonymized' WHERE user_id=?",
                    (user_id,))
        cur.execute("DELETE FROM users WHERE id=?", (user_id,))


# ── history & referral views ─────────────────────────────────────────────────
def history(user_id: int, limit: int = 50) -> dict:
    purchases = [_public_purchase(r) for r in db.query_all(
        "SELECT * FROM purchases WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit))]
    ledger = [{
        "id": r["id"], "delta": r["delta"], "balance_after": r["balance_after"],
        "reason": r["reason"], "ref_type": r["ref_type"], "ref_id": r["ref_id"],
        "meta": json.loads(r["meta"]) if r["meta"] else None,
        "created_at": r["created_at"],
    } for r in db.query_all(
        "SELECT * FROM card_ledger WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit))]
    return {"purchases": purchases, "ledger": ledger}


# ── background generations (persisted, resumable) ────────────────────────────
def gen_create(gen_id: str, user_id: int, chat_id: Optional[str], title: Optional[str],
               card_count: int, request: dict, charged: int, charge_mode: str) -> None:
    now = _now()
    with db.tx() as cur:
        cur.execute(
            """INSERT INTO generations
               (id, user_id, chat_id, title, status, progress, card_count, request_json,
                files_json, charged, charge_mode, created_at, updated_at)
               VALUES (?,?,?,?, 'running', 0, ?, ?, '[]', ?, ?, ?, ?)""",
            (gen_id, user_id, chat_id, title, card_count,
             json.dumps(request, ensure_ascii=False), charged, charge_mode, now, now))


def gen_update(gen_id: str, *, status: Optional[str] = None, progress: Optional[int] = None,
               caption: Optional[str] = None, files: Optional[list] = None,
               description: Optional[str] = None, design: Optional[dict] = None,
               error: Optional[str] = None) -> None:
    sets, vals = [], []
    if status is not None: sets.append("status=?"); vals.append(status)
    if progress is not None: sets.append("progress=?"); vals.append(int(progress))
    if caption is not None: sets.append("caption=?"); vals.append(caption)
    if files is not None: sets.append("files_json=?"); vals.append(json.dumps(files, ensure_ascii=False))
    if description is not None: sets.append("description=?"); vals.append(description)
    if design is not None: sets.append("design_json=?"); vals.append(json.dumps(design, ensure_ascii=False))
    if error is not None: sets.append("error=?"); vals.append(error)
    sets.append("updated_at=?"); vals.append(_now())
    vals.append(gen_id)
    with db.tx() as cur:
        cur.execute(f"UPDATE generations SET {', '.join(sets)} WHERE id=?", vals)


def _gen_public(row: sqlite3.Row) -> dict:
    files = json.loads(row["files_json"]) if row["files_json"] else []
    # Маркетплейс для иконки в сайдбаре — достаём из исходного request_json,
    # который gen_create записывает атомарно. От любых сюрпризов защищаемся.
    marketplace = None
    try:
        req = json.loads(row["request_json"]) if row["request_json"] else {}
        marketplace = req.get("marketplace") or req.get("market") or req.get("kind")
    except Exception:
        marketplace = None
    # thumb для сайдбара — первая готовая карточка. Для running без карточек
    # пусто, фронт подставит дефолтную плашку.
    thumb = files[0] if files else None
    return {
        "id": row["id"], "status": row["status"], "progress": row["progress"],
        "caption": row["caption"], "title": row["title"], "card_count": row["card_count"],
        "files": files,
        "description": row["description"] or "",
        "design": json.loads(row["design_json"]) if row["design_json"] else None,
        "error": row["error"], "chat_id": row["chat_id"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "marketplace": marketplace,   # 'wb' | 'oz' | None — иконка в сайдбаре
        "thumb": thumb,               # URL первой карточки либо null
    }


def gen_get(gen_id: str, user_id: Optional[int] = None) -> Optional[dict]:
    row = db.query_one("SELECT * FROM generations WHERE id=?", (gen_id,))
    if not row:
        return None
    if user_id is not None and row["user_id"] != user_id:
        return None
    return _gen_public(row)


def gen_owner(gen_id: str) -> Optional[int]:
    r = db.query_one("SELECT user_id FROM generations WHERE id=?", (gen_id,))
    return r["user_id"] if r else None


def gen_active(user_id: int, limit: int = 8) -> list[dict]:
    """Последние генерации юзера (любого статуса), новые сверху.
    Раньше дефолтно отдавали 8 — слишком мало для сайдбара истории чатов,
    после переустановки приложения юзер видел только 8 последних. limit
    клампим на верхней границе (200), чтобы не отдать слишком жирный JSON."""
    limit = max(1, min(int(limit or 8), 200))
    rows = db.query_all(
        "SELECT * FROM generations WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit))
    return [_gen_public(r) for r in rows]


def gen_mark_stale_running() -> None:
    """При старте сервера незавершённые джобы помечаем error (поток не пережил рестарт)."""
    with db.tx() as cur:
        cur.execute("UPDATE generations SET status='error', error='прервано рестартом сервера', "
                    "updated_at=? WHERE status='running'", (_now(),))


# ── admin write operations ───────────────────────────────────────────────────
def admin_adjust_balance(user_id: int, delta: int, note: Optional[str]) -> int:
    with db.tx() as cur:
        if not cur.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise ServiceError("Пользователь не найден", "not_found", 404)
        return _apply_delta(cur, user_id, int(delta), "adjust", "admin", note, {"note": note})


def set_admin_flag(user_id: int, value: bool) -> None:
    with db.tx() as cur:
        cur.execute("UPDATE users SET is_admin=?, updated_at=? WHERE id=?",
                    (1 if value else 0, _now(), user_id))


def create_promo(code: str, cards: int, max_total_uses: Optional[int],
                 per_user_limit: int) -> None:
    code = (code or "").strip().upper()
    if not code:
        raise ServiceError("Пустой код", "empty")
    with db.tx() as cur:
        if cur.execute("SELECT 1 FROM promo_codes WHERE code=?", (code,)).fetchone():
            raise ServiceError("Такой промокод уже есть", "exists", 409)
        cur.execute(
            """INSERT INTO promo_codes
               (code, kind, cards, max_total_uses, per_user_limit, active, created_at)
               VALUES (?, 'cards', ?, ?, ?, 1, ?)""",
            (code, int(cards), max_total_uses, int(per_user_limit or 1), _now()))


def toggle_promo(promo_id: int, active: bool) -> None:
    with db.tx() as cur:
        cur.execute("UPDATE promo_codes SET active=? WHERE id=?", (1 if active else 0, promo_id))


def referral_stats(user_id: int) -> dict:
    row = get_user_row(user_id)
    invited = db.query_one(
        "SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status!='void'",
        (user_id,))["c"]
    rewarded = db.query_one(
        "SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status='rewarded'",
        (user_id,))["c"]
    earned = db.query_one(
        """SELECT COALESCE(SUM(delta),0) s FROM card_ledger
           WHERE user_id=? AND reason='referral' AND delta>0""",
        (user_id,))["s"]
    return {
        "referral_code": row["referral_code"],
        "invited": invited, "rewarded": rewarded, "cards_earned": int(earned),
        "reward_per_purchase": pricing.REFERRAL_PURCHASE_CARDS,
        "reward_signup": pricing.REFERRAL_SIGNUP_CARDS,
    }

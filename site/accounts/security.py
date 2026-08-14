"""
Security primitives — password hashing and email validation.

Passwords: ``hashlib.scrypt`` (memory-hard, stdlib, no external deps). Stored as
``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`` so parameters travel with the hash
and can be upgraded later without breaking existing users. A pbkdf2 fallback is
provided for the (unlikely) case scrypt is unavailable in a given OpenSSL build.

Session tokens are opaque random strings stored server-side (see service.py /
db.sessions), so there is nothing to forge — we never trust client-supplied
state for identity.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

# scrypt cost params (CPU/memory). n must be a power of two.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024   # 64 MiB headroom for n=16384,r=8,p=1

_PBKDF2_ROUNDS = 200_000

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def hash_password(password: str) -> str:
    if not password or len(password) < 6:
        raise ValueError("Пароль должен быть не короче 6 символов")
    salt = secrets.token_bytes(16)
    pw = password.encode("utf-8")
    try:
        dk = hashlib.scrypt(pw, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                            dklen=_SCRYPT_DKLEN, maxmem=_SCRYPT_MAXMEM)
        return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"
    except (ValueError, Exception):
        dk = hashlib.pbkdf2_hmac("sha256", pw, salt, _PBKDF2_ROUNDS, dklen=_SCRYPT_DKLEN)
        return f"pbkdf2${_PBKDF2_ROUNDS}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or not password:
        return False
    try:
        parts = stored.split("$")
        scheme = parts[0]
        pw = password.encode("utf-8")
        if scheme == "scrypt":
            _, n, r, p, salt_b64, hash_b64 = parts
            dk = hashlib.scrypt(pw, salt=_b64d(salt_b64), n=int(n), r=int(r), p=int(p),
                                dklen=len(_b64d(hash_b64)), maxmem=_SCRYPT_MAXMEM)
            return hmac.compare_digest(dk, _b64d(hash_b64))
        if scheme == "pbkdf2":
            _, rounds, salt_b64, hash_b64 = parts
            expected = _b64d(hash_b64)
            dk = hashlib.pbkdf2_hmac("sha256", pw, _b64d(salt_b64), int(rounds), dklen=len(expected))
            return hmac.compare_digest(dk, expected)
    except Exception:
        return False
    return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_referral_code() -> str:
    """Short, unambiguous, shareable code (no 0/O/1/I confusion)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(7))


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    email = normalize_email(email)
    return bool(_EMAIL_RE.match(email)) and len(email) <= 254

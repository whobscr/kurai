"""Passwords, TOTP, recovery codes and the encryption of stored secrets."""

import base64
import hashlib
import hmac
import re
import secrets
import time

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from kurai.config import get_config

_hasher = PasswordHasher()

TOTP_ISSUER = "Kurai"
RECOVERY_CODES = 8


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_id(token: str) -> str:
    """What is stored for a session token: its hash, so a database copy holds no usable cookie."""
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- stored secrets (API keys, the Telegram token, Navidrome passwords, TOTP secrets) ----------


def _fernet() -> Fernet:
    digest = hashlib.sha256(("kurai-secrets:" + get_config().secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str | None) -> str:
    """The plain text, or "" when there is none or it was encrypted with another KURAI_SECRET_KEY."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


# ---------- TOTP (optional, per user) ----------


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=TOTP_ISSUER)


def totp_step(secret: str, code: str, after: int | None = None) -> int | None:
    """The time step of a valid code (the current one, or the one before or after, for a clock that is a little
    off), or None. A step not later than `after` (the last code used) doesn't count: a code works once."""
    code = code.replace(" ", "")
    if not secret or not (code.isdigit() and len(code) == 6):
        return None
    totp = pyotp.TOTP(secret)
    now = int(time.time()) // totp.interval
    for step in (now, now - 1, now + 1):
        if (after is None or step > after) and hmac.compare_digest(totp.at(step * totp.interval), code):
            return step
    return None


def looks_like_recovery_code(code: str) -> bool:
    """«abc123-def456»: only these are checked against the (slow to compare) stored hashes."""
    return bool(re.fullmatch(r"[0-9a-f]{6}-[0-9a-f]{6}", code.strip().lower()))


class Throttle:
    """At most `limit` failures per key within `window` seconds (in this process's memory: a restart forgets)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        now = time.monotonic()
        recent = [t for t in self._failures.get(key, []) if now - t < self.window]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.limit

    def fail(self, key: str) -> None:
        self._failures.setdefault(key, []).append(time.monotonic())
        if len(self._failures) > 10_000:  # forget the stale keys now and then
            for k in list(self._failures):
                self._recent(k)

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)


def new_recovery_codes() -> tuple[list[str], list[str]]:
    """Codes to show once, and their hashes to store."""
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(RECOVERY_CODES)]
    return codes, [hash_password(c) for c in codes]


def use_recovery_code(hashes: list[str], code: str) -> list[str] | None:
    """The remaining hashes when the code is one of them (it can't be used again), else None."""
    code = code.strip().lower()
    for i, h in enumerate(hashes):
        if verify_password(h, code):
            return hashes[:i] + hashes[i + 1 :]
    return None

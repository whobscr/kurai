"""Passwords, TOTP, recovery codes and the encryption of stored secrets."""

import base64
import hashlib
import secrets

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


def verify_totp(secret: str, code: str) -> bool:
    code = code.replace(" ", "")
    return bool(secret) and code.isdigit() and pyotp.TOTP(secret).verify(code, valid_window=1)


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

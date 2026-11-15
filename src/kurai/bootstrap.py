"""What a fresh installation needs before anyone logs in: the first admin (admin / admin, changed at the first login),
Kurai's own Navidrome admin account, and the web push keys. Safe to run on every start."""

import base64
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select

from kurai import db as database
from kurai import security, settings
from kurai.db.models import User
from kurai.i18n import _
from kurai.recommend import library

log = logging.getLogger(__name__)

DEFAULT_ADMIN = ("admin", "admin")


def _vapid_keys() -> tuple[str, str]:
    """A P-256 key pair: the public key as the browser wants it (uncompressed point, base64url), the private one as
    PEM (pywebpush's format)."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return base64.urlsafe_b64encode(public).decode().rstrip("="), private


async def bootstrap() -> None:
    async with database.session() as db:
        # a fresh installation has no admin (the last one can't be demoted or disabled)
        if (await db.execute(select(func.count()).select_from(User).where(User.role == "admin"))).scalar_one() == 0:
            username, password = DEFAULT_ADMIN
            db.add(User(username=username, display_name=_("Администратор"),
                        password_hash=security.hash_password(password), role="admin", must_change_password=True))
            log.info("created the first admin: %s / %s (to be changed at the first login)", username, password)
        values = await settings.get_all(db)
        # a secret that can't be read is never replaced: with the right KURAI_SECRET_KEY back it works again
        locked = set(await settings.unreadable(db))
        if locked:
            log.error("stored secrets can't be decrypted (KURAI_SECRET_KEY changed?): %s", ", ".join(sorted(locked)))
        if not values["webpush.private_key"] and "webpush.private_key" not in locked:
            public, private = _vapid_keys()
            await settings.put(db, {"webpush.public_key": public, "webpush.private_key": private})
        if not values["navidrome.admin_password"] and "navidrome.admin_password" not in locked:
            await settings.put(db, {"navidrome.admin_password": security.random_token(24)})
        await db.commit()
        try:
            if await library.navidrome().ensure_admin(await library.admin_account(db)):
                log.info("created Kurai's admin account in Navidrome")
        except Exception as e:  # Navidrome may still be starting: the library sync and account changes try again
            log.warning("Navidrome's admin account not ready: %s", e)

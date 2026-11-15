"""Users: added by an admin only (no self-registration). Each gets a matching Navidrome account with the same
username and password, so the music apps log in with the same credentials; Kurai keeps that password encrypted to
write the user's private playlists."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import security
from kurai.db.models import User
from kurai.i18n import _
from kurai.recommend.library import admin_account, navidrome

MIN_PASSWORD = 8


class AccountError(Exception):
    pass


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(_("Пароль — не короче 8 символов"))


async def create_user(db: AsyncSession, username: str, display_name: str, password: str, role: str = "user") -> User:
    username = username.strip().lower()
    if not username.isascii() or not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise AccountError(_("Имя для входа — латинские буквы, цифры, «.», «-», «_»"))
    check_password(password)
    if (await db.execute(select(User).where(User.username == username))).scalar_one_or_none():
        raise AccountError(_("Такое имя для входа уже есть"))
    user = User(username=username, display_name=display_name.strip() or username,
                password_hash=security.hash_password(password), role="admin" if role == "admin" else "user")
    nd = navidrome()
    token = await nd.login(await admin_account(db))
    user.navidrome_id = await nd.create_user(token, username, user.display_name, password)
    user.navidrome_password = security.encrypt(password)
    db.add(user)
    await db.commit()
    return user


async def set_password(db: AsyncSession, user: User, password: str) -> None:
    """Kurai's and Navidrome's password together (one account for the web app and the music apps)."""
    check_password(password)
    nd = navidrome()
    token = await nd.login(await admin_account(db))
    if user.navidrome_id:
        await nd.set_password(token, user.navidrome_id, user.username, user.display_name, password)
    else:  # the first admin (made before Navidrome was reachable) gets its music account here
        user.navidrome_id = await nd.create_user(token, user.username, user.display_name, password)
    user.navidrome_password = security.encrypt(password)
    user.password_hash = security.hash_password(password)
    user.must_change_password = False
    await db.commit()


async def remaining_admins(db: AsyncSession, except_id: int) -> int:
    return len((await db.execute(select(User.id).where(User.role == "admin", User.active.is_(True),
                                                       User.id != except_id))).all())


async def set_role(db: AsyncSession, user: User, role: str) -> None:
    if role != "admin" and user.is_admin and await remaining_admins(db, user.id) == 0:
        raise AccountError(_("Нельзя снять роль с последнего администратора"))
    user.role = "admin" if role == "admin" else "user"
    await db.commit()


async def set_active(db: AsyncSession, user: User, active: bool) -> None:
    if not active and user.is_admin and await remaining_admins(db, user.id) == 0:
        raise AccountError(_("Нельзя отключить последнего администратора"))
    user.active = active
    await db.commit()

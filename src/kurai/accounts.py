"""Users: added by an admin only (no self-registration). Each gets a matching Navidrome account with the same
username and password, so the music apps log in with the same credentials; Kurai keeps that password encrypted to
write the user's private playlists."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import security
from kurai.db.models import Listen, Playlist, PulseEvent, Session, Taste, User
from kurai.i18n import _
from kurai.integrations.navidrome import NavidromeError, forget_account
from kurai.jobs import queue
from kurai.recommend import playlists
from kurai.recommend.library import admin_login, user_account

log = logging.getLogger(__name__)

MIN_PASSWORD = 8
SESSION_DAYS = 30  # a session unused this long ends (the cookie lives as long)


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
    nd, token = await admin_login(db)
    user.navidrome_id = await nd.create_user(token, username, user.display_name, password)
    user.navidrome_password = security.encrypt(password)
    db.add(user)
    try:
        await db.commit()
    except Exception:  # e.g. the same name added at the same moment: the music account goes too
        await db.rollback()
        await nd.delete_user(token, user.navidrome_id)
        raise
    return user


async def set_password(db: AsyncSession, user: User, password: str) -> None:
    """Kurai's and Navidrome's password together (one account for the web app and the music apps)."""
    check_password(password)
    nd, token = await admin_login(db)
    if user.navidrome_id:
        await nd.set_password(token, user.navidrome_id, user.username, user.display_name, password)
    else:  # the first admin (made before Navidrome was reachable) gets its music account here
        user.navidrome_id = await nd.create_user(token, user.username, user.display_name, password)
    user.navidrome_password = security.encrypt(password)
    user.password_hash = security.hash_password(password)
    user.must_change_password = False
    await db.commit()


async def forget_listens(db: AsyncSession, user: User) -> None:
    """«Забыть прослушивания». Navidrome can't clear an account's plays, so the user's music account is made anew,
    with the same name and password (the music apps go on logging in): Navidrome deletes the old one's plays, stars,
    ratings, playlists and play queue with it. Kurai forgets its copy of them, the pulse's plays and skips, the taste
    and «Любимое», writes its own playlists into the new account and makes the taste (from «Что вы любите слушать» only)
    and the pulse anew. The old account's id is let go before the new one is made, so after a failure halfway a
    second try only makes the account."""
    account = user_account(user)
    if account is not None:
        nd, token = await admin_login(db)
        if user.navidrome_id:
            await nd.delete_user(token, user.navidrome_id)
            user.navidrome_id = None
            await db.commit()
        forget_account(user.username)
        user.navidrome_id = await nd.create_user(token, user.username, user.display_name, account.password)
    for model in (Listen, PulseEvent, Taste):
        await db.execute(delete(model).where(model.user_id == user.id))
    await db.execute(update(Playlist).where(Playlist.user_id == user.id).values(navidrome_id=None))
    await db.commit()
    await playlists.build_favorites(db, user, create=False)  # «Любимое» has nothing left: it goes
    for playlist in (await db.execute(select(Playlist).where(Playlist.user_id == user.id))).scalars():
        try:
            await playlists.write(db, user, playlist)
        except NavidromeError as e:  # the next version of it is written anyway
            log.warning("playlist %s not written into the new account: %s", playlist.id, e)
    await queue.enqueue(db, "pulse", {"retaste": True}, user_id=user.id, priority=20, dedupe_key=f"pulse-start:{user.id}")
    await db.commit()


async def end_sessions(db: AsyncSession, user_id: int, except_id: str | None = None) -> None:
    """Logs the user out everywhere (but the session `except_id`), e.g. after a password change."""
    query = delete(Session).where(Session.user_id == user_id)
    if except_id:
        query = query.where(Session.id != except_id)
    await db.execute(query)
    await db.commit()


async def prune_sessions(db: AsyncSession) -> int:
    """Removes the sessions unused for longer than a session lives (the night's work does it)."""
    before = datetime.now(UTC) - timedelta(days=SESSION_DAYS)
    removed = (await db.execute(delete(Session).where(Session.last_seen < before))).rowcount
    await db.commit()
    return removed


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

"""The plugin's work: find new album folders in the incoming folder, and move the approved ones into the library."""

import asyncio
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import settings
from kurai.notify import notify_admins
from kurai_incoming.config import get_config
from kurai_incoming.models import IncomingAlbum

AUDIO = {".flac", ".mp3", ".ogg", ".opus", ".m4a", ".wav", ".aiff", ".ape", ".wv"}


def parse_folder(name: str) -> tuple[str, str]:
    """«Artist - Album» → (artist, album); anything else is just the album's title."""
    artist, sep, title = name.partition(" - ")
    return (artist.strip(), title.strip()) if sep and title.strip() else ("", name.strip())


def _safe(name: str) -> str:
    return "".join("_" if c in '/\\:*?"<>|' else c for c in name).strip(" .") or "_"


async def scan(db: AsyncSession) -> dict:
    """The job «incoming_scan»: new folders become rows (approved at once with auto-approval); approved ones move."""
    values = await settings.get_all(db)
    root = Path(values["incoming.dir"])
    if not root.is_dir():
        return {"skipped": f"нет папки {root}"}
    known = set((await db.execute(select(IncomingAlbum.folder))).scalars())
    found = 0
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in known):
        files = [f for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO]
        if not files:
            continue
        artist, title = parse_folder(folder.name)
        db.add(IncomingAlbum(folder=folder.name, artist=artist, title=title, files=len(files),
                             size=sum(f.stat().st_size for f in files),
                             status="approved" if values["incoming.auto_approve"] else "new"))
        found += 1
    await db.commit()
    if found and not values["incoming.auto_approve"]:
        await notify_admins(db, f"Во входящих новых альбомов: {found}", "/admin/incoming")
    return {"found": found, "moved": await move_approved(db, root, Path(get_config().music_dir))}


async def move_approved(db: AsyncSession, root: Path, library: Path) -> int:
    moved = 0
    for album in (await db.execute(select(IncomingAlbum).where(IncomingAlbum.status == "approved"))).scalars().all():
        source = root / album.folder
        target = library / _safe(album.artist or "Разные исполнители") / _safe(album.title)
        if not source.is_dir():
            album.status, album.note = "failed", "папки больше нет"
        elif target.exists():
            album.status, album.note = "failed", f"в библиотеке уже есть {target.relative_to(library)}"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, source, target)  # files: off the event loop
            album.status, album.note = "moved", str(target.relative_to(library))
            moved += 1
        await db.commit()
    return moved  # Navidrome finds the new files at its next scan (hourly by default)

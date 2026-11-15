"""The plugin's table. It is declared on Kurai's Base (so a foreign key to `users` would resolve) but created only by
the plugin's own migrations (migrations/), never by Kurai's."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from kurai.db.models import Base


class IncomingAlbum(Base):
    """A folder found in the incoming folder."""

    __tablename__ = "incoming_albums"

    id: Mapped[int] = mapped_column(primary_key=True)
    folder: Mapped[str] = mapped_column(Text, unique=True)  # its name inside the incoming folder
    artist: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    files: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="new")  # new | approved | rejected | moved | failed
    note: Mapped[str] = mapped_column(Text, default="")
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

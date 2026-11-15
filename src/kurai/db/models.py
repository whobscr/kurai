"""The database schema. Changes go through Alembic migrations (migrations/versions), never by hand."""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from kurai.audio import CLAP_DIM, EFFNET_DIM


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="user")  # "admin" | "user"
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret: Mapped[str | None] = mapped_column(Text)  # encrypted (security.secrets)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    recovery_codes: Mapped[list] = mapped_column(JSONB, default=list)  # argon2 hashes, one use each
    accent_color: Mapped[str | None] = mapped_column(String(7))  # None: the system's default
    theme_mode: Mapped[str] = mapped_column(String(8), default="auto")  # "light" | "dark" | "auto"
    # the matching Navidrome account; its password is Kurai's to keep (encrypted) so it can write the user's playlists
    navidrome_id: Mapped[str | None] = mapped_column(String(64))
    navidrome_password: Mapped[str | None] = mapped_column(Text)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_link_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    notify_push: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_telegram: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _now()

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie's token
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # the password was right but the TOTP code is still to come
    pending_totp: Mapped[bool] = mapped_column(Boolean, default=False)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()
    last_seen: Mapped[datetime] = _now()


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB)


class Track(Base):
    """A track of the library, as Navidrome knows it."""

    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Navidrome's song id
    title: Mapped[str] = mapped_column(Text)
    artist: Mapped[str] = mapped_column(Text, default="")
    album: Mapped[str] = mapped_column(Text, default="")
    album_id: Mapped[str] = mapped_column(String(64), default="")
    artist_mbid: Mapped[str | None] = mapped_column(String(36))
    release_group_mbid: Mapped[str | None] = mapped_column(String(36))
    path: Mapped[str] = mapped_column(Text, default="")
    duration: Mapped[int] = mapped_column(Integer, default=0)  # seconds
    year: Mapped[int | None] = mapped_column(Integer)
    genres: Mapped[list] = mapped_column(JSONB, default=list)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    # the text embedding of "artist — title — album — genres" and the model that made it (vectors of different models
    # can't be compared: a model change re-embeds everything)
    embedding = mapped_column(Vector(), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    seen_at: Mapped[datetime] = _now()


class TrackAudio(Base):
    """What the audio analysis heard in a track's file (kurai.audio). A changed file (another size) is analysed again;
    a file that couldn't be analysed keeps its error and is skipped until it changes or an admin retries."""

    __tablename__ = "track_audio"

    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)  # the file's size when analysed
    effnet = mapped_column(Vector(EFFNET_DIM), nullable=True)  # the sound (Essentia's Discogs-EffNet)
    effnet_model: Mapped[str | None] = mapped_column(String(64))
    clap = mapped_column(Vector(CLAP_DIM), nullable=True)  # sound and words in one space (CLAP)
    clap_model: Mapped[str | None] = mapped_column(String(128))
    bpm: Mapped[float | None] = mapped_column(Float)
    key: Mapped[str | None] = mapped_column(String(16))  # e.g. "A minor"
    danceability: Mapped[float | None] = mapped_column(Float)  # 0..1
    voice: Mapped[float | None] = mapped_column(Float)  # 0 (instrumental) .. 1 (vocals)
    moods: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"happy": 0.8, …}
    styles: Mapped[list] = mapped_column(JSONB, default=list)  # Discogs styles, [["Electronic---Ambient", 0.6], …]
    themes: Mapped[list] = mapped_column(JSONB, default=list)  # MTG-Jamendo moods/themes, [["calm", 0.4], …]
    error: Mapped[str | None] = mapped_column(Text)
    seconds: Mapped[float | None] = mapped_column(Float)  # how long the analysis took
    analyzed_at: Mapped[datetime] = _now()


class Listen(Base):
    """What one user did with one track (Navidrome's play count, last play, star and rating)."""

    __tablename__ = "listens"
    __table_args__ = (UniqueConstraint("user_id", "track_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    last_played: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    starred: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[int] = mapped_column(Integer, default=0)  # 0-5, 0: none


class Taste(Base):
    """A user's taste: the weighted mean of the (unit) vectors of what they played and liked; not normalized, so the
    recommendations can centre it on the library's mean like any track (vectors.Joint)."""

    __tablename__ = "tastes"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    vector = mapped_column(Vector(), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    audio_vector = mapped_column(Vector(EFFNET_DIM), nullable=True)  # the same, from how the tracks sound
    top_artists: Mapped[list] = mapped_column(JSONB, default=list)
    tracks_used: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = _now()


class Playlist(Base):
    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # "nightly" | "ask" | "starter"
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    track_ids: Mapped[list] = mapped_column(JSONB, default=list)
    navidrome_id: Mapped[str | None] = mapped_column(String(64))
    feedback: Mapped[int] = mapped_column(Integer, default=0)  # +1 / -1 / 0
    created_at: Mapped[datetime] = _now()


class Job(Base):
    """A unit of background work. The worker takes them by priority; night-only ones wait for the night window."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | done | failed
    priority: Mapped[int] = mapped_column(Integer, default=50)  # lower runs first
    night_only: Mapped[bool] = mapped_column(Boolean, default=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    dedupe_key: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    link: Mapped[str] = mapped_column(Text, default="")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    endpoint: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    keys: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class LlmCall(Base):
    """One model request's timing, so the pages can say «usually takes ~N s» and the night's capacity."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    task: Mapped[str] = mapped_column(String(48))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    thinking: Mapped[bool] = mapped_column(Boolean, default=False)
    seconds: Mapped[float] = mapped_column(Float)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _now()

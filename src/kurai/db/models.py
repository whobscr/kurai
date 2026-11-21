"""The database schema. Changes go through Alembic migrations (migrations/versions), never by hand."""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from kurai.artists import artist_key
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
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)  # the last code's time step: a code works once
    recovery_codes: Mapped[list] = mapped_column(JSONB, default=list)  # argon2 hashes, one use each
    accent_color: Mapped[str | None] = mapped_column(String(7))  # None: the system's default
    theme_mode: Mapped[str] = mapped_column(String(8), default="auto")  # "light" | "dark" | "auto"
    # the matching Navidrome account; its password is Kurai's to keep (encrypted) so it can write the user's playlists
    navidrome_id: Mapped[str | None] = mapped_column(String(64))
    navidrome_password: Mapped[str | None] = mapped_column(Text)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_link_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    telegram_link_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # the code is valid an hour
    notify_push: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_telegram: Mapped[bool] = mapped_column(Boolean, default=True)
    # what they said they like before listening to anything (recommend/start.py): styles of the library, own words;
    # None: not asked yet
    start_styles: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    start_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    start_asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # the first taste made from those answers (stored once at start_asked_at, not re-drawn nightly): same shape as a
    # taste vector
    start_text_vector = mapped_column(Vector(), nullable=True)
    start_audio_vector = mapped_column(Vector(EFFNET_DIM), nullable=True)
    start_top_artists: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    start_text_model: Mapped[str | None] = mapped_column(String(128))
    start_audio_model: Mapped[str | None] = mapped_column(String(64))
    # «Любимое»: how many tracks, and whether the night makes it anew
    favorites_size: Mapped[int] = mapped_column(Integer, default=50, server_default="50")
    favorites_nightly: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
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
    __table_args__ = (Index("ix_tracks_artist", "artist"), Index("ix_tracks_artist_key", "artist_key"))

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Navidrome's song id
    title: Mapped[str] = mapped_column(Text)
    artist: Mapped[str] = mapped_column(Text, default="")
    # kurai/artists.py: who it's by; made from `artist` when not given (the library's sync gives it, updates too)
    artist_key: Mapped[str] = mapped_column(Text, server_default="",
                                            default=lambda ctx: artist_key(ctx.get_current_parameters().get("artist")))
    album: Mapped[str] = mapped_column(Text, default="")
    album_id: Mapped[str] = mapped_column(String(64), default="")
    artist_mbid: Mapped[str | None] = mapped_column(String(36))
    release_group_mbid: Mapped[str | None] = mapped_column(String(36))
    recording_mbid: Mapped[str | None] = mapped_column(String(36))
    disc_number: Mapped[int | None] = mapped_column(Integer)
    track_number: Mapped[int | None] = mapped_column(Integer)
    # how much the world listens to this recording (ListenBrainz, recommend/popularity.py); None: not asked yet
    listens: Mapped[int | None] = mapped_column(BigInteger)
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
    __table_args__ = (
        Index("ix_track_audio_effnet", "effnet", postgresql_using="hnsw",
              postgresql_ops={"effnet": "vector_cosine_ops"}),
        Index("ix_track_audio_clap", "clap", postgresql_using="hnsw", postgresql_ops={"clap": "vector_cosine_ops"}),
    )

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
    __table_args__ = (UniqueConstraint("user_id", "track_id"), Index("ix_listens_user", "user_id"))

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    last_played: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    starred: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[int] = mapped_column(Integer, default=0)  # 0-5, 0: none
    # when Kurai itself last wrote this row (note_listen). sync_listens's grace window keeps recent local writes even
    # if Navidrome hasn't caught up yet — a failed Navidrome write no longer erases the user's explicit action.
    updated_at: Mapped[datetime] = _now()


class Taste(Base):
    """A user's taste: the weighted mean of the (unit) vectors of what they played and liked; not normalized, so the
    recommendations can centre it on the library's mean like any track (vectors.Joint)."""
    __tablename__ = "tastes"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    vector = mapped_column(Vector(), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    audio_vector = mapped_column(Vector(EFFNET_DIM), nullable=True)  # the same, from how the tracks sound
    # the Essentia model that made `audio_vector`; a change invalidates it until the next build_taste
    audio_model: Mapped[str | None] = mapped_column(String(64))
    top_artists: Mapped[list] = mapped_column(JSONB, default=list)
    # «слушают вместе» (recommend/related.py): [{"name", "mbid", "score", "via", "depth"}], the strongest first
    related_artists: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    related_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracks_used: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = _now()


class Playlist(Base):
    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # "pulse" | "nightly" | "ask" | "starter" | "saved" (the user's own) | "favorites" («Любимое», one a user)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    track_ids: Mapped[list] = mapped_column(JSONB, default=list)
    navidrome_id: Mapped[str | None] = mapped_column(String(64))
    feedback: Mapped[int] = mapped_column(Integer, default=0)  # +1 / -1 / 0
    created_at: Mapped[datetime] = _now()


class Job(Base):
    """A unit of background work. The worker takes them by priority; night-only ones wait for the night window."""
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_queue", "status", "priority", "id"),
                      Index("ix_jobs_dedupe", "dedupe_key", "status"))

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


class PulseEvent(Base):
    """What a user just did with a track, seen live in Navidrome's «now playing» («Пульс» follows it): played on
    or skipped. `chosen`: the user picked the track themselves (a library-search set) — not the pulse's doing.
    `disliked`: an explicit 👎, regardless of how long it played — distinct from an abandonment skip."""
    __tablename__ = "pulse_events"
    __table_args__ = (Index("ix_pulse_events_user_at", "user_id", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    skipped: Mapped[bool] = mapped_column(Boolean)
    seconds: Mapped[int] = mapped_column(Integer)  # how long it played
    chosen: Mapped[bool] = mapped_column(Boolean, default=False)
    disliked: Mapped[bool] = mapped_column(Boolean, default=False)
    not_now: Mapped[bool] = mapped_column(Boolean, default=False)  # «Не сейчас»: a mood mismatch, not a dislike
    at: Mapped[datetime] = _now()


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user", "user_id", "created_at"),)

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


class Popularity(Base):
    """How much the world listens to an artist or an album (ListenBrainz's listens), by MusicBrainz id: what's popular
    in the library before anyone here has played anything (recommend/popularity.py)."""
    __tablename__ = "popularity"

    mbid: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # "artist" | "release_group"
    listens: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = _now()


class ArtistId(Base):
    """An artist's MusicBrainz id found by name (recommend/artist_ids.py) for the tracks whose tags have none; by the
    artist's key (kurai/artists.py). `mbid` empty: looked up and not found (asked again after a while)."""
    __tablename__ = "artist_ids"

    artist_key: Mapped[str] = mapped_column(Text, primary_key=True)
    mbid: Mapped[str | None] = mapped_column(String(36))
    looked_up_at: Mapped[datetime] = _now()


class SpaceMean(Base):
    """Cached mean of unit vectors per (space, model), so retrieval.joint() doesn't recompute them on every pulse
    rebuild. Refreshed by the nightly job and on model change (admin.save)."""
    __tablename__ = "space_means"

    space: Mapped[str] = mapped_column(String(16), primary_key=True)  # "text" | "sound" | "clap"
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    vector = mapped_column(Vector(), nullable=False)
    computed_at: Mapped[datetime] = _now()

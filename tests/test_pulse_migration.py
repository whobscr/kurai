"""Regression tests for the Wave -> Pulse database migration.

Run against disposable PostgreSQL with pgvector installed:
    KURAI_TEST_DATABASE_URL=postgresql+psycopg://... uv run pytest -q tests/test_pulse_migration.py

A private schema is created for each test. The public schema is never dropped.
"""

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

TEST_DB = os.environ.get(
    "KURAI_MIGRATION_TEST_DATABASE_URL",
    os.environ.get(
        "KURAI_TEST_DATABASE_URL",
        "postgresql+psycopg://kurai:kurai@127.0.0.1:55432/kurai",
    ),
)
OLD_REVISION = "0017"


def _config(url: str) -> Config:
    migrations = Path(__file__).resolve().parents[1] / "src" / "kurai" / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture
def migration_schema():
    """Migrate an isolated schema, leaving the normal test schema untouched."""
    schema = f"test_pulse_{uuid.uuid4().hex[:12]}"
    base_url = make_url(TEST_DB)
    base_engine = create_engine(base_url)

    with base_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    isolated_url = base_url.update_query_dict(
        {"options": f"-csearch_path={schema},public"}
    )
    engine = create_engine(isolated_url)
    cfg = _config(isolated_url.render_as_string(hide_password=False))

    try:
        command.upgrade(cfg, OLD_REVISION)
        yield engine, cfg
    finally:
        engine.dispose()
        with base_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        base_engine.dispose()


def _seed(engine):
    """Seed a representative old installation after revision 0017."""
    with engine.begin() as conn:
        user_id = conn.execute(
            text("""
                INSERT INTO users (username, password_hash)
                VALUES ('pulse-migration-user', 'test-hash') RETURNING id
            """)
        ).scalar_one()

        conn.execute(text("""
            INSERT INTO tracks (id, title, artist) VALUES
            ('pulse-migration-track-1', 'Track one', 'Artist one'),
            ('pulse-migration-track-2', 'Track two', 'Artist two')
        """))

        event_id = conn.execute(
            text("""
                INSERT INTO wave_events
                    (user_id, track_id, skipped, seconds, chosen, disliked, not_now)
                VALUES
                    (:uid, 'pulse-migration-track-1', false, 187, true, false, false)
                RETURNING id
            """),
            {"uid": user_id},
        ).scalar_one()

        playlist_id = conn.execute(
            text("""
                INSERT INTO playlists (user_id, kind, name, description, track_ids)
                VALUES (:uid, 'wave', 'Моя волна', 'legacy description',
                        '["pulse-migration-track-1", "pulse-migration-track-2"]'::jsonb)
                RETURNING id
            """),
            {"uid": user_id},
        ).scalar_one()

        custom_playlist_id = conn.execute(
            text("""
                INSERT INTO playlists (user_id, kind, name, description, track_ids)
                VALUES (:uid, 'wave', 'My personal mix', 'custom',
                        '["pulse-migration-track-2"]'::jsonb)
                RETURNING id
            """),
            {"uid": user_id},
        ).scalar_one()

        job_id = conn.execute(
            text("""
                INSERT INTO jobs
                    (kind, payload, status, priority, night_only, user_id, dedupe_key)
                VALUES
                    ('wave', '{"user_id": 42, "retaste": true}'::jsonb,
                     'queued', 20, false, :uid, :dedupe)
                RETURNING id
            """),
            {"uid": user_id, "dedupe": f"wave:{user_id}"},
        ).scalar_one()

        # Current code has three persisted namespaces to rename.
        conn.execute(text("""
            INSERT INTO settings (key, value) VALUES
                ('wave.enabled', 'true'::jsonb),
                ('mix.wave.mood', 'true'::jsonb),
                ('mix.wave.session_hours', '7.5'::jsonb),
                ('mix.wave.size', '55'::jsonb),
                ('ui.wave_art_hours', '3'::jsonb)
        """))

    return {
        "user_id": user_id,
        "event_id": event_id,
        "playlist_id": playlist_id,
        "custom_playlist_id": custom_playlist_id,
        "job_id": job_id,
    }


def test_upgrade_preserves_existing_data_and_downgrade_roundtrips(migration_schema):
    engine, cfg = migration_schema
    ids = _seed(engine)

    command.upgrade(cfg, "head")

    tables = set(inspect(engine).get_table_names())
    assert "pulse_events" in tables
    assert "wave_events" not in tables

    indexes = {i["name"] for i in inspect(engine).get_indexes("pulse_events")}
    assert "ix_pulse_events_user_at" in indexes
    assert "ix_wave_events_user_at" not in indexes

    with engine.begin() as conn:
        event = conn.execute(
            text("""
                SELECT id, user_id, track_id, skipped, seconds, chosen, disliked, not_now
                FROM pulse_events WHERE id = :id
            """),
            {"id": ids["event_id"]},
        ).mappings().one()
        assert dict(event) == {
            "id": ids["event_id"],
            "user_id": ids["user_id"],
            "track_id": "pulse-migration-track-1",
            "skipped": False,
            "seconds": 187,
            "chosen": True,
            "disliked": False,
            "not_now": False,
        }

        rows = conn.execute(
            text("""
                SELECT id, kind, name, description, track_ids
                FROM playlists WHERE id IN (:id1, :id2)
            """),
            {"id1": ids["playlist_id"], "id2": ids["custom_playlist_id"]},
        ).mappings().all()
        playlists = {r["id"]: r for r in rows}
        assert playlists[ids["playlist_id"]]["kind"] == "pulse"
        assert playlists[ids["playlist_id"]]["name"] == "Пульс"
        assert playlists[ids["playlist_id"]]["description"] == "legacy description"
        assert playlists[ids["playlist_id"]]["track_ids"] == [
            "pulse-migration-track-1", "pulse-migration-track-2"
        ]
        assert playlists[ids["custom_playlist_id"]]["kind"] == "pulse"
        assert playlists[ids["custom_playlist_id"]]["name"] == "My personal mix"

        job = conn.execute(
            text("""
                SELECT kind, payload, status, priority, dedupe_key
                FROM jobs WHERE id = :id
            """),
            {"id": ids["job_id"]},
        ).mappings().one()
        assert job["kind"] == "pulse"
        assert job["payload"] == {"user_id": 42, "retaste": True}
        assert job["status"] == "queued"
        assert job["priority"] == 20
        assert job["dedupe_key"] == f"pulse:{ids['user_id']}"

        expected_settings = {
            "pulse.enabled": True,
            "mix.pulse.mood": True,
            "mix.pulse.session_hours": 7.5,
            "mix.pulse.size": 55,
            "ui.pulse_art_hours": 3,
        }
        actual = dict(conn.execute(text("SELECT key, value FROM settings")).all())
        for key, value in expected_settings.items():
            assert actual[key] == value
        for old_key in (
            "wave.enabled",
            "mix.wave.mood",
            "mix.wave.session_hours",
            "mix.wave.size",
            "ui.wave_art_hours",
        ):
            assert old_key not in actual

        # The renamed table must remain writable and retain its FK constraints.
        foreign_keys = inspect(conn).get_foreign_keys("pulse_events")
        assert len(foreign_keys) >= 2
        conn.execute(text("""
            INSERT INTO pulse_events
                (user_id, track_id, skipped, seconds, chosen, disliked, not_now)
            VALUES (:uid, 'pulse-migration-track-2', true, 12, false, true, false)
        """), {"uid": ids["user_id"]})

    command.downgrade(cfg, OLD_REVISION)

    tables = set(inspect(engine).get_table_names())
    assert "wave_events" in tables
    assert "pulse_events" not in tables
    indexes = {i["name"] for i in inspect(engine).get_indexes("wave_events")}
    assert "ix_wave_events_user_at" in indexes

    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM wave_events WHERE id = :id"),
            {"id": ids["event_id"]},
        ).scalar_one() == 1
        assert conn.execute(
            text("SELECT kind FROM playlists WHERE id = :id"),
            {"id": ids["playlist_id"]},
        ).scalar_one() == "wave"
        assert conn.execute(
            text("SELECT name FROM playlists WHERE id = :id"),
            {"id": ids["playlist_id"]},
        ).scalar_one() == "Моя волна"
        assert conn.execute(
            text("SELECT kind FROM jobs WHERE id = :id"),
            {"id": ids["job_id"]},
        ).scalar_one() == "wave"
        assert conn.execute(
            text("SELECT dedupe_key FROM jobs WHERE id = :id"),
            {"id": ids["job_id"]},
        ).scalar_one() == f"wave:{ids['user_id']}"

        old_keys = set(conn.execute(
            text("SELECT key FROM settings")
        ).scalars().all())
        assert {
            "wave.enabled",
            "mix.wave.mood",
            "mix.wave.session_hours",
            "mix.wave.size",
            "ui.wave_art_hours",
        }.issubset(old_keys)

    # A rollback followed by another forward migration must work too.
    command.upgrade(cfg, "head")
    tables = set(inspect(engine).get_table_names())
    assert "pulse_events" in tables
    assert "wave_events" not in tables


def test_new_settings_values_win_when_both_namespaces_exist(migration_schema):
    """Conflict policy: keep the already-existing pulse.* value; delete wave.*."""
    engine, cfg = migration_schema
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO settings (key, value) VALUES
                ('wave.enabled', 'true'::jsonb),
                ('pulse.enabled', 'false'::jsonb),
                ('mix.wave.size', '40'::jsonb),
                ('mix.pulse.size', '60'::jsonb)
        """))

    command.upgrade(cfg, "head")

    with engine.begin() as conn:
        values = dict(conn.execute(text("SELECT key, value FROM settings")).all())

    assert "wave.enabled" not in values
    assert values["pulse.enabled"] is False
    assert "mix.wave.size" not in values
    assert values["mix.pulse.size"] == 60

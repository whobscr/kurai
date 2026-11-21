
"""Rename My Wave persistence identifiers to Pulse.

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve existing event history and rename its table.
    op.rename_table("wave_events", "pulse_events")
    op.execute(
        "ALTER INDEX ix_wave_events_user_at "
        "RENAME TO ix_pulse_events_user_at"
    )

    # Rename the persisted playlist type and its default display name.
    op.execute(
        """
        UPDATE playlists
        SET name = 'Пульс'
        WHERE kind = 'wave' AND name = 'Моя волна'
        """
    )
    op.execute(
        "UPDATE playlists SET kind = 'pulse' WHERE kind = 'wave'"
    )

    # Keep queued jobs executable by the renamed worker handler.
    op.execute(
        "UPDATE jobs SET kind = 'pulse' WHERE kind = 'wave'"
    )
    op.execute(
        """
        UPDATE jobs
        SET dedupe_key = 'pulse:' || substring(dedupe_key FROM 6)
        WHERE dedupe_key LIKE 'wave:%'
        """
    )

    # Rename settings namespace. If both keys exist, prefer the
    # already existing pulse.* value and remove the obsolete wave.* key.
    op.execute(
        """
        DELETE FROM settings old
        WHERE old.key LIKE 'wave.%'
          AND EXISTS (
              SELECT 1
              FROM settings current
              WHERE current.key = 'pulse.' || substring(old.key FROM 6)
          )
        """
    )
    op.execute(
        """
        UPDATE settings
        SET key = 'pulse.' || substring(key FROM 6)
        WHERE key LIKE 'wave.%'
        """
    )


def downgrade() -> None:
    # Restore the old settings namespace.
    op.execute(
        """
        UPDATE settings
        SET key = 'wave.' || substring(key FROM 7)
        WHERE key LIKE 'pulse.%'
        """
    )

    # Restore job identifiers.
    op.execute(
        """
        UPDATE jobs
        SET dedupe_key = 'wave:' || substring(dedupe_key FROM 7)
        WHERE dedupe_key LIKE 'pulse:%'
        """
    )
    op.execute(
        "UPDATE jobs SET kind = 'wave' WHERE kind = 'pulse'"
    )

    # Restore playlist identifiers.
    op.execute(
        "UPDATE playlists SET kind = 'wave' WHERE kind = 'pulse'"
    )
    op.execute(
        """
        UPDATE playlists
        SET name = 'Моя волна'
        WHERE kind = 'wave' AND name = 'Пульс'
        """
    )

    # Restore the event table and index names.
    op.execute(
        "ALTER INDEX ix_pulse_events_user_at "
        "RENAME TO ix_wave_events_user_at"
    )
    op.rename_table("pulse_events", "wave_events")

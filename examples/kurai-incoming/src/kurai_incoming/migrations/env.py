"""Alembic's environment for the plugin's tables, run by `kurai web` / `kurai worker` after Kurai's own migrations.
Its own version table keeps its history apart from Kurai's (and from other plugins')."""

from alembic import context
from kurai_incoming import models  # noqa: F401  (the plugin's tables in the metadata)
from sqlalchemy import engine_from_config, pool

from kurai.db.models import Base

TABLES = {"incoming_albums"}
config = context.config


def include(obj, name, type_, reflected, compare_to) -> bool:
    """Only the plugin's tables, for `alembic revision --autogenerate`."""
    return type_ != "table" or name in TABLES


def run() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.",
                                     poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True,
                          version_table="alembic_version_incoming", include_object=include)
        with context.begin_transaction():
            context.run_migrations()


run()

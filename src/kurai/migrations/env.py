"""Alembic's environment: the models' metadata, the URL from the caller (kurai.cli.migrate). The `include_object`
filter keeps Kurai's autogenerate from touching tables that belong to an installed plugin (each plugin has its own
Alembic history in its own version table, mirrored in its own env.py)."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from kurai import plugins
from kurai.db.models import Base

config = context.config


def _include_object(target, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        # a plugin's tables are named after it (the convention documented in docs/plugins.md); the plugin's own
        # env.py is the source of truth for its tables — this is a best-effort heuristic for autogenerate only
        for p in plugins.installed():
            if p.migrations is not None and name.startswith(p.name + "_"):
                return False
    return True


def run() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.",
                                     poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True,
                          include_object=_include_object)
        with context.begin_transaction():
            context.run_migrations()


run()
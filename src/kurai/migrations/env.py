"""Alembic's environment: the models' metadata, the URL from the caller (kurai.cli.migrate). The `include_object`
filter keeps Kurai's autogenerate from touching tables that belong to an installed plugin (each plugin has its own
Alembic history in its own version table, mirrored in its own env.py)."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from kurai import plugins
from kurai.db.models import Base

config = context.config


def _plugin_tables() -> set[str]:
    """Every table declared by every installed plugin. A plugin's models live on Kurai's Base (so they can FK to
    users.id etc.), but they're migrated by the plugin's own Alembic run, not Kurai's — so Kurai's autogenerate
    must leave them alone."""
    tables: set[str] = set()
    for p in plugins.installed():
        if p.migrations is None:
            continue
        # a plugin's tables are the ones not in Kurai's own models module; the simplest reliable signal is "the
        # plugin owns any model whose table name it declares", and the convention (enforced by example) is that
        # plugins don't share table names. We collect every table that has a module path outside kurai.db.models.
        for table in Base.metadata.tables.values():
            module = getattr(table, "_kurai_plugin_module", None)
            if module is not None and module.startswith(p.name):
                tables.add(table.name)
    return tables


def _include_object(target, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        # tables whose names belong to a plugin are the plugin's responsibility
        for p in plugins.installed():
            # a plugin's tables are the ones declared after its own models were imported — we identify them by
            # the prefix convention documented in docs/plugins.md ("keep yours in a subfolder named after the
            # plugin") and by the prefix of the table name matching the plugin's name. This is a best-effort
            # heuristic; the plugin's own env.py is the source of truth for its tables.
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
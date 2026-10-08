"""Alembic env using portal_auth._engine() for DB connection.

Why portal_auth: portal_auth.py line 203 reads DB_URL from env
(mysql+pymysql://portal:<pw>@127.0.0.1:3306/radius). Reusing portal_auth._engine()
guarantees migrations use the same DB credentials the portal itself uses.
No drift risk between migration and runtime.

OFFLINE MODE (--sql): generates SQL without connecting. Use for verification
before prod apply. See migrations/versions/001_data_limit_bytes.py docstring
for the full rollback procedure.
"""
from logging.config import fileConfig
import os
import sys

from sqlalchemy import pool
from alembic import context

# Resolve paths so portal_auth is importable regardless of cwd.
# alembic.ini prepend_sys_path = . adds the working tree root, so we can
# import host.vpn-portal.portal_auth directly.
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_ROOT = os.path.dirname(THIS_DIR)
HOST_DIR = os.path.join(WORKSPACE_ROOT, 'host')
for p in (WORKSPACE_ROOT, HOST_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# portal_auth exposes no SQLAlchemy declarative metadata (the portal uses raw
# text() queries, not the ORM). So target_metadata stays None — migrations
# use op.* primitives directly.
target_metadata = None


def _get_engine_url_and_engine():
    """Lazily import portal_auth + return (url, engine).

    Lazy import keeps env.py lightweight — fastapi/argon2/etc. only load
    when an actual migration runs, not when alembic just inspects --help.

    Use importlib.import_module because the directory is named 'vpn-portal'
    (with a dash), which is not a valid Python identifier — direct
    `from vpn-portal.portal_auth import _engine` is a SyntaxError.
    """
    import importlib
    portal_auth_module = importlib.import_module('vpn-portal.portal_auth')
    eng = portal_auth_module._engine()
    return str(eng.url), eng


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Generates SQL via --sql flag without DB connection. Used for pre-deploy
    verification (reviewing the SQL that WOULD run before running it for real).

    Calls to context.execute() emit the SQL string to script output.
    """
    url, _ = _get_engine_url_and_engine()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Connects to MariaDB via portal_auth._engine() and applies migrations.
    PROD-DEPLOY: only run after the pre-deploy backup is verified (see
    migrations/versions/001_data_limit_bytes.py docstring).
    """
    _, engine = _get_engine_url_and_engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
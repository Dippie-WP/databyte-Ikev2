"""Alembic env.py for vpn-portal on prod (/opt/vpn-portal/).

The portal dir has a dash ('vpn-portal') which is not a valid Python
identifier for direct import. We add /opt/vpn-portal/ to sys.path and
import portal_auth directly.

Set VPN_PORTAL_DIR env var to override the default location.
"""
import sys
import os
from logging.config import fileConfig
from sqlalchemy import pool
from alembic import context

portal_dir = os.environ.get("VPN_PORTAL_DIR", "/opt/vpn-portal")
if portal_dir not in sys.path:
    sys.path.insert(0, portal_dir)

import portal_auth  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline():
    url = str(portal_auth._engine().url)
    context.configure(
        url=url, target_metadata=target_metadata,
        literal_binds=True, dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = portal_auth._engine()
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

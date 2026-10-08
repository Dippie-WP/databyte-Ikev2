"""add data_limit_bytes BIGINT NOT NULL DEFAULT 0 to customers + tiers

Revision ID: 001_data_limit_bytes
Revises:
Create Date: 2026-10-08 06:45:00.000000

Why:
  /opt/vpn-portal/app.py:1183 (and 20+ other call sites: 354, 412, 1190, 1204,
  1626, 1656, 1721, 1731, 1754, 1763, 2286, 2292, 2425, 2879, 2923, 3113,
  3128) SELECTs `c.data_limit_bytes` and `t.data_limit_bytes`. Neither column
  exists in MariaDB `radius.customers` or `radius.tiers` — every request to
  /api/customers and /api/customers/active-sessions fires:
    sqlalchemy.exc.OperationalError (1054, "Unknown column 'c.data_limit_bytes'
    in 'SELECT'")
  and returns HTTP 500. /var/log/vpn-portal/portal.log shows the 1054 firing
  multiple times per day since 2026-10-08 (verified live this session).

What this migration does:
  Adds data_limit_bytes BIGINT NOT NULL DEFAULT 0 to:
    - radius.customers (per-customer cap; 0 = unlimited for now)
    - radius.tiers    (tier base cap; 0 = unlimited for now)
  DEFAULT 0 is safe because no production data exists in these columns yet
  (they were just missing from the schema). After this migration, app.py
  reads return 0 until tier-specific caps are wired (separate TKT).

ROLLBACK PROCEDURE (verify locally before commit):
  1. alembic downgrade -1            # revert this single migration
  2. alembic downgrade base          # revert ALL migrations (only this one exists now)
  3. git revert <commit-sha>         # revert the commit; if already deployed,
                                     #   follow up with alembic downgrade -1
  4. mysqldump restore from backup    # nuclear option, per HOT-208

PRE-DEPLOY BACKUP (mandatory before running `alembic upgrade head` on prod):
  Per HOT-208 — verify backup covers the critical paths before destructive
  schema change:
    ssh root@154.65.110.44 '
      TS=$(date -u +%Y%m%dT%H%M%SZ)
      mysqldump -uroot radius \
        > /var/backups/pre-001-data-limit-bytes-${TS}.sql
      sha256sum /var/backups/pre-001-data-limit-bytes-${TS}.sql
        | tee /var/backups/pre-001-data-limit-bytes-${TS}.sha256
      rclone copy /var/backups/pre-001-data-limit-bytes-${TS}.sql \
        rustfs:open-claw-push/vpn-portal-migration-backups/
    '
  Then `alembic upgrade head` ONLY after the rustfs copy succeeds.

POST-DEPLOY VERIFY:
  curl -u admin:<pw> https://vpn-portal.databyte.co.za/api/customers
  should NOT 500 anymore (returns 200 with customer list).

DOWNGRADE TEST (offline, no DB needed):
  alembic upgrade --sql head    # generates forward SQL
  alembic downgrade --sql -1    # generates reverse SQL
  Verify both look right + reverse each other.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '001_data_limit_bytes'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add data_limit_bytes BIGINT NOT NULL DEFAULT 0 to customers + tiers.

    DEFAULT 0 = "no cap / unlimited" until tier-specific caps are wired
    (separate TKT).
    """
    op.add_column(
        'customers',
        sa.Column(
            'data_limit_bytes',
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text('0'),
        ),
    )
    op.add_column(
        'tiers',
        sa.Column(
            'data_limit_bytes',
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text('0'),
        ),
    )


def downgrade() -> None:
    """Drop data_limit_bytes from customers + tiers. Reverses upgrade().

    Safe because columns were empty (DEFAULT 0) at migration time. If tier
    caps were wired before this downgrade, cap values are LOST — that's the
    documented trade-off of rolling back schema for an unreleased feature.
    """
    op.drop_column('tiers', 'data_limit_bytes')
    op.drop_column('customers', 'data_limit_bytes')
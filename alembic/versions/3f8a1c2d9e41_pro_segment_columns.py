"""pro segment columns

Revision ID: 3f8a1c2d9e41
Revises: 174c4acce215
Create Date: 2026-09-12 10:00:00.000000

Schema only. Existing predictions are filled by `python -m app.ingestion.cli
backfill-segments`, which has to run after `reference` has loaded Valve tiers - at migration
time `leagues.valve_tier` is still empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f8a1c2d9e41"
down_revision: str | None = "174c4acce215"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leagues", sa.Column("valve_tier", sa.String(length=16), nullable=True))
    op.add_column("predictions", sa.Column("league_id", sa.BigInteger(), nullable=True))
    op.add_column("predictions", sa.Column("valve_tier", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("predictions", "valve_tier")
    op.drop_column("predictions", "league_id")
    op.drop_column("leagues", "valve_tier")

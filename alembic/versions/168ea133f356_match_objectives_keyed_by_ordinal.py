"""match objectives keyed by (match_id, ordinal)

The event log is rebuilt from raw payloads on every re-parse. With a surrogate id and no
natural key each rebuild appended the whole log again, so the key becomes the position in
the source array. Existing rows are cleared: the layer is derived from raw_matches.

Revision ID: 168ea133f356
Revises: d1a7f3c90b42
Create Date: 2026-08-27 04:29:00.939902
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '168ea133f356'
down_revision: str | None = 'd1a7f3c90b42'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Autogenerate saw the columns but not the primary key move, and dropping `id` while it
    # still is the key would leave the table without one. Written by hand.
    op.execute("delete from match_objectives")
    op.drop_constraint("pk_match_objectives", "match_objectives", type_="primary")
    op.drop_column("match_objectives", "id")
    op.add_column("match_objectives", sa.Column("ordinal", sa.Integer(), nullable=False))
    op.add_column("match_objectives", sa.Column("player_slot", sa.Integer(), nullable=True))
    op.create_primary_key("pk_match_objectives", "match_objectives", ["match_id", "ordinal"])


def downgrade() -> None:
    op.execute("delete from match_objectives")
    op.drop_constraint("pk_match_objectives", "match_objectives", type_="primary")
    op.drop_column("match_objectives", "player_slot")
    op.drop_column("match_objectives", "ordinal")
    op.add_column(
        "match_objectives",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    )
    op.create_primary_key("pk_match_objectives", "match_objectives", ["id"])

"""series: surrogate key, identity is (league_id, valve_series_id)

Valve's series id is not globally unique. Measured on 5300 real pro matches: keyed by that
id alone, 19 of 2555 series spanned more than 12 hours and one covered two different leagues
- unrelated series had been fused into one, which corrupts game_in_series and the series
scores that feed the model.

The normalized layer is fully derivable from `raw_matches`, so this migration rebuilds the
table rather than performing an intricate in-place key change. Run afterwards:

    docker compose exec api python -m app.ingestion.cli normalize

Revision ID: d1a7f3c90b42
Revises: c25b0299e248
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1a7f3c90b42"
down_revision: str | None = "c25b0299e248"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("fk_matches_series_id_series", "matches", type_="foreignkey")
    op.execute("update matches set series_id = null, game_in_series = null")
    op.drop_table("series")

    op.create_table(
        "series",
        sa.Column("series_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("valve_series_id", sa.BigInteger(), nullable=True),
        sa.Column("league_id", sa.BigInteger(), nullable=True),
        sa.Column("stage_id", sa.BigInteger(), nullable=True),
        sa.Column("team_a_id", sa.BigInteger(), nullable=True),
        sa.Column("team_b_id", sa.BigInteger(), nullable=True),
        sa.Column("format", sa.String(length=8), nullable=True),
        sa.Column("score_a", sa.Integer(), nullable=False),
        sa.Column("score_b", sa.Integer(), nullable=False),
        sa.Column("winner_team_id", sa.BigInteger(), nullable=True),
        sa.Column("is_draw", sa.Boolean(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valve_series_type", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "NOT (is_draw AND winner_team_id IS NOT NULL)",
            name=op.f("ck_series_draw_has_no_winner"),
        ),
        sa.ForeignKeyConstraint(
            ["league_id"],
            ["leagues.league_id"],
            name=op.f("fk_series_league_id_leagues"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["stage_id"],
            ["tournament_stages.stage_id"],
            name=op.f("fk_series_stage_id_tournament_stages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_a_id"],
            ["teams.team_id"],
            name=op.f("fk_series_team_a_id_teams"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_b_id"],
            ["teams.team_id"],
            name=op.f("fk_series_team_b_id_teams"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("series_id", name=op.f("pk_series")),
        # The identity the surrogate key stands in for.
        sa.UniqueConstraint("league_id", "valve_series_id", name="uq_series_league_valve_id"),
    )
    op.create_index(op.f("ix_series_league_id"), "series", ["league_id"], unique=False)
    op.create_index(op.f("ix_series_stage_id"), "series", ["stage_id"], unique=False)

    op.create_foreign_key(
        "fk_matches_series_id_series",
        "matches",
        "series",
        ["series_id"],
        ["series_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Back to the (broken) natural key. Series rows are dropped: the surrogate ids cannot
    be mapped back, and the layer is rebuilt from raw anyway."""
    op.drop_constraint("fk_matches_series_id_series", "matches", type_="foreignkey")
    op.execute("update matches set series_id = null, game_in_series = null")
    op.drop_index(op.f("ix_series_stage_id"), table_name="series")
    op.drop_index(op.f("ix_series_league_id"), table_name="series")
    op.drop_table("series")

    op.create_table(
        "series",
        sa.Column("series_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("league_id", sa.BigInteger(), nullable=True),
        sa.Column("stage_id", sa.BigInteger(), nullable=True),
        sa.Column("team_a_id", sa.BigInteger(), nullable=True),
        sa.Column("team_b_id", sa.BigInteger(), nullable=True),
        sa.Column("format", sa.String(length=8), nullable=True),
        sa.Column("score_a", sa.Integer(), nullable=False),
        sa.Column("score_b", sa.Integer(), nullable=False),
        sa.Column("winner_team_id", sa.BigInteger(), nullable=True),
        sa.Column("is_draw", sa.Boolean(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valve_series_type", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "NOT (is_draw AND winner_team_id IS NOT NULL)",
            name=op.f("ck_series_draw_has_no_winner"),
        ),
        sa.ForeignKeyConstraint(
            ["league_id"],
            ["leagues.league_id"],
            name=op.f("fk_series_league_id_leagues"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["stage_id"],
            ["tournament_stages.stage_id"],
            name=op.f("fk_series_stage_id_tournament_stages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_a_id"],
            ["teams.team_id"],
            name=op.f("fk_series_team_a_id_teams"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_b_id"],
            ["teams.team_id"],
            name=op.f("fk_series_team_b_id_teams"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("series_id", name=op.f("pk_series")),
    )
    op.create_index(op.f("ix_series_league_id"), "series", ["league_id"], unique=False)
    op.create_index(op.f("ix_series_stage_id"), "series", ["stage_id"], unique=False)

    op.create_foreign_key(
        "fk_matches_series_id_series",
        "matches",
        "series",
        ["series_id"],
        ["series_id"],
        ondelete="SET NULL",
    )

"""Permission codes (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

import argparse

from app.auth.permissions import (
    PIPELINE_COMMANDS,
    RUN_CHAIN,
    RUNS_VIEW,
    known_permissions,
    run_permission,
)
from app.ingestion.cli import build_parser

#: A debugging view of one Liquipedia page, not a data command (spec section 2).
NOT_PIPELINE_COMMANDS = {"liquipedia"}


def _cli_commands() -> set[str]:
    parser = build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(subparsers.choices)


def test_every_data_command_of_the_cli_has_a_permission() -> None:
    """A command added to the CLI and not here would have a button nobody can see."""
    assert set(PIPELINE_COMMANDS) == _cli_commands() - NOT_PIPELINE_COMMANDS


def test_a_run_permission_names_its_command() -> None:
    assert run_permission("backfill") == "pipeline.run.backfill"


def test_known_permissions_cover_viewing_the_chain_and_every_command() -> None:
    codes = known_permissions()
    assert codes[:2] == (RUNS_VIEW, RUN_CHAIN)
    assert set(codes[2:]) == {run_permission(c) for c in PIPELINE_COMMANDS}
    assert len(codes) == len(set(codes))

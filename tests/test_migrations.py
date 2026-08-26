"""Guards against the schema drifting away from the migrations.

These checks need no database - they catch the everyday mistake of adding a model and
forgetting the migration. CI additionally applies the migrations against a real Postgres
(upgrade head, then downgrade base), which is what proves they actually run.
"""

import re
from pathlib import Path

import pytest

from app.db import models  # noqa: F401  - populates Base.metadata
from app.db.base import Base

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"

CREATE_TABLE = re.compile(r"op\.create_table\(\s*['\"](\w+)['\"]")
REVISION = re.compile(r"^revision: str = ['\"](\w+)['\"]", re.MULTILINE)
DOWN_REVISION = re.compile(r"^down_revision: str \| None = (?:['\"](\w+)['\"]|None)", re.MULTILINE)


def migration_files() -> list[Path]:
    return sorted(p for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__"))


def test_at_least_one_migration_exists() -> None:
    """A model layer with no migration is a schema that only exists in Python."""
    assert migration_files(), "no migrations in alembic/versions"


def test_every_model_table_is_created_by_some_migration() -> None:
    created: set[str] = set()
    for path in migration_files():
        created.update(CREATE_TABLE.findall(path.read_text(encoding="utf-8")))

    missing = set(Base.metadata.tables) - created
    assert not missing, (
        f"tables exist in the models but no migration creates them: {sorted(missing)}. "
        f"Run: docker compose exec api alembic revision --autogenerate -m '<описание>'"
    )


def test_migration_history_is_linear() -> None:
    """Exactly one head. Two heads mean two branches were merged without rebasing the
    migration, and `alembic upgrade head` fails for everyone."""
    revisions: dict[str, str | None] = {}
    for path in migration_files():
        source = path.read_text(encoding="utf-8")
        revision = REVISION.search(source)
        assert revision, f"{path.name} has no revision id"
        down = DOWN_REVISION.search(source)
        revisions[revision.group(1)] = down.group(1) if down else None

    parents = {down for down in revisions.values() if down is not None}
    heads = set(revisions) - parents
    assert len(heads) == 1, f"expected a single migration head, found {sorted(heads)}"

    roots = [rev for rev, down in revisions.items() if down is None]
    assert len(roots) == 1, f"expected a single root migration, found {sorted(roots)}"


@pytest.mark.parametrize("table", ["series", "matches", "tournament_stages", "predictions"])
def test_core_tables_are_present(table: str) -> None:
    """Spot-check the tables the Bo2 rules and the prediction log depend on."""
    assert table in Base.metadata.tables

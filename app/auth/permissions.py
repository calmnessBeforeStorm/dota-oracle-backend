"""Permission codes carried in the access token.

Section 2 of the design introduces a command registry, and that registry becomes the source of
`PIPELINE_COMMANDS`. Until then the list lives here and a test holds it to the ingestion CLI, so
a command added there cannot silently lack a permission.
"""

RUNS_VIEW = "pipeline.runs.view"
RUN_CHAIN = "pipeline.run.chain"

PIPELINE_COMMANDS: tuple[str, ...] = (
    "reference",
    "backfill",
    "catch-up",
    "normalize",
    "details",
    "resolve-outcomes",
    "map-leagues",
    "refresh-meta",
    "refresh-stages",
    "link-stages",
    "prematch",
    "featurize",
    "backfill-segments",
    "status",
)


def run_permission(command: str) -> str:
    return f"pipeline.run.{command}"


def known_permissions() -> tuple[str, ...]:
    """Every code that exists today - what `create-user` and `grant-all` hand out."""
    return (RUNS_VIEW, RUN_CHAIN, *(run_permission(c) for c in PIPELINE_COMMANDS))

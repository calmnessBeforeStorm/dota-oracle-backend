"""The daily dataset refresh, and the order inside it.

`featurize` falls back to a prior of 0.5 and an empty pre-match block for any map with no
pre-match row. That fallback is correct in isolation and wrong on a schedule: run the two
steps the other way round, or run only the second, and the table fills with defaults sitting
in the same columns as measured values - which is the shape of defect this codebase has now
found three times and which no metric reports.
"""

from typing import Any

import pytest

from app.workers import training_set


@pytest.mark.asyncio
async def test_prematch_is_rebuilt_before_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_prematch(_factory: Any) -> Any:
        calls.append("prematch")
        return type("R", (), {"rows_written": 7})()

    async def fake_featurize(_factory: Any) -> Any:
        calls.append("featurize")
        return type("R", (), {"snapshots": 42, "matches_used": 3, "skipped": {}})()

    monkeypatch.setattr(training_set, "rebuild_prematch", fake_prematch)
    monkeypatch.setattr(training_set, "featurize", fake_featurize)

    written = await training_set.run_training_set_refresh(None)  # type: ignore[arg-type]

    assert calls == ["prematch", "featurize"], "featurize would read a prior of 0.5 for every map"
    assert written == 42


@pytest.mark.asyncio
async def test_refresh_never_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    """`featurize` is called without `rebuild`. A truncate belongs to a person, not a cron.

    It costs nothing to skip: `featurize` has no cursor, so an ordinary pass already rewrites
    every row from every stored payload.
    """
    seen: dict[str, Any] = {}

    async def fake_prematch(_factory: Any) -> Any:
        return type("R", (), {"rows_written": 0})()

    async def fake_featurize(_factory: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return type("R", (), {"snapshots": 0, "matches_used": 0, "skipped": {}})()

    monkeypatch.setattr(training_set, "rebuild_prematch", fake_prematch)
    monkeypatch.setattr(training_set, "featurize", fake_featurize)

    await training_set.run_training_set_refresh(None)  # type: ignore[arg-type]

    assert seen.get("rebuild") in (None, False)

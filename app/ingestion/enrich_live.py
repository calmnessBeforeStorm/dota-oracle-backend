"""Fill gaps in the normalized layer from the poller's own snapshots.

A match first learned about through `resolve-outcomes` arrives with team ids and little
else. The summary layer would fill the rest in - except that `/proMatches` never carries
these matches: it covered 13 of the 448 maps we had predicted and scored. The poller saw
all 448 and wrote down the league id and, wherever Valve named them, both teams.

**Only gaps are filled.** A `/proMatches` summary is a better source than a scoreboard
sampled mid-fight, so anything already recorded wins and this pass is free to re-run.

Two things are deliberately not taken from a snapshot. The league *name*: Valve's live
scoreboard has none, and inventing one would be worse than the honest "Лига 17599".
And teams with a negative id: Valve hands those out to line-ups assembled for a single
game, and a row for one is a name nobody can ever look up.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.matches import Match
from app.db.models.raw import RawLiveSnapshot
from app.db.models.reference import League, Team

log = get_logger(__name__)


@dataclass
class EnrichReport:
    """What the pass actually changed, so a silent no-op cannot look like work."""

    snapshots: int = 0
    teams: int = 0
    leagues: int = 0
    matches: int = 0


def _named_teams(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for side in ("radiant_team", "dire_team"):
        team = payload.get(side) or {}
        team_id, name = team.get("team_id"), team.get("team_name")
        # Positive id and a name, both required: an id without a name teaches nothing, and
        # a negative id is not a team at all.
        if isinstance(team_id, int) and team_id > 0 and name:
            rows.append({"team_id": team_id, "name": name})
    return rows


async def _latest_payloads(session: AsyncSession) -> dict[int, dict[str, Any]]:
    """The most recent snapshot of each match.

    The latest one, not the first: early in a match Valve often reports one side and fills
    the other in several minutes later.
    """
    newest = (
        select(
            RawLiveSnapshot.match_id,
            func.max(RawLiveSnapshot.captured_at).label("captured_at"),
        )
        .group_by(RawLiveSnapshot.match_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(RawLiveSnapshot.match_id, RawLiveSnapshot.payload).join(
                newest,
                (RawLiveSnapshot.match_id == newest.c.match_id)
                & (RawLiveSnapshot.captured_at == newest.c.captured_at),
            )
        )
    ).all()
    return {int(match_id): dict(payload) for match_id, payload in rows}


async def enrich_from_snapshots(session: AsyncSession) -> EnrichReport:
    """Fill missing team names and league ids from the latest snapshot of each match."""
    report = EnrichReport()
    payloads = await _latest_payloads(session)
    report.snapshots = len(payloads)
    if not payloads:
        return report

    teams: dict[int, str] = {}
    league_of: dict[int, int] = {}
    for match_id, payload in payloads.items():
        for row in _named_teams(payload):
            teams[int(row["team_id"])] = str(row["name"])
        league_id = payload.get("league_id")
        if isinstance(league_id, int) and league_id > 0:
            league_of[match_id] = league_id

    if teams:
        known = set(
            (
                await session.scalars(
                    select(Team.team_id).where(Team.team_id.in_(teams), Team.name.is_not(None))
                )
            ).all()
        )
        fresh = [
            {"team_id": team_id, "name": name}
            for team_id, name in sorted(teams.items())
            if team_id not in known
        ]
        if fresh:
            statement = insert(Team).values(fresh)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[Team.team_id],
                    # Only where we hold no name: the summary layer owns this column.
                    set_={"name": func.coalesce(Team.name, statement.excluded.name)},
                )
            )
            report.teams = len(fresh)

    # Matches whose league is still blank; the rest already have a better answer.
    blank = set(
        (
            await session.scalars(
                select(Match.match_id).where(
                    Match.match_id.in_(league_of), Match.league_id.is_(None)
                )
            )
        ).all()
    )
    if not blank:
        return report

    wanted = {league_of[match_id] for match_id in blank}
    existing = set(
        (await session.scalars(select(League.league_id).where(League.league_id.in_(wanted)))).all()
    )
    missing = sorted(wanted - existing)
    if missing:
        # `matches.league_id` is a foreign key. The row is created bare: a live scoreboard
        # knows the id and nothing else, and `map-leagues` fills the rest later.
        await session.execute(insert(League).values([{"league_id": lid} for lid in missing]))
        report.leagues = len(missing)

    await session.execute(
        update(Match),
        [{"match_id": match_id, "league_id": league_of[match_id]} for match_id in sorted(blank)],
    )
    report.matches = len(blank)

    log.info(
        "enrich.live_snapshots",
        snapshots=report.snapshots,
        teams=report.teams,
        leagues=report.leagues,
        matches=report.matches,
    )
    return report

"""Participants and results for a tournament page (F4, spec section 8.1).

What this module does **not** build is a bracket, and the reason is worth writing down so
nobody reaches for it again: our data has no elimination structure. A series carries its
teams, its score and its stage - never a round, never "the winner of this plays the winner
of that". Dota playoffs are almost always double elimination, so upper and lower bracket
cannot be told apart from dates and results alone, and a bracket reconstructed from them
would be a guess drawn as a fact. It needs Liquipedia's bracket templates parsed into a
`round` and a position on `series`, which is its own piece of work.

What the data does support, and what is here: who played, how they did, and every series
grouped under the stage it belongs to.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.matches import Match, Series
from app.db.models.reference import Team
from app.schemas.common import SeriesResult, TeamBrief, TournamentParticipant


async def _team_names(session: AsyncSession, team_ids: Sequence[int]) -> dict[int, str | None]:
    wanted = {int(t) for t in team_ids if t}
    if not wanted:
        return {}
    rows = (
        await session.execute(select(Team.team_id, Team.name).where(Team.team_id.in_(wanted)))
    ).all()
    return {int(team_id): name for team_id, name in rows}


#: How long after its last map a series is taken to be over.
#:
#: A Bo5 runs about four hours and the gap between maps is under an hour, so nothing that
#: waits half a day is still being played. Generous on purpose: calling a live series decided
#: is a visible error, while waiting a few extra hours costs nothing.
SETTLED_AFTER = timedelta(hours=12)


def outcome_from_maps(
    score_a: int, score_b: int, last_map_at: datetime | None, now: datetime
) -> bool:
    """Whether a settled series can be called from its map score alone.

    The format is what tells you a series has *finished* - two won maps end a Bo3 and not a
    Bo5. But once nobody is playing any more, the maps that exist are all the maps there
    were, and the side that won more of them won the series. Measured: 11600 of our 13588
    series can be read this way, against 1577 that carry a recorded winner.

    A level score is deliberately not called. 1-1 is a drawn Bo2 or an abandoned Bo3, and
    only the format separates those (spec section 5.5), so it stays unknown - which is a
    third state the UI already renders.
    """
    if score_a == score_b or last_map_at is None:
        return False
    return now - last_map_at >= SETTLED_AFTER


async def series_for(session: AsyncSession, league_id: int) -> list[SeriesResult]:
    """Every series of the tournament, oldest first, with the maps that made it up.

    Ordered by when the first map was actually played rather than by `scheduled_at`: the
    schedule is null for most rows, while a played map always has a timestamp.
    """
    rows = (
        await session.execute(
            select(
                Series.series_id,
                Series.stage_id,
                Series.format,
                Series.team_a_id,
                Series.team_b_id,
                Series.score_a,
                Series.score_b,
                Series.winner_team_id,
                Series.is_draw,
                func.min(Match.start_time).label("played_at"),
                func.count(Match.match_id).label("maps"),
                # Ordered inside the aggregate: the UI links to the first map, and "first"
                # has to mean the one that was played first.
                func.array_remove(
                    func.array_agg(
                        aggregate_order_by(Match.match_id, Match.start_time.nulls_last())
                    ),
                    None,
                ).label("match_ids"),
            )
            .join(Match, Match.series_id == Series.series_id, isouter=True)
            .where(Series.league_id == league_id)
            .group_by(
                Series.series_id,
                Series.stage_id,
                Series.format,
                Series.team_a_id,
                Series.team_b_id,
                Series.score_a,
                Series.score_b,
                Series.winner_team_id,
                Series.is_draw,
            )
            .order_by(func.min(Match.start_time).nulls_last(), Series.series_id)
        )
    ).all()

    names = await _team_names(session, [t for row in rows for t in (row[3], row[4])])
    now = datetime.now(UTC)
    last_map = await _last_map_at(session, [int(row[0]) for row in rows])

    results: list[SeriesResult] = []
    for row in rows:
        score_a, score_b = int(row[5]), int(row[6])
        winner, source = row[7], ("recorded" if row[7] is not None else None)
        if (
            winner is None
            and not row[8]
            and outcome_from_maps(score_a, score_b, last_map.get(int(row[0])), now)
        ):
            winner = row[3] if score_a > score_b else row[4]
            source = "maps"

        results.append(
            SeriesResult(
                series_id=int(row[0]),
                stage_id=row[1],
                format=row[2],
                team_a=TeamBrief(team_id=row[3], name=names.get(int(row[3])) if row[3] else None),
                team_b=TeamBrief(team_id=row[4], name=names.get(int(row[4])) if row[4] else None),
                score_a=score_a,
                score_b=score_b,
                winner_team_id=winner,
                outcome_source=source,
                is_draw=bool(row[8]),
                played_at=row[9],
                maps=int(row[10]),
                match_ids=[int(m) for m in (row[11] or [])],
            )
        )
    return results


async def _last_map_at(session: AsyncSession, series_ids: list[int]) -> dict[int, datetime]:
    """When each series last had a map start. `min` is already in the main query; this is the
    other end, and only this one says whether anybody is still playing."""
    if not series_ids:
        return {}
    rows = (
        await session.execute(
            select(Match.series_id, func.max(Match.start_time))
            .where(Match.series_id.in_(series_ids))
            .group_by(Match.series_id)
        )
    ).all()
    return {int(series_id): at for series_id, at in rows if at is not None}


def participants_from(results: Sequence[SeriesResult]) -> list[TournamentParticipant]:
    """Who played and how they did, derived from the series rather than stored.

    There is no participants table: a roster list would be a second source of truth for
    something the results already state. A draw is counted as its own outcome rather than
    as half a win - Bo2 ends 1-1 and the UI must not round that away (spec section 5.5).
    """
    tally: dict[int, TournamentParticipant] = {}

    def entry(team_id: int, name: str | None) -> TournamentParticipant:
        if team_id not in tally:
            tally[team_id] = TournamentParticipant(team=TeamBrief(team_id=team_id, name=name))
        return tally[team_id]

    for result in results:
        sides = [
            (result.team_a, result.score_a, result.score_b),
            (result.team_b, result.score_b, result.score_a),
        ]
        for team, own_maps, other_maps in sides:
            if team.team_id is None:
                continue
            row = entry(team.team_id, team.name)
            row.maps_won += own_maps
            row.maps_lost += other_maps
            # A series with no winner and no draw flag has not finished; counting it either
            # way would invent a result.
            if result.is_draw:
                row.series_drawn += 1
            elif result.winner_team_id is not None:
                if result.winner_team_id == team.team_id:
                    row.series_won += 1
                else:
                    row.series_lost += 1

    return sorted(
        tally.values(),
        key=lambda p: (-p.series_won, p.series_lost, -(p.maps_won - p.maps_lost)),
    )

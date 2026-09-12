"""Feed of finished matches for the home screen (spec section 8.1).

The home screen is built around the probability of a running match, and Tier 1 matches run
a few hours a day. This feed is what the screen shows the rest of the time, which makes it
subject to the same honesty rules as the accuracy dashboard: no "the model got it right"
verdict, and no substituting a neighbouring minute under someone else's caption. Only matches
predicted in professional leagues appear (design 2026-09-11-pro-segment).
"""

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.matches import Match, Series
from app.db.models.reference import League, Team
from app.db.models.training import Prediction
from app.domain.segments import PRO_VALVE_TIERS, display_tier, display_tier_expr
from app.schemas.common import (
    PredictionPoint,
    RecentMatch,
    SeriesBrief,
    TeamBrief,
)

#: The bucket where the task is no longer noise and not yet settled. Measured over 156
#: scored matches: accuracy is 51.3% in 0-4 - a coin - and already 70.6% in 10-14. Taking a
#: point from outside the bucket and calling it "minute ten" is not allowed.
TENTH_MINUTE = 10
TENTH_MINUTE_LAST = 14


def pick_tenth_minute(curve: list[PredictionPoint]) -> PredictionPoint | None:
    """First point inside the 10-14 bucket, or None.

    None means a dash on the card. Substituting minute 3 or minute 22 under the caption
    "at minute ten" is the same kind of lie the dashboard invariants guard against, and it
    is impossible to notice from the outside.

    Returns the whole point rather than one number: the card has to name *the* model
    version that made this particular claim.
    """
    for point in curve:
        if TENTH_MINUTE <= point.minute <= TENTH_MINUTE_LAST:
            return point
    return None


def thin(curve: list[PredictionPoint], target: int = 30) -> list[PredictionPoint]:
    """Thin the curve down to roughly `target` points, keeping both ends.

    A sparkline the width of a card cannot show sixty points, and sending them over the
    wire is pointless. The ends are kept: the opening and the outcome are the only two
    places anybody reads precisely.
    """
    if len(curve) <= target:
        return curve
    step = len(curve) / target
    picked = [curve[int(i * step)] for i in range(target)]
    if picked[-1] is not curve[-1]:
        picked[-1] = curve[-1]
    return picked


async def recent_matches(
    session: AsyncSession, limit: int = 20, tiers: Sequence[str] | None = None
) -> list[RecentMatch]:
    """Finished matches we predicted in professional leagues, newest first.

    `tiers` filters on the display tier - Liquipedia's, with an unmapped `premium` league
    reading as Tier 1 - and is applied here rather than in the browser: twenty newest matches
    can hold no Tier 1 at all while older ones do.
    """
    # A skeleton match has no league; the poller wrote the one it saw on every prediction.
    league_id = func.coalesce(Match.league_id, Prediction.league_id)
    statement = (
        select(Match.match_id)
        .join(Prediction, Prediction.match_id == Match.match_id)
        .outerjoin(League, League.league_id == league_id)
        .where(Match.radiant_win.is_not(None), Prediction.valve_tier.in_(PRO_VALVE_TIERS))
        .group_by(Match.match_id, Match.start_time)
        .order_by(Match.start_time.desc().nulls_last(), Match.match_id.desc())
        .limit(limit)
    )
    if tiers:
        statement = statement.where(
            display_tier_expr(Prediction.valve_tier, League.tier).in_(list(tiers))
        )
    match_ids = list((await session.scalars(statement)).all())
    if not match_ids:
        return []

    matches = {
        row.match_id: row
        for row in (await session.scalars(select(Match).where(Match.match_id.in_(match_ids)))).all()
    }

    # One league and one Valve tier per match: all its predictions come from the same league.
    predicted_league: dict[int, tuple[int | None, str | None]] = {
        int(match_id): (league, valve)
        for match_id, league, valve in (
            await session.execute(
                select(
                    Prediction.match_id,
                    func.max(Prediction.league_id),
                    func.max(Prediction.valve_tier),
                )
                .where(Prediction.match_id.in_(match_ids))
                .group_by(Prediction.match_id)
            )
        ).all()
    }

    def league_of(match: Match) -> int | None:
        return match.league_id or predicted_league.get(match.match_id, (None, None))[0]

    curves: dict[int, list[PredictionPoint]] = {match_id: [] for match_id in match_ids}
    # Version is kept per minute, not per match. A live match can be predicted by two
    # versions in a row (a promotion mid-game), and then "the match's version" is a
    # quantity that does not exist. The card names the version of the claim it prints.
    versions: dict[tuple[int, int], str] = {}
    rows = (
        await session.execute(
            select(
                Prediction.match_id,
                Prediction.minute,
                func.max(Prediction.p_radiant),
                func.max(Prediction.predicted_at),
                func.max(Prediction.model_version),
            )
            .where(Prediction.match_id.in_(match_ids))
            .group_by(Prediction.match_id, Prediction.minute)
            .order_by(Prediction.match_id, Prediction.minute)
        )
    ).all()
    for match_id, minute, p_radiant, predicted_at, version in rows:
        curves[match_id].append(
            PredictionPoint(
                minute=int(minute), p_radiant=float(p_radiant), predicted_at=predicted_at
            )
        )
        versions[(match_id, int(minute))] = version

    team_ids = {
        team_id
        for match in matches.values()
        for team_id in (match.radiant_team_id, match.dire_team_id)
        if team_id
    }
    names: dict[int, str | None] = (
        {
            int(team_id): name
            for team_id, name in (
                await session.execute(
                    select(Team.team_id, Team.name).where(Team.team_id.in_(team_ids))
                )
            ).all()
        }
        if team_ids
        else {}
    )

    league_ids = {league for match in matches.values() if (league := league_of(match))}
    leagues: dict[int, tuple[str | None, str | None]] = (
        {
            int(league_id): (name, tier)
            for league_id, name, tier in (
                await session.execute(
                    select(League.league_id, League.name, League.tier).where(
                        League.league_id.in_(league_ids)
                    )
                )
            ).all()
        }
        if league_ids
        else {}
    )

    series_ids = {match.series_id for match in matches.values() if match.series_id}
    series_rows = (
        {
            row.series_id: row
            for row in (
                await session.scalars(select(Series).where(Series.series_id.in_(series_ids)))
            ).all()
        }
        if series_ids
        else {}
    )

    result: list[RecentMatch] = []
    for match_id in match_ids:
        match = matches[match_id]
        curve = curves[match_id]
        tenth = pick_tenth_minute(curve)
        match_league = league_of(match)
        league_name, liquipedia_tier = leagues.get(match_league or 0, (None, None))
        valve_tier = predicted_league.get(match_id, (None, None))[1]
        series = series_rows.get(match.series_id or 0)
        result.append(
            RecentMatch(
                match_id=match_id,
                league_id=match_league,
                league_name=league_name,
                tier=display_tier(valve_tier, liquipedia_tier),
                radiant=TeamBrief(
                    team_id=match.radiant_team_id, name=names.get(match.radiant_team_id or 0)
                ),
                dire=TeamBrief(team_id=match.dire_team_id, name=names.get(match.dire_team_id or 0)),
                radiant_win=bool(match.radiant_win),
                started_at=match.start_time,
                series=SeriesBrief(
                    series_id=match.series_id,
                    format=series.format if series and series.format else None,
                    score_a=series.score_a if series else 0,
                    score_b=series.score_b if series else 0,
                    winner_team_id=series.winner_team_id if series else None,
                    is_draw=bool(series.is_draw) if series else False,
                    game_in_series=match.game_in_series or 1,
                    is_conditional_game=bool(match.is_conditional_game),
                ),
                curve=thin(curve),
                p_at_ten=tenth.p_radiant if tenth else None,
                # The version behind that very claim. No minute ten, no version: naming
                # another one would put words in the model's mouth.
                model_version=versions.get((match_id, tenth.minute), "") if tenth else "",
            )
        )
    return result

"""The live loop (spec sections 2.4, 8.1, 10, phase 5).

Every 30 seconds: fetch the tournament games in progress, keep the raw payload, turn each
into a prediction, log it, and push it to whoever is watching.

Two things about this loop are worth stating plainly.

**The raw snapshot is written before anything else is attempted.** History can be
re-downloaded; a live snapshot exists only while the match is being played. If the feature
code or the model throws, the payload is already safe and the minute can be re-derived
later - which is also the only way the train/serve regression test in section 6.4 will ever
get its fixtures.

**Every prediction served is logged to `predictions`** with the model version and the exact
features behind it. Without that, a quality drop a month from now is unexplainable.
"""

from datetime import UTC, datetime
from typing import Any

import orjson
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redis import get_redis, publish_prediction
from app.db.models.enums import SeriesFormat
from app.db.models.raw import RawLiveSnapshot
from app.db.models.reference import League, TournamentStage
from app.db.models.training import Prediction
from app.db.session import get_session_factory
from app.domain.series import is_conditional_game
from app.features.adapters.steam import from_live_league_game, has_scoreboard
from app.features.game_state import SeriesContext
from app.features.live import build_live_features
from app.ingestion.clients.steam import SteamClient
from app.ingestion.repository import utcnow
from app.ingestion.sources import RawSource
from app.ml.predictor import get_predictor

log = get_logger(__name__)

#: Where the API reads the current feed from. Short-lived on purpose: a stale entry is worse
#: than an empty feed, because it looks live.
LIVE_FEED_KEY = "live:feed"
LIVE_FEED_TTL = 120


def _series_context(game: dict[str, Any], fmt: SeriesFormat | None) -> SeriesContext:
    """Series position of this map.

    The format comes from our own stage table, never from Valve's `series_type` - it cannot
    express Bo2 at all (spec section 5.5).

    An unknown format has to become *something* for the feature vector, and Bo1 is the
    least-claiming choice. But it must not then be read back as knowledge: pretending a map
    is the first of a Bo1 while the series score says 1-0 would mark it "decisive", which is
    a fabricated label rather than a neutral one. `series_format_known` below is what keeps
    the two apart.
    """
    radiant_wins = int(game.get("radiant_series_wins", 0) or 0)
    dire_wins = int(game.get("dire_series_wins", 0) or 0)
    game_in_series = radiant_wins + dire_wins + 1

    return SeriesContext(
        series_format=fmt or SeriesFormat.BO1,
        game_in_series=game_in_series,
        # Only claimable when the format is actually known.
        is_conditional_game=(
            is_conditional_game(fmt, game_in_series) if fmt is not None else False
        ),
        radiant_series_wins=radiant_wins,
        dire_series_wins=dire_wins,
        format_known=fmt is not None,
    )


async def _league_context(
    session: AsyncSession, league_ids: set[int]
) -> dict[int, tuple[str | None, str, SeriesFormat | None, bool | None]]:
    """Name, tier, current stage format and LAN flag per league, from what phase 2 marked up.

    `is_lan` is here because the feature vector needs it and only this table has it. It stays
    None for an unmapped league, and the vector reports that rather than defaulting to
    "online" - the same rule the format follows.
    """
    if not league_ids:
        return {}

    leagues = {
        int(league_id): (name, str(tier), is_lan)
        for league_id, name, tier, is_lan in (
            await session.execute(
                select(League.league_id, League.name, League.tier, League.is_lan).where(
                    League.league_id.in_(league_ids)
                )
            )
        ).all()
    }

    now = datetime.now(UTC)
    formats: dict[int, SeriesFormat] = {}
    for league_id, default_format, starts_at, ends_at in (
        await session.execute(
            select(
                TournamentStage.league_id,
                TournamentStage.default_format,
                TournamentStage.starts_at,
                TournamentStage.ends_at,
            ).where(TournamentStage.league_id.in_(league_ids))
        )
    ).all():
        # The stage running today is the one this map belongs to.
        if starts_at and ends_at and starts_at <= now <= ends_at:
            formats[int(league_id)] = SeriesFormat(str(default_format))

    return {
        league_id: (name, tier, formats.get(league_id), is_lan)
        for league_id, (name, tier, is_lan) in leagues.items()
    }


def _feed_entry(
    game: dict[str, Any],
    p_radiant: float,
    model_version: str,
    minute: int,
    league_name: str | None,
    tier: str,
    series: SeriesContext,
    series_format_known: bool,
) -> dict[str, Any]:
    scoreboard = game.get("scoreboard") or {}
    radiant = game.get("radiant_team") or {}
    dire = game.get("dire_team") or {}
    return {
        "match_id": int(game.get("match_id", 0) or 0),
        "league_id": int(game.get("league_id", 0) or 0),
        "league_name": league_name,
        "tier": tier,
        "radiant": {
            "team_id": radiant.get("team_id"),
            "name": radiant.get("team_name"),
            "logo_url": None,
        },
        "dire": {
            "team_id": dire.get("team_id"),
            "name": dire.get("team_name"),
            "logo_url": None,
        },
        "game_time": int(float(scoreboard.get("duration", 0) or 0)),
        "radiant_score": int((scoreboard.get("radiant") or {}).get("score", 0) or 0),
        "dire_score": int((scoreboard.get("dire") or {}).get("score", 0) or 0),
        "p_radiant": p_radiant,
        "model_version": model_version,
        "minute": minute,
        "series": {
            "series_id": None,
            # Null rather than a guess: the UI shows a format badge only when there is one.
            "format": series.series_format.value if series_format_known else None,
            "score_a": series.radiant_series_wins,
            "score_b": series.dire_series_wins,
            "winner_team_id": None,
            "is_draw": False,
            "game_in_series": series.game_in_series,
            "is_conditional_game": series.is_conditional_game,
        },
        # Our numbers run ahead of what the viewer sees; the UI has to say so or it reads
        # as spoiling the match (spec section 7.4).
        "stream_delay_s": int(game.get("stream_delay_s", 0) or 0),
    }


async def _register_unseen_leagues(session: AsyncSession, league_ids: set[int]) -> int:
    """Record leagues the backfill has never reached.

    A live game routinely belongs to a tournament outside our slice of history. Noting the
    id costs nothing and is what lets the Liquipedia mapping reach it later; the name and
    tier stay unknown rather than being invented from the game payload, which carries
    neither.
    """
    if not league_ids:
        return 0
    now = utcnow()
    statement = insert(League).values(
        [{"league_id": league_id, "created_at": now, "updated_at": now} for league_id in league_ids]
    )
    # Existing rows are left exactly as they are: this pass knows less than they do.
    statement = statement.on_conflict_do_nothing(index_elements=[League.league_id])
    result = await session.execute(statement)
    # CursorResult carries rowcount; the base Result type mypy infers here does not.
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def poll_live_games(ctx: dict[str, Any]) -> int:
    """One tick of the live loop. Returns the number of games predicted for."""
    session_factory = get_session_factory()
    predictor = get_predictor()

    try:
        async with SteamClient() as client:
            games = await client.live_league_games()
    except Exception as exc:
        log.warning("live_poll.fetch_failed", error=str(exc))
        return 0

    captured_at = datetime.now(UTC)
    feed: list[dict[str, Any]] = []
    not_started = 0

    async with session_factory() as session:
        league_ids = {int(g.get("league_id", 0) or 0) for g in games if g.get("league_id")}
        discovered = await _register_unseen_leagues(session, league_ids)
        contexts = await _league_context(session, league_ids)

        for game in games:
            match_id = int(game.get("match_id", 0) or 0)
            if not match_id:
                continue

            # Raw first, always: this payload cannot be fetched again once the match ends.
            session.add(
                RawLiveSnapshot(
                    match_id=match_id,
                    server_steam_id=game.get("server_steam_id"),
                    source=str(RawSource.STEAM_LIVE_LEAGUE_GAMES),
                    captured_at=captured_at,
                    payload=game,
                )
            )

            # Before the horn the entry has no scoreboard, and a state built from it says
            # every building is destroyed. Nothing downstream can tell that apart from a
            # real state, so it is refused here rather than predicted on. The raw payload is
            # already stored above - the draft is worth keeping even when the game is not
            # yet worth scoring.
            if not has_scoreboard(game):
                not_started += 1
                continue

            league_id = int(game.get("league_id", 0) or 0)
            # A live game can belong to a league the backfill has never reached, so the
            # name is genuinely unknown rather than blank - the UI falls back to the id.
            league_name, tier, fmt, is_lan = contexts.get(league_id, (None, "unknown", None, None))
            series = _series_context(game, fmt)

            try:
                state = from_live_league_game(game, series=series, is_lan=is_lan)
                features = build_live_features(state)
                p_radiant = predictor.predict_proba_radiant(features)
            except Exception as exc:
                log.warning("live_poll.game_failed", match_id=match_id, error=str(exc))
                continue

            session.add(
                Prediction(
                    match_id=match_id,
                    minute=state.minute,
                    predicted_at=captured_at,
                    model_version=predictor.version,
                    p_radiant=p_radiant,
                    features=features,
                )
            )

            entry = _feed_entry(
                game,
                p_radiant,
                predictor.version,
                state.minute,
                league_name,
                tier,
                series,
                series_format_known=fmt is not None,
            )
            feed.append(entry)
            await publish_prediction(match_id, orjson.dumps(entry).decode())

        await session.commit()

    redis = get_redis()
    await redis.set(LIVE_FEED_KEY, orjson.dumps(feed).decode(), ex=LIVE_FEED_TTL)

    log.info(
        "live_poll.tick",
        games=len(games),
        predicted=len(feed),
        drafting=not_started,
        new_leagues=discovered,
    )
    return len(feed)

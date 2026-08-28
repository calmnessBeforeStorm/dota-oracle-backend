"""The match card for a game that is still being played (F2, spec section 8.1).

A live match is not in the normalized layer yet - `match_players`, `match_drafts` and
`match_objectives` are built from a detail payload that only exists once the game is over.
The card therefore used to fall back to "the curve is all we have", which left the page
showing two anonymous sides, a score of 0:0 and nothing else.

That fallback was written before the live poller stored its payloads. It is wrong now:
`GetLiveLeagueGames` carries team names and logos, the kill score, the series score, the
full draft, and every player's hero, level, K/D/A, net worth, GPM and XPM. All of it is in
`raw_live_snapshots`, one row every thirty seconds. This module reads the most recent one
and fills in what the normalized layer cannot answer yet.

Filling gaps, never overwriting - the same rule the detail parsers follow (invariant 13).
Once the match is normalized, that layer wins: it comes from a parsed replay rather than
from a scoreboard sampled mid-fight.
"""

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.card import hero_lookup
from app.db.models.raw import RawLiveSnapshot
from app.db.models.reference import Player
from app.schemas.common import DraftEntry, MatchPlayerBrief, SeriesBrief, TeamBrief

#: Valve numbers dire's players from 128 in the normalized tables, and the card sorts on it.
#: The live scoreboard restarts at 0 for each side, so dire needs the same offset applied or
#: both sides collide on slots 0-4.
DIRE_SLOT_OFFSET = 128


async def latest_snapshot(session: AsyncSession, match_id: int) -> dict[str, Any] | None:
    """The most recent live payload for this match, or None if the poller never saw it."""
    payload = (
        await session.execute(
            select(RawLiveSnapshot.payload)
            .where(RawLiveSnapshot.match_id == match_id)
            .order_by(desc(RawLiveSnapshot.captured_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    return dict(payload) if payload else None


def teams_from(payload: dict[str, Any]) -> tuple[TeamBrief, TeamBrief]:
    radiant = payload.get("radiant_team") or {}
    dire = payload.get("dire_team") or {}
    return (
        TeamBrief(team_id=radiant.get("team_id"), name=radiant.get("team_name")),
        TeamBrief(team_id=dire.get("team_id"), name=dire.get("team_name")),
    )


def series_from(payload: dict[str, Any], series_format: str | None) -> SeriesBrief:
    """Series standing as the live entry reports it.

    The format is not taken from `series_type`: Valve cannot express Bo2 there, so it comes
    from the caller, which resolved it through Liquipedia, or stays null (spec section 5.5).
    Map number is the wins so far plus this one - the only place it can come from while the
    series is still being played.
    """
    radiant_wins = int(payload.get("radiant_series_wins", 0) or 0)
    dire_wins = int(payload.get("dire_series_wins", 0) or 0)
    return SeriesBrief(
        series_id=None,
        format=series_format,
        score_a=radiant_wins,
        score_b=dire_wins,
        winner_team_id=None,
        is_draw=False,
        game_in_series=radiant_wins + dire_wins + 1,
        is_conditional_game=False,
    )


def kill_score(payload: dict[str, Any]) -> tuple[int, int]:
    scoreboard = payload.get("scoreboard") or {}
    return (
        int((scoreboard.get("radiant") or {}).get("score", 0) or 0),
        int((scoreboard.get("dire") or {}).get("score", 0) or 0),
    )


def map_score(
    players: list[MatchPlayerBrief], live: dict[str, Any] | None, is_live: bool
) -> tuple[int | None, int | None]:
    """Kills on this map, from whichever source is actually right.

    The rosters win whenever there are any, and for a finished match they are the only right
    answer: the last live snapshot was taken mid-game, so reading the score off it reports
    whatever it happened to be thirty seconds before the ancient fell. Measured while
    building this - one match showed 52:48 from the snapshot and 51:53 from its rosters.

    While the game is running it is the other way round: the rosters, if they exist at all,
    are older than the scoreboard.
    """
    if is_live and live:
        return kill_score(live)
    if players:
        return (
            sum(p.kills or 0 for p in players if p.is_radiant),
            sum(p.kills or 0 for p in players if not p.is_radiant),
        )
    if live:
        return kill_score(live)
    return (None, None)


def stream_delay_seconds(payload: dict[str, Any]) -> int:
    """What Valve says the broadcast is behind by (spec section 7.4).

    Shown rather than assumed: our numbers run ahead of what a viewer sees, and the size of
    that gap is the difference between a useful notice and a spoiler.
    """
    return int(payload.get("stream_delay_s", 0) or 0)


async def draft_from(session: AsyncSession, payload: dict[str, Any]) -> list[DraftEntry]:
    """Picks and bans from both sides, with hero names resolved.

    The live scoreboard lists them per side in the order they happened but does not number
    them across the draft, so the true interleaving is lost. They are emitted bans-first
    within each side and ordered by side, which is honest about what is known: the sequence
    is a reconstruction, and the detail payload replaces it with the real one when the match
    is parsed.

    Names are looked up here for the same reason `draft_for` does it: without them the strip
    renders a row of hero ids, which is a number nobody can read as a hero.
    """
    scoreboard = payload.get("scoreboard") or {}
    picked: list[tuple[int, bool, bool]] = []
    for side, is_radiant in (("radiant", True), ("dire", False)):
        team = scoreboard.get(side) or {}
        for is_pick, key in ((False, "bans"), (True, "picks")):
            for item in team.get(key) or []:
                hero_id = item.get("hero_id")
                if hero_id:
                    picked.append((int(hero_id), is_pick, is_radiant))

    heroes = await hero_lookup(session, [hero_id for hero_id, _, _ in picked])
    return [
        DraftEntry(
            order=order,
            is_pick=is_pick,
            is_radiant=is_radiant,
            hero_id=hero_id,
            hero_name=heroes.get(hero_id, (None, None))[0],
            hero_image=heroes.get(hero_id, (None, None))[1],
        )
        for order, (hero_id, is_pick, is_radiant) in enumerate(picked)
    ]


async def players_from(session: AsyncSession, payload: dict[str, Any]) -> list[MatchPlayerBrief]:
    """Both rosters as the scoreboard reports them right now."""
    scoreboard = payload.get("scoreboard") or {}
    raw: list[tuple[dict[str, Any], bool]] = []
    for side, is_radiant in (("radiant", True), ("dire", False)):
        for player in (scoreboard.get(side) or {}).get("players") or []:
            raw.append((player, is_radiant))
    if not raw:
        return []

    heroes = await hero_lookup(session, [int(p["hero_id"]) for p, _ in raw if p.get("hero_id")])
    account_ids = {int(p["account_id"]) for p, _ in raw if p.get("account_id")}
    names: dict[int, str | None] = {}
    if account_ids:
        names = {
            int(account_id): name
            for account_id, name in (
                await session.execute(
                    select(Player.account_id, Player.name).where(Player.account_id.in_(account_ids))
                )
            ).all()
        }

    out: list[MatchPlayerBrief] = []
    for player, is_radiant in raw:
        hero_id = int(player.get("hero_id") or 0) or None
        hero = heroes.get(hero_id) if hero_id else None
        account_id = player.get("account_id")
        slot = int(player.get("player_slot", 0) or 0)
        out.append(
            MatchPlayerBrief(
                player_slot=slot if is_radiant else slot + DIRE_SLOT_OFFSET,
                is_radiant=is_radiant,
                hero_id=hero_id,
                hero_name=hero[0] if hero else None,
                hero_image=hero[1] if hero else None,
                account_id=account_id,
                player_name=names.get(int(account_id)) if account_id else None,
                kills=player.get("kills"),
                # The live scoreboard calls it `death`, singular, and only here.
                deaths=player.get("death"),
                assists=player.get("assists"),
                last_hits=player.get("last_hits"),
                denies=player.get("denies"),
                net_worth=player.get("net_worth"),
                gold_per_min=player.get("gold_per_min"),
                xp_per_min=player.get("xp_per_min"),
            )
        )
    return out

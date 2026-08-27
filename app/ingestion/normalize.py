"""Raw payloads -> normalized layer (spec section 4.2, phase 1).

Reads only from `raw_matches`, never from the network. That is the point of keeping raw
payloads forever: this step can be re-run from scratch as often as the parsing rules change,
without spending a single API call.

What a `/proMatches` summary can and cannot fill:

  can  - leagues (id, name), teams (id, name), series identity and map scores,
         matches (outcome, timing, side, series membership)
  cannot - rosters, drafts, objectives (those need GET /matches/{id}, one call per map)
         - the series FORMAT, and therefore `is_conditional_game`; both come from
           Liquipedia in phase 2 and stay NULL here rather than being guessed
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import case, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.matches import Match, MatchDraft, MatchObjective, MatchPlayer, Series
from app.db.models.raw import RawMatch
from app.db.models.reference import League, Team
from app.features.buildings import BASE, BuildingKill, parse_npc_id
from app.ingestion.repository import utcnow
from app.ingestion.sources import RawSource

log = get_logger(__name__)

#: Valve uses 0 for "this map belongs to no series". Treating it as a real id would fuse
#: every standalone map in the dataset into one gigantic fake series.
NO_SERIES = 0


@dataclass
class NormalizeReport:
    raw_seen: int = 0
    leagues: int = 0
    teams: int = 0
    series: int = 0
    matches: int = 0
    match_players: int = 0
    match_drafts: int = 0
    match_objectives: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "raw_seen": self.raw_seen,
            "leagues": self.leagues,
            "teams": self.teams,
            "series": self.series,
            "matches": self.matches,
            "match_players": self.match_players,
            "match_drafts": self.match_drafts,
            "match_objectives": self.match_objectives,
            "skipped": self.skipped,
        }


def series_id_of(payload: dict[str, Any]) -> int | None:
    """Real series id, or None for a standalone map.

    Both 0 and a missing field mean "no series" - measured on real data, 265 of 5300 rows
    carry 0 and 23 carry null.
    """
    raw = payload.get("series_id")
    if raw is None:
        return None
    value = int(raw)
    return None if value == NO_SERIES else value


def team_id_of(payload: dict[str, Any], side: str) -> int | None:
    """0 means the side had no registered team (stand-in stack, unregistered roster)."""
    raw = payload.get(f"{side}_team_id")
    if raw is None:
        return None
    value = int(raw)
    return None if value == 0 else value


async def _upsert(session: AsyncSession, model: Any, rows: list[dict[str, Any]], key: Any) -> int:
    """Upsert on a single-column natural key, refreshing every non-key column supplied.

    Columns absent from `rows` are left alone, which is what lets a later pass enrich these
    tables - Liquipedia filling in league tiers, say - without this one wiping the work.
    """
    if not rows:
        return 0
    statement = insert(model).values(rows)
    updatable = sorted(set(rows[0]) - {key.name, "created_at"})
    statement = statement.on_conflict_do_update(
        index_elements=[key],
        set_={name: getattr(statement.excluded, name) for name in updatable},
    )
    await session.execute(statement)
    return len(rows)


async def normalize_pro_matches(
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = 500,
    limit: int | None = None,
) -> NormalizeReport:
    """Rebuild the normalized layer from stored `/proMatches` summaries."""
    report = NormalizeReport()
    offset = 0

    while True:
        async with session_factory() as session:
            statement = (
                select(RawMatch.payload)
                .where(RawMatch.source == str(RawSource.OPENDOTA_PRO_MATCHES))
                .order_by(RawMatch.match_id)
                .offset(offset)
                .limit(batch_size if limit is None else min(batch_size, limit - offset))
            )
            payloads = list((await session.execute(statement)).scalars().all())

        if not payloads:
            break

        await _normalize_batch(session_factory, payloads, report)
        offset += len(payloads)
        report.raw_seen = offset
        log.info("normalize.batch", seen=offset)

        if limit is not None and offset >= limit:
            break

    await _recompute_series_positions(session_factory, report)
    log.info("normalize.done", **report.as_log_fields())
    return report


async def _normalize_batch(
    session_factory: async_sessionmaker[AsyncSession],
    payloads: list[dict[str, Any]],
    report: NormalizeReport,
) -> None:
    now = utcnow()
    leagues: dict[int, dict[str, Any]] = {}
    teams: dict[int, dict[str, Any]] = {}
    series: dict[tuple[int, int], dict[str, Any]] = {}
    matches: list[dict[str, Any]] = []

    for payload in payloads:
        match_id = payload.get("match_id")
        if match_id is None:
            report.skip("no match_id")
            continue

        league_id = payload.get("leagueid") or None
        if league_id:
            leagues[league_id] = {
                "league_id": league_id,
                "name": payload.get("league_name"),
                "created_at": now,
                "updated_at": now,
            }

        radiant_id = team_id_of(payload, "radiant")
        dire_id = team_id_of(payload, "dire")
        for team_id, name in ((radiant_id, "radiant_name"), (dire_id, "dire_name")):
            if team_id:
                teams[team_id] = {
                    "team_id": team_id,
                    "name": payload.get(name),
                    "created_at": now,
                    "updated_at": now,
                }

        # Valve's series id only means something inside a league, so a map with no league
        # cannot be attached to a series at all - it is treated as standalone.
        valve_sid = series_id_of(payload)
        series_key = (league_id, valve_sid) if league_id and valve_sid is not None else None
        if series_key is not None and series_key not in series:
            series[series_key] = {
                "valve_series_id": valve_sid,
                "league_id": league_id,
                # Sides swap between maps, so the series-level teams are pinned to whichever
                # side each took in the first map we see; scores are recomputed against that.
                "team_a_id": radiant_id,
                "team_b_id": dire_id,
                # format stays NULL: it comes from Liquipedia (spec section 5.5), and the
                # Valve hint below cannot express Bo2 at all.
                "valve_series_type": payload.get("series_type"),
                "created_at": now,
                "updated_at": now,
            }

        matches.append(
            {
                "match_id": int(match_id),
                "league_id": league_id,
                "series_key": series_key,
                "radiant_team_id": radiant_id,
                "dire_team_id": dire_id,
                "radiant_win": payload.get("radiant_win"),
                "start_time": _to_utc(payload.get("start_time")),
                "duration": payload.get("duration"),
                # `version` is null for unparsed matches: no per-minute series available.
                "is_parsed": payload.get("version") is not None,
                "created_at": now,
                "updated_at": now,
            }
        )

    async with session_factory() as session:
        report.leagues += await _upsert(session, League, list(leagues.values()), League.league_id)
        report.teams += await _upsert(session, Team, list(teams.values()), Team.team_id)

        surrogate_ids = await _upsert_series(session, list(series.values()))
        report.series += len(series)

        # Resolve the (league, valve id) key each map was tagged with into the surrogate
        # series id. Anything unresolved stays a standalone map rather than guessing.
        for row in matches:
            row["series_id"] = surrogate_ids.get(row.pop("series_key"))

        report.matches += await _upsert(session, Match, matches, Match.match_id)
        await session.commit()


async def _upsert_series(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> dict[tuple[int, int], int]:
    """Upsert series on (league_id, valve_series_id) and return the surrogate ids."""
    if not rows:
        return {}

    insert_stmt = insert(Series).values(rows)
    updatable = sorted(set(rows[0]) - {"league_id", "valve_series_id", "created_at"})
    returning_stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_series_league_valve_id",
        set_={name: getattr(insert_stmt.excluded, name) for name in updatable},
    ).returning(Series.series_id, Series.league_id, Series.valve_series_id)

    result = await session.execute(returning_stmt)
    return {
        (league_id, valve_series_id): series_id
        for series_id, league_id, valve_series_id in result.all()
    }


def _to_utc(start_time: int | None) -> Any:
    from datetime import UTC, datetime

    return None if start_time is None else datetime.fromtimestamp(int(start_time), tz=UTC)


#: Map order and series scores are global properties: a later batch can insert a map that
#: belongs before one already stored. Recomputing in SQL after the load is both simpler and
#: correct, where incremental bookkeeping would drift.
_RENUMBER_GAMES = text("""
    update matches m
    set game_in_series = ranked.position
    from (
        select match_id,
               row_number() over (
                   partition by series_id order by start_time nulls last, match_id
               ) as position
        from matches
        where series_id is not null
    ) as ranked
    where m.match_id = ranked.match_id
      and m.game_in_series is distinct from ranked.position
""")

#: Wins per side of the series, counted against the teams pinned on the series row.
#: winner_team_id and is_draw stay untouched: deciding them needs the format (phase 2).
_RESCORE_SERIES = text("""
    update series s
    set score_a = tally.wins_a,
        score_b = tally.wins_b
    from (
        select se.series_id,
               count(*) filter (
                   where (m.radiant_win and m.radiant_team_id = se.team_a_id)
                      or (not m.radiant_win and m.dire_team_id = se.team_a_id)
               ) as wins_a,
               count(*) filter (
                   where (m.radiant_win and m.radiant_team_id = se.team_b_id)
                      or (not m.radiant_win and m.dire_team_id = se.team_b_id)
               ) as wins_b
        from series se
        join matches m on m.series_id = se.series_id
        where m.radiant_win is not null
        group by se.series_id
    ) as tally
    where s.series_id = tally.series_id
      and (s.score_a is distinct from tally.wins_a or s.score_b is distinct from tally.wins_b)
""")


async def _recompute_series_positions(
    session_factory: async_sessionmaker[AsyncSession], report: NormalizeReport
) -> None:
    async with session_factory() as session:
        # CursorResult carries rowcount; the base Result type mypy infers here does not.
        renumbered = (await session.execute(_RENUMBER_GAMES)).rowcount  # type: ignore[attr-defined]
        rescored = (await session.execute(_RESCORE_SERIES)).rowcount  # type: ignore[attr-defined]
        await session.commit()
    log.info("normalize.series_recomputed", renumbered=renumbered, rescored=rescored)


async def normalized_counts(session: AsyncSession) -> dict[str, int]:
    """Row counts of the normalized layer, for the status command and for tests."""
    counts: dict[str, int] = {}
    for label, model in (
        ("leagues", League),
        ("teams", Team),
        ("series", Series),
        ("matches", Match),
    ):
        result = await session.execute(select(func.count()).select_from(model))
        counts[label] = int(result.scalar_one())

    unknown_format = await session.execute(
        select(func.count()).select_from(Series).where(Series.format.is_(None))
    )
    counts["series without format (phase 2)"] = int(unknown_format.scalar_one())
    return counts


# --- match details -----------------------------------------------------------------------

#: Radiant occupies slots 0-4, dire 128-132. This is the only side marker in the payload.
DIRE_SLOT_THRESHOLD = 128


def _as_key(value: Any) -> str | None:
    """`objectives[].key` arrives as a number for some event types and a string for others."""
    return None if value is None else str(value)[:64]


def parse_match_detail(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Split one `/matches/{id}` payload into rows for the normalized tables.

    Note what is NOT read here: `series_id` and `series_type` come back null from this
    endpoint (verified against the live API), so series membership stays owned by the
    /proMatches summaries and must not be overwritten from details.
    """
    match_id = int(payload["match_id"])

    players = [
        {
            "match_id": match_id,
            "player_slot": int(player["player_slot"]),
            "account_id": player.get("account_id"),
            "hero_id": player.get("hero_id"),
            "is_radiant": int(player["player_slot"]) < DIRE_SLOT_THRESHOLD,
            "lane_role": player.get("lane_role"),
            "kills": player.get("kills"),
            "deaths": player.get("deaths"),
            "assists": player.get("assists"),
            "last_hits": player.get("last_hits"),
            "denies": player.get("denies"),
            "net_worth": player.get("net_worth"),
            "gold_per_min": player.get("gold_per_min"),
            "xp_per_min": player.get("xp_per_min"),
            "leaver_status": player.get("leaver_status"),
            "is_standin": False,  # needs roster history from Liquipedia (phase 2)
        }
        for player in payload.get("players") or []
        if player.get("player_slot") is not None
    ]

    drafts = [
        {
            "match_id": match_id,
            "order": int(entry["order"]),
            "is_pick": bool(entry["is_pick"]),
            "hero_id": int(entry["hero_id"]),
            "team": int(entry["team"]),
        }
        for entry in payload.get("picks_bans") or []
        if entry.get("order") is not None and entry.get("hero_id") is not None
    ]

    objectives = [
        {
            "match_id": match_id,
            "ordinal": ordinal,
            "time": int(event.get("time", 0)),
            "type": str(event.get("type", ""))[:64],
            "team": event.get("team"),
            "key": _as_key(event.get("key")),
            "player_slot": event.get("player_slot"),
        }
        for ordinal, event in enumerate(payload.get("objectives") or [])
        if event.get("type")
    ]

    return {"players": players, "drafts": drafts, "objectives": objectives}


#: The building an npc id names, spelled the way OpenDota spells it. `match_objectives.key`
#: already holds those names, and rendering the id back into one keeps a single vocabulary
#: in the table instead of two that mean the same thing.
_SIDE_NAMES = {True: "goodguys", False: "badguys"}
_FIRST_TOWER = {True: 16, False: 26}
_FIRST_RAX = {True: 38, False: 44}
#: Valve numbers radiant 2 and dire 3 in the objectives log, and 0 and 1 in the draft log.
_OBJECTIVE_TEAM = {True: 2, False: 3}


def _building_name(kill: BuildingKill, npc_id: int) -> str:
    side = _SIDE_NAMES[kill.is_radiant]
    if kill.kind == "ancient":
        return f"npc_dota_{side}_fort"
    if kill.lane == BASE:
        return f"npc_dota_{side}_tower4"
    if kill.kind == "tower":
        tier = (npc_id - _FIRST_TOWER[kill.is_radiant]) // 3 + 1
        return f"npc_dota_{side}_tower{tier}_{kill.lane}"
    melee = npc_id - _FIRST_RAX[kill.is_radiant] < 3
    return f"npc_dota_{side}_{'melee' if melee else 'range'}_rax_{kill.lane}"


#: `match_players.lane_role` and `.leaver_status` are integers on OpenDota's scale. STRATZ
#: names the same things with enum strings, so they are translated rather than stored as
#: text - the column has one meaning, and two vocabularies in it would be a silent trap for
#: anything that reads it. A value with no counterpart maps to NULL rather than to a
#: plausible-looking number (invariant 12).
_STRATZ_LANE_ROLE = {"SAFE_LANE": 1, "MID_LANE": 2, "OFF_LANE": 3, "JUNGLE": 4}
_STRATZ_LEAVER_STATUS = {
    "NONE": 0,
    "DISCONNECTED": 1,
    "DISCONNECTED_TOO_LONG": 2,
    "ABANDONED": 3,
    "AFK": 4,
    "NEVER_CONNECTED": 5,
    "NEVER_CONNECTED_TOO_LONG": 6,
}


def parse_stratz_match_detail(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Split one STRATZ `match(id:)` payload into rows for the normalized tables.

    Same three keys and the same column names as `parse_match_detail`, so both sources land
    in the same tables through the same upserts.

    Note what is NOT read here, exactly as on the OpenDota side, and for a sharper reason:
    `seriesId` *is* present in this payload. Series membership is owned by the /proMatches
    summaries, and writing it from a detail parse would let the weaker source overwrite the
    stronger one (invariant 11).
    """
    match_id = int(payload["id"])

    players = [
        {
            "match_id": match_id,
            "player_slot": int(player["playerSlot"]),
            "account_id": player.get("steamAccountId"),
            "hero_id": player.get("heroId"),
            "is_radiant": bool(player.get("isRadiant")),
            "lane_role": _STRATZ_LANE_ROLE.get(str(player.get("lane") or "")),
            "kills": player.get("kills"),
            "deaths": player.get("deaths"),
            "assists": player.get("assists"),
            "last_hits": player.get("numLastHits"),
            "denies": player.get("numDenies"),
            "net_worth": player.get("networth"),
            "gold_per_min": player.get("goldPerMinute"),
            "xp_per_min": player.get("experiencePerMinute"),
            "leaver_status": _STRATZ_LEAVER_STATUS.get(str(player.get("leaverStatus") or "")),
            "is_standin": False,  # needs roster history from Liquipedia (phase 2)
        }
        for player in payload.get("players") or []
        if player.get("playerSlot") is not None
    ]

    drafts: list[dict[str, Any]] = []
    for entry in payload.get("pickBans") or []:
        hero_id = entry.get("heroId") if entry.get("isPick") else entry.get("bannedHeroId")
        if entry.get("order") is None or hero_id is None:
            continue
        drafts.append(
            {
                "match_id": match_id,
                "order": int(entry["order"]),
                "is_pick": bool(entry["isPick"]),
                "hero_id": int(hero_id),
                "team": 0 if entry.get("isRadiant") else 1,
            }
        )

    objectives: list[dict[str, Any]] = []
    for event in payload.get("towerDeaths") or []:
        npc_id = int(event.get("npcId") or 0)
        kill = parse_npc_id(npc_id)
        if kill is None:  # ids 36 and 37 are not buildings we track
            continue
        objectives.append(
            {
                "match_id": match_id,
                "ordinal": len(objectives),
                "time": int(event.get("time", 0)),
                "type": "building_kill",
                "team": _OBJECTIVE_TEAM[kill.is_radiant],
                "key": _building_name(kill, npc_id),
                "player_slot": None,
            }
        )

    return {"players": players, "drafts": drafts, "objectives": objectives}


#: How to read a detail payload, by the source it came from. The tuple is
#: (parser, id field, patch field, "was it parsed" field) - the two providers disagree on
#: all four. `None` for the patch field means the source cannot supply one: STRATZ numbers
#: game versions differently, and a number from the wrong scale is worse than a NULL
#: (invariant 12).
_DETAIL_PARSERS: dict[
    str, tuple[Callable[[dict[str, Any]], dict[str, list[dict[str, Any]]]], str, str | None, str]
] = {
    str(RawSource.STRATZ_MATCH): (parse_stratz_match_detail, "id", None, "parsedDateTime"),
    str(RawSource.OPENDOTA_MATCH): (parse_match_detail, "match_id", "patch", "version"),
}

#: Which payload wins when a map has been fetched from both. STRATZ first: it is the source
#: the training set is built from, so the normalized tables describe the same match the
#: snapshots do. Declaration order in `_DETAIL_PARSERS` is the precedence.
_SOURCE_RANK = case(
    {source: rank for rank, source in enumerate(_DETAIL_PARSERS)},
    value=RawMatch.source,
)


async def normalize_match_details(
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = 200,
    limit: int | None = None,
) -> NormalizeReport:
    """Rebuild rosters, drafts and objectives from stored detail payloads, either source."""
    report = NormalizeReport()
    offset = 0

    while True:
        async with session_factory() as session:
            # One payload per map, not one per stored row. A map fetched from both
            # providers has two, and feeding both into the same insert makes Postgres
            # refuse the statement - the second row hits the same (match_id, player_slot)
            # the first one did.
            statement = (
                select(RawMatch.source, RawMatch.payload)
                .distinct(RawMatch.match_id)
                .where(RawMatch.source.in_(list(_DETAIL_PARSERS)))
                .order_by(RawMatch.match_id, _SOURCE_RANK)
                .offset(offset)
                .limit(batch_size if limit is None else min(batch_size, limit - offset))
            )
            rows = list((await session.execute(statement)).all())

        if not rows:
            break

        players: list[dict[str, Any]] = []
        drafts: list[dict[str, Any]] = []
        objectives: list[dict[str, Any]] = []
        enrichment: list[dict[str, Any]] = []

        for source, payload in rows:
            parse, id_field, patch_field, parsed_field = _DETAIL_PARSERS[source]
            if payload.get(id_field) is None:
                report.skip("no match_id")
                continue
            parsed = parse(payload)
            players.extend(parsed["players"])
            drafts.extend(parsed["drafts"])
            objectives.extend(parsed["objectives"])
            enrichment.append(
                {
                    "match_id": int(payload[id_field]),
                    # Only what the summary could not tell us. Series fields are present in
                    # the STRATZ payload and absent from the OpenDota one, and are
                    # deliberately not touched either way (invariant 11).
                    #
                    # `patch` is left NULL for STRATZ on purpose: its `gameVersionId` is a
                    # different numbering than OpenDota's `patch`, and writing one into a
                    # column that means the other is worse than not knowing (invariant 12).
                    "patch": payload.get(patch_field) if patch_field else None,
                    "is_parsed": payload.get(parsed_field) is not None,
                    "updated_at": utcnow(),
                }
            )

        touched = [int(row["match_id"]) for row in enrichment]
        async with session_factory() as session:
            report.match_players += await _replace_children(session, MatchPlayer, players, touched)
            report.match_drafts += await _replace_children(session, MatchDraft, drafts, touched)
            report.match_objectives += await _replace_children(
                session, MatchObjective, objectives, touched
            )
            await _enrich_matches(session, enrichment)
            await session.commit()

        offset += len(rows)
        report.raw_seen = offset
        log.info("normalize_details.batch", seen=offset)

        if limit is not None and offset >= limit:
            break

    log.info("normalize_details.done", **report.as_log_fields())
    return report


#: Postgres refuses a statement with more than 32767 bind parameters, and one row spends one
#: per column. A batch of detail payloads easily clears that - a few hundred maps carry
#: thousands of objectives between them - so inserts are chunked to stay under it. The margin
#: is there because the limit is per statement, not per row list.
_MAX_BIND_PARAMS = 30000


async def _replace_children(
    session: AsyncSession, model: Any, rows: list[dict[str, Any]], match_ids: list[int]
) -> int:
    """Replace a match's child rows wholesale rather than merging into what is there.

    Upserting on `(match_id, ordinal)` looks equivalent and is not: a re-parse that produces
    *fewer* rows refreshes the ones it covers and leaves the tail behind. That is exactly
    what happened when a map parsed from OpenDota (43 objectives) was re-parsed from STRATZ
    (24) - the match card showed every late event twice, once from each parse.

    Child rows belong to the parse that produced them, so the parse owns all of them.
    """
    if not match_ids:
        return 0

    await session.execute(delete(model).where(model.match_id.in_(match_ids)))
    if not rows:
        return 0

    # Postgres refuses a statement carrying more than 32767 bind parameters, and one row
    # spends one per column; a few hundred maps carry thousands of objectives between them.
    chunk_size = max(1, _MAX_BIND_PARAMS // len(rows[0]))
    for start in range(0, len(rows), chunk_size):
        await session.execute(insert(model).values(rows[start : start + chunk_size]))

    return len(rows)


async def _enrich_matches(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    """Fill in what only the detail payload knows, without disturbing the rest of the row."""
    if not rows:
        return
    await session.execute(update(Match), rows)

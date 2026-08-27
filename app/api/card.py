"""Assembling the parts of a match card (F2, spec section 8.1).

Kept out of the route so the decoding can be tested without HTTP. Two of the three parts do
real work rather than copying columns: hero ids become names, and Valve's event vocabulary
becomes something a client can label.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.matches import MatchDraft, MatchObjective, MatchPlayer
from app.db.models.reference import Hero, Player
from app.features.buildings import parse_building_key
from app.schemas.common import DraftEntry, MatchPlayerBrief, TimelineEvent

#: Valve numbers radiant 2 and dire 3 in the objectives log.
RADIANT_TEAM_NUMBER = 2

#: `CHAT_MESSAGE_*` types worth showing on a card, and what to call them. Everything absent
#: from this table is dropped: a courier death is noise next to a barracks falling, and an
#: unrecognised type is better missing than rendered as a raw Valve constant.
CHAT_EVENT_KINDS = {
    "CHAT_MESSAGE_ROSHAN_KILL": "roshan",
    "CHAT_MESSAGE_AEGIS": "aegis",
    "CHAT_MESSAGE_AEGIS_STOLEN": "aegis",
    "CHAT_MESSAGE_DENIED_AEGIS": "aegis",
    "CHAT_MESSAGE_FIRSTBLOOD": "first_blood",
    "CHAT_MESSAGE_MINIBOSS_KILL": "tormentor",
}


async def hero_lookup(
    session: AsyncSession, hero_ids: Sequence[int]
) -> dict[int, tuple[str, str | None]]:
    """hero_id -> (display name, image path). Absent ids simply do not appear."""
    wanted = {int(h) for h in hero_ids if h}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(Hero.hero_id, Hero.localized_name, Hero.image_path).where(
                Hero.hero_id.in_(wanted)
            )
        )
    ).all()
    return {int(hero_id): (name, image) for hero_id, name, image in rows}


async def players_for(session: AsyncSession, match_id: int) -> list[MatchPlayerBrief]:
    rows = (
        (
            await session.execute(
                select(MatchPlayer)
                .where(MatchPlayer.match_id == match_id)
                .order_by(MatchPlayer.player_slot)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    heroes = await hero_lookup(session, [r.hero_id for r in rows if r.hero_id])
    account_ids = {int(r.account_id) for r in rows if r.account_id}
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

    out = []
    for row in rows:
        hero = heroes.get(int(row.hero_id)) if row.hero_id else None
        out.append(
            MatchPlayerBrief(
                player_slot=row.player_slot,
                is_radiant=row.is_radiant,
                hero_id=row.hero_id,
                hero_name=hero[0] if hero else None,
                hero_image=hero[1] if hero else None,
                account_id=row.account_id,
                # Left null when unknown rather than filled with the account id, which
                # reads as a name and is not one.
                player_name=names.get(int(row.account_id)) if row.account_id else None,
                kills=row.kills,
                deaths=row.deaths,
                assists=row.assists,
                last_hits=row.last_hits,
                denies=row.denies,
                net_worth=row.net_worth,
                gold_per_min=row.gold_per_min,
                xp_per_min=row.xp_per_min,
            )
        )
    return out


async def draft_for(session: AsyncSession, match_id: int) -> list[DraftEntry]:
    rows = (
        (
            await session.execute(
                select(MatchDraft).where(MatchDraft.match_id == match_id).order_by(MatchDraft.order)
            )
        )
        .scalars()
        .all()
    )
    heroes = await hero_lookup(session, [r.hero_id for r in rows])

    return [
        DraftEntry(
            order=row.order,
            is_pick=row.is_pick,
            # The draft log numbers radiant 0 and dire 1 - a different scheme from the
            # objectives log, which is exactly why neither number leaves this module.
            is_radiant=row.team == 0,
            hero_id=row.hero_id,
            hero_name=heroes.get(int(row.hero_id), (None, None))[0],
            hero_image=heroes.get(int(row.hero_id), (None, None))[1],
        )
        for row in rows
    ]


def decode_objective(row: MatchObjective) -> TimelineEvent | None:
    """One stored objective as something the client can label, or None to drop it."""
    if row.type == "building_kill":
        kill = parse_building_key(str(row.key or ""))
        if kill is None:
            return None
        return TimelineEvent(
            time=row.time,
            minute=max(row.time, 0) // 60,
            kind=kill.kind,
            # The side that LOST the building - which is what the npc name encodes.
            is_radiant=kill.is_radiant,
            lane=kill.lane,
        )

    kind = CHAT_EVENT_KINDS.get(row.type)
    if kind is None:
        return None
    return TimelineEvent(
        time=row.time,
        minute=max(row.time, 0) // 60,
        kind=kind,
        is_radiant=None if row.team is None else row.team == RADIANT_TEAM_NUMBER,
    )


async def timeline_for(session: AsyncSession, match_id: int) -> list[TimelineEvent]:
    rows = (
        (
            await session.execute(
                select(MatchObjective)
                .where(MatchObjective.match_id == match_id)
                .order_by(MatchObjective.time, MatchObjective.ordinal)
            )
        )
        .scalars()
        .all()
    )
    return [event for event in (decode_objective(row) for row in rows) if event is not None]

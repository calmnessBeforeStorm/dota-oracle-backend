"""The match card beyond the probability curve (F2, spec section 8.1).

Rosters, draft and timeline. The interesting part is not the copying of columns but the two
places where Valve's vocabulary has to be decoded, and the one place where two different
numbering schemes for "which side" meet.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.card import decode_objective, draft_for, players_for, timeline_for
from app.db.models.matches import Match, MatchDraft, MatchObjective, MatchPlayer
from app.db.models.reference import Hero, Player

MATCH_ID = 4242


async def seed(session: AsyncSession) -> None:
    session.add_all(
        [
            Hero(
                hero_id=1,
                name="npc_dota_hero_antimage",
                localized_name="Anti-Mage",
                roles=[],
                image_path="/img/antimage.png",
            ),
            Hero(
                hero_id=2,
                name="npc_dota_hero_axe",
                localized_name="Axe",
                roles=[],
                image_path="/img/axe.png",
            ),
            Player(account_id=111, name="Miracle-"),
            Match(match_id=MATCH_ID, start_time=datetime(2026, 8, 1, tzinfo=UTC)),
        ]
    )
    await session.flush()
    session.add_all(
        [
            MatchPlayer(
                match_id=MATCH_ID,
                player_slot=0,
                account_id=111,
                hero_id=1,
                is_radiant=True,
                kills=7,
                deaths=1,
                assists=3,
                net_worth=20000,
                gold_per_min=600,
                xp_per_min=700,
            ),
            # No pro profile: /proPlayers only lists players who have one.
            MatchPlayer(
                match_id=MATCH_ID,
                player_slot=128,
                account_id=999,
                hero_id=2,
                is_radiant=False,
                kills=1,
                deaths=7,
                assists=0,
                net_worth=9000,
            ),
            MatchDraft(match_id=MATCH_ID, order=0, is_pick=False, hero_id=2, team=1),
            MatchDraft(match_id=MATCH_ID, order=1, is_pick=True, hero_id=1, team=0),
        ]
    )
    await session.commit()


class TestRosters:
    async def test_hero_ids_become_names_and_images(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed(session)

        players = await players_for(session, MATCH_ID)

        radiant = next(p for p in players if p.is_radiant)
        assert radiant.hero_name == "Anti-Mage"
        assert radiant.hero_image == "/img/antimage.png"

    async def test_a_known_player_gets_a_name(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed(session)

        players = await players_for(session, MATCH_ID)

        assert next(p for p in players if p.account_id == 111).player_name == "Miracle-"

    async def test_an_unknown_player_stays_nameless(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Filling it with the account id would read as a name and is not one."""
        await seed(session)

        players = await players_for(session, MATCH_ID)

        assert next(p for p in players if p.account_id == 999).player_name is None

    async def test_stats_are_carried_through(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed(session)

        radiant = next(p for p in await players_for(session, MATCH_ID) if p.is_radiant)

        assert (radiant.kills, radiant.deaths, radiant.assists) == (7, 1, 3)
        assert radiant.net_worth == 20000

    async def test_a_match_with_no_roster_returns_nothing(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await players_for(session, 999999) == []


class TestDraft:
    async def test_entries_come_back_in_draft_order(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed(session)

        draft = await draft_for(session, MATCH_ID)

        assert [entry.order for entry in draft] == [0, 1]

    async def test_the_draft_numbering_is_translated_to_a_side(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The draft log numbers radiant 0 and dire 1 - a different scheme from the
        objectives log, which numbers them 2 and 3. Neither number may leave the backend."""
        await seed(session)

        draft = await draft_for(session, MATCH_ID)

        assert next(e for e in draft if e.hero_id == 1).is_radiant is True
        assert next(e for e in draft if e.hero_id == 2).is_radiant is False

    async def test_picks_and_bans_are_distinguished(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed(session)

        draft = await draft_for(session, MATCH_ID)

        assert [entry.is_pick for entry in draft] == [False, True]


class TestTimelineDecoding:
    def event(self, **overrides: object) -> MatchObjective:
        defaults: dict[str, object] = {
            "match_id": MATCH_ID,
            "ordinal": 0,
            "time": 600,
            "type": "building_kill",
            "team": None,
            "key": "npc_dota_goodguys_tower1_top",
        }
        return MatchObjective(**{**defaults, **overrides})

    def test_a_building_kill_names_the_side_that_lost_it(self) -> None:
        decoded = decode_objective(self.event())

        assert decoded is not None
        assert (decoded.kind, decoded.is_radiant, decoded.lane) == ("tower", True, "top")
        assert decoded.minute == 10

    def test_barracks_and_ancient_are_distinguished_from_towers(self) -> None:
        rax = decode_objective(self.event(key="npc_dota_badguys_melee_rax_mid"))
        fort = decode_objective(self.event(key="npc_dota_badguys_fort"))

        assert rax is not None and rax.kind == "barracks"
        assert fort is not None and fort.kind == "ancient"

    def test_an_unrecognised_building_is_dropped(self) -> None:
        assert decode_objective(self.event(key="npc_dota_something_new")) is None

    def test_roshan_carries_the_side_that_took_it(self) -> None:
        """The objectives log numbers radiant 2 and dire 3 - the opposite convention from
        the draft log, and the reason both are decoded here."""
        radiant = decode_objective(self.event(type="CHAT_MESSAGE_ROSHAN_KILL", team=2, key=None))
        dire = decode_objective(self.event(type="CHAT_MESSAGE_ROSHAN_KILL", team=3, key=None))

        assert radiant is not None and radiant.is_radiant is True
        assert dire is not None and dire.is_radiant is False

    def test_every_aegis_variant_reads_as_an_aegis(self) -> None:
        for kind in (
            "CHAT_MESSAGE_AEGIS",
            "CHAT_MESSAGE_AEGIS_STOLEN",
            "CHAT_MESSAGE_DENIED_AEGIS",
        ):
            decoded = decode_objective(self.event(type=kind, team=2, key=None))
            assert decoded is not None and decoded.kind == "aegis"

    def test_noise_is_dropped_rather_than_shown_as_a_valve_constant(self) -> None:
        assert decode_objective(self.event(type="CHAT_MESSAGE_COURIER_LOST", key=None)) is None

    def test_a_pre_horn_event_lands_in_minute_zero(self) -> None:
        """First blood before the horn is real, and a negative minute is not."""
        decoded = decode_objective(
            self.event(type="CHAT_MESSAGE_FIRSTBLOOD", time=-15, team=2, key=None)
        )

        assert decoded is not None
        assert decoded.time == -15
        assert decoded.minute == 0


class TestTimelineQuery:
    async def test_events_come_back_in_time_order(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed(session)
        session.add_all(
            [
                MatchObjective(
                    match_id=MATCH_ID,
                    ordinal=0,
                    time=1200,
                    type="building_kill",
                    key="npc_dota_badguys_tower1_mid",
                ),
                MatchObjective(
                    match_id=MATCH_ID,
                    ordinal=1,
                    time=600,
                    type="building_kill",
                    key="npc_dota_goodguys_tower1_top",
                ),
                MatchObjective(
                    match_id=MATCH_ID, ordinal=2, time=900, type="CHAT_MESSAGE_COURIER_LOST"
                ),
            ]
        )
        await session.commit()

        timeline = await timeline_for(session, MATCH_ID)

        assert [event.time for event in timeline] == [600, 1200]  # courier dropped


# The HTTP layer is deliberately not tested here. The `client` fixture drives the real
# application, which talks to the real database rather than the throwaway test schema, so a
# seeded match is invisible to it. The route itself is three lines of wiring over the three
# functions above; it is verified against a real match instead.

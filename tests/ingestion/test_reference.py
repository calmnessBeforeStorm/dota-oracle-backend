"""Hero and pro-player reference data (spec section 2.2/A3)."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.reference import Hero, Player
from app.ingestion.reference import (
    parse_heroes,
    parse_pro_players,
    refresh_heroes,
    refresh_pro_players,
)

# Shaped after a real /constants/heroes response, trimmed to what is stored.
HEROES: dict[str, Any] = {
    "1": {
        "id": 1,
        "name": "npc_dota_hero_antimage",
        "localized_name": "Anti-Mage",
        "primary_attr": "agi",
        "attack_type": "Melee",
        "roles": ["Carry", "Escape"],
        "img": "/apps/dota2/images/dota_react/heroes/antimage.png?",
    },
    "2": {
        "id": 2,
        "name": "npc_dota_hero_axe",
        "localized_name": "Axe",
        "primary_attr": "str",
        "attack_type": "Melee",
        "roles": ["Initiator"],
        "img": "/apps/dota2/images/dota_react/heroes/axe.png?",
    },
}

PRO_PLAYERS: list[dict[str, Any]] = [
    {"account_id": 111, "name": "Miracle-", "personaname": "steamname", "loccountrycode": "JO"},
    {"account_id": 222, "name": None, "personaname": "SomeSteamName", "loccountrycode": None},
]


class FakeClient:
    async def heroes(self) -> dict[str, Any]:
        return HEROES

    async def pro_players(self) -> list[dict[str, Any]]:
        return PRO_PLAYERS


class TestParsingHeroes:
    def test_reads_the_id_from_the_value_not_the_key(self) -> None:
        rows = parse_heroes(HEROES)
        assert {row["hero_id"] for row in rows} == {1, 2}

    def test_keeps_both_names(self) -> None:
        antimage = next(r for r in parse_heroes(HEROES) if r["hero_id"] == 1)
        assert antimage["name"] == "npc_dota_hero_antimage"
        assert antimage["localized_name"] == "Anti-Mage"

    def test_falls_back_to_the_internal_name(self) -> None:
        """A hero with no display name is a data problem worth seeing, not one to hide."""
        rows = parse_heroes({"9": {"id": 9, "name": "npc_dota_hero_mystery"}})
        assert rows[0]["localized_name"] == "npc_dota_hero_mystery"

    def test_entries_without_an_id_are_dropped(self) -> None:
        assert parse_heroes({"x": {"name": "npc_dota_hero_x"}}) == []

    def test_image_path_is_stored_as_given(self) -> None:
        """Not expanded into a full URL - the CDN host has changed before."""
        antimage = next(r for r in parse_heroes(HEROES) if r["hero_id"] == 1)
        assert antimage["image_path"].startswith("/apps/dota2/")


class TestParsingProPlayers:
    def test_prefers_the_pro_handle_over_the_steam_name(self) -> None:
        rows = parse_pro_players(PRO_PLAYERS)
        assert next(r for r in rows if r["account_id"] == 111)["name"] == "Miracle-"

    def test_falls_back_to_the_steam_name(self) -> None:
        rows = parse_pro_players(PRO_PLAYERS)
        assert next(r for r in rows if r["account_id"] == 222)["name"] == "SomeSteamName"

    def test_duplicate_accounts_are_collapsed(self) -> None:
        """A duplicated id in one batch would otherwise make the upsert hit the same row
        twice in a single statement, which Postgres refuses outright."""
        rows = parse_pro_players([*PRO_PLAYERS, PRO_PLAYERS[0]])
        assert len(rows) == 2

    def test_entries_without_an_account_are_dropped(self) -> None:
        assert parse_pro_players([{"name": "ghost"}]) == []


class TestRefresh:
    async def test_heroes_land_in_the_table(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        written = await refresh_heroes(FakeClient(), sessionmaker)

        assert written == 2
        async with sessionmaker() as check:
            names = (await check.execute(select(Hero.localized_name))).scalars().all()
        assert sorted(names) == ["Anti-Mage", "Axe"]

    async def test_rerunning_refreshes_rather_than_duplicating(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await refresh_heroes(FakeClient(), sessionmaker)
        await refresh_heroes(FakeClient(), sessionmaker)

        async with sessionmaker() as check:
            rows = (await check.execute(select(Hero))).scalars().all()
        assert len(rows) == 2

    async def test_pro_players_land_in_the_table(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        written = await refresh_pro_players(FakeClient(), sessionmaker)

        assert written == 2
        async with sessionmaker() as check:
            row = (await check.execute(select(Player).where(Player.account_id == 111))).scalar_one()
        assert row.name == "Miracle-"
        assert row.country == "JO"

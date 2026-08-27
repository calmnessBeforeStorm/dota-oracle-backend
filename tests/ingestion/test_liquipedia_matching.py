"""Matching leagues to Liquipedia pages (spec section 3).

A wrong match mislabels every game of a tournament as Tier 1 or not, so the tests here are
mostly about what must NOT be matched: a different edition of the same series, and pages
that are not tournaments at all.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.enums import LeagueTier
from app.db.models.reference import League, LeagueMapping, TournamentStage
from app.ingestion.liquipedia.matching import (
    AUTO_ACCEPT_SCORE,
    MappingProposal,
    best_proposal,
    name_score,
    normalize_name,
    read_categories,
)
from app.ingestion.liquipedia.sync import (
    apply_mapping,
    propose_mappings,
    sync_liquipedia_leagues,
)

TIER1 = ["Category:Tournaments", "Category:Tier 1 Tournaments", "Category:Offline Tournaments"]
TIER2_ONLINE = [
    "Category:Tournaments",
    "Category:Tier 2 Tournaments",
    "Category:Online Tournaments",
]
NOT_A_TOURNAMENT = ["Category:Players", "Category:Danish Players"]


class TestNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("DreamLeague Season 29", "dreamleague 29"),
            ("DreamLeague/29", "dreamleague 29"),
            ("The International/2024", "international 2024"),
            ("Esports World Cup 2026", "world cup 2026"),
        ],
    )
    def test_reduces_to_identifying_tokens(self, raw: str, expected: str) -> None:
        """ "Season" is noise, the edition number is identity."""
        assert normalize_name(raw) == expected


class TestScoring:
    def test_slash_notation_matches_the_spelled_out_name(self) -> None:
        assert name_score("DreamLeague Season 29", "DreamLeague/29") == 1.0

    def test_different_edition_of_the_same_series_is_rejected(self) -> None:
        """The single most dangerous confusion: the names differ by one digit but the
        tournaments are months apart."""
        score = name_score("DreamLeague Season 29", "DreamLeague/28")
        assert score < AUTO_ACCEPT_SCORE
        assert score <= 0.49

    def test_unrelated_names_score_low(self) -> None:
        assert name_score("Destiny League", "The International/2024") < 0.4

    def test_empty_name_is_not_a_match(self) -> None:
        assert name_score("", "DreamLeague/29") == 0.0


class TestCategories:
    def test_reads_tier_and_venue(self) -> None:
        tier, is_lan, is_tournament = read_categories(TIER1)
        assert (tier, is_lan, is_tournament) == (LeagueTier.TIER1, True, True)

    def test_online_tournament(self) -> None:
        tier, is_lan, _ = read_categories(TIER2_ONLINE)
        assert (tier, is_lan) == (LeagueTier.TIER2, False)

    def test_player_page_is_not_a_tournament(self) -> None:
        tier, _, is_tournament = read_categories(NOT_A_TOURNAMENT)
        assert tier is LeagueTier.UNKNOWN
        assert is_tournament is False

    def test_venue_unknown_stays_none(self) -> None:
        """Neither Offline nor Online listed - guessing either would be a fabrication."""
        _, is_lan, _ = read_categories(["Category:Tournaments"])
        assert is_lan is None


class TestBestProposal:
    def test_prefers_a_tournament_over_a_better_scoring_article(self) -> None:
        """Search ranks team and player pages highly; they can never be the answer."""
        proposal = best_proposal(
            1,
            "DreamLeague Season 29",
            [
                ("DreamLeague Season 29", NOT_A_TOURNAMENT, None),
                ("DreamLeague/29", TIER1, None),
            ],
        )
        assert proposal is not None
        assert proposal.page_title == "DreamLeague/29"
        assert proposal.is_confident

    def test_low_score_is_not_confident(self) -> None:
        proposal = best_proposal(1, "Destiny League", [("The International/2024", TIER1, None)])
        assert proposal is not None
        assert not proposal.is_confident

    def test_no_candidates_yields_nothing(self) -> None:
        assert best_proposal(1, "Whatever", []) is None


class FakeLiquipedia:
    """Stands in for the wiki: search hits, categories and page source."""

    def __init__(
        self,
        hits: dict[str, list[str]],
        categories: dict[str, list[str]],
        descriptions: dict[str, str] | None = None,
    ) -> None:
        self.hits = hits
        self.categories = categories
        self.descriptions = descriptions or {}
        self.wikitext_calls: list[str] = []

    async def query(self, **params: object) -> dict[str, object]:
        if params.get("list") == "search":
            titles = self.hits.get(str(params.get("srsearch")), [])
            return {"query": {"search": [{"title": t} for t in titles]}}
        titles = str(params.get("titles", "")).split("|")
        return {
            "query": {
                "pages": {
                    str(i): {
                        "title": title,
                        "categories": [{"title": c} for c in self.categories.get(title, [])],
                        "pageprops": (
                            {"metadescl": self.descriptions[title], "displaytitle": title}
                            if title in self.descriptions
                            else {}
                        ),
                    }
                    for i, title in enumerate(titles)
                }
            }
        }

    async def page_wikitext(self, page: str) -> str:
        self.wikitext_calls.append(page)
        return (
            "== Format ==\n"
            "*'''Group Stage'''\n"
            "**All series are {{Abbr/Bo2}}\n"
            "*'''Playoffs'''\n"
            "**All matches are {{Abbr/Bo3}}\n"
            "== Prize Pool ==\n"
        )


async def seed_league(session: AsyncSession, league_id: int, name: str) -> None:
    now = datetime.now(UTC)
    session.add(League(league_id=league_id, name=name, created_at=now, updated_at=now))
    await session.commit()


class TestSync:
    async def test_proposes_without_writing(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_league(session, 1, "DreamLeague Season 29")
        client = FakeLiquipedia(
            {"DreamLeague Season 29": ["DreamLeague/29"]}, {"DreamLeague/29": TIER1}
        )

        proposals = await propose_mappings(client, sessionmaker)

        assert len(proposals) == 1
        assert proposals[0].is_confident
        league = (await session.execute(select(League))).scalar_one()
        assert league.liquipedia_slug is None  # nothing persisted

    async def test_apply_records_history_and_updates_the_league(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_league(session, 1, "DreamLeague Season 29")
        proposal = MappingProposal(
            league_id=1,
            league_name="DreamLeague Season 29",
            page_title="DreamLeague/29",
            score=1.0,
            tier=LeagueTier.TIER1,
            is_lan=True,
            is_tournament=True,
        )

        await apply_mapping(session, proposal)
        await session.commit()

        league = (await session.execute(select(League))).scalar_one()
        assert league.tier == LeagueTier.TIER1
        assert league.liquipedia_slug == "DreamLeague/29"
        mapping = (await session.execute(select(LeagueMapping))).scalar_one()
        assert mapping.superseded_at is None
        assert mapping.decided_by == "auto"

    async def test_reclassification_supersedes_rather_than_overwrites(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Spec section 3: the mapping is versioned, because why a match counted as Tier 1
        six months ago has to stay answerable."""
        await seed_league(session, 1, "Some League")
        first = MappingProposal(1, "Some League", "Some/2026", 0.9, LeagueTier.TIER1, True, True)
        second = MappingProposal(1, "Some League", "Some/2026", 0.9, LeagueTier.TIER2, True, True)

        await apply_mapping(session, first)
        await session.commit()
        await apply_mapping(session, second, decided_by="manual", note="reclassified")
        await session.commit()

        rows = (await session.execute(select(LeagueMapping))).scalars().all()
        assert len(rows) == 2
        active = [r for r in rows if r.superseded_at is None]
        assert len(active) == 1
        assert active[0].tier == LeagueTier.TIER2
        assert active[0].note == "reclassified"

        league = (await session.execute(select(League))).scalar_one()
        assert league.tier == LeagueTier.TIER2

    async def test_writes_stages_including_bo2(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_league(session, 1, "DreamLeague Season 29")
        client = FakeLiquipedia(
            {"DreamLeague Season 29": ["DreamLeague/29"]}, {"DreamLeague/29": TIER1}
        )

        report = await sync_liquipedia_leagues(client, sessionmaker, apply=True)

        assert report.applied == 1
        stages = (await session.execute(select(TournamentStage))).scalars().all()
        assert {s.default_format for s in stages} == {"bo2", "bo3"}

    async def test_uncertain_matches_are_left_for_a_human(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_league(session, 1, "Destiny League")
        client = FakeLiquipedia(
            {"Destiny League": ["The International/2024"]}, {"The International/2024": TIER1}
        )

        report = await sync_liquipedia_leagues(client, sessionmaker, apply=True)

        assert report.applied == 0
        assert len(report.needs_review) == 1
        assert client.wikitext_calls == []  # no expensive parse call spent on a bad match
        league = (await session.execute(select(League))).scalar_one()
        assert league.liquipedia_slug is None

    async def test_rerun_updates_stages_in_place(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_league(session, 1, "DreamLeague Season 29")
        client = FakeLiquipedia(
            {"DreamLeague Season 29": ["DreamLeague/29"]}, {"DreamLeague/29": TIER1}
        )

        await sync_liquipedia_leagues(client, sessionmaker, apply=True)
        # only_unmapped defaults on, so a second pass must find nothing left to do
        second = await sync_liquipedia_leagues(client, sessionmaker, apply=True)

        assert second.leagues_seen == 0
        stages = (await session.execute(select(TournamentStage))).scalars().all()
        assert len(stages) == 2


class TestConflicts:
    async def test_two_leagues_claiming_one_page_are_both_held_back(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A Liquipedia page describes exactly one tournament, so two leagues matching it
        means at least one is wrong - and applying either would be a coin flip."""
        await seed_league(session, 1, "DreamLeague Season 29")
        await seed_league(session, 2, "DreamLeague Season 29")
        client = FakeLiquipedia(
            {"DreamLeague Season 29": ["DreamLeague/29"]}, {"DreamLeague/29": TIER1}
        )

        report = await sync_liquipedia_leagues(client, sessionmaker, apply=True)

        assert report.applied == 0
        assert report.conflicts == {"DreamLeague/29": ["DreamLeague Season 29"] * 2}
        assert len(report.needs_review) == 2
        leagues = (await session.execute(select(League))).scalars().all()
        assert all(league.liquipedia_slug is None for league in leagues)

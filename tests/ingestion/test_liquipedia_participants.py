"""Roster overlap, the escalation signal (spec section 3).

Names and dates are shared by unrelated tournaments - the same organiser runs the same
series every quarter and half the calendar overlaps - but the field that actually turned up
is close to unique. It is also the most expensive thing to read, so it is spent last.

The wikitext samples are the shapes seen on the live wiki.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.liquipedia.matching import LeagueEvidence, combine_signals
from app.ingestion.liquipedia.participants import (
    extract_participants,
    normalize_team,
    roster_overlap,
)
from app.ingestion.liquipedia.sync import escalate_uncertain, propose_mappings
from tests.ingestion.test_liquipedia_matching import (
    TIER1,
    FakeLiquipedia,
    seed_league,
)

WIKITEXT = """
{{TeamCard|team=Team Spirit|qualifier=DPC}}
{{TeamCard|team=Virtus.Pro}}
{{TeamCard|team=ex-HEROIC}}
{{Match|{{Opponent|Team Falcons|score=2}}|{{Opponent|Nigma Galaxy|score=0}}}}
{{Placement|1|{{Opponent|Team Spirit}}}}
"""

#: How the same organisations are spelled on our side, from OpenDota.
OURS = {
    normalize_team(name)
    for name in ["Team Spirit", "Virtus.pro", "ex-HEROIC", "Team Falcons", "Nigma Galaxy "]
}


class TestExtraction:
    def test_reads_teams_from_every_template_shape(self) -> None:
        found = extract_participants(WIKITEXT)
        assert "team spirit" in found
        assert "virtus pro" in found
        assert "team falcons" in found

    def test_ignores_parameters_that_are_not_names(self) -> None:
        """`{{Opponent|score=2}}` matches the pattern but names no team."""
        found = extract_participants("{{Opponent|score=2}}{{Opponent|points=1000}}")
        assert found == set()

    def test_empty_page_yields_nothing(self) -> None:
        assert extract_participants("") == set()


class TestNormalization:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Virtus.Pro", "Virtus.pro"),
            ("ex-HEROIC", "Ex-Heroic"),
            ("Nigma Galaxy ", "Nigma Galaxy"),
        ],
    )
    def test_spelling_differences_collapse(self, left: str, right: str) -> None:
        """Observed on real data: the two sources disagree on case, punctuation and
        trailing whitespace for the same organisation."""
        assert normalize_team(left) == normalize_team(right)


class TestOverlap:
    def test_full_roster_match(self) -> None:
        assert roster_overlap(OURS, extract_participants(WIKITEXT)) == 1.0

    def test_no_shared_teams(self) -> None:
        assert roster_overlap({"team spirit"}, {"navi", "og"}) == 0.0

    def test_measured_against_our_side_only(self) -> None:
        """A page listing qualifier teams we never saw is more complete, not wrong."""
        assert roster_overlap({"team spirit"}, {"team spirit", "og", "navi", "liquid"}) == 1.0

    def test_empty_side_gives_no_signal(self) -> None:
        assert roster_overlap(set(), {"team spirit"}) is None


class TestScoringWithRoster:
    def test_shared_roster_carries_a_weak_name(self) -> None:
        """Two unrelated tournaments do not field the same teams, so this signal is allowed
        to decide rather than only nudge."""
        assert combine_signals(name=0.45, overlap=None, teams=None, roster=0.9) >= 0.82

    def test_absent_roster_overrules_a_perfect_name(self) -> None:
        assert combine_signals(name=1.0, overlap=1.0, teams=1.0, roster=0.0) <= 0.3

    def test_partial_overlap_only_nudges(self) -> None:
        nudged = combine_signals(name=0.5, overlap=None, teams=None, roster=0.5)
        assert 0.5 < nudged < 0.82


class TestEscalation:
    async def test_spends_the_budget_on_the_closest_calls_first(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        # Both pairs are real, from the live run: they scored 0.62 and 0.29 on names alone.
        await seed_league(session, 1, "Trinity League")
        await seed_league(session, 2, "Lunar Trophy")
        client = FakeLiquipedia(
            {
                "Trinity League": ["VK Play League/1/Legends"],
                "Lunar Trophy": ["Cringe Station/Lunar Horse Trophy/8"],
            },
            {"VK Play League/1/Legends": TIER1, "Cringe Station/Lunar Horse Trophy/8": TIER1},
        )
        proposals = await propose_mappings(client, sessionmaker)
        evidence = {
            p.league_id: LeagueEvidence(team_names=frozenset({"team spirit"})) for p in proposals
        }

        _, stats = await escalate_uncertain(client, proposals, evidence, budget=1)

        assert stats.escalated == 1
        # The closer call gets the single fetch: a lookup is worth most where the decision
        # is nearly balanced.
        assert client.wikitext_calls == ["Cringe Station/Lunar Horse Trophy/8"]

    async def test_budget_of_zero_spends_nothing(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_league(session, 1, "Lunar Trophy")
        client = FakeLiquipedia(
            {"Lunar Trophy": ["Cringe Station/Lunar Horse Trophy/8"]},
            {"Cringe Station/Lunar Horse Trophy/8": TIER1},
        )
        proposals = await propose_mappings(client, sessionmaker)

        returned, stats = await escalate_uncertain(client, proposals, {}, budget=0)

        assert stats.escalated == 0
        assert client.wikitext_calls == []
        assert returned == proposals

    async def test_confident_proposals_are_never_escalated(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The cheap signals already settled it; a page fetch would buy nothing."""
        await seed_league(session, 1, "DreamLeague Season 29")
        client = FakeLiquipedia(
            {"DreamLeague Season 29": ["DreamLeague/29"]}, {"DreamLeague/29": TIER1}
        )
        proposals = await propose_mappings(client, sessionmaker)
        evidence = {1: LeagueEvidence(team_names=frozenset({"team spirit"}))}

        _, stats = await escalate_uncertain(client, proposals, evidence, budget=5)

        assert stats.escalated == 0
        assert client.wikitext_calls == []


def test_signals_report_the_roster_when_it_was_looked_up() -> None:
    """The review report exists to show the evidence; a signal that decided the score and
    then vanished from the output is worse than useless."""
    from app.db.models.enums import LeagueTier
    from app.ingestion.liquipedia.matching import MappingProposal

    escalated = MappingProposal(
        league_id=1,
        league_name="Lunar Trophy",
        page_title="Cringe Station/Lunar Horse Trophy/7",
        score=0.84,
        tier=LeagueTier.TIER3,
        is_lan=False,
        is_tournament=True,
        name_only_score=0.62,
        date_overlap=1.0,
        team_agreement=0.88,
        roster_overlap=0.667,
    )
    assert "roster 0.67" in escalated.signals

    not_escalated = MappingProposal(
        league_id=1,
        league_name="x",
        page_title="y",
        score=0.5,
        tier=LeagueTier.UNKNOWN,
        is_lan=None,
        is_tournament=True,
    )
    assert "roster" not in not_escalated.signals


class TestRenamedTeams:
    """Teams rename constantly, and betting sponsors make it systematic.

    Valve bars them from The International, so PARIVISION enters as Vision and BetBoom Team
    as BoomBoys, while Liquipedia keeps the canonical name. Observed in our own data as
    "TEAM VISION", "PVISION", "BoomBoys" and "Team Yandex" - the last two even under two
    different team ids.
    """

    def test_renames_lower_the_overlap_without_breaking_it(self) -> None:
        ours = {
            normalize_team(n)
            for n in ["BoomBoys", "TEAM VISION", "Team Spirit", "Team Falcons", "Team Liquid"]
        }
        theirs = {
            normalize_team(n)
            for n in ["BetBoom Team", "PARIVISION", "Team Spirit", "Team Falcons", "Team Liquid"]
        }

        overlap = roster_overlap(ours, theirs)
        assert overlap is not None
        # Three of five still match, which is far above the veto and correctly identifies
        # the tournament despite two organisations wearing different names.
        assert overlap == pytest.approx(0.6)
        assert combine_signals(name=0.5, overlap=None, teams=None, roster=overlap) > 0.5

    def test_a_renamed_champion_cannot_overturn_an_agreeing_roster(self) -> None:
        """The bug this guards: the champion is one name where the roster is a dozen, so a
        single rename must not throw away a mapping the roster already confirmed."""
        with_roster = combine_signals(
            name=0.6, overlap=1.0, teams=1.0, roster=0.9, winner_agrees=False
        )
        assert with_roster > 0.5

    def test_a_mismatched_champion_still_vetoes_without_roster_support(self) -> None:
        without_roster = combine_signals(
            name=0.9, overlap=1.0, teams=1.0, roster=None, winner_agrees=False
        )
        assert without_roster <= 0.5

"""Reading tournament facts out of Liquipedia's generated page description (spec section 3).

Every string here is a real `metadescl` value taken from the live wiki. The spec asks for
matching on name plus dates plus prize pool, and this is where the dates and the prize pool
come from without paying for a rate-limited `parse` call per candidate.
"""

from datetime import date

import pytest

from app.db.models.enums import LeagueTier
from app.ingestion.liquipedia.matching import (
    LeagueEvidence,
    best_proposal,
    combine_signals,
    count_agreement,
    date_overlap,
)
from app.ingestion.liquipedia.meta import parse_dates, parse_meta_description, parse_prize_pool

# --- real descriptions, copied verbatim ---------------------------------------------------

DREAMLEAGUE_29 = (
    "DreamLeague Season 29 is an online European Dota 2 tournament organized by ESL Gaming. "
    "This Tier 1 tournament took place from May 13 to 24 2026 featuring 16 teams competing "
    "over a total prize pool of $1,000,000 USD."
)
ASGARD = (
    "Asgard Championship Season 1 is an online European Dota 2 tournament organized by ACE "
    "Esports. This Tier 3 tournament took place from Jul 28 to Aug 11 2026 featuring 12 "
    "teams competing over a total prize pool of $20000 USD."
)
BLAST_SLAM = (
    "BLAST SLAM VII is an online & offline Danish Dota 2 tournament organized by BLAST. "
    "This Tier 1 tournament took place from May 26 to Jun 07 2026 featuring 12 teams "
    "competing over a total prize pool of $1,000,000 USD."
)
BETBOOM_SHOWMATCH = (
    "BetBoom Streamers Battle 13 is an online Russian Dota 2 Showmatch organized by "
    "ESforce, GLuck, and MoviEStudio. This Tier 3 Showmatch took place from May 18 to 24 "
    "2026 featuring 8 teams competing over a total prize pool of 4,000,000₽ RUB."
)


class TestDates:
    def test_same_month_range(self) -> None:
        assert parse_dates(DREAMLEAGUE_29) == (date(2026, 5, 13), date(2026, 5, 24))

    def test_range_across_a_month_boundary(self) -> None:
        """The second date carries its own month: "from Jul 28 to Aug 11 2026"."""
        assert parse_dates(ASGARD) == (date(2026, 7, 28), date(2026, 8, 11))

    def test_range_across_new_year_backdates_the_start(self) -> None:
        """A range that reads backwards started the previous year."""
        start, end = parse_dates("took place from Dec 28 to Jan 05 2026")
        assert start == date(2025, 12, 28)
        assert end == date(2026, 1, 5)

    def test_single_day_event(self) -> None:
        assert parse_dates("took place on Jan 15 2026") == (date(2026, 1, 15), date(2026, 1, 15))

    def test_no_dates_is_not_an_error(self) -> None:
        assert parse_dates("nothing datelike here") == (None, None)


class TestPrizePool:
    def test_dollar_amount_with_separators(self) -> None:
        assert parse_prize_pool(DREAMLEAGUE_29) == (1_000_000.0, "USD")

    def test_dollar_amount_without_separators(self) -> None:
        assert parse_prize_pool(ASGARD) == (20_000.0, "USD")

    def test_non_dollar_currency(self) -> None:
        amount, currency = parse_prize_pool(BETBOOM_SHOWMATCH)
        assert (amount, currency) == (4_000_000.0, "RUB")


class TestDescription:
    def test_reads_the_full_picture(self) -> None:
        meta = parse_meta_description(DREAMLEAGUE_29, "DreamLeague Season 29")
        assert meta.tier is LeagueTier.TIER1
        assert meta.is_lan is False
        assert meta.team_count == 16
        assert meta.start_date == date(2026, 5, 13)
        assert meta.display_name == "DreamLeague Season 29"
        assert meta.is_showmatch is False

    def test_hybrid_event_counts_as_lan(self) -> None:
        """ "online & offline" - the offline half is the one that matters."""
        assert parse_meta_description(BLAST_SLAM).is_lan is True

    def test_showmatch_is_flagged(self) -> None:
        """Showmatches are not competitive results and must not train anything."""
        meta = parse_meta_description(BETBOOM_SHOWMATCH)
        assert meta.is_showmatch is True
        assert meta.tier is LeagueTier.TIER3

    def test_empty_description_yields_nothing_invented(self) -> None:
        meta = parse_meta_description("")
        assert meta.tier is LeagueTier.UNKNOWN
        assert meta.start_date is None
        assert meta.team_count is None


class TestSignals:
    def test_full_containment_scores_one(self) -> None:
        overlap = date_overlap(
            (date(2026, 5, 14), date(2026, 5, 23)), (date(2026, 5, 13), date(2026, 5, 24))
        )
        assert overlap == 1.0

    def test_disjoint_windows_score_zero(self) -> None:
        overlap = date_overlap(
            (date(2026, 5, 1), date(2026, 5, 10)), (date(2026, 7, 1), date(2026, 7, 10))
        )
        assert overlap == 0.0

    def test_unknown_dates_give_no_signal(self) -> None:
        assert date_overlap((None, None), (date(2026, 5, 1), date(2026, 5, 2))) is None

    def test_team_counts(self) -> None:
        assert count_agreement(16, 16) == 1.0
        assert count_agreement(8, 16) == 0.5
        assert count_agreement(None, 16) is None


class TestCombining:
    def test_disjoint_dates_veto_a_perfect_name(self) -> None:
        """The trap this exists for: two editions of one series read almost identically,
        and the dates are the only thing that separates them."""
        assert combine_signals(name=1.0, overlap=0.0, teams=1.0) <= 0.45

    def test_matching_dates_lift_a_near_miss(self) -> None:
        """A correct match sitting just under the threshold should be rescued."""
        lifted = combine_signals(name=0.80, overlap=1.0, teams=1.0)
        assert lifted > 0.82

    def test_corroboration_cannot_carry_a_bad_name(self) -> None:
        assert combine_signals(name=0.30, overlap=1.0, teams=1.0) < 0.82

    def test_wildly_different_team_count_caps_the_score(self) -> None:
        assert combine_signals(name=1.0, overlap=None, teams=0.1) <= 0.6

    def test_no_extra_signals_leaves_the_name_alone(self) -> None:
        assert combine_signals(name=0.7, overlap=None, teams=None) == 0.7


class TestProposalWithEvidence:
    def test_dates_rescue_the_real_pgl_wallachia_case(self) -> None:
        """Observed live: the names scored 0.80 and the match was correct."""
        meta = parse_meta_description(
            "PGL Wallachia Season 8 is an offline Romanian Dota 2 tournament organized by "
            "PGL. This Tier 1 tournament took place from Apr 18 to 26 2026 featuring 16 "
            "teams competing over a total prize pool of $1,000,000 USD.",
            "PGL Wallachia Season 8",
        )
        proposal = best_proposal(
            1,
            "PGL Wallachia 2026 Season 8",
            [("PGL/Wallachia/8", ["Category:Tournaments", "Category:Tier 1 Tournaments"], meta)],
            LeagueEvidence(
                first_match=date(2026, 4, 18), last_match=date(2026, 4, 26), team_count=16
            ),
        )
        assert proposal is not None
        assert proposal.is_confident
        assert proposal.tier is LeagueTier.TIER1

    def test_wrong_edition_is_rejected_even_with_a_close_name(self) -> None:
        meta = parse_meta_description(
            "DreamLeague Season 28 is an online European Dota 2 tournament. This Tier 1 "
            "tournament took place from Feb 03 to 15 2026 featuring 16 teams competing "
            "over a total prize pool of $1,000,000 USD.",
            "DreamLeague Season 28",
        )
        proposal = best_proposal(
            1,
            "DreamLeague Season 29",
            [("DreamLeague/28", ["Category:Tournaments"], meta)],
            LeagueEvidence(
                first_match=date(2026, 5, 13), last_match=date(2026, 5, 24), team_count=16
            ),
        )
        assert proposal is not None
        assert not proposal.is_confident

    @pytest.mark.parametrize("missing", [None])
    def test_absent_metadata_falls_back_to_the_name(self, missing: None) -> None:
        proposal = best_proposal(
            1, "DreamLeague Season 29", [("DreamLeague/29", ["Category:Tournaments"], missing)]
        )
        assert proposal is not None
        assert proposal.is_confident
        assert proposal.date_overlap is None

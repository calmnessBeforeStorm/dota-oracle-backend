"""Ordering the live feed when the tier is unknown (F1, spec section 8.1).

The feed is whatever `GetLiveLeagueGames` reports, and on a normal day that is community
cups. Measured 10.09.2026: 35 live games across 25 leagues, every one of them
`tier = unknown` - so a tier filter alone leaves the viewer with an unordered list of names
they have never heard of.

Team history separates them where the markup cannot. Same day, matches from the last 60
days, median of the *smaller* of the two teams' match counts:

    tier1     78
    tier3     21
    tier2      8
    unknown    5

And it finds what the markup missed: among the leagues live at that moment, Destiny League
scored 1018 while the rest sat between 1 and 10. That is a tournament of real teams that
simply has no Liquipedia page - exactly the row that should rise.

The smaller of the two counts is the measure, not the average: a veteran roster against a
stack playing its first game is not a professional match, and averaging would call it one.
"""

from app.ingestion.workers.live_poller import notability, sort_feed


class TestNotability:
    def test_takes_the_weaker_side(self) -> None:
        # Averaging would rate this 250 and float it to the top of the feed.
        assert notability(radiant_matches=500, dire_matches=1) == 1

    def test_two_known_teams_score_high(self) -> None:
        assert notability(radiant_matches=500, dire_matches=420) == 420

    def test_an_unknown_team_is_zero_not_missing(self) -> None:
        # A team we hold nothing for is a stack with no history, which is information, not
        # an absence: it belongs at the bottom rather than being dropped from the feed.
        assert notability(radiant_matches=None, dire_matches=300) == 0
        assert notability(radiant_matches=None, dire_matches=None) == 0


class TestFeedOrder:
    def test_the_feed_is_sorted_by_notability(self) -> None:
        feed = [
            {"match_id": 1, "team_history": 3},
            {"match_id": 2, "team_history": 1018},
            {"match_id": 3, "team_history": 10},
        ]

        assert [entry["match_id"] for entry in sort_feed(feed)] == [2, 3, 1]

    def test_ties_keep_the_order_valve_gave(self) -> None:
        # Sorting has to be stable: a feed that reshuffles equal rows every 20 seconds makes
        # the page twitch for no reason.
        feed = [
            {"match_id": 1, "team_history": 5},
            {"match_id": 2, "team_history": 5},
            {"match_id": 3, "team_history": 5},
        ]

        assert [entry["match_id"] for entry in sort_feed(feed)] == [1, 2, 3]

    def test_a_feed_without_the_field_does_not_explode(self) -> None:
        # Entries written by an older poller can still be in Redis across a deploy.
        feed = [{"match_id": 1}, {"match_id": 2, "team_history": 7}]

        assert [entry["match_id"] for entry in sort_feed(feed)] == [2, 1]

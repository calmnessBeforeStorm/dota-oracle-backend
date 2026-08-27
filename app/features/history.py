"""Rolling history: hero winrates, team form, head-to-head (spec section 6.2).

Three accumulators sharing one discipline. Each is fed a finished match and answers
questions only about matches it has already been fed, so a single chronological sweep can
ask them for a pre-match view and then hand them the result.

Computing any of these over the whole dataset instead would be the classic leak: a hero's
winrate over a period includes the very match being described.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

#: Weight of a result this many days old halves. Recent form should dominate, but a month
#: ago is evidence, not noise.
FORM_HALF_LIFE_DAYS = 30.0

#: Results older than this stop counting at all - rosters change and the sample stops being
#: about the same team.
FORM_WINDOW = timedelta(days=120)

#: Head-to-head beyond six months is a different pair of teams wearing the same names.
H2H_WINDOW = timedelta(days=180)

#: Games below which a hero's winrate is mostly prior. Shrinkage pulls it back to even
#: rather than letting a 2-0 hero read as unbeatable.
HERO_PRIOR_GAMES = 50


def decay(age: timedelta, half_life_days: float = FORM_HALF_LIFE_DAYS) -> float:
    return float(0.5 ** (age.total_seconds() / 86400.0 / half_life_days))


@dataclass
class HeroStats:
    """Winrate per hero, from matches already seen.

    The crudest of the three options in spec section 6.3 - a plain winrate table before any
    synergy or counter-pick modelling - and deliberately so: it is the version whose failure
    modes are obvious.
    """

    wins: dict[int, float] = field(default_factory=lambda: defaultdict(float))
    games: dict[int, float] = field(default_factory=lambda: defaultdict(float))

    def winrate(self, hero_id: int) -> float:
        """Shrunk toward even, so a rarely picked hero cannot dominate the feature."""
        played = self.games.get(hero_id, 0.0)
        won = self.wins.get(hero_id, 0.0)
        return (won + 0.5 * HERO_PRIOR_GAMES) / (played + HERO_PRIOR_GAMES)

    def side_advantage(
        self, radiant_heroes: tuple[int, ...], dire_heroes: tuple[int, ...]
    ) -> float:
        """Mean winrate of one draft against the other, centred on zero."""
        if not radiant_heroes or not dire_heroes:
            return 0.0
        radiant = sum(self.winrate(h) for h in radiant_heroes) / len(radiant_heroes)
        dire = sum(self.winrate(h) for h in dire_heroes) / len(dire_heroes)
        return radiant - dire

    def observe(
        self, radiant_heroes: tuple[int, ...], dire_heroes: tuple[int, ...], radiant_win: bool
    ) -> None:
        for hero in radiant_heroes:
            self.games[hero] += 1
            self.wins[hero] += 1.0 if radiant_win else 0.0
        for hero in dire_heroes:
            self.games[hero] += 1
            self.wins[hero] += 0.0 if radiant_win else 1.0


@dataclass(frozen=True)
class Result:
    played_at: datetime
    won: bool


@dataclass
class TeamForm:
    """Recent results per team, weighted by how long ago they were."""

    results: dict[int, deque[Result]] = field(default_factory=lambda: defaultdict(deque))

    def form(self, team_id: int | None, now: datetime) -> float:
        """Time-decayed winrate in [0, 1], or 0.5 when there is nothing to go on."""
        if team_id is None:
            return 0.5
        recent = [r for r in self.results.get(team_id, ()) if now - r.played_at <= FORM_WINDOW]
        if not recent:
            return 0.5

        weight = sum(decay(now - r.played_at) for r in recent)
        won = sum(decay(now - r.played_at) for r in recent if r.won)
        return won / weight if weight else 0.5

    def maps_since(self, team_id: int | None, now: datetime, window: timedelta) -> int:
        """How many maps a team has played recently - fatigue, and a series in progress."""
        if team_id is None:
            return 0
        return sum(1 for r in self.results.get(team_id, ()) if now - r.played_at <= window)

    def rest_days(self, team_id: int | None, now: datetime) -> float | None:
        """Days since this team last played. None when we have never seen them."""
        played = self.results.get(team_id) if team_id is not None else None
        if not played:
            return None
        return (now - max(r.played_at for r in played)).total_seconds() / 86400.0

    def observe(self, team_id: int | None, played_at: datetime, won: bool) -> None:
        if team_id is None:
            return
        history = self.results[team_id]
        history.append(Result(played_at, won))
        # Bounded so a long backfill cannot grow these without limit; far more than the
        # window can hold.
        while len(history) > 400:
            history.popleft()


@dataclass
class HeadToHead:
    """Results between specific pairs of teams."""

    meetings: dict[tuple[int, int], deque[tuple[datetime, bool]]] = field(
        default_factory=lambda: defaultdict(deque)
    )

    @staticmethod
    def _key(left: int, right: int) -> tuple[int, int]:
        return (left, right) if left <= right else (right, left)

    def advantage(self, team_id: int | None, opponent_id: int | None, now: datetime) -> float:
        """Time-decayed head-to-head winrate for `team_id`, 0.5 when they have not met."""
        if team_id is None or opponent_id is None:
            return 0.5
        key = self._key(team_id, opponent_id)
        recent = [
            (played_at, won)
            for played_at, won in self.meetings.get(key, ())
            if now - played_at <= H2H_WINDOW
        ]
        if not recent:
            return 0.5

        # Stored from the perspective of the lower id; flip when asking for the other side.
        flip = key[0] != team_id
        weight = sum(decay(now - played_at) for played_at, _ in recent)
        won = sum(decay(now - played_at) for played_at, won in recent if (won != flip))
        return won / weight if weight else 0.5

    def observe(
        self, team_id: int | None, opponent_id: int | None, played_at: datetime, team_won: bool
    ) -> None:
        if team_id is None or opponent_id is None:
            return
        key = self._key(team_id, opponent_id)
        # Always stored from the lower id's point of view.
        won_by_key_owner = team_won if key[0] == team_id else not team_won
        history = self.meetings[key]
        history.append((played_at, won_by_key_owner))
        while len(history) > 100:
            history.popleft()

"""`GameState` - the single intermediate representation every data source is adapted into.

Spec section 6.4 (train/serve parity). Feature computation must exist in exactly ONE place,
used both by the offline pipeline (OpenDota parsed replays) and by the live service
(Steam GetRealtimeStats). The two sources have completely different field layouts, so each
gets an adapter into this struct, and features are only ever computed from this struct.

Skipping this guarantees train/serve skew: the model looks great in the notebook and worse
in production, with no way to tell why.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.db.models.enums import SeriesFormat


@dataclass(frozen=True)
class TeamState:
    """State of one side at a given minute."""

    score: int = 0  # kills
    net_worth: int = 0
    # Living buildings. Barracks: 2 per lane (melee/ranged).
    towers_alive: dict[str, int] = field(default_factory=dict)  # {"top": 3, "mid": 3, "bot": 3}
    barracks_alive: dict[str, int] = field(default_factory=dict)  # {"top": 2, "mid": 2, "bot": 2}
    ancient_alive: bool = True
    player_net_worths: tuple[int, ...] = ()

    @property
    def tower_count(self) -> int:
        return sum(self.towers_alive.values())

    @property
    def barracks_count(self) -> int:
        return sum(self.barracks_alive.values())


@dataclass(frozen=True)
class SeriesContext:
    """Series-level context (spec section 5.5).

    `game_in_series` alone is misleading; it must always travel with `series_format` and
    `is_conditional_game`, otherwise the model learns the format artifact.
    """

    series_format: SeriesFormat = SeriesFormat.BO1
    game_in_series: int = 1
    is_conditional_game: bool = False
    radiant_series_wins: int = 0
    dire_series_wins: int = 0
    #: Whether `series_format` was known or filled in.
    #:
    #: An unknown format collapses to Bo1 here, because a feature vector of floats has no way
    #: to say "unknown" - and a Bo1 has one map, so `is_conditional_game` is then forced to
    #: False. Measured: 19.4% of maps in series with a known format are conditional against
    #: 0.1% of the filled ones, so without this flag that feature does not mean "this map is
    #: decisive", it means "this map is decisive *and* we mapped its league to Liquipedia".
    format_known: bool = False


@dataclass(frozen=True)
class GameState:
    """Full state of one map at one minute. The only input features are computed from."""

    match_id: int
    minute: int
    radiant: TeamState
    dire: TeamState
    gold_adv: int  # radiant minus dire
    xp_adv: int
    roshan_kills: int = 0
    aegis_holder_is_radiant: bool | None = None
    roshan_respawn_in: int | None = None  # seconds, None if alive
    radiant_picks: tuple[int, ...] = ()  # hero ids
    dire_picks: tuple[int, ...] = ()
    series: SeriesContext = field(default_factory=SeriesContext)
    #: What was knowable before the map started (spec section 6.2): skill, form, head to
    #: head, draft. Empty when the pre-match sweep has not covered this match - which is
    #: honest, since the alternative is inventing a strength for teams we know nothing about.
    prematch: Mapping[str, float] = field(default_factory=dict)
    is_lan: bool | None = None
    tier: int = 1  # always 1 at inference time (spec section 5.4)
    prematch_prior: float | None = None  # p(radiant win) from the pre-match model

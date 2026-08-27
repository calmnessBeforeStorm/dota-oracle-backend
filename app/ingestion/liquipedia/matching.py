"""Matching OpenDota leagues to Liquipedia tournament pages (spec section 3).

The two sources name the same tournament differently, and neither carries the other's id:

    OpenDota   "DreamLeague Season 29"        Liquipedia  "DreamLeague/29"
    OpenDota   "Esports World Cup 2026"       Liquipedia  "Esports World Cup/2026"
    OpenDota   "EPL Masters 2026"             Liquipedia  ?

So the mapping is scored, not derived, and the spec calls it semi-manual for good reason:
a wrong match silently mislabels every game of a tournament as Tier 1 or not. Everything
here produces *proposals* with a score; what crosses the confidence threshold can be
applied automatically, and the rest is meant for a human.
"""

import re
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher

from app.db.models.enums import LeagueTier
from app.ingestion.liquipedia.meta import TournamentMeta

#: Liquipedia separates a tournament from its edition with a slash.
_SEPARATORS = re.compile(r"[/_]+")
#: Everything else non-word, dashes of every width included, collapses here.
_NOISE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

#: Words that carry no identity: they appear in one source and not the other.
_STOPWORDS = frozenset(
    {"dota", "dota2", "the", "presented", "by", "powered", "season", "tournament", "esports"}
)

#: Liquipedia category -> what it tells us.
TIER_CATEGORIES: dict[str, LeagueTier] = {
    "Category:Tier 1 Tournaments": LeagueTier.TIER1,
    "Category:Tier 2 Tournaments": LeagueTier.TIER2,
    "Category:Tier 3 Tournaments": LeagueTier.TIER3,
}
LAN_CATEGORY = "Category:Offline Tournaments"
ONLINE_CATEGORY = "Category:Online Tournaments"
TOURNAMENT_CATEGORY = "Category:Tournaments"


def normalize_name(name: str) -> str:
    """Reduce a tournament name to the tokens that actually identify it.

    "DreamLeague Season 29" and "DreamLeague/29" both come out as "dreamleague 29", which is
    the whole point: the edition number is identity, the word "Season" is not.
    """
    text = _SEPARATORS.sub(" ", name.lower())
    text = _NOISE.sub(" ", text)
    tokens = [t for t in _SPACES.sub(" ", text).strip().split() if t and t not in _STOPWORDS]
    return " ".join(tokens)


def name_score(league_name: str, page_title: str) -> float:
    """Similarity in 0..1 between an OpenDota league name and a Liquipedia page title.

    Token overlap is blended with sequence similarity: overlap alone rates "DreamLeague/29"
    and "DreamLeague/28" as identical, sequence similarity alone punishes the reordering
    that the slash notation causes.
    """
    left, right = normalize_name(league_name), normalize_name(page_title)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    left_tokens, right_tokens = set(left.split()), set(right.split())
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(None, left, right).ratio()

    # A digit present in one name and absent in the other usually means a different edition
    # of the same series - the single most dangerous confusion here.
    left_digits = {t for t in left_tokens if t.isdigit()}
    right_digits = {t for t in right_tokens if t.isdigit()}
    if left_digits and right_digits and not (left_digits & right_digits):
        return min(0.49, (overlap + sequence) / 2)

    return (overlap + sequence) / 2


@dataclass(frozen=True)
class LeagueEvidence:
    """What our own data says about a league, for cross-checking a candidate page.

    Derived from the matches we already hold, so it costs nothing and is independent of
    anything Liquipedia claims - which is what makes it worth checking against.
    """

    first_match: date | None = None
    last_match: date | None = None
    team_count: int | None = None
    #: Normalized names of the teams we actually saw play. The escalation signal.
    team_names: frozenset[str] = frozenset()
    #: Who won the last match of the league - our best guess at the champion.
    champion: str | None = None


def date_overlap(
    ours: tuple[date | None, date | None], theirs: tuple[date | None, date | None]
) -> float | None:
    """Share of our own match window covered by the tournament's stated dates, 0..1.

    None when either side is unknown. Tournaments cluster in the same weeks, so a high
    overlap is weak evidence for a match while a zero overlap is strong evidence against.
    """
    (our_start, our_end), (their_start, their_end) = ours, theirs
    if not (our_start and our_end and their_start and their_end):
        return None

    latest_start = max(our_start, their_start)
    earliest_end = min(our_end, their_end)
    overlap = (earliest_end - latest_start).days + 1
    if overlap <= 0:
        return 0.0

    span = (our_end - our_start).days + 1
    return min(1.0, overlap / span)


def count_agreement(ours: int | None, theirs: int | None) -> float | None:
    """1.0 when the team counts match, falling off with the difference."""
    if ours is None or theirs is None or theirs <= 0:
        return None
    return max(0.0, 1.0 - abs(ours - theirs) / theirs)


@dataclass(frozen=True)
class MappingProposal:
    """A candidate mapping, with everything a reviewer needs to accept or reject it."""

    league_id: int
    league_name: str
    page_title: str
    score: float
    tier: LeagueTier
    is_lan: bool | None
    #: False when the page is not a tournament at all - a team or player article that the
    #: search happened to rank highly.
    is_tournament: bool
    #: Similarity of the names alone, before the other signals adjusted it.
    name_only_score: float = 0.0
    #: None when either side could not supply dates or a team count.
    date_overlap: float | None = None
    team_agreement: float | None = None
    #: Share of our teams found on the page. None until the expensive lookup is spent.
    roster_overlap: float | None = None
    #: Whether the page's winner is the team that won our last match. None when unknown.
    winner_agrees: bool | None = None
    is_showmatch: bool = False
    #: Kept so the facts read off the page can be persisted without fetching them again.
    meta: TournamentMeta | None = None

    @property
    def is_confident(self) -> bool:
        return self.is_tournament and self.score >= AUTO_ACCEPT_SCORE

    @property
    def signals(self) -> str:
        """Compact rendering of the corroborating evidence, for the review report."""
        parts = []
        parts.append(f"name {self.name_only_score:.2f}")
        parts.append("dates -" if self.date_overlap is None else f"dates {self.date_overlap:.2f}")
        parts.append(
            "teams -" if self.team_agreement is None else f"teams {self.team_agreement:.2f}"
        )
        # Only present once the expensive lookup has been spent, so its absence is itself
        # information: this candidate was never escalated.
        if self.roster_overlap is not None:
            parts.append(f"roster {self.roster_overlap:.2f}")
        if self.winner_agrees is not None:
            parts.append("winner ok" if self.winner_agrees else "winner MISMATCH")
        return " ".join(parts)


#: Above this a proposal is applied without asking. Chosen to sit above the "different
#: edition of the same series" band that the digit rule caps at 0.49.
AUTO_ACCEPT_SCORE = 0.82


def read_categories(categories: list[str]) -> tuple[LeagueTier, bool | None, bool]:
    """Turn a page's category list into (tier, is_lan, is_tournament)."""
    tier = LeagueTier.UNKNOWN
    for name, value in TIER_CATEGORIES.items():
        if name in categories:
            tier = value
            break

    is_lan: bool | None = None
    if LAN_CATEGORY in categories:
        is_lan = True
    elif ONLINE_CATEGORY in categories:
        is_lan = False

    is_tournament = TOURNAMENT_CATEGORY in categories or tier is not LeagueTier.UNKNOWN
    return tier, is_lan, is_tournament


#: Below this the dates disagree outright and the name similarity cannot be trusted,
#: however high it is: two editions of the same series read almost identically.
DATE_VETO_SCORE = 0.45

#: Team counts this far apart mean different tournaments even under the same name.
TEAM_VETO_AGREEMENT = 0.5


#: Below this the page names almost none of the teams we saw: a different tournament.
ROSTER_VETO = 0.35

#: Above this the page names most of our field. Two unrelated tournaments do not share a
#: roster, so this is allowed to carry a weak name over the line on its own.
ROSTER_DECISIVE = 0.7


def combine_signals(
    name: float,
    overlap: float | None,
    teams: float | None,
    roster: float | None = None,
    winner_agrees: bool | None = None,
) -> float:
    """Fold the corroborating signals into the final confidence.

    The name stays the driver: tournaments cluster in the same weeks and field the same
    number of teams, so dates and counts agreeing says little on its own. Their real value
    is negative - a candidate whose dates do not overlap ours at all is wrong no matter how
    the names read, which is exactly the "different edition of the same series" trap.
    """
    score = name

    if overlap is not None:
        if overlap == 0.0:
            return min(score, DATE_VETO_SCORE)
        # Small, deliberate: enough to rescue a correct match sitting just below the
        # threshold, not enough to carry a bad name over it.
        score = min(1.0, score + 0.08 * overlap)

    if teams is not None:
        if teams < TEAM_VETO_AGREEMENT:
            return min(score, 0.6)
        score = min(1.0, score + 0.04 * teams)

    if roster is not None:
        # Unlike the other signals this one is nearly unique to a tournament, so it is
        # allowed to decide rather than only to nudge - in both directions.
        if roster < ROSTER_VETO:
            return min(score, 0.3)
        if roster >= ROSTER_DECISIVE:
            return max(score, 0.5 + 0.5 * roster)
        score = min(1.0, score + 0.15 * roster)

    if winner_agrees is not None:
        # One name against a dozen, so it confirms modestly. Disagreement is louder: two
        # tournaments with the same field but different champions are different events.
        score = min(1.0, score + 0.05) if winner_agrees else min(score, 0.5)

    return score


def best_proposal(
    league_id: int,
    league_name: str,
    candidates: list[tuple[str, list[str], TournamentMeta | None]],
    evidence: LeagueEvidence | None = None,
) -> MappingProposal | None:
    """Pick the best-scoring candidate page for one league.

    `candidates` is (page_title, categories, meta) - the last from `prop=pageprops`, which
    costs the same batched request as the categories. Returns None when nothing scored.
    """
    ours = LeagueEvidence() if evidence is None else evidence
    best: MappingProposal | None = None

    for title, categories, meta in candidates:
        tier, is_lan, is_tournament = read_categories(categories)

        # The page title is a slug; the display name is what OpenDota also stores, so it
        # usually matches far better. Take whichever reads closer.
        titles = [title]
        if meta and meta.display_name:
            titles.append(meta.display_name)
        name = max(name_score(league_name, candidate) for candidate in titles)

        overlap = (
            date_overlap((ours.first_match, ours.last_match), (meta.start_date, meta.end_date))
            if meta
            else None
        )
        teams = count_agreement(ours.team_count, meta.team_count) if meta else None

        # The description states the tier too, and it is generated from the same source as
        # the category, so it fills in when the category list is truncated.
        if tier is LeagueTier.UNKNOWN and meta and meta.tier is not LeagueTier.UNKNOWN:
            tier = meta.tier
        if is_lan is None and meta:
            is_lan = meta.is_lan

        proposal = MappingProposal(
            league_id=league_id,
            league_name=league_name,
            page_title=title,
            score=combine_signals(name, overlap, teams),
            tier=tier,
            is_lan=is_lan,
            is_tournament=is_tournament or bool(meta and meta.start_date),
            name_only_score=name,
            date_overlap=overlap,
            team_agreement=teams,
            is_showmatch=bool(meta and meta.is_showmatch),
        )
        # A non-tournament page never wins over a tournament one, however well it scores.
        if best is None or (proposal.is_tournament, proposal.score) > (
            best.is_tournament,
            best.score,
        ):
            best = proposal

    return best if best and best.score > 0 else None

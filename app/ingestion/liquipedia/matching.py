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
from difflib import SequenceMatcher

from app.db.models.enums import LeagueTier

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

    @property
    def is_confident(self) -> bool:
        return self.is_tournament and self.score >= AUTO_ACCEPT_SCORE


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


def best_proposal(
    league_id: int,
    league_name: str,
    candidates: list[tuple[str, list[str]]],
) -> MappingProposal | None:
    """Pick the best-scoring candidate page for one league.

    `candidates` is (page_title, categories) as returned by the search plus a category
    lookup. Returns None when nothing scored above zero.
    """
    best: MappingProposal | None = None
    for title, categories in candidates:
        tier, is_lan, is_tournament = read_categories(categories)
        proposal = MappingProposal(
            league_id=league_id,
            league_name=league_name,
            page_title=title,
            score=name_score(league_name, title),
            tier=tier,
            is_lan=is_lan,
            is_tournament=is_tournament,
        )
        # A non-tournament page never wins over a tournament one, however well it scores.
        if best is None or (proposal.is_tournament, proposal.score) > (
            best.is_tournament,
            best.score,
        ):
            best = proposal

    return best if best and best.score > 0 else None

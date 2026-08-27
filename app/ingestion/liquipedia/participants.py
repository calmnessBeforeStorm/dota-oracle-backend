"""Participant rosters from a tournament page (spec section 3).

The strongest identity signal available, and the most expensive. Names and dates can both
be shared by unrelated tournaments - the same organiser runs the same series every quarter,
and half the calendar overlaps - but the list of teams that actually turned up is close to
unique, and we already know ours exactly from the matches we hold.

Reading it needs the page source, which is the `parse` call rate limited to one request per
30 seconds. That is why it is spent only on candidates the cheap signals could not settle.
"""

import re
from difflib import SequenceMatcher

#: `|team=Team Spirit` in participant tables and prize pool rows.
_TEAM_PARAM = re.compile(r"\|\s*team\d*\s*=\s*([^\n|}]+)")
#: `{{Opponent|Team Spirit|score=2}}` in match and bracket templates.
_OPPONENT = re.compile(r"\{\{\s*Opponent\s*\|([^|}\n]+)", re.IGNORECASE)

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

#: Template parameters that look like a team name but are not.
_NOT_A_TEAM = re.compile(r"^\s*\w+\s*=", re.UNICODE)

#: Two spellings this close are the same organisation: "Virtus.Pro" against "Virtus.pro",
#: "ex-HEROIC" against "Ex-Heroic".
SAME_TEAM_RATIO = 0.9

#: Names are not stable identifiers, and no amount of string matching fixes that.
#: Valve bars betting sponsors from The International, so PARIVISION enters as Vision and
#: BetBoom Team as BoomBoys, while Liquipedia keeps the canonical name. Observed in our own
#: data: "TEAM VISION", "PVISION", "BoomBoys", "Team Yandex" - and "BoomBoys" under two
#: different team ids.
#:
#: Nothing here tries to resolve that. Roster overlap survives it because it is a ratio over
#: a dozen names rather than an equality, so a few renames lower the score without breaking
#: the match - which is also why the veto threshold sits well below one, and why a single
#: mismatched champion is not allowed to overturn an agreeing roster.


def normalize_team(name: str) -> str:
    """Case, spacing and punctuation differ between the two sources for the same team."""
    text = _PUNCTUATION.sub(" ", name.lower())
    return _SPACES.sub(" ", text).strip()


def extract_participants(wikitext: str) -> set[str]:
    """Normalized team names mentioned on the page.

    Deliberately greedy: participant tables, prize pool rows and match templates all name
    teams, and a superset costs nothing here - the comparison is an overlap, not equality.
    """
    found: set[str] = set()
    for pattern in (_TEAM_PARAM, _OPPONENT):
        for raw in pattern.findall(wikitext):
            # `{{Opponent|score=2}}` and friends match the pattern but name no team.
            if _NOT_A_TEAM.match(raw):
                continue
            name = normalize_team(raw)
            if name and not name.isdigit() and len(name) > 1:
                found.add(name)
    return found


def roster_overlap(ours: set[str], theirs: set[str]) -> float | None:
    """Share of our teams that appear on the page, 0..1, or None if either side is empty.

    Measured against our own roster rather than the union: a page listing qualifier teams we
    never saw should not be penalised for being more complete than our slice of the data.
    """
    if not ours or not theirs:
        return None

    remaining = set(theirs)
    matched = 0
    for team in ours:
        if team in remaining:
            remaining.discard(team)
            matched += 1
            continue
        # Fall back to near-identical spellings before giving up on the team.
        close = next(
            (
                other
                for other in remaining
                if SequenceMatcher(None, team, other).ratio() >= SAME_TEAM_RATIO
            ),
            None,
        )
        if close is not None:
            remaining.discard(close)
            matched += 1

    return matched / len(ours)


#: `{{Placement|1|{{Opponent|Team Spirit}}}}` - the prize pool table, first row.
_FIRST_PLACE = re.compile(
    r"\{\{\s*Placement\s*\|\s*1\s*\|(?P<body>.{0,200}?)\}\}\s*\}\}", re.IGNORECASE | re.DOTALL
)


def extract_winner(wikitext: str) -> str | None:
    """Normalized name of the team placed first, if the page states one.

    A far narrower signal than the roster - one name against a dozen - but it is the single
    fact hardest for two different tournaments to share, and it costs nothing once the page
    has already been fetched for the roster.
    """
    match = _FIRST_PLACE.search(wikitext)
    if not match:
        return None
    opponent = _OPPONENT.search(match.group("body"))
    if not opponent or _NOT_A_TEAM.match(opponent.group(1)):
        return None
    name = normalize_team(opponent.group(1))
    return name or None


def same_team(left: str | None, right: str | None) -> bool | None:
    """Whether two names denote the same organisation. None when either is missing."""
    if not left or not right:
        return None
    if left == right:
        return True
    return SequenceMatcher(None, left, right).ratio() >= SAME_TEAM_RATIO

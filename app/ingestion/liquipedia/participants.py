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

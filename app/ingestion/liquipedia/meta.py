"""Structured tournament facts from Liquipedia page properties (spec section 3).

Matching on the name alone is not enough - the spec asks for name plus dates plus prize
pool - but dates and prize pool live in the page infobox, and reading that costs a `parse`
call rate limited to one per 30 seconds. Over five candidates per league that is over an
hour of waiting.

`prop=pageprops` avoids it. Liquipedia generates a `metadescl` property with a fixed shape:

    DreamLeague Season 29 is an online European Dota 2 tournament organized by ESL Gaming.
    This Tier 1 tournament took place from May 13 to 24 2026 featuring 16 teams competing
    over a total prize pool of $1,000,000 USD.

That is a plain `query` request, batched across up to fifty titles at one per two seconds,
and it carries the dates, the tier, the venue, the team count and the prize pool. The
companion `displaytitle` property gives the human-readable name, which is what OpenDota
also stores - "DreamLeague Season 29" rather than the page title "DreamLeague/29".

The text is generated, not written by hand, so parsing it is reasonable. It is still prose:
every field is optional and a miss returns None rather than a guess.
"""

import re
from dataclasses import dataclass
from datetime import date

from app.db.models.enums import LeagueTier

TIER_WORDS: dict[str, LeagueTier] = {
    "tier 1": LeagueTier.TIER1,
    "tier 2": LeagueTier.TIER2,
    "tier 3": LeagueTier.TIER3,
}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip

#: "from May 13 to 24 2026" - one month, the second date is a bare day.
_SAME_MONTH = re.compile(
    r"from\s+(?P<m>[A-Za-z]{3})\w*\s+(?P<d1>\d{1,2})\s+to\s+(?P<d2>\d{1,2})\s+(?P<y>\d{4})",
    re.IGNORECASE,
)
#: "from Jul 28 to Aug 11 2026" - spans a month boundary.
_CROSS_MONTH = re.compile(
    r"from\s+(?P<m1>[A-Za-z]{3})\w*\s+(?P<d1>\d{1,2})\s+to\s+"
    r"(?P<m2>[A-Za-z]{3})\w*\s+(?P<d2>\d{1,2})\s+(?P<y>\d{4})",
    re.IGNORECASE,
)
#: "on Jan 15 2026" - a single-day event.
_SINGLE_DAY = re.compile(
    r"on\s+(?P<m>[A-Za-z]{3})\w*\s+(?P<d>\d{1,2})\s+(?P<y>\d{4})", re.IGNORECASE
)

_TEAM_COUNT = re.compile(r"featuring\s+(?P<n>\d+)\s+teams", re.IGNORECASE)
#: "$1,000,000 USD" but also "4,000,000₽ RUB" - the symbol sits on either side of the
#: amount depending on the currency, so both positions are allowed.
_PRIZE = re.compile(
    r"prize\s+pool\s+of\s+(?P<symbol>[^\d\s]*)\s*(?P<amount>[\d,.]+)\s*"
    r"[^\s\w]*\s*(?P<currency>[A-Z]{3})?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TournamentMeta:
    """What the generated description states. Every field is optional."""

    display_name: str | None = None
    tier: LeagueTier = LeagueTier.UNKNOWN
    is_lan: bool | None = None
    start_date: date | None = None
    end_date: date | None = None
    team_count: int | None = None
    prize_pool: float | None = None
    currency: str | None = None
    #: Showmatches are not competitive results and must not train anything.
    is_showmatch: bool = False


def month_number(name: str) -> int | None:
    """Month from its name or three-letter abbreviation."""
    return _MONTHS.get(name[:3].lower())


def parse_dates(text: str) -> tuple[date | None, date | None]:
    """Read the date range. Cross-month is tried first: it is the more specific pattern."""
    cross = _CROSS_MONTH.search(text)
    if cross:
        m1, m2 = month_number(cross.group("m1")), month_number(cross.group("m2"))
        year = int(cross.group("y"))
        if m1 and m2:
            start = date(year, m1, int(cross.group("d1")))
            end = date(year, m2, int(cross.group("d2")))
            # A range that runs backwards spans New Year: it started the previous year.
            if end < start:
                start = date(year - 1, m1, int(cross.group("d1")))
            return start, end

    same = _SAME_MONTH.search(text)
    if same:
        month = month_number(same.group("m"))
        year = int(same.group("y"))
        if month:
            return (
                date(year, month, int(same.group("d1"))),
                date(year, month, int(same.group("d2"))),
            )

    single = _SINGLE_DAY.search(text)
    if single:
        month = month_number(single.group("m"))
        if month:
            day = date(int(single.group("y")), month, int(single.group("d")))
            return day, day

    return None, None


def parse_prize_pool(text: str) -> tuple[float | None, str | None]:
    match = _PRIZE.search(text)
    if not match:
        return None, None
    try:
        amount = float(match.group("amount").replace(",", ""))
    except ValueError:
        return None, None
    currency = match.group("currency")
    if currency is None and match.group("symbol") == "$":
        currency = "USD"
    return amount, currency


def parse_meta_description(text: str, display_name: str | None = None) -> TournamentMeta:
    """Turn a `metadescl` string into structured facts."""
    if not text:
        return TournamentMeta(display_name=display_name)

    lowered = text.lower()

    tier = LeagueTier.UNKNOWN
    for word, value in TIER_WORDS.items():
        if word in lowered:
            tier = value
            break

    # "online & offline" happens for hybrid events; the offline half is what matters for
    # the LAN flag, so it wins.
    is_lan: bool | None = None
    if "offline" in lowered:
        is_lan = True
    elif "online" in lowered:
        is_lan = False

    start, end = parse_dates(text)
    teams = _TEAM_COUNT.search(text)
    prize, currency = parse_prize_pool(text)

    return TournamentMeta(
        display_name=display_name,
        tier=tier,
        is_lan=is_lan,
        start_date=start,
        end_date=end,
        team_count=int(teams.group("n")) if teams else None,
        prize_pool=prize,
        currency=currency,
        is_showmatch="showmatch" in lowered,
    )

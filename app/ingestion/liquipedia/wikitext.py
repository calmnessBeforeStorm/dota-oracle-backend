"""Extracting series formats from Liquipedia tournament pages (spec sections 3, 5.5).

This is the only place the format of a series can come from. Valve's `series_type` cannot
express Bo2 at all, so without this the group stages of The International - which are Bo2 -
would be silently recorded as something else.

Liquipedia writes formats as templates inside the prose of the `== Format ==` section:

    *'''Group Stage''' - ''October 12 - October 15, 2023''
    **'''Phase One'''
    ***Single round-robin
    ***All series are {{Abbr/Bo2}}

so the stage is a bold bullet and the format is an `{{Abbr/BoN}}` marker nested under it.
That is prose, not a schema: the extraction is best effort and reports its own confidence.
Anything ambiguous is meant for human review, exactly as the semi-manual mapping in
section 3 anticipates.
"""

import re
from dataclasses import dataclass

from app.db.models.enums import SeriesFormat, StageType

#: `{{Abbr/Bo3}}` and `{{abbr/Bo3}}` both occur in the wild.
ABBR_FORMAT = re.compile(r"\{\{\s*abbr/(bo[1-5])\s*\}\}", re.IGNORECASE)

#: A bullet whose content starts with a bold title: the start of a stage.
STAGE_HEADING = re.compile(r"^(?P<depth>\*+)\s*'''(?P<title>[^']+)'''(?P<rest>.*)$")

FORMAT_SECTION = re.compile(r"^==+\s*Format\s*==+\s*$(?P<body>.*?)(?=^==[^=]|\Z)", re.M | re.S)

_STAGE_TYPE_HINTS: tuple[tuple[str, StageType], ...] = (
    ("swiss", StageType.SWISS),
    ("group", StageType.GROUP),
    ("round-robin", StageType.GROUP),
    ("round robin", StageType.GROUP),
    ("standings", StageType.GROUP),
    ("league", StageType.GROUP),
    ("playoff", StageType.PLAYOFF),
    ("bracket", StageType.PLAYOFF),
    ("main event", StageType.PLAYOFF),
    ("final", StageType.PLAYOFF),
    ("elimination", StageType.PLAYOFF),
)

#: Stages that describe who is taking part rather than how they play.
_NOT_A_STAGE = ("participants", "prize", "broadcast", "qualified teams")


@dataclass(frozen=True)
class StageFormat:
    """One stage of a tournament, as read off the page."""

    name: str
    stage_type: StageType
    default_format: SeriesFormat
    #: Every format marker seen under this stage, in order. More than one means the stage
    #: mixes formats (a Bo5 grand final inside a Bo3 bracket, typically).
    formats_seen: tuple[SeriesFormat, ...]
    #: The line the default was taken from - what a reviewer needs to judge the guess.
    evidence: str

    @property
    def is_ambiguous(self) -> bool:
        """True when the stage mixed formats and the default is a judgement call."""
        return len(set(self.formats_seen)) > 1


def extract_format_section(wikitext: str) -> str | None:
    match = FORMAT_SECTION.search(wikitext)
    return match.group("body") if match else None


def classify_stage(name: str) -> StageType | None:
    lowered = name.lower()
    if any(word in lowered for word in _NOT_A_STAGE):
        return None
    for hint, stage_type in _STAGE_TYPE_HINTS:
        if hint in lowered:
            return stage_type
    return None


#: Exceptions are carved out in the same sentence as the rule - "Grand Final is Bo5, all
#: other matches are Bo3" - so each marker has to be judged by its own clause, not by the
#: whole line.
_CLAUSE_SPLIT = re.compile(r"[,;]")

#: Wording that means "this governs the stage" rather than "this one match".
_GENERAL_WORDS = ("all ", "every ")

#: Wording that marks a single match carved out of the rule.
_EXCEPTION_WORDS = ("grand final", "final is", "decider", "tiebreak")


def markers_in(line: str) -> list[tuple[SeriesFormat, str]]:
    """Every format marker on a line, paired with the clause it sits in."""
    found: list[tuple[SeriesFormat, str]] = []
    for clause in _CLAUSE_SPLIT.split(line):
        for marker in ABBR_FORMAT.findall(clause):
            found.append((SeriesFormat(marker.lower()), clause.strip()))
    return found


def _pick_default(candidates: list[tuple[SeriesFormat, str]]) -> SeriesFormat:
    """Choose the format that governs the stage as a whole.

    A clause claiming to cover everything ("all other matches are Bo3") beats one naming a
    single match ("Grand Final is Bo5"). Failing that, the most frequent marker wins, and
    ties fall back to the first seen.
    """
    for fmt, clause in candidates:
        lowered = clause.lower()
        if any(word in lowered for word in _GENERAL_WORDS) and not any(
            word in lowered for word in _EXCEPTION_WORDS
        ):
            return fmt

    counts: dict[SeriesFormat, int] = {}
    for fmt, _ in candidates:
        counts[fmt] = counts.get(fmt, 0) + 1
    top = max(counts.values())
    return next(fmt for fmt, _ in candidates if counts[fmt] == top)


def parse_stage_formats(wikitext: str) -> list[StageFormat]:
    """Read the Format section into stages that carry a series format.

    Stages with no format marker - participant lists, prize breakdowns - are dropped rather
    than guessed at.
    """
    body = extract_format_section(wikitext)
    if body is None:
        return []

    stages: list[tuple[str, list[tuple[SeriesFormat, str]], list[str]]] = []
    #: Bold headings currently open, by bullet depth. A nested one is a sub-stage of its
    #: parent ("Group Stage" -> "Phase One"), so the name is the whole chain.
    open_titles: dict[int, str] = {}

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if not line.startswith("*"):
            continue

        heading = STAGE_HEADING.match(line)
        if heading:
            depth = len(heading.group("depth"))
            for deeper in [d for d in open_titles if d >= depth]:
                del open_titles[deeper]
            open_titles[depth] = heading.group("title").strip()

            name = " / ".join(open_titles[d] for d in sorted(open_titles))
            stages.append((name, [], []))
            # The heading line itself can carry the marker.
            for fmt, clause in markers_in(heading.group("rest")):
                stages[-1][1].append((fmt, clause))
                stages[-1][2].append(line)
            continue

        found = markers_in(line)
        if found and stages:
            for fmt, clause in found:
                stages[-1][1].append((fmt, clause))
                stages[-1][2].append(line)

    parsed: list[StageFormat] = []
    for name, candidates, lines in stages:
        if not candidates:
            continue
        stage_type = classify_stage(name)
        if stage_type is None:
            continue
        parsed.append(
            StageFormat(
                name=name,
                stage_type=stage_type,
                default_format=_pick_default(candidates),
                formats_seen=tuple(fmt for fmt, _ in candidates),
                # The whole line, not the clause: a reviewer needs the context to judge.
                evidence=" ".join(lines[0].split()),
            )
        )
    return parsed

"""Reading series formats off real Liquipedia pages (spec sections 3, 5.5).

The fixtures are the genuine `== Format ==` sections of three tournaments, saved verbatim
from the live wiki. They cover the cases that matter: a Bo2 group stage (the format Valve
data cannot express at all), a stage that mixes a Bo5 grand final into a Bo3 bracket, and
the lowercase spelling of the template.
"""

from pathlib import Path

import pytest

from app.db.models.enums import SeriesFormat, StageType
from app.ingestion.liquipedia.wikitext import (
    classify_stage,
    extract_format_section,
    parse_stage_formats,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "liquipedia"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.format.txt").read_text(encoding="utf-8")


def stage_named(stages: list, fragment: str):
    matches = [s for s in stages if fragment.lower() in s.name.lower()]
    assert matches, f"no stage matching {fragment!r} in {[s.name for s in stages]}"
    return matches[0]


class TestTheInternational2023:
    """The reason this module exists: TI group stages are Bo2."""

    def test_finds_the_bo2_group_stage(self) -> None:
        stages = parse_stage_formats(fixture("the_international_2023"))
        phase_one = stage_named(stages, "Phase One")

        assert phase_one.default_format is SeriesFormat.BO2
        assert phase_one.stage_type is StageType.GROUP
        assert "All series are" in phase_one.evidence

    def test_sub_stages_keep_their_parent_in_the_name(self) -> None:
        stages = parse_stage_formats(fixture("the_international_2023"))
        phase_one = stage_named(stages, "Phase One")
        assert phase_one.name.startswith("Group Stage")

    def test_seeding_decider_is_bo3(self) -> None:
        stages = parse_stage_formats(fixture("the_international_2023"))
        assert stage_named(stages, "Phase Two").default_format is SeriesFormat.BO3

    def test_participants_section_is_not_a_stage(self) -> None:
        """It lists who plays, not how - inventing a format for it would be a fabrication."""
        stages = parse_stage_formats(fixture("the_international_2023"))
        assert not [s for s in stages if "participants" in s.name.lower()]


class TestDreamLeague29:
    def test_group_stage_is_bo3(self) -> None:
        stages = parse_stage_formats(fixture("dreamleague_29"))
        group = stage_named(stages, "Group Stage")
        assert group.default_format is SeriesFormat.BO3
        assert group.stage_type is StageType.GROUP

    def test_playoffs_default_to_bo3_despite_a_bo5_grand_final(self) -> None:
        """ "Grand Final is Bo5, all other matches are Bo3" - the stage default is the
        clause that covers everything, not the one naming a single match."""
        stages = parse_stage_formats(fixture("dreamleague_29"))
        playoffs = stage_named(stages, "Playoffs")

        assert playoffs.default_format is SeriesFormat.BO3
        assert SeriesFormat.BO5 in playoffs.formats_seen
        assert playoffs.is_ambiguous  # mixed formats: flagged for a human to confirm


class TestDpc2021:
    def test_handles_the_lowercase_template(self) -> None:
        """Pages write both {{Abbr/Bo3}} and {{abbr/Bo3}}."""
        stages = parse_stage_formats(fixture("dpc_2021_sea_lower"))
        standings = stage_named(stages, "Standings")
        assert standings.default_format is SeriesFormat.BO3
        assert standings.stage_type is StageType.GROUP


class TestSectionExtraction:
    def test_returns_none_without_a_format_section(self) -> None:
        assert extract_format_section("== Prize Pool ==\nnothing here") is None

    def test_stops_at_the_next_section(self) -> None:
        body = extract_format_section("== Format ==\n*'''A''' {{Abbr/Bo3}}\n== Prize Pool ==\n$1")
        assert body is not None
        assert "Prize Pool" not in body


class TestStageClassification:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Group Stage", StageType.GROUP),
            ("Standings", StageType.GROUP),
            ("Swiss Stage", StageType.SWISS),
            ("Playoffs", StageType.PLAYOFF),
            ("Main Event", StageType.PLAYOFF),
            ("Double-elimination bracket", StageType.PLAYOFF),
        ],
    )
    def test_recognises_stage_kinds(self, name: str, expected: StageType) -> None:
        assert classify_stage(name) is expected

    @pytest.mark.parametrize("name", ["Participants", "Prize Pool", "Broadcast Talent"])
    def test_rejects_non_stages(self, name: str) -> None:
        assert classify_stage(name) is None


class TestUnknownShapes:
    def test_stage_without_a_format_marker_is_dropped(self) -> None:
        """No marker means no knowledge. A default here would end up in the training data
        as if it were fact (spec section 5.5)."""
        stages = parse_stage_formats("== Format ==\n*'''Group Stage'''\n**Round robin\n")
        assert stages == []

    def test_empty_wikitext_is_not_an_error(self) -> None:
        assert parse_stage_formats("") == []


class TestTheInternational2026:
    """The Format section written in HTML instead of wiki lists.

    Liquipedia explains the choice on the page: "HTML is used instead of MediaWiki lists due
    to a bug where mw-collapsible sticks the content in a separate div right below, which
    splits the list. Use MediaWiki lists for anything that is not as complicated as this."

    So the pages that most need parsing are the ones written in the shape the parser could
    not read - and it stayed invisible, because a page with no readable stages looks exactly
    like a page with no stages. The International 2026 is a mapped Tier 1 league whose 58
    series all sat without a format while its Format section plainly held {{Abbr/Bo3}} and
    {{Abbr/Bo5}}.
    """

    def stages(self) -> list:
        return parse_stage_formats(fixture("the_international_2026"), fallback_year=2026)

    def test_the_stages_are_found_at_all(self) -> None:
        assert self.stages()

    def test_the_group_stage_is_bo3(self) -> None:
        assert stage_named(self.stages(), "group").default_format is SeriesFormat.BO3

    def test_the_group_stage_is_classified_as_one(self) -> None:
        assert stage_named(self.stages(), "group").stage_type is StageType.GROUP

    def test_the_main_event_defaults_to_bo3_and_flags_the_bo5_final(self) -> None:
        """ "Grand Final is Bo5, all other matches are Bo3" - the default is the rule, not the
        exception, and the disagreement is surfaced rather than silently resolved."""
        playoff = stage_named(self.stages(), "main event")

        assert playoff.default_format is SeriesFormat.BO3
        assert SeriesFormat.BO5 in playoff.formats_seen

    def test_the_collapsible_rules_do_not_become_stages(self) -> None:
        """The swiss pairing criteria are a nested ordered list inside the group stage. They
        carry no format marker, so they must not turn into stages of their own."""
        names = [s.name.lower() for s in self.stages()]

        assert not any("matches won" in name or "criteria" in name for name in names)


class TestHtmlLists:
    def test_a_wiki_list_is_left_alone(self) -> None:
        """Pages that were already readable must take exactly the path they always did."""
        body = "== Format ==\n*'''A''' {{Abbr/Bo3}}\n**All matches are {{Abbr/Bo3}}"

        assert (
            extract_format_section(body)
            == "\n*'''A''' {{Abbr/Bo3}}\n**All matches are {{Abbr/Bo3}}"
        )

    def test_nesting_becomes_bullet_depth(self) -> None:
        body = (
            "== Format ==\n<ul><li>'''Group Stage'''\n"
            "<ul><li>All matches are {{Abbr/Bo3}}</li></ul></li></ul>"
        )

        assert extract_format_section(body) == "*'''Group Stage'''\n**All matches are {{Abbr/Bo3}}"

    def test_html_comments_are_dropped(self) -> None:
        body = "== Format ==\n<!-- why HTML -->\n<ul><li>'''A''' {{Abbr/Bo5}}</li></ul>"

        assert extract_format_section(body) == "*'''A''' {{Abbr/Bo5}}"

    def test_an_empty_item_produces_no_bullet(self) -> None:
        body = "== Format ==\n<ul><li></li><li>'''A''' {{Abbr/Bo1}}</li></ul>"

        assert extract_format_section(body) == "*'''A''' {{Abbr/Bo1}}"

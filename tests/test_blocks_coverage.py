"""blocks.json and the page-coverage check."""

import unittest

from generator import blocks, coverage


class TestBlockMarks(unittest.TestCase):
    def test_start_and_end_lines_are_found_in_order(self):
        lines = [
            "Prose about Fomoraij.",
            "YAAROWSLOW, THE MANY",
            "Solitary, large",
            "> Regenerate wounds",
            "Prose after.",
        ]
        listed = [
            {
                "kind": "stat-block",
                "name": "Yaarowslow, the Many",
                "start": "YAAROWSLOW, THE MANY",
                "end": "> Regenerate wounds",
            }
        ]
        starts, ends = blocks.line_marks(listed, lines)
        self.assertEqual(list(starts), [1])
        self.assertEqual(list(ends), [3])

    def test_a_start_the_corpus_lost_is_reported(self):
        listed = [{"kind": "stat-block", "name": "Gone", "start": "NO SUCH LINE"}]
        self.assertEqual(
            blocks.missing_starts(listed, ["something else"]), ["NO SUCH LINE"]
        )

    def test_drift_names_what_is_missing_and_what_is_new(self):
        listed = [{"kind": "stat-block", "name": "Crinwin"}]
        found = [{"kind": "stat-block", "name": "Swarm"}]
        gone, new = blocks.drift(listed, found)
        self.assertEqual(gone, ['stat-block "Crinwin"'])
        self.assertEqual(new, ['stat-block "Swarm"'])

    def test_found_in_html_reads_kind_and_name(self):
        html = (
            '<div class="stat-block" id="crinwin">'
            '<h3 class="stat-name">Crinwin</h3></div>'
            '<div class="roll-table" id="danger"><div class="roll-table-head">'
            '<span class="roll-label">Danger</span></div></div>'
        )
        self.assertEqual(
            blocks.found_in_html(html),
            [
                {"kind": "stat-block", "name": "Crinwin", "id": "crinwin"},
                {"kind": "roll-table", "name": "Danger", "id": "danger"},
            ],
        )


class TestCoverage(unittest.TestCase):
    def test_a_line_the_page_never_shows_is_reported(self):
        lines = ["The swarm engulfs a victim.", "Dropped on the floor here."]
        html = "<p>The swarm engulfs a victim.</p>"
        self.assertEqual(
            coverage.unshown_lines(lines, html), ["Dropped on the floor here."]
        )

    def test_rewrapping_recasing_and_page_refs_are_not_losses(self):
        lines = [
            "a line the renderer re-wrapped and re-cased",
            "Regenerate wounds from anything but orichalcum (page 162)",
        ]
        html = (
            "<p>A line the renderer <em>re-wrapped</em>\n and re-cased</p>"
            "<li>Regenerate wounds from anything but "
            '<a href="x.html">orichalcum</a></li>'
        )
        self.assertEqual(coverage.unshown_lines(lines, html), [])

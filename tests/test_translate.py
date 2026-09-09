"""Sheets and corpus translations: the marker round trip, the work file, the
text memory (``generator/sheet.py``, ``generator/translate.py``)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generator import corpus  # noqa: E402
from generator.sheet import render_sheet  # noqa: E402
from generator.text import B_OFF, B_ON, M_SEP  # noqa: E402
from generator.translate import (  # noqa: E402
    TextMemory,
    check_alignment,
    coverage,
    text_field_indexes,
)

SHEET = """# a sheet
SHEET\tfollower\tfollower-1\tFollower 1
H2\tFollower 1
FIELD\tf1-name\tName\tname
STAT\tf1-armor\tArmor\t0\t0\t9
LIST\tfs-lines
CK\tf1-tag0\t<i>group</i>
ENDLIST
P\tSee [[npcs-followers|NPCs & Followers]].
ENDSHEET
"""

SHEET_DE = SHEET.replace("Follower 1", "Gefolgsmann 1").replace(
    "\tName\tname", "\tName\tName"
).replace("Armor", "Rüstung").replace("<i>group</i>", "<i>Gruppe</i>").replace(
    "See [[npcs-followers|NPCs & Followers]].", "Siehe [[npcs-followers|NSCs & Gefolge]]."
)


def plain_link(text: str) -> str:
    return text.replace(B_ON, "<strong>").replace(B_OFF, "</strong>").replace(
        "\x06", "<em>"
    ).replace("\x07", "</em>")


class SheetTest(unittest.TestCase):
    def test_round_trip_and_render(self):
        lines, pages = corpus.parse_text(SHEET, "sheet")
        back = corpus.dump_text(lines, pages)
        self.assertEqual(corpus.parse_text(back, "sheet"), (lines, pages))
        html = render_sheet(lines, "followers", plain_link)
        self.assertIn('data-sheet="follower" data-hp-key="follower-1"', html)
        self.assertIn('<h2 id="follower-1">Follower 1</h2>', html)
        self.assertIn('data-field-key="followers:f1-name"', html)
        self.assertIn('data-spin-min="0" data-spin-max="9"', html)
        self.assertIn('data-check-id="f1-tag0"', html)
        self.assertIn('<a class="wiki-link" href="npcs-followers.html" data-slug="npcs-followers">', html)

    def test_translation_keeps_english_ids(self):
        en, _ = corpus.parse_text(SHEET, "en")
        de, _ = corpus.parse_text(SHEET_DE, "de")
        self.assertEqual(check_alignment(en, de), [])
        html = render_sheet(de, "followers", plain_link, id_lines=en)
        self.assertIn('<h2 id="follower-1">Gefolgsmann 1</h2>', html)
        self.assertIn('data-field-key="followers:f1-name"', html)
        self.assertIn("<em>Gruppe</em>", html)
        self.assertIn("NSCs &amp; Gefolge", html.replace("NSCs & Gefolge", "NSCs &amp; Gefolge"))
        done, total = coverage(en, de, sheet=True)
        self.assertEqual((done, total), (6, 6))  # SHEET label, H2, FIELD, STAT, CK, P

    def test_misaligned_translation_is_refused(self):
        en, _ = corpus.parse_text(SHEET, "en")
        bad, _ = corpus.parse_text(SHEET_DE.replace("CK\t", "P\t"), "de")
        self.assertTrue(check_alignment(en, bad))

    def test_text_fields(self):
        self.assertEqual(text_field_indexes("STAT", ["k", "Armor", "0", "0", "9"], sheet=True), [1])
        self.assertEqual(text_field_indexes("FIELD", ["k", "Name", "name"], sheet=True), [1, 2])
        self.assertEqual(text_field_indexes("VR", ["Bronze knife", "2"], sheet=False), [0])
        self.assertEqual(text_field_indexes("H2", ["4 & 5", "NPC connections"], sheet=False), [1])
        self.assertEqual(text_field_indexes("HR", [], sheet=False), [])


class TextMemoryTest(unittest.TestCase):
    def test_lookup_shapes(self):
        en = ["\x02H2 Steading Improvements", B_ON + "Requires" + B_OFF + " all of the following:", "\x02VR Bronze knife" + M_SEP + "2"]
        de = ["\x02H2 Verbesserungen der Siedlung", B_ON + "Erfordert" + B_OFF + " alles Folgende:", "\x02VR Bronzemesser" + M_SEP + "2"]
        tm = TextMemory.from_lines(en, de)
        # a heading asked for without its colon, plain
        self.assertEqual(tm.get("Steading Improvements"), "Verbesserungen der Siedlung")
        # raw query keeps sentinels, plain query loses them
        self.assertEqual(tm.get(en[1]), de[1])
        self.assertEqual(tm.get("Requires all of the following:"), "Erfordert alles Folgende:")
        # the renderer stripped the English colon: so is the translation's
        self.assertEqual(tm.get("Requires all of the following"), "Erfordert alles Folgende")
        # a value-table item, not its value
        self.assertEqual(tm.get("Bronze knife"), "Bronzemesser")
        self.assertEqual(tm.get("2"), "2")
        # a shouted label retitled by the renderer comes back retitled
        tm2 = TextMemory.from_lines(["\x02C " + B_ON + "STONE WALL" + B_OFF + " A rampart."], ["\x02C " + B_ON + "STEINMAUER" + B_OFF + " Ein Wall."])
        self.assertEqual(tm2.get("Stone Wall"), "Steinmauer")
        # the whole line was never asked for: that is a leak; the sub-segment hit is not
        self.assertEqual(tm2.unused(), [B_ON + "STONE WALL" + B_OFF + " A rampart."])


if __name__ == "__main__":
    unittest.main()

"""Round-trip tests for the on-disk corpus format (``generator/corpus.py``).

Run with ``python -m unittest`` from the repository root.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generator import corpus  # noqa: E402
from generator.text import (  # noqa: E402
    B_OFF,
    B_ON,
    I_OFF,
    I_ON,
    M_BOX,
    M_ENDBOX,
    M_FACE,
    M_H2,
    M_HR,
    M_SEP,
    M_STATS,
    M_STEP,
    M_VR,
    MARKERS,
    heading_pages,
)

SAMPLE = [
    M_H2 + "Death's Door",
    "When you are reduced to 0 HP, " + B_ON + "roll" + B_OFF + " " + I_ON + "+nothing" + I_OFF + ".",
    M_VR + "Bronze knife" + M_SEP + "2",
    M_STEP + "4 & 5" + M_SEP + "NPC connections",
    M_H2 + "",  # a heading with an empty payload must stay a heading
    M_BOX,
    "Size: small",
    M_ENDBOX,
    M_HR,
    "a literal backslash \\ and a tab\there and a <less-than",
    "",
    M_STATS + json.dumps({"str": "+1", "die": "d8"}),
    M_FACE + "back",
    B_ON + "bold" + B_OFF + I_ON + "italic" + I_OFF + " ◇ (2 uses) — “quoted”",
]
PAGES = [41, 41, 41, 42, 42, 42, 42, 42, 43, 43, 43, 44, 44, 44]


class LineRoundTrip(unittest.TestCase):
    def test_every_registered_marker_round_trips(self):
        for tag, marker in MARKERS.items():
            line = marker + ("payload" if marker.endswith(" ") else "")
            file_line = corpus.encode_line(line)
            self.assertTrue(file_line.startswith(tag), (tag, file_line))
            self.assertEqual(corpus.decode_line(file_line), line)

    def test_empty_payload_keeps_its_marker(self):
        self.assertEqual(corpus.encode_line(M_H2), "H2\t")
        self.assertEqual(corpus.decode_line("H2\t"), M_H2)

    def test_bare_marker_has_no_tab(self):
        self.assertEqual(corpus.encode_line(M_BOX), "BOX")
        self.assertEqual(corpus.decode_line("BOX"), M_BOX)
        with self.assertRaises(corpus.CorpusError):
            corpus.decode_line("BOX\tsomething")

    def test_inline_formatting_and_escapes(self):
        line = "a \\ b\tc <d> " + B_ON + "e" + B_OFF + I_ON + "f" + I_OFF
        enc = corpus.encode_line(line)
        self.assertEqual(enc, "P\ta \\\\ b\\tc \\<d> <b>e</b><i>f</i>")
        self.assertEqual(corpus.decode_line(enc), line)

    def test_bare_less_than_is_rejected(self):
        with self.assertRaises(corpus.CorpusError):
            corpus.decode_line("P\ta <b>x</b> < y")

    def test_unknown_tag_is_rejected(self):
        with self.assertRaises(corpus.CorpusError):
            corpus.decode_line("ZZ\tpayload")
        with self.assertRaises(corpus.CorpusError):
            corpus.encode_line("\x02ZZ payload")

    def test_untagged_line_is_rejected(self):
        with self.assertRaises(corpus.CorpusError):
            corpus.decode_line("just prose")


class FileRoundTrip(unittest.TestCase):
    def test_dump_and_parse(self):
        text = corpus.dump_text(SAMPLE, PAGES, "Harm & Healing · Book I")
        self.assertTrue(text.startswith("# Harm & Healing · Book I\n"))
        self.assertIn("PAGE\t41\n", text)
        self.assertIn("\nH2\tDeath's Door\n", text)
        lines, pages = corpus.parse_text(text)
        self.assertEqual(lines, SAMPLE)
        self.assertEqual(pages, PAGES)

    def test_page_lines_only_where_the_page_turns(self):
        text = corpus.dump_text(SAMPLE, PAGES)
        self.assertEqual(text.count("PAGE\t"), len(set(PAGES)))

    def test_heading_pages_follow_the_lines(self):
        self.assertEqual(
            heading_pages(SAMPLE, PAGES),
            [(41, "Death's Door"), (42, "")],
        )
        # A plaqued heading rejoins number and title with a space.
        self.assertEqual(
            heading_pages([M_H2 + "4 & 5" + M_SEP + "NPC connections"], [9]),
            [(9, "4 & 5 NPC connections")],
        )

    def test_write_and_read_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = {"id": "book1", "label": "Book I", "title": "Book I — Stonetop",
                    "filename": "x.pdf", "slug_prefix": ""}
            arts = [
                {"title": "Harm & Healing", "slug": "harm-healing", "start_page": 41,
                 "end_page": 44, "kind": "article", "book": "book1",
                 "book_label": "Book I", "toc_labels": ["Death's Door"]},
                {"title": "Maps", "slug": "maps", "start_page": 1, "end_page": 2,
                 "kind": "maps", "book": "book1", "book_label": "Book I"},
            ]
            # A stale file from an earlier extraction is swept.
            (root / "book1").mkdir()
            (root / "book1" / "renamed-away.txt").write_text("P\tx\n")
            n = corpus.write_book(root, book, 600, arts, {"harm-healing": (SAMPLE, PAGES)})
            self.assertEqual(n, 1)
            self.assertFalse((root / "book1" / "renamed-away.txt").exists())
            self.assertTrue(corpus.has_book(root, "book1"))
            manifest, arts_back, texts = corpus.read_book(root, "book1")
            self.assertEqual(manifest["page_count"], 600)
            self.assertEqual(arts_back, arts)
            self.assertEqual(texts, {"harm-healing": (SAMPLE, PAGES)})
            # The file is LF-terminated UTF-8 without a BOM.
            raw = (root / "book1" / "harm-healing.txt").read_bytes()
            self.assertNotIn(b"\r", raw)
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()

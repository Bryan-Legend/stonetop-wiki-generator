"""Did every line of the book reach the page?

The extractor and the renderer between them decide what each line of a book
becomes. When they get a layout wrong the usual symptom is not a crash but
silence: a stat block swallows the paragraph after it, a wrapped tag line is
read as a creature's name, the rest of the block is dropped, and the page
looks fine unless you happen to read it beside the PDF (which is how
Yaarowslow the Many went missing from Book II p. 142).

So the build checks itself: every line of a page's corpus should turn up in
the page it built, and whatever does not is reported. The comparison ignores
whitespace, punctuation and case, because a line is re-wrapped, re-cased and
re-joined on its way to HTML; what it cannot ignore is words that are gone.

Known rewrites are allowed for here, not silenced elsewhere:

* a page reference becomes a link and its words ("see page 168") go,
* the playbook inserts' HP box prints "Max. 6" into the middle of a line,
* Book I's anatomy of a monster numbers its example's parts with callouts.
"""

from __future__ import annotations

import html as _html
import re

from .text import (
    PAGE_NUMS,
    M_H3,
    M_VR,
    undouble_words,
    M_BAND,
    M_BOX,
    M_ENDBOX,
    M_FACE,
    M_HR,
    M_ICON,
    M_MARK,
    M_PB,
    M_STATS,
    M_TH,
    strip_markers,
)

# Markers that carry no text of their own (or carry a payload the reader
# never sees): nothing to look for in the page.
_SKIP_MARKERS = (
    M_ICON,
    M_HR,
    M_BOX,
    M_ENDBOX,
    M_MARK,
    M_FACE,
    M_TH,
    M_STATS,
    M_BAND,
    M_PB,
)

# The part of a page reference a link takes the place of: "page 438",
# "pages 106 and 107" — not the "see" or the "Book II," around it, which
# the page keeps.
# (the same numbers the renderer reads: "336 and 350", "213, 223, and 472")
_PAGE_REF = re.compile(r"\bpages?\s+" + PAGE_NUMS, re.I)
_MAX_BOX = re.compile(r"\bMax\.?\s*\d+", re.I)
_CALLOUT = re.compile(r"\s\d{1,2}$")
_TAGS = re.compile(r"<[^>]+>")
# A link that stands in for "page N" (``data-ref="page"``): its words are the
# target's name, which the book never printed there.
_PAGE_REF_LINK = re.compile(r'<a\b[^>]*\bdata-ref="page"[^>]*>.*?</a>', re.S)
# A reference the corpus broke across two lines ("See page" / "514 for more
# about improvements."): the page shows it whole, each half alone does not
# match. Strip the dangling half from each line.
_TRAILING_REF = re.compile(
    r"\(?\s*(?:see\s+)?(?:Book(?:\s+[IVX]+,?)?\s*)?(?:pages?|page\s+\d+,?)?\s*$", re.I
)
# The inserts' HP box, whose label lands in the stat line ("…(0 vs. iron) HP
# Damage bronze hatchet"): the renderer takes it out.
_REPEATED_TAIL = re.compile(r"^(.*?)\s*\b(\S.*?\S)\s+\2\s*$")
_HP_VALUE = re.compile(r"\bHP\s*\d")
_HP_BOX = re.compile(r"\s+HP(?=\s*$|\s+(?:Damage|Instinct|Special|Cost)\b)")
_LEADING_NUM = re.compile(
    r"^\s*(?:I{1,2},\s*(?:pages?\s+)?)?"
    r"(?:\d+(?:\s*(?:[-\u2013,]|and)\s*\d+)*)?\s*\)?[.,;:]?\s*"
)
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)

# Below this a line is too short to look for: "d6", "or", a lone numeral —
# its letters are bound to turn up somewhere on the page.
_MIN_LEN = 12


_REF_WORDS = re.compile(r"\b(?:see|book\s+i{1,2})\b", re.I)


def _norm(text: str) -> str:
    text = _PAGE_REF.sub(" ", text)
    text = _REF_WORDS.sub(" ", text)
    text = _MAX_BOX.sub(" ", text)
    text = _CALLOUT.sub("", text.strip())
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def page_text(body_html: str) -> str:
    """The page's words, normalised for comparison."""
    body = _SCRIPT.sub(" ", body_html)
    body = _PAGE_REF_LINK.sub(" ", body)
    return _norm(_html.unescape(_TAGS.sub(" ", body)))


def unshown_lines(lines: list[str], body_html: str, title: str = "") -> list[str]:
    """The page's corpus lines whose words are not in the page it built.

    ``title`` is the page's: a line that is a piece of it (a chapter title
    set over two lines, "Writing" / "Moves & Love Letters") is shown as the
    page's title, which is not in the body.
    """
    shown = page_text(body_html)
    title_key = _norm(title)
    missing: list[str] = []
    for line in lines:
        if line.startswith(_SKIP_MARKERS):
            continue
        text = strip_markers(line).strip()
        # An arcanum's card leaves out its "Mysteries of…" heading, which the
        # card's face already says; and a follower's HP box ("Starts at 13
        # each") is folded into its stat line as "HP 13".
        if line.startswith(M_H3) and text.startswith("Mysteries of"):
            continue
        if re.match(r"^Starts at \d+(?: each)?$", text):
            continue
        plain = _HP_BOX.sub("", text) if _HP_VALUE.search(text) else text
        key = _norm(_LEADING_NUM.sub("", _TRAILING_REF.sub("", plain)))
        # A line the extractor doubled ("A folktale folktale") is shown once.
        undoubled = _norm(_REPEATED_TAIL.sub(r"\1 \2", undouble_words(plain)))
        # A table row whose wrapped tail the renderer took back sits
        # between the row's words and its value (Hillfolk's livestock).
        if line.startswith(M_VR):
            item, _, val = line[len(M_VR):].partition("\x03")
            item_key = _norm(strip_markers(item))
            if len(item_key) >= _MIN_LEN and item_key in shown:
                continue
        if len(key) < _MIN_LEN or key in shown or undoubled in shown or (title_key and key in title_key):
            continue
        missing.append(text)
    return missing


def report(
    slug: str, lines: list[str], body_html: str, *, examples: int = 3, title: str = ""
) -> int:
    """Print what the page dropped. Returns how many lines that was."""
    missing = unshown_lines(lines, body_html, title)
    if not missing:
        return 0
    print(
        f"  WARNING: {slug}: {len(missing)} line(s) of book text are not on "
        "the page:"
    )
    for text in missing[:examples]:
        print(f"      {text[:110]}")
    if len(missing) > examples:
        print(f"      … and {len(missing) - examples} more")
    return len(missing)

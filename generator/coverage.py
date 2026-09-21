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

_PAGE_REF = re.compile(
    r"\(?\s*(?:see\s+)?(?:Book\s+[IVX]+,?\s*)?pages?\s+[\d–,\-\s]+"
    r"(?:for\s+context)?\)?",
    re.I,
)
_MAX_BOX = re.compile(r"\bMax\.?\s*\d+", re.I)
_CALLOUT = re.compile(r"\s\d{1,2}$")
_TAGS = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)

# Below this a line is too short to look for: "d6", "or", a lone numeral —
# its letters are bound to turn up somewhere on the page.
_MIN_LEN = 12


def _norm(text: str) -> str:
    text = _PAGE_REF.sub(" ", text)
    text = _MAX_BOX.sub(" ", text)
    text = _CALLOUT.sub("", text.strip())
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def page_text(body_html: str) -> str:
    """The page's words, normalised for comparison."""
    body = _SCRIPT.sub(" ", body_html)
    return _norm(_html.unescape(_TAGS.sub(" ", body)))


def unshown_lines(lines: list[str], body_html: str) -> list[str]:
    """The page's corpus lines whose words are not in the page it built."""
    shown = page_text(body_html)
    missing: list[str] = []
    for line in lines:
        if line.startswith(_SKIP_MARKERS):
            continue
        text = strip_markers(line).strip()
        key = _norm(text)
        if len(key) < _MIN_LEN or key in shown:
            continue
        missing.append(text)
    return missing


def report(slug: str, lines: list[str], body_html: str, *, examples: int = 3) -> int:
    """Print what the page dropped. Returns how many lines that was."""
    missing = unshown_lines(lines, body_html)
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

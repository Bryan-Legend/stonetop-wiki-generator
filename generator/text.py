"""
Shared text layer: marker constants, inline-format sentinels, line classifiers, and title/slug helpers.

Everything here is pure string work — no PDF, no HTML shell — so both the
extractor (PDF → marker lines) and the structurer (marker lines → HTML) import
from it and neither imports the other.
"""

from __future__ import annotations

import html
import re

# Monster/creature tag words commonly used in Stonetop stat blocks
ORG_TAGS = {"horde", "group", "solitary"}
TAG_WORDS = ORG_TAGS | {
    "small",
    "large",
    "huge",
    "tiny",
    "hoarder",
    "cautious",
    "stealthy",
    "terrifying",
    "planar",
    "construct",
    "devious",
    "intelligent",
    "magical",
    "organized",
    "amorphous",
    "clever",
    "mindless",
    "divine",
    "legendary",
    "brutal",
    "fearless",
    "drunkard",
    "hardy",
    "craven",
    "meek",
    "opportunistic",
    "terrifying",
    "deceptive",
    "hoarder",
    "cautious",
    "stealthy",
    "planar",
    "construct",
    "arcane",
    "divine",
    "undead",
    "spirit",
    "elemental",
    "ancient",
    "wretched",
    "violent",
    "corrupted",
    "emanation",
}

# d6, 2d10, d10+3, 1d4-1 (optional spaces around +/-; avoid eating 1d4-1d6 ranges)
DICE_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(\d{0,2}d(?:4|6|8|10|12|20|100)"
    r"(?:\s*[+\-–—]\s*\d{1,3}(?![dD\d]))?)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
# (page 12), (pages 8-11), (page 282, 350), (see page 39)
PAGE_REF_RE = re.compile(
    r"\((?:see\s+)?pages?\s+([\d,\s\-–—]+)\)",
    re.IGNORECASE,
)
BARE_PAGE_RE = re.compile(
    r"(?<![\w/])(?:see\s+)?pages?\s+([\d,\s\-–—]+)(?![\w/])",
    re.IGNORECASE,
)
ROLL_HEADER_RE = re.compile(
    r"^(\d{0,2}d(?:4|6|8|10|12|20))\s+(.+)$", re.IGNORECASE
)
ROLL_HEADER_DICE_ONLY = re.compile(r"^(\d{0,2}d(?:4|6|8|10|12|20))$", re.IGNORECASE)
# "size 1d6" / reverse dice-label form (must end with a die expression)
ROLL_HEADER_REV_RE = re.compile(
    r"^(.{1,40}?)\s+(\d{0,2}d(?:4|6|8|10|12|20))$", re.IGNORECASE
)
ENTRY_RE = re.compile(
    r"^(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?\s+(.+)$"
)
HP_LINE_RE = re.compile(r"\bHP\s*\d+", re.IGNORECASE)
# Trade/value tables: "goods value", "weapons armor value", "services value", etc.
VALUE_HEADER_RE = re.compile(
    r"^(?:"
    r"weapons?\s*(?:&\s*|and\s+)?armor|"
    r"goods|"
    r"coin|"
    r"food\s*(?:&\s*|and\s+)?lodging|"
    r"services"
    r")\s*value$",
    re.IGNORECASE,
)
# Item ending with a price: "Wheelbarrow 1", "... free", "... +2"
# Optional trailing parenthetical: "… iron 2 (immobile)"
VALUE_ROW_RE = re.compile(
    r"^(.+?)\s+(\d{1,2}|\+\d{1,2}|free)(?:\s+(\([^)]*\)))?\s*$",
    re.IGNORECASE,
)
LONE_VALUE_RE = re.compile(r"^(\d{1,2}|\+\d{1,2}|free)$", re.IGNORECASE)


def parse_value_row(line: str) -> tuple[str, str] | None:
    """Parse a trade/services line into (item, value) or None."""
    L = line.strip()
    if not L or looks_like_value_header(L):
        return None
    if L.lower().startswith(("on a ", "when ", "pick ", "roll ", "if the ")):
        return None

    # "… 0 for a week" / "… 1 or dangerous…" / "… 2 your own" / "… 3 trade opportunities"
    m = re.match(
        r"^(.+?)\s+(\d{1,2}|\+\d{1,2}|free)\s+"
        r"(for|or|your|trade)\b(.*)$",
        L,
        re.I,
    )
    if m:
        tail = (m.group(3) + m.group(4)).strip()
        item = f"{m.group(1).strip()} {tail}".strip()
        item = re.sub(r"\s*trade opportunities\s*$", "", item, flags=re.I).strip()
        return item, m.group(2)

    m = VALUE_ROW_RE.match(L)
    if m:
        item = m.group(1).strip()
        val = m.group(2)
        paren = m.group(3) or ""
        # Reject if the only digit was inside an earlier paren fragment
        # e.g. bad split of "(steel, 1 piercing) 1" is OK; item won't end with ','
        if item.endswith((",", "(", "/")):
            return None
        if paren:
            item = f"{item} {paren}".strip()
        return item, val

    return None


def normalize_text(s: str) -> str:
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    # Keep en/em dash distinct from ASCII hyphen so wrap logic does not
    # treat "came—" + "when" as a soft-hyphenated word ("camewhen").
    s = s.replace("\u2013", "–").replace("\u2014", "—")
    s = s.replace("\u00ad", "")  # soft hyphen
    s = s.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "")
    # Object replacement char: the PDF's stand-in for an inline image (the
    # threat-type icons), never text. Left in, it renders as an OBJ tofu.
    s = s.replace("\ufffc", "")
    s = s.replace("ä", "•")  # PDF dingbat often extracted as ä
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def undouble_words(text: str) -> str:
    """'The The Ruined Ruined Tower Tower' → 'The Ruined Tower'."""
    words = text.split()
    out: list[str] = []
    i = 0
    while i < len(words):
        if i + 1 < len(words) and words[i].lower() == words[i + 1].lower():
            out.append(words[i])
            i += 2
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def is_fully_pairwise_doubled(line: str) -> bool:
    words = line.split()
    if len(words) < 2 or len(words) % 2 != 0:
        return False
    return all(
        words[i].lower() == words[i + 1].lower() for i in range(0, len(words), 2)
    )


# ---------------------------------------------------------------------------
# Rich (span/drawing-aware) extraction
#
# The book marks structure with vector art and special fonts that plain text
# extraction loses: spiral bullets (two kinds — questions get a tail flourish),
# open-square checkboxes, outline diamonds for inventory slots/uses, Fell Type
# small-caps headers on trade/value tables, Avara-Bold headings, horizontal rules, category icons, and a ruled
# box around each chapter's steading summary. extract_article_lines()
# re-reads all of that and emits marker-prefixed lines that structure_html
# understands. Markers use \x02 so they can never collide with body text.
# ---------------------------------------------------------------------------
M_B = "\x02B "        # spiral bullet item
M_B2 = "\x02B2 "      # nested (tier-2) bullet under a People-style entry
M_Q = "\x02Q "        # question-spiral bullet item
M_BC = "\x02BC "      # further paragraph of the list item above it
# Marks a checkbox item while a hazard card's body is being gathered.
CHECK_PART = "\x01C"
M_E = "\x02E "        # ellipsis ("...") list item
M_C = "\x02C "        # checkbox item
M_H2 = "\x02H2 "      # Avara-Bold section heading
M_H3 = "\x02H3 "      # Avara-Bold sub-heading (or creature name)
M_H4 = "\x02H4 "      # bold label inside the chapter info box
M_TH = "\x02TH "      # Fell Type header with no value column
M_VT = "\x02VT "      # value table start (payload: title)
M_VR = "\x02VR "      # value table row (payload: item \x03 value)
M_VA = "\x02VA "      # append fragment to previous table's last row
M_VF = "\x02VF "      # value table footnote
M_BOX = "\x02BOX"     # chapter info box start
M_ENDBOX = "\x02ENDBOX"
M_MARK = "\x02MARK "  # progress-mark track (payload: count of marks)
M_HR = "\x02HR"       # horizontal rule (column separator line)
M_ICON = "\x02ICON "  # category icon (payload: rel path under images/)
# Playbook character sheets (Book I pp. 105-140). The nine sheets are set to
# one rigid template, and these carry the parts of it that plain flow loses.
M_C2 = "\x02C2 "      # checkbox item indented under the move above it
M_CX = "\x02CX "      # checkbox printed already ticked (a starting move)
M_CX2 = "\x02CX2 "    # ...indented as well
M_STATS = "\x02STATS "  # the stat block (payload: JSON)
M_WRITE = "\x02WRITE "  # full-measure write-in box (payload: printed label)
M_STEP = "\x02STEP "  # numbered step of a walkthrough (payload: n \x03 text)
M_BAND = "\x02BAND"   # full-measure rule: end of a banded region
# A major arcanum's two pages: which face of the card the lines below are.
M_FACE = "\x02FACE "  # payload: "front" | "back"

# Sheet markers: the hand-authored sheets under pages/ (the follower inserts,
# the inventory, the steading playbook) are written in the corpus format with
# these, rendered by generator/sheet.py. The extractor never emits them. A
# payload's sub-fields are separated by M_SEP (a tab on disk); which of them
# are translatable text is recorded in generator/translate.py.
M_SHEET = "\x02SHEET "        # kind [hp-key label]: opens a .follower-sheet
M_ENDSHEET = "\x02ENDSHEET"
M_DIV = "\x02DIV "            # class [id]: a plain wrapper
M_ENDDIV = "\x02ENDDIV"
M_FIELD = "\x02FIELD "        # key label placeholder: one write-in line
M_FIELDS = "\x02FIELDS "      # class label key-prefix count placeholder: a list of write-ins
M_NOTES = "\x02NOTES "        # key label rows: a textarea
M_STAT = "\x02STAT "          # key label default min max [gloss aria]: a spinbox
M_DMG = "\x02DMG "            # key label default [title]: the damage box + roll button
M_HPMOUNT = "\x02HPMOUNT"     # where the HP tracker mounts
M_TRACK = "\x02TRACK "        # id label steps step-word [class]: a row of marks
M_LIST = "\x02LIST "          # classes [id]: opens a <ul>
M_ENDLIST = "\x02ENDLIST"
M_CK = "\x02CK "              # id text: a check item with a fixed id
M_CKX = "\x02CKX "            # text: a check item printed ticked and locked
M_CKW = "\x02CKW "            # id key placeholder: a check box beside a write-in
M_INV = "\x02INV "            # id slots text: an inventory line with ◇ slots
M_INVW = "\x02INVW "          # id key placeholder aria: an inventory write-in
M_ITEM = "\x02ITEM "          # key default [aria]: a write-in list line
M_PLACE = "\x02PLACE "        # letter text [key]: a lettered place of interest
M_LI = "\x02LI "              # text: a plain list item
M_TYPE = "\x02TYPE "          # check-id name key-other examples: a follower type's head
M_STATLINE = "\x02STATLINE "  # text: a type's stat line
M_STATBLOCK = "\x02STATBLOCK "  # name tags line...: a follower stat block
M_TABLE = "\x02TABLE "        # key-prefix rows: opens a write-in table
M_COL = "\x02COL "            # key-suffix header: one column of it
M_ROW = "\x02ROW "            # default per column: a pre-filled row
M_ENDTABLE = "\x02ENDTABLE"
M_NAMEHEAD = "\x02NAMEHEAD "  # check-id key placeholder aria-check aria-name: a card's write-in title
M_NOTE = "\x02NOTE "          # text: a muted paragraph
M_GLOSS = "\x02GLOSS "        # text: a gloss paragraph
M_EXTRACT = "\x02EXTRACT "    # from [to blocks]: splice of the book's extraction
# Payload separator inside a marker (value-table row: item \x03 value; a
# numbered step or plaqued heading: number \x03 text).
M_SEP = "\x03"

# Every marker by its tag, the text between the \x02 and the payload. This is
# the registry the on-disk corpus (``corpus.py``) is written and read with:
# a marker that ends in a space carries a payload, one that does not is bare.
MARKERS: dict[str, str] = {
    m[1:].strip(): m
    for m in (
        M_B, M_B2, M_Q, M_BC, M_E, M_C, M_H2, M_H3, M_H4, M_TH, M_VT, M_VR,
        M_VA, M_VF, M_BOX, M_ENDBOX, M_MARK, M_HR, M_ICON, M_C2, M_CX, M_CX2,
        M_STATS, M_WRITE, M_STEP, M_BAND, M_FACE,
        M_SHEET, M_ENDSHEET, M_DIV, M_ENDDIV, M_FIELD, M_FIELDS, M_NOTES,
        M_STAT, M_DMG, M_HPMOUNT, M_TRACK, M_LIST, M_ENDLIST, M_CK, M_CKX,
        M_CKW, M_INV, M_INVW, M_ITEM, M_PLACE, M_LI, M_TYPE, M_STATLINE,
        M_STATBLOCK, M_TABLE, M_COL, M_ROW, M_ENDTABLE, M_NAMEHEAD, M_NOTE, M_GLOSS,
        M_EXTRACT,
    )
}
SHEET_MARKERS = frozenset(
    m[1:].strip()
    for m in (
        M_SHEET, M_ENDSHEET, M_DIV, M_ENDDIV, M_FIELD, M_FIELDS, M_NOTES,
        M_STAT, M_DMG, M_HPMOUNT, M_TRACK, M_LIST, M_ENDLIST, M_CK, M_CKX,
        M_CKW, M_INV, M_INVW, M_ITEM, M_PLACE, M_LI, M_TYPE, M_STATLINE,
        M_STATBLOCK, M_TABLE, M_COL, M_ROW, M_ENDTABLE, M_NAMEHEAD, M_NOTE, M_GLOSS,
        M_EXTRACT,
    )
)

MARKER_RE = re.compile(r"^\x02[A-Z0-9]+ ?")
VAL_TOKEN_RE = re.compile(
    r"^(\d{1,2}\*?|\+\d{1,2}\*?|free|\d{1,2} or \d{1,2})$", re.I
)

# Inline formatting sentinels carried through the pipeline and turned into
# <strong>/<em> only at the final linkify step. Structural analysis always
# runs on de-tokenized text (see _defmt / strip_markers).
B_ON, B_OFF = "\x04", "\x05"   # bold
I_ON, I_OFF = "\x06", "\x07"   # italic
_FMT_TOKENS = str.maketrans("", "", B_ON + B_OFF + I_ON + I_OFF)
_FMT_SET = frozenset((B_ON, B_OFF, I_ON, I_OFF))


def _split_trailing_fmt(s: str) -> tuple[str, str]:
    """Split off any trailing inline-format sentinels: (body, trailing)."""
    i = len(s)
    while i > 0 and s[i - 1] in _FMT_SET:
        i -= 1
    return s[:i], s[i:]


def _split_leading_fmt(s: str) -> tuple[str, str]:
    """Split off any leading inline-format sentinels: (leading, body)."""
    i = 0
    while i < len(s) and s[i] in _FMT_SET:
        i += 1
    return s[:i], s[i:]


def _cancel_fmt_seam(tail: str, lead: str) -> str:
    """Cancel matching OFF/ON sentinel pairs where a line-wrapped run rejoins,
    so a hyphen-split word stays a single formatted run (and one link)."""
    t, l = list(tail), list(lead)
    while t and l and (
        (t[-1] == B_OFF and l[0] == B_ON)
        or (t[-1] == I_OFF and l[0] == I_ON)
    ):
        t.pop()
        l.pop(0)
    return "".join(t) + "".join(l)


def _defmt(s: str) -> str:
    """Drop inline bold/italic sentinels (keep structural \\x02 markers)."""
    if not s:
        return ""
    return s.translate(_FMT_TOKENS)


def strip_markers(line: str) -> str:
    if not line:
        return ""
    if line.startswith("\x02"):
        line = MARKER_RE.sub("", line)
    return line.translate(_FMT_TOKENS)


def heading_pages(lines: list[str], pages: list[int]) -> list[tuple[int, str]]:
    """``(page, heading text)`` for every heading marker, in line order.

    ``pages`` runs parallel to ``lines`` (see ``extract_article_lines``). The
    text is the heading as the reader sees it — a plaqued heading's number
    and title rejoined with a space. ``build_page_section_map`` pairs these
    with the sections the structurer makes of the same headings, which is how
    an in-article "see page 18" learns which section it points at.
    """
    return [
        (pg, _defmt(strip_markers(ln)).replace(M_SEP, " "))
        for ln, pg in zip(lines, pages)
        if ln.startswith((M_H2, M_H3))
    ]


def fmt_to_html(s: str) -> str:
    """Convert inline formatting sentinels to <strong>/<em> (post-escape)."""
    if not s:
        return s
    s = (
        s.replace(B_ON, "<strong>").replace(B_OFF, "</strong>")
        .replace(I_ON, "<em>").replace(I_OFF, "</em>")
    )
    # A run split by a line-wrap ("…one </em></strong> <strong><em>waystone…")
    # renders identically to one run — collapse the seam (and its extra space).
    s = re.sub(r"\s*</em></strong>\s+<strong><em>\s*", " ", s)
    s = re.sub(r"\s*</strong>\s+<strong>\s*", " ", s)
    s = re.sub(r"\s*</em>\s+<em>\s*", " ", s)
    return s


# Leading bold run of a list/entry item: "\x04Name\x05 rest of the text"
BOLD_PREFIX_RE = re.compile(r"^\x04([^\x04\x05]*)\x05")


def split_bold_prefix(s: str) -> tuple[str, str]:
    """Return (plain bold_prefix, text with the leading bold sentinels removed)."""
    m = BOLD_PREFIX_RE.match(s or "")
    if m:
        prefix = _defmt(m.group(1))
        return prefix, prefix + s[m.end():]
    return "", s or ""


# Book I's threat chapter anchors an icon above each type's move list, but
# the placeholder exported empty (a bare U+FFFC on its own line, printing
# nothing). These stand in for it, keyed by the type as the book names it in
# "GM moves for <type>:". Beasts reuse the icon the bestiary already uses.
THREAT_TYPE_ICONS: dict[str, str] = {
    "afflictions": "affliction",
    "beasts": "beast",
    "institutions": "institution",
    "macguffins": "macguffin",
    "magical entities": "magical-entity",
    "rabble": "rabble",
    "villains": "villain",
    "wildcards": "wildcard",
}

THREAT_MOVES_RE = re.compile(r"^GM moves for\s+(.+?)\s*:\s*$", re.I)


def is_running_header(line: str, article_title: str, *, near_page_top: bool = False) -> bool:
    t = line.strip()
    if not t:
        return True
    # Pairwise-doubled running heads: "The The Ruined Ruined Tower Tower"
    if is_fully_pairwise_doubled(t):
        return True
    words = t.split()
    if len(words) >= 2 and len(words) % 2 == 0:
        half = len(words) // 2
        if [w.lower() for w in words[:half]] == [w.lower() for w in words[half:]]:
            return True
    if len(words) == 2 and words[0].lower() == words[1].lower():
        return True

    cleaned = undouble_words(t)
    at = re.sub(r"[^a-z0-9]+", "", article_title.lower())
    lt = re.sub(r"[^a-z0-9]+", "", t.lower())
    lc = re.sub(r"[^a-z0-9]+", "", cleaned.lower())
    # Exact / undoubled title as running head (any occurrence — it's never useful body)
    if at and (lt == at or lc == at or lt == at + at or lc == at + at):
        return True
    if near_page_top and at and len(lc) >= 6:
        # A truncated title fragment ("The Dread Riv") is a running head.
        if at.startswith(lc):
            return True
        # The title plus only trailing noise (a page number, a doubled word) is
        # a running head — but the title followed by real sentence text is body
        # ("The Dread River is the eastern border of the World's End").
        if lc.startswith(at) and len(lc) <= len(at) + 6:
            return True
    # Short-form running head of a longer title ("Stonetop" on
    # "The village of Stonetop" pages)
    if near_page_top and at and len(lc) >= 6 and at.endswith(lc):
        return True
    if t.lower() in {"contents", "index"}:
        return True
    return False


def looks_like_roll_header(line: str) -> bool:
    """True if line is a dice-table header (``1d6 size``, bare ``1d12``, or reverse).

    Rejects item prose that merely starts with a dice expression
    (``1d6 glass vials (fragile, Value 0), etched…``).
    """
    if not line:
        return False
    m = ROLL_HEADER_RE.match(line)
    if m:
        label = (m.group(2) or "").strip()
        # Real headers are short labels: "variety", "minor arcanum",
        # "architectural elements". Item lines carry parens, Value, or clauses.
        if len(label) > 48:
            return False
        if "(" in label or ")" in label:
            return False
        if re.search(r"\bValues?\b", label, re.I):
            return False
        if label.count(",") >= 2:
            return False
        if label.endswith((".", ";", ":")):
            return False
        return True
    if ROLL_HEADER_DICE_ONLY.match(line):
        return True
    # reverse "label 1d6" — only when not also a numbered entry
    m_rev = ROLL_HEADER_REV_RE.match(line)
    if m_rev and not ENTRY_RE.match(line):
        label = (m_rev.group(1) or "").strip()
        if len(label) <= 40 and "(" not in label:
            return True
    return False


def looks_like_inline_creature(line: str) -> bool:
    """GM-note creature line: ``Wynfor & Tiwlip (small, entranced, docile):``.

    Sites' example write-up sets these in body type, so they never arrive as
    ``M_H3``. Without this they get swallowed by the previous stat block.
    """
    m = re.match(r"^(.+?)\(([^)]+)\):\s*(.*)$", (line or "").strip())
    if not m:
        return False
    name, paren, rest = m.group(1).strip(), m.group(2), m.group(3)
    if len(name) < 2 or len(name) > 70:
        return False
    tags = [t.strip() for t in paren.split(",") if t.strip()]
    if not tags:
        return False

    def _tagok(t: str) -> bool:
        tl = t.lower()
        if any(ch.isdigit() for ch in t):
            return False
        if tl in TAG_WORDS or tl.rstrip("s") in TAG_WORDS:
            return True
        return bool(re.fullmatch(r"[a-z][a-z\-]*", tl))

    if not all(_tagok(t) for t in tags):
        return False
    rs = rest.strip()
    if not rs:
        return True
    return bool(re.match(r"^(HP|Instinct|Damage|Armor|Notes)\b", rs, re.I))


def looks_like_heading(line: str) -> bool:
    if not line or len(line) > 55:
        return False
    # Running-header garbage should never become an <h2>
    if is_fully_pairwise_doubled(line):
        return False
    if line.endswith((".", ",", ";", ":")) and not line.endswith(":"):
        return False
    # Sentence ending inside closing quotes ('…or "Go-Between."')
    if line.rstrip("\"'”’").endswith((".", ",", ";")):
        return False
    if ENTRY_RE.match(line):
        return False
    if HP_LINE_RE.search(line):
        return False
    if line.startswith(("•", "-", "–")):
        return False
    if looks_like_roll_header(line):
        return False
    if looks_like_value_header(line) or VALUE_ROW_RE.match(line):
        return False
    if LONE_VALUE_RE.match(line):
        return False
    # Table-row wrap fragments: "Value 2)" after a cut parenthetical
    if re.match(r"^Values?\s*\d+\s*\)?\.?$", line.strip(), re.I):
        return False
    # Closing paren-only fragments / short wrap crumbs
    if re.match(r"^[\d\s,;:.)\]]+$", line.strip()):
        return False
    # Never treat cross-refs as headings — they must become links
    if re.search(r"\(pages?\s+[\d,\s\-–—]+\)", line, re.I):
        return False
    if re.search(r"\bpages?\s+\d", line, re.I):
        return False
    # Mostly Title Case / short label
    words = line.split()
    if len(words) > 8:
        return False
    # Common section words or short phrases without lowercase filler-only
    if line[0].isupper() or line[0].isdigit():
        # Small words (and/of/the…) don't make a title look like a sentence
        small = {
            "a", "an", "the", "and", "or", "of", "to", "in", "for", "from",
            "vs", "vs.", "on", "with", "by", "as", "at",
        }
        lowerish = sum(
            1
            for w in words
            if w and w[0].islower() and w.lower().strip(".,;:") not in small
        )
        if lowerish >= 3:
            return False
        return True
    return False


def parse_page_nums(spec: str) -> list[int]:
    """Parse '12', '8-11', '282, 350', '282, 350-352' into page numbers (range ends only for ranges)."""
    nums: list[int] = []
    for part in re.split(r"[,;]", spec):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            # link to start of range only (avoid huge lists)
            nums.append(a)
            if b != a:
                nums.append(b)
        elif part.isdigit():
            nums.append(int(part))
    # unique preserve order
    seen = set()
    out = []
    for n in nums:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def looks_like_tag_line(line: str) -> bool:
    """Horde, small, stealthy, … or Solitary, brutal, fearless, drunkard"""
    if not line or len(line) > 160:
        return False
    if HP_LINE_RE.search(line) or line.lower().startswith("damage"):
        return False
    parts = [p.strip() for p in re.split(r"[,;]", line) if p.strip()]
    if not parts:
        return False
    first = parts[0].lower().split()[0] if parts[0] else ""
    # Primary org type is enough (e.g. "Horde, hardy" or even "Solitary")
    # — but only when the line reads like a tag list, not prose that happens
    # to open on an org word ("group to answer during play. When you…").
    if first in ORG_TAGS:
        if len(parts) > 1:
            return True
        words = parts[0].split()
        if len(words) <= 3 and not re.search(r"[.!?]", parts[0]):
            return True
    hits = 0
    for p in parts:
        tok = p.lower().split()
        if not tok:
            continue
        if tok[0] in TAG_WORDS or any(t in TAG_WORDS for t in tok):
            hits += 1
    # Leading non-org adjectives (ghostly, Forge Lord, …) still count as a
    # tag line when enough known tags follow.
    return hits >= 2


def looks_like_value_header(line: str) -> bool:
    t = re.sub(r"\s+", " ", line.strip().lower().replace("&", " "))
    if not t.endswith(" value"):
        return False
    head = t[: -len(" value")].strip()
    # Accept known price-table headers
    if head in {
        "goods",
        "coin",
        "services",
        "weapons",
        "weapon",
        "armor",
        "weapons armor",
        "weapon armor",
        "weapons and armor",
        "food",
        "food lodging",
        "food and lodging",
        "lodging",
    }:
        return True
    if VALUE_HEADER_RE.match(line.strip()):
        return True
    return False


def should_join(a: str, b: str) -> bool:
    # Analyze on de-tokenized text (keeps \x02 markers, drops inline
    # bold/italic sentinels); merge_wrapped_lines concatenates the originals.
    raw_a, raw_b = a, b
    # Item tag lines are italic in the PDF — check BEFORE stripping sentinels.
    if _is_item_tag_line(a) or _is_item_tag_line(b):
        # Allow joining two consecutive tag wrap lines (", close, thrown," +
        # "1 piercing") but never tags + following prose.
        if _is_item_tag_line(a) and (
            _is_item_tag_line(b)
            or _is_pure_arcana_tag_line(strip_markers(_defmt(b)))
        ):
            return True
        # Prose left dangling on a connector carries on into the next line,
        # even when that line is italic enough to read as a tag list
        # ("…on how to play or run it, see" + "Book I: Stonetop.", or the
        # read-aloud's "The goal is to" + "make the game enjoyable, …").
        if not _is_item_tag_line(a) and re.search(
            r"\b(?:see|of|and|or|the|to|a|an|with|for|that|than|but|in|on|at|by)"
            r"\s*$",
            _defmt(a),
            re.I,
        ):
            return True
        # Prose that *lists* tags rather than tagging an item closes with the
        # list itself ("*thrown*, *warm*," + "etc. are fictional cues").
        if _is_item_tag_line(a) and re.match(r"^etc\.", _defmt(b), re.I):
            return True
        return False
    a = _defmt(a)
    b = _defmt(b)
    # Structural marker lines are atomic — never merge into or out of them
    # (bullet items may still absorb plain continuations; see the a-side rule)
    if b.startswith("\x02"):
        return False
    if a.startswith("\x02"):
        if a[:3] in (M_B, M_Q, M_E, M_C) or a.startswith((M_B2, M_BC)):
            a = strip_markers(a)
        else:
            return False
    # Never glue pure arcana tag lines to/from neighbors
    # ("A giant's dormitory" + "magical" + "In a ruin…")
    if _is_pure_arcana_tag_line(a) or _is_pure_arcana_tag_line(b):
        # Two pure tag fragments may still wrap ("beautiful," + "1 piercing")
        if _is_pure_arcana_tag_line(a) and _is_pure_arcana_tag_line(b):
            return True
        return False
    if not a or not b:
        return False
    # Steading requirement group-headers start their own line, never a wrap
    if re.match(r"^(Requires?\b|And then\b|And (?:either|one|any)\b)", b, re.I):
        return False
    # A stat-block move followed by capitalized prose is flavor text, not a
    # wrap ("Throw a tantrum…" + "Misshapen brutes with sagging flesh…")
    if a.startswith("• ") and b[0:1].isupper() and not a.endswith((",", ";", ":", "-", "—")):
        m_move = re.search(r"([A-Za-z']+)\W*$", a)
        if not m_move or m_move.group(1).lower() not in {
            "the", "a", "an", "of", "and", "or", "to", "in", "on", "at",
            "for", "by", "with", "from", "their", "its",
        }:
            return False
    # Instinct lines are short; capitalized follow-ons are new flavor prose
    if re.match(r"^Instinct\b", a, re.I) and b[0:1].isupper():
        return False
    # Name lists and similar wraps: "…, Owan, Ragan," + "Renan, Seadha, …"
    if (
        a.endswith(",")
        and b[0:1].isupper()
        and "," in b
        and not looks_like_value_header(b)
        and not looks_like_tag_line(b)
        and not _is_all_caps_label(b)
    ):
        return True
    # Split mid page-ref BEFORE heading heuristics — otherwise
    # "… (page" + "436), especially…" is rejected as a short "heading".
    if re.search(r"\((?:see\s+)?pages?\s*$", a, re.I) and re.match(r"^\d", b):
        return True
    if re.search(r"\((?:see\s+)?pages?\s+\d*$", a, re.I) and re.match(
        r"^[\d,\s\-–—)]", b
    ):
        return True
    # Separate list/paragraph entries that each carry their own page ref:
    # "…near the Dread River" + "Spirits of the wild (page 356)…"
    # Exception: a proper name split across the break, where the tail line is
    # capitalized word(s) then a page ref ("…up in the Huffel" + "Peaks
    # (page 236)…") — that's one wrapped sentence, so let it join.
    name_wrap = bool(
        re.search(r"\b[A-Z][A-Za-z'’\-]*$", a)
        and re.match(
            r"^[A-Z][A-Za-z'’\-]*(?:\s+[A-Z][A-Za-z'’\-]*){0,2}\s+"
            r"\((?:see\s+)?pages?\s+\d",
            b,
        )
    )
    if (
        # …and a's own ref closes it out, so a reads as a finished entry
        # rather than a sentence still in flight ("…up from the Flats
        # (page 126), bringing the last of the Tempest" + "Lords low…")
        re.search(
            r"\(pages?\s+[\d,\s\-–—]+\)[.,\s]*$", a, re.I
        )
        and re.search(r"\(pages?\s+[\d,\s\-–—]+\)", b, re.I)
        and b[0:1].isupper()
        and not a.endswith((",", ";", ":", "—", "-"))
        and not name_wrap
        and not re.search(
            r"\b(?:the|a|an|of|to|by|and|or|for|with|into|from)\s*$", a, re.I
        )
    ):
        return False
    if is_running_header(b, ""):
        return False
    # A line ending on a dangling connector word (article/preposition/
    # conjunction) is almost always mid-sentence, so join even when the next
    # line looks like a short heading ("…en route to" + "Gordin's Delve").
    if (
        re.search(
            r"\b(?:the|a|an|of|to|and|or|for|with|into|from|in|on|at|by)\s*$",
            a,
            re.I,
        )
        and not ENTRY_RE.match(b)
        and not ROLL_HEADER_RE.match(b)
        and not ROLL_HEADER_DICE_ONLY.match(b)
        and not looks_like_tag_line(b)
        and not HP_LINE_RE.search(b)
        and not b.startswith(("•", "·"))
    ):
        return True
    # A set-off italic passage doesn't run on into the roman prose beneath it
    # — the crinwin's song ends at "gray of skin", it doesn't carry into
    # "They've always been there, in the Wood". A lowercase follow-on is
    # still a wrap of the same sentence.
    if (
        b[0:1].isupper()
        and italic_coverage(raw_a) >= 0.9
        and italic_coverage(raw_b) < 0.5
    ):
        return False
    # A proper name split across the break. A capitalized word introduced by
    # "the" ("…the Highway crosses the West" + "Road a few miles from town"),
    # or a possessive ("…between Stonetop and Gordin’s" + "Delve. The
    # Highway…") — neither ends a sentence, so the wrap carries on however the
    # next line's capital reads.
    if (
        b[0:1].isupper()
        and not a.endswith((".", "!", "?", ":", ";"))
        and (
            re.search(r"\b[Tt]he\s+[A-Z][A-Za-z'’\-]*$", a)
            or re.search(r"[A-Za-z][’']s$", a)
        )
    ):
        return True
    # "…, lose" + "1d4 HP:" — a dice amount of a stat is a sentence wrapping,
    # not a table called "HP" or a heading. The line above is mid-sentence.
    if re.match(
        r"^\d{0,2}d\d+\s+(HP|hit points|damage|armor)\b", b, re.I
    ) and not re.search(r"[.:!?)]\s*$", a):
        return True
    if looks_like_heading(b) and len(b) < 40:
        return False
    if ROLL_HEADER_RE.match(b) or ROLL_HEADER_DICE_ONLY.match(b):
        return False
    m_entry = ENTRY_RE.match(b)
    if m_entry:
        # A sentence wrapping onto its own number — "…ask the GM" + "3
        # questions about the wider world" — is prose, not a table row: the
        # line above stops on a bare word and the "entry" opens lowercase.
        if (
            re.search(r"[A-Za-z]$", a)
            and (m_entry.group(3) or "")[:1].islower()
            and not looks_like_heading(a)
            and not ENTRY_RE.match(a)  # a table row never swallows the next
        ):
            return True
        return False
    # Unclosed parenthesis: "Wynfor & Tiwlip (small, entranced," + "docile): HP 3"
    if a.count("(") > a.count(")") and a.endswith((",", "-")):
        return True
    if looks_like_tag_line(b) or HP_LINE_RE.search(b):
        return False
    if b.startswith(("•", "·")):
        return False
    if b.startswith(("When you", "When ", "If you", "On a ", "Choose", "Pick ")):
        # new mechanical paragraph — only join if a clearly mid-word
        if not a.endswith("-"):
            return False
    # Capitalized intro lines ending in a colon start their own paragraph
    # ("For disposition, choose or have someone roll:")
    if b.endswith(":") and b[0:1].isupper() and len(b) < 60 and not a.endswith("-"):
        return False
    # Never glue a price-table header onto the previous item line
    if looks_like_value_header(b):
        return False
    if re.search(
        r"\b(goods|coin|services|weapons?|food(\s+and)?\s+lodging)\s+value\s*$",
        b,
        re.I,
    ):
        return False
    # Em/en dash end of line ("came—") always continues
    if a.endswith(("–", "—")):
        return True
    # Soft hyphenated line wrap (not em/en dash)
    if a.endswith("-") and not a.endswith("--"):
        return True
    # Ends with (page N) — still often mid-sentence
    if re.search(r"\(pages?\s+\d+\)$", a, re.I):
        return True
    # Semicolons chain clauses of one sentence, and the book breaks the line
    # at them ("…then PCs won't torture anyone;" + "monsters and NPCs won't…").
    # A capitalized follow-on is a new paragraph, so require the lowercase —
    # and a bold opener means a move's next outcome ("**on a 6-**, your light
    # snuffs out"), which is its own line however the one above it ended.
    if (
        a.endswith(";")
        and b[0:1].islower()
        and not raw_b.lstrip().startswith(B_ON)
    ):
        return True
    # A colon inside a title splits the title, not the sentence: the run is
    # italic on both sides of the break ("…read the rest of this book and
    # *Book II:*" + "*The Wider World and Other Wonders*.").
    if (
        a.endswith(":")
        and raw_a.rstrip().endswith(I_OFF)
        and raw_b.lstrip().startswith(I_ON)
    ):
        return True
    # A closing quote that isn't closing a sentence carries on
    # ("More \"civilized\"" + "towns and cities lie farther to the south.").
    if a.endswith('"') and a[-2:-1] not in ".!?" and b[0:1].islower():
        return True
    # Soft wrap: previous doesn't end sentence, next continues lowercase
    if a[-1] in ".!?:;…\"":
        return False
    if a.endswith(")") and not re.search(r"\(pages?\s+\d+\)$", a, re.I):
        # closing paren mid-thought is often still a wrap
        if b[0].islower():
            return True
        return False
    if b[0].islower() or b[0] in "\"'(":
        return True
    # Damage/special lines often wrap mid-phrase with capital
    if a.endswith((",", ";")):
        return True
    # Mid-sentence wraps onto a capital word: "ways of the" + "Fen and…"
    # or "a dozen or so" + "Guards are stationed…".
    # Use last-word continuations that almost never end a sentence.
    # Intentionally exclude "with" so monster moves after "… over with" stay separate.
    cont_last = {
        "the",
        "a",
        "an",
        "of",
        "and",
        "or",
        "to",
        "for",
        "by",
        "as",
        "in",
        "on",
        "at",
        "so",  # "or so"
        "its",
        "their",
        "his",
        "her",
        "our",
        "your",
        "my",
        "this",
        "that",
        "these",
        "those",
        "some",
        "any",
        "no",
        "not",
        "but",
        "than",
        "from",
        "into",
        "onto",
        "upon",
        "between",
        "among",
        "through",
        "over",
        "under",
        "near",
        "like",
        "pair",  # "a pair"
        "dozen",  # "a dozen"
        "few",
        "many",
        "most",
        "more",
        "less",
        "such",
        "each",
        "every",
        "other",
        "another",
        "both",
        "all",
        "what",  # "has what the"
        "which",
        "who",
        "whose",
        "where",
        "when",
        "how",
        "if",
        "unless",
        "until",
        "while",
        "though",
        "although",
        "because",
        "since",
        "whether",
        "nor",
        "yet",
        "across",
        "along",
        "around",
        "behind",
        "beside",
        "beyond",
        "during",
        "inside",
        "outside",
        "toward",
        "towards",
        "via",
        "vs",
        "per",
        "plus",
        "vs.",
    }
    m_last = re.search(r"([A-Za-z']+)\W*$", a)
    last = m_last.group(1).lower() if m_last else ""
    if last in cont_last and b[0:1].isupper():
        # Don't glue onto a pure short section title (Title Case, no mid-lowercase)
        b_words = b.split()
        lowerish = sum(1 for w in b_words if w and w[0].islower())
        if looks_like_heading(b) and lowerish == 0 and len(b_words) <= 4:
            return False
        return True
    # Only join on articles/prepositions when the next line continues mid-phrase
    # (lowercase start already handled). Avoid gluing monster moves onto
    # "Instinct to … with" / "… to".
    if b[0:1].islower() and last in cont_last | {"with", "like"}:
        return True
    # Soft wrap mid multi-word proper name onto capital + continuing prose:
    # "meadows of the Huffel" + "Peaks (page 236). The petals are edible..."
    # "runes of the Green" + "Lords (page 210). Fae magic..."
    # A proper name split across the break and continuing into its page ref
    # ("…the servants of the Stone" + "Lords (page 382) rebelled…"). The first
    # line can be short enough to read as a heading, so this runs ahead of the
    # heading guard below — but only when it ends on a capitalized word that
    # follows a lowercase one, i.e. it really is running prose.
    if (
        b[0:1].isupper()
        and not looks_like_heading(b)
        and re.search(r"\b[a-z]+\s+[A-Z][A-Za-z'’\-]*$", a)
        and re.match(
            r"^[A-Z][A-Za-z'’\-]*(?:\s+[A-Z][A-Za-z'’\-]*){0,3}\s+"
            r"\((?:see\s+)?pages?\s+\d",
            b,
        )
    ):
        return True
    # (last word is not a cont_last preposition, so the rule above misses it)
    if (
        b[0:1].isupper()
        and not looks_like_heading(a)
        and not looks_like_heading(b)
    ):
        b_words = b.split()
        lowerish = sum(1 for w in b_words if w and w[0:1].islower())
        # Name continuation straight into a page ref:
        # "Lords (page 210)" / "Peaks (page 236). The petals..."
        if re.match(
            r"^[A-Z][A-Za-z'’\-]*(?:\s+[A-Z][A-Za-z'’\-]*){0,3}\s+"
            r"\((?:see\s+)?pages?\s+\d",
            b,
        ):
            return True
        # A finished-looking list line shouldn't swallow the next paragraph
        # ("…animal noises" + "Hallways are 10 feet wide…").
        if re.match(r"^[A-Z][A-Za-z]+s?\s+(?:are|is|was|were)\b", b):
            return False
        if lowerish >= 2 and len(b) >= 30:
            return True
        # Short wrap shard with a page ref and a little more prose
        # ("Lords (page 210). Fae magic (page 94)" — only one lowercase word)
        if lowerish >= 1 and re.search(r"\((?:see\s+)?pages?\s+\d", b, re.I):
            return True
    # "An (see below)" + "unnerving tableau"
    if a.lower().endswith("(see below)") or a.lower().endswith("see below)"):
        return True
    return False


def slugify_id(text: str) -> str:
    s = text.lower().replace("'", "").replace("'", "").replace("`", "")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "section"


def normalize_section_key(name: str) -> str:
    """Normalize a section/monster name for matching link text."""
    s = name.lower().strip()
    s = re.sub(r"[\"'“”‘’]", "", s)
    s = re.sub(r"^(a|an|the)\s+", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_all_caps_label(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 3 or len(line) > 55:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= 0.85


_ARCANA_TAGS = {
    "magical",
    "fragile",
    "immobile",
    "terrifying",
    "crude",
    "slow",
    "beautiful",
    "warm",
    "close",
    "reach",
    "awkward",
    "indestructible",
    "implanted",
    "large",
    "cumbersome",
    "reload",
    "near",
    "far",
    "hand",
    "messy",
    "area",
    "dangerous",
    "forceful",
    "ignorearmor",
    "ap",
    "thrown",
    "twohanded",
    "worn",
    "applied",
}


def _strip_leading_inventory_marks(s: str) -> str:
    """Drop leading inventory-slot diamonds and stray commas from a tag line."""
    return re.sub(r"^[◇\s,]+", "", (s or "").strip()).strip()


def _is_pure_arcana_tag_line(line: str) -> bool:
    """True if the whole line is one or more arcana tags (e.g. 'magical', 'fragile, immobile')."""
    L = _strip_leading_inventory_marks(line.strip().lstrip(",").strip())
    if not L or len(L) > 90:
        return False
    # Diamonds as uses mid-line: "slow (◇)" — strip for part checks
    L = L.replace("◇", "").strip()
    parts = [p.strip() for p in re.split(r"[,;]", L) if p.strip()]
    if not parts:
        return False
    for p in parts:
        pl = p.lower().strip().rstrip("?")
        if pl in _ARCANA_TAGS:
            continue
        # "Value 0", "Value 4" (gear price tag used on treasures)
        if re.match(r"^values?\s*\d+$", pl):
            continue
        # "+1 damage", "1 piercing", bare "+2"
        if re.match(
            r"^\+?\d+(\s+(damage|piercing|armor|uses|weight|readiness))?$",
            pl,
        ):
            continue
        # strip digits and recheck single tag token
        core = re.sub(r"[\d+\s]", "", pl)
        if core in _ARCANA_TAGS:
            continue
        return False
    return True


def italic_coverage(line: str) -> float:
    """Share of the line's characters that sit inside an italic run."""
    bare = strip_markers(_defmt(line)).strip()
    if not bare:
        return 0.0
    ital = sum(
        len(c)
        for c in re.findall(re.escape(I_ON) + r"([^\x07]*)" + re.escape(I_OFF), line)
    )
    return ital / len(bare)


def is_set_off_italic(line: str) -> bool:
    """Is this paragraph one of the book's set-off italic passages?

    Both books italicize whole paragraphs to hold them apart from the rules
    around them — the examples of play, the read-aloud text the GM speaks to
    the table, the odd song. They read as quotations and belong in a
    ``<blockquote>``. An italic run *inside* a sentence doesn't qualify, nor
    does an item's tag list, which is italic end to end for other reasons.
    """
    if not line or line.startswith("\x02"):
        return False
    bare = strip_markers(_defmt(line)).strip()
    if len(bare) < 60:
        return False
    if _is_item_tag_line(line) or _is_pure_arcana_tag_line(bare):
        return False
    return italic_coverage(line) >= 0.9


def _is_item_tag_line(line: str) -> bool:
    """
    Item / treasure tag line as printed in Book II.

    In the PDF these are always italic (and often lead with a stray comma and/or
    an inventory-slot diamond). Prefer italic coverage over a hard-coded word
    list; fall back to the pure-tag keyword check for de-tokenized plain lines.
    """
    if not line or line.startswith("\x02"):
        return False
    # Measure italic coverage on the raw (sentinel-bearing) line
    ital_chunks = re.findall(re.escape(I_ON) + r"([^\x07]*)" + re.escape(I_OFF), line)
    ital_chars = sum(len(c) for c in ital_chunks)
    bare = re.sub(r"[\x02-\x07]", "", line)
    bare_stripped = bare.strip()
    if not bare_stripped:
        return False
    plain = _strip_leading_inventory_marks(
        strip_markers(_defmt(line)).strip().lstrip(", ").strip()
    )
    if not plain or len(plain) > 160:
        return False
    # Ignore diamonds when measuring "is this only tags?"
    plain_nod = plain.replace("◇", "").strip(" ,")
    # Italic-dominated lines: book style for gear/monster tags
    # (diamonds are not italic; exclude them from the coverage denominator)
    bare_for_ratio = re.sub(r"[◇\s,]+", " ", bare_stripped).strip()
    ratio_den = max(len(bare_for_ratio), 1)
    # Tags are italic end to end. Half-italic lines are prose carrying an
    # italic run — a move trigger wrapping mid-sentence ("*promise they've
    # made*, gain advantage") reads as two short tags otherwise.
    if ital_chars >= max(3, int(0.85 * ratio_den)):
        # Reject italic prose sentences ("When you…", long period-ended text)
        if re.match(
            r"^(When |Whenever |The |If |Pick |For |See |Consider |Most |Some )",
            plain_nod,
            re.I,
        ):
            return False
        if plain_nod.endswith((".", "!", "…")) and len(plain_nod) > 48:
            return False
        # Book I's read-aloud text is italic end to end as well, and its
        # wrapped lines break at commas into fragments that are each tag-sized
        # ("*This game is collaborative, with each of*"). Prose gives itself
        # away where a tag list never would: quotation marks, contractions,
        # first/second-person pronouns, or a line left hanging on a connector.
        if re.search(r"[\"“”]", plain_nod):
            return False
        if re.search(r"\b\w+[’'](?:s|t|re|ve|ll|d|m)\b", plain_nod):
            return False
        if re.search(
            r"\b(?:we|us|our|you|your|i|my|they|their|them)\b", plain_nod, re.I
        ):
            return False
        if re.search(
            r"\b(?:of|and|or|the|to|a|an|with|for|that|than|but|in|on|at|by)$",
            plain_nod,
            re.I,
        ):
            return False
        parts = [p.strip() for p in re.split(r"[,;]", plain_nod) if p.strip()]
        if not parts:
            return False
        # Tag parts are short phrases ("beautiful", "1 piercing", "Value 4")
        if all(len(p) <= 40 and len(p.split()) <= 5 for p in parts):
            return True
    return _is_pure_arcana_tag_line(plain)


# Words that stay lowercase inside a title, unless they lead it.
_TITLE_MINOR = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "nor", "of", "off", "on", "onto", "or", "over", "the", "to", "up", "upon",
    "with", "vs",
    # The same job in the languages the wiki is translated into: a shouted
    # improvement name comes back retitled in every language. Only words no
    # English title would carry mid-line.
    "de", "del", "della", "dell", "delle", "dei", "degli", "di", "da", "dal",
    "dalla", "la", "le", "les", "il", "lo", "gli", "los", "las", "du", "des",
    "à", "al", "aux", "en", "y", "e", "et", "und", "der", "dem", "von", "zum",
    "zur", "im", "na", "w", "z", "i", "o", "um", "uma", "do", "dos", "ao",
    "pelo", "pela", "por", "para", "con", "com", "sur", "ou", "oder", "el",
    "agli", "alla", "nel", "nella", "sul", "sulla", "ai", "au",
}


def smart_title(text: str) -> str:
    """Title-case ``text``, lowercasing minor words.

    ``str.title()`` can't do this job: it capitalises after an apostrophe
    ("STORM'S" -> "Storm'S") and capitalises every minor word ("Arms And Armor").
    """
    if not text:
        return text

    def recase(m: "re.Match[str]") -> str:
        word = m.group(0).lower()
        if word in _TITLE_MINOR:
            return word
        return word[:1].upper() + word[1:]

    # Hyphens split words ("STORM-BRINGER" -> "Storm-Bringer"); apostrophes
    # don't. Letters are any script's — "RÉCOLTE" is one word, not "R", "É",
    # "COLTE" — and an apostrophe after a one-letter elision starts a new
    # word ("L'AUROCHS" -> "L'Aurochs"), as French and Italian set it.
    out = re.sub(r"[^\W\d_](?:[^\W\d_]|['’])*", recase, text)
    out = re.sub(r"(?<![^\W\d_])([LlDd]['’])([^\W\d_])", lambda m: m.group(1) + m.group(2).upper(), out)
    # First and last words are capitalised even when minor — "The Flesh …",
    # and "APPENDIX A" must not become "Appendix a".
    out = out[:1].upper() + out[1:]
    words = list(re.finditer(r"[^\W\d_](?:[^\W\d_]|['’])*", out))
    if words:
        last = words[-1]
        out = (
            out[: last.start()]
            + last.group(0)[:1].upper()
            + last.group(0)[1:]
            + out[last.end() :]
        )
    return out


def titlecase_label(text: str) -> str:
    """Recase a label whose casing carries no information.

    Headings render in a small-caps display face, which draws lowercase letters
    as small capitals. Either extreme loses the effect: the books set move names
    and arcana powers in full caps ("CHART A COURSE"), which comes out as
    unbroken full caps, and roll-table labels in full lower case ("why they
    might care"), which comes out as unbroken small caps with no lead capital.
    Both are retitled; genuinely mixed-case input is left alone.
    """
    if not text:
        return text
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return text
    if all(c.isupper() for c in letters) or all(c.islower() for c in letters):
        return smart_title(text)
    # A shouted label can lead a mixed-case line: stat blocks name a creature
    # then gloss it ("ANDALAU OF THE FLUTE, dancing wind spirit"), and appendix
    # titles prefix a section ("APPENDIX A: Ages of the World"). Only the label
    # is shouted, so retitle up to the separator and leave the rest.
    for mark in (",", ":"):
        head, sep, tail = text.partition(mark)
        head_letters = [c for c in head if c.isalpha()]
        if sep and head_letters and all(c.isupper() for c in head_letters):
            return smart_title(head) + sep + tail
    return text


def titlecase_name(text: str) -> str:
    """Recase a *name* — a card face, creature, or discovery.

    Same as :func:`titlecase_label`, plus sentence case. The books write many
    names as a sentence ("A giant's dormitory", "Spitting drake", "The village
    of Stonetop"); in a small-caps face that draws one full capital followed by
    a long run of small capitals. Names read better in title case, so a name
    holding a lowercase word that a title would capitalise is retitled. A name
    that is already title case has no such word and is left alone.
    """
    # Retitle a shouted label first, then judge the result: a stat block can be
    # both shouted and under-capitalised ("ANDALAU OF THE FLUTE, dancing wind
    # spirit"), and the gloss should be titled like any other name's.
    text = titlecase_label(text)
    # Under-capitalised if some word that a title would capitalise is lowercase.
    # A proper noun alone doesn't make a name title-cased ("The village of
    # Stonetop"), and a name that is already title case has no such word
    # ("Hec'tumel, Pale Serpent" — "Pale" and "Serpent" are both capitalised).
    # A hyphenated compound counts as one word: the book writes "The Would-be
    # Hero", and its lowercase tail is the spelling, not a missing capital.
    words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", text)
    if len(words) < 2:
        return text
    if any(w[:1].islower() and w.lower() not in _TITLE_MINOR for w in words[1:]):
        return smart_title(text)
    return text


def slugify(text: str) -> str:
    text = text.lower().replace("'", "").replace("'", "").replace("`", "")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def html_to_search_text(body_html: str) -> str:
    """Strip HTML/scripts to plain text for full-text search indexing."""
    if not body_html:
        return ""
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", body_html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = re.sub(r" +", " ", text)
    return text.strip()

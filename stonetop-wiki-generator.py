#!/usr/bin/env python3
"""
Build a static wiki from Stonetop Book II (1-up PDF).

Usage:
  python stonetop-wiki-generator.py --input <folder> --output <folder>

``--input`` is a folder containing the Book II 1-up PDF (and optionally
``Maps/`` campaign sheets). ``--output`` is where the static site is written
(index.html, pages/, css/, js/, images/).

PDF extraction, HTML structuring, linkify, and arcana parsing live in this
same module (formerly wiki_content.py).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

import fitz  # PyMuPDF


# ---------------------------------------------------------------------------
# PDF extraction & HTML structuring (was wiki_content.py)
# ---------------------------------------------------------------------------

# Mid-page gutter for typical Stonetop 1-up pages (~396pt wide, 2 columns)
DEFAULT_GUTTER = 198

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

MARKER_RE = re.compile(r"^\x02[A-Z0-9]+ ?")
VAL_TOKEN_RE = re.compile(
    r"^(\d{1,2}\*?|\+\d{1,2}|free|\d{1,2} or \d{1,2})$", re.I
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


def render_rich_text(s: str, link_fn) -> str:
    """Linkify text (inline formatting sentinels become tags inside link_fn)."""
    return link_fn(s)


def _lead_bold_prefix(text_spans: list[dict], text: str) -> str:
    """Visible text of the leading run of bold spans (for entry detection)."""
    parts: list[str] = []
    for g in text_spans:
        f = g["font"]
        if "Bold" in f and not f.startswith("Avara") and "FellType" not in f:
            parts.append(g["text"])
        else:
            break
    if not parts:
        return ""
    prefix = normalize_text(re.sub(r"\s+", " ", " ".join(parts))).strip()
    prefix = re.sub(r"\s+", " ", prefix).strip()
    if prefix and _defmt(text).startswith(prefix):
        return prefix
    return ""


def _dedupe_rects(rects: list) -> list:
    out = []
    seen = set()
    for r in rects:
        key = (round(r.x0), round(r.y0))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out



# PDF image xrefs → semantic game-icons.net assets (static/images/icons/*.svg).
# White-on-transparent SVGs from https://game-icons.net/ (CC BY 3.0); CSS
# recolors them to --accent.
ICON_XREF_TO_NAME: dict[int, str] = {
    18815: "spring",
    18816: "summer",
    18817: "winter",
    18818: "autumn",
    18819: "people",
    18825: "arcana",
    18826: "treasure",
    18831: "danger",
    18849: "undead",
    18866: "aberration",
    18867: "beast",
    18869: "spirit",
    18870: "person",
    18879: "fae",
    18881: "site",
    18882: "aberration",
    18883: "material",
    18884: "construct",
    18885: "beast-large",
    18899: "primordial",
    18900: "threat",
    18910: "spirit",
    4210: "hunter",
    4525: "time",
    5913: "sea",
    6614: "flora",
    7427: "spirit",
    7479: "construct",
    7548: "aberration",
}


def resolve_book_icon(xref: int, icon_dir: Path | None = None) -> str | None:
    """
    Map a PDF category-icon xref to a static game-icons SVG.

    Returns path relative to images/ (e.g. ``icons/beast.svg``). Copies the
    SVG into *icon_dir* when provided so the output wiki is self-contained.
    """
    if not xref:
        return None
    name = ICON_XREF_TO_NAME.get(int(xref), "default")
    fname = f"{name}.svg"
    static_src = Path(__file__).resolve().parent / "static" / "images" / "icons" / fname
    if not static_src.is_file():
        static_src = (
            Path(__file__).resolve().parent / "static" / "images" / "icons" / "default.svg"
        )
        fname = "default.svg"
    if not static_src.is_file():
        return None
    if icon_dir is not None:
        icon_dir = Path(icon_dir)
        icon_dir.mkdir(parents=True, exist_ok=True)
        dest = icon_dir / fname
        if not dest.exists() or dest.stat().st_size != static_src.stat().st_size:
            try:
                shutil.copy2(static_src, dest)
            except OSError:
                pass
    return f"icons/{fname}"


def book_icon_img_html(rel_path: str, *, rel_prefix: str = "../") -> str:
    """HTML <img> for a book category icon (path under images/)."""
    if not rel_path:
        return ""
    src = f"{rel_prefix}images/{rel_path.lstrip('/')}"
    return (
        f'<img class="book-icon" src="{html.escape(src)}" alt="" '
        f'width="18" height="18" loading="lazy">'
    )


def extract_page_rich(
    page: fitz.Page,
    article_title: str = "",
    first_page: bool = False,
    state: dict | None = None,
    *,
    y_clip: tuple[float, float] | None = None,
    single_column: bool = False,
) -> list[str]:
    """Marker-annotated reading-order lines (left column then right).

    ``y_clip`` restricts to a vertical band (used for minor-arcana halves).
    ``single_column`` disables the mid-page gutter split (arcana cards read
    as one column full width).
    """
    if state is None:
        state = {}
    gutter = page.rect.width * 0.5
    if 350 < page.rect.width < 450:
        gutter = DEFAULT_GUTTER
    if single_column:
        gutter = page.rect.width + 1  # everything falls in the left column
    page_h = page.rect.height
    cy0, cy1 = y_clip if y_clip else (float("-inf"), float("inf"))

    spans: list[dict] = []
    seen_spans: set = set()
    for b in page.get_text("dict").get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            for s in ln.get("spans", []):
                txt = (s.get("text") or "").replace("\t", " ")
                if not txt.strip():
                    continue
                x0, y0, x1, y1 = s["bbox"]
                if not (cy0 <= y0 < cy1):
                    continue
                # Some headers are double-printed at identical coordinates
                key = (round(x0), round(y0), txt.strip())
                if key in seen_spans:
                    continue
                seen_spans.add(key)
                spans.append(
                    {
                        "x": x0,
                        "y": y0,
                        "x1": x1,
                        "text": txt,
                        "font": s.get("font", ""),
                        "size": s.get("size", 9.0),
                    }
                )
    if not spans:
        return []

    spirals: list = []
    tails: list = []
    diamonds: list = []
    boxes_sq: list = []
    hrules: list = []  # horizontal rules (hairlines)
    box_rect = None
    for dr in page.get_drawings():
        r = dr["rect"]
        if not (cy0 <= (r.y0 + r.y1) / 2 < cy1):
            continue
        w, h = r.width, r.height
        ni = len(dr["items"])
        if 4.0 <= w <= 8.0 and 4.0 <= h <= 8.0:
            if ni >= 15:
                spirals.append(r)
            elif ni == 4:
                diamonds.append(r)
            elif 7 <= ni <= 10:
                boxes_sq.append(r)  # open-square checkbox
        elif w <= 4.5 and h <= 6.5 and 5 <= ni <= 9:
            tails.append(r)  # flourish on the question spiral
        elif w >= 40 and h <= 1.5 and ni <= 3:
            hrules.append(r)
        elif first_page and 110 <= w <= 320 and h >= 90 and box_rect is None:
            box_rect = r
    spirals = _dedupe_rects(spirals)
    diamonds = _dedupe_rects(diamonds)
    boxes_sq = _dedupe_rects(boxes_sq)
    hrules = _dedupe_rects(hrules)

    # Small category icons (beast / undead / solitary / treasure / …)
    icons: list[dict] = []
    try:
        for info in page.get_image_info(xrefs=True):
            bb = info["bbox"]
            bw = bb[2] - bb[0]
            bh = bb[3] - bb[1]
            if not (10.0 <= bw <= 24.0 and 10.0 <= bh <= 24.0):
                continue
            ymid = (bb[1] + bb[3]) / 2
            if not (cy0 <= ymid < cy1):
                continue
            xref = info.get("xref")
            if not xref:
                continue
            icons.append(
                {
                    "x0": bb[0],
                    "y0": bb[1],
                    "x1": bb[2],
                    "y1": bb[3],
                    "ymid": ymid,
                    "xref": int(xref),
                }
            )
    except Exception:
        icons = []

    # Chapter info box (steading summary) — only if it really holds the stats
    box_spans: list[dict] = []
    if box_rect is not None:
        cand = [
            s
            for s in spans
            if box_rect.x0 - 2 <= s["x"] <= box_rect.x1 + 2
            and box_rect.y0 - 2 <= s["y"] <= box_rect.y1 + 2
        ]
        if any(
            s["text"].strip().startswith(("Size", "Population", "Prosperity"))
            for s in cand
        ):
            ids = {id(s) for s in cand}
            box_spans = cand
            spans = [s for s in spans if id(s) not in ids]
        else:
            box_rect = None

    def build_lines(region: list[dict], x_lo: float, x_hi: float) -> list[dict]:
        """Cluster spans into visual lines, attach glyph markers."""
        # Only glyphs inside this region's x-range may mark its lines —
        # otherwise a bullet in the left column marks right-column lines
        # that happen to share its y.
        def _loc(rects: list) -> list:
            return [r for r in rects if x_lo - 6 <= r.x0 <= x_hi]

        r_spirals = _loc(spirals)
        r_tails = _loc(tails)
        r_diamonds = _loc(diamonds)
        r_checks = _loc(boxes_sq)
        r_hrules = _loc(hrules)
        r_icons = [ic for ic in icons if x_lo - 6 <= ic["x0"] <= x_hi]

        region = sorted(region, key=lambda s: (s["y"], s["x"]))
        lines: list[list[dict]] = []
        for s in region:
            if lines and abs(s["y"] - lines[-1][0]["y"]) <= 4.5:
                lines[-1].append(s)
            else:
                lines.append([s])
        recs: list[dict] = []
        for group in lines:
            group.sort(key=lambda s: s["x"])
            size = max(g["size"] for g in group)
            y_top = min(g["y"] for g in group)
            y_c = y_top + size / 2
            # Dingbat spans are glyph bullets, not text
            text_spans = []
            bullet = None
            ding = 0
            for g in group:
                f = g["font"]
                if "Dingbat" in f or "Wingdings" in f:
                    ding += len(g["text"].strip())
                    if not text_spans and g["text"].strip():
                        bullet = "stat" if "ITC" in f else "check"
                    continue
                text_spans.append(g)
            if not text_spans:
                # A row of lone dingbat glyphs → an arcana progress-mark track
                if ding >= 2:
                    recs.append({"y": y_top, "marks": ding})
                continue
            first_x = text_spans[0]["x"]
            # Category icon left of / near this line. Icons often sit slightly
            # above the tag line and share an x with tab-indented body text,
            # so keep the y band loose and don't require x1 < first_x.
            # Consume the icon so the next line (tags) does not re-attach it.
            icon_xref = None
            for j, ic in enumerate(r_icons):
                if abs(ic["ymid"] - y_c) > 11.0:
                    continue
                if ic["x0"] > first_x + 6:
                    continue
                if first_x - ic["x0"] > 42:
                    continue
                icon_xref = ic["xref"]
                r_icons.pop(j)
                break
            # Vector glyphs on this line
            row_dia = sorted(
                (d for d in r_diamonds if abs((d.y0 + d.y1) / 2 - y_c) <= 4.0),
                key=lambda d: d.x0,
            )
            if bullet is None:
                for r in r_checks:
                    if (
                        abs((r.y0 + r.y1) / 2 - y_c) <= 5.5
                        and r.x0 < first_x
                        and first_x - r.x0 <= 25
                    ):
                        bullet = "check"
                        break
            if bullet is None:
                for r in r_spirals:
                    if (
                        abs((r.y0 + r.y1) / 2 - y_c) <= 6.0
                        and r.x0 < first_x
                        and first_x - r.x0 <= 25
                    ):
                        bullet = "b"
                        for t in r_tails:
                            if (
                                abs(t.x0 - r.x1) <= 5.0
                                and abs(t.y0 - r.y0) <= 5.0
                            ):
                                bullet = "q"
                                break
                        break
            # Assemble text with diamonds inserted by x position; add spaces
            # only across real gaps so punctuation spans stay attached.
            # Track bold/italic per span as (text, bold, ital) segments.
            segs: list[list] = []  # [text, bold, ital]
            prev_x1: float | None = None
            di = 0

            def _style(font: str) -> tuple[bool, bool]:
                bold = (
                    "Bold" in font
                    and not font.startswith("Avara")
                    and "FellType" not in font
                )
                return bold, ("Italic" in font)

            def _push(seg: str, b: bool, ital: bool) -> None:
                if not seg:
                    return
                if segs and segs[-1][1] == b and segs[-1][2] == ital:
                    segs[-1][0] += seg
                else:
                    segs.append([seg, b, ital])

            def _append(seg: str, x0: float, x1: float, b: bool, ital: bool) -> None:
                nonlocal prev_x1
                if segs and prev_x1 is not None:
                    gap = x0 - prev_x1
                    last = segs[-1][0]
                    if gap > 1.0 and not last.endswith(" ") and not seg.startswith(" "):
                        _push(" ", b, ital)
                _push(seg, b, ital)
                prev_x1 = x1

            for g in text_spans:
                gb, gi = _style(g["font"])
                while di < len(row_dia) and row_dia[di].x0 < g["x"] - 1:
                    _append("◇ ", row_dia[di].x0, row_dia[di].x1, False, False)
                    di += 1
                _append(g["text"], g["x"], g["x1"], gb, gi)
            while di < len(row_dia):
                _append("◇", row_dia[di].x0, row_dia[di].x1, False, False)
                di += 1

            def _wrap(seg_text: str, b: bool, ital: bool) -> str:
                if not (b or ital):
                    return seg_text
                core = seg_text.strip()
                if not core:
                    return seg_text  # whitespace-only: never wrap
                # Keep whitespace outside the tags so it collapses cleanly
                lead = seg_text[: len(seg_text) - len(seg_text.lstrip())]
                trail = seg_text[len(seg_text.rstrip()):]
                if ital:
                    core = I_ON + core + I_OFF
                if b:
                    core = B_ON + core + B_OFF
                return lead + core + trail

            text = "".join(_wrap(t, b, ital) for t, b, ital in segs)
            text = re.sub(r"◇\s+(?=◇)", "◇", text)
            text = text.replace("( ◇", "(◇")
            text = re.sub(r"\s+([,;:.?!])", r"\1", text)
            text = normalize_text(re.sub(r"\s+", " ", text)).strip()
            if not _defmt(text).strip():
                continue
            fonts = defaultdict(int)
            for g in text_spans:
                fonts[(g["font"], round(g["size"]))] += len(g["text"])
            dom_font, dom_size = max(fonts, key=fonts.get)
            has_value = False
            val_text = ""
            last = text_spans[-1]
            if (
                len(text_spans) >= 2
                and VAL_TOKEN_RE.match(last["text"].strip())
                and last["size"] <= 9.5
            ):
                has_value = True
                val_text = last["text"].strip()
            recs.append(
                {
                    "y": y_top,
                    "x": first_x,
                    "text": text,
                    "font": dom_font,
                    "size": dom_size,
                    "bullet": bullet,
                    "has_value": has_value,
                    "val": val_text,
                    "val_x": last["x"] if has_value else 0.0,
                    "bold_lead": "Bold" in text_spans[0]["font"],
                    "all_bold": all("Bold" in g["font"] for g in text_spans),
                    "bold_prefix": _lead_bold_prefix(text_spans, text),
                    "icon_xref": icon_xref,
                }
            )
        # Horizontal rules interleaved by y
        for r in r_hrules:
            recs.append({"y": r.y0, "hr": True})
        recs.sort(key=lambda rec: (rec.get("y", 0), 0 if rec.get("hr") else 1))
        return recs

    def emit_region(recs: list[dict], col_x0: float, in_box: bool) -> list[str]:
        out: list[str] = []
        prev_y: float | None = None
        table = state.get("table") if not in_box else None

        def flush_table(keep_open: bool = False):
            nonlocal table
            if table is None:
                return
            header_done = table.get("header_emitted", False)
            if table["rows"]:
                if not header_done:
                    out.append(M_VT + table["title"])
                    header_done = True
                for item, val in table["rows"]:
                    out.append(f"{M_VR}{item}\x03{val}")
                for note in table["notes"]:
                    out.append(M_VF + note)
            elif not header_done and not keep_open:
                out.append(M_TH + table["title"])
            if keep_open:
                state["table"] = {
                    "title": table["title"],
                    "rows": [],
                    "notes": [],
                    "header_emitted": header_done,
                    "resumed": True,
                }
            else:
                state.pop("table", None)
            table = None

        def close_table():
            flush_table(keep_open=False)

        idx = 0
        n_recs = len(recs)
        entry_active = False  # inside a People-style entry (for tier-2 bullets)
        while idx < n_recs:
            rec = recs[idx]
            if rec.get("hr"):
                y_hr = float(rec["y"])
                # Cross-column hairlines share a y; skip the second copy.
                if any(abs(y_hr - yy) < 5.0 for yy in emitted_hr_ys):
                    idx += 1
                    continue
                emitted_hr_ys.append(y_hr)
                close_table()
                # Don't stack HRs back-to-back within a column either
                if not (out and out[-1] == M_HR):
                    out.append(M_HR)
                prev_y = rec["y"]
                idx += 1
                continue
            if "marks" in rec:
                close_table()
                out.append(M_MARK + str(rec["marks"]))
                prev_y = rec["y"]
                idx += 1
                continue
            if rec.get("icon_xref"):
                rel = resolve_book_icon(
                    rec["icon_xref"], state.get("icon_dir")
                )
                if rel:
                    # Icons often sit on the tag/stat line under an Avara name
                    # heading; attach them to that heading instead.
                    if out and (
                        out[-1].startswith(M_H2) or out[-1].startswith(M_H3)
                    ):
                        out.insert(len(out) - 1, M_ICON + rel)
                    else:
                        out.append(M_ICON + rel)
            text = rec["text"]
            y = rec["y"]
            gap = (y - prev_y) if prev_y is not None else 999.0
            prev_y = y
            idx += 1

            # Page furniture
            if text.isdigit() and len(text) <= 3 and y > page_h - 40:
                continue
            near_top = y < 100
            if is_running_header(_defmt(text), article_title, near_page_top=near_top):
                continue
            if is_fully_pairwise_doubled(_defmt(text)):
                text = undouble_words(_defmt(text))
                if is_running_header(text, article_title, near_page_top=True):
                    continue

            # De-tokenized copy for all content-based structural decisions;
            # `text` keeps its inline bold/italic sentinels for output.
            dtext = _defmt(text)

            # Tail of the previously emitted line (persists across columns)
            prev_tail = _defmt(state.get("last_line") or "")
            state["last_line"] = dtext

            font = rec["font"]
            is_avara = font.startswith("Avara")
            # Fell Type at ~12pt marks table headers; at 9pt it's just
            # small-caps styling inside prose ("terrain", "encounter")
            is_fell = "FellType" in font and rec["size"] >= 10.5

            # Headings end any open table
            if is_avara or is_fell:
                close_table()
                state["last_line"] = ""
                entry_active = False

            if is_avara:
                if rec["size"] >= 16:
                    continue  # chapter title — page shell already shows it
                if dtext.isdigit():
                    continue
                if rec["size"] >= 11:
                    out.append(M_H2 + dtext)
                else:
                    out.append(M_H3 + dtext)
                continue

            if is_fell:
                title = dtext.strip(" .")
                title = re.sub(r"\s*value\s*$", "", title, flags=re.I).strip(" .")
                if re.match(r"^\d{0,2}d(?:4|6|8|10|12|20)\b", title, re.I):
                    # dice-table header ("1d6 discovery") — let the
                    # roll-table parser handle it as a plain line
                    out.append(title)
                    continue
                if not title:
                    title = "value"
                table = {"title": title, "rows": [], "notes": []}
                state.pop("table", None)
                continue

            # Inside a value table? (item text de-tokenized — tables are styled)
            if table is not None:
                if rec["has_value"] and rec["val_x"] - col_x0 > 100:
                    item = dtext[: dtext.rfind(rec["val"])].strip() if dtext.endswith(rec["val"]) else dtext
                    if item.endswith("("):  # value glued oddly — keep whole
                        item = dtext
                    # Wrapped item whose value prints on the second line
                    if (
                        table["rows"]
                        and table["rows"][-1][1] == ""
                        and rec["x"] - col_x0 >= 7
                        and not item.startswith(("...", "…"))
                    ):
                        prev_item, _ = table["rows"][-1]
                        table["rows"][-1] = (prev_item + " " + item, rec["val"])
                    else:
                        table["rows"].append((item, rec["val"]))
                    state["table"] = table
                    continue
                if dtext.startswith("*"):
                    table["notes"].append(dtext)
                    continue
                indent = rec["x"] - col_x0
                wrapish = indent >= 7 or dtext[:1].islower() or dtext[:1] in "(◇"
                if table["rows"] and wrapish and not rec["bullet"]:
                    it, val = table["rows"][-1]
                    table["rows"][-1] = (it + " " + dtext, val)
                    continue
                # Wrap of the previous column's last row, continuing at the
                # top of this column
                if (
                    table.get("resumed")
                    and not table["rows"]
                    and wrapish
                    and not rec["bullet"]
                ):
                    out.append(M_VA + dtext)
                    continue
                # flush line, no value: keep as a blank-value row only if the
                # table clearly continues right after
                if (
                    table["rows"]
                    and idx < n_recs
                    and recs[idx]["has_value"]
                    and recs[idx]["val_x"] - col_x0 > 100
                    and not rec["bullet"]
                ):
                    table["rows"].append((dtext, ""))
                    continue
                close_table()

            # Bullets / checkboxes / ellipsis items. The leading bold lead-in
            # is already wrapped by the inline-formatting tokens in `text`.
            def _marked(t: str) -> str:
                return t

            if rec["bullet"] == "check":
                out.append(M_C + _marked(text))
                continue
            if rec["bullet"] == "stat":
                out.append("• " + _marked(text))
                continue
            if rec["bullet"] in ("b", "q"):
                marker = M_Q if rec["bullet"] == "q" else M_B
                if marker == M_B and entry_active and not in_box:
                    marker = M_B2  # sub-bullet of the current entry
                if re.match(r"^(\.\.\.|…)", dtext):
                    marker = M_E
                    text = re.sub(r"^[\x04\x06]*(\.\.\.|…)\s*", "", text)
                out.append(marker + _marked(text))
                continue
            if re.match(r"^(\.\.\.|…)\s*\S", dtext):
                out.append(M_E + re.sub(r"^[\x04\x06]*(\.\.\.|…)\s*", "", text))
                continue

            # People/Places-style entry: bold lead-in on a hanging indent
            # ("Brennan, onetime bandit leader…") — render as a list item.
            # A small gap means a wrap of the previous line whose first word
            # happens to be a bold cross-ref — not a new entry; at a column
            # start, require the previous column to have ended a sentence.
            if (
                not in_box
                and rec["bold_prefix"]
                and 12 <= rec["x"] - col_x0 <= 30
                and (
                    15 < gap < 900
                    or (
                        gap >= 900
                        and (not prev_tail or prev_tail[-1:] in '.!?):;"')
                    )
                )
            ):
                out.append(M_B + _marked(text))
                entry_active = True
                continue

            # Bold labels inside the info box ("Resources", "Defenses +1")
            if in_box and rec["all_bold"] and len(dtext) <= 40:
                out.append(M_H4 + dtext)
                continue

            # Wrap continuation of a list item directly above; a bold lead
            # ("Loyalist instinct …", "Defenses +1") starts a new thought.
            # A steading requirement group-header ("Requires…", "And then…")
            # must stay its own line so improvement blocks parse correctly.
            if (
                out
                and gap <= 13.5
                and not rec["bold_lead"]
                and not re.match(
                    r"^(Requires?\b|And then\b|And (?:either|one|any)\b)",
                    dtext, re.I,
                )
                and (
                    out[-1][:3] in (M_B, M_Q, M_E, M_C)
                    or out[-1].startswith((M_B2, "• "))
                )
            ):
                body_p, tail_p = _split_trailing_fmt(out[-1])
                lead_p, rest_p = _split_leading_fmt(text)
                # Soft hyphen wrap only (not em/en dash: "came—" + "when")
                if (
                    body_p.endswith("-")
                    and not body_p.endswith(("–", "—", "--"))
                    and rest_p[:1].islower()
                ):
                    out[-1] = body_p[:-1] + _cancel_fmt_seam(tail_p, lead_p) + rest_p
                elif body_p.endswith(("–", "—")):
                    out[-1] = body_p + _cancel_fmt_seam(tail_p, lead_p) + rest_p
                else:
                    out[-1] = out[-1] + " " + text
                continue

            entry_active = False  # a plain paragraph ends the current entry
            out.append(text)

        # A table still open at column end may continue in the next column
        flush_table(keep_open=True)
        return out

    result: list[str] = []
    # Matching left/right column hairlines share a y; only emit one per band
    # so L→R reading order does not produce stacked double <hr>s.
    emitted_hr_ys: list[float] = []

    if box_spans:
        recs = build_lines(box_spans, box_rect.x0 - 2, box_rect.x1 + 2)
        inner = emit_region(recs, box_rect.x0, True)
        if inner:
            result.append(M_BOX)
            result.extend(inner)
            result.append(M_ENDBOX)

    cols: list[list[dict]] = [[], []]
    for s in spans:
        cols[0 if s["x"] < gutter else 1].append(s)
    for ci, col in enumerate(cols):
        if not col:
            continue
        col_x0 = min(s["x"] for s in col)
        x_lo, x_hi = (0.0, gutter) if ci == 0 else (gutter, page.rect.width)
        recs = build_lines(col, x_lo, x_hi)
        # resume a table that carried over from the previous column/page
        carried = state.get("table")
        result.extend(emit_region(recs, col_x0, False))
        # if the carried table produced no continuation rows, drop the state
        if carried is not None and state.get("table") is carried:
            state.pop("table", None)

    # Collapse any remaining consecutive HRs from within a single column
    cleaned: list[str] = []
    for line in result:
        if line == M_HR and cleaned and cleaned[-1] == M_HR:
            continue
        cleaned.append(line)
    return cleaned


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
    """True if line is a dice-table header (``1d6 size``, bare ``1d12``, or reverse)."""
    if not line:
        return False
    if ROLL_HEADER_RE.match(line) or ROLL_HEADER_DICE_ONLY.match(line):
        return True
    # reverse "label 1d6" — only when not also a numbered entry
    if ROLL_HEADER_REV_RE.match(line) and not ENTRY_RE.match(line):
        return True
    return False


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
    if first in ORG_TAGS:
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
    a = _defmt(a)
    b = _defmt(b)
    # Structural marker lines are atomic — never merge into or out of them
    # (bullet items may still absorb plain continuations; see the a-side rule)
    if b.startswith("\x02"):
        return False
    if a.startswith("\x02"):
        if a[:3] in (M_B, M_Q, M_E, M_C) or a.startswith(M_B2):
            a = strip_markers(a)
        else:
            return False
    # Never glue pure arcana tag lines to/from neighbors
    # ("A giant's dormitory" + "magical" + "In a ruin…")
    if _is_pure_arcana_tag_line(a) or _is_pure_arcana_tag_line(b):
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
    if re.search(r"\(pages?\s*$", a, re.I) and re.match(r"^\d", b):
        return True
    if re.search(r"\(pages?\s+\d*$", a, re.I) and re.match(
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
        re.search(r"\(pages?\s+[\d,\s\-–—]+\)", a, re.I)
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
    if looks_like_heading(b) and len(b) < 40:
        return False
    if ROLL_HEADER_RE.match(b) or ROLL_HEADER_DICE_ONLY.match(b):
        return False
    if ENTRY_RE.match(b):
        return False
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


def merge_wrapped_lines(lines: list[str]) -> list[str]:
    if not lines:
        return []
    out: list[str] = []
    buf = lines[0]
    for nxt in lines[1:]:
        if should_join(buf, nxt):
            # Test for a trailing hyphen past any inline-format sentinels, so a
            # bold/italic word wrapped across a line ("crys-" + "tal") still
            # de-hyphenates instead of becoming "crys- tal".
            body, tail = _split_trailing_fmt(buf)
            if body.endswith(("–", "—")):
                # Em/en dash line break: keep the dash, no extra space
                lead, rest = _split_leading_fmt(nxt)
                buf = body + _cancel_fmt_seam(tail, lead) + rest
            elif body.endswith("-") and not body.endswith("--"):
                lead, rest = _split_leading_fmt(nxt)
                if rest[:1].islower():
                    # Soft-hyphen wrap; cancel the format seam so it's one run.
                    buf = body[:-1] + _cancel_fmt_seam(tail, lead) + rest
                else:
                    # Capital after the hyphen → real compound; keep it, glue.
                    buf = buf + nxt
            else:
                buf = buf + " " + nxt
        else:
            out.append(buf)
            buf = nxt
    out.append(buf)
    return out


def dice_button(expr: str) -> str:
    # Normalize en/em dashes to ASCII for the roller; keep display as written
    e = expr.lower().replace("–", "-").replace("—", "-")
    e = re.sub(r"\s*([+\-])\s*", r"\1", e)  # d10 + 3 → d10+3
    return (
        f'<button type="button" class="dice-roll" data-dice="{html.escape(e)}" '
        f'title="Click to roll {html.escape(expr)}">{html.escape(expr)}</button>'
    )


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


def _strip_link_label_noise(label: str) -> str:
    """
    Drop list/prose glue that page-ref capture often includes:
    'or wolves', 'maybe even a kleztigr', 'and revenants'
    """
    s = label.strip()
    # Repeatedly peel leading connectors / hedges / articles
    lead = re.compile(
        r"^(?:"
        r"or|and|maybe|even|perhaps|possibly|just|like|including|"
        r"a|an|the|some|any|its|their"
        r")\s+",
        re.I,
    )
    while True:
        n = lead.sub("", s)
        if n == s:
            break
        s = n
    return s.strip(" ,;:-")


def _singularize_word(word: str) -> str | None:
    """Best-effort English singular for the last word of a monster name."""
    w = word.lower()
    if len(w) <= 2:
        return None
    # wolves → wolf, knives → knife
    if w.endswith("ves") and len(w) > 4:
        return w[:-3] + "f"
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    # boxes/churches/dishes → strip es; NOT drakes/caves (vowel + consonant + es)
    if re.search(r"(?:s|ss|sh|ch|x|z)es$", w):
        return w[:-2]
    if w.endswith("oes") and len(w) > 3:
        return w[:-2]  # heroes → hero, potatoes → potato
    if w.endswith("es") and len(w) > 3:
        # drakes → drake, bears handled via plain -s below
        return w[:-1]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 2:
        return w[:-1]
    return None


def _section_match_keys(label: str) -> list[str]:
    """Generate normalized keys to match a link label to a section/monster id."""
    cleaned = _strip_link_label_noise(label)
    base = normalize_section_key(cleaned)
    if not base:
        return []
    keys: list[str] = [base]
    # Also try original without noise strip (already normalized)
    raw = normalize_section_key(label)
    if raw and raw not in keys:
        keys.append(raw)

    def add(k: str) -> None:
        k = k.strip()
        if k and k not in keys:
            keys.append(k)

    for candidate in list(keys):
        words = candidate.split()
        if not words:
            continue
        last = words[-1]
        stem = _singularize_word(last)
        if stem and stem != last:
            add(" ".join(words[:-1] + [stem]) if len(words) > 1 else stem)
        # pluralize if singular
        if not last.endswith("s"):
            add(" ".join(words[:-1] + [last + "s"]) if len(words) > 1 else last + "s")
        # drop trailing type words: "pack drake" ↔ "pack"
        stripped = re.sub(
            r"\s+(drake|bear|folk|spirit|ghost|wolf|boar|cougar)s?$",
            "",
            candidate,
        ).strip()
        if stripped and stripped != candidate:
            add(stripped)
        # last word alone (wolves → wolf already handled; "kleztigr")
        if len(words) > 1:
            add(last)
            if stem:
                add(stem)
    # Trailing phrases: "they suffer the forest s wrath" → "forest s wrath"
    base_words = base.split()
    if len(base_words) >= 3:
        for k_len in (4, 3, 2):
            if len(base_words) > k_len:
                tail = " ".join(base_words[-k_len:])
                if len(tail) >= 8:
                    add(tail)
    return keys


def resolve_section_fragment(
    page: int | None,
    label: str | None,
    target_slug: str | None,
    section_index: dict | None,
) -> str | None:
    """
    Find a #fragment for a monster/section name on a target page/article.
    section_index keys:
      by_page_norm: {(page_int, norm): (slug, id)}
      by_slug_norm: {(slug, norm): id}
    """
    if not label or not section_index:
        return None
    keys = _section_match_keys(label)
    if not keys:
        return None

    by_page = section_index.get("by_page_norm") or {}
    by_slug = section_index.get("by_slug_norm") or {}

    def page_hit(key: str) -> str | None:
        # Only trust a by-page hit whose section lives on the link's target
        # article (one PDF page can map to several arcana slugs).
        entry = by_page.get((page, key))
        if entry and (target_slug is None or entry[0] == target_slug):
            return entry[1]
        return None

    for k in keys:
        if not k:
            continue
        if page is not None:
            hit = page_hit(k)
            if hit:
                return hit
        if target_slug and (target_slug, k) in by_slug:
            return by_slug[(target_slug, k)]
        # Compact form without spaces
        compact = k.replace(" ", "")
        if page is not None:
            hit = page_hit(compact)
            if hit:
                return hit
        if target_slug and (target_slug, compact) in by_slug:
            return by_slug[(target_slug, compact)]
    return None


class AnchorRegistry:
    """Assign unique fragment ids for headings and stat blocks on a page."""

    def __init__(self) -> None:
        self.used: set[str] = set()
        self.sections: list[dict] = []  # {id, name, norm}

    def add(self, name: str) -> str:
        base = slugify_id(name)
        sid = base
        n = 2
        while sid in self.used:
            sid = f"{base}-{n}"
            n += 1
        self.used.add(sid)
        self.sections.append(
            {"id": sid, "name": name, "norm": normalize_section_key(name)}
        )
        return sid


def _book_id_from_token(token: str) -> str | None:
    t = (token or "").strip().lower()
    if t in ("i", "1"):
        return "book1"
    if t in ("ii", "2"):
        return "book2"
    # Roman II is two I's — handle after single I
    if t == "ii" or t.replace(" ", "") == "ii":
        return "book2"
    return None


# Exact article/arcana title -> article. The bold label in "**Title** (page N)"
# is more authoritative than the page number, which cannot disambiguate two
# articles printed on the same page (minor arcana are two cards per page, so a
# page resolves to only one of them). Populated by set_title_index().
_TITLE_INDEX: dict[str, dict] = {}


def set_title_index(articles: list[dict]) -> None:
    _TITLE_INDEX.clear()
    for art in articles:
        t = (art.get("title") or "").strip()
        if not t:
            continue
        _TITLE_INDEX.setdefault(t.lower(), art)
        if t.lower().startswith("the "):
            _TITLE_INDEX.setdefault(t[4:].lower(), art)


def linkify_pages(
    text: str,
    lookup: dict[int, dict],
    current_slug: str | None,
    section_index: dict | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> str:
    """Escape text and turn page refs + dice into HTML.

    When text says ``Book II, page 270``, resolve against Book II's page map
    even if the current article is from Book I (and vice versa).
    """

    placeholders: list[str] = []

    def store(s: str) -> str:
        placeholders.append(s)
        return f"\x00{len(placeholders)-1}\x00"

    def resolve_lookup(book_id: str | None):
        if book_id and lookups and book_id in lookups:
            return lookups[book_id], (section_indexes or {}).get(book_id)
        return lookup, section_index

    def page_link(
        page: int,
        label: str | None = None,
        book_id: str | None = None,
    ) -> str:
        lk, sidx = resolve_lookup(book_id)
        art = lk.get(page)
        if not art:
            return html.escape(label or f"p. {page}")
        frag = resolve_section_fragment(page, label, art["slug"], sidx)
        # When the page path finds no in-article section for a distinctive
        # label, but that label uniquely names a section elsewhere, trust the
        # name over the page number. Fixes arcana discovery refs like
        # "A metal man (page 533)" where one printed page holds two arcana.
        if label and not frag:
            uniq = resolve_unique_section(label, sidx)
            if uniq and uniq[0] != art["slug"]:
                u_slug, u_sid, u_title = uniq
                if u_slug == current_slug:
                    return (
                        f'<a class="wiki-link" href="#{u_sid}" data-slug="{u_slug}" '
                        f'data-fragment="{u_sid}">'
                        f"{html.escape(label)}</a>"
                    )
                return (
                    f'<a class="wiki-link" href="{u_slug}.html#{u_sid}" '
                    f'data-slug="{u_slug}" data-fragment="{u_sid}">'
                    f"{html.escape(label)}</a>"
                )
        # Same article: in-page fragment if possible
        if art["slug"] == current_slug:
            if frag and label:
                return (
                    f'<a class="wiki-link" href="#{frag}" data-slug="{art["slug"]}" '
                    f'data-fragment="{frag}">'
                    f"{html.escape(label)}</a>"
                )
            return html.escape(label) if label else ""
        text_out = label if label else art["title"]
        href = f"{art['slug']}.html"
        if frag:
            href = f"{href}#{frag}"
        return (
            f'<a class="wiki-link" href="{href}" data-slug="{art["slug"]}" '
            f'{f"data-fragment=\"{frag}\" " if frag else ""}'
            f">{html.escape(text_out)}</a>"
        )

    def links_for_pages(
        pages: list[int],
        label: str | None = None,
        book_id: str | None = None,
    ) -> str:
        if not pages:
            return html.escape(label) if label else ""
        if label:
            primary = page_link(pages[0], label, book_id=book_id)
            if len(pages) == 1:
                return primary
            extras = " · ".join(
                page_link(p, book_id=book_id) for p in pages[1:]
            )
            return f"{primary} ({extras})" if extras else primary
        return " · ".join(page_link(p, book_id=book_id) for p in pages)

    work = text

    # Cross-book first: "Makers' Roads (Book II, page 270)" / "Book I, page 245"
    # Match Roman numerals carefully: Book II before Book I.
    def repl_book_page(m: re.Match) -> str:
        raw_book = m.group("book")
        # Regex only captures single char; re-scan for II
        full = m.group(0)
        book_m = re.search(r"Book\s*(II|I|2|1)\b", full, re.I)
        book_tok = book_m.group(1) if book_m else raw_book
        book_id = _book_id_from_token(book_tok)
        if not book_id:
            return m.group(0)
        title = (m.group("title") or "").strip() or None
        pages = parse_page_nums(m.group("pages"))
        return store(links_for_pages(pages, title, book_id=book_id))

    work = re.sub(
        r"(?P<title>(?:[A-Za-z][A-Za-z0-9'’\-]*(?:\s+[A-Za-z][A-Za-z0-9'’\-]*){0,6})\s+)?"
        r"\(?"
        r"(?:see\s+)?"
        r"Book\s*(?P<book>II|I|2|1)\s*[,:]?\s*"
        r"(?:starting\s+on\s+)?"
        r"pages?\s+(?P<pages>[\d,\s\-–—]+)"
        r"\)?",
        repl_book_page,
        work,
        flags=re.IGNORECASE,
    )

    # Bold cross-ref immediately followed by a page ref:
    # "**Mudslides** (page 376)" / "**Ghosts (**page 76)" → make the *bold*
    # text the (deep) link and drop the now-redundant "(page N)".
    def repl_bold_pageref(m: re.Match) -> str:
        inner = m.group("t")
        pages = parse_page_nums(m.group("pg"))
        poss = m.groupdict().get("poss") or ""
        clean = _defmt(inner).strip()
        clean = re.sub(r"[\s(]+$", "", clean).strip()
        if not pages or len(clean) < 2:
            return m.group(0)
        lk, sidx = resolve_lookup(None)
        # Prefer an exact title match over the page number (which can't tell
        # apart two arcana printed on the same page).
        art_by_title = _TITLE_INDEX.get(clean.lower())
        art = art_by_title or lk.get(pages[0])
        if not art:
            return m.group(0)
        # A title-resolved article is unrelated to `pages[0]`, so don't derive an
        # in-page fragment from that page.
        frag = (
            None
            if art_by_title
            else resolve_section_fragment(pages[0], clean, art["slug"], sidx)
        )
        disp_inner = re.sub(r"[\s(]+$", "", inner)
        # Keep possessive inside the link: "Stone Lords'"
        disp = html.escape(B_ON + disp_inner + B_OFF) + html.escape(poss)
        if art["slug"] == current_slug:
            if frag:
                return store(
                    f'<a class="wiki-link" href="#{frag}" '
                    f'data-slug="{art["slug"]}" data-fragment="{frag}">'
                    f"{disp}</a>"
                )
            return store(disp)
        href = f'{art["slug"]}.html' + (f"#{frag}" if frag else "")
        frag_attr = f'data-fragment="{frag}" ' if frag else ""
        return store(
            f'<a class="wiki-link" href="{href}" data-slug="{art["slug"]}" '
            f"{frag_attr}>{disp}</a>"
        )

    # Allow optional possessive between bold close and the page ref:
    # "Stone Lords' (page 382)"
    work = re.sub(
        r"\x04(?P<t>[^\x04\x05]*?)\x05"
        r"(?P<poss>(?:'|’)s?)?"
        r"\s*\(?\s*(?:see\s+)?"
        r"pages?\s+(?P<pg>[\d,\s\-–—]+)\)",
        repl_bold_pageref,
        work,
        flags=re.IGNORECASE,
    )

    # Title (page N[, M]) — same-book
    def repl_title_page(m: re.Match) -> str:
        title = m.group(1).strip()
        pages = parse_page_nums(m.group(2))
        # Regex may over-capture preceding words (up to 7). Prefer the resolved
        # article title when the capture ends with it, e.g.
        # "certain snow-covered meadows of the Huffel Peaks (page 236)"
        # → "certain snow-covered meadows of the " + link("Huffel Peaks").
        prefix = ""
        if pages:
            lk, _ = resolve_lookup(None)
            art = lk.get(pages[0]) if lk else None
            if art:
                at = (art.get("title") or "").strip()
                if at:
                    low = title.lower()
                    for cand in (at, at[4:] if at.lower().startswith("the ") else at):
                        c_low = cand.lower()
                        if not c_low:
                            continue
                        if low == c_low:
                            break
                        if low.endswith(c_low) and low[: -len(c_low)].endswith(" "):
                            # Keep original casing; put over-captured words back outside the link
                            prefix = title[: -len(cand)].rstrip()
                            title = title[-len(cand) :]
                            break
        # Peel list glue: "or wolves", "maybe even a kleztigr"
        cleaned = _strip_link_label_noise(title)
        if cleaned and normalize_section_key(cleaned) != normalize_section_key(
            title
        ):
            t_words = title.split()
            c_words = cleaned.split()
            n = len(c_words)
            if 0 < n <= len(t_words) and normalize_section_key(
                " ".join(t_words[-n:])
            ) == normalize_section_key(cleaned):
                head = " ".join(t_words[:-n]).strip()
                title = " ".join(t_words[-n:])
                if head:
                    prefix = (prefix + " " + head).strip() if prefix else head
        link = links_for_pages(pages, title)
        if prefix:
            return store(html.escape(prefix) + " " + link)
        return store(link)

    work = re.sub(
        r"([A-Za-z][A-Za-z0-9'’\-]*(?:\s+[A-Za-z][A-Za-z0-9'’\-]*){0,6})\s+"
        r"\((?:see\s+)?pages?\s+([\d,\s\-–—]+)\)",
        repl_title_page,
        work,
    )

    def repl_paren(m: re.Match) -> str:
        pages = parse_page_nums(m.group(1))
        return store(links_for_pages(pages))

    work = PAGE_REF_RE.sub(repl_paren, work)

    def repl_bare(m: re.Match) -> str:
        pages = parse_page_nums(m.group(1))
        return store(links_for_pages(pages))

    work = BARE_PAGE_RE.sub(repl_bare, work)

    # Escape remaining text in segments between placeholders
    parts = re.split(r"(\x00\d+\x00)", work)
    out = []
    for part in parts:
        m = re.fullmatch(r"\x00(\d+)\x00", part)
        if m:
            out.append(placeholders[int(m.group(1))])
        else:
            esc = html.escape(part)
            esc = DICE_RE.sub(lambda mm: dice_button(mm.group(1)), esc)
            out.append(esc)
    # Inline bold/italic sentinels → tags (after escaping and link insertion)
    return fmt_to_html("".join(out))


def auto_link_titles(html_text: str, articles: list[dict], current_slug: str | None) -> str:
    """Link bare article titles in HTML text nodes (simple pass)."""
    # Build longest-first titles
    titles = []
    for art in articles:
        if art["slug"] == current_slug:
            continue
        titles.append((art["title"], art))
        if "," in art["title"]:
            titles.append((art["title"].split(",")[0].strip(), art))
        if art["title"].lower().startswith("the "):
            titles.append((art["title"][4:], art))
    titles.sort(key=lambda x: -len(x[0]))

    # Only operate outside tags
    chunks = re.split(r"(<[^>]+>)", html_text)
    for i, chunk in enumerate(chunks):
        if not chunk or chunk.startswith("<"):
            continue
        for title, art in titles:
            if len(title) < 4:
                continue
            # word-boundary-ish match, not already inside a link from previous
            pattern = re.compile(
                r"(?<![\\w/])(" + re.escape(title) + r")(?![\\w/])",
                re.IGNORECASE,
            )

            def repl(m, art=art, title=title):
                return (
                    f'<a class="wiki-link" href="{art["slug"]}.html" '
                    f'data-slug="{art["slug"]}">'
                    f"{m.group(1)}</a>"
                )

            chunk = pattern.sub(repl, chunk, count=1)  # at most one per title per chunk
        chunks[i] = chunk
    return "".join(chunks)


def render_roll_table(
    dice: str,
    label: str,
    entries: list[tuple[str, str]],
    lookup: dict,
    current_slug: str | None,
    section_index: dict | None = None,
    anchor_id: str | None = None,
    link_kw: dict | None = None,
) -> str:
    lkw = link_kw or {}
    rows = []
    for num, body in entries:
        rows.append(
            f"<tr><th scope=\"row\">{html.escape(num)}</th>"
            f"<td>{linkify_pages(body, lookup, current_slug, section_index, **lkw)}</td></tr>"
        )
    id_attr = f' id="{html.escape(anchor_id)}"' if anchor_id else ""
    return (
        f'<div class="roll-table"{id_attr}>'
        f'<div class="roll-table-head">'
        f"{dice_button(dice)}"
        f' <span class="roll-label">{html.escape(label)}</span>'
        f"</div>"
        f'<table><tbody>{"".join(rows)}</tbody></table>'
        f"</div>"
    )


def render_value_table(
    title: str,
    rows: list[tuple[str, str]],
    lookup: dict,
    current_slug: str | None,
    section_index: dict | None = None,
    link_kw: dict | None = None,
    notes: list[str] | None = None,
) -> str:
    lkw = link_kw or {}
    body = []
    for item, val in rows:
        body.append(
            f"<tr><td>{linkify_pages(item, lookup, current_slug, section_index, **lkw)}</td>"
            f'<td class="val">{html.escape(val)}</td></tr>'
        )
    notes_html = "".join(
        f'<div class="value-note">{html.escape(nt)}</div>' for nt in (notes or [])
    )
    return (
        f'<div class="value-table">'
        f'<div class="value-table-head">{html.escape(title)}</div>'
        f"<table><tbody>{''.join(body)}</tbody></table>"
        f"{notes_html}"
        f"</div>"
    )


def render_stat_block(
    name: str,
    lines: list[str],
    lookup: dict,
    current_slug: str | None,
    section_index: dict | None = None,
    anchor_id: str | None = None,
    link_kw: dict | None = None,
    check_id: str | None = None,
    icon_html: str = "",
) -> str:
    """Compact monster/enemy/threat block — minimal vertical space."""
    lkw = link_kw or {}
    # Stat blocks are visually styled via CSS; parse/render on plain text so
    # inline formatting sentinels never break the HP/Damage/tag detection.
    name = _defmt(name)
    # Preserve embedded roll-table payloads; de-tokenized for everything else.
    norm_lines: list[str] = []
    for l in lines:
        if l.startswith("__ROLL_TABLE__") or l.startswith(M_C):
            norm_lines.append(l)
        else:
            norm_lines.append(_defmt(l))
    lines = norm_lines
    tags = ""
    stats: list[str] = []
    moves: list[str] = []
    other: list[str] = []
    checks: list[str] = []
    roll_tables: list[tuple[str, str, list[tuple[str, str]]]] = []
    seen_instinct = False
    in_questions = False

    for line in lines:
        if line.startswith("__ROLL_TABLE__"):
            payload = line[len("__ROLL_TABLE__") :]
            dice_s, _, rest = payload.partition("\x01")
            label_s, _, rest = rest.partition("\x01")
            ents: list[tuple[str, str]] = []
            for part in rest.split("\x02"):
                if not part:
                    continue
                num_s, _, body_s = part.partition("\x03")
                ents.append((num_s, body_s))
            roll_tables.append((dice_s, label_s, ents))
            continue
        if line.startswith(M_C):
            checks.append(line[len(M_C):].strip())
            continue
        low = line.lower().strip()
        if re.match(r"^questions\s*:?\s*$", low):
            in_questions = True
            other.append("Questions")
            continue
        if not tags and re.match(r"^Threat\b", line, re.I):
            tags = line
            continue
        # Don't absorb the next monster's identity
        if (
            tags
            and stats
            and looks_like_tag_line(line)
            and not low.startswith(("damage", "hp", "instinct"))
        ):
            # leftover identity of a following creature — stop via caller usually
            other.append(line)
            continue
        if looks_like_tag_line(line) and not tags:
            tags = line
        elif HP_LINE_RE.search(line) or low.startswith(
            ("damage", "special quality", "special qualities", "instinct", "armor")
        ):
            if low.startswith("instinct"):
                seen_instinct = True
            # Damage lines often continue with bare "d6 (...)" on next line
            if (
                stats
                and re.match(r"^d\d+", low)
                and stats[-1].lower().startswith("damage")
            ):
                stats[-1] = stats[-1] + " " + line
            else:
                stats.append(line)
        elif re.match(r"^d\d+", low) and stats:
            stats[-1] = stats[-1] + " " + line
        elif line.startswith("•") or line.startswith("·"):
            item = line.lstrip("•· ").strip()
            # After flavor notes (or a Questions section), trailing bullets
            # are options/requirements — keep them in notes, not moves.
            if in_questions or (other and seen_instinct and moves):
                other.append("• " + item)
            else:
                moves.append(item)
        elif low.startswith("when "):
            other.append(line)
        else:
            # Continue a Damage line that wrapped mid-parenthetical
            if stats and stats[-1].lower().startswith("damage") and (
                line[0:1].islower()
                or low.startswith(
                    ("maw ", "1 piercing", "piercing)", "advantage)", "messy,")
                )
                or stats[-1].endswith(",")
                or stats[-1].endswith("(")
                or re.match(r"^d\d+", low)
            ):
                stats[-1] = stats[-1] + " " + line
                continue
            # After instinct, short action phrases are moves (not more stats)
            if seen_instinct or (
                stats and any(s.lower().startswith("instinct") for s in stats)
            ):
                seen_instinct = True
                if low.startswith("instinct"):
                    stats.append(line)
                    continue
                # Long / narrative prose after the block → flavor notes, not moves
                is_bullet = line.startswith("•") or line.startswith("·")
                is_prose = (
                    not is_bullet
                    and line[0:1].isupper()
                    and (
                        len(line) > 55
                        or bool(other)
                        or low.startswith(
                            ("a ", "an ", "the ", "she ", "he ", "they ", "then ")
                        )
                    )
                )
                if is_prose:
                    other.append(line)
                    continue
                if len(line) < 100:
                    moves.append(line.lstrip("•· ").strip())
                    continue
            if moves and not looks_like_heading(line) and line[0:1].islower():
                moves[-1] = moves[-1] + " " + line
            else:
                other.append(line)

    moves = [m for m in moves if m.strip()]
    # Drop accidental second-creature tag lines from other
    notes = []
    for o in other:
        if looks_like_tag_line(o) or HP_LINE_RE.search(o):
            continue
        notes.append(o)

    def lf(t: str) -> str:
        return linkify_pages(t, lookup, current_slug, section_index, **lkw)

    def rr(t: str) -> str:
        return render_rich_text(t, lf)

    id_attr = f' id="{html.escape(anchor_id)}"' if anchor_id else ""
    parts = [
        f'<div class="stat-block"{id_attr}>'
        f'<h3 class="stat-name">{icon_html}{html.escape(name)}</h3>'
    ]
    if tags:
        parts.append(f'<p class="stat-tags">{rr(tags)}</p>')
    if stats:
        compact = " · ".join(s for s in stats)
        parts.append(f'<p class="stat-stats">{rr(compact)}</p>')
    if moves:
        parts.append('<ul class="stat-moves">')
        for mv in moves:
            parts.append(f"<li>{rr(mv)}</li>")
        parts.append("</ul>")
    for o in notes:
        if o.startswith("• "):
            # bullet note — keep as a compact list item style paragraph
            parts.append(f'<p class="stat-note">• {rr(o[2:])}</p>')
        else:
            parts.append(f'<p class="stat-note">{rr(o)}</p>')
    for dice_s, label_s, ents in roll_tables:
        rows_html = "".join(
            f'<tr><th scope="row">{html.escape(num_s)}</th>'
            f"<td>{rr(body_s)}</td></tr>"
            for num_s, body_s in ents
        )
        parts.append(
            f'<div class="roll-table roll-table-inline">'
            f'<div class="roll-table-head">'
            f"{dice_button(dice_s)}"
            f' <span class="roll-label">{html.escape(label_s)}</span>'
            f"</div>"
            f"<table><tbody>{rows_html}</tbody></table>"
            f"</div>"
        )
    if checks:
        parts.append(render_check_list(checks, rr, check_id or "chk"))
    parts.append("</div>")
    return "".join(parts)


def _is_chapter_toc_prose(line: str) -> bool:
    """True if this looks like the first body paragraph after a chapter TOC."""
    if not line:
        return False
    L = line.strip()
    if len(L) >= 70:
        return True
    if len(L) >= 48:
        words = L.split()
        lowerish = sum(1 for w in words if w and w[0].islower())
        if lowerish >= 2:
            return True
        if L.endswith((".", "!", "?")) and lowerish >= 1:
            return True
    # Common chapter openers
    if re.match(
        r"^(This chapter|There are|Player characters|Sites are|Maybe you|"
        r"A threat is|Discoveries are|NPCs are|The homefront|Stonetop is|"
        r"The basic moves|When you|Introductions are|Most events|"
        r"Use this procedure|Thanks to|The following works)",
        L,
        re.I,
    ) and len(L) >= 35:
        return True
    return False


def _is_chapter_toc_garbage(line: str) -> bool:
    """Column-glued junk often trailing a TOC (e.g. 'If you want of play…')."""
    L = line.strip()
    if not L:
        return True
    words = L.split()
    lowerish = sum(1 for w in words if w and w[0].islower())
    # Multi-word soup without sentence punctuation
    if len(words) >= 5 and lowerish >= 2 and not L.endswith((".", "!", "?", ":")):
        return True
    if len(L) > 42 and lowerish >= 2 and not L.endswith((".", "!", "?", ":")):
        return True
    # Lone wrap shards
    if L.lower() in {"up", "plan", "door", "moves", "fly", "change"}:
        return True
    return False


def split_chapter_toc(
    lines: list[str], article_title: str
) -> tuple[list[str], list[str]]:
    """
    Peel a chapter-opener table of contents off the start of extracted lines.

    Book I chapter first pages list section titles in a multi-column TOC.
    Those short lines currently become spurious <h2>s; strip them here so
    they can be rendered as sidebar deep links instead.

    Returns (toc_labels, body_lines). If no TOC is detected, toc is empty
    and body_lines is the original list.
    """
    if not lines or len(lines) < 5:
        return [], lines

    prose_idx: int | None = None
    for i, line in enumerate(lines):
        # Rich structural markers mean this is not a chapter-opener TOC page
        if line.startswith("\x02"):
            return [], lines
        if _is_chapter_toc_prose(line):
            prose_idx = i
            break
    # Need a cluster of short TOC lines before prose
    if prose_idx is None or prose_idx < 3:
        return [], lines

    at = normalize_text(article_title).lower() if article_title else ""
    toc: list[str] = []
    for line in lines[:prose_idx]:
        L = line.strip()
        if not L:
            continue
        if at and normalize_text(L).lower() == at:
            continue
        if is_running_header(L, article_title, near_page_top=True):
            continue
        if _is_chapter_toc_garbage(L):
            continue
        # Short label / ALL CAPS move name
        if looks_like_heading(L) or (
            len(L) <= 40
            and L[0:1].isupper()
            and not L.endswith((".", ";", ","))
        ):
            toc.append(L)
        elif len(L) > 55:
            # Unexpected long non-prose before prose_idx — abort
            return [], lines

    if len(toc) < 3:
        return [], lines

    body = lines[prose_idx:]
    # Drop column-glued junk that sat between TOC and real prose
    while body and _is_chapter_toc_garbage(body[0]):
        body = body[1:]
    if not body:
        return [], lines
    return toc, body


def match_toc_to_sections(
    toc_labels: list[str], sections: list[dict]
) -> list[dict]:
    """
    Match chapter TOC labels to real body section anchors.

    TOC text is often truncated by multi-column PDF extraction
    (\"The flow\" → body heading \"The flow of play\"). Prefer the body's
    full name and id for the sidebar.

    Returns list of {name, id} for sidebar deep links (document order).
    """
    if not toc_labels or not sections:
        return []

    used: set[str] = set()
    out: list[dict] = []

    for label in toc_labels:
        norm = normalize_section_key(label)
        if not norm or len(norm) < 2:
            continue
        # Move-style TOC labels (BOLSTER, TRADE &, DEATH'S) → prefer ALL-CAPS sections
        letters = [c for c in label if c.isalpha()]
        toc_caps = bool(letters) and (
            sum(1 for c in letters if c.isupper()) / len(letters) >= 0.75
        )
        best: dict | None = None
        best_score = 0
        for sec in sections:
            sid = sec.get("id") or ""
            if not sid or sid in used:
                continue
            sname = sec.get("name") or ""
            sn = sec.get("norm") or normalize_section_key(sname)
            if not sn:
                continue
            sec_letters = [c for c in sname if c.isalpha()]
            sec_caps = bool(sec_letters) and (
                sum(1 for c in sec_letters if c.isupper()) / len(sec_letters) >= 0.6
            )
            # Don't let "TRADE &" latch onto prose like "Trade with Gordin's…"
            if toc_caps and not sec_caps:
                continue
            score = 0
            if sn == norm:
                score = 100
            elif sn.startswith(norm) and len(norm) >= 3:
                score = 80 + min(len(norm), 20)
            elif norm.startswith(sn) and len(sn) >= 4:
                score = 55 + min(len(sn), 15)
            elif len(norm) >= 6 and (
                sn.startswith(norm + " ") or f" {norm} " in f" {sn} "
            ):
                score = 45
            # Compact ALL-CAPS moves: "deaths door" vs "death s door"
            if score < 40:
                sn_c = sn.replace(" ", "")
                n_c = norm.replace(" ", "")
                if sn_c == n_c:
                    score = 95
                elif sn_c.startswith(n_c) and len(n_c) >= 4:
                    score = 70
            if toc_caps and sec_caps and score > 0:
                score += 10
            if score > best_score:
                best_score = score
                best = sec
        if best and best_score >= 40:
            used.add(best["id"])
            out.append({"name": best["name"], "id": best["id"]})

    return out


def extract_article_lines(
    doc: fitz.Document,
    start_page: int,
    end_page: int,
    article_title: str,
    *,
    icon_dir: Path | None = None,
) -> list[str]:
    """Rich (span/drawing-aware) line extraction for an article page range."""
    raw: list[str] = []
    state: dict = {}
    if icon_dir is not None:
        state["icon_dir"] = Path(icon_dir)
    first = True
    for pno in range(start_page - 1, end_page):
        if pno < 0 or pno >= doc.page_count:
            continue
        raw.extend(
            extract_page_rich(
                doc[pno],
                article_title=article_title,
                first_page=first,
                state=state,
            )
        )
        first = False
    return merge_wrapped_lines(raw)


def _is_all_caps_label(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 3 or len(line) > 55:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= 0.85


def _is_require_header(line: str) -> bool:
    """True for steading-improvement requirement section headers."""
    L = line.strip()
    if not L:
        return False
    # "Requires all of the following" / "Requires both:" / "Requires getting…"
    # Not prose like "requires ___: if you don't meet the requirements…"
    if re.match(
        r"^Requires?\s+("
        r"all\b|both\b|either\b|one\b|any\b|\d+\b|"
        r"getting\b|the\s+following\b"
        r")",
        L,
        re.I,
    ):
        return True
    # "And then, all of the following" / "And then each of these:"
    if re.match(
        r"^And then,?\s*(?:all |each )?of (?:the following|these)\b",
        L,
        re.I,
    ):
        return True
    # "And either of these, to germinate the seeds:"
    if re.match(
        r"^And (?:either|one|any) of (?:the following|these)\b",
        L,
        re.I,
    ):
        return True
    return False


def _is_require_end(line: str) -> bool:
    L = line.strip()
    if re.match(r"^When you (?:mark|meet) all the requirements\b", L, re.I):
        return True
    if re.match(r"^Henceforth\b", L, re.I):
        return True
    return False


def _is_require_item(line: str) -> bool:
    line = strip_markers(line)
    if not line or not line.strip():
        return False
    L = line.strip()
    if _is_require_header(L) or _is_require_end(L):
        return False
    if re.fullmatch(r"steading improvement", L, re.I):
        return False
    # Don't swallow the next major section
    if L.lower() in {
        "terrain",
        "questions",
        "hooks",
        "places",
        "dangers",
        "discoveries",
        "impressions",
        "lore",
        "names",
        "people",
        "secrets",
        "size",
        "population",
        "prosperity",
        "defenses",
        "resources",
    }:
        return False
    if ROLL_HEADER_RE.match(L) or ROLL_HEADER_DICE_ONLY.match(L):
        return False
    if HP_LINE_RE.search(L):
        return False
    if looks_like_tag_line(L):
        return False
    # Next improvement title in ALL CAPS after a complete block
    if _is_all_caps_label(L) and len(L.split()) <= 4:
        return False
    return len(L) < 220


def render_check_list(
    items: list[str],
    link_fn,
    list_id: str,
) -> str:
    """Interactive checkbox list for steading improvement requirements."""
    if not items:
        return ""
    rows = []
    for idx, item in enumerate(items):
        cid = f"{list_id}-{idx}"
        rows.append(
            f'<li class="check-item">'
            f'<label for="{html.escape(cid)}">'
            f'<input type="checkbox" class="wiki-check" id="{html.escape(cid)}" '
            f'data-check-id="{html.escape(cid)}"> '
            f"<span>{link_fn(item)}</span>"
            f"</label></li>"
        )
    return (
        f'<ul class="check-list" data-check-list="{html.escape(list_id)}">'
        f'{"".join(rows)}</ul>'
    )


def render_mark_track(n: int, list_id: str, label: str = "Progress") -> str:
    """Horizontal checkbox track for major-arcana progress marks (☐ ☐ ☐ …)."""
    if n <= 0:
        return ""
    steps = []
    for idx in range(n):
        cid = f"{list_id}-{idx}"
        steps.append(
            f'<label class="track-step" for="{html.escape(cid)}" title="Mark {idx + 1}">'
            f'<input type="checkbox" class="wiki-check" id="{html.escape(cid)}" '
            f'data-check-id="{html.escape(cid)}" aria-label="Mark {idx + 1}">'
            f"</label>"
        )
    return (
        f'<div class="arcana-track" data-check-list="{html.escape(list_id)}">'
        f'<span class="track-label">{html.escape(label)}</span>'
        f'<span class="track-steps">{"".join(steps)}</span>'
        f"</div>"
    )


def _benefit_continues(line: str) -> bool:
    """Prose that continues a steading-improvement benefit section."""
    L = line.strip()
    if not L:
        return False
    if _is_require_header(L) or _is_all_caps_label(L):
        return False
    if re.fullmatch(r"steading improvement", L, re.I):
        return False
    if ROLL_HEADER_RE.match(L) or ROLL_HEADER_DICE_ONLY.match(L):
        return False
    if re.match(
        r"^(When you|Henceforth|Also,|Stonetop gains|Every |The steading|"
        r"You can |You may |This |These )",
        L,
        re.I,
    ):
        return True
    # Longer prose that isn't a short section heading
    if len(L) > 50 and not (looks_like_heading(L) and len(L.split()) <= 5):
        return True
    return False


def _render_steading_block_rich(
    lines: list[str],
    start: int,
    link_fn,
    anchors: "AnchorRegistry",
    next_check_id,
    rich_fn,
) -> tuple[str, int]:
    """
    Render a steading-improvement block from the rich marker stream.

    Gathers the block's marker lines (checkbox titles/requirements plus the
    plain group-headers, blurb, and benefit paragraphs between them),
    converts them to the plain-text form that ``try_parse_improvement_block``
    understands, and delegates to it so multi-group requirements ("And then,
    all of the following…"), blurbs, and wrapped titles render correctly.

    Returns (html, next_index).
    """
    n = len(lines)
    i = start
    plain: list[str] = ["steading improvement"]
    while i < n:
        L = lines[i]
        if L.startswith(M_C):
            raw = L[len(M_C):]
            bp, full = split_bold_prefix(raw)
            if bp and _is_all_caps_label(bp):
                # Checkbox on a title line: emit the name, then any trailing
                # text (a blurb or a group-header) as its own line.
                plain.append(bp)
                rest = full[len(bp):].strip()
                if rest:
                    plain.append(rest)
            else:
                plain.append(strip_markers(L).strip())
            i += 1
            continue
        if L.startswith("\x02"):
            break  # heading / table / other structure ends the block
        plain.append(strip_markers(L).strip())
        i += 1

    parsed = try_parse_improvement_block(plain, 0, link_fn, anchors, next_check_id)
    if parsed is not None:
        html_block, consumed = parsed
        # Anything the parser did not consume → plain paragraphs
        tail = "".join(
            f"<p>{link_fn(t)}</p>" for t in plain[consumed:] if t.strip()
        )
        return html_block + tail, i

    # Fallback: render whatever we gathered as paragraphs
    body = "".join(f"<p>{link_fn(t)}</p>" for t in plain[1:] if t.strip())
    return f'<div class="steading-improvement">{body}</div>', i


def _render_artifact_block_rich(
    lines: list[str],
    start: int,
    title: str,
    link_fn,
    anchors: "AnchorRegistry",
) -> tuple[str, int]:
    """
    Render a tagged artifact / discovery as a card block.

    Layout (from the rich stream):
        M_H3  <title>            (already consumed → passed as ``title``)
        <arcana tag line>        ("magical", "hand, magical, +1 damage", …)
        <description prose …>
        [Something interesting: …]
        [Something useful: …]
    Prose lines are re-flowed into paragraphs, breaking before the "Something
    …" notes and at sentence boundaries. Returns (html, next_index).
    """
    n = len(lines)
    i = start + 1  # skip the heading line
    # Tag line: de-tokenized (styled italic via CSS); strip a stray lead comma
    tags = re.sub(r"^[\x04\x06,\s]+", "", strip_markers(lines[i])).strip()
    i += 1

    # Body keeps inline formatting sentinels for rendering
    body: list[str] = []
    while i < n and not lines[i].startswith("\x02"):
        b = lines[i].strip()
        if b:
            body.append(b)
        i += 1

    # Re-flow into paragraphs; decisions use de-tokenized text
    paras: list[tuple[str, bool]] = []  # (token_text, is_note)
    for L in body:
        dL = _defmt(L)
        if not dL:
            continue
        note = bool(
            re.match(r"^(Something (?:interesting|useful)\b|When you\b)", dL, re.I)
        )
        if not paras:
            paras.append((L, note))
        elif note:
            paras.append((L, True))
        elif _defmt(paras[-1][0]).rstrip().endswith((".", "!", "?")) and dL[:1].isupper():
            paras.append((L, False))
        else:
            prev, pn = paras[-1]
            body_pv, tail_pv = _split_trailing_fmt(prev.rstrip())
            lead_pv, rest_pv = _split_leading_fmt(L)
            if body_pv.endswith("-") and rest_pv[:1].islower():
                paras[-1] = (body_pv[:-1] + _cancel_fmt_seam(tail_pv, lead_pv) + rest_pv, pn)
            else:
                paras[-1] = (prev + " " + L, pn)

    hid = anchors.add(title.rstrip(":"))
    parts = [
        f'<div class="discovery-block" id="{html.escape(hid)}">',
        f'<h3 class="discovery-name">{html.escape(title.rstrip(":"))}</h3>',
    ]
    if tags:
        parts.append(f'<p class="discovery-tags">{link_fn(tags)}</p>')
    for text, note in paras:
        cls = ' class="discovery-note"' if note else ""
        parts.append(f"<p{cls}>{link_fn(text)}</p>")
    parts.append("</div>")
    return "".join(parts), i


def try_parse_improvement_block(
    lines: list[str],
    start: int,
    link_fn,
    anchors: AnchorRegistry,
    next_check_id,
) -> tuple[str, int] | None:
    """
    Parse a steading improvement (or similar) requirement block with checkboxes.

    Returns (html, next_index) or None if lines[start] is not a block start.
    """
    n = len(lines)
    if start >= n:
        return None
    line = lines[start].strip()
    starts_si = bool(re.fullmatch(r"steading improvement", line, re.I))
    starts_req = _is_require_header(line) and bool(
        re.match(r"^Requires?\b", line, re.I)
    )
    starts_caps = _is_all_caps_label(line) and any(
        _is_require_header(lines[start + k])
        and re.match(r"^Requires?\b", lines[start + k].strip(), re.I)
        for k in range(1, min(5, n - start))
    )
    if not (starts_si or starts_req or starts_caps):
        return None

    j = start
    kind = ""
    if starts_si:
        kind = "Steading improvement"
        j += 1

    title_parts: list[str] = []
    while (
        j < n
        and _is_all_caps_label(lines[j])
        and not _is_require_header(lines[j])
    ):
        title_parts.append(lines[j].strip())
        j += 1
        if len(title_parts) >= 4:
            break
    title = " ".join(title_parts)

    blurb = ""
    if (
        j < n
        and not _is_require_header(lines[j])
        and not _is_require_end(lines[j])
        and not _is_all_caps_label(lines[j])
        and len(lines[j]) < 160
        and any(
            _is_require_header(lines[j + k])
            and re.match(r"^Requires?\b", lines[j + k].strip(), re.I)
            for k in range(0, min(3, n - j))
        )
    ):
        if not _is_require_header(lines[j]):
            blurb = lines[j].strip()
            j += 1

    if j >= n or not (
        _is_require_header(lines[j])
        and re.match(r"^Requires?\b", lines[j].strip(), re.I)
    ):
        return None

    block_start = j
    hid = anchors.add(title or "Steading improvement")
    parts = [f'<div class="steading-improvement" id="{html.escape(hid)}">']
    if kind or starts_si:
        parts.append('<p class="si-kind">Steading improvement</p>')
    if title:
        parts.append(f'<h3 class="si-title">{html.escape(title)}</h3>')
    if blurb:
        parts.append(f'<p class="si-blurb">{link_fn(blurb)}</p>')

    n_checks = 0
    # Requirement groups (Requires… / And then, all of the following…)
    while j < n and _is_require_header(lines[j]):
        header = lines[j].strip().rstrip(":")
        j += 1
        if header.endswith("?"):
            parts.append(f'<p class="si-requires">{html.escape(header)}</p>')
        else:
            parts.append(f'<p class="si-requires">{html.escape(header)}:</p>')
        items: list[str] = []
        while j < n and _is_require_item(lines[j]):
            items.append(strip_markers(lines[j]).strip())
            j += 1
        if items:
            # Guard against runaway false positives (prose treated as items)
            if len(items) > 30:
                return None
            n_checks += len(items)
            parts.append(
                render_check_list(items, link_fn, next_check_id(title or "req"))
            )

    # Must have actually produced checklist items and advanced
    if n_checks == 0 or j <= block_start:
        return None

    # Benefits: When you mark… / Henceforth…
    while j < n and (
        re.match(r"^When you (?:mark|meet) all the requirements\b", lines[j], re.I)
        or re.match(r"^Henceforth\b", lines[j], re.I)
        or (
            parts
            and _benefit_continues(lines[j])
            and re.match(r"^(Also,|Stonetop gains|Every )", lines[j], re.I)
        )
    ):
        parts.append(f"<p>{link_fn(lines[j])}</p>")
        j += 1
        while j < n and _benefit_continues(lines[j]):
            # Stop if this looks like a new short section title
            L2 = lines[j].strip()
            if looks_like_heading(L2) and len(L2.split()) <= 4 and len(L2) < 40:
                if not re.match(
                    r"^(When |Henceforth|Also|Stonetop|Every |The steading)",
                    L2,
                    re.I,
                ):
                    break
            parts.append(f"<p>{link_fn(lines[j])}</p>")
            j += 1

    parts.append("</div>")
    if j <= start:
        return None
    return "\n".join(parts), j


def structure_html(
    lines: list[str],
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    anchors: AnchorRegistry | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, list[dict]]:
    """Turn cleaned lines into structured HTML (tables, stat blocks, lists).

    Returns (html, sections) where sections is [{id, name, norm}, ...].
    """
    lines = list(lines)  # this function rewrites entries in place
    out: list[str] = []
    i = 0
    n = len(lines)
    if anchors is None:
        anchors = AnchorRegistry()
    link_kw = {
        "lookups": lookups,
        "section_indexes": section_indexes,
        "current_book": current_book,
    }
    check_list_n = 0

    def peek(k=0):
        j = i + k
        return lines[j] if 0 <= j < n else ""

    def link(text: str) -> str:
        return linkify_pages(
            text, lookup, current_slug, section_index, **link_kw
        )

    def next_check_id(prefix: str = "req") -> str:
        nonlocal check_list_n
        check_list_n += 1
        base = slugify_id(prefix) if prefix else "req"
        return f"{base}-{check_list_n}"

    rich_mode = any(l.startswith("\x02") for l in lines)
    forced_creature: set[int] = set()
    pending_icon: str | None = None  # images/… path from M_ICON

    def take_icon_html() -> str:
        nonlocal pending_icon
        if not pending_icon:
            return ""
        img = book_icon_img_html(pending_icon, rel_prefix="../")
        pending_icon = None
        return img

    while i < n:
        line = lines[i]

        # --- Structural markers from rich PDF extraction ---
        if line.startswith("\x02"):
            if line == M_HR:
                # Collapse consecutive rules; skip under headings (CSS border);
                # drop hairlines after monster cards (layout between stat blocks).
                prev_html = out[-1] if out else ""
                under_heading = bool(re.search(r"</h[1-4]>\s*$", prev_html))
                prev_stat = bool(out and 'class="stat-block"' in out[-1])
                if under_heading or prev_stat or (out and out[-1] == "<hr>"):
                    i += 1
                    continue
                out.append("<hr>")
                pending_icon = None
                i += 1
                continue
            if line.startswith(M_ICON):
                # Keep the first of back-to-back duplicate icons
                path = line[len(M_ICON):].strip() or None
                if path:
                    pending_icon = path
                i += 1
                while i < n and lines[i].startswith(M_ICON):
                    i += 1
                continue
            if line == M_BOX:
                inner: list[str] = []
                j = i + 1
                while j < n and lines[j] != M_ENDBOX:
                    inner.append(lines[j])
                    j += 1
                inner_html, _ = structure_html(
                    inner,
                    article_title,
                    lookup,
                    articles,
                    current_slug,
                    section_index=section_index,
                    anchors=anchors,
                    lookups=lookups,
                    section_indexes=section_indexes,
                    current_book=current_book,
                )
                out.append(f'<div class="infobox">{inner_html}</div>')
                i = j + 1
                continue
            if line == M_ENDBOX:
                i += 1
                continue
            if line.startswith(M_H2):
                txt = line[len(M_H2):].strip()
                if (
                    is_running_header(txt, article_title, near_page_top=True)
                    or normalize_text(txt).lower()
                    == normalize_text(article_title).lower()
                ):
                    pending_icon = None
                    i += 1
                    continue
                hid = anchors.add(txt.rstrip(":"))
                ic = take_icon_html()
                out.append(
                    f'<h2 id="{html.escape(hid)}">'
                    f"{ic}{html.escape(txt.rstrip(':'))}</h2>"
                )
                i += 1
                continue
            if line.startswith(M_TH):
                txt = line[len(M_TH):].strip()
                # "steading improvement" label → custom improvement block
                if txt.lower() == "steading improvement":
                    pending_icon = None
                    block_html, i = _render_steading_block_rich(
                        lines, i + 1, link, anchors, next_check_id,
                        lambda t: render_rich_text(t, link),
                    )
                    out.append(block_html)
                    continue
                hid = anchors.add(txt)
                ic = take_icon_html()
                out.append(
                    f'<h3 id="{html.escape(hid)}" class="table-heading">'
                    f"{ic}{html.escape(txt)}</h3>"
                )
                i += 1
                continue
            if line.startswith(M_H4):
                txt = line[len(M_H4):].strip()
                pending_icon = None
                out.append(f"<h4>{html.escape(txt)}</h4>")
                i += 1
                continue
            if line.startswith(M_H3):
                bare = line[len(M_H3):].strip()
                # Creature/threat block: HP-carrying monsters, and also
                # HP-less threats (Fire, Gylglyd vines, The Forest's Wrath)
                # that lead with tags, Damage, Instinct, or "Threat (…)".
                creature = False
                for k in range(1, 6):
                    cand = peek(k)
                    if not cand:
                        break
                    if cand.startswith(M_H3):
                        continue
                    if cand.startswith("\x02"):
                        break
                    c = strip_markers(cand)
                    if c.startswith("• "):
                        continue
                    low = c.lower()
                    if (
                        HP_LINE_RE.search(c)
                        or re.match(r"^(damage|instinct|threat)\b", low)
                        or (k <= 3 and looks_like_tag_line(c))
                    ):
                        creature = True
                        break
                if creature:
                    forced_creature.add(i)
                    lines[i] = bare
                    # keep pending_icon for the stat-block name
                    continue
                # Artifact / tagged-discovery block: heading immediately
                # followed by an item tag line ("magical", "hand, magical,
                # +1 damage", ", immobile"). Render as a card, not a heading.
                nxt = peek(1)
                if (
                    nxt
                    and not nxt.startswith("\x02")
                    and _is_pure_arcana_tag_line(strip_markers(nxt))
                ):
                    ic = take_icon_html()
                    block_html, i = _render_artifact_block_rich(
                        lines, i, bare, link, anchors
                    )
                    if ic and block_html.startswith("<"):
                        # Prefixed title inside the artifact card if present
                        block_html = block_html.replace(
                            f">{html.escape(bare)}",
                            f">{ic}{html.escape(bare)}",
                            1,
                        )
                    out.append(block_html)
                    continue
                # Hazard card: icon heading + prose until next rule/creature
                # (e.g. Vitrified horrors).
                ic = take_icon_html()
                body_parts: list[str] = []
                j = i + 1
                while j < n:
                    L2 = lines[j]
                    if L2 == M_HR or L2.startswith(
                        (M_ICON, M_H2, M_H3, M_VT, M_TH, M_BOX)
                    ):
                        break
                    if L2.startswith("\x02") and not L2.startswith(
                        (M_B, M_B2, M_Q, M_E, M_C)
                    ):
                        break
                    if L2.startswith((M_B, M_B2, M_Q, M_E)):
                        for pref in (M_B2, M_Q, M_E, M_B):
                            if L2.startswith(pref):
                                body_parts.append("• " + L2[len(pref):].strip())
                                break
                    else:
                        body_parts.append(_defmt(L2))
                    j += 1
                if ic:
                    hid = anchors.add(bare.rstrip(":"))
                    parts_h = [
                        f'<div class="hazard-block" id="{html.escape(hid)}">',
                        f'<h3 class="stat-name">{ic}'
                        f"{html.escape(bare.rstrip(':'))}</h3>",
                    ]
                    bi = 0
                    while bi < len(body_parts):
                        if body_parts[bi].startswith("• "):
                            items_h: list[str] = []
                            while bi < len(body_parts) and body_parts[bi].startswith(
                                "• "
                            ):
                                items_h.append(body_parts[bi][2:].strip())
                                bi += 1
                            parts_h.append(
                                '<ul class="bullets">'
                                + "".join(
                                    f"<li>{render_rich_text(it, link)}</li>"
                                    for it in items_h
                                )
                                + "</ul>"
                            )
                        else:
                            parts_h.append(
                                f"<p>{render_rich_text(body_parts[bi], link)}</p>"
                            )
                            bi += 1
                    parts_h.append("</div>")
                    out.append("".join(parts_h))
                    i = j
                    continue
                hid = anchors.add(bare.rstrip(":"))
                out.append(
                    f'<h3 id="{html.escape(hid)}">'
                    f"{ic}{html.escape(bare.rstrip(':'))}</h3>"
                )
                i += 1
                continue
            if line.startswith(M_VA):
                frag = line[len(M_VA):].strip()
                prev = out[-1] if out else ""
                k = prev.rfind('</td><td class="val">')
                if 'class="value-table"' in prev and k != -1:
                    out[-1] = prev[:k] + " " + link(frag) + prev[k:]
                elif frag:
                    out.append(f"<p>{link(frag)}</p>")
                i += 1
                continue
            if line.startswith((M_VT, M_VR, M_VF)):
                vt_title: str | None = None
                if line.startswith(M_VT):
                    vt_title = line[len(M_VT):].strip()
                    i += 1
                rows_v: list[tuple[str, str]] = []
                notes_v: list[str] = []
                while i < n and lines[i].startswith((M_VR, M_VF)):
                    L = lines[i]
                    if L.startswith(M_VR):
                        body_, _, val_ = L[len(M_VR):].partition("\x03")
                        rows_v.append((body_.strip(), val_.strip()))
                    else:
                        notes_v.append(L[len(M_VF):].strip())
                    i += 1
                if (
                    vt_title is None
                    and rows_v
                    and out
                    and 'class="value-table"' in out[-1]
                ):
                    # continuation of the table we just rendered
                    row_html = "".join(
                        f"<tr><td>{link(item)}</td>"
                        f'<td class="val">{html.escape(val)}</td></tr>'
                        for item, val in rows_v
                    )
                    out[-1] = out[-1].replace(
                        "</tbody></table>", row_html + "</tbody></table>", 1
                    )
                    continue
                pretty = re.sub(r"\s+", " ", (vt_title or "Value")).strip()
                if not pretty.lower().endswith("value"):
                    pretty = pretty + " value"
                out.append(
                    render_value_table(
                        pretty.title(),
                        rows_v,
                        lookup,
                        current_slug,
                        section_index,
                        link_kw=link_kw,
                        notes=notes_v,
                    )
                )
                continue
            if line[:3] == M_B or line.startswith(M_B2):
                # bullet run, possibly with tier-2 items nested under entries
                items: list[tuple[int, str]] = []
                while i < n and (
                    lines[i][:3] == M_B or lines[i].startswith(M_B2)
                ):
                    L = lines[i]
                    if L.startswith(M_B2):
                        items.append((2, L[len(M_B2):].strip()))
                    else:
                        items.append((1, L[3:].strip()))
                    i += 1
                parts = ['<ul class="bullets">']
                open_li = False
                open_sub = False
                for lvl, it in items:
                    h = render_rich_text(it, link)
                    if lvl == 1:
                        if open_sub:
                            parts.append("</ul>")
                            open_sub = False
                        if open_li:
                            parts.append("</li>")
                        parts.append(f"<li>{h}")
                        open_li = True
                    else:
                        if not open_li:
                            parts.append("<li>")
                            open_li = True
                        if not open_sub:
                            parts.append('<ul class="bullets">')
                            open_sub = True
                        parts.append(f"<li>{h}</li>")
                if open_sub:
                    parts.append("</ul>")
                if open_li:
                    parts.append("</li>")
                parts.append("</ul>")
                out.append("".join(parts))
                continue
            if line[:3] in (M_Q, M_E):
                kind3 = line[:3]
                cls = {M_Q: "questions", M_E: "ellipsis"}[kind3]
                items_q = []
                while i < n and lines[i][:3] == kind3:
                    items_q.append(lines[i][3:].strip())
                    i += 1
                out.append(
                    f'<ul class="{cls}">'
                    + "".join(
                        f"<li>{render_rich_text(it, link)}</li>"
                        for it in items_q
                    )
                    + "</ul>"
                )
                continue
            if line.startswith(M_C):
                items = []
                while i < n and lines[i].startswith(M_C):
                    items.append(lines[i][len(M_C):].strip())
                    i += 1
                out.append(
                    render_check_list(
                        items,
                        lambda t: render_rich_text(t, link),
                        next_check_id(current_slug or "chk"),
                    )
                )
                continue
            # Unknown marker — strip and reprocess as plain text
            lines[i] = strip_markers(line)
            continue

        # Below here `line` is a non-marker line. Structural parsers run on the
        # de-tokenized text; `orig_line` keeps inline formatting for prose.
        orig_line = line
        line = _defmt(line)

        # --- Steading improvement / requirement checklists ---
        parsed = try_parse_improvement_block(
            lines, i, link, anchors, next_check_id
        )
        if parsed is not None:
            html_block, i = parsed
            out.append(html_block)
            continue
        # Lone "steading improvement" label with no following Requires — skip
        if re.fullmatch(r"steading improvement", line.strip(), re.I):
            i += 1
            continue

        # --- Value / price tables (Trade & Barter, services, etc.) ---
        # "goods value", "weapons armor value", or "weapons armor" + "value"/ "&"
        val_title = None
        if looks_like_value_header(line):
            val_title = re.sub(r"\s+", " ", line.strip())
            # drop trailing "value" for display? keep full
            i += 1
            if peek(0) in {"&", "and"}:
                i += 1
        elif (
            re.match(
                r"^(weapons?|goods|coin|food|services)(\s+\w+){0,3}$",
                line.strip(),
                re.I,
            )
            and peek(0).lower() in {"value", "&", "and"}
        ):
            # "weapons armor" then "value" or "&" then maybe "value" implied
            parts = [line.strip()]
            i += 1
            while i < n and lines[i].lower() in {"&", "and", "value"}:
                if lines[i].lower() == "value":
                    parts.append("value")
                i += 1
            val_title = " ".join(parts) if "value" in " ".join(parts).lower() else (
                " ".join(parts) + " value"
            )
        if val_title:
            rows: list[tuple[str, str]] = []
            pending_val: str | None = None
            while i < n:
                L = lines[i]
                if L.startswith("\x02"):
                    break
                # stop at new section/table/stat
                if (
                    looks_like_value_header(L)
                    or ROLL_HEADER_RE.match(L)
                    or ROLL_HEADER_DICE_ONLY.match(L)
                    or (
                        looks_like_heading(L)
                        and not VALUE_ROW_RE.match(L)
                        and not LONE_VALUE_RE.match(L)
                        and len(L) < 40
                    )
                    or looks_like_tag_line(L)
                    or HP_LINE_RE.search(L)
                ):
                    # don't stop on short goods names that look like headings
                    if looks_like_heading(L) and VALUE_ROW_RE.match(L):
                        pass
                    elif looks_like_value_header(L) or ROLL_HEADER_RE.match(L) or ROLL_HEADER_DICE_ONLY.match(L) or looks_like_tag_line(L) or HP_LINE_RE.search(L):
                        break
                    elif looks_like_heading(L) and L.lower() not in {
                        "block & tackle",
                        "wheelbarrow",
                        "cartload of timber (immobile)",
                    }:
                        # section headings like "Special items", "Moves"
                        if not re.search(r"\d|free|\+|armor|weapon|tool|cart|mirror|lock|silver|apartment|house|killing|guide|crew|prospector|servant|bodyguard|engineer|assassin", L, re.I):
                            if L[0].isupper() and len(L.split()) <= 4 and not VALUE_ROW_RE.match(L):
                                break

                # Lone value on its own line (column layout: value | item)
                if LONE_VALUE_RE.match(L):
                    pending_val = LONE_VALUE_RE.match(L).group(1)
                    i += 1
                    continue

                parsed = parse_value_row(L)
                if parsed:
                    item, val = parsed
                    i += 1
                    # continuations of item description (no value yet on next)
                    while (
                        i < n
                        and should_join(item, lines[i])
                        and not parse_value_row(lines[i])
                        and not LONE_VALUE_RE.match(lines[i])
                        and not looks_like_value_header(lines[i])
                    ):
                        item = item + " " + lines[i]
                        i += 1
                    rows.append((item, val))
                    pending_val = None
                    continue

                # pending value from previous lone digit + this is the item
                if pending_val and not looks_like_heading(L):
                    item = L
                    i += 1
                    while (
                        i < n
                        and should_join(item, lines[i])
                        and not VALUE_ROW_RE.match(lines[i])
                        and not LONE_VALUE_RE.match(lines[i])
                    ):
                        item = item + " " + lines[i]
                        i += 1
                    rows.append((item, pending_val))
                    pending_val = None
                    continue

                # item without trailing value yet; peek for lone value next
                if (
                    peek(1)
                    and LONE_VALUE_RE.match(peek(1))
                    and not looks_like_value_header(L)
                    and not L.startswith("When ")
                ):
                    item = L
                    i += 1
                    val = LONE_VALUE_RE.match(lines[i]).group(1)
                    i += 1
                    rows.append((item, val))
                    continue

                # Item with no price on the line (Hauberk, Spare parts) — include with "—"
                # only if clearly still inside the table (next line is a value row or header)
                if (
                    rows
                    and not looks_like_heading(L)
                    and not looks_like_value_header(L)
                    and not L.startswith("When ")
                    and (
                        (peek(1) and parse_value_row(peek(1)))
                        or (peek(1) and looks_like_value_header(peek(1)))
                        or (peek(1) and LONE_VALUE_RE.match(peek(1)))
                    )
                ):
                    rows.append((L, "—"))
                    i += 1
                    continue

                # soft-join continuation of last item (no value on wrapped line)
                if rows and should_join(rows[-1][0], L) and not parse_value_row(L):
                    item, val = rows[-1]
                    rows[-1] = (item + " " + L, val)
                    i += 1
                    continue

                break

            if rows:
                # Prettier title
                title = re.sub(r"\s+", " ", val_title)
                title = re.sub(r"\s*&\s*", " & ", title)
                if not title.lower().endswith("value"):
                    title = title + " value"
                out.append(
                    render_value_table(
                        title.title(),
                        rows,
                        lookup,
                        current_slug,
                        section_index,
                        link_kw=link_kw,
                    )
                )
                continue
            # fall through if no rows collected — reprocess line as normal
            # (i may have advanced; if no rows, emit title as heading)
            out.append(f"<h2>{html.escape(val_title)}</h2>")
            continue

        # --- Roll table header ---
        # Patterns: "1d12 theme", "theme"+"1d12", "1d12" alone, "label 1d12"
        # (roll tables are CSS-styled, so parse/render on de-tokenized text)
        dpeek1 = _defmt(peek(1))
        dice = label = None
        m = ROLL_HEADER_RE.match(line)
        m_rev = ROLL_HEADER_REV_RE.match(line)
        if m:
            dice, label = m.group(1), m.group(2).strip()
        elif m_rev and not ENTRY_RE.match(line):
            label, dice = m_rev.group(1).strip(), m_rev.group(2)
        elif (
            len(line) <= 40
            and not ENTRY_RE.match(line)
            and ROLL_HEADER_DICE_ONLY.match(dpeek1)
        ):
            # "theme" then "1d12"
            label = line
            dice = dpeek1
            i += 1  # consume dice line below after setting
        elif ROLL_HEADER_DICE_ONLY.match(line):
            dice = line
            # label may be previous short heading already emitted, or next line
            if out and re.search(r"<h2>[^<]{1,40}</h2>\s*$", out[-1]):
                label = re.sub(r"</?h2>", "", out[-1]).strip()
                out.pop()
            elif dpeek1 and len(dpeek1) < 40 and not ENTRY_RE.match(dpeek1):
                # only treat next as label if it does NOT look like entry start
                if not re.match(r"^\d+", dpeek1):
                    i += 1
                    label = dpeek1
                else:
                    label = "result"
            else:
                label = "result"
        if dice:
            i += 1  # move past header line(s); label+dice path already advanced once
            entries: list[tuple[str, str]] = []
            while i < n:
                # Hairlines under dice headers are decorative — skip them so
                # the entry list is not cut off (which broke roll tables).
                if lines[i] == M_HR:
                    i += 1
                    continue
                if lines[i].startswith(M_ICON):
                    i += 1
                    continue
                if lines[i].startswith("\x02"):
                    break
                cur = _defmt(lines[i])
                e = ENTRY_RE.match(cur)
                if e:
                    num = e.group(1) + (f"-{e.group(2)}" if e.group(2) else "")
                    body = e.group(3).strip()
                    i += 1
                    # continuations (also skip decorative HRs mid-entry)
                    while i < n:
                        if lines[i] == M_HR:
                            # Peek past hairlines: a following roll header means
                            # the next table starts — do not consume the HR/header.
                            j = i + 1
                            while j < n and (
                                lines[j] == M_HR or lines[j].startswith(M_ICON)
                            ):
                                j += 1
                            if j < n and looks_like_roll_header(_defmt(lines[j])):
                                break
                            i += 1
                            continue
                        if lines[i].startswith("\x02"):
                            break
                        nxt = _defmt(lines[i])
                        if ENTRY_RE.match(nxt):
                            break
                        # Next dice table (e.g. "1d6 signs" after size row 6)
                        # must not be glued into this row — that merged tables.
                        if (
                            looks_like_roll_header(nxt)
                            or looks_like_heading(nxt)
                            or looks_like_tag_line(nxt)
                            or looks_like_value_header(nxt)
                        ):
                            break
                        # Always glue non-entry lines into the current row
                        # (wrapped descriptions; e.g. wonder #9's second sentence).
                        if nxt.endswith("-") and not nxt.endswith(("–", "—", "--")):
                            body = body.rstrip("-") + nxt.lstrip("-")
                        elif body.endswith(("–", "—")):
                            body = body + nxt
                        else:
                            body = body + " " + nxt
                        i += 1
                    entries.append((num, body))
                    continue
                # stop at next section/table/stat
                if (
                    looks_like_heading(cur)
                    or looks_like_roll_header(cur)
                    or looks_like_tag_line(cur)
                ):
                    break
                # orphan continuation of last entry (before next numbered row)
                if entries and not ENTRY_RE.match(cur):
                    if (
                        looks_like_roll_header(cur)
                        or looks_like_heading(cur)
                        or looks_like_tag_line(cur)
                    ):
                        break
                    if cur.endswith(":") and cur[0:1].isupper() and len(cur) < 60:
                        break
                    num, body = entries[-1]
                    entries[-1] = (num, body + " " + cur)
                    i += 1
                    continue
                break
            # Prefer a real label over placeholder "result"
            if (not label or label == "result") and out:
                # pull from immediately previous short h2
                if re.search(r"<h2>[^<]{1,40}</h2>\s*$", out[-1]):
                    label = re.sub(r"</?h2>", "", out[-1]).strip()
                    out.pop()

            # Merge with previous incomplete table (e.g. 1-7 left col, 8-12 right)
            if entries and out:
                prev = out[-1]
                dice_l = dice.lower()
                if 'class="roll-table"' in prev and f'data-dice="{dice_l}"' in prev:
                    last_num = None
                    pm = re.findall(r'<th scope="row">([\d\-]+)</th>', prev)
                    if pm:
                        try:
                            last_num = int(pm[-1].split("-")[-1])
                        except ValueError:
                            last_num = None
                    try:
                        first_new = int(entries[0][0].split("-")[0])
                    except ValueError:
                        first_new = -1
                    if last_num is not None and first_new == last_num + 1:
                        row_html = "".join(
                            f'<tr><th scope="row">{html.escape(num)}</th>'
                            f"<td>{link(body)}</td></tr>"
                            for num, body in entries
                        )
                        out[-1] = prev.replace(
                            "</tbody></table>", row_html + "</tbody></table>"
                        )
                        continue
            if entries:
                # Sanitize garbage labels
                if label and re.search(r"[)(]", label) and len(label) < 12:
                    label = "result"
                out.append(
                    render_roll_table(
                        dice,
                        label or "result",
                        entries,
                        lookup,
                        current_slug,
                        section_index,
                        anchor_id=anchors.add(label or dice) if label else None,
                        link_kw=link_kw,
                    )
                )
                continue
            # No entries found — fall through as heading
            out.append(
                f"<h2>{dice_button(dice)} "
                f'<span class="roll-label">{html.escape(label or "")}</span></h2>'
            )
            continue

        # --- Stat block: Name + tag line + HP/Damage ---
        _not_creature = {
            "dangers",
            "discoveries",
            "hooks",
            "lore",
            "questions",
            "impressions",
            "origins",
            "nests",
            "sites",
            "terrain",
            "themes",
            "names",
            "wasps",
            "activities",
            "resources",
            "defenses",
            "places",
            "secrets",
            "moves",
            "people",
            "living conditions",
            "folks from elsewhere",
            "shoddy construction",
            "the delves",
            "chance encounters",
            "commonly available",
            "special items",
            "trade & barter",
            "trade and barter",
        }
        dp1, dp2 = _defmt(peek(1)), _defmt(peek(2))
        is_creature_start = i in forced_creature or (
            len(line) <= 48
            and not line.endswith(".")
            and line.lower() not in _not_creature
            and not looks_like_value_header(line)
            and not VALUE_ROW_RE.match(line)
            and (
                (
                    looks_like_tag_line(dp1)
                    and (
                        HP_LINE_RE.search(dp2)
                        or HP_LINE_RE.search(dp1)
                        or dp2.lower().startswith("damage")
                    )
                )
                # tags may be missing; name then HP
                or (
                    HP_LINE_RE.search(dp1)
                    and not looks_like_heading(dp1)
                )
            )
        )
        if is_creature_start:
            name = line
            # "Ferocedes Ogran, ghostly" → name + leading tags
            extra_tags: list[str] = []
            m_name = re.match(
                r"^(.+?),\s*([a-z][\w\-]*(?:\s*,\s*[a-z][\w\-]*)*)$",
                name,
            )
            if m_name:
                tail = m_name.group(2)
                bits = [t.strip() for t in tail.split(",") if t.strip()]
                if bits and all(
                    b[0:1].islower() or b.lower() in TAG_WORDS for b in bits
                ):
                    name = m_name.group(1).strip()
                    extra_tags.extend(bits)
            creature_icon = take_icon_html()
            i += 1
            block_lines: list[str] = []
            tag_prefix = list(extra_tags)  # folded into first real tag line
            # Boundaries: horizontal rules and the next creature's icon/heading.
            # Trailing bullets, checklists, Questions, in-card roll tables, and
            # flavor all stay until one of those boundaries.
            while i < n:
                L = lines[i]
                if L == M_HR:
                    # Decorative HR mid-card before roll-table entries
                    j = i + 1
                    while j < n and lines[j] == M_HR:
                        j += 1
                    if j < n and ENTRY_RE.match(_defmt(lines[j])):
                        i = j
                        continue
                    break
                if L.startswith(M_ICON):
                    break
                if L.startswith(M_H2) or L.startswith(M_VT) or L.startswith(M_TH):
                    break
                if L.startswith(M_BOX) or L == M_ENDBOX:
                    break
                if L.startswith(M_C):
                    block_lines.append(L)  # keep marker for check-list render
                    i += 1
                    continue
                if L.startswith(M_B2):
                    block_lines.append("• " + L[len(M_B2) :].strip())
                    i += 1
                    continue
                if L.startswith(M_B):
                    block_lines.append("• " + L[len(M_B) :].strip())
                    i += 1
                    continue
                if L.startswith(M_Q):
                    block_lines.append("• " + L[len(M_Q) :].strip())
                    i += 1
                    continue
                if L.startswith(M_E):
                    block_lines.append("• " + L[len(M_E) :].strip())
                    i += 1
                    continue
                if L.startswith(M_H3):
                    bare_h = L[len(M_H3) :].strip()
                    bare_l = bare_h.rstrip(":").lower()
                    if bare_l == "questions" or bare_l.startswith("questions"):
                        block_lines.append(bare_h.rstrip(":") + ":")
                        i += 1
                        continue
                    nxt = _defmt(peek(1))
                    # Type subtitle: "Forge Lord" then real tag line
                    if len(bare_h.split()) <= 4 and (
                        looks_like_tag_line(nxt)
                        or HP_LINE_RE.search(nxt or "")
                        or re.match(
                            r"^(damage|instinct|threat)\b", nxt or "", re.I
                        )
                    ):
                        tag_prefix.append(bare_h)
                        i += 1
                        continue
                    if (
                        looks_like_tag_line(nxt)
                        or HP_LINE_RE.search(nxt or "")
                        or re.match(r"^(damage|instinct|threat)\b", nxt or "", re.I)
                    ):
                        break
                    if bare_l in _not_creature:
                        break
                    break
                if L.startswith("\x02"):
                    break
                plain = _defmt(L)
                if tag_prefix and (
                    looks_like_tag_line(plain)
                    or HP_LINE_RE.search(plain)
                    or re.match(r"^(damage|instinct|threat)\b", plain, re.I)
                ):
                    if looks_like_tag_line(plain):
                        plain = ", ".join(tag_prefix) + ", " + plain
                    else:
                        block_lines.append(", ".join(tag_prefix))
                    tag_prefix = []
                # In-card roll table header + rows (e.g. "1d6 current task")
                m_roll = ROLL_HEADER_RE.match(plain)
                m_dice_only = ROLL_HEADER_DICE_ONLY.match(plain)
                if m_roll or (
                    m_dice_only
                    and len(plain.split()) <= 6
                    and not plain.lower().startswith("damage")
                ):
                    if m_roll:
                        dice_s, label_s = m_roll.group(1), m_roll.group(2).strip()
                    else:
                        dice_s, label_s = plain, "result"
                        # "1d6 current task"
                        m_lab = re.match(
                            r"^(\d{0,2}d(?:4|6|8|10|12|20))\s+(.+)$",
                            plain,
                            re.I,
                        )
                        if m_lab:
                            dice_s, label_s = m_lab.group(1), m_lab.group(2).strip()
                    i += 1
                    while i < n and lines[i] == M_HR:
                        i += 1
                    entries_c: list[tuple[str, str]] = []
                    while i < n:
                        if lines[i] == M_HR:
                            break
                        if lines[i].startswith("\x02"):
                            break
                        cur_e = _defmt(lines[i])
                        em = ENTRY_RE.match(cur_e)
                        if not em:
                            break
                        num_e = em.group(1) + (
                            f"-{em.group(2)}" if em.group(2) else ""
                        )
                        body_e = em.group(3).strip()
                        i += 1
                        while i < n and not lines[i].startswith("\x02"):
                            if lines[i] == M_HR:
                                break
                            nxt_e = _defmt(lines[i])
                            if ENTRY_RE.match(nxt_e):
                                break
                            if looks_like_roll_header(nxt_e) or looks_like_heading(
                                nxt_e
                            ):
                                break
                            body_e = body_e + " " + nxt_e
                            i += 1
                        entries_c.append((num_e, body_e))
                    if entries_c:
                        # stash as a renderable HTML fragment line
                        block_lines.append(
                            "__ROLL_TABLE__"
                            + dice_s
                            + "\x01"
                            + label_s
                            + "\x01"
                            + "\x02".join(f"{a}\x03{b}" for a, b in entries_c)
                        )
                    continue
                if plain.startswith("•") or plain.startswith("·"):
                    block_lines.append("• " + plain.lstrip("•· ").strip())
                else:
                    block_lines.append(plain)
                i += 1
            if tag_prefix:
                block_lines.insert(0, ", ".join(tag_prefix))
            out.append(
                render_stat_block(
                    name,
                    block_lines,
                    lookup,
                    current_slug,
                    section_index,
                    anchor_id=anchors.add(name),
                    link_kw=link_kw,
                    check_id=next_check_id(name),
                    icon_html=creature_icon,
                )
            )
            continue

        # --- Custom move block (ASK AROUND, CAROUSE, RECRUIT, …) ---
        # An ALL-CAPS move name in body font followed by a "When …" trigger.
        if (
            rich_mode
            and not line.startswith("\x02")
            and _is_all_caps_label(line)
            and peek(1)
            and strip_markers(peek(1)).lstrip().startswith("When ")
        ):
            name = line.strip()
            hid = anchors.add(name)
            i += 1
            inner: list[str] = []
            while i < n:
                L = lines[i]
                # Next move / heading / table ends this block
                if L.startswith(("\x02H", "\x02TH", "\x02VT", "\x02BOX")):
                    break
                if (
                    not L.startswith("\x02")
                    and _is_all_caps_label(L)
                    and peek(1)
                    and strip_markers(peek(1)).lstrip().startswith("When ")
                ):
                    break
                inner.append(L)
                i += 1
            # Leading plain lines are the trigger; join them into one sentence
            # (keep inline formatting sentinels so link() renders bold/italic)
            k = 0
            trig: list[str] = []
            while (
                k < len(inner)
                and not inner[k].startswith("\x02")
                and not _defmt(inner[k]).startswith(("•", "·"))
            ):
                trig.append(inner[k].strip())
                k += 1
            trigger = " ".join(t for t in trig if t.strip())
            rest_html, _ = structure_html(
                inner[k:],
                article_title,
                lookup,
                articles,
                current_slug,
                section_index=section_index,
                anchors=anchors,
                lookups=lookups,
                section_indexes=section_indexes,
                current_book=current_book,
            )
            block = (
                f'<div class="move-block" id="{html.escape(hid)}">'
                f'<h3 class="move-name">{html.escape(name)}</h3>'
            )
            if trigger:
                block += f'<p class="move-trigger">{link(trigger)}</p>'
            block += rest_html + "</div>"
            out.append(block)
            continue

        # --- Heading ---
        # In rich mode real headings arrive as markers; only ALL-CAPS move
        # names (set in body font, e.g. "ASK AROUND") still need this path.
        if rich_mode and looks_like_heading(line) and not _is_all_caps_label(line):
            out.append(f"<p>{link(line)}</p>")
            i += 1
            continue
        if looks_like_heading(line) and (
            len(line) < 45
            or line.endswith(":")
            or line in {
                "Themes",
                "Hooks",
                "Lore",
                "Questions",
                "Names",
                "Dangers",
                "Discoveries",
                "Origins",
                "Nests",
                "Impressions",
                "Always",
                "Spring",
                "Summer",
                "Autumn",
                "Winter",
                "Sites",
                "Terrain",
                "Activities",
                "Resources",
                "Defenses",
                "Places",
            }
        ):
            # Don't treat article title / doubled running heads as h2
            cleaned_h = undouble_words(line)
            if is_fully_pairwise_doubled(line) or is_running_header(
                line, article_title, near_page_top=True
            ):
                i += 1
                continue
            if normalize_text(cleaned_h).lower() == normalize_text(article_title).lower():
                i += 1
                continue
            level = "h2" if len(cleaned_h) < 40 else "h3"
            hid = anchors.add(cleaned_h.rstrip(":"))
            out.append(
                f'<{level} id="{html.escape(hid)}">'
                f"{html.escape(cleaned_h.rstrip(':'))}</{level}>"
            )
            i += 1
            continue

        # --- Bullet list run ---
        if line.startswith("•") or line.startswith("·"):
            items = []
            while i < n and _defmt(lines[i]).lstrip().startswith(("•", "·")):
                items.append(_defmt(lines[i]).lstrip("•· ").strip())
                i += 1
                # join soft wraps already done; also join if next is continuation
                while i < n and should_join(items[-1], lines[i]) and not _defmt(lines[i]).lstrip().startswith(("•", "·")):
                    items[-1] = items[-1] + " " + _defmt(lines[i])
                    i += 1
            if not items:
                i += 1  # never spin on a lone bullet glyph
                continue
            out.append("<ul>")
            for it in items:
                out.append(f"<li>{link(it)}</li>")
            out.append("</ul>")
            continue

        # --- Numbered standalone list that isn't a formal 1dN table ---
        if ENTRY_RE.match(line) and peek(1) and ENTRY_RE.match(_defmt(peek(1))):
            entries = []
            while i < n and ENTRY_RE.match(_defmt(lines[i])):
                e = ENTRY_RE.match(_defmt(lines[i]))
                assert e
                num = e.group(1) + (f"-{e.group(2)}" if e.group(2) else "")
                body = e.group(3).strip()
                i += 1
                while i < n and should_join(body, lines[i]) and not ENTRY_RE.match(_defmt(lines[i])):
                    body = body + " " + _defmt(lines[i])
                    i += 1
                entries.append((num, body))
            out.append('<div class="roll-table bare-numbered"><table><tbody>')
            for num, body in entries:
                out.append(
                    f"<tr><th scope=\"row\">{html.escape(num)}</th>"
                    f"<td>{link(body)}</td></tr>"
                )
            out.append("</tbody></table></div>")
            continue

        # --- Regular paragraph (preserve inline bold/italic formatting) ---
        out.append(f"<p>{link(orig_line)}</p>")
        i += 1

    html_body = "\n".join(out)
    return html_body, anchors.sections


def _region_lines(
    page: fitz.Page,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
) -> list[str]:
    """Extract reading-order lines from a rectangular region of a page."""
    words = page.get_text("words")
    in_region = []
    for w in words:
        cx = (w[0] + w[2]) / 2
        cy = (w[1] + w[3]) / 2
        if x0 <= cx < x1 and y0 <= cy < y1:
            in_region.append(w)
    if not in_region:
        return []
    by_y: dict[float, list] = defaultdict(list)
    for w in in_region:
        by_y[round(w[1] * 2) / 2].append(w)
    lines: list[str] = []
    for y in sorted(by_y):
        text = normalize_text(
            " ".join(t[4] for t in sorted(by_y[y], key=lambda z: z[0]))
        )
        if not text:
            continue
        if is_fully_pairwise_doubled(text):
            text = undouble_words(text)
        else:
            # still collapse local doubles
            text = undouble_words(text) if is_fully_pairwise_doubled(text) else text
            # partial doubles are common in arcana titles
            text = undouble_words(text)
        if text.lower() in {"front", "back"} or (
            text.isdigit() and len(text) <= 3
        ):
            continue
        if "appendix" in text.lower():
            continue
        lines.append(text)
    return lines


def _card_title_from_sides(left: list[str], right: list[str]) -> str:
    """Prefer the named power on the back (right); fall back to front discovery title."""
    tag_re = re.compile(
        r"^(,?\s*)?(magical|fragile|immobile|terrifying|crude|slow|beautiful|"
        r"warm|close|reach|awkward|indestructible|large|cumbersome)\b",
        re.I,
    )
    junk_title = re.compile(
        r"^(starts at|souls\b|lost memories|daylight|vitality|hold\b|max\b|"
        r"uses\b|pouch of|when |if you|you can|your |mark |spend |"
        r"roll |on a |the spell|alas,)",
        re.I,
    )

    def title_from(side: list[str]) -> str | None:
        parts: list[str] = []
        for i, line in enumerate(side[:5]):
            if not line or tag_re.match(line):
                if parts:
                    break
                continue
            if junk_title.match(line):
                if parts:
                    break
                continue
            if line.lower() in {"front", "back"} or re.fullmatch(r"\d+", line):
                continue
            parts.append(line.rstrip(",").strip())
            joined = " ".join(parts)
            nxt = side[i + 1] if i + 1 < len(side) else ""
            # Continue when the name clearly wraps onto the next line
            incomplete = (
                line.endswith((",", "-", "'s", "\u2019s"))
                or len(line.split()) <= 2
                or (
                    len(joined) < 28
                    and nxt
                    and nxt[0:1].isupper()
                    and not junk_title.match(nxt)
                    and not tag_re.match(nxt)
                    and not nxt.lower().startswith("when ")
                )
            )
            if not incomplete and len(joined) >= 6:
                break
            if len(parts) >= 3:
                break
        if not parts:
            return None
        title = undouble_words(" ".join(parts)).strip(" ,")
        # Strip form labels / tags accidentally glued to the name
        title = re.sub(
            r"\s+(Heat|Strain|Disturbance|Breath|Preparation|Charges?|Ken|"
            r"Sway|Authority|Loyalty|Charge|Ire|Readiness|Vitality|Daylight|"
            r"Souls)\s*:?\s*$",
            "",
            title,
            flags=re.I,
        )
        title = re.sub(r"\s+You have\b.*$", "", title, flags=re.I)
        title = re.sub(r"\s+As long as\b.*$", "", title, flags=re.I)
        title = re.sub(
            r"\s*,\s*(?:far|close|near|hand|reach|magical|forceful|loud|"
            r"reload|ignores|warm|fragile|crude).*$",
            "",
            title,
            flags=re.I,
        )
        title = re.sub(r"\s+raspy voice\b.*$", "", title, flags=re.I)
        title = title.strip(" ,:;")
        if junk_title.match(title) or len(title) < 3:
            return None
        return title

    # Prefer named power (back), then discovery object (front)
    for side in (right, left):
        t = title_from(side)
        if t:
            return t
    return "Minor Arcanum"


def extract_minor_arcana_card(
    doc: fitz.Document,
    page_1based: int,
    half: str,
) -> tuple[str, list[str]]:
    """
    Minor arcana PDF pages hold TWO cards (top + bottom), each with
    front (left) and back (right). Returns (title, lines) for one card.
    half is 'top' or 'bottom'.
    """
    page = doc[page_1based - 1]
    w, h = page.rect.width, page.rect.height
    mid_x = w * 0.5
    # Split near the front/back labels (~y=307 on most pages)
    mid_y = h * 0.50
    for winfo in page.get_text("words"):
        if winfo[4].lower() in {"front", "back"} and 270 < winfo[1] < 340:
            mid_y = winfo[1] - 2
            break

    if half == "top":
        y0, y1 = 45, mid_y
    else:
        y0, y1 = mid_y, h - 25

    left = _region_lines(page, 0, mid_x, y0, y1)
    right = _region_lines(page, mid_x, w, y0, y1)
    title = _card_title_from_sides(left, right)
    # Front then back reading order
    lines = left + right
    # Drop pure tag-only first lines already handled; merge wraps
    lines = merge_wrapped_lines(lines)
    return title, lines


def list_minor_arcana_cards(
    doc: fitz.Document, start_page: int, end_page: int
) -> list[dict]:
    """
    Enumerate all minor arcana cards in the page range (2 per page → 64 total).
    Each dict: title, start_page, end_page, half ('top'|'bottom').
    """
    cards: list[dict] = []
    for p in range(start_page, end_page + 1):
        for half in ("top", "bottom"):
            title, _ = extract_minor_arcana_card(doc, p, half)
            if not title or title == "Minor Arcanum":
                # try still include with page fallback
                title = f"Minor Arcanum (p. {p} {half})"
            cards.append(
                {
                    "title": title,
                    "start_page": p,
                    "end_page": p,
                    "half": half,
                }
            )
    return cards


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


def _is_pure_arcana_tag_line(line: str) -> bool:
    """True if the whole line is one or more arcana tags (e.g. 'magical', 'fragile, immobile')."""
    L = line.strip().lstrip(",").strip()
    if not L or len(L) > 70:
        return False
    parts = [p.strip() for p in re.split(r"[,;]", L) if p.strip()]
    if not parts:
        return False
    for p in parts:
        pl = p.lower().strip()
        if pl in _ARCANA_TAGS:
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


def _split_discovery_and_tags(line: str) -> tuple[str, str]:
    """'An old scroll case, fragile' → ('An old scroll case', 'fragile')."""
    L = line.strip().lstrip(",").strip()
    if not L:
        return "", ""
    # Whole line is tags
    if _is_pure_arcana_tag_line(L):
        parts = [p.strip() for p in re.split(r"[,;]", L) if p.strip()]
        return "", ", ".join(parts)
    # Trailing tags after comma
    tag_alt = "|".join(
        re.escape(t) for t in sorted(_ARCANA_TAGS, key=len, reverse=True)
    )
    m = re.match(
        r"^(.+?)[,\s]+((?:(?:" + tag_alt + r")[\s,]*)+)$",
        L,
        re.I,
    )
    if m:
        return m.group(1).strip(" ,"), re.sub(r"\s+", " ", m.group(2).strip(" ,"))
    # Space-separated trailing tags only (no following prose):
    # "A giant's dormitory magical"
    m2 = re.match(
        r"^(.+?)\s+(" + tag_alt + r"(?:\s*,\s*(?:" + tag_alt + r"))*)\s*$",
        L,
        re.I,
    )
    if m2 and len(m2.group(1).split()) <= 8:
        disc = m2.group(1).strip(" ,")
        # Don't strip tags off a sentence that merely ends with a tag word by chance
        if not disc.endswith((".", "!", "?", "…")):
            return disc, re.sub(r"\s+", " ", m2.group(2).strip(" ,"))
    return L, ""


def _peel_discovery_tag_prose(line: str) -> tuple[str, str, str]:
    """
    Split a mashed discovery line into (title, tags, prose).
    'A giant\\'s dormitory magical In a ruin…' →
    (\"A giant's dormitory\", 'magical', 'In a ruin…')
    """
    L = line.strip()
    if not L:
        return "", "", ""
    tag_alt = "|".join(
        re.escape(t) for t in sorted(_ARCANA_TAGS, key=len, reverse=True)
    )
    m = re.match(
        r"^(.{{2,50}}?)\s+({tags}(?:\s*,\s*(?:{tags}))*)\s+([A-Z].+)$".format(
            tags=tag_alt
        ),
        L,
        re.I,
    )
    if m and len(m.group(1).split()) <= 6:
        disc = m.group(1).strip(" ,")
        if not disc.endswith((".", "!", "?", "…")):
            return (
                disc,
                re.sub(r"\s+", " ", m.group(2).strip(" ,")),
                m.group(3).strip(),
            )
    disc, tags = _split_discovery_and_tags(L)
    return disc, tags, ""


def _is_unlock_intro(line: str) -> bool:
    low = line.lower()
    if re.search(
        r"(you can learn|you can unlock|to unlock|to learn|the manual reveals|"
        r"there is magic here|the pictograms|the notes reveal|to unlock the|"
        r"need one of the following|need one of these|you must\b|"
        r"but to learn|but you must|but need|but…|but\.\.\.)",
        low,
    ):
        return True
    if "but" in low and (
        "learn" in low or "unlock" in low or "must" in low or "secret" in low
    ):
        return True
    return False


def _is_unlock_item(line: str) -> bool:
    L = line.strip()
    if not L:
        return False
    if L.startswith(("…", "...", "•", "·")):
        return True
    # Bare requirement after "need one of the following"
    if len(L) < 160 and L[0:1].isupper() and not re.match(r"^When you\b", L, re.I):
        if re.match(
            r"^(A |An |The |Some |Risk |Spend |Get |Acquire |Dig |"
            r"Translate |Decipher |Study |Meditate |First )",
            L,
        ):
            return True
    return False


def _is_unlock_divider(line: str) -> bool:
    L = line.strip()
    low = L.lower()
    if re.match(r"^or\.+$", low) or low in {"or…", "or...", "or"}:
        return True
    if re.match(r"^and then\b", low):
        return True
    if re.match(r"^and either\b", low):
        return True
    if re.match(r"^either\b", low) and len(L) < 40:
        return True
    return False


def _is_tag_token(tok: str) -> bool:
    t = tok.lower().strip(",+;")
    if t in _ARCANA_TAGS:
        return True
    if re.match(r"^\+?\d+$", t):
        return True
    if t in {"piercing", "damage", "armor", "uses", "weight"}:
        return True
    # "+1" already; "1d4" not a tag
    return False


def _strip_power_line_tags(line: str) -> tuple[str, str]:
    """
    'The Broom's Lullaby magical, area, near' →
    (\"The Broom's Lullaby\", 'magical, area, near')
    """
    L = line.strip().lstrip(",").strip()
    if not L:
        return "", ""
    # Leading comma-tags only
    if L.startswith(",") or (looks_like_tag_line(L) and not HP_LINE_RE.search(L)):
        return "", L.lstrip(", ").strip()
    # Try split discovery/tags helper (works for trailing comma-tags)
    name, tags = _split_discovery_and_tags(L)
    if tags:
        return name, tags
    # Space-separated trailing tags: "Name magical, area, near" or "Name magical area near"
    # Normalize commas to spaces for token walk
    rough = re.sub(r"[,;]+", " ", L)
    words = rough.split()
    tag_i = len(words)
    while tag_i > 1 and _is_tag_token(words[tag_i - 1]):
        tag_i -= 1
    if tag_i < len(words) and tag_i >= 1:
        # Rebuild tags from original trailing portion when possible
        name = " ".join(words[:tag_i])
        tag_str = ", ".join(words[tag_i:])
        return name, tag_str
    return L, ""


def _line_matches_power_title(line: str, power_norm: str) -> bool:
    if not power_norm or not line:
        return False
    name, _tags = _strip_power_line_tags(line)
    ln = normalize_section_key(name)
    if not ln:
        return False
    if ln == power_norm:
        return True
    # Allow title without leading "The "
    if power_norm.startswith("the ") and ln == power_norm[4:]:
        return True
    if ln.startswith("the ") and power_norm == ln[4:]:
        return True
    return False


def _find_power_title_index(lines: list[str], article_title: str) -> int | None:
    """Index where the power name (back of card) begins."""
    power = (article_title or "").strip()
    power_norm = normalize_section_key(power)
    if power_norm:
        for i, line in enumerate(lines):
            if re.match(r"^When you\b", line, re.I):
                continue
            if line.strip().startswith(("…", "...", "•", "·")):
                continue
            if _is_unlock_intro(line) and not _line_matches_power_title(line, power_norm):
                continue
            # Title alone, or title + tags on one line
            if _line_matches_power_title(line, power_norm):
                # Prefer later occurrence (back of card) over mention in unlock prose
                # Only accept if line is short-ish or starts with the title
                name, _ = _strip_power_line_tags(line)
                if len(line) <= len(power) + 40 or line.lower().startswith(
                    name[:12].lower()
                ):
                    # Skip if this is long unlock prose containing the name
                    if len(line) > len(power) + 50 and not re.match(
                        r"^" + re.escape(name), line, re.I
                    ):
                        continue
                    return i
            if i + 1 < len(lines) and len(lines[i + 1]) < 48:
                joined = line + " " + lines[i + 1]
                if _line_matches_power_title(joined, power_norm):
                    return i
            # Multi-line title fragments
            name, _ = _strip_power_line_tags(line)
            ln = normalize_section_key(name)
            if (
                ln
                and len(ln) >= 4
                and power_norm.startswith(ln + " ")
                and i + 1 < len(lines)
                and not re.match(r"^When you\b", lines[i + 1], re.I)
                and len(lines[i + 1]) < 40
            ):
                jn = normalize_section_key(name + " " + lines[i + 1])
                if jn == power_norm:
                    return i

    # Fallback: line immediately before first post-unlock "When you"
    for i, line in enumerate(lines):
        if not re.match(r"^When you\b", line, re.I) or i == 0:
            continue
        for j in range(i - 1, max(-1, i - 5), -1):
            L = lines[j].strip()
            if not L or L.startswith(("…", "...")):
                continue
            if _is_unlock_intro(L):
                continue
            if _is_unlock_item(L) and L.startswith(("…", "...")):
                continue
            name, _ = _strip_power_line_tags(L)
            if len(name.split()) > 8:
                continue
            ln = normalize_section_key(name)
            if power_norm and (
                ln == power_norm
                or power_norm.startswith(ln)
                or ln in power_norm
            ):
                return j
            if not power_norm and len(name.split()) <= 5:
                return j
        # No title line — power starts at this When you
        return i
    return None


def _merge_orphan_bullets(lines: list[str]) -> list[str]:
    """'•' / 'Consume…' → '• Consume…'; join mid-bullet wraps."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        L = lines[i].strip()
        if L in {"•", "·", "-", "–"} and i + 1 < len(lines):
            nxt = lines[i + 1].strip().lstrip("•· ").strip()
            if nxt:
                # Cost/HP/etc. are stats, not bullet bodies
                if re.match(r"^(Cost|HP|Armor|Damage|Instinct|Loyalty)\b", nxt, re.I):
                    out.append(nxt)
                    i += 2
                    continue
                # Continue previous fragment ending with 'and' / 'or'
                if out and re.search(
                    r"\b(and|or|to|the|a|an|with|,)\s*$", out[-1].rstrip(), re.I
                ):
                    out[-1] = out[-1].rstrip() + " " + nxt
                else:
                    out.append("• " + nxt)
                i += 2
                continue
        if L.startswith("•") and len(L.lstrip("•· ").strip()) == 0:
            i += 1
            continue
        # Bullet line that continues previous incomplete bullet/prose
        if L.startswith(("•", "·")):
            body = L.lstrip("•· ").strip()
            if out and re.search(
                r"\b(and|or|to|the|a|an|with|,)\s*$", out[-1].rstrip(), re.I
            ):
                out[-1] = out[-1].rstrip() + " " + body
                i += 1
                continue
            out.append("• " + body)
            i += 1
            continue
        # Mid-wrap without bullet marker
        if (
            out
            and (
                out[-1].startswith("•")
                or re.search(r"\b(and|or)\s*$", out[-1].rstrip(), re.I)
            )
            and (
                re.search(r"\b(and|or|to|the|a|an|with|,)\s*$", out[-1].rstrip(), re.I)
                or out[-1].count("(") > out[-1].count(")")
            )
            and L
            and not re.match(r"^When you\b", L, re.I)
            and not HP_LINE_RE.search(L)
            and not re.match(r"^(Cost|HP|Armor|Damage|Instinct)\b", L, re.I)
        ):
            out[-1] = out[-1].rstrip() + " " + L
            i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def _is_track_line(line: str) -> bool:
    """Blaze: 4nil, 1d4, 1d6… or similar stepped tracks."""
    return bool(
        re.match(
            r"^[A-Za-z][A-Za-z\s]{0,20}:\s*.*\b(nil|\d*d\d+)\b",
            line.strip(),
            re.I,
        )
    )


def _render_track_line(line: str, link_fn) -> str:
    """Render 'Blaze: 4nil, 1d4, 1d6, 1d8, 1d10' as a stepped track."""
    m = re.match(r"^([^:]+):\s*(.+)$", line.strip())
    if not m:
        return f'<p class="arcana-track">{link_fn(line)}</p>'
    label, rest = m.group(1).strip(), m.group(2).strip()
    steps = [s.strip() for s in re.split(r"[,/|]", rest) if s.strip()]
    cells = []
    for s in steps:
        # dice in step
        cell = html.escape(s)
        cell = DICE_RE.sub(
            lambda mm: dice_button(mm.group(1)), cell
        )
        cells.append(f'<span class="track-step">{cell}</span>')
    return (
        f'<div class="arcana-track">'
        f'<span class="track-label">{html.escape(label)}</span>'
        f'<span class="track-steps">{"".join(cells)}</span>'
        f"</div>"
    )


def _looks_like_follower_name(line: str) -> bool:
    L = line.strip()
    if not L or len(L) > 50 or re.match(r"^When you\b", L, re.I):
        return False
    if HP_LINE_RE.search(L) or looks_like_tag_line(L):
        return False
    words = L.split()
    if len(words) > 6:
        return False
    # Title Case or Proper Name
    return L[0:1].isupper() and not L.endswith(".")


def _dedupe_arcana_title(s: str) -> str:
    """Collapse a doubled card title ("A folktale folktale" -> "A folktale")."""
    s = re.sub(r"\s+", " ", s).strip()
    words = s.split()
    n = len(words)
    for k in range(1, n // 2 + 1):
        tail = words[n - k:]
        if words[n - 2 * k: n - k] == tail:
            # drop the repeated trailing phrase
            return " ".join(words[: n - k])
    return s


_ARCANA_TAG_WORD = re.compile(
    r"^(magical|fragile|immobile|beautiful|terrifying|close|reach|near|far|"
    r"hand|worn|warm|crude|slow|applied|thrown|messy|forceful|area|"
    r"dangerous|loud|ap|reload|cumbersome|awkward|indestructible|implanted|"
    r"large|two-handed)\b[\s,]*",
    re.I,
)


def _arcana_tags_prose(text: str):
    """Peel a leading tag run (italic words, +N damage, etc.) off a line.

    Returns (tags_string, remaining_prose). The prose keeps its formatting.
    """
    t = re.sub(r"^[◇\s,]+", "", text)
    tags: list[str] = []
    while t:
        m = re.match(r"^\x06([^\x06\x07]*)\x07[\s,]*", t)
        if m:
            tags.append(_defmt(m.group(1)).strip(" ,"))
            t = t[m.end():]
            continue
        m = re.match(r"^\+?\d+\s*(?:damage|piercing|armor|uses)\b[\s,]*", t)
        if m:
            tags.append(m.group(0).strip(" ,"))
            t = t[m.end():]
            continue
        m = _ARCANA_TAG_WORD.match(t)
        if m:
            tags.append(m.group(1))
            t = t[m.end():]
            continue
        break
    tag_str = ", ".join(x for x in tags if x)
    return tag_str, t.strip()


def _arcana_strip_running(line: str) -> str:
    """Drop 'front'/'back'/page-number running-header junk from a card line."""
    d = _defmt(strip_markers(line))
    d = re.sub(r"\bfront\s*\d*\b", "", d, flags=re.I)
    d = re.sub(r"\bback\s*\d*\b", "", d, flags=re.I)
    return re.sub(r"\s+", " ", d).strip()


def structure_minor_arcana_html(
    lines: list[str],
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, list[dict]]:
    """Layout a minor arcanum from rich marker lines (front + back faces)."""
    anchors = AnchorRegistry()
    link_kw = {
        "lookups": lookups,
        "section_indexes": section_indexes,
        "current_book": current_book,
    }

    def link(text: str) -> str:
        return linkify_pages(text, lookup, current_slug, section_index, **link_kw)

    def content(raw: str) -> str:
        return raw[len("\x02H3 "):] if raw.startswith("\x02H3 ") else _arcana_content(raw)

    power = (article_title or "Minor Arcanum").strip()
    power_norm = normalize_section_key(power)

    # Drop stray running-header-only lines
    lines = [l for l in lines if l.startswith("\x02") or _arcana_strip_running(l)]

    # Face split at the power-name H3 (the second card title)
    h3_idx = [i for i, l in enumerate(lines) if l.startswith("\x02H3")]
    split = None
    for i in h3_idx:
        nm = normalize_section_key(
            _dedupe_arcana_title(_defmt(strip_markers(lines[i])))
        )
        if nm == power_norm and i > 0:
            split = i
            break
    if split is None and len(h3_idx) >= 2:
        split = h3_idx[1]
    if split is None:
        # No clear back face; treat everything as front
        split = len(lines)

    front = lines[:split]
    back = lines[split:]

    def render_move(trigger: str, fl: list[str], j: int, n: int):
        body = [trigger]
        picks: list[str] = []
        seen_pick = False
        while j < n:
            r = fl[j]
            l = _defmt(strip_markers(r)).strip()
            if not l:
                j += 1
                continue
            if r.startswith(("\x02H", "\x02FACE", "\x02TH", M_MARK)):
                break
            if re.match(r"^(When you|When |During this battle|If )\b", l, re.I) and not r.startswith((M_B, M_B2)):
                break
            if r.startswith((M_B, M_B2)):
                picks.append(_arcana_content(r))
                seen_pick = True
                j += 1
                continue
            if seen_pick:
                picks.append(_arcana_content(r))
            else:
                body.append(_arcana_content(r))
            j += 1
        block = f'<div class="arcana-move"><p>{link(" ".join(body))}</p>'
        if picks:
            block += '<ul class="arcana-picks">'
            for p in picks:
                block += f"<li>{link(p)}</li>"
            block += "</ul>"
        block += "</div>"
        return block, j

    # ---- Front face: discovery + unlock ----
    fparts: list[str] = []
    disc_name = ""
    disc_tags = ""
    i = 0
    n = len(front)
    # Discovery name: the first run of consecutive H3 title lines. A long title
    # wraps onto a second H3 line (e.g. "Runes around" + "a ruined hall"), so
    # join them instead of leaving the tail as a stray paragraph.
    while i < n and not front[i].startswith("\x02H3"):
        i += 1
    title_parts: list[str] = []
    while i < n and front[i].startswith("\x02H3"):
        title_parts.append(_defmt(strip_markers(front[i])))
        i += 1
    disc_name = _dedupe_arcana_title(" ".join(title_parts))
    # tags + description + unlock
    desc_paras: list[str] = []
    unlock_intro = ""
    unlock_items: list[str] = []
    unlock_dividers: dict[int, str] = {}
    in_unlock = False
    while i < n:
        raw = front[i]
        i += 1
        d = _defmt(strip_markers(raw)).strip()
        if not d:
            continue
        if raw.startswith("\x02TH"):
            continue
        if raw.startswith((M_C, M_E)):
            in_unlock = True
            item = _arcana_content(raw)
            item = re.sub(r"^[\s…\.]+", "", item)
            unlock_items.append(item)
            continue
        if in_unlock:
            # "or:" / "and then:" dividers between requirement groups
            if len(d) <= 24 and re.match(r"^(or|and(?: then)?|either)\b", d, re.I):
                unlock_dividers[len(unlock_items)] = d.rstrip(":.") + ":"
                continue
            unlock_items[-1] = unlock_items[-1] + " " + _arcana_content(raw) if unlock_items else _arcana_content(raw)
            continue
        # tags on the first content line
        if not desc_paras and not disc_tags:
            tg, prose = _arcana_tags_prose(_arcana_content(raw))
            if tg:
                disc_tags = tg
            if prose:
                desc_paras.append(prose)
            continue
        desc_paras.append(_arcana_content(raw))
    # The last description paragraph before the checklist is the unlock intro
    if unlock_items and desc_paras:
        dl_last = _defmt(desc_paras[-1])
        if re.search(r"(you (?:can|either|must|need)|following|:|…|\.\.\.)\s*$", dl_last, re.I) or "you " in dl_last.lower():
            unlock_intro = desc_paras.pop()

    fparts.append('<p class="arcana-face-label">Discovery</p>')
    if disc_name:
        did = anchors.add(disc_name)
        fparts.append(
            f'<h3 id="{html.escape(did)}" class="arcana-discovery">'
            f"{html.escape(disc_name)}</h3>"
        )
    if disc_tags:
        fparts.append(f'<p class="arcana-tags">{link(disc_tags)}</p>')
    for p in desc_paras:
        fparts.append(f"<p>{link(p)}</p>")
    if unlock_items:
        fparts.append('<div class="arcana-unlock">')
        if unlock_intro:
            fparts.append(
                f'<p class="arcana-unlock-intro">{link(unlock_intro)}</p>'
            )
        lid = f"arcana-{slugify_id(power)}-unlock"
        # split into groups by dividers
        groups: list[tuple[str, list[str]]] = []
        cur_hdr = ""
        cur: list[str] = []
        for idx, it in enumerate(unlock_items):
            if idx in unlock_dividers:
                groups.append((cur_hdr, cur))
                cur_hdr = unlock_dividers[idx]
                cur = []
            cur.append(it)
        groups.append((cur_hdr, cur))
        gi = 0
        for hdr, items in groups:
            if hdr:
                fparts.append(f'<p class="si-requires">{html.escape(hdr)}</p>')
            fparts.append(render_check_list(items, link, f"{lid}-{gi}"))
            gi += 1
        fparts.append("</div>")

    # ---- Back face: power moves ----
    bparts: list[str] = []
    power_tags = ""
    i = 0
    n = len(back)
    # skip to first non-header; capture power-name H3 (skip, shown in header)
    while i < n:
        raw = back[i]
        d = _defmt(strip_markers(raw)).strip()
        if raw.startswith("\x02H3") and normalize_section_key(
            _dedupe_arcana_title(d)
        ) == power_norm:
            i += 1
            break
        if raw.startswith("\x02TH") or not d or d.lower() in ("front", "back"):
            i += 1
            continue
        break
    first_prose = True
    while i < n:
        raw = back[i]
        d = _defmt(strip_markers(raw)).strip()
        if not d or raw.startswith("\x02TH") or d.lower() in ("front", "back"):
            i += 1
            continue
        if raw.startswith(M_MARK):
            nm = int(raw[len(M_MARK):] or "0")
            bparts.append(
                render_mark_track(
                    nm, f"arcana-{slugify_id(power)}-uses", label="Uses"
                )
            )
            i += 1
            continue
        # use / level track lines: "Blaze: ◇ 1d4, …" or "hours: ◇◇◇"
        if "◇" in d and re.match(r"^[A-Za-z][\w\s]{0,18}:\s*◇", d):
            bparts.append(f'<p class="arcana-uses">{link(_arcana_content(raw))}</p>')
            i += 1
            continue
        named = _arcana_named_move(_arcana_content(raw))
        if named and not re.match(r"^When", named[0], re.I):
            name, mtags, trig = named
            hid = anchors.add(name.title() if name.isupper() else name)
            label = html.escape(name)
            if mtags:
                label += f' <span class="arcana-sub-tags">({html.escape(mtags)})</span>'
            bparts.append(f'<h3 id="{html.escape(hid)}" class="arcana-sub">{label}</h3>')
            i += 1
            if trig:
                block, i = render_move(trig, back, i, n)
                bparts.append(block)
            continue
        if re.match(r"^(When you|When |During this battle|If )\b", d, re.I):
            block, i = render_move(_arcana_content(raw), back, i + 1, n)
            bparts.append(block)
            continue
        # power tags line / prose
        if first_prose:
            tg, prose = _arcana_tags_prose(_arcana_content(raw))
            if tg and not prose:
                power_tags = tg
                first_prose = False
                i += 1
                continue
            first_prose = False
        bparts.append(f"<p>{link(_arcana_content(raw))}</p>")
        i += 1

    hid = anchors.add(power)
    parts = [f'<div class="arcana-card" id="{html.escape(hid)}">']
    parts.append('<div class="arcana-face arcana-front">')
    parts.extend(fparts)
    parts.append("</div>")
    if bparts:
        parts.append('<div class="arcana-face arcana-back-face">')
        parts.append('<p class="arcana-face-label">Power</p>')
        parts.append(f'<h2 class="arcana-power-name">{html.escape(power)}</h2>')
        if power_tags:
            parts.append(f'<p class="arcana-tags">{link(power_tags)}</p>')
        parts.extend(bparts)
        parts.append("</div>")
    parts.append("</div>")
    return "\n".join(parts), anchors.sections


def extract_major_arcana_rich(
    doc: fitz.Document,
    start_page: int,
    end_page: int,
    article_title: str,
) -> list[str]:
    """Rich (marker + formatting) extraction of a two-page major-arcana card.

    Each page is a single full-width column; front page first, then back.
    """
    out: list[str] = []
    for pno in range(start_page - 1, end_page):
        if pno < 0 or pno >= doc.page_count:
            continue
        out.append("\x02FACE " + ("front" if pno == start_page - 1 else "back"))
        out.extend(
            extract_page_rich(
                doc[pno], article_title=article_title, single_column=True
            )
        )
    return merge_wrapped_lines(out)


def extract_minor_arcana_rich(
    doc: fitz.Document,
    page_1based: int,
    half: str,
) -> list[str]:
    """Rich extraction of one minor-arcana card (a top/bottom half of a page).

    Front is the left column, back the right — the default gutter split.
    """
    page = doc[page_1based - 1]
    h = page.rect.height
    mid_y = h * 0.50
    for winfo in page.get_text("words"):
        if winfo[4].lower() in {"front", "back"} and 270 < winfo[1] < 340:
            mid_y = winfo[1] - 2
            break
    y0, y1 = (45, mid_y) if half == "top" else (mid_y, h - 25)
    lines = extract_page_rich(page, y_clip=(y0, y1))
    lines = merge_wrapped_lines(lines)
    # Strip card front/back labels + page numbers that wrap into the text
    cleaned: list[str] = []
    for l in lines:
        l = re.sub(r"\s*\bfront\b\s*\d*\s*\bback\b\s*$", "", l, flags=re.I)
        l = re.sub(r"\s*\b(front|back)\b\s*\d*\s*$", "", l, flags=re.I)
        if _defmt(strip_markers(l)).strip():
            cleaned.append(l)
    return cleaned


def extract_major_arcana_blocks(
    doc: fitz.Document,
    start_page: int,
    end_page: int,
) -> list[str]:
    """
    Extract major-arcana card text using PDF text blocks (reading order).
    Much cleaner than word-stream extraction for two-page front/back cards.
    """
    paras: list[str] = []
    for pno in range(start_page - 1, end_page):
        if pno < 0 or pno >= doc.page_count:
            continue
        page = doc[pno]
        blocks = page.get_text("blocks")
        blocks = sorted(
            [b for b in blocks if str(b[4]).strip()],
            key=lambda b: (round(b[1] / 3) * 3, b[0]),
        )
        for b in blocks:
            raw = str(b[4])
            if not raw.strip():
                continue
            # Per-line first (preserve list items), then soft-wrap join
            raw_lines = [
                normalize_text(p) for p in raw.split("\n") if normalize_text(p)
            ]
            if not raw_lines:
                continue
            merged: list[str] = []
            for p in raw_lines:
                low = p.lower()
                if "appendix" in low:
                    continue
                if low in {"front", "back"} or re.fullmatch(r"\d{1,3}", low):
                    continue
                new_item = re.match(
                    r"^(When you|You |Your |Pick |During |Attack |Strike |"
                    r"Disengage |Name |Mysteries |Moves|Consequences|"
                    r"UNQUENCHED|A [A-Z])",
                    p,
                )
                if merged and not new_item and (
                    p[0:1].islower()
                    or p[0:1].isdigit()
                    or p[0:1] in "([+\""
                    or merged[-1].endswith(("-", "–", "—", ",", ";", "—"))
                    or not merged[-1].endswith((".", ":", "!", "?"))
                ):
                    merged[-1] = merged[-1] + " " + p
                else:
                    merged.append(p)
            for joined in merged:
                joined = re.sub(r"[ \t]+", " ", joined).strip()
                if not joined:
                    continue
                # "TITLE When you..." packed on one line
                m = re.match(
                    r"^([A-Z][A-Z0-9\s'\-]{2,40}?)\s+(When you\b.*)$", joined
                )
                if m and len(m.group(1).split()) <= 6:
                    paras.append(m.group(1).strip())
                    paras.append(m.group(2).strip())
                    continue
                paras.append(joined)
    return paras


def _is_mark_track_line(line: str) -> int:
    """
    Detect progress mark rows like 'l l l l l' (PDF dingbats as 'l').
    Returns number of marks, or 0 if not a track.
    """
    L = line.strip()
    # only l's and spaces (and maybe o/O/□)
    if re.fullmatch(r"[lLoO□☐○●•·\s]{2,}", L):
        n = len(re.findall(r"[lLoO□☐○●•·]", L))
        if 2 <= n <= 12:
            return n
    return 0


def _arcana_content(raw: str) -> str:
    """Strip a leading \\x02 marker but keep inline formatting sentinels."""
    if raw.startswith("\x02"):
        return MARKER_RE.sub("", raw)
    return raw


def _arcana_named_move(text: str):
    """Parse "NAME [(tags)] [When you ...]" into (name, tags, trigger) or None."""
    m = BOLD_PREFIX_RE.match(text)
    if not m:
        return None
    name = _defmt(m.group(1)).strip()
    if not name or not _is_all_caps_label(name):
        return None
    rest = text[m.end():]
    tags = ""
    tm = re.match(r"^[\s]*\x06([^\x06\x07]*)\x07\s*", rest)
    if tm:
        cand = _defmt(tm.group(1)).strip()
        if "(" in cand or _is_pure_arcana_tag_line(cand):
            tags = cand.strip(" ()")
            rest = rest[tm.end():]
    trigger = rest.strip()
    return name, tags, trigger


def _arcana_desc_tags(text: str):
    """Split a description line into (tags, description); tags may be italic."""
    t = re.sub(r"^[◇\s]+", "", text)
    tags = ""
    m = re.match(r"^\x06([^\x06\x07]*)\x07\s*", t)
    if m and _is_pure_arcana_tag_line(_defmt(m.group(1))):
        tags = _defmt(m.group(1)).strip(" ,")
        t = t[m.end():]
    return tags, t.strip()


def structure_major_arcana_html(
    lines: list[str],
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, list[dict]]:
    """Layout a major arcanum from rich marker lines (front/back faces)."""
    anchors = AnchorRegistry()
    link_kw = {
        "lookups": lookups,
        "section_indexes": section_indexes,
        "current_book": current_book,
    }

    def link(text: str) -> str:
        return linkify_pages(text, lookup, current_slug, section_index, **link_kw)

    power = (article_title or "Major Arcanum").strip()
    power_norm = normalize_section_key(power)

    faces: dict[str, list[str]] = {"front": [], "back": []}
    cur = "front"
    for l in lines:
        if l.startswith("\x02FACE "):
            cur = l[len("\x02FACE "):].strip() or cur
            faces.setdefault(cur, [])
            continue
        faces.setdefault(cur, []).append(l)

    def render_face(fl: list[str], face: str, front: bool):
        out: list[str] = []
        tags = ""
        desc_done = False
        cons_items: list[str] = []
        section = "moves"
        i = 0
        n = len(fl)

        def dl(s: str) -> str:
            return _defmt(strip_markers(s)).strip()

        def collect_move(trigger_text: str, j: int):
            body = [trigger_text]
            picks: list[str] = []
            seen_pick = False
            while j < n:
                r = fl[j]
                l = dl(r)
                if not l or l.lower() in ("front", "back"):
                    j += 1
                    continue
                if r.startswith(("\x02H", "\x02FACE", "\x02TH", M_MARK)):
                    break
                if r.startswith(M_C):
                    break
                if _arcana_named_move(_arcana_content(r)):
                    break
                if re.match(r"^(When you|During this battle)\b", l, re.I):
                    break
                if r.startswith((M_B, M_B2)):
                    picks.append(_arcana_content(r))
                    seen_pick = True
                    j += 1
                    continue
                if seen_pick:
                    picks.append(_arcana_content(r))
                else:
                    body.append(_arcana_content(r))
                j += 1
            block = f'<div class="arcana-move"><p>{link(" ".join(body))}</p>'
            if picks:
                block += '<ul class="arcana-picks">'
                for p in picks:
                    block += f"<li>{link(p)}</li>"
                block += "</ul>"
            block += "</div>"
            return block, j

        while i < n:
            raw = fl[i]
            L = dl(raw)
            if not L:
                i += 1
                continue
            if raw.startswith("\x02TH") or L.lower() in ("front", "back"):
                i += 1
                continue
            if raw.startswith("\x02H2") and L.lower() == "moves":
                i += 1
                continue
            if raw.startswith(("\x02H2", "\x02H3")) and re.match(
                r"^Mysteries of\b", L, re.I
            ):
                i += 1
                continue
            if raw.startswith(("\x02H2", "\x02H3")) and normalize_section_key(
                L
            ) == power_norm:
                i += 1
                continue
            if raw.startswith("\x02H2") and re.match(r"^Consequences\b", L, re.I):
                section = "consequences"
                i += 1
                continue
            if section == "consequences":
                if raw.startswith(M_C):
                    cons_items.append(_arcana_content(raw))
                i += 1
                continue
            if raw.startswith(M_MARK):
                nm = int(raw[len(M_MARK):] or "0")
                lid = f"arcana-{slugify_id(power)}-{face}-marks"
                out.append(render_mark_track(nm, lid, label="Progress marks"))
                i += 1
                continue
            named = _arcana_named_move(_arcana_content(raw))
            if named:
                name, mtags, trigger = named
                hid = anchors.add(name.title() if name.isupper() else name)
                label = html.escape(name)
                if mtags:
                    label += (
                        f' <span class="arcana-sub-tags">'
                        f"({html.escape(mtags)})</span>"
                    )
                out.append(
                    f'<h3 id="{html.escape(hid)}" class="arcana-sub">{label}</h3>'
                )
                i += 1
                if trigger and re.match(
                    r"^(When you|During)\b", _defmt(trigger), re.I
                ):
                    block, i = collect_move(trigger, i)
                    out.append(block)
                elif trigger:
                    out.append(f"<p>{link(trigger)}</p>")
                continue
            if re.match(r"^(When you|During this battle)\b", L, re.I):
                block, i = collect_move(_arcana_content(raw), i + 1)
                out.append(block)
                continue
            if front and not desc_done:
                t, desc = _arcana_desc_tags(_arcana_content(raw))
                if t:
                    tags = t
                if desc:
                    out.append(f"<p>{link(desc)}</p>")
                desc_done = True
                i += 1
                continue
            out.append(f"<p>{link(_arcana_content(raw))}</p>")
            i += 1

        if cons_items:
            lid = f"arcana-{slugify_id(power)}-cons"
            out.append('<div class="arcana-unlock arcana-consequences">')
            out.append('<p class="si-requires">Consequences</p>')
            out.append(render_check_list(cons_items, link, lid))
            out.append(
                '<p class="arcana-note"><em>Mark consequences as they apply.</em></p>'
            )
            out.append("</div>")
        return "\n".join(out), tags

    front_html, tags = render_face(faces.get("front", []), "front", True)
    back_html, _ = render_face(faces.get("back", []), "back", False)

    hid = anchors.add(power)
    parts = [
        f'<div class="arcana-card arcana-major" id="{html.escape(hid)}">',
        '<div class="arcana-face arcana-front">',
        '<p class="arcana-face-label">Front</p>',
        f'<h2 class="arcana-power-name">{html.escape(power)}</h2>',
    ]
    if tags:
        parts.append(f'<p class="arcana-tags">{link(tags)}</p>')
    parts.append(front_html)
    parts.append("</div>")
    if back_html.strip():
        parts.append('<div class="arcana-face arcana-back-face">')
        parts.append('<p class="arcana-face-label">Back &mdash; Mysteries</p>')
        parts.append(back_html)
        parts.append("</div>")
    parts.append("</div>")
    return "\n".join(parts), anchors.sections


def minor_arcana_html_from_pdf(
    doc: fitz.Document,
    page_1based: int,
    half: str,
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    lines: list[str] | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, str, list[dict]]:
    """Build HTML for a single minor arcanum card (front+back).

    Returns (body_html, excerpt, sections).
    """
    # Rich extraction preserves formatting, checkboxes, and diamonds
    lines = extract_minor_arcana_rich(doc, page_1based, half)
    body, sections = structure_minor_arcana_html(
        lines,
        article_title,
        lookup,
        articles,
        current_slug,
        section_index=section_index,
        lookups=lookups,
        section_indexes=section_indexes,
        current_book=current_book,
    )
    excerpt = ""
    for line in lines:
        if line.startswith("\x02"):
            continue
        clean = _defmt(line).strip()
        if len(clean) >= 50 and not clean.lower().startswith("when you"):
            excerpt = clean
            break
    if not excerpt and lines:
        excerpt = _defmt(strip_markers(lines[0]))
    if len(excerpt) > 320:
        excerpt = excerpt[:319].rsplit(" ", 1)[0] + "…"
    return body, excerpt, sections


def major_arcana_html_from_pdf(
    doc: fitz.Document,
    start_page: int,
    end_page: int,
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    lines: list[str] | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, str, list[dict]]:
    """Build HTML for a major arcanum (two-page card)."""
    # Rich extraction preserves formatting, checkboxes, and mark tracks
    lines = extract_major_arcana_rich(doc, start_page, end_page, article_title)
    body, sections = structure_major_arcana_html(
        lines,
        article_title,
        lookup,
        articles,
        current_slug,
        section_index=section_index,
        lookups=lookups,
        section_indexes=section_indexes,
        current_book=current_book,
    )
    excerpt = ""
    for line in lines:
        if line.startswith("\x02"):
            continue
        clean = _defmt(line).strip()
        if len(clean) >= 50 and not clean.lower().startswith("when you"):
            excerpt = clean
            break
    if not excerpt and lines:
        excerpt = _defmt(strip_markers(lines[0]))
    if len(excerpt) > 320:
        excerpt = excerpt[:319].rsplit(" ", 1)[0] + "…"
    return body, excerpt, sections


def article_html_from_pdf(
    doc: fitz.Document,
    start_page: int,
    end_page: int,
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    lines: list[str] | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
    icon_dir: Path | None = None,
) -> tuple[str, str, list[dict]]:
    """
    Returns (body_html, excerpt_text, sections).
    """
    if lines is None:
        lines = extract_article_lines(
            doc, start_page, end_page, article_title, icon_dir=icon_dir
        )
    body, sections = structure_html(
        lines,
        article_title,
        lookup,
        articles,
        current_slug,
        section_index=section_index,
        lookups=lookups,
        section_indexes=section_indexes,
        current_book=current_book,
    )
    # Excerpt: whole prose paragraphs until we have at least ~50 words,
    # always ending on a paragraph boundary
    paras: list[str] = []
    words = 0
    in_box = False
    for line in lines:
        if line == "\x02BOX":
            in_box = True
            continue
        if line == "\x02ENDBOX":
            in_box = False
            continue
        # prose paragraphs only — skip headings, lists, tables, infobox
        if in_box or line.startswith("\x02"):
            continue
        clean = strip_markers(line).strip()
        if len(clean) < 40:
            continue
        if ENTRY_RE.match(clean) or looks_like_tag_line(clean):
            continue
        if HP_LINE_RE.search(clean) or ROLL_HEADER_RE.match(clean):
            continue
        paras.append(clean)
        words += len(clean.split())
        if words >= 50:
            break
    excerpt = "\n\n".join(paras)
    if not excerpt and lines:
        excerpt = strip_markers(lines[0])
    return body, excerpt, sections


def build_section_index(
    per_slug_sections: dict[str, list[dict]],
    articles: list[dict],
) -> dict:
    """Build cross-page lookup for section/monster deep links."""
    by_slug_norm: dict[tuple[str, str], str] = {}
    by_page_norm: dict[tuple[int, str], tuple[str, str]] = {}
    title_by_slug = {a["slug"]: a.get("title") or a["slug"] for a in articles}
    # Distinctive (multi-word) section names that live on exactly one article, so
    # a page ref whose label names such a section can be resolved to the right
    # article even when the printed page holds several arcana (two per page) or
    # the page->article mapping is imperfect.
    name_owner: dict[str, str] = {}
    name_sid: dict[str, str] = {}
    name_multi: set[str] = set()
    for art in articles:
        slug = art["slug"]
        sections = per_slug_sections.get(slug) or []
        for sec in sections:
            norm = sec.get("norm") or normalize_section_key(sec["name"])
            sid = sec["id"]
            by_slug_norm[(slug, norm)] = sid
            # Also index without spaces for tight matches
            by_slug_norm[(slug, norm.replace(" ", ""))] = sid
            start = art.get("start_page") or 0
            end = art.get("end_page") or start
            for p in range(start, end + 1):
                by_page_norm[(p, norm)] = (slug, sid)
                by_page_norm[(p, norm.replace(" ", ""))] = (slug, sid)
            if " " in norm:  # multi-word only, to avoid hijacking title cross-refs
                if norm in name_multi:
                    pass
                elif norm in name_owner and name_owner[norm] != slug:
                    name_multi.add(norm)
                    name_owner.pop(norm, None)
                    name_sid.pop(norm, None)
                else:
                    name_owner[norm] = slug
                    name_sid[norm] = sid
    by_name_unique = {
        norm: (slug, name_sid[norm], title_by_slug.get(slug, slug))
        for norm, slug in name_owner.items()
    }
    return {
        "by_slug_norm": by_slug_norm,
        "by_page_norm": by_page_norm,
        "by_name_unique": by_name_unique,
    }


def resolve_unique_section(
    label: str | None, section_index: dict | None
) -> tuple[str, str, str] | None:
    """(slug, section_id, article_title) for a label that uniquely names a
    multi-word section anywhere in the book, else None."""
    if not label or not section_index:
        return None
    uniq = section_index.get("by_name_unique") or {}
    if not uniq:
        return None
    keys = _section_match_keys(label)
    for k in keys:
        if k in uniq:
            return uniq[k]
    # Some arcana discovery headings are extracted truncated (a wrapped title
    # like "Runes around a ruined hall" indexed as "Runes around"). Fall back to
    # the label's leading words (>= 2) so the hook still resolves.
    for k in keys:
        words = k.split()
        for j in range(len(words) - 1, 1, -1):
            pref = " ".join(words[:j])
            if pref in uniq:
                return uniq[pref]
    return None


# ---------------------------------------------------------------------------
# Site shell, CLI, and build orchestration
# ---------------------------------------------------------------------------

PDF_FILENAME = (
    "Book_II_-_The_Wider_World_and_Other_Wonders_(1-up)_-_2nd_printing.pdf"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Build a static Stonetop Book II wiki from the 1-up PDF "
            "(optional Maps/ campaign sheets in the input folder)."
        )
    )
    p.add_argument(
        "-i",
        "--input",
        type=Path,
        default=Path.cwd(),
        help=(
            "Folder containing the Book II 1-up PDF. Optional subfolder: "
            "Maps/ (campaign map sheets). "
            "Default: current working directory."
        ),
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=(
            "Folder to write the wiki into. "
            "Default: <input>/Stonetop_Wiki"
        ),
    )
    return p.parse_args(argv)


def find_campaign_map_jpgs(input_dir: Path) -> list[Path]:
    """
    Optional high-res campaign map sheets.

    Looks under ``Maps/`` or ``maps/`` inside *input_dir* for files named
    like ``Map *.jpg`` (top-level or nested). Missing maps is fine — PDF
    map spreads are always rendered from the book.
    """
    bases = [input_dir / "Maps", input_dir / "maps"]
    found: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for pattern in (
            "Map *.jpg",
            "Map *.jpeg",
            "Map *.png",
            "**/Map *.jpg",
            "**/Map *.jpeg",
            "**/Map *.png",
        ):
            for src in sorted(base.glob(pattern)):
                key = src.name.lower()
                if key in seen:
                    continue
                seen.add(key)
                found.append(src)
    return found


def slugify(text: str) -> str:
    text = text.lower().replace("'", "").replace("'", "").replace("`", "")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def load_toc(doc: fitz.Document) -> list[tuple[int, str, int]]:
    """Return level-1 TOC entries: (level, title, page_1based)."""
    items = []
    for level, title, page in doc.get_toc():
        if page < 1:
            continue
        items.append((level, title.strip(), page))
    return items


def articles_from_toc(
    toc: list[tuple[int, str, int]],
    *,
    book: str = "book2",
    slug_prefix: str = "",
    book_label: str = "Book II",
) -> list[dict]:
    """
    Build ordered article list from level-1 TOC (skip Contents, Index).
    Each article: title, slug, start_page, end_page, kind, book
    """
    level1 = [(t, p) for level, t, p in toc if level == 1]
    articles = []
    for i, (title, start) in enumerate(level1):
        end = level1[i + 1][1] - 1 if i + 1 < len(level1) else None
        low = title.lower()
        if low in ("contents", "index") or low.startswith("index"):
            continue
        kind = "maps" if low == "maps" else "article"
        base_slug = slugify(title)
        articles.append(
            {
                "title": title,
                "slug": f"{slug_prefix}{base_slug}",
                "start_page": start,
                "end_page": end,
                "kind": kind,
                "book": book,
                "book_label": book_label,
            }
        )
    return articles


def finalize_article_ranges(articles: list[dict], page_count: int) -> None:
    """Fill missing end_page values from the next article / doc length."""
    if articles and articles[-1]["end_page"] is None:
        articles[-1]["end_page"] = page_count
    for i, art in enumerate(articles):
        if art["end_page"] is None:
            if i + 1 < len(articles):
                art["end_page"] = articles[i + 1]["start_page"] - 1
            else:
                art["end_page"] = page_count


def _undouble_words(text: str) -> str:
    """Turn 'An An old old scroll scroll' into 'An old scroll'."""
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


def _arcana_title_from_pdf(doc: fitz.Document, page_1based: int) -> str:
    """Read the card title from the first content lines of an arcana page."""
    from collections import defaultdict

    page = doc[page_1based - 1]
    words = page.get_text("words")
    by_y: dict[float, list] = defaultdict(list)
    for w in words:
        # titles sit in the left column on card pages
        if w[0] < 210:
            by_y[round(w[1] * 2) / 2].append(w)

    skip_re = re.compile(
        r"^(appendix|index|front|back|\d+$|,|"
        r"magical|fragile|immobile|terrifying|crude|slow|"
        r"beautiful|warm|close|reach|awkward|indestructible|"
        r"implanted|large)\b",
        re.I,
    )
    tag_only = re.compile(
        r"^[\s,_]*(?:magical|fragile|immobile|terrifying|crude|slow|"
        r"beautiful|warm|close|reach|awkward|indestructible|implanted|"
        r"large|armor|damage)[\s,_\d+]*$",
        re.I,
    )

    candidates: list[str] = []
    for y in sorted(by_y):
        raw = " ".join(t[4] for t in sorted(by_y[y], key=lambda x: x[0]))
        raw = _undouble_words(raw.strip())
        raw = re.sub(r"\s+", " ", raw)
        if not raw:
            continue
        if "appendix" in raw.lower():
            continue
        if skip_re.match(raw) or tag_only.match(raw):
            if candidates:
                break
            continue
        candidates.append(raw.rstrip(",").strip())
        if len(candidates) >= 4:
            break

    parts: list[str] = []
    for i, raw in enumerate(candidates):
        if tag_only.match(raw) or skip_re.match(raw):
            break
        parts.append(raw)
        joined = " ".join(parts)
        # Pull one more line if title clearly wraps
        nxt = candidates[i + 1] if i + 1 < len(candidates) else ""
        # Don't pull body prose into the title
        nl = nxt.lower() if nxt else ""
        if nxt and (
            nl in {"when", "when you"}
            or nl.startswith(
                ("when ", "when you", "a leather", "this ", "about ", "at the ")
            )
        ):
            wraps = False
        else:
            wraps = (
                raw.endswith(",")
                or raw.endswith("-")
                or (
                    nxt
                    and (nxt[0:1].islower() or len(nxt.split()) <= 2)
                    and not tag_only.match(nxt)
                    and not skip_re.match(nxt)
                    and nl not in {"when"}
                )
            )
        if not wraps and len(joined) >= 8:
            break
        if len(parts) >= 3:
            break

    title = " ".join(parts).strip(" ,")
    title = re.sub(r"\s+", " ", title)
    title = _undouble_words(title)
    # Title-case lightly if all lowercase-ish
    if title and title == title.lower():
        title = title.title()
    return title or f"Arcana (p. {page_1based})"


# Canonical major arcana names (PDF titles often wrap mid-word)
MAJOR_ARCANA_BY_PAGE = {
    540: "Staff of the Lidless Orb",
    542: "Twisted Spear",
    544: "Demonhide Cloak",
    546: "Noruba's Ice Sphere",
    548: "Mindgem",
    550: "Whispering Rocks",
    552: "Blood-quenched Sword",
    554: "Shield of the Wisent Witch",
    556: "Hec'tumel Codex",
    558: "Red Scepter",
    560: "Ring of Daagon",
    562: "Rune-laden Scales",
    564: "Blackwood Fetishes",
    566: "Storm Markings",
    568: "Ineffable Words",
    570: "Redwood Effigy",
    572: "Hungering Maw of Hlad",
    574: "Azure Hand",
}


def expand_arcana_articles(
    doc: fitz.Document, articles: list[dict]
) -> list[dict]:
    """
    Replace bulk Minor/Major Arcana appendix entries with a hub page plus
    one article per arcanum.

    Minor: 2 cards per PDF page (top/bottom), front+back → 64 total.
    Major: 2 PDF pages per arcanum → 18 total.
    """
    out: list[dict] = []
    for art in articles:
        low = art["title"].lower()
        is_minor = "minor arcana" in low
        is_major = "major arcana" in low
        if not (is_minor or is_major):
            out.append(art)
            continue

        start = art["start_page"]
        end = art["end_page"] or start
        kind_prefix = "minor" if is_minor else "major"
        children: list[dict] = []

        if is_minor:
            cards = list_minor_arcana_cards(doc, start, end)
            for card in cards:
                title = card["title"]
                slug = f"{kind_prefix}-{slugify(title)}"
                base_slug = slug
                n = 2
                while any(c["slug"] == slug for c in children):
                    slug = f"{base_slug}-{n}"
                    n += 1
                children.append(
                    {
                        "title": title,
                        "slug": slug,
                        "start_page": card["start_page"],
                        "end_page": card["end_page"],
                        "kind": "arcana",
                        "arcana_type": "minor",
                        "hub_slug": art["slug"],
                        "half": card["half"],  # top | bottom
                        "book": art.get("book", "book2"),
                        "book_label": art.get("book_label", ""),
                    }
                )
        else:
            p = start
            while p <= end:
                card_end = min(p + 1, end)
                if p in MAJOR_ARCANA_BY_PAGE:
                    title = MAJOR_ARCANA_BY_PAGE[p]
                else:
                    title = _arcana_title_from_pdf(doc, p)
                    for sp, name in MAJOR_ARCANA_BY_PAGE.items():
                        if abs(sp - p) <= 1:
                            title = name
                            break
                slug = f"{kind_prefix}-{slugify(title)}"
                base_slug = slug
                n = 2
                while any(c["slug"] == slug for c in children):
                    slug = f"{base_slug}-{n}"
                    n += 1
                children.append(
                    {
                        "title": title,
                        "slug": slug,
                        "start_page": p,
                        "end_page": card_end,
                        "kind": "arcana",
                        "arcana_type": "major",
                        "hub_slug": art["slug"],
                        "book": art.get("book", "book2"),
                        "book_label": art.get("book_label", ""),
                    }
                )
                p = card_end + 1

        hub = {
            **art,
            "kind": "arcana-hub",
            "children": [
                {"title": c["title"], "slug": c["slug"]} for c in children
            ],
        }
        out.append(hub)
        out.extend(children)
        print(
            f"  Split {art['title']}: {len(children)} individual pages "
            f"({start}-{end})"
        )
    return out


def extract_section_html_blocks(body: str, section_meta: list[dict]) -> dict[str, dict]:
    """
    Pull full HTML for each deep-link target from a page body.

    Returns {id: {name, html, kind}} for stat-blocks, roll-tables, and
    heading sections (heading + following content until next same/higher heading).
    """
    name_by_id = {s["id"]: s.get("name") or s["id"] for s in (section_meta or [])}
    out: dict[str, dict] = {}

    # Self-contained blocks: stat-block, roll-table, value-table
    for m in re.finditer(
        r'<(div)\s+class="(stat-block|roll-table|value-table)"(\s+id="([^"]+)")?',
        body,
    ):
        tag, kind, _idattr, sid = m.group(1), m.group(2), m.group(3), m.group(4)
        if not sid:
            continue
        start = m.start()
        # Find matching close for this outer div (no nested same-class assumed for stat-block)
        depth = 0
        i = start
        end = None
        while i < len(body):
            open_m = re.match(r"<div\b", body[i:], re.I)
            close_m = re.match(r"</div\s*>", body[i:], re.I)
            if open_m:
                depth += 1
                i += open_m.end()
                continue
            if close_m:
                depth -= 1
                i += close_m.end()
                if depth == 0:
                    end = i
                    break
                continue
            i += 1
        if end is None:
            continue
        html_block = body[start:end]
        out[sid] = {
            "name": name_by_id.get(sid, sid.replace("-", " ").title()),
            "html": html_block,
            "kind": kind,
        }

    # Heading sections: <h2 id="..."> / <h3 id="...">
    for m in re.finditer(r'<(h([23]))\s+id="([^"]+)"[^>]*>', body, re.I):
        tag, level, sid = m.group(1), int(m.group(2)), m.group(3)
        if sid in out:
            continue  # prefer block extraction if somehow shared
        start = m.start()
        # End at next heading of same or higher level
        rest = body[m.end() :]
        end_rel = len(rest)
        for hm in re.finditer(r"<h([1-6])\b", rest, re.I):
            if int(hm.group(1)) <= level:
                end_rel = hm.start()
                break
        # Also stop before image gallery
        gal = rest.find('<section class="image-gallery"')
        if gal != -1 and gal < end_rel:
            end_rel = gal
        chunk = body[start : m.end() + end_rel].strip()
        # Skip empty-ish headings with almost no content
        text_only = re.sub(r"<[^>]+>", " ", chunk)
        if len(text_only.strip()) < 8:
            continue
        out[sid] = {
            "name": name_by_id.get(sid, sid.replace("-", " ").title()),
            "html": chunk,
            "kind": "section",
        }

    return out


def arcana_hub_html(art: dict) -> str:
    """Index body for Minor/Major Arcana hub pages."""
    kids = art.get("children") or []
    kind = "Minor" if "minor" in art["title"].lower() else "Major"
    items = []
    for c in kids:
        items.append(
            f'<li><a class="wiki-link" href="{html.escape(c["slug"])}.html" '
            f'data-slug="{html.escape(c["slug"])}">'
            f'{html.escape(c["title"])}</a></li>'
        )
    return (
        f"<p>Individual {kind.lower()} arcana from Book II. "
        f"Each entry is its own page.</p>"
        f'<div class="arcana-index"><h2>All {kind} Arcana</h2>'
        f"<ol>{''.join(items)}</ol></div>"
    )


def build_page_lookup(articles: list[dict]) -> dict[int, dict]:
    """Every page number -> article for linking."""
    lookup: dict[int, dict] = {}
    ordered = sorted(articles, key=lambda a: a["start_page"])
    for i, art in enumerate(ordered):
        start = art["start_page"]
        end = art["end_page"]
        if end is None:
            end = ordered[i + 1]["start_page"] - 1 if i + 1 < len(ordered) else start
        for p in range(start, end + 1):
            lookup[p] = art
    return lookup


def page_shell(
    title: str,
    slug: str,
    body_html: str,
    articles: list[dict],
    rel_prefix: str = "../",
    section_navs: dict[str, list[dict]] | None = None,
    content_class: str = "content",
) -> str:
    section_navs = section_navs or {}
    nav_items = []
    for art in articles:
        is_current = art["slug"] == slug
        classes: list[str] = []
        if art.get("kind") == "arcana":
            classes.append("nav-arcana")
        elif art.get("kind") == "arcana-hub":
            classes.append("nav-hub")
        secs = section_navs.get(art["slug"]) or []
        if secs:
            classes.append("has-sections")
        if is_current:
            classes.append("current")
        cls_attr = f' class="{" ".join(classes)}"' if classes else ""
        art_slug = html.escape(art["slug"])
        link = (
            f'<a href="{art_slug}.html">{html.escape(art["title"])}</a>'
        )
        if secs:
            # Nested deep links into chapter sections (from first-page TOC)
            sub = []
            for sec in secs:
                sid = html.escape(sec["id"])
                sname = html.escape(sec["name"])
                sub.append(
                    f'<li class="nav-section">'
                    f'<a href="{art_slug}.html#{sid}">{sname}</a></li>'
                )
            nav_items.append(
                f"<li{cls_attr}>{link}"
                f'<ul class="nav-sections">{"".join(sub)}</ul></li>'
            )
        else:
            nav_items.append(f"<li{cls_attr}>{link}</li>")
    nav_html = "\n".join(nav_items)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} — Stonetop Wiki</title>
  <link rel="stylesheet" href="{rel_prefix}css/wiki.css">
</head>
<body>
  <a class="skip-link" href="#main">Skip to content</a>
  <button type="button" class="sidebar-toggle" id="sidebar-toggle" aria-label="Toggle navigation">☰</button>
  <div class="layout">
    <aside class="sidebar" id="sidebar">
      <div class="sidebar-head">
        <a class="site-title" href="{rel_prefix}index.html">Stonetop Wiki</a>
        <input type="search" id="nav-filter" class="nav-filter" placeholder="Search wiki…" autocomplete="off" aria-label="Search wiki">
        <div id="search-results" class="search-results" hidden></div>
      </div>
      <nav class="toc" aria-label="Topics">
        <ul id="nav-list">
          {nav_html}
        </ul>
      </nav>
    </aside>
    <div class="main-wrap">
      <div class="content-scroll" id="main">
        <main class="{content_class}">
          {body_html}
        </main>
      </div>
    </div>
  </div>
  <div id="wiki-preview" class="wiki-preview" hidden></div>
  <div id="dice-toast" class="dice-toast" hidden></div>
  <script src="{rel_prefix}js/wiki.js"></script>
</body>
</html>
"""


def prepare_map_images(
    doc: fitz.Document,
    maps_art: dict,
    img_dir: Path,
    input_dir: Path,
) -> list[dict]:
    """
    Copy high-quality campaign maps and render PDF map spreads for the Maps page.
    Returns ordered list of image meta dicts for maps_body_html.
    """
    out: list[dict] = []
    maps_out = img_dir / "maps"
    maps_out.mkdir(parents=True, exist_ok=True)

    # 1) Optional HQ campaign maps from Maps/ (any nested layout).
    #    Keep one large view per distinct map: use only the 11x14 sheets
    #    (drop the redundant 8.5x11 and A4 print sizes), and skip The Vicinity
    #    and The World's End — the labeled book spreads below already cover them.
    for src in find_campaign_map_jpgs(input_dir):
        stem_low = src.stem.lower()
        if "11 x 14" not in stem_low:
            continue
        if "vicinity" in stem_low or "world" in stem_low:
            continue
        ext = src.suffix.lower() or ".jpg"
        dest_name = slugify(src.stem) + ext
        dest = maps_out / dest_name
        try:
            shutil.copy2(src, dest)
        except OSError:
            if not dest.exists():
                continue
        label = src.stem
        # "Map 1 - Stonetop - 11 x 14" → "Map 1 — Stonetop"
        label = re.sub(r"\s*-\s*11\s*x\s*14\s*$", "", label, flags=re.I)
        label = re.sub(r"\s*-\s*A4\s*$", "", label, flags=re.I)
        label = re.sub(r"\s*-\s*8\.?5\s*x\s*11\s*$", "", label, flags=re.I)
        label = re.sub(r"\s*-\s*", " — ", label, count=1)
        out.append(
            {
                "file": f"maps/{dest_name}",
                "label": label,
                "hq": True,
            }
        )

    # 2) Full-page renders of the PDF Maps chapter
    start = (maps_art.get("start_page") or 1) - 1  # 0-based
    end = maps_art.get("end_page") or maps_art.get("start_page") or 1  # 1-based incl.
    for pno in range(start, end):
        if pno < 0 or pno >= doc.page_count:
            continue
        page = doc[pno]
        mat = fitz.Matrix(1.5, 1.5)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        fname = f"map_page_{pno + 1}.jpg"
        dest = maps_out / fname
        pix.save(str(dest))
        out.append(
            {
                "file": f"maps/{fname}",
                "label": f"PDF map spread (p. {pno + 1})",
                "fullpage": True,
                "page": pno + 1,
            }
        )

    print(f"  Maps: {len(out)} images prepared")
    return out


MAP_PIN_COLORS = [
    "#e2534a",  # red
    "#e08a3c",  # orange
    "#e6c34a",  # yellow
    "#5aa85a",  # green
    "#4a90d9",  # blue
    "#9b6dc4",  # purple
]


def maps_body_html(images: list[dict]) -> str:
    """Render the Maps page: one full-height horizontal strip. The labeled book
    map spreads come first, then the campaign maps. Each map is a pin canvas;
    a floating toolbar drops colored, labeled pins (saved in localStorage)."""
    hq = [i for i in images if i.get("hq")]
    spreads = sorted(
        (i for i in images if i.get("fullpage")),
        key=lambda i: i.get("page", 0),
    )

    def canvas(im: dict, *, full: bool) -> str:
        src = html.escape(f"../images/{im['file']}")
        alt = html.escape(im.get("label") or "Map")
        map_id = html.escape(Path(im["file"]).name)
        full_attr = f' data-full="{src}"' if full else ""
        return (
            f'<div class="map-canvas" data-map="{map_id}"{full_attr}>'
            f'<img src="{src}" alt="{alt}" loading="lazy" draggable="false">'
            f"</div>"
        )

    items = [canvas(im, full=False) for im in spreads]
    items += [canvas(im, full=True) for im in hq]
    if not items:
        return ""

    swatches = "".join(
        f'<button type="button" class="map-color" data-color="{c}" '
        f'style="--sw:{c}" aria-label="Pin colour {c}"></button>'
        for c in MAP_PIN_COLORS
    )
    tools = (
        '<div class="map-tools" id="map-tools">'
        '<button type="button" class="map-add" id="map-add" aria-pressed="false">'
        "\U0001f4cd Add pin</button>"
        f'<div class="map-colors" id="map-colors">{swatches}</div>'
        "</div>"
    )
    return f'{tools}<div class="maps-strip">{"".join(items)}</div>'


def copy_static_assets(out: Path) -> None:
    """
    Copy everything under static/ into the wiki output (css/, js/, …).

    Generated data files (previews-data.js, search-index.js, …) are written
    later by main() and may live alongside these copied assets.
    """
    static_root = Path(__file__).resolve().parent / "static"
    if not static_root.is_dir():
        raise SystemExit(f"static assets folder not found: {static_root}")

    n = 0
    for src in sorted(static_root.rglob("*")):
        if not src.is_file():
            continue
        dest = out / src.relative_to(static_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        n += 1
        print(f"  copied static/{src.relative_to(static_root).as_posix()}")
    if n == 0:
        raise SystemExit(f"no static assets found under {static_root}")
    print(f"  static assets: {n} file(s)")


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

def write_index_custom(articles: list[dict], previews: dict, out_path: Path) -> None:
    nav_items = []
    for art in articles:
        extra = ""
        if art.get("kind") == "arcana":
            extra = ' class="nav-arcana"'
        elif art.get("kind") == "arcana-hub":
            extra = ' class="nav-hub"'
        nav_items.append(
            f'<li{extra}><a href="pages/{html.escape(art["slug"])}.html">'
            f'{html.escape(art["title"])}</a></li>'
        )

    cards = []
    for art in articles:
        if art.get("kind") == "arcana":
            continue
        pv = previews.get(art["slug"], {})
        excerpt = pv.get("excerpt") or ""
        cards.append(
            f'<a class="index-card" href="pages/{art["slug"]}.html">'
            f'<p class="card-title">{html.escape(art["title"])}</p>'
            f'<p class="card-excerpt">{html.escape(excerpt)}</p></a>'
        )

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stonetop Wiki</title>
  <link rel="stylesheet" href="css/wiki.css">
</head>
<body>
  <a class="skip-link" href="#main">Skip to content</a>
  <button type="button" class="sidebar-toggle" id="sidebar-toggle" aria-label="Toggle navigation">☰</button>
  <div class="layout">
    <aside class="sidebar" id="sidebar">
      <div class="sidebar-head">
        <a class="site-title" href="index.html">Stonetop Wiki</a>
        <input type="search" id="nav-filter" class="nav-filter" placeholder="Search wiki…" autocomplete="off" aria-label="Search wiki">
        <div id="search-results" class="search-results" hidden></div>
      </div>
      <nav class="toc" aria-label="Topics">
        <ul id="nav-list">
          {''.join(nav_items)}
        </ul>
      </nav>
    </aside>
    <div class="main-wrap">
      <div class="content-scroll" id="main">
        <main class="content">
        <div class="index-hero">
          <p class="lede">A static, hyperlinked wiki for <em>Stonetop</em>
          <strong>Book II — The Wider World</strong>.
          Page numbers are links; dice expressions roll on click; hover a link for a preview
          (full stat blocks when deep-linked). Scroll sideways through columns on topic pages.</p>
        </div>
        <h2>Topics</h2>
        <div class="index-grid">
          {''.join(cards)}
        </div>
        </main>
      </div>
    </div>
  </div>
  <div id="wiki-preview" class="wiki-preview" hidden></div>
  <div id="dice-toast" class="dice-toast" hidden></div>
  <script src="js/wiki.js"></script>
</body>
</html>
"""
    out_path.write_text(html_out, encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    input_dir = args.input.expanduser().resolve()
    out = (
        args.output.expanduser().resolve()
        if args.output is not None
        else (input_dir / "Stonetop_Wiki").resolve()
    )
    pdf_path = input_dir / PDF_FILENAME
    legacy_out = input_dir / "Book_II_Wiki"

    print("Building Stonetop wiki (Book II)…")
    print(f"  input:  {input_dir}")
    print(f"  output: {out}")
    if not pdf_path.exists():
        raise SystemExit(
            f"PDF not found: {pdf_path}\n"
            f"Place the Book II 1-up PDF ({PDF_FILENAME}) in the input folder,\n"
            "or pass --input pointing at the folder that contains it."
        )

    if legacy_out.is_dir() and not out.exists():
        try:
            legacy_out.rename(out)
            print(f"  Renamed {legacy_out.name} → {out.name}")
        except OSError as e:
            print(f"  note: could not rename legacy folder ({e}); building fresh")

    for sub in ("pages", "css", "js", "images", "images/icons"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    for old in (out / "pages").glob("*.html"):
        try:
            old.unlink()
        except OSError:
            pass

    # Drop previous PDF-extracted icon bitmaps; static SVGs are re-copied below.
    icons_out = out / "images" / "icons"
    for old in icons_out.glob("*"):
        try:
            old.unlink()
        except OSError:
            pass

    copy_static_assets(out)
    icon_dir = icons_out

    doc = fitz.open(str(pdf_path))
    toc = load_toc(doc)
    articles = articles_from_toc(
        toc,
        book="book2",
        slug_prefix="",
        book_label="Book II",
    )
    finalize_article_ranges(articles, doc.page_count)
    articles = expand_arcana_articles(doc, articles)

    print("Articles:")
    for art in articles:
        if art.get("kind") == "arcana":
            continue
        print(
            f"  {art['start_page']:4d}-{art['end_page']:4d}  "
            f"{art['slug'][:42]:42s}  {art.get('kind') or 'article'}"
        )
    n_arc = sum(1 for a in articles if a.get("kind") == "arcana")
    print(f"  (+ {n_arc} individual arcana pages)")
    print(f"  Total pages: {len(articles)}")

    lookup = build_page_lookup(articles)
    lookups = {"book2": lookup}
    set_title_index(articles)
    previews: dict[str, dict] = {}

    # Campaign maps + PDF map spreads (maps page only)
    maps_art = next((a for a in articles if a.get("kind") == "maps"), None)
    map_images: list[dict] = []
    if maps_art:
        print("Preparing maps…")
        map_images = prepare_map_images(doc, maps_art, out / "images", input_dir)

    print("Indexing sections (for deep links)…")
    lines_cache: dict[str, list[str]] = {}
    toc_cache: dict[str, list[str]] = {}
    sections_by_slug: dict[str, list[dict]] = {}
    for art in articles:
        slug = art["slug"]
        if art["kind"] in ("maps", "arcana-hub"):
            sections_by_slug[slug] = []
            toc_cache[slug] = []
            continue
        if art.get("kind") == "arcana" and art.get("arcana_type") == "minor":
            _t, lines = extract_minor_arcana_card(
                doc, art["start_page"], art.get("half") or "top"
            )
            toc_labels: list[str] = []
            lines_cache[slug] = lines
            toc_cache[slug] = toc_labels
            _body, _ex, sections = minor_arcana_html_from_pdf(
                doc,
                art["start_page"],
                art.get("half") or "top",
                art["title"],
                lookup,
                articles,
                current_slug=slug,
                section_index=None,
                lines=lines,
                lookups=lookups,
                current_book="book2",
            )
        elif art.get("kind") == "arcana" and art.get("arcana_type") == "major":
            lines = extract_article_lines(
                doc,
                art["start_page"],
                art["end_page"],
                art["title"],
                icon_dir=icon_dir,
            )
            toc_labels = []
            lines_cache[slug] = lines
            toc_cache[slug] = toc_labels
            _body, _ex, sections = major_arcana_html_from_pdf(
                doc,
                art["start_page"],
                art["end_page"],
                art["title"],
                lookup,
                articles,
                current_slug=slug,
                section_index=None,
                lines=lines,
                lookups=lookups,
                current_book="book2",
            )
        else:
            lines = extract_article_lines(
                doc,
                art["start_page"],
                art["end_page"],
                art["title"],
                icon_dir=icon_dir,
            )
            toc_labels, lines = split_chapter_toc(lines, art["title"])
            lines_cache[slug] = lines
            toc_cache[slug] = toc_labels
            _body, _ex, sections = article_html_from_pdf(
                doc,
                art["start_page"],
                art["end_page"],
                art["title"],
                lookup,
                articles,
                current_slug=slug,
                section_index=None,
                lines=lines,
                lookups=lookups,
                current_book="book2",
            )
        sections_by_slug[slug] = sections

    section_navs: dict[str, list[dict]] = {}
    for slug, toc_labels in toc_cache.items():
        if not toc_labels:
            continue
        matched = match_toc_to_sections(toc_labels, sections_by_slug.get(slug) or [])
        if matched:
            section_navs[slug] = matched
    if section_navs:
        print(
            f"  Chapter section nav: {len(section_navs)} pages, "
            f"{sum(len(v) for v in section_navs.values())} deep links"
        )

    section_index = build_section_index(sections_by_slug, articles)
    section_indexes = {"book2": section_index}
    n_sec = sum(len(v) for v in sections_by_slug.values())
    print(f"  Indexed {n_sec} sections/monsters across {len(sections_by_slug)} pages")

    print("Building pages…")
    search_docs: list[dict] = []
    for art in articles:
        slug = art["slug"]
        body = ""
        excerpt = ""
        card_preview_html = None

        if art["kind"] == "maps":
            body = maps_body_html(map_images)
            page_html = page_shell(
                art["title"],
                slug,
                body,
                articles,
                rel_prefix="../",
                section_navs=section_navs,
                content_class="content maps-page",
            )
            excerpt = (
                "Maps of Stonetop, the vicinity, and the World's End — "
                "campaign sheets and PDF spreads."
            )
        elif art.get("kind") == "arcana-hub":
            body = arcana_hub_html(art)
            excerpt = (
                f"Index of {len(art.get('children') or [])} individual arcana entries."
            )
            page_html = page_shell(
                art["title"],
                slug,
                body,
                articles,
                rel_prefix="../",
                section_navs=section_navs,
            )
        else:
            lines = lines_cache.get(slug)
            if art.get("kind") == "arcana" and art.get("arcana_type") == "minor":
                body, excerpt, _secs = minor_arcana_html_from_pdf(
                    doc,
                    art["start_page"],
                    art.get("half") or "top",
                    art["title"],
                    lookup,
                    articles,
                    current_slug=slug,
                    section_index=section_index,
                    lines=lines,
                    lookups=lookups,
                    section_indexes=section_indexes,
                    current_book="book2",
                )
            elif art.get("kind") == "arcana" and art.get("arcana_type") == "major":
                body, excerpt, _secs = major_arcana_html_from_pdf(
                    doc,
                    art["start_page"],
                    art["end_page"],
                    art["title"],
                    lookup,
                    articles,
                    current_slug=slug,
                    section_index=section_index,
                    lines=lines,
                    lookups=lookups,
                    section_indexes=section_indexes,
                    current_book="book2",
                )
            else:
                body, excerpt, _secs = article_html_from_pdf(
                    doc,
                    art["start_page"],
                    art["end_page"],
                    art["title"],
                    lookup,
                    articles,
                    current_slug=slug,
                    section_index=section_index,
                    lines=lines,
                    lookups=lookups,
                    section_indexes=section_indexes,
                    current_book="book2",
                )
            # Keep pure card HTML for hover previews (before nav chrome)
            if art.get("kind") == "arcana":
                card_preview_html = body
            if art.get("kind") == "arcana" and art.get("hub_slug"):
                hub = art["hub_slug"]
                hub_title = (
                    "Minor Arcana"
                    if art.get("arcana_type") == "minor"
                    else "Major Arcana"
                )
                body = (
                    f'<p class="arcana-back"><a class="wiki-link" href="{hub}.html" '
                    f'data-slug="{hub}">← All {hub_title}</a></p>\n'
                    + body
                )
            page_html = page_shell(
                art["title"],
                slug,
                body,
                articles,
                rel_prefix="../",
                section_navs=section_navs,
            )

        section_blocks: dict[str, dict] = {}
        if art["kind"] not in ("maps", "arcana-hub") and body:
            section_blocks = extract_section_html_blocks(
                body, sections_by_slug.get(slug, [])
            )
            for s in sections_by_slug.get(slug, []):
                if s["id"] not in section_blocks:
                    section_blocks[s["id"]] = {
                        "name": s["name"],
                        "html": "",
                        "kind": "section",
                    }

        thumb = None
        if art["kind"] == "maps" and map_images:
            thumb = map_images[0].get("file")
        previews[slug] = {
            "title": art["title"],
            "excerpt": excerpt,
            "image": thumb,
            "book": "book2",
            "sections": section_blocks,
        }
        if card_preview_html:
            previews[slug]["html"] = card_preview_html
            previews[slug]["kind"] = "arcana"
        (out / "pages" / f"{slug}.html").write_text(page_html, encoding="utf-8")

        search_text = html_to_search_text(body)
        section_names = " ".join(
            s.get("name") or "" for s in (sections_by_slug.get(slug) or [])
        )
        combined = f"{art['title']}\n{section_names}\n{search_text}".strip()
        if len(combined) > 80_000:
            combined = combined[:80_000]
        search_docs.append(
            {
                "slug": slug,
                "title": art["title"],
                "book": "book2",
                "excerpt": (excerpt or "")[:280],
                "text": combined,
            }
        )

    previews_json = json.dumps(previews, ensure_ascii=False, indent=2)

    page_map: dict[str, dict] = {}
    for pnum, art in lookup.items():
        slug = art["slug"]
        sec_map = {}
        for s in sections_by_slug.get(slug, []):
            sec_map[s["norm"]] = s["id"]
            sec_map[s["name"].lower()] = s["id"]
        page_map[str(pnum)] = {
            "slug": slug,
            "title": art["title"],
            "sections": sec_map,
        }
    page_maps = {"book2": page_map}
    page_map_json = json.dumps(page_maps, ensure_ascii=False, indent=2)
    # JS globals (not separate JSON) so hover previews work over file://
    (out / "js" / "previews-data.js").write_text(
        "window.WIKI_PREVIEWS = "
        + previews_json
        + ";\nwindow.WIKI_PAGE_MAP = "
        + page_map_json
        + ";\n",
        encoding="utf-8",
    )
    search_json = json.dumps(search_docs, ensure_ascii=False, separators=(",", ":"))
    (out / "js" / "search-index.js").write_text(
        "window.WIKI_SEARCH_INDEX = " + search_json + ";\n",
        encoding="utf-8",
    )
    print(f"  Search index: {len(search_docs)} pages, {len(search_json)//1024} KB")
    write_index_custom(articles, previews, out / "index.html")
    print(f"Done. Open {out / 'index.html'}")


if __name__ == "__main__":
    main()

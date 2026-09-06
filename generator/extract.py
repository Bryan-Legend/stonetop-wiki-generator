"""
PDF → marker lines (the only module that needs PyMuPDF).

``extract_page_rich`` reads span fonts, vector drawings and small images off a
1-up page and emits ````-prefixed marker lines; ``extract_article_lines``
runs it over an article's page range and merges wrapped lines. The arcana
extractors clip the same reader to a card's region. Map image extraction lives
here too, since it reads the PDF.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

try:
    import fitz  # PyMuPDF — only needed to extract from the PDFs
except ImportError:  # pragma: no cover
    fitz = None

from .text import (
    M_FACE,
    B_OFF,
    B_ON,
    I_OFF,
    I_ON,
    MARKER_RE,
    M_B,
    M_B2,
    M_BAND,
    M_BC,
    M_BOX,
    M_C,
    M_C2,
    M_CX,
    M_CX2,
    M_E,
    M_ENDBOX,
    M_H2,
    M_H3,
    M_H4,
    M_HR,
    M_ICON,
    M_MARK,
    M_Q,
    M_STATS,
    M_STEP,
    M_TH,
    M_VA,
    M_VF,
    M_VR,
    M_VT,
    M_WRITE,
    VAL_TOKEN_RE,
    _cancel_fmt_seam,
    _defmt,
    _is_all_caps_label,
    _split_leading_fmt,
    _split_trailing_fmt,
    is_fully_pairwise_doubled,
    is_running_header,
    italic_coverage,
    looks_like_heading,
    normalize_text,
    should_join,
    slugify,
    strip_markers,
    undouble_words,
)


# ---------------------------------------------------------------------------
# PDF extraction & HTML structuring (was wiki_content.py)
# ---------------------------------------------------------------------------

# Mid-page gutter for typical Stonetop 1-up pages (~396pt wide, 2 columns)
DEFAULT_GUTTER = 198
# Scale / compass type sitting on a dungeon map image (Book I Sites p. 373).
MAP_SCALE_RE = re.compile(
    r"^\d+\s*(?:feet|foot|ft|yd|m|mi|paces?)\.?$", re.I
)
MAP_COMPASS = {"N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST"}


def _span_mid_x(sp: dict) -> float:
    """Horizontal centre — column assignment uses this, not x0.

    Right-column type often starts a fraction of a point left of mid-page
    (Book I Sites: 'Exterior' at x=197.8 with gutter 198). Using the left
    edge dumps that whole column into the left one, and build_lines then
    glues same-baseline pairs ('As they approach Exterior').
    """
    return (sp["x"] + sp["x1"]) / 2


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



# PDF image xrefs → semantic game-icons.net assets (images/icons/*.svg in the wiki).
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
    Map a PDF category-icon xref to a game-icons SVG under *icon_dir*.

    Returns path relative to images/ (e.g. ``icons/beast.svg``). Icons ship
    with the wiki folder; this never copies files.
    """
    if not xref:
        return None
    name = ICON_XREF_TO_NAME.get(int(xref), "default")
    fname = f"{name}.svg"
    if icon_dir is not None:
        icon_dir = Path(icon_dir)
        if not (icon_dir / fname).is_file():
            if (icon_dir / "default.svg").is_file():
                fname = "default.svg"
            else:
                return None
    return f"icons/{fname}"


# Per-book typography conventions.
#
# Book II sets chapter titles at 24pt Avara-Bold and everything smaller is a
# real heading, so anything >= 16pt is page furniture to drop (the 18pt runs
# are chapter-title continuations like "Aratis, " / "the Lawkeeper").
# Book I sets *section* headings at 20pt and playbook titles at 24pt — the same
# size as its chapter titles, which the running-header/article-title guards
# drop anyway — so only the decorative 32pt+ type is furniture there. Section
# headings in both books wrap across lines ("The" / "conversation",
# "Should the players" / "read this?").
BOOK_TYPE_STYLE: dict[str, dict] = {
    "book1": {"heading_max_size": 28.0, "merge_wrapped_headings": True},
    "book2": {"heading_max_size": 16.0, "merge_wrapped_headings": True},
}


# Display faces the maps letter their labels in (the body is ACaslon/Avara).
MAP_LABEL_FONTS = ("FellType", "FeltTip")


def map_label_spans(page: fitz.Page) -> set:
    """Keys of text spans that belong to a picture, not to the article.

    Map lettering is stroked and then filled, so every label is drawn twice at
    identical coordinates; body type never is. That alone isn't enough (Book
    II's running heads are double-printed too), so a label also has to sit
    inside an illustration and be set in one of the map faces. Once five of
    those turn up on a page the picture really is a map, and the rest of the
    double-printed type inside it goes as well — the title curving across the
    spread ("The World's End", one span per glyph) and italic asides like
    "to Lygos and other points south".

    Without this the region maps bleed into the prose: Book I's *The setting*
    picks up "M o f a n g" and "e h T", and Book II's *Makers* scatters
    "R i m e / L o r / d s" through the site table.
    """
    try:
        pics = [
            info["bbox"]
            for info in page.get_image_info()
            if info["bbox"][2] - info["bbox"][0] >= 100
            and info["bbox"][3] - info["bbox"][1] >= 100
        ]
    except Exception:
        return set()
    if not pics:
        return set()

    counts: dict = defaultdict(int)
    recs: dict = {}
    for b in page.get_text("dict").get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            for s in ln.get("spans", []):
                txt = (s.get("text") or "").strip()
                if not txt:
                    continue
                bb = s["bbox"]
                key = (round(bb[0], 1), round(bb[1], 1), txt)
                counts[key] += 1
                recs[key] = s

    # A label sits *on* the picture if its middle does — the spreads bleed off
    # the page, so the last glyph of a label ("Mofang", "mountains") hangs past
    # the image box and full containment would leave it stranded in the prose.
    def on(bbox, pic) -> bool:
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        return pic[0] - 6 <= cx <= pic[2] + 6 and pic[1] - 6 <= cy <= pic[3] + 6

    def in_map_face(span) -> bool:
        return any(f in (span.get("font") or "") for f in MAP_LABEL_FONTS)

    inside = [
        key
        for key, n in counts.items()
        if n >= 2 and any(on(recs[key]["bbox"], p) for p in pics)
    ]
    labels = {k for k in inside if in_map_face(recs[k])}
    if not labels:
        return set()

    # Pictures that letter themselves. A tinted panel behind a sidebar is an
    # image too, but nothing is double-printed on it, so it never qualifies and
    # its body text is safe.
    lettered = [p for p in pics if any(on(recs[k]["bbox"], p) for k in labels)]
    if len(labels) >= 5:
        # Five labels in and it really is a map: the rest of its double-printed
        # layer goes too — the title curving across the spread, italic asides.
        labels |= {k for k in inside if any(on(recs[k]["bbox"], p) for p in lettered)}
    # Ornament set in the same face but printed once — the rules flanking the
    # sample card's "minor arcanum" — is part of the picture as well.
    labels |= {
        k
        for k, s in recs.items()
        if in_map_face(s) and any(on(s["bbox"], p) for p in lettered)
    }
    # Dungeon-plan images (the Green Lord's Tomb on Sites p. 373) letter
    # room keys and a scale. The scale often sits a few points past the
    # image box; overlap with padding catches it without eating captions.
    for pic in pics:
        pw, ph = pic[2] - pic[0], pic[3] - pic[1]
        if pw < 150 or ph < 150:
            continue
        pad = 16.0
        for key, s in recs.items():
            bb = s["bbox"]
            if (
                bb[2] < pic[0] - pad
                or bb[0] > pic[2] + pad
                or bb[3] < pic[1] - pad
                or bb[1] > pic[3] + pad
            ):
                continue
            txt = key[2].strip()
            if (
                len(txt) <= 2
                or txt.upper() in MAP_COMPASS
                or MAP_SCALE_RE.match(txt)
            ):
                labels.add(key)
    return labels


# ---------------------------------------------------------------------------
# Playbook character sheets (Book I pp. 105-140)
#
# The nine sheets share one rigid four-page template: front (background,
# instinct, appearance, name box), stats + moves, special possessions + more
# moves, then the playbook's own sections and the introductions walkthrough.
# Column flow alone can't read them: the sheet is banded, and a band's two
# columns belong to that band only. Reading the page as two full-height
# columns deals the top band's right half into the middle of the bottom
# band's left one, which is how Special Possessions ended up buried in the
# move list and half the stat block landed between two moves.
# ---------------------------------------------------------------------------

# The six stats, in printed order, with the debility that dims each pair.
PLAYBOOK_STATS: tuple[tuple[str, str], ...] = (
    ("Strength", "STR"),
    ("Dexterity", "DEX"),
    ("Intelligence", "INT"),
    ("Wisdom", "WIS"),
    ("Constitution", "CON"),
    ("Charisma", "CHA"),
)
PLAYBOOK_DEBILITIES: tuple[tuple[str, tuple[str, str]], ...] = (
    ("weakened", ("STR", "DEX")),
    ("dazed", ("INT", "WIS")),
    ("miserable", ("CON", "CHA")),
)
# Second row of the stat block. HP carries the sheet's own cap, so it is read
# off the page rather than fixed here.
PLAYBOOK_TRACKS: tuple[str, ...] = ("Damage", "HP", "Armor", "XP", "Level")


def playbook_band_ys(page: fitz.Page) -> list[float]:
    """Y of every full-measure rule on the page — the sheet's band breaks.

    The rule is a hairline on some pages and a rough 2pt band on others, and
    it is drawn as vector art on some and as an image on others; where it is
    art it comes in overlapping pieces that bleed off the page. What tells it
    from the column hairlines above every section head is that it is *one*
    run across the whole measure, where a pair of column rules sharing a
    baseline leaves the gutter open between them.
    """
    segs: list[tuple[float, float, float]] = []
    for dr in page.get_drawings():
        r = dr["rect"]
        if r.height <= 4.5 and r.width >= 100:
            segs.append((r.y0, r.x0, r.x1))
    try:
        for info in page.get_image_info():
            b = info["bbox"]
            if (b[3] - b[1]) <= 4.5 and (b[2] - b[0]) >= 100:
                segs.append((b[1], b[0], b[2]))
    except Exception:
        pass
    segs.sort()
    clusters: list[list[tuple[float, float, float]]] = []
    for seg in segs:
        if clusters and abs(seg[0] - clusters[-1][0][0]) <= 3.0:
            clusters[-1].append(seg)
        else:
            clusters.append([seg])
    out: list[float] = []
    for cl in clusters:
        runs: list[list[float]] = []
        for _, x0, x1 in sorted(cl, key=lambda t: t[1]):
            if runs and x0 - runs[-1][1] <= 5.0:
                runs[-1][1] = max(runs[-1][1], x1)
            else:
                runs.append([x0, x1])
        if any(hi - lo >= 300 for lo, hi in runs):
            out.append(sum(t[0] for t in cl) / len(cl))
    return out


def playbook_stat_block(spans: list[dict], page_h: float) -> tuple[dict, set] | None:
    """Read the stat block off page 2 of a sheet.

    Returns the block and the ids of the spans it consumed, or None when this
    page has no stat block. Everything but the damage die, the HP cap and the
    score line is fixed by the game, so those three are all that is read.
    """
    head = next(
        (
            sp
            for sp in spans
            if sp["text"].strip() == "Stats"
            and sp["font"].startswith("Avara")
            and sp["y"] < 120
        ),
        None,
    )
    if head is None:
        return None
    # The block runs from its heading to the "Moves" head under it.
    end = next(
        (
            sp["y"]
            for sp in spans
            if sp["text"].strip() == "Moves"
            and sp["font"].startswith("Avara")
            and sp["y"] > head["y"]
        ),
        head["y"] + 150.0,
    )
    band = [sp for sp in spans if head["y"] - 2 <= sp["y"] < end - 2]
    text = {sp["text"].strip() for sp in band}

    gloss = next(
        (t for t in text if t.startswith("Assign these scores")), ""
    )
    die = next(
        (t for t in text if re.fullmatch(r"d\d+", t)), ""
    )
    hp = next(
        (t for t in text if re.fullmatch(r"HP \(max \d+\)", t)), "HP"
    )
    # Only claim the block when it really is one — all six stats present.
    if not all(
        any(sp["text"].strip() == name for sp in band)
        for name, _ in PLAYBOOK_STATS
    ):
        return None
    block = {
        "gloss": gloss,
        "die": die,
        "hp": hp,
        "stats": [list(pair) for pair in PLAYBOOK_STATS],
        "debilities": [[n, list(p)] for n, p in PLAYBOOK_DEBILITIES],
        "tracks": list(PLAYBOOK_TRACKS),
    }
    return block, {id(sp) for sp in band}


def playbook_write_box(
    page: fitz.Page, spans: list[dict]
) -> tuple[str, set] | None:
    """The sheet's write-in box ("I am called...") and the spans inside it."""
    best = None
    for dr in page.get_drawings():
        r = dr["rect"]
        if 100 <= r.width <= 350 and 18 <= r.height <= 70 and len(dr["items"]) >= 20:
            if best is None or r.height > best.height:
                best = r
    if best is None:
        return None
    inside = [
        sp
        for sp in spans
        if best.x0 - 2 <= sp["x"] and sp["x1"] <= best.x1 + 4
        and best.y0 - 2 <= sp["y"] and sp["y"] <= best.y1 + 4
        # The box sits in the bottom corner, where the folio sits too.
        and not sp["text"].strip().isdigit()
    ]
    if not inside:
        return None
    label = " ".join(
        sp["text"].strip() for sp in sorted(inside, key=lambda sp: (sp["y"], sp["x"]))
    ).strip()
    if not label:
        return None
    return label, {id(sp) for sp in inside}


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

    ``state["book_style"]`` ("book1" / "book2", default "book2") selects the
    per-book type conventions — see ``BOOK_TYPE_STYLE``.
    """
    if state is None:
        state = {}
    # Playbook sheets are banded and pre-printed; see playbook_band_ys().
    playbook = bool(state.get("playbook"))
    style = (
        BOOK_TYPE_STYLE.get(state.get("book_style") or "book2")
        or BOOK_TYPE_STYLE["book2"]
    )
    gutter = page.rect.width * 0.5
    if 350 < page.rect.width < 450:
        gutter = DEFAULT_GUTTER
    if single_column:
        gutter = page.rect.width + 1  # everything falls in the left column
    page_h = page.rect.height
    cy0, cy1 = y_clip if y_clip else (float("-inf"), float("inf"))

    map_labels = map_label_spans(page)

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
                if (round(x0, 1), round(y0, 1), txt.strip()) in map_labels:
                    continue  # lettering on the artwork, not the article
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
    badges: list = []  # rounded plaques the numbered steps sit in
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
                # Older/alternate encoding: 4-path outline diamond
                diamonds.append(r)
            elif ni == 1 and abs(w - h) <= 1.0:
                # Book II 1-up: inventory/uses diamonds are a single Quad path
                # (~6.6×6.6). Spiral bullets are multi-item (~5.4×5.7, ni≥15).
                items = dr.get("items") or []
                if items and items[0] and items[0][0] == "qu":
                    diamonds.append(r)
                elif 5.5 <= w <= 7.5:
                    diamonds.append(r)
            elif 7 <= ni <= 10 and sum(
                1 for it in dr["items"] if it[0] in ("l", "re")
            ) >= 2:
                # Open-square checkbox — a box has straight sides. Illustration
                # detail at this size is drawn entirely in Béziers (the eyes in
                # the playbook portraits beside "The characters"), and without
                # this it turns three playbook blurbs into a checklist.
                boxes_sq.append(r)
        elif w <= 4.5 and h <= 6.5 and 5 <= ni <= 9:
            tails.append(r)  # flourish on the question spiral
        elif w >= 40 and h <= 1.5 and ni <= 3:
            hrules.append(r)
        elif 11 <= w <= 45 and 12 <= h <= 25 and ni == 22:
            # Numbered-step plaque. Chapters set them at 15-45pt behind a
            # heading; the playbooks' introductions walkthrough uses the same
            # plaque two points smaller, beside body copy.
            badges.append(r)
        elif first_page and 110 <= w <= 320 and h >= 90 and box_rect is None:
            box_rect = r
    spirals = _dedupe_rects(spirals)
    diamonds = _dedupe_rects(diamonds)
    badges = _dedupe_rects(badges)

    # Numbered steps ("4 & 5  NPC connections") print their numbers on a
    # rounded plaque, the "&" between them a few points smaller and a
    # baseline lower — three separate lines as far as clustering is
    # concerned. Fuse whatever sits on a plaque into one span so the number
    # reads as a unit and stays with the heading beside it.
    for plaque in badges:
        on_it = [
            sp
            for sp in spans
            if plaque.x0 - 1 <= (sp["x"] + sp["x1"]) / 2 <= plaque.x1 + 1
            and plaque.y0 - 4 <= sp["y"] <= plaque.y1 + 1
        ]
        if not on_it:
            continue
        on_it.sort(key=lambda sp: sp["x"])
        # The number is set in the heading face. Anything else on the plaque
        # is ornament (Book II's appendix sets a big Caslon numeral behind
        # each one) and goes with it rather than into the heading.
        numerals = [sp for sp in on_it if "Avara" in sp["font"]] or on_it
        biggest = max(numerals, key=lambda sp: sp["size"])
        label = " ".join(
            sp["text"].strip() for sp in numerals if sp["text"].strip()
        )
        keep = {id(sp) for sp in on_it}
        spans = [sp for sp in spans if id(sp) not in keep]
        spans.append(
            {
                "x": numerals[0]["x"],
                "y": biggest["y"],
                "x1": numerals[-1]["x1"],
                "text": label,
                "font": biggest["font"],
                "size": biggest["size"],
                "badge": label,
            }
        )
    if badges:
        spans.sort(key=lambda sp: (sp["y"], sp["x"]))
    boxes_sq = _dedupe_rects(boxes_sq)
    hrules = _dedupe_rects(hrules)
    if playbook and badges:
        # Each step of a walkthrough is ruled off, and its plaque is stamped
        # over that rule. The step already reads as its own item, and the
        # rule lands between the step and its second line.
        hrules = [
            r
            for r in hrules
            if not any(abs(r.y0 - b.y0) <= 3.5 for b in badges)
        ]

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

    # The sheet's pre-printed furniture: the stat block and the name box are
    # laid out as art with type dropped into it, so they are read whole and
    # taken out of the flow rather than left to wrap as paragraphs.
    stats_json = ""
    write_label = ""
    if playbook:
        found = playbook_stat_block(spans, page_h)
        if found is not None:
            block, used = found
            stats_json = json.dumps(block, ensure_ascii=False)
            spans = [sp for sp in spans if id(sp) not in used]
        found_box = playbook_write_box(page, spans)
        if found_box is not None:
            write_label, used = found_box
            spans = [sp for sp in spans if id(sp) not in used]

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
        # A section head and its gloss can share a baseline (the Ages of the
        # World timeline: "Time of Cataclysm" + "Centuries ago, over a span of
        # decades."). Peel the heading type off so it still reads as a heading
        # instead of being outvoted by the smaller type beside it.
        split: list[list[dict]] = []
        for group in lines:
            group.sort(key=lambda s: s["x"])
            k = 0
            while k < len(group) and group[k]["font"].startswith("Avara"):
                k += 1
            rest = "".join(g["text"] for g in group[k:]).strip()
            if (
                0 < k < len(group)
                and group[0]["size"] >= (9.5 if playbook else 11)
                and not any(g["font"].startswith("Avara") for g in group[k:])
                # A parenthetical qualifier belongs to the heading itself
                # ("7 GM moves (optional)"); a gloss is its own line.
                and not rest.startswith("(")
                and len(rest) >= 12
            ):
                split.append(group[:k])
                split.append(group[k:])
            else:
                split.append(group)
        lines = split

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
            ding_x = None
            for g in group:
                f = g["font"]
                if "Dingbat" in f or "Wingdings" in f:
                    ding += len(g["text"].strip())
                    if not text_spans and g["text"].strip():
                        bullet = "stat" if "ITC" in f else "check"
                        if ding_x is None:
                            ding_x = g["x"]
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
            # A playbook's starting moves come pre-marked: a ZapfDingbats
            # tick drawn inside the checkbox. Read as a bullet it turns the
            # move into a bulleted aside, so look for the box around it.
            checked = False
            if playbook and bullet == "stat" and ding_x is not None:
                for r in r_checks:
                    if (
                        abs((r.y0 + r.y1) / 2 - y_c) <= 5.5
                        and r.x0 - 2.5 <= ding_x <= r.x1 + 2.5
                    ):
                        bullet = "check"
                        checked = True
                        break
            if bullet is None:
                for r in r_checks:
                    if (
                        abs((r.y0 + r.y1) / 2 - y_c) <= 5.5
                        and r.x0 < first_x
                        and first_x - r.x0 <= 25
                    ):
                        bullet = "check"
                        break
            # The item's own box is the leftmost one on its line — a move
            # that can be taken more than once prints two or three, and only
            # the first says where the item hangs.
            check_x = None
            if bullet == "check":
                row_checks = [
                    r
                    for r in r_checks
                    if abs((r.y0 + r.y1) / 2 - y_c) <= 5.5
                    and r.x0 <= first_x + 2
                    and first_x - r.x0 <= 30
                ]
                if row_checks:
                    check_x = min(r.x0 for r in row_checks)
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
            # Keep inventory diamonds from gluing to neighboring words:
            # "case◇ with" → "case ◇ with"
            text = re.sub(r"([A-Za-z0-9])◇", r"\1 ◇", text)
            text = re.sub(r"◇([A-Za-z0-9])", r"◇ \1", text)
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
            # Book I's gear lists (common/special items) set their table
            # headers in 9pt Fell — the same size the books use for inline
            # small-caps cross-refs — so size alone can't tell them apart.
            # A header opens in Fell and closes with a Fell "value" sitting
            # over the value column; a stray Caslon footnote marker in
            # between is fine ("bronze weapons* value").
            fell_spans = [g for g in text_spans if "FellType" in g["font"]]
            fell_head = (
                len(fell_spans) >= 2
                and fell_spans[0] is text_spans[0]
                and fell_spans[-1]["text"].strip().lower() == "value"
            )
            recs.append(
                {
                    "y": y_top,
                    "x": first_x,
                    "text": text,
                    "font": dom_font,
                    "size": dom_size,
                    "bullet": bullet,
                    "check_x": check_x,
                    "checked": checked,
                    "has_value": has_value,
                    "val": val_text,
                    "val_x": last["x"] if has_value else 0.0,
                    "lead_font": text_spans[0]["font"],
                    "lead_size": text_spans[0]["size"],
                    "bold_lead": "Bold" in text_spans[0]["font"],
                    "all_bold": all("Bold" in g["font"] for g in text_spans),
                    "bold_prefix": _lead_bold_prefix(text_spans, text),
                    "badge": next(
                        (g["badge"] for g in text_spans if g.get("badge")), None
                    ),
                    "fell_head": fell_head,
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
        head_size: float | None = None  # type size of the last heading emitted
        head_x: float | None = None  # its left edge (continuations hang left)
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
        # Left edges: the region's margin, and the line before this one — a
        # list item's further paragraphs sit at its body indent, not the margin.
        base_x = min(
            (r["x"] for r in recs if r.get("x") is not None), default=0.0
        )
        prev_x: float | None = None
        cur_x: float | None = None
        list_kind: str | None = None  # marker of the list item in progress
        list_bold_lead = False  # …and whether it opened on a bold lead-in
        while idx < n_recs:
            rec = recs[idx]
            if rec.get("hr"):
                y_hr = float(rec["y"])
                # Cross-column hairlines share a y; skip the second copy.
                if any(abs(y_hr - yy) < 5.0 for yy in emitted_hr_ys):
                    idx += 1
                    continue
                emitted_hr_ys.append(y_hr)
                # A hairline drawn directly under a value-table header
                # underlines it (Book I's gear lists rule every header);
                # it opens the table rather than ending it.
                if table is not None and not table["rows"]:
                    prev_y = rec["y"]
                    idx += 1
                    continue
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
            prev_x, cur_x = cur_x, rec.get("x")
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
            # A numbered plaque only ever sits beside a step heading, so it
            # settles the question even when the heading is short enough for
            # a trailing gloss to outvote it ("6 Stakes (optional)" is mostly
            # Caslon by character count).
            is_avara = font.startswith("Avara") or bool(rec.get("badge"))
            # A sheet sets its section heads ("Stats", "Moves", "Special
            # possessions") two points below the chapter threshold, and the
            # qualifier beside them is longer than the head itself, so the
            # dominant face is Caslon. The face the line opens in tells it.
            sheet_head = (
                playbook
                and str(rec.get("lead_font", "")).startswith("Avara")
                and float(rec.get("lead_size") or 0) >= 9.5
                and not rec.get("badge")
            )
            is_avara = is_avara or sheet_head
            # A plaque beside body type is a step of the introductions
            # walkthrough, not a section heading.
            if playbook and rec.get("badge") and not font.startswith("Avara"):
                num = rec["badge"]
                body = text
                cut = body.find(num)
                if cut >= 0:
                    body = body[:cut] + body[cut + len(num):]
                out.append(M_STEP + num + "\x03" + body.strip())
                entry_active = False
                list_kind = None
                continue
            # Fell Type at ~12pt marks table headers; at 9pt it's just
            # small-caps styling inside prose ("terrain", "encounter")
            is_fell = "FellType" in font and (
                rec["size"] >= 10.5 or rec.get("fell_head")
            )

            # Headings end any open table
            if is_avara or is_fell:
                close_table()
                state["last_line"] = ""
                entry_active = False

            if is_avara:
                if rec["size"] >= style["heading_max_size"]:
                    continue  # chapter title — page shell already shows it
                if dtext.isdigit():
                    continue
                # Big headings often wrap ("The" / "conversation"); a heading
                # line opening lowercase continues the one above it. So does a
                # capitalized one that hangs to the left of the stat name above
                # it, same size, on the very next baseline — stat names are set
                # with a hanging indent and run to three lines ("Hec'tumel, Pale
                # Serpent!" / "Slitherer In Darkness!" / "Death Is Its Eyes!").
                # The chapter TOC that opens a Book I chapter is centered 12pt
                # type on 18pt leading, so it fails both the size and the gap.
                if (
                    style["merge_wrapped_headings"]
                    and out
                    and out[-1].startswith((M_H2, M_H3))
                    and (
                        dtext[:1].islower()
                        or (
                            head_size is not None
                            and head_x is not None
                            and rec["size"] < 11
                            and abs(rec["size"] - head_size) <= 0.6
                            and gap <= 1.35 * rec["size"]
                            and rec["x"] < head_x - 2
                        )
                    )
                ):
                    out[-1] = out[-1].rstrip() + " " + dtext
                    continue
                head_size = rec["size"]
                head_x = rec["x"]
                head_text = dtext
                plaque = rec.get("badge")
                if plaque:
                    # The number sits on a lower baseline than the title, so
                    # it can cluster either side of it ("Stakes 6 (optional)").
                    rest = re.sub(
                        r"\s{2,}", " ", dtext.replace(plaque, "", 1).strip()
                    )
                    if rest:  # "4 & 5" \x03 "NPC connections"
                        head_text = plaque + "\x03" + rest
                # A plaque marks a numbered step, always a section heading —
                # the dominant type size can say otherwise when a gloss
                # outweighs the title ("6 Stakes (optional)").
                if rec["size"] >= 11 or plaque or sheet_head:
                    out.append(M_H2 + head_text)
                else:
                    out.append(M_H3 + head_text)
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
                    table["in_note"] = False
                    state["table"] = table
                    continue
                if dtext.startswith("*"):
                    table["notes"].append(dtext)
                    table["in_note"] = True
                    continue
                indent = rec["x"] - col_x0
                wrapish = indent >= 7 or dtext[:1].islower() or dtext[:1] in "(◇"
                # A dingbat opening an indented continuation is an inventory
                # diamond, not a checkbox — value tables carry no checklists
                # ("butcher for" / "◇ provisions (◇◇◇◇◇◇ uses)").
                bulleted = bool(rec["bullet"]) and not (
                    rec["bullet"] == "check" and indent >= 7
                )
                # A footnote wraps too ("* +1 if sold/traded on to anyone
                # other than" / "mammoth herders") — the second line belongs
                # to the note, not to the row above it.
                if table.get("in_note") and wrapish and not bulleted:
                    table["notes"][-1] += " " + dtext
                    continue
                if table["rows"] and wrapish and not bulleted:
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
                # table clearly continues right after. A table can open on one
                # (a group label like "Ivory" over its priced varieties).
                if (
                    idx < n_recs
                    and recs[idx].get("has_value")
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
                # On a sheet, a box indented past the column margin marks a
                # move that hangs under the one above it (Borrow Power and
                # Call the Spirits under Spirit Tongue).
                sub = False
                if playbook and rec.get("check_x") is not None:
                    sub = (rec["check_x"] - col_x0) >= 5.0
                if playbook and rec.get("checked"):
                    out.append((M_CX2 if sub else M_CX) + _marked(text))
                else:
                    out.append((M_C2 if sub else M_C) + _marked(text))
                list_kind = M_C
                continue
            if rec["bullet"] == "stat":
                out.append("• " + _marked(text))
                list_kind = None
                continue
            if rec["bullet"] in ("b", "q"):
                marker = M_Q if rec["bullet"] == "q" else M_B
                if marker == M_B and entry_active and not in_box:
                    marker = M_B2  # sub-bullet of the current entry
                if re.match(r"^(\.\.\.|…)", dtext):
                    marker = M_E
                    text = re.sub(r"^[\x04-\x07]*(?:\.\.\.|…)[\x04-\x07]*\s*", "", text)
                out.append(marker + _marked(text))
                list_kind = marker
                list_bold_lead = text.lstrip().startswith(B_ON)
                continue
            if re.match(r"^(\.\.\.|…)\s*\S", dtext):
                out.append(M_E + re.sub(r"^[\x04-\x07]*(?:\.\.\.|…)[\x04-\x07]*\s*", "", text))
                list_kind = M_E
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
                list_kind = M_B
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
                    or out[-1].startswith((M_B2, M_BC, "• "))
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

            # An example of play or read-aloud passage, set italic end to
            # end. Its wrapped lines are ordinary prose and can break the
            # line-joining heuristics in ways nothing recovers from — a new
            # sentence mid-paragraph reads exactly like a new paragraph
            # ("…WHOOMP WHOOMP WHOOMP!" + "Rhianna, there's a banging at your
            # door"). The page says which it is: 10.8pt of leading inside a
            # paragraph, 21.6pt between them.
            if (
                out
                and not out[-1].startswith("\x02")
                and gap <= 13.5
                and prev_x is not None
                and abs(rec["x"] - prev_x) <= 1.5
                and italic_coverage(text) >= 0.9
                and italic_coverage(out[-1]) >= 0.9
            ):
                body_i, tail_i = _split_trailing_fmt(out[-1])
                lead_i, rest_i = _split_leading_fmt(text)
                if (
                    body_i.endswith("-")
                    and not body_i.endswith(("–", "—", "--"))
                    and rest_i[:1].islower()
                ):
                    out[-1] = body_i[:-1] + _cancel_fmt_seam(tail_i, lead_i) + rest_i
                else:
                    out[-1] = out[-1] + " " + text
                continue

            # A further paragraph of the question above. Getting Started
            # answers each bolded question inline and then runs on for a
            # paragraph or two — "No, they don't add their STR or DEX to
            # their damage rolls." belongs to the question, and the book
            # indents it to the answer to say so. It reads as a stray
            # aside once the list closes around it.
            #
            # The indent is all the evidence there is (a paragraph that
            # really ends the list dedents to the column margin), and it is
            # not always visible: a column made up entirely of Q&A has no
            # margin type to measure against. So this stays narrow — a
            # bolded question spiral, which is the shape that answers at
            # this length, and never the plainer lists whose neighbours are
            # ordinary prose.
            if (
                out
                and prev_x is not None
                and gap > 13.5  # a paragraph break, not a wrapped line
                and rec["x"] >= prev_x - 1.0
                and list_kind == M_Q
                and list_bold_lead
                and out[-1].startswith((M_Q, M_BC))
            ):
                out.append(M_BC + text)
                continue

            entry_active = False  # a plain paragraph ends the current entry
            list_kind = None
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

    # A page whose body text sits entirely on one side of the gutter is a
    # single column set beside a full-height illustration, and its lines run
    # ragged — they can start left of the gutter and reach well past it (The
    # Time of Cataclysm). Splitting those at mid-page deals half of each line
    # to each column and scrambles the reading order, so move the gutter to
    # the edge of the text instead; page furniture stays on the empty side.
    if not single_column:
        # Body type only, and enough of it to tell a column from a title page
        body = [
            s
            for s in spans
            if 60 <= s["y"] <= page_h - 40 and s["size"] <= 11.5
        ]
        if len(body) >= 8:
            near = [s for s in body if (s["x"] + s["x1"]) / 2 < gutter]
            far = [s for s in body if (s["x"] + s["x1"]) / 2 >= gutter]
            # The block as a whole has to sit off to one side. Type centered
            # on the gutter is a chapter TOC, not a column beside a picture.
            mid = (
                min(s["x"] for s in body) + max(s["x1"] for s in body)
            ) / 2
            stray = max(2, 0.04 * len(body))
            if far and mid > gutter + 30 and len(near) <= stray:
                gutter = min(min(s["x"] for s in far) - 4, gutter)
            elif near and mid < gutter - 30 and len(far) <= stray:
                gutter = max(max(s["x1"] for s in near) + 4, gutter)

    # Text set to the full measure (the mediography, an appendix table) runs
    # straight through mid-page, so splitting spans there interleaves halves
    # of every line. Tell it from a real two-column page by the break: on two
    # columns a shared baseline carries a line that stops short of the gutter
    # and another that starts past it, while a full-measure line crosses it
    # unbroken. Page furniture aside, one kind or the other holds a page.
    rows: list[list[dict]] = []
    for sp in sorted(spans, key=lambda sp: (sp["y"], sp["x"])):
        if rows and abs(sp["y"] - rows[-1][0]["y"]) <= 4.5:
            rows[-1].append(sp)
        else:
            rows.append([sp])
    for row in rows:
        row.sort(key=lambda sp: sp["x"])

    if not single_column:
        two_col = False
        crossing = 0
        for row in rows:
            segs: list[list[float]] = []  # [x0, x1] per unbroken run
            last_mid: float | None = None
            for sp in row:
                mid = _span_mid_x(sp)
                # Never glue a left-col span to a right-col one, even when
                # the gap at the gutter is under 8pt (Sites example notes).
                same_side = last_mid is None or (last_mid < gutter) == (
                    mid < gutter
                )
                if segs and same_side and sp["x"] - segs[-1][1] <= 8.0:
                    segs[-1][1] = max(segs[-1][1], sp["x1"])
                else:
                    segs.append([sp["x"], sp["x1"]])
                last_mid = mid
            mids = [_span_mid_x(sp) for sp in row]
            if any(m < gutter for m in mids) and any(m >= gutter for m in mids):
                two_col = True
                break
            crossing += sum(1 for x0, x1 in segs if x0 < gutter < x1)
        if not two_col and crossing >= 3:
            gutter = page.rect.width + 1

    # A playbook page is banded by full-measure rules, and each band's two
    # columns belong to that band alone: read as two full-height columns, the
    # top band's right half lands in the middle of the bottom band's left one.
    bands: list[tuple[float, float]] = [(float("-inf"), float("inf"))]
    if playbook:
        cuts = [y for y in playbook_band_ys(page) if 20.0 < y < page_h - 20.0]
        edges = [float("-inf")] + sorted(cuts) + [float("inf")]
        bands = [(lo, hi) for lo, hi in zip(edges, edges[1:])]

    if stats_json:
        result.append(M_STATS + stats_json)

    for bi, (band_lo, band_hi) in enumerate(bands):
        band_spans = [sp for sp in spans if band_lo <= sp["y"] < band_hi]
        if not band_spans:
            continue
        if bi and result and result[-1] != M_BAND:
            result.append(M_BAND)
        cols: list[list[dict]] = [[], []]
        for sp in band_spans:
            cols[0 if _span_mid_x(sp) < gutter else 1].append(sp)
        for ci, col in enumerate(cols):
            if not col:
                continue
            col_x0 = min(sp["x"] for sp in col)
            x_lo, x_hi = (0.0, gutter) if ci == 0 else (gutter, page.rect.width)
            recs = build_lines(col, x_lo, x_hi)
            # resume a table that carried over from the previous column/page
            carried = state.get("table")
            result.extend(emit_region(recs, col_x0, False))
            # if the carried table produced no continuation rows, drop the state
            if carried is not None and state.get("table") is carried:
                state.pop("table", None)

    if write_label:
        result.append(M_WRITE + write_label)

    # Collapse any remaining consecutive HRs from within a single column
    cleaned: list[str] = []
    for line in result:
        if line == M_HR and cleaned and cleaned[-1] == M_HR:
            continue
        cleaned.append(line)
    return cleaned


# Every word the books set on one line, hyphens included, counted across the
# whole build. Used to tell a soft line-break hyphen from a compound one.
BOOK_TOKENS: dict[str, int] = {}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*[A-Za-z]")


def index_book_tokens(docs) -> None:
    BOOK_TOKENS.clear()
    for doc in docs:
        for page in doc:
            for w in WORD_RE.findall(normalize_text(page.get_text())):
                k = w.lower()
                BOOK_TOKENS[k] = BOOK_TOKENS.get(k, 0) + 1


def hyphen_is_compound(body: str, rest: str) -> bool:
    """True when the book prints this wrapped word hyphenated elsewhere.

    A line-end hyphen is ambiguous: "crys-" + "tal" is a soft wrap to undo,
    while "rock-" + "cut" is a compound to keep. Let the book settle it — the
    hyphenated form attested in running text, the joined form never.
    """
    if not BOOK_TOKENS:
        return False
    a = re.search(r"([A-Za-z][A-Za-z'\-]*)-$", strip_markers(_defmt(body)))
    b = re.match(r"([A-Za-z][A-Za-z'\-]*)", strip_markers(_defmt(rest)))
    if not a or not b:
        return False
    x, y = a.group(1).lower(), b.group(1).lower()
    return bool(BOOK_TOKENS.get(x + "-" + y)) and not BOOK_TOKENS.get(x + y)


def merge_wrapped_lines(
    lines: list[str], pages: list[int] | None = None
) -> list[str] | tuple[list[str], list[int]]:
    """Join soft-wrapped lines back into one.

    With ``pages`` (one PDF page number per line) the merged lines' pages are
    returned beside them — a merged line sits on the page its first piece
    came from, so the page map survives a paragraph that turns a page.
    """
    if not lines:
        return ([], []) if pages is not None else []
    out: list[str] = []
    out_pages: list[int] = []
    buf = lines[0]
    buf_page = pages[0] if pages is not None else 0
    for i, nxt in enumerate(lines[1:], 1):
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
                    if hyphen_is_compound(body, rest):
                        # Compound the book hyphenates in running text too
                        buf = body + _cancel_fmt_seam(tail, lead) + rest
                    else:
                        # Soft wrap; cancel the seam so it reads as one run.
                        buf = body[:-1] + _cancel_fmt_seam(tail, lead) + rest
                else:
                    # Capital after the hyphen → real compound; keep it, glue.
                    buf = buf + nxt
            else:
                buf = buf + " " + nxt
        else:
            out.append(buf)
            out_pages.append(buf_page)
            buf = nxt
            buf_page = pages[i] if pages is not None else 0
    out.append(buf)
    out_pages.append(buf_page)
    if pages is not None:
        return out, out_pages
    return out


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
    lines: list[str], article_title: str, *, allow_markers: bool = False
) -> tuple[list[str], list[str]]:
    """
    Peel a chapter-opener table of contents off the start of extracted lines.

    Book I chapter first pages list section titles in a multi-column TOC.
    Those short lines currently become spurious <h2>s; strip them here so
    they can be rendered as sidebar deep links instead.

    Book I sets those TOC labels in the same Avara-Bold as real headings, so
    they arrive as ``M_H2``/``M_H3`` marker lines. ``allow_markers`` lets the
    leading run contain heading markers (any other marker — bullet, table,
    rule — still means this is not a TOC page).

    Returns (toc_labels, body_lines). If no TOC is detected, toc is empty
    and body_lines is the original list.
    """
    if not lines or len(lines) < 5:
        return [], lines

    def plain(line: str) -> str:
        return strip_markers(_defmt(line)).strip() if allow_markers else line.strip()

    prose_idx: int | None = None
    for i, line in enumerate(lines):
        # Rich structural markers mean this is not a chapter-opener TOC page
        if line.startswith("\x02"):
            if allow_markers and line.startswith((M_H2, M_H3)):
                continue
            return [], lines
        if _is_chapter_toc_prose(plain(line)):
            prose_idx = i
            break
    # Need a cluster of short TOC lines before prose
    if prose_idx is None or prose_idx < 3:
        return [], lines

    at = normalize_text(article_title).lower() if article_title else ""
    toc: list[str] = []
    for line in lines[:prose_idx]:
        L = plain(line)
        if not L:
            continue
        if at and normalize_text(L).lower() == at:
            continue
        if is_running_header(L, article_title, near_page_top=True):
            continue
        # Heading-marker lines are real Avara-Bold labels, never column glue.
        is_head = line.startswith((M_H2, M_H3))
        if not is_head and _is_chapter_toc_garbage(L):
            continue
        # Short label / ALL CAPS move name
        if is_head or looks_like_heading(L) or (
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
    while (
        body
        and not body[0].startswith("\x02")
        and _is_chapter_toc_garbage(plain(body[0]))
    ):
        body = body[1:]
    if not body:
        return [], lines
    return toc, body


def is_playbook_range(doc: fitz.Document, start_page: int, end_page: int) -> bool:
    """True for the nine playbook character sheets.

    What marks a sheet is its stat block: one page carrying all six scores,
    labelled the way only the sheet labels them. The chapters talk about
    stats and special possessions too, so the words alone are not enough —
    "Playing the Game" heads a section with each of them.
    """
    want = {"(STR)", "(DEX)", "(INT)", "(WIS)", "(CON)", "(CHA)"}
    for pno in range(start_page - 1, min(end_page, doc.page_count)):
        if pno < 0:
            continue
        try:
            blocks = doc[pno].get_text("dict").get("blocks", [])
        except Exception:
            continue
        seen: set[str] = set()
        for b in blocks:
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    t = (sp.get("text") or "").strip()
                    if t in want:
                        seen.add(t)
        if seen >= want:
            return True
    return False


def _sheet_move_item(line: str) -> bool:
    """True for a move on a sheet — a checkbox item named in full caps."""
    if not line.startswith((M_C, M_C2, M_CX, M_CX2)):
        return False
    body = strip_markers(line)
    lead = re.match(r"^[A-Z][A-Z0-9'’&\-\. ]{2,}", _defmt(body).strip())
    return bool(lead and _is_all_caps_label(lead.group(0).strip()))


def merge_playbook_steps(
    lines: list[str], pages: list[int]
) -> tuple[list[str], list[int]]:
    """Fold a step's wrapped lines back into the step.

    A numbered step is body copy with a plaque beside its first line, so the
    lines under it come through as loose paragraphs. The step ends where the
    next one starts, or at the list of questions it introduces.
    """
    out: list[str] = []
    out_pages: list[int] = []
    for i, line in enumerate(lines):
        if (
            out
            and out[-1].startswith(M_STEP)
            and line
            and not MARKER_RE.match(line)
        ):
            out[-1] = out[-1].rstrip() + " " + line.lstrip()
            continue
        out.append(line)
        out_pages.append(pages[i])
    return out, out_pages


def reorder_playbook_lines(
    lines: list[str], pages: list[int]
) -> tuple[list[str], list[int]]:
    """Put Special Possessions ahead of the moves, and the moves in one run.

    The sheet prints the possessions across the top of its third page, which
    drops them into the middle of a move list that runs from the foot of page
    two to the foot of page three. On paper the bands keep them apart; in a
    single column they have to be moved. ``pages`` travels with the lines.
    """
    def head_at(i: int) -> str:
        ln = lines[i]
        for m in (M_H2, M_H3):
            if ln.startswith(m):
                return _defmt(ln[len(m):]).strip()
        return ""

    moves_i = next(
        (i for i in range(len(lines)) if head_at(i).lower() == "moves"), -1
    )
    poss_i = next(
        (
            i
            for i in range(len(lines))
            if head_at(i).lower().startswith("special possession")
        ),
        -1,
    )
    if moves_i < 0 or poss_i < 0 or poss_i < moves_i:
        return lines, pages
    # The block runs to the first move that follows it.
    end = next(
        (i for i in range(poss_i + 1, len(lines)) if _sheet_move_item(lines[i])),
        -1,
    )
    if end < 0:
        return lines, pages
    block = [i for i in range(poss_i, end) if lines[i] != M_BAND]
    while block and lines[block[-1]] == M_HR:
        block.pop()
    rest = list(range(poss_i)) + list(range(end, len(lines)))
    order = rest[:moves_i] + block + rest[moves_i:]
    return [lines[i] for i in order], [pages[i] for i in order]


def extract_article_lines(
    doc: fitz.Document,
    start_page: int,
    end_page: int,
    article_title: str,
    *,
    icon_dir: Path | None = None,
    book_style: str = "book2",
    playbook: bool | None = None,
    pages_out: list[int] | None = None,
) -> list[str]:
    """Rich (span/drawing-aware) line extraction for an article page range.

    ``pages_out``, when given, receives the PDF page number (1-based, the
    number printed on the page) of every returned line, in the same order —
    the corpus writes them out as ``PAGE`` lines, and ``heading_pages`` reads
    the section-to-page map off them.
    """
    raw: list[str] = []
    raw_pages: list[int] = []
    if playbook is None:
        playbook = book_style == "book1" and is_playbook_range(
            doc, start_page, end_page
        )
    state: dict = {"book_style": book_style, "playbook": playbook}
    if icon_dir is not None:
        state["icon_dir"] = Path(icon_dir)
    first = True
    for pno in range(start_page - 1, end_page):
        if pno < 0 or pno >= doc.page_count:
            continue
        got = extract_page_rich(
            doc[pno],
            article_title=article_title,
            first_page=first,
            state=state,
        )
        raw.extend(got)
        raw_pages.extend([pno + 1] * len(got))
        first = False
    lines, pages = merge_wrapped_lines(raw, raw_pages)
    if playbook:
        lines, pages = reorder_playbook_lines(lines, pages)
        lines, pages = merge_playbook_steps(lines, pages)
        keep = [i for i, ln in enumerate(lines) if ln != M_BAND]
        lines = [lines[i] for i in keep]
        pages = [pages[i] for i in keep]
    if pages_out is not None:
        pages_out.extend(pages)
    return lines


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
    # Lines that are never part of a power/discovery title (tracks, age marks,
    # move triggers, form labels). Keep Shell Game of Souls intact — "Souls"
    # only junks a line that *starts* with it as a form label.
    junk_title = re.compile(
        r"^(starts at|souls\b|lost memories|daylight|vitality|hold\b|max\b|"
        r"uses\b|pouch of|when |if you|you can|your |mark |spend |"
        r"roll |on a |the spell|alas,|"
        r"preparation\b|youthful\b|mature\b|elderly\b|"
        r"heat\b|strain\b|disturbance\b|breath\b|readiness\b|"
        r"charges?\b|ken\b|sway\b|authority\b|loyalty\b|charge\b|ire\b)",
        re.I,
    )
    # "youthful, mature, elderly" — comma list of short lowercase track options
    track_opts = re.compile(
        r"^[a-z][a-z0-9\-]*(?:\s*,\s*[a-z][a-z0-9\-]*)+$"
    )

    def is_non_title_line(line: str) -> bool:
        L = (line or "").strip()
        if not L:
            return True
        if junk_title.match(L) or tag_re.match(L):
            return True
        if track_opts.match(L):
            return True
        return False

    def title_from(side: list[str]) -> str | None:
        parts: list[str] = []
        for i, line in enumerate(side[:5]):
            if not line or is_non_title_line(line):
                if parts:
                    break
                continue
            if line.lower() in {"front", "back"} or re.fullmatch(r"\d+", line):
                continue
            parts.append(line.rstrip(",").strip())
            joined = " ".join(parts)
            nxt = side[i + 1] if i + 1 < len(side) else ""
            # Continue only when the name clearly wraps (not onto a track line).
            incomplete = (
                line.endswith((",", "-", "—", "'s", "\u2019s"))
                or bool(re.search(r"\b(of|the|a|an|and|or|to|for)\s*$", line, re.I))
                or (
                    # One-word start of a multi-word title ("Thunderous" / "Bellow")
                    len(line.split()) == 1
                    and len(joined) < 28
                    and nxt
                    and nxt[0:1].isupper()
                    and not is_non_title_line(nxt)
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
        # Strip form labels glued after the name ("… Souls (max CON):", "… Heat:").
        # Require ":" or "(…)" so titles that end in the word itself
        # (Shell Game of Souls) are kept intact.
        title = re.sub(
            r"\s+(Heat|Strain|Disturbance|Breath|Preparation|Charges?|Ken|"
            r"Sway|Authority|Loyalty|Charge|Ire|Readiness|Vitality|Daylight|"
            r"Souls)\s*(?:[:：].*|\([^)]*\).*)$",
            "",
            title,
            flags=re.I,
        )
        # Bare form-label word stuck on the same line ("… Iron Body Preparation").
        # Only labels that never appear as a real title word — not Authority
        # (Sigil of Authority), Souls (Shell Game of Souls), etc.
        title = re.sub(
            r"\s+(Heat|Strain|Disturbance|Breath|Preparation|Charges?|"
            r"Readiness|Vitality|Daylight|Uses|Hold)\s*$",
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
        if is_non_title_line(title) or len(title) < 3:
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


def extract_major_arcana_rich(
    doc: fitz.Document,
    start_page: int,
    end_page: int,
    article_title: str,
    pages_out: list[int] | None = None,
) -> list[str]:
    """Rich (marker + formatting) extraction of a two-page major-arcana card.

    Each page is a single full-width column; front page first, then back.
    """
    out: list[str] = []
    out_pages: list[int] = []
    for pno in range(start_page - 1, end_page):
        if pno < 0 or pno >= doc.page_count:
            continue
        got = [M_FACE + ("front" if pno == start_page - 1 else "back")]
        got.extend(
            extract_page_rich(
                doc[pno], article_title=article_title, single_column=True
            )
        )
        out.extend(got)
        out_pages.extend([pno + 1] * len(got))
    lines, pages = merge_wrapped_lines(out, out_pages)
    if pages_out is not None:
        pages_out.extend(pages)
    return lines


def extract_minor_arcana_rich(
    doc: fitz.Document,
    page_1based: int,
    half: str,
    pages_out: list[int] | None = None,
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
    if pages_out is not None:
        pages_out.extend([page_1based] * len(cleaned))
    return cleaned


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

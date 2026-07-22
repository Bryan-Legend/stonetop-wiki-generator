#!/usr/bin/env python3
"""
Build a static wiki from Stonetop Book II (1-up PDF).

Usage:
  python build_book_ii_wiki.py --input <folder> --output <folder>

``--input`` is a folder containing the Book II 1-up PDF (and optionally
``Maps/`` campaign sheets). ``--output`` is where the static site is written
(index.html, pages/, css/, js/, images/).
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

from wiki_content import (
    article_html_from_pdf,
    build_section_index,
    extract_article_lines,
    extract_minor_arcana_card,
    list_minor_arcana_cards,
    major_arcana_html_from_pdf,
    match_toc_to_sections,
    minor_arcana_html_from_pdf,
    set_title_index,
    split_chapter_toc,
)

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

    for sub in ("pages", "css", "js", "images"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    for old in (out / "pages").glob("*.html"):
        try:
            old.unlink()
        except OSError:
            pass

    copy_static_assets(out)

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
                doc, art["start_page"], art["end_page"], art["title"]
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
                doc, art["start_page"], art["end_page"], art["title"], rich=True
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
    (out / "previews.json").write_text(previews_json, encoding="utf-8")

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
    (out / "page-map.json").write_text(page_map_json, encoding="utf-8")
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

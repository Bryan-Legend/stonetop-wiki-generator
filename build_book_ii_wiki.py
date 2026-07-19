#!/usr/bin/env python3
"""
Build a static wiki from Stonetop Book II (PDF + markdown).

Outputs to Stonetop_Wiki/:
  index.html, pages/*.html, css/wiki.css, js/wiki.js, images/maps/, previews.json
"""

from __future__ import annotations

import html
import json
import re
import shutil
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import fitz  # PyMuPDF
from markdown_it import MarkdownIt

from wiki_content import (
    article_html_from_pdf,
    build_section_index,
    extract_article_lines,
    extract_minor_arcana_card,
    list_minor_arcana_cards,
    major_arcana_html_from_pdf,
    match_toc_to_sections,
    minor_arcana_html_from_pdf,
    split_chapter_toc,
)

ROOT = Path(__file__).resolve().parent
BOOK_II = ROOT / "Book_II"
PDF_BOOK_II = ROOT / "Book_II_-_The_Wider_World_and_Other_Wonders_(1-up)_-_2nd_printing.pdf"
OUT = ROOT / "Stonetop_Wiki"
LEGACY_OUT = ROOT / "Book_II_Wiki"

# Back-compat aliases used elsewhere in this file
PDF_PATH = PDF_BOOK_II


def find_campaign_map_jpgs() -> list[Path]:
    """
    Optional high-res campaign map sheets.

    Looks under Maps/ or maps/ for files named like ``Map *.jpg``
    (top-level or nested). Missing maps is fine — PDF map spreads are
    always rendered from the book.
    """
    bases = [ROOT / "Maps", ROOT / "maps"]
    found: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for pattern in ("Map *.jpg", "Map *.jpeg", "Map *.png", "**/Map *.jpg", "**/Map *.jpeg", "**/Map *.png"):
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


def title_from_filename(filename: str) -> str:
    base = re.sub(r"^\d+-", "", filename)
    if base.endswith(".md"):
        base = base[:-3]
    base = base.replace("-", " ")
    # Fix common title casing later; keep readable words
    return base


def nice_title(raw: str) -> str:
    """Title-case-ish from filename words, preserving known particles."""
    small = {"of", "the", "and", "or", "a", "an", "to", "in", "for"}
    words = raw.replace("_", " ").split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if i > 0 and lw in small:
            out.append(lw)
        else:
            out.append(w[:1].upper() + w[1:] if w else w)
    return " ".join(out)


def load_toc(doc: fitz.Document) -> list[tuple[int, str, int]]:
    """Return level-1 TOC entries: (level, title, page_1based)."""
    items = []
    for level, title, page in doc.get_toc():
        if page < 1:
            continue
        items.append((level, title.strip(), page))
    return items


def match_files_to_toc(
    toc: list[tuple[int, str, int]],
    md_files: list[str],
    *,
    book: str = "book2",
    slug_prefix: str = "",
    book_label: str = "Book II",
) -> list[dict]:
    """
    Build ordered article list from level-1 TOC (skip Contents, Index).
    Each article: title, slug, start_page, end_page, md_path|None, kind, book
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
                "md_file": None,
                "kind": kind,
                "book": book,
                "book_label": book_label,
            }
        )

    # Match markdown files to articles — exact slug first, then fuzzy
    used_files: set[str] = set()

    def file_slugs(f: str) -> list[str]:
        fslug = slugify(re.sub(r"^\d+-", "", f[:-3]))
        return [
            fslug,
            re.sub(r"^appendix-[a-d]-", "appendix-", fslug),
            re.sub(r"^appendix-[a-d]-", "", fslug),
        ]

    # Pass A: exact slug matches
    for art in articles:
        if art["kind"] == "maps":
            continue
        tslug = slugify(art["title"])
        tslug_alt = re.sub(r"^appendix-[a-d]-", "appendix-", tslug)
        tslug_alt2 = re.sub(r"^appendix-[a-d]-", "", tslug)
        targets = {tslug, tslug_alt, tslug_alt2}
        for f in md_files:
            if f in used_files:
                continue
            if targets & set(file_slugs(f)):
                art["md_file"] = f
                used_files.add(f)
                break

    # Pass B: fuzzy for remaining (high threshold to avoid Expectations→Expeditions)
    for art in articles:
        if art["kind"] == "maps" or art.get("md_file"):
            continue
        best_f, best_score = None, 0.0
        tslug = slugify(art["title"])
        tslug_alt = re.sub(r"^appendix-[a-d]-", "appendix-", tslug)
        tslug_alt2 = re.sub(r"^appendix-[a-d]-", "", tslug)
        for f in md_files:
            if f in used_files:
                continue
            scores = [
                SequenceMatcher(None, tslug, fs).ratio() for fs in file_slugs(f)
            ] + [
                SequenceMatcher(None, tslug_alt, fs).ratio() for fs in file_slugs(f)
            ] + [
                SequenceMatcher(None, tslug_alt2, fs).ratio() for fs in file_slugs(f)
            ]
            score = max(scores)
            if score > best_score:
                best_score = score
                best_f = f
        if best_f and best_score >= 0.72:
            art["md_file"] = best_f
            used_files.add(best_f)
        elif best_score > 0 and best_score < 0.72:
            print(
                f"  note: weak MD match for '{art['title']}' [{book}] "
                f"(best={best_score:.2f}, skipped)"
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
                        "md_file": None,
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
                        "md_file": None,
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


def md_to_html_body(md_text: str) -> str:
    """Convert markdown to HTML (basic)."""
    md = MarkdownIt("commonmark", {"breaks": True, "html": False})
    # Enable tables if present
    try:
        md.enable("table")
    except Exception:
        pass
    return md.render(md_text)


# Patterns for dice expressions (not years etc.)
# d6, 2d10, d10+3, 1d4-1 — optional spaces around +/-; avoid 1d4-1d6 ranges
DICE_RE = re.compile(
    r"(?<![A-Za-z0-9_])"  # not part of identifier
    r"(\d{0,2}d(?:4|6|8|10|12|20|100)"
    r"(?:\s*[+\-–—]\s*\d{1,3}(?![dD\d]))?)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# Page reference patterns in source markdown
PAGE_PAREN_RE = re.compile(
    r"\((?:see\s+)?pages?\s+(\d{1,3})(?:\s*[-–—]\s*(\d{1,3}))?\)",
    re.IGNORECASE,
)
PAGE_BARE_RE = re.compile(
    r"(?<![\w/])(?:see\s+)?pages?\s+(\d{1,3})(?:\s*[-–—]\s*(\d{1,3}))?(?![\w/])",
    re.IGNORECASE,
)
# Bold title, optional short words, then page paren — e.g. **X** entry (page 12)
# Group1=title, Group2=intervening words (kept), Group3=start page, Group4=end page
BOLD_PAGE_RE = re.compile(
    r"\*\*([^*]+?)\*\*((?:\s+[\w''\-]+){0,5})\s*\((?:see\s+)?pages?\s+(\d{1,3})(?:\s*[-–—]\s*(\d{1,3}))?\)",
    re.IGNORECASE,
)
# Title without bold
TITLE_PAGE_RE = re.compile(
    r"(?<!\*)\b([A-Z][A-Za-z'’\-]*(?:\s+(?:of|the|and|or|a|an|to|in|for|from|[A-Z][A-Za-z'’\-]*)){0,6})\s+"
    r"\((?:see\s+)?pages?\s+(\d{1,3})(?:\s*[-–—]\s*(\d{1,3}))?\)",
)


def link_for_page(
    page: int, lookup: dict[int, dict], link_text: str | None = None
) -> str:
    art = lookup.get(page)
    if not art:
        return html.escape(link_text or f"page {page}")
    text = link_text if link_text else art["title"]
    href = f"{art['slug']}.html"
    return (
        f'<a class="wiki-link" href="{href}" data-slug="{art["slug"]}" '
        f'title="{html.escape(art["title"])}">{html.escape(text)}</a>'
    )


def process_inline_links(
    text: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None = None,
) -> str:
    """
    Replace page references with hyperlinks before markdown conversion.
    Uses temporary placeholders so markdown doesn't mangle HTML.
    """
    placeholders: list[str] = []

    def store(html_snippet: str) -> str:
        idx = len(placeholders)
        placeholders.append(html_snippet)
        # ASCII token unlikely in source; survives markdown_it
        return f"WIKIPLACEHOLDER{idx}ENDPH"

    # 1) **Title** [optional words] (page N)
    def repl_bold_page(m: re.Match) -> str:
        title = m.group(1).strip()
        middle = m.group(2) or ""
        page = int(m.group(3))
        art = lookup.get(page)
        if art and art["slug"] == current_slug:
            return f"**{title}**{middle}"
        return store(link_for_page(page, lookup, title)) + middle

    text = BOLD_PAGE_RE.sub(repl_bold_page, text)

    # 2) remaining (page N) / (pages N-M)
    def repl_paren(m: re.Match) -> str:
        page = int(m.group(1))
        art = lookup.get(page)
        if art and art["slug"] == current_slug:
            return ""  # drop self page-ref parenthetical
        return store(link_for_page(page, lookup))

    text = PAGE_PAREN_RE.sub(repl_paren, text)

    # 3) bare "page N" / "pages N-M" / "see page N"
    def repl_bare(m: re.Match) -> str:
        page = int(m.group(1))
        art = lookup.get(page)
        if art and art["slug"] == current_slug:
            return ""
        return store(link_for_page(page, lookup))

    text = PAGE_BARE_RE.sub(repl_bare, text)

    # 4) Auto-link remaining **Exact Title** that match OTHER article titles
    title_map = {}
    for art in articles:
        if art["slug"] == current_slug:
            continue
        title_map[art["title"].lower()] = art
        t = art["title"]
        if t.lower().startswith("the "):
            title_map[t[4:].lower()] = art
        title_map[nice_title(art["slug"].replace("-", " ")).lower()] = art
        # common short forms
        if "," in t:
            title_map[t.split(",")[0].strip().lower()] = art

    def repl_bold_title(m: re.Match) -> str:
        inner = m.group(1).strip()
        if "WIKIPLACEHOLDER" in inner:
            return m.group(0)
        key = inner.lower()
        art = title_map.get(key)
        if not art:
            key2 = re.sub(r"\s+", " ", key).strip(" .,;:")
            art = title_map.get(key2)
        if art:
            return store(
                f'<a class="wiki-link" href="{art["slug"]}.html" data-slug="{art["slug"]}" '
                f'title="{html.escape(art["title"])}"><strong>{html.escape(inner)}</strong></a>'
            )
        return m.group(0)

    text = re.sub(r"\*\*([^*]+?)\*\*", repl_bold_title, text)

    # Convert markdown with placeholders intact
    body = md_to_html_body(text)

    # Restore placeholders (markdown may wrap them in <p> tags mid-token rarely)
    def restore(m: re.Match) -> str:
        return placeholders[int(m.group(1))]

    body = re.sub(r"WIKIPLACEHOLDER(\d+)ENDPH", restore, body)

    # Dice rolls in HTML
    def repl_dice(m: re.Match) -> str:
        expr = m.group(1)
        data = (
            expr.lower()
            .replace("–", "-")
            .replace("—", "-")
        )
        data = re.sub(r"\s*([+\-])\s*", r"\1", data)
        return (
            f'<button type="button" class="dice-roll" data-dice="{html.escape(data)}" '
            f'title="Click to roll {html.escape(expr)}">{html.escape(expr)}</button>'
        )

    body = DICE_RE.sub(repl_dice, body)
    return body


def extract_excerpt(md_text: str, max_len: int = 320) -> str:
    """First meaningful paragraph for hover previews."""
    skip_phrases = {
        "choose or roll",
        "themes",
        "hooks",
        "lore",
        "questions",
        "impressions",
        "always",
        "activities",
        "spring",
        "summer",
        "autumn",
        "winter",
        "front",
        "back",
    }
    candidates: list[str] = []
    for line in md_text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        if re.fullmatch(r"[-*]\s*\d+", s):
            continue
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
        s = re.sub(r"\*([^*]+)\*", r"\1", s)
        s = re.sub(r"_([^_]+)_", r"\1", s)
        s = re.sub(r"\(pages?\s+\d+(?:\s*[-–—]\s*\d+)?\)", "", s, flags=re.I)
        s = re.sub(r"^[-*]\s+", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) < 40:
            continue
        if s.lower().rstrip(".") in skip_phrases:
            continue
        if s.lower().startswith("choose or roll"):
            continue
        # Prefer sentences with setting proper nouns / prose
        candidates.append(s)
        if len(candidates) >= 6:
            break
    # Prefer longest early candidate (usually real prose, not a fragment)
    if not candidates:
        return ""
    ranked = sorted(
        enumerate(candidates),
        key=lambda iv: (-min(len(iv[1]), 200), iv[0]),
    )
    ordered = [c for _, c in ranked]
    text = ordered[0]
    for c in ordered[1:]:
        if len(text) >= max_len * 0.7:
            break
        if c not in text:
            text = text + " " + c
    if len(text) > max_len:
        text = text[: max_len - 1].rsplit(" ", 1)[0] + "…"
    return text


def page_shell(
    title: str,
    slug: str,
    body_html: str,
    articles: list[dict],
    rel_prefix: str = "../",
    section_navs: dict[str, list[dict]] | None = None,
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
        <main class="content">
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
) -> list[dict]:
    """
    Copy high-quality campaign maps and render PDF map spreads for the Maps page.
    Returns ordered list of image meta dicts for maps_body_html.
    """
    out: list[dict] = []
    maps_out = img_dir / "maps"
    maps_out.mkdir(parents=True, exist_ok=True)

    # 1) Optional HQ campaign maps from Maps/ (any nested layout)
    for src in find_campaign_map_jpgs():
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


def maps_body_html(images: list[dict]) -> str:
    """Render the Maps topic page from prepared map images."""
    parts = [
        "<p>Maps of Stonetop, the vicinity, and the World's End. "
        "If you placed campaign map sheets in a <code>Maps/</code> folder they "
        "appear first; PDF book spreads follow. "
        "Click any map to open full size.</p>"
    ]
    hq = [i for i in images if i.get("hq")]
    full = [i for i in images if i.get("fullpage")]
    other = [i for i in images if not i.get("hq") and not i.get("fullpage")]

    def block(title: str, items: list[dict], css: str = "") -> str:
        if not items:
            return ""
        figs = []
        for im in items:
            src = f"../images/{im['file']}"
            label = im.get("label") or Path(im["file"]).stem.replace("-", " ").title()
            figs.append(
                f'<figure class="wiki-figure map-figure">'
                f'<a href="{html.escape(src)}" target="_blank" rel="noopener">'
                f'<img src="{html.escape(src)}" alt="{html.escape(label)}" loading="lazy">'
                f"</a><figcaption>{html.escape(label)}</figcaption></figure>"
            )
        cls = f'gallery-grid maps {css}'.strip()
        return (
            f"<h2>{html.escape(title)}</h2>"
            f'<div class="{cls}">{"".join(figs)}</div>'
        )

    parts.append(block("Campaign maps", hq))
    parts.append(block("PDF map spreads", full))
    parts.append(block("Other", other))
    if len(parts) == 1:
        parts.append(
            "<p><em>No map images found. PDF map spreads are rendered when the "
            "Maps chapter is present in the book PDF. Optionally place campaign "
            "sheets named <code>Map *.jpg</code> under a <code>Maps/</code> "
            "folder.</em></p>"
        )
    return "\n".join(parts)


CSS = r"""/* Stonetop Wiki (Book II) */
:root {
  --bg: #12100e;
  --bg-elev: #1c1814;
  --bg-hover: #2a2218;
  --ink: #e8e0d4;
  --muted: #9a8b78;
  --rule: #4a3f32;
  --accent: #d4a574;
  --link: #e0b888;
  --link-hover: #f0d0a0;
  --quote: #cfc4b4;
  --ok: #8fbc8f;
  --danger: #c97b7b;
  --sidebar-w: 17.5rem;
  --font-serif: Georgia, "Times New Roman", serif;
  --font-sans: "Segoe UI", system-ui, -apple-system, sans-serif;
  --radius: 6px;
  --shadow: 0 8px 28px rgba(0,0,0,.45);
}

*, *::before, *::after { box-sizing: border-box; }

html { scroll-behavior: smooth; }

body {
  margin: 0;
  font: 16px/1.55 var(--font-sans);
  color: var(--ink);
  background: var(--bg);
  min-height: 100vh;
}

.skip-link {
  position: absolute;
  left: -9999px;
  top: 0;
  background: var(--accent);
  color: #111;
  padding: .5rem 1rem;
  z-index: 100;
}
.skip-link:focus { left: .5rem; top: .5rem; }

html, body {
  height: 100%;
  overflow: hidden;
}

.layout {
  display: flex;
  height: 100%;
  min-height: 100vh;
  overflow: hidden;
}

/* Sidebar */
.sidebar {
  position: relative;
  align-self: stretch;
  width: var(--sidebar-w);
  height: 100%;
  max-height: 100vh;
  overflow: auto;
  background: var(--bg-elev);
  border-right: 1px solid var(--rule);
  flex-shrink: 0;
  z-index: 20;
}

.sidebar-head {
  padding: 1rem 1rem .75rem;
  border-bottom: 1px solid var(--rule);
  position: sticky;
  top: 0;
  background: var(--bg-elev);
  z-index: 1;
}

.site-title {
  font-family: var(--font-serif);
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--accent);
  text-decoration: none;
  display: block;
}
.site-title:hover { color: var(--link-hover); }
.site-title { margin-bottom: .65rem; }

.nav-filter {
  width: 100%;
  padding: .4rem .55rem;
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--ink);
  font: inherit;
  font-size: .88rem;
}
.nav-filter:focus {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.search-results {
  /* Fixed popup so it can be wider than the narrow sidebar */
  position: fixed;
  z-index: 250;
  width: min(36rem, calc(100vw - 1.25rem));
  max-height: min(50vh, 28rem);
  margin: 0;
  overflow-y: auto;
  border: 1px solid var(--rule);
  border-top: 3px solid var(--accent);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: var(--shadow);
}
.search-results[hidden] { display: none; }
.search-results-meta {
  margin: 0;
  padding: 0.35rem 0.55rem;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--muted, #6b6560);
  border-bottom: 1px solid var(--rule);
  position: sticky;
  top: 0;
  background: var(--bg);
}
.search-hit {
  display: block;
  padding: 0.45rem 0.55rem;
  border-bottom: 1px solid var(--rule);
  text-decoration: none;
  color: inherit;
}
.search-hit:last-child { border-bottom: none; }
.search-hit:hover {
  background: var(--bg-hover);
}
.search-hit-title {
  display: block;
  font-size: 0.86rem;
  font-weight: 600;
  color: var(--accent);
  margin: 0 0 0.15rem;
}
.search-hit-where {
  display: block;
  font-size: 0.7rem;
  color: var(--muted, #6b6560);
  margin: 0 0 0.2rem;
}
.search-hit-snippet {
  display: block;
  font-size: 0.78rem;
  line-height: 1.35;
  color: var(--quote);
}
.search-hit-snippet mark {
  background: rgba(224, 184, 136, 0.35);
  color: inherit;
  padding: 0 0.1em;
  border-radius: 2px;
}
.search-empty {
  margin: 0;
  padding: 0.55rem;
  font-size: 0.82rem;
  color: var(--muted, #6b6560);
}

.toc { padding: .5rem 0 1.5rem; }
.toc ul {
  list-style: none;
  margin: 0;
  padding: 0;
}
.toc li a {
  display: block;
  padding: .28rem .9rem;
  color: var(--quote);
  text-decoration: none;
  font-size: .86rem;
  border-left: 3px solid transparent;
}
.toc li a:hover {
  background: var(--bg-hover);
  color: var(--accent);
}
.toc li.current a {
  color: var(--accent);
  border-left-color: var(--accent);
  background: var(--bg-hover);
  font-weight: 600;
}
.toc li.nav-arcana a {
  padding-left: 1.35rem;
  font-size: 0.8rem;
  color: var(--muted);
}
.toc li.nav-arcana.current a,
.toc li.nav-arcana a:hover {
  color: var(--accent);
}
.toc li.nav-hub a {
  font-weight: 600;
}
.toc li.nav-book-label {
  list-style: none;
  margin: 0.85rem 0 0.35rem;
  padding: 0.45rem 0.9rem 0.25rem;
  font-family: var(--font-serif);
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--accent);
  border-top: 1px solid var(--rule);
  pointer-events: none;
}
.toc li.nav-book-label:first-child {
  margin-top: 0.25rem;
  border-top: none;
}
.toc li.has-sections > a {
  font-weight: 600;
}
.toc ul.nav-sections {
  list-style: none;
  margin: 0.15rem 0 0.35rem;
  padding: 0 0 0 0.65rem;
  border-left: 2px solid var(--rule);
}
.toc li.nav-section {
  margin: 0;
}
.toc li.nav-section a {
  display: block;
  padding: 0.18rem 0.55rem 0.18rem 0.5rem;
  font-size: 0.78rem;
  line-height: 1.3;
  color: var(--muted, #6b6560);
  text-decoration: none;
  border-radius: 3px;
}
.toc li.nav-section a:hover {
  color: var(--accent);
  background: rgba(0, 0, 0, 0.04);
}
.toc li.current ul.nav-sections {
  border-left-color: var(--accent);
}
.toc li.current li.nav-section a {
  color: var(--ink, #2a2622);
}
.toc li.hidden { display: none; }

.arcana-index ol {
  columns: 2;
  column-gap: 1.5rem;
  padding-left: 1.25rem;
}
.arcana-index li {
  break-inside: avoid;
  margin: 0.25rem 0;
}
.arcana-back {
  margin: 0 0 0.75rem;
  font-size: 0.88rem;
}

.main-wrap {
  flex: 1;
  min-width: 0;
  max-width: none;
  margin: 0;
  padding: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.sidebar-toggle {
  display: none;
  position: fixed;
  top: 0.5rem;
  left: 0.5rem;
  z-index: 30;
  background: var(--bg-elev);
  border: 1px solid var(--rule);
  color: var(--accent);
  font-size: 1.25rem;
  padding: .25rem .55rem;
  border-radius: var(--radius);
  cursor: pointer;
}

/*
 * Horizontal “scroll of pages”:
 * fixed height, narrow columns, content fills top-to-bottom then left-to-right.
 * Extra columns appear off-screen with a bottom scrollbar.
 */
.content-scroll {
  flex: 1;
  height: 100%;
  min-height: 0;
  overflow-x: auto;
  overflow-y: hidden;
  border: none;
  border-radius: 0;
  background: var(--bg);
  scrollbar-color: var(--accent) var(--bg-elev);
  scrollbar-width: thin;
}
.content-scroll::-webkit-scrollbar {
  height: 10px;
}
.content-scroll::-webkit-scrollbar-track {
  background: var(--bg-elev);
}
.content-scroll::-webkit-scrollbar-thumb {
  background: var(--rule);
  border-radius: 5px;
}
.content-scroll::-webkit-scrollbar-thumb:hover {
  background: var(--accent);
}

.content {
  --col-w: 360px;
  font-size: 0.88rem;
  line-height: 1.4;
  height: 100%;
  box-sizing: border-box;
  padding: 0.85rem 1rem 1rem;
  column-width: var(--col-w);
  column-gap: 1.25rem;
  column-rule: 1px solid var(--rule);
  column-fill: auto; /* fill first column, then next — enables horizontal growth */
  /* width grows with number of columns */
  width: max-content;
  max-width: none;
}
.content > :first-child { margin-top: 0; }

.content h1 {
  font-family: var(--font-serif);
  font-size: 1.35rem;
  color: var(--accent);
  margin: 0.5rem 0 0.45rem;
}
.content h1:first-child { display: none; }

.content h2 {
  font-family: var(--font-serif);
  font-size: 1.05rem;
  color: var(--accent);
  margin: 0.85rem 0 0.35rem;
  border-bottom: 1px solid var(--rule);
  padding-bottom: .15rem;
  break-after: avoid;
  break-inside: avoid;
}

.content h3 {
  font-family: var(--font-serif);
  font-size: 0.98rem;
  color: var(--link);
  margin: 0.7rem 0 0.3rem;
  break-after: avoid;
  break-inside: avoid;
}

.content h4 {
  font-size: 0.92rem;
  color: var(--quote);
  margin: 0.55rem 0 0.25rem;
}

.content p { margin: 0.35rem 0; orphans: 3; widows: 3; }

.content ul, .content ol {
  margin: 0.3rem 0 0.45rem;
  padding-left: 1.1rem;
}
.content li { margin: 0.12rem 0; break-inside: avoid; }

.content blockquote {
  margin: 0.5rem 0;
  padding: 0.35rem 0.65rem;
  border-left: 3px solid var(--accent);
  background: var(--bg-elev);
  color: var(--quote);
  break-inside: avoid;
}

.content code {
  font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
  font-size: .9em;
  background: var(--bg-elev);
  padding: .1em .35em;
  border-radius: 3px;
}

.content strong { color: #f0e6d8; }

.content hr {
  border: none;
  border-top: 1px solid var(--rule);
  margin: 0.75rem 0;
}

.content table {
  width: 100%;
  border-collapse: collapse;
  margin: 0.35rem 0;
  font-size: 0.82rem;
  break-inside: avoid;
}
.content th, .content td {
  border: 1px solid var(--rule);
  padding: 0.22rem 0.4rem;
  text-align: left;
  vertical-align: top;
}
.content th {
  background: var(--bg-elev);
  color: var(--accent);
  font-family: var(--font-serif);
  white-space: nowrap;
  width: 2.4rem;
}

/* Trade / value tables */
.value-table {
  break-inside: avoid;
  margin: 0.45rem 0 0.65rem;
  background: var(--bg-elev);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  overflow: hidden;
  max-width: var(--col-w);
}
.value-table-head {
  padding: 0.35rem 0.5rem;
  border-bottom: 1px solid var(--rule);
  background: var(--bg-hover);
  font-family: var(--font-serif);
  color: var(--accent);
  font-weight: 700;
  font-size: 0.92rem;
}
.value-table table {
  width: 100%;
  margin: 0;
  border: none;
  font-size: 0.82rem;
}
.value-table td {
  border-left: none;
  border-right: none;
  padding: 0.2rem 0.45rem;
  vertical-align: top;
}
.value-table tr:last-child td { border-bottom: none; }
.value-table td.val {
  text-align: right;
  white-space: nowrap;
  font-weight: 700;
  color: var(--accent);
  width: 2.75rem;
  font-variant-numeric: tabular-nums;
}

/* Roll tables */
.roll-table {
  break-inside: avoid;
  margin: 0.45rem 0 0.65rem;
  background: var(--bg-elev);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  overflow: hidden;
  max-width: var(--col-w);
}
.roll-table-head {
  display: flex;
  align-items: baseline;
  gap: 0.4rem;
  padding: 0.35rem 0.5rem;
  border-bottom: 1px solid var(--rule);
  background: var(--bg-hover);
  font-family: var(--font-serif);
  color: var(--accent);
  font-weight: 700;
  font-size: 0.92rem;
}
.roll-table .roll-label { color: var(--accent); }
.roll-table table { margin: 0; border: none; }
.roll-table th, .roll-table td { border-left: none; border-right: none; }
.roll-table tr:last-child th,
.roll-table tr:last-child td { border-bottom: none; }

/* Enemy / monster blocks — tight, card-like */
.stat-block {
  break-inside: avoid;
  margin: 0.4rem 0 0.55rem;
  padding: 0.4rem 0.55rem 0.45rem;
  background: var(--stat-bg, #241c16);
  border: 1px solid var(--rule);
  border-left: 3px solid var(--accent);
  border-radius: 0 var(--radius) var(--radius) 0;
  font-size: 0.82rem;
  line-height: 1.28;
  max-width: var(--col-w);
}
.stat-block .stat-name {
  font-family: var(--font-serif);
  font-size: 0.98rem;
  color: var(--accent);
  margin: 0 0 0.15rem;
  border: none;
  padding: 0;
}
.stat-block .stat-tags {
  margin: 0 0 0.2rem;
  color: var(--muted);
  font-style: italic;
  font-size: 0.8rem;
}
.stat-block .stat-stats {
  margin: 0.15rem 0;
  color: var(--ink);
}
.stat-block .stat-stats br { content: ""; }
.stat-block .stat-moves {
  margin: 0.2rem 0 0;
  padding-left: 1rem;
}
.stat-block .stat-moves li {
  margin: 0.05rem 0;
}
.stat-block .stat-note {
  margin: 0.25rem 0 0;
  color: var(--quote);
  font-size: 0.8rem;
}
/* Deep-link target highlight */
.stat-block:target,
h2:target,
h3:target,
.roll-table:target,
.target-highlight {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  border-radius: 2px;
}

/* Images flow into the rightmost columns after text */

/* Index: normal vertical flow, no horizontal columns */
body:has(.index-grid) {
  overflow: auto;
}
body:has(.index-grid) .layout {
  height: auto;
  min-height: 100vh;
  overflow: visible;
}
body:has(.index-grid) .main-wrap {
  overflow: visible;
}
body:has(.index-grid) .content-scroll {
  height: auto;
  min-height: 0;
  overflow: visible;
  border: none;
  background: transparent;
}
body:has(.index-grid) .content {
  width: auto;
  height: auto;
  column-width: auto;
  columns: 1;
  padding: 1rem 1.25rem 2rem;
}

@media (max-width: 700px) {
  .content { --col-w: min(360px, calc(100vw - 2rem)); }
}

/* Wiki links */
a.wiki-link {
  color: var(--link);
  text-decoration: none;
  border-bottom: 1px solid rgba(224, 184, 136, .35);
  transition: color .12s, border-color .12s, background .12s;
}
a.wiki-link:hover {
  color: var(--link-hover);
  border-bottom-color: var(--link-hover);
  background: rgba(212, 165, 116, .12);
}
a.wiki-link strong {
  color: inherit;
  font-weight: 700;
}

/* Dice */
button.dice-roll {
  display: inline;
  font: inherit;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  color: #1a1410;
  background: linear-gradient(180deg, #e8c090, var(--accent));
  border: 1px solid #a67c4a;
  border-radius: 4px;
  padding: .05em .4em;
  margin: 0 .1em;
  cursor: pointer;
  line-height: 1.4;
  box-shadow: 0 1px 0 rgba(0,0,0,.25);
  vertical-align: baseline;
}
button.dice-roll:hover {
  filter: brightness(1.08);
}
button.dice-roll:active {
  transform: translateY(1px);
}
button.dice-roll.rolling {
  animation: dice-pulse .35s ease;
}
@keyframes dice-pulse {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.08); }
}

/* Campaign maps (Maps page only — not inverted) */
.gallery-grid.maps {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(16rem, 1fr));
  gap: 1rem;
  margin: 0.75rem 0 1.5rem;
  break-inside: avoid;
}
.wiki-figure.map-figure {
  margin: 0;
  break-inside: avoid;
  background: var(--bg-elev);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  overflow: hidden;
}
.wiki-figure.map-figure a {
  display: block;
  line-height: 0;
}
.wiki-figure.map-figure img {
  width: 100%;
  height: auto;
  display: block;
  /* Real campaign maps — keep natural colors */
  filter: none;
}
.wiki-figure.map-figure figcaption {
  padding: 0.45rem 0.65rem;
  font-size: 0.82rem;
  color: var(--muted);
  border-top: 1px solid var(--rule);
}
/* On the maps page, allow vertical scroll and wider single column for big maps.
   Scope to .content so hover previews cannot trigger this. */
.content:has(.gallery-grid.maps) {
  column-count: 1;
  column-width: auto;
  max-width: 56rem;
}
.content-scroll:has(.gallery-grid.maps) {
  overflow: auto;
}

.dice-toast {
  position: fixed;
  bottom: 1.5rem;
  left: 50%;
  transform: translateX(-50%) translateY(120%);
  background: var(--bg-elev);
  border: 1px solid var(--accent);
  color: var(--ink);
  padding: .65rem 1.15rem;
  border-radius: 999px;
  box-shadow: var(--shadow);
  font-family: var(--font-serif);
  font-size: 1.05rem;
  z-index: 1000;
  transition: transform .25s ease, opacity .25s ease;
  opacity: 0;
  pointer-events: none;
}
.dice-toast.show {
  transform: translateX(-50%) translateY(0);
  opacity: 1;
}
.dice-toast .result {
  color: var(--accent);
  font-weight: 700;
  font-size: 1.2em;
  margin-left: .35rem;
}

/* Arcana cards */
.arcana-card {
  break-inside: avoid;
  margin: 0.5rem 0 1.25rem;
  border: 1px solid var(--rule);
  border-radius: calc(var(--radius) + 2px);
  background: var(--bg-elev);
  box-shadow: var(--shadow);
  overflow: hidden;
  max-width: 36rem;
}
.arcana-card.arcana-major {
  max-width: 42rem;
}
.arcana-face {
  padding: 0.85rem 1rem 1rem;
}
.arcana-front {
  border-bottom: 1px solid var(--rule);
  background: linear-gradient(180deg, rgba(212, 165, 116, 0.08), transparent 3rem);
}
.arcana-back-face {
  background: rgba(0, 0, 0, 0.12);
}
.arcana-face-label {
  margin: 0 0 0.35rem;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
}
.arcana-discovery {
  margin: 0 0 0.25rem;
  font-family: var(--font-serif);
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--ink);
}
.arcana-tags {
  margin: 0 0 0.55rem;
  font-size: 0.82rem;
  font-style: italic;
  color: var(--muted);
}
.arcana-unlock {
  margin-top: 0.65rem;
  padding: 0.55rem 0.7rem;
  border: 1px solid var(--rule);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  background: rgba(0, 0, 0, 0.18);
}
.arcana-unlock-intro {
  margin: 0 0 0.4rem;
  font-size: 0.92rem;
}
.arcana-power-name {
  margin: 0 0 0.65rem;
  font-family: var(--font-serif);
  font-size: 1.25rem;
  font-weight: 700;
  color: var(--accent);
  letter-spacing: 0.01em;
}
.arcana-move {
  margin: 0 0 0.75rem;
  padding-bottom: 0.55rem;
  border-bottom: 1px dashed rgba(74, 63, 50, 0.7);
}
.arcana-move:last-child {
  border-bottom: none;
  margin-bottom: 0;
  padding-bottom: 0;
}
.arcana-move p {
  margin: 0 0 0.35rem;
  font-size: 0.94rem;
}
.arcana-picks {
  margin: 0.25rem 0 0;
  padding-left: 1.15rem;
  font-size: 0.9rem;
}
.arcana-picks li {
  margin: 0.15rem 0;
}
.arcana-sub {
  margin: 0.75rem 0 0.35rem;
  font-size: 1rem;
  color: var(--accent);
}
.arcana-note {
  margin: 0.35rem 0 0;
  font-size: 0.85rem;
  color: var(--muted);
}
.arcana-major-body {
  padding: 0 0.15rem;
  font-size: 0.94rem;
}
.arcana-consequences {
  margin: 0.75rem 1rem 1rem;
}
.arcana-track {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem 0.5rem;
  margin: 0.35rem 0 0.75rem;
  padding: 0.45rem 0.55rem;
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: rgba(0, 0, 0, 0.15);
  font-size: 0.9rem;
}
.arcana-track .track-label {
  font-weight: 700;
  color: var(--accent);
  margin-right: 0.25rem;
}
.arcana-track .track-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem;
}
.arcana-track .track-step {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0.2rem 0.35rem;
  border: 1px solid var(--rule);
  border-radius: 0.35rem;
  background: var(--bg);
  cursor: pointer;
  line-height: 1;
}
.arcana-track .track-step input.wiki-check {
  margin: 0;
  width: 1.05rem;
  height: 1.05rem;
  accent-color: var(--accent);
  cursor: pointer;
}
.arcana-follower {
  margin: 0.65rem 0 0.85rem;
  padding: 0.55rem 0.7rem;
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  background: rgba(0, 0, 0, 0.2);
}
.arcana-follower .arcana-sub {
  margin-top: 0;
}
.arcana-stat {
  margin: 0.2rem 0;
  font-size: 0.88rem;
  font-variant-numeric: tabular-nums;
}
.arcana-moves-list {
  margin-top: 0.4rem;
}
/* Arcana pages: normal vertical flow, left-aligned (not multi-column).
   Must target .content (not body:has) so hover previews that inject
   .arcana-card into #wiki-preview do not reflow gazetteer pages. */
.content:has(.arcana-card) {
  columns: 1;
  column-count: 1;
  column-width: auto;
  column-gap: normal;
  column-rule: none;
  width: 100%;
  max-width: 42rem;
  height: auto;
  min-height: 100%;
  margin: 0;
  box-sizing: border-box;
}
.content:has(.arcana-card) .arcana-card {
  break-inside: auto;
  margin-left: 0;
  margin-right: 0;
  max-width: none;
}
.content-scroll:has(.arcana-card) {
  overflow-x: hidden;
  overflow-y: auto;
}

/* Steading improvements + requirement checkboxes */
.steading-improvement,
.requirement-block {
  break-inside: avoid;
  margin: 0.75rem 0 1rem;
  padding: 0.65rem 0.8rem 0.75rem;
  border: 1px solid var(--rule);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  background: rgba(0, 0, 0, 0.03);
}
.si-kind {
  margin: 0 0 0.2rem;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--accent);
}
.si-title {
  margin: 0 0 0.4rem;
  font-family: var(--font-serif);
  font-size: 1.05rem;
  font-weight: 700;
  color: var(--ink);
  letter-spacing: 0.02em;
}
.si-blurb {
  margin: 0 0 0.45rem;
  font-style: italic;
  color: var(--muted, #6b6560);
  font-size: 0.92rem;
}
.si-requires {
  margin: 0.45rem 0 0.3rem;
  font-weight: 600;
  font-size: 0.9rem;
}
ul.check-list {
  list-style: none;
  margin: 0.15rem 0 0.5rem;
  padding: 0;
}
li.check-item {
  margin: 0.2rem 0;
}
li.check-item label {
  display: flex;
  align-items: flex-start;
  gap: 0.45rem;
  cursor: pointer;
  line-height: 1.35;
  font-size: 0.92rem;
}
li.check-item input.wiki-check {
  margin: 0.2rem 0 0;
  flex-shrink: 0;
  width: 1rem;
  height: 1rem;
  accent-color: var(--accent);
  cursor: pointer;
}
li.check-item input.wiki-check:checked + span {
  text-decoration: line-through;
  opacity: 0.72;
}
.steading-improvement > p {
  margin: 0.4rem 0 0;
  font-size: 0.92rem;
}

/* Hover preview (Wikipedia-style) */
.wiki-preview {
  position: fixed;
  z-index: 900;
  width: min(36rem, calc(100vw - 1.5rem));
  max-height: min(28rem, calc(100vh - 2rem));
  overflow: auto;
  background: var(--bg-elev);
  border: 1px solid var(--rule);
  border-top: 3px solid var(--accent);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  pointer-events: none;
  opacity: 0;
  transform: translateY(4px);
  transition: opacity .12s ease, transform .12s ease;
}
.wiki-preview.pv-arcana {
  width: min(28rem, calc(100vw - 1.5rem));
  max-height: min(80vh, 42rem);
}
.wiki-preview.visible {
  opacity: 1;
  transform: translateY(0);
  pointer-events: auto;
}
.wiki-preview .pv-excerpt a.wiki-link {
  color: var(--link);
  text-decoration: none;
  border-bottom: 1px solid rgba(224, 184, 136, .35);
}
.wiki-preview .pv-excerpt a.wiki-link:hover {
  color: var(--link-hover);
  border-bottom-color: var(--link-hover);
}
.wiki-preview .pv-title {
  font-family: var(--font-serif);
  font-size: 1.25rem;
  font-weight: 700;
  color: var(--accent);
  padding: .85rem 1rem .4rem;
  margin: 0;
}
.wiki-preview .pv-body {
  display: flex;
  gap: .9rem;
  padding: .35rem 1rem 1rem;
  font-size: 1rem;
  color: var(--quote);
  line-height: 1.5;
}
.wiki-preview .pv-thumb {
  width: 140px;
  height: 140px;
  object-fit: cover;
  border-radius: 4px;
  flex-shrink: 0;
  background: var(--bg);
  border: 1px solid var(--rule);
  filter: invert(1) hue-rotate(180deg);
}
.wiki-preview .pv-excerpt {
  margin: 0;
  display: -webkit-box;
  -webkit-line-clamp: 12;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.wiki-preview .pv-kind {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-left: 0.35rem;
  font-family: var(--font-sans);
}
/* Full section/stat-block deep-link preview */
.wiki-preview .pv-full {
  display: block;
  padding: 0.35rem 1rem 1rem;
  max-height: min(22rem, calc(100vh - 6rem));
  overflow: auto;
  font-size: 0.92rem;
  line-height: 1.4;
  color: var(--ink);
}
.wiki-preview.pv-arcana .pv-full {
  max-height: none;
  padding: 0.55rem 0.65rem 0.75rem;
}
.wiki-preview .pv-full .arcana-card {
  margin: 0;
  box-shadow: none;
  max-width: none;
  border: none;
  background: transparent;
}
.wiki-preview .pv-full .arcana-face {
  padding: 0.45rem 0.25rem 0.55rem;
}
.wiki-preview .pv-full .arcana-front,
.wiki-preview .pv-full .arcana-back-face {
  background: transparent;
}
.wiki-preview .pv-full .arcana-unlock {
  background: rgba(0, 0, 0, 0.22);
}
.wiki-preview .pv-full > :first-child { margin-top: 0; }
.wiki-preview .pv-full .stat-block {
  margin: 0;
  max-width: none;
}
.wiki-preview .pv-full .stat-name {
  font-size: 1.05rem;
}
.wiki-preview .pv-full .roll-table,
.wiki-preview .pv-full .value-table {
  margin: 0;
  max-width: none;
}
.wiki-preview .pv-full h2,
.wiki-preview .pv-full h3 {
  margin: 0 0 0.4rem;
  font-size: 1.05rem;
  border-bottom: 1px solid var(--rule);
  padding-bottom: 0.15rem;
}
.wiki-preview .pv-full p { margin: 0.35rem 0; }
.wiki-preview .pv-full ul { margin: 0.3rem 0; padding-left: 1.1rem; }
.wiki-preview .pv-loading {
  padding: 1.25rem;
  color: var(--muted);
  font-size: 1rem;
}


/* Index */
.index-hero {
  padding: 1rem 0 1.5rem;
}
.index-hero p.lede {
  color: var(--muted);
  font-size: 1.05rem;
  max-width: 40rem;
}
.index-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr));
  gap: .65rem;
  margin: 1.25rem 0;
}
.index-card {
  display: block;
  background: var(--bg-elev);
  border: 1px solid var(--rule);
  border-radius: var(--radius);
  padding: .75rem .9rem;
  text-decoration: none;
  color: var(--ink);
  transition: border-color .12s, background .12s;
}
.index-card:hover {
  border-color: var(--accent);
  background: var(--bg-hover);
}
.index-card .card-title {
  font-family: var(--font-serif);
  color: var(--accent);
  font-weight: 700;
  font-size: 1rem;
  margin: 0 0 .35rem;
}
.index-card .card-excerpt {
  font-size: .82rem;
  color: var(--muted);
  margin: 0;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.4;
}

/* Mobile */
@media (max-width: 860px) {
  .sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    transform: translateX(-100%);
    transition: transform .2s ease;
    box-shadow: var(--shadow);
  }
  .sidebar.open { transform: translateX(0); }
  .sidebar-toggle { display: inline-block; }
  .main-wrap { padding: 0; }
  body.sidebar-open::after {
    content: "";
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,.45);
    z-index: 15;
  }
}

@media print {
  .sidebar, .sidebar-toggle, .dice-toast, .wiki-preview { display: none !important; }
  .main-wrap { max-width: none; }
  html, body { height: auto; overflow: visible; }
  body { background: #fff; color: #111; }
  .content h1, .content h2 { color: #333; }
  .content-scroll { height: auto; overflow: visible; }
  a.wiki-link { color: #000; border: none; text-decoration: underline; }
  button.dice-roll {
    background: none;
    border: none;
    color: inherit;
    padding: 0;
    box-shadow: none;
    font-weight: inherit;
  }
}
"""

JS = r"""/* Stonetop Book II Wiki — dice rolls + hover previews */
(function () {
  "use strict";

  const SCRIPT_BASE = (function () {
    const scripts = document.getElementsByTagName("script");
    for (let i = scripts.length - 1; i >= 0; i--) {
      const src = scripts[i].src || "";
      if (src.indexOf("wiki.js") !== -1) {
        return src.replace(/js\/wiki\.js.*$/, "");
      }
    }
    return "";
  })();

  let previews = null;
  let previewsPromise = null;

  function loadPreviews() {
    if (previews) return Promise.resolve(previews);
    if (typeof window.WIKI_PREVIEWS === "object" && window.WIKI_PREVIEWS) {
      previews = window.WIKI_PREVIEWS;
      return Promise.resolve(previews);
    }
    if (previewsPromise) return previewsPromise;
    // Prefer previews-data.js (works with file://); fall back to fetch JSON
    previewsPromise = new Promise(function (resolve) {
      const s = document.createElement("script");
      s.src = SCRIPT_BASE + "js/previews-data.js";
      s.onload = function () {
        previews = window.WIKI_PREVIEWS || {};
        resolve(previews);
      };
      s.onerror = function () {
        fetch(SCRIPT_BASE + "previews.json")
          .then(function (r) {
            return r.ok ? r.json() : {};
          })
          .then(function (data) {
            previews = data || {};
            resolve(previews);
          })
          .catch(function () {
            previews = {};
            resolve(previews);
          });
      };
      document.head.appendChild(s);
    });
    return previewsPromise;
  }

  /* ---------- Dice ---------- */
  /** Parse/roll NdS, NdS+M, NdS-M (e.g. d10+3, 1d4-1, 2d6). */
  function rollDice(expr) {
    const cleaned = String(expr || "")
      .toLowerCase()
      .replace(/[–—]/g, "-")
      .replace(/\s+/g, "");
    const m = cleaned.match(/^(\d*)d(\d+)([+-]\d+)?$/);
    if (!m) return null;
    const n = m[1] === "" ? 1 : parseInt(m[1], 10);
    const sides = parseInt(m[2], 10);
    const mod = m[3] ? parseInt(m[3], 10) : 0;
    if (!n || n > 99 || !sides) return null;
    let total = 0;
    const parts = [];
    for (let i = 0; i < n; i++) {
      const v = 1 + Math.floor(Math.random() * sides);
      parts.push(v);
      total += v;
    }
    total += mod;
    const diceExpr = (n === 1 ? "" : String(n)) + "d" + sides;
    const modExpr = mod === 0 ? "" : mod > 0 ? "+" + mod : String(mod);
    return {
      total: total,
      parts: parts,
      mod: mod,
      expr: diceExpr + modExpr,
      n: n,
      sides: sides,
    };
  }

  const toast = document.getElementById("dice-toast");
  let toastTimer = null;

  function showDiceResult(result) {
    if (!toast) return;
    var detail = "";
    if (result.parts.length > 1 || result.mod) {
      var bits = result.parts.join(" + ");
      if (result.mod) {
        bits +=
          result.mod > 0
            ? " + " + result.mod
            : " − " + Math.abs(result.mod);
      }
      detail = " (" + bits + ")";
    }
    toast.innerHTML =
      '<span class="label">' +
      result.expr +
      "</span>" +
      detail +
      ' → <span class="result">' +
      result.total +
      "</span>";
    toast.hidden = false;
    // force reflow
    void toast.offsetWidth;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toast.classList.remove("show");
      setTimeout(function () {
        toast.hidden = true;
      }, 250);
    }, 2200);
  }

  document.addEventListener("click", function (e) {
    const btn = e.target.closest(".dice-roll");
    if (!btn) return;
    e.preventDefault();
    const result = rollDice(btn.getAttribute("data-dice"));
    if (!result) return;
    btn.classList.remove("rolling");
    void btn.offsetWidth;
    btn.classList.add("rolling");
    showDiceResult(result);
  });

  /* ---------- Hover previews ---------- */
  const bubble = document.getElementById("wiki-preview");
  let hideTimer = null;
  let activeLink = null;
  let pageMap = null;

  function loadPageMap() {
    if (pageMap) return Promise.resolve(pageMap);
    if (typeof window.WIKI_PAGE_MAP === "object" && window.WIKI_PAGE_MAP) {
      pageMap = window.WIKI_PAGE_MAP;
      return Promise.resolve(pageMap);
    }
    return loadPreviews().then(function () {
      pageMap = window.WIKI_PAGE_MAP || {};
      return pageMap;
    });
  }

  function hidePreview() {
    if (!bubble) return;
    bubble.classList.remove("visible");
    activeLink = null;
    setTimeout(function () {
      if (!bubble.classList.contains("visible")) bubble.hidden = true;
    }, 150);
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hidePreview, 280);
  }

  function cancelHide() {
    clearTimeout(hideTimer);
    hideTimer = null;
  }

  function positionPreview(link) {
    if (!bubble) return;
    const rect = link.getBoundingClientRect();
    const margin = 10;
    const bw = bubble.offsetWidth || 520;
    const bh = bubble.offsetHeight || 320;
    let left = rect.left;
    // Overlap the link slightly so the cursor can cross into the bubble
    let top = rect.bottom + 2;

    if (left + bw > window.innerWidth - margin) {
      left = window.innerWidth - bw - margin;
    }
    if (left < margin) left = margin;

    if (top + bh > window.innerHeight - margin) {
      top = rect.top - bh - 2;
    }
    if (top < margin) top = margin;

    bubble.style.left = left + "px";
    bubble.style.top = top + "px";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** Turn "(page 12)" / "pages 8-11" / "Book II, page 270" in excerpt text into wiki links. */
  function linkifyPageRefs(text, map, book) {
    var rawMap = map || {};
    function resolveBookMap(raw, bookId) {
      if (!raw) return {};
      if (raw.book1 || raw.book2) {
        return raw[bookId || "book2"] || {};
      }
      return raw;
    }
    function bookIdFromToken(tok) {
      var t = String(tok || "").toLowerCase();
      if (t === "i" || t === "1") return "book1";
      if (t === "ii" || t === "2") return "book2";
      return null;
    }
    var defaultBook = book || "book2";
    var mapDefault = resolveBookMap(rawMap, defaultBook);
    var escaped = escapeHtml(text || "No summary available.");

    function pageHref(slug) {
      var path = location.pathname.replace(/\\/g, "/");
      if (/\/pages\/[^/]+\.html$/i.test(path)) {
        return slug + ".html";
      }
      return "pages/" + slug + ".html";
    }

    function normSection(name) {
      return String(name || "")
        .toLowerCase()
        .replace(/[\"'“”‘’]/g, "")
        .replace(/^(a|an|the)\s+/, "")
        .replace(/[^a-z0-9]+/g, " ")
        .replace(/\s+/g, " ")
        .trim();
    }

    function resolveFragment(info, label) {
      if (!info || !info.sections || !label) return "";
      var keys = [normSection(label)];
      var k0 = keys[0];
      if (k0.endsWith("s") && k0.length > 3) keys.push(k0.slice(0, -1));
      else if (k0) keys.push(k0 + "s");
      for (var i = 0; i < keys.length; i++) {
        if (info.sections[keys[i]]) return info.sections[keys[i]];
      }
      return "";
    }

    function linkForPage(num, label, bookId) {
      var m = bookId ? resolveBookMap(rawMap, bookId) : mapDefault;
      var info = m[String(num)];
      if (!info || !info.slug) {
        return escapeHtml(label || "page " + num);
      }
      var frag = resolveFragment(info, label);
      var href = pageHref(info.slug) + (frag ? "#" + frag : "");
      var title = info.title || "";
      if (label && frag) title = label + " — " + title;
      return (
        '<a class="wiki-link" href="' +
        href +
        '" data-slug="' +
        escapeHtml(info.slug) +
        '"' +
        (frag ? ' data-fragment="' + escapeHtml(frag) + '"' : "") +
        ' title="' +
        escapeHtml(title) +
        '">' +
        escapeHtml(label || info.title || "page " + num) +
        "</a>"
      );
    }

    // Cross-book: Title (Book II, page 270) / Book I, page 245
    escaped = escaped.replace(
      /(?:([A-Za-z][A-Za-z0-9'’\-]*(?:\s+[A-Za-z][A-Za-z0-9'’\-]*){0,6})\s+)?\(?Book\s*(II|I|2|1)\s*[,:]?\s*(?:starting\s+on\s+)?pages?\s+([\d,\s\-–—]+)\)?/gi,
      function (_m, title, bookTok, spec) {
        var bid = bookIdFromToken(bookTok);
        var nums = String(spec).match(/\d+/g) || [];
        if (!bid || !nums.length) return _m;
        var primary = linkForPage(nums[0], title || null, bid);
        if (nums.length === 1) return primary;
        var extras = nums
          .slice(1)
          .map(function (n) {
            return linkForPage(n, null, bid);
          })
          .join(" · ");
        return primary + " (" + extras + ")";
      }
    );

    // Title (page 12) / Title (pages 8-11) / Title (page 282, 350)
    escaped = escaped.replace(
      /([A-Za-z][A-Za-z0-9'’\-]*(?:\s+[A-Za-z][A-Za-z0-9'’\-]*){0,6})\s+\((?:see\s+)?pages?\s+([\d,\s\-–—]+)\)/gi,
      function (_m, title, spec) {
        var nums = String(spec).match(/\d+/g) || [];
        if (!nums.length) return _m;
        var primary = linkForPage(nums[0], title);
        if (nums.length === 1) return primary;
        var extras = nums
          .slice(1)
          .map(function (n) {
            return linkForPage(n);
          })
          .join(" · ");
        return primary + " (" + extras + ")";
      }
    );

    // Remaining (page 12) / (pages 8-11)
    escaped = escaped.replace(
      /\((?:see\s+)?pages?\s+([\d,\s\-–—]+)\)/gi,
      function (_m, spec) {
        var nums = String(spec).match(/\d+/g) || [];
        if (!nums.length) return _m;
        return nums.map(function (n) { return linkForPage(n); }).join(" · ");
      }
    );

    // Bare "page 12" / "pages 8-11" (avoid matching mid-word / already-linked)
    escaped = escaped.replace(
      /(^|[^A-Za-z0-9_\/])((?:see\s+)?pages?\s+)([\d,\s\-–—]+)(?![A-Za-z0-9_\/])/gi,
      function (_m, pre, _label, spec) {
        var nums = String(spec).match(/\d+/g) || [];
        if (!nums.length) return _m;
        return pre + nums.map(function (n) { return linkForPage(n); }).join(" · ");
      }
    );

    return escaped;
  }

  function showPreview(link, data, map) {
    if (!bubble || !data) return;
    activeLink = link;
    var frag = link.getAttribute("data-fragment") || "";
    // Support href="#id" / "page.html#id" even without data-fragment
    if (!frag) {
      var href = link.getAttribute("href") || "";
      var hash = href.indexOf("#");
      if (hash !== -1) frag = href.slice(hash + 1);
    }

    var section =
      frag && data.sections && data.sections[frag] ? data.sections[frag] : null;

    // Full deep-link target (stat block, table, or section body)
    if (section && section.html) {
      bubble.classList.remove("pv-arcana");
      bubble.innerHTML =
        '<p class="pv-title">' +
        escapeHtml(section.name || data.title || "") +
        (section.kind === "stat-block"
          ? ' <span class="pv-kind">monster</span>'
          : "") +
        "</p>" +
        '<div class="pv-body pv-full">' +
        section.html +
        "</div>";
    } else if (data.html) {
      // Full page body for arcana cards (and anything that stores html)
      bubble.classList.add("pv-arcana");
      bubble.innerHTML =
        '<div class="pv-body pv-full pv-card">' + data.html + "</div>";
    } else {
      bubble.classList.remove("pv-arcana");
      let thumb = "";
      if (data.image) {
        thumb =
          '<img class="pv-thumb" src="' +
          SCRIPT_BASE +
          "images/" +
          data.image +
          '" alt="">';
      }
      bubble.innerHTML =
        '<p class="pv-title">' +
        escapeHtml(data.title || "") +
        "</p>" +
        '<div class="pv-body">' +
        thumb +
        '<p class="pv-excerpt">' +
        linkifyPageRefs(
          data.excerpt || "No summary available.",
          map,
          data.book || "book2"
        ) +
        "</p></div>";
    }
    // Bind checkboxes in dynamically injected preview HTML (arcana unlocks, etc.)
    var previewSlug =
      link.getAttribute("data-slug") ||
      (function () {
        var h = link.getAttribute("href") || "";
        var m = h.replace(/\\/g, "/").match(/([^\/#]+)\.html/i);
        return m ? m[1] : "";
      })();
    if (previewSlug && typeof window.bindWikiChecks === "function") {
      window.bindWikiChecks(bubble, previewSlug);
    }
    bubble.hidden = false;
    positionPreview(link);
    void bubble.offsetWidth;
    bubble.classList.add("visible");
  }

  document.addEventListener("mouseover", function (e) {
    // Ignore links inside the preview bubble for *switching* target,
    // but still cancel hide so the bubble stays interactive.
    if (bubble && bubble.contains(e.target)) {
      cancelHide();
      return;
    }
    const link = e.target.closest("a.wiki-link");
    if (!link) return;
    // No hover previews for sidebar navigation links
    if (link.closest(".sidebar, #sidebar, nav.toc")) return;
    // Only trigger from main content links with a slug
    const slug = link.getAttribute("data-slug");
    if (!slug) return;
    cancelHide();
    Promise.all([loadPreviews(), loadPageMap()]).then(function (pair) {
      var data = pair[0];
      var map = pair[1];
      if (!data[slug]) return;
      if (!link.matches(":hover") && !(bubble && bubble.matches(":hover"))) return;
      showPreview(link, data[slug], map);
    });
  });

  document.addEventListener("mouseout", function (e) {
    if (bubble && bubble.contains(e.target)) {
      var relB = e.relatedTarget;
      if (relB && bubble.contains(relB)) return;
      if (relB && activeLink && activeLink.contains(relB)) return;
      scheduleHide();
      return;
    }
    const link = e.target.closest("a.wiki-link");
    if (!link) return;
    const related = e.relatedTarget;
    if (related && (link.contains(related) || (bubble && bubble.contains(related)))) {
      return;
    }
    scheduleHide();
  });

  if (bubble) {
    bubble.addEventListener("mouseenter", cancelHide);
    bubble.addEventListener("mouseleave", function (e) {
      var rel = e.relatedTarget;
      if (rel && activeLink && activeLink.contains(rel)) return;
      scheduleHide();
    });
  }

  document.addEventListener("scroll", function () {
    if (activeLink && bubble && bubble.classList.contains("visible")) {
      positionPreview(activeLink);
    }
  }, true);

  window.addEventListener("blur", hidePreview);

  /* ---------- Sidebar search (titles + full page text) ---------- */
  const filter = document.getElementById("nav-filter");
  const navList = document.getElementById("nav-list");
  const searchResults = document.getElementById("search-results");
  let searchIndex = null;
  let searchIndexPromise = null;

  function loadSearchIndex() {
    if (searchIndex) return Promise.resolve(searchIndex);
    if (typeof window.WIKI_SEARCH_INDEX === "object" && window.WIKI_SEARCH_INDEX) {
      searchIndex = window.WIKI_SEARCH_INDEX;
      return Promise.resolve(searchIndex);
    }
    if (searchIndexPromise) return searchIndexPromise;
    searchIndexPromise = new Promise(function (resolve) {
      const s = document.createElement("script");
      s.src = SCRIPT_BASE + "js/search-index.js";
      s.onload = function () {
        searchIndex = window.WIKI_SEARCH_INDEX || [];
        resolve(searchIndex);
      };
      s.onerror = function () {
        searchIndex = [];
        resolve(searchIndex);
      };
      document.head.appendChild(s);
    });
    return searchIndexPromise;
  }

  function pageHrefFromSlug(slug) {
    var path = location.pathname.replace(/\\/g, "/");
    if (/\/pages\/[^/]+\.html$/i.test(path)) {
      return slug + ".html";
    }
    return "pages/" + slug + ".html";
  }

  function escapeHtmlSearch(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function queryTerms(q) {
    return String(q || "")
      .toLowerCase()
      .split(/\s+/)
      .filter(function (t) {
        return t.length > 0;
      });
  }

  function haystackHasTerms(hay, terms) {
    if (!terms.length) return true;
    for (var i = 0; i < terms.length; i++) {
      if (hay.indexOf(terms[i]) === -1) return false;
    }
    return true;
  }

  function makeSnippet(text, terms, radius) {
    radius = radius || 42;
    if (!text) return "";
    var low = text.toLowerCase();
    var best = -1;
    for (var i = 0; i < terms.length; i++) {
      var at = low.indexOf(terms[i]);
      if (at !== -1 && (best === -1 || at < best)) best = at;
    }
    if (best < 0) {
      var head = text.slice(0, radius * 2);
      return escapeHtmlSearch(head) + (text.length > head.length ? "…" : "");
    }
    var start = Math.max(0, best - radius);
    var end = Math.min(text.length, best + radius + 12);
    var slice = text.slice(start, end);
    var esc = escapeHtmlSearch(slice);
    // Highlight each term (case-insensitive)
    for (var t = 0; t < terms.length; t++) {
      var term = terms[t];
      if (!term) continue;
      var re = new RegExp(
        "(" + term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")",
        "gi"
      );
      esc = esc.replace(re, "<mark>$1</mark>");
    }
    return (start > 0 ? "…" : "") + esc + (end < text.length ? "…" : "");
  }

  function positionSearchResults() {
    var resultsEl = searchResults;
    if (!resultsEl || resultsEl.hidden || !filter) return;
    var r = filter.getBoundingClientRect();
    var gap = 6;
    var maxW = Math.min(36 * 16, window.innerWidth - 20);
    var width = Math.max(r.width, maxW);
    // Prefer aligning under the search box; keep fully on-screen
    var left = r.left;
    if (left + width > window.innerWidth - 10) {
      left = Math.max(10, window.innerWidth - width - 10);
    }
    if (left < 10) left = 10;
    var top = r.bottom + gap;
    var maxH = Math.min(window.innerHeight * 0.5, 28 * 16);
    if (top + 120 > window.innerHeight) {
      // Flip above the box if near the bottom
      top = Math.max(10, r.top - gap - Math.min(maxH, 240));
    }
    resultsEl.style.left = left + "px";
    resultsEl.style.top = top + "px";
    resultsEl.style.width = width + "px";
    resultsEl.style.maxHeight = maxH + "px";
  }

  function runSearch(q) {
    if (!filter || !navList) return;
    var terms = queryTerms(q);
    var resultsEl = searchResults;

    if (!terms.length) {
      if (resultsEl) {
        resultsEl.hidden = true;
        resultsEl.innerHTML = "";
      }
      navList.querySelectorAll("li").forEach(function (li) {
        li.classList.remove("hidden");
      });
      return;
    }

    loadSearchIndex().then(function (index) {
      // Re-check query in case it changed while loading
      var current = filter.value.trim();
      if (queryTerms(current).join(" ") !== terms.join(" ")) return;

      var hits = [];
      var matchSlugs = {};
      for (var i = 0; i < index.length; i++) {
        var doc = index[i];
        var titleHay = (doc.title || "").toLowerCase();
        var textHay = (doc.text || "").toLowerCase();
        var titleHit = haystackHasTerms(titleHay, terms);
        var textHit = haystackHasTerms(textHay, terms);
        if (!titleHit && !textHit) continue;
        matchSlugs[doc.slug] = true;
        var rank = titleHit ? 0 : 1;
        // Prefer denser title matches
        if (titleHit && titleHay === terms.join(" ")) rank = -1;
        hits.push({
          slug: doc.slug,
          title: doc.title,
          book: doc.book,
          titleHit: titleHit,
          textHit: textHit,
          rank: rank,
          snippet: textHit
            ? makeSnippet(doc.text || "", terms)
            : escapeHtmlSearch(doc.excerpt || doc.title || ""),
        });
      }
      hits.sort(function (a, b) {
        if (a.rank !== b.rank) return a.rank - b.rank;
        return (a.title || "").localeCompare(b.title || "");
      });

      // Filter sidebar nav: keep items whose title matches OR slug is a content hit
      navList.querySelectorAll(":scope > li").forEach(function (li) {
        if (li.classList.contains("nav-book-label")) {
          li.classList.remove("hidden");
          return;
        }
        var a = li.querySelector(":scope > a");
        var href = a ? a.getAttribute("href") || "" : "";
        var slugMatch = href.match(/([^\/]+)\.html/i);
        var slug = slugMatch ? slugMatch[1] : "";
        var titleText = (li.textContent || "").toLowerCase();
        var titleMatch = haystackHasTerms(titleText, terms);
        var contentMatch = !!(slug && matchSlugs[slug]);
        var show = titleMatch || contentMatch;
        li.classList.toggle("hidden", !show);

        li.querySelectorAll("li.nav-section").forEach(function (sub) {
          var st = (sub.textContent || "").toLowerCase();
          var parentHit = titleMatch || contentMatch;
          var subHit = haystackHasTerms(st, terms);
          sub.classList.toggle("hidden", !subHit && !parentHit);
          if (subHit) li.classList.remove("hidden");
        });
      });

      // Hide book labels with no visible children after filter
      var labels = navList.querySelectorAll(":scope > li.nav-book-label");
      labels.forEach(function (lab) {
        var next = lab.nextElementSibling;
        var any = false;
        while (next && !next.classList.contains("nav-book-label")) {
          if (!next.classList.contains("hidden")) {
            any = true;
            break;
          }
          next = next.nextElementSibling;
        }
        lab.classList.toggle("hidden", !any);
      });

      if (!resultsEl) return;
      if (!hits.length) {
        resultsEl.hidden = false;
        resultsEl.innerHTML =
          '<p class="search-empty">No pages match “' +
          escapeHtmlSearch(current) +
          '”.</p>';
        positionSearchResults();
        return;
      }
      var maxShow = 40;
      var html = [];
      html.push(
        '<p class="search-results-meta">' +
          hits.length +
          (hits.length === 1 ? " page" : " pages") +
          (hits.length > maxShow ? " (showing " + maxShow + ")" : "") +
          "</p>"
      );
      for (var h = 0; h < hits.length && h < maxShow; h++) {
        var hit = hits[h];
        var where =
          hit.book === "book1"
            ? "Book I"
            : hit.book === "book2"
              ? "Book II"
              : "";
        if (hit.titleHit && hit.textHit) where += (where ? " · " : "") + "title + text";
        else if (hit.titleHit) where += (where ? " · " : "") + "title";
        else where += (where ? " · " : "") + "in text";
        html.push(
          '<a class="search-hit" href="' +
            pageHrefFromSlug(hit.slug) +
            '">' +
            '<span class="search-hit-title">' +
            escapeHtmlSearch(hit.title) +
            "</span>" +
            (where
              ? '<span class="search-hit-where">' +
                escapeHtmlSearch(where) +
                "</span>"
              : "") +
            '<span class="search-hit-snippet">' +
            hit.snippet +
            "</span></a>"
        );
      }
      resultsEl.hidden = false;
      resultsEl.innerHTML = html.join("");
      positionSearchResults();
    });
  }

  if (filter && navList) {
    var searchTimer = null;
    filter.addEventListener("input", function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () {
        runSearch(filter.value.trim());
      }, 120);
    });
    window.addEventListener("resize", positionSearchResults);
    window.addEventListener(
      "scroll",
      function () {
        if (searchResults && !searchResults.hidden) positionSearchResults();
      },
      true
    );
    // Prefetch index so first search is snappy
    loadSearchIndex();
  }

  const toggle = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("sidebar");
  if (toggle && sidebar) {
    toggle.addEventListener("click", function () {
      sidebar.classList.toggle("open");
      document.body.classList.toggle("sidebar-open", sidebar.classList.contains("open"));
    });
    document.addEventListener("click", function (e) {
      if (!sidebar.classList.contains("open")) return;
      if (sidebar.contains(e.target) || toggle.contains(e.target)) return;
      sidebar.classList.remove("open");
      document.body.classList.remove("sidebar-open");
    });
  }

  /* Keep sidebar scroll position across in-wiki navigations */
  (function () {
    var KEY = "stonetop-wiki-sidebar-scroll";
    var el = document.getElementById("sidebar");
    if (!el) return;

    function saveScroll() {
      try {
        sessionStorage.setItem(KEY, String(el.scrollTop));
      } catch (e) {}
    }

    function restoreScroll() {
      try {
        var raw = sessionStorage.getItem(KEY);
        if (raw == null || raw === "") return;
        var y = parseInt(raw, 10);
        if (!isFinite(y) || y < 0) return;
        el.scrollTop = y;
        // Layout/fonts can shift — re-apply after paint
        requestAnimationFrame(function () {
          el.scrollTop = y;
        });
        setTimeout(function () {
          el.scrollTop = y;
        }, 50);
      } catch (e) {}
    }

    restoreScroll();

    var saveTimer = null;
    el.addEventListener(
      "scroll",
      function () {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(saveScroll, 80);
      },
      { passive: true }
    );

    // Save immediately when following a nav link (before unload)
    el.addEventListener("click", function (e) {
      var a = e.target.closest("a[href]");
      if (!a || !el.contains(a)) return;
      saveScroll();
    });

    window.addEventListener("pagehide", saveScroll);
    window.addEventListener("beforeunload", saveScroll);
  })();

  /* ---------- Persistent requirement checkboxes ---------- */
  (function () {
    var KEY = "stonetop-wiki-checks";

    function loadState() {
      try {
        return JSON.parse(localStorage.getItem(KEY) || "{}") || {};
      } catch (e) {
        return {};
      }
    }
    function saveState(state) {
      try {
        localStorage.setItem(KEY, JSON.stringify(state));
      } catch (e) {}
    }

    /** Page slug from a path or href, e.g. .../minor-ice-weaving.html → minor-ice-weaving */
    function slugFromPath(path) {
      var m = String(path || "")
        .replace(/\\/g, "/")
        .match(/([^\/#]+)\.html/i);
      return m ? m[1] : "";
    }

    function storageKey(pageSlug, checkId) {
      return pageSlug + "#" + checkId;
    }

    /** True if this check is marked under pageSlug (or any legacy path key). */
    function isChecked(state, pageSlug, checkId) {
      if (state[storageKey(pageSlug, checkId)]) return true;
      var suffix = "#" + checkId;
      for (var k in state) {
        if (
          Object.prototype.hasOwnProperty.call(state, k) &&
          state[k] &&
          k.endsWith(suffix)
        ) {
          return true;
        }
      }
      return false;
    }

    /**
     * Restore + bind wiki checkboxes under root for a logical page slug.
     * Safe to call on dynamically injected preview HTML.
     */
    function bindWikiChecks(root, pageSlug) {
      if (!root || !pageSlug) return;
      var state = loadState();
      root.querySelectorAll("input.wiki-check[data-check-id]").forEach(function (box) {
        var id = box.getAttribute("data-check-id");
        if (!id) return;
        // Already bound for this slug — still refresh checked state
        var full = storageKey(pageSlug, id);
        box.checked = isChecked(state, pageSlug, id);
        box.setAttribute("data-check-page", pageSlug);
        if (box.getAttribute("data-check-bound") === pageSlug) return;
        box.setAttribute("data-check-bound", pageSlug);
        box.addEventListener("change", function () {
          var st = loadState();
          if (box.checked) st[full] = true;
          else {
            delete st[full];
            // Clear legacy keys for same check id
            var suffix = "#" + id;
            Object.keys(st).forEach(function (k) {
              if (k.endsWith(suffix)) delete st[k];
            });
          }
          saveState(st);
          // Sync any other visible boxes for the same page+id (page + popup)
          document
            .querySelectorAll(
              'input.wiki-check[data-check-id="' +
                id.replace(/"/g, '\\"') +
                '"][data-check-page="' +
                pageSlug.replace(/"/g, '\\"') +
                '"]'
            )
            .forEach(function (other) {
              if (other !== box) other.checked = box.checked;
            });
        });
      });
    }

    window.bindWikiChecks = bindWikiChecks;

    // Bind checks on the current page at load
    var currentSlug = slugFromPath(location.pathname);
    if (currentSlug) bindWikiChecks(document, currentSlug);
  })();

  /* Scroll to #fragment targets inside the horizontal multi-column pane */
  function scrollToFragment() {
    var id = (location.hash || "").replace(/^#/, "");
    if (!id) return;
    var el = document.getElementById(id);
    if (!el) return;
    el.classList.add("target-highlight");
    var scroll = document.querySelector(".content-scroll");
    if (scroll) {
      // Place the element near the left of the scroll viewport
      var elRect = el.getBoundingClientRect();
      var scRect = scroll.getBoundingClientRect();
      var delta = elRect.left - scRect.left - 24;
      scroll.scrollLeft += delta;
      // Vertical alignment within the column pane
      var vDelta = elRect.top - scRect.top - 16;
      if (Math.abs(vDelta) > 8) {
        // columns are fixed height; element is already in-flow vertically per column
      }
    } else {
      el.scrollIntoView({ block: "start", inline: "nearest" });
    }
  }
  window.addEventListener("DOMContentLoaded", scrollToFragment);
  window.addEventListener("hashchange", scrollToFragment);
  // Also handle in-page fragment clicks
  document.addEventListener("click", function (e) {
    var a = e.target.closest("a[href*='#']");
    if (!a) return;
    var href = a.getAttribute("href") || "";
    // same-page fragment only needs delayed scroll after navigation
    if (href.charAt(0) === "#") {
      setTimeout(scrollToFragment, 0);
    }
  });

  /* Horizontal wheel scroll on the multi-column content area */
  document.querySelectorAll(".content-scroll").forEach(function (el) {
    el.addEventListener(
      "wheel",
      function (e) {
        // Index page uses vertical flow — leave native scroll alone
        if (el.scrollWidth <= el.clientWidth + 2) return;
        // Map vertical wheel (and trackpad) to horizontal scroll
        var dx = e.deltaX;
        var dy = e.deltaY;
        if (Math.abs(dy) >= Math.abs(dx) && dy !== 0) {
          e.preventDefault();
          el.scrollLeft += dy;
        } else if (dx !== 0) {
          // already horizontal gesture — keep default unless we need to force
          e.preventDefault();
          el.scrollLeft += dx;
        }
      },
      { passive: false }
    );
  });

  // Prefetch previews
  loadPreviews();
})();
"""


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


def main() -> None:
    print("Building Stonetop wiki (Book II)…")
    if not PDF_BOOK_II.exists():
        raise SystemExit(
            f"PDF not found: {PDF_BOOK_II}\n"
            "Place the Book II 1-up PDF in this folder (see README)."
        )
    if not BOOK_II.is_dir():
        print(
            f"  note: {BOOK_II.name}/ not found — building from PDF only."
        )

    if LEGACY_OUT.is_dir() and not OUT.exists():
        try:
            LEGACY_OUT.rename(OUT)
            print(f"  Renamed {LEGACY_OUT.name} → {OUT.name}")
        except OSError as e:
            print(f"  note: could not rename legacy folder ({e}); building fresh")

    for sub in ("pages", "css", "js", "images"):
        (OUT / sub).mkdir(parents=True, exist_ok=True)

    for old in (OUT / "pages").glob("*.html"):
        try:
            old.unlink()
        except OSError:
            pass

    (OUT / "css" / "wiki.css").write_text(CSS, encoding="utf-8")
    (OUT / "js" / "wiki.js").write_text(JS, encoding="utf-8")

    doc = fitz.open(str(PDF_BOOK_II))
    toc = load_toc(doc)
    md_files = (
        sorted(f.name for f in BOOK_II.glob("*.md")) if BOOK_II.is_dir() else []
    )
    articles = match_files_to_toc(
        toc,
        md_files,
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
    previews: dict[str, dict] = {}

    # Campaign maps + PDF map spreads (maps page only)
    maps_art = next((a for a in articles if a.get("kind") == "maps"), None)
    map_images: list[dict] = []
    if maps_art:
        print("Preparing maps…")
        map_images = prepare_map_images(doc, maps_art, OUT / "images")

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
                doc, art["start_page"], art["end_page"], art["title"]
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
        (OUT / "pages" / f"{slug}.html").write_text(page_html, encoding="utf-8")

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
    (OUT / "previews.json").write_text(previews_json, encoding="utf-8")

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
    (OUT / "page-map.json").write_text(page_map_json, encoding="utf-8")
    (OUT / "js" / "previews-data.js").write_text(
        "window.WIKI_PREVIEWS = "
        + previews_json
        + ";\nwindow.WIKI_PAGE_MAP = "
        + page_map_json
        + ";\n",
        encoding="utf-8",
    )
    search_json = json.dumps(search_docs, ensure_ascii=False, separators=(",", ":"))
    (OUT / "js" / "search-index.js").write_text(
        "window.WIKI_SEARCH_INDEX = " + search_json + ";\n",
        encoding="utf-8",
    )
    print(f"  Search index: {len(search_docs)} pages, {len(search_json)//1024} KB")
    write_index_custom(articles, previews, OUT / "index.html")

    (OUT / "README.md").write_text(
        """# Stonetop Wiki

Open `index.html` in a browser (or serve this folder with any static file server).

## Contents

- **Book II** — The Wider World (places, peoples, deities, arcana, …)

## Features

- Sidebar **Search wiki…** filters topics by title *and* full page text (with snippets)
- Page numbers are hyperlinks; dice expressions roll on click
- Hover previews (full stat blocks when deep-linked)
- Steading-improvement requirement checkboxes (saved in the browser)
- **Maps** page with campaign map sheets + PDF map spreads
- Horizontal multi-column layout on topic pages

## Rebuild

```bash
python build_book_ii_wiki.py
```
""",
        encoding="utf-8",
    )
    print(f"Done. Open {OUT / 'index.html'}")


if __name__ == "__main__":
    main()

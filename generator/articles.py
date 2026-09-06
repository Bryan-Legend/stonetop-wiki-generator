"""
What the books contain: the ``BOOKS`` table, the PDF outline → article list,
chapter merges and splits, arcana expansion and numbering, slug uniqueness.
"""

from __future__ import annotations

import re

try:
    import fitz  # PyMuPDF — only needed to extract from the PDFs
except ImportError:  # pragma: no cover
    fitz = None

from .extract import list_minor_arcana_cards
from .sites import SITES_BOOK_ID
from .text import slugify, titlecase_name


def discover_articles(doc: fitz.Document, book: dict) -> list[dict]:
    """The book's article list, in nav order, from its PDF outline.

    Chapters the outline files as siblings of their host are folded back in
    (``CHAPTER_MERGES``), the handout chapter is split into a hub and a page
    per sheet (``CHAPTER_SPLITS``), and the arcana appendices into a hub and
    a page per card. Slugs are unique within the book here; ``ensure_unique_slugs``
    settles collisions across books once every book is in hand.
    """
    toc = load_toc(doc)
    articles = articles_from_toc(
        toc,
        book=book["id"],
        slug_prefix=book["slug_prefix"],
        book_label=book["label"],
    )
    for art in articles:
        art["book_title"] = book.get("title") or book["label"]
    finalize_article_ranges(articles, doc.page_count)
    articles = merge_chapter_sections(articles, book["id"])
    # Playbooks & Inserts (Book I) becomes a hub + a page per handout.
    articles = split_chapter_articles(toc, articles, book["id"])
    # Arcana appendices (Book II) become a hub + one page per arcanum.
    return expand_arcana_articles(doc, articles)


# The books the wiki is built from, in nav order. Each 1-up PDF is optional:
# whichever ones are present in the input folder get built.
# Book II first (setting), Book I last (rules) so the sidebar ends with rules.
BOOKS: list[dict] = [
    {
        "id": "book1",
        "label": "Book I",
        "title": "Book I — Stonetop",
        "filename": "Book_I_-_Stonetop_(1-up)_-_2nd_printing.pdf",
        "slug_prefix": "",
    },
    {
        "id": "book2",
        "label": "Book II",
        "title": "Book II — The Wider World",
        "filename": (
            "Book_II_-_The_Wider_World_and_Other_Wonders_(1-up)_-_2nd_printing.pdf"
        ),
        "slug_prefix": "",
    },
]


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
        title = titlecase_name(title)
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


# Chapters the PDF outline files as siblings but the book treats as sections
# of the chapter above them. Book I's opening chapter is the case: it points
# the players at "the first few chapters of this book: Welcome to Stonetop
# (this chapter), Playing the Game …" — and Expectations / The Setting / Why
# Play? are set as 20pt section headings inside it, not as chapter openers.
# Keyed by book, then by host chapter title (titlecased, as ``articles_from_toc``
# leaves them).
CHAPTER_MERGES: dict[str, dict[str, list[str]]] = {
    "book1": {
        "Welcome to Stonetop": ["Expectations", "The Setting", "Why Play?"],
    },
}


def merge_chapter_sections(articles: list[dict], book: str) -> list[dict]:
    """Fold ``CHAPTER_MERGES`` sub-chapters into the chapter that hosts them.

    The host's page range grows to cover them, and their titles are kept on
    ``toc_labels`` so the sidebar still deep-links each one (the merged page
    has no printed chapter TOC of its own to harvest).
    """
    plan = CHAPTER_MERGES.get(book)
    if not plan:
        return articles

    by_title = {a["title"]: a for a in articles}
    absorbed: set[int] = set()
    for host_title, section_titles in plan.items():
        host = by_title.get(host_title)
        if not host:
            continue
        taken = [by_title[t] for t in section_titles if t in by_title]
        if not taken:
            continue
        host["end_page"] = taken[-1]["end_page"] or host["end_page"]
        host["toc_labels"] = [a["title"] for a in taken]
        absorbed.update(id(a) for a in taken)
        print(
            f"  Merged into {host['slug']}: "
            + ", ".join(a["slug"] for a in taken)
        )
    return [a for a in articles if id(a) not in absorbed]


# Chapters the book prints as one run of pages but which read as a shelf of
# separate documents rather than a single article: nine playbooks, nine
# inserts, and the steading playbook, each a handout in its own right and
# sixty pages of scrolling when they share a page. The PDF's own outline says
# where each begins.
CHAPTER_SPLITS: dict[str, set[str]] = {
    "book1": {"Playbooks & Inserts"},
}


def split_chapter_articles(
    toc: list[tuple[int, str, int]], articles: list[dict], book: str
) -> list[dict]:
    """Break the chapters in ``CHAPTER_SPLITS`` into a hub plus a page each.

    The chapter's level-2 entries are shelves, not documents: where one has
    entries beneath it those become the pages ("Playbooks" → the nine
    playbooks), and where it has none the entry is a page itself (the steading
    playbook). The hub keeps the chapter's opening pages and lists its parts.
    """
    names = CHAPTER_SPLITS.get(book)
    if not names:
        return articles

    out: list[dict] = []
    for art in articles:
        end = art.get("end_page") or art["start_page"]
        if art["title"] not in names or art.get("kind") != "article":
            out.append(art)
            continue

        inner = [
            (lvl, t.strip(), pg)
            for lvl, t, pg in toc
            if lvl >= 2 and art["start_page"] <= pg <= end
        ]
        shelves = [
            (titlecase_name(t), pg)
            for i, (lvl, t, pg) in enumerate(inner)
            if lvl == 2 and i + 1 < len(inner) and inner[i + 1][0] > lvl
        ]
        starts = [
            (titlecase_name(t), pg)
            for i, (lvl, t, pg) in enumerate(inner)
            if not (lvl == 2 and i + 1 < len(inner) and inner[i + 1][0] > lvl)
        ]
        if not starts:
            out.append(art)
            continue
        # The hub runs to the first handout, absorbing the chapter opener and
        # whatever shelf heading it opens under. A shelf that starts later
        # ("Inserts", after the playbooks) has its own introduction to carry,
        # so it becomes a page too rather than trailing the playbook above it.
        hub_end = starts[0][1] - 1
        starts.extend((t, pg) for t, pg in shelves if pg > hub_end)
        starts.sort(key=lambda pair: pair[1])

        children: list[dict] = []
        for i, (title, pg) in enumerate(starts):
            children.append(
                {
                    "title": title,
                    "slug": slugify(title),
                    "start_page": pg,
                    "end_page": (
                        starts[i + 1][1] - 1 if i + 1 < len(starts) else end
                    ),
                    "kind": "article",
                    "hub_slug": art["slug"],
                    "book": art.get("book"),
                    "book_label": art.get("book_label", ""),
                    "book_title": art.get("book_title", ""),
                }
            )

        hub = {
            **art,
            "end_page": starts[0][1] - 1,
            "children": [
                {"title": c["title"], "slug": c["slug"]} for c in children
            ],
        }
        out.append(hub)
        out.extend(children)
        print(
            f"  Split {art['title']}: {len(children)} pages "
            f"({starts[0][1]}-{end})"
        )
    return out


BOOK_SLUG_SUFFIX = {
    "book1": "-book-i",
    "book2": "-book-ii",
    SITES_BOOK_ID: "-site",
}


def ensure_unique_slugs(articles: list[dict]) -> None:
    """
    Make slugs unique across books (every page is a sibling of index.html).

    The first article to claim a slug keeps it; later ones get their book
    appended ("threats" → "threats-book-ii"). Hub/child cross-references are
    rewritten to match.
    """
    seen: set[str] = set()
    renames: dict[str, str] = {}
    for art in articles:
        slug = art["slug"]
        if slug not in seen:
            seen.add(slug)
            continue
        base = f"{slug}{BOOK_SLUG_SUFFIX.get(art.get('book') or '', '-2')}"
        new = base
        n = 2
        while new in seen:
            new = f"{base}-{n}"
            n += 1
        renames[slug] = new
        art["slug"] = new
        seen.add(new)
        print(f"  Slug collision: {slug} → {new}")

    if not renames:
        return
    for art in articles:
        hub = art.get("hub_slug")
        if hub in renames:
            art["hub_slug"] = renames[hub]
        for child in art.get("children") or []:
            if child.get("slug") in renames:
                child["slug"] = renames[child["slug"]]


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
                        "book_title": art.get("book_title", ""),
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
                        "book_title": art.get("book_title", ""),
                    }
                )
                p = card_end + 1

        # The appendix order is the order of the printed arcana deck, so a
        # card's position here is the number printed on it (minor 1-64,
        # major 1-18). Carry it so pages can be found in the physical stack.
        for i, child in enumerate(children, 1):
            child["number"] = i

        hub = {
            **art,
            "kind": "arcana-hub",
            "children": [
                {"title": c["title"], "slug": c["slug"], "number": c["number"]}
                for c in children
            ],
        }
        out.append(hub)
        out.extend(children)
        print(
            f"  Split {art['title']}: {len(children)} individual pages "
            f"({start}-{end})"
        )
    return out


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

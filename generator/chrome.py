"""
The wiki shell around an article: page template, sidebar nav, hub pages,
hand-authored page overrides (``pages/``), the home page, sitemap and robots.
"""

from __future__ import annotations

import datetime
import html
import json
import re
import shutil
from pathlib import Path

from . import REPO_ROOT
from .i18n import (
    UI_FALLBACK,
    alternates_for,
    body_source_sha,
    lang_switch_html,
    localize_body_links,
)
from .corpus import parse_text
from .sheet import render_sheet, sheet_excerpt
from .structure import T_english, extract_section_html_blocks
from .text import (
    _is_all_caps_label,
    html_to_search_text,
    normalize_section_key,
    slugify_id,
    titlecase_label,
)

# Project credit in the wiki sidebar footer.
GITHUB_PROJECT_URL = "https://github.com/Bryan-Legend/stonetop-wiki-generator"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
ISSUES_URL = GITHUB_PROJECT_URL.rstrip("/") + "/issues"


def sidebar_foot_html(ui: dict | None = None, lang_switch: str = "") -> str:
    """Sticky footer under the topic nav (attribution + GitHub project link).

    CC BY-SA 4.0 requires attribution and a license notice on the shared work,
    so the credit line ships on every page, not just the home page. A
    translation is an adaptation under that same license, so the credit is
    translated with the page rather than left in English beneath it.
    """
    ui = ui or UI_FALLBACK
    credit = ui.get("credit") or UI_FALLBACK["credit"]
    credit_html = credit.format(
        work="<em>Stonetop</em>",
        license=(
            f'<a href="{html.escape(LICENSE_URL)}" rel="license">'
            f"CC BY-SA 4.0</a>"
        ),
    )
    dice = html.escape(ui.get("dice_sound") or UI_FALLBACK["dice_sound"])
    return (
        f'<div class="sidebar-foot">'
        f'{lang_switch}'
        f'<span class="sidebar-credit">{credit_html}</span>'
        f'<a class="sidebar-github" href="{html.escape(GITHUB_PROJECT_URL)}">'
        f"GitHub</a>"
        f'<button type="button" class="sound-toggle" id="sound-toggle" '
        f'aria-pressed="true" title="{dice}">'
        f'<span class="sound-on" aria-hidden="true">\U0001f50a</span>'
        f'<span class="sound-off" aria-hidden="true">\U0001f507</span>'
        f'<span class="sound-label">{dice}</span></button>'
        f"</div>"
    )


BUILD_MANIFEST = ".build-manifest"
# How much of the wiki a language must hold before it is given its own
# previews/search data rather than reading the English pair at the root.
# Its own copy costs ~15 MB, nearly all of it the stat blocks the hover
# previews carry, so a language holding a handful of pages would be shipping
# the English file back with a few entries swapped. Below the bar it keeps
# reading the root's, which is right while coverage is partial — results lead
# to the pages that exist.
DATA_COVERAGE = 0.5
SITE_BASE_URL = "https://stonetop-wiki.github.io"


SITE_NAME = "Stonetop"
# The name in the sidebar and on the home page. The game stays "Stonetop";
# this is the site.
EDITION_NAME = "Stonetop Web Edition"

# Cloudflare Web Analytics beacon, injected into every generated page's <head>.
# Kept as a plain constant (not inlined) because the token payload contains
# braces that an f-string template would swallow.
ANALYTICS_HTML = (
    "  <!-- Cloudflare Web Analytics -->"
    "<script type='module' src='https://static.cloudflareinsights.com/beacon.min.js' "
    "data-cf-beacon='{\"token\": \"4536408101e64ba59733ed69779dd45c\"}'></script>"
    "<!-- End Cloudflare Web Analytics -->"
)


_DESC_PAGE_RE = re.compile(
    r"\s*\((?:see\s+)?(?:Book\s+[IVXLC]+,\s*)?pages?\s*"
    r"\d+(?:\s*[-–—]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–—]\s*\d+)?)*\)",
    re.I,
)
_DESC_BARE_PAGE_RE = re.compile(
    r"\s*\b(?:Book\s+[IVXLC]+,\s*)?pages?\s*"
    r"\d+(?:\s*[-–—]\s*\d+)?\b",
    re.I,
)


def strip_page_refs(text: str) -> str:
    """Drop "(page 200)" cruft from prose meant to be read without links.

    Hover previews keep the raw refs — wiki.js linkifies them into the target
    page's title. Index cards cannot nest an <a>, and <meta> takes no markup,
    so both read the stripped version instead.
    """
    out = _DESC_PAGE_RE.sub("", text or "")
    out = _DESC_BARE_PAGE_RE.sub("", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"\(\s*\)", "", out)
    return " ".join(out.split())


def meta_description(text: str, limit: int = 160) -> str:
    """One-line summary for <meta name="description">, trimmed on a word."""
    clean = strip_page_refs(text)
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rsplit(" ", 1)[0].rstrip(",.;:—-") + "…"


def social_meta_html(
    title: str,
    description: str,
    path: str,
    *,
    og_locale: str = "en_US",
    alternates: list[dict] | None = None,
) -> str:
    """Description + Open Graph/Twitter tags, canonical, and hreflang.

    Open Graph is what Discord, Reddit, and Slack read when someone pastes a
    link — the audience this wiki is shared with — so it ships on every page.

    ``alternates`` is the page's whole language cluster, English included, in
    ``{"hreflang", "path"}`` form. Google only trusts an hreflang set that is
    reciprocal and self-referential, so every page in the cluster lists every
    page in the cluster *and itself*, and the English page carries the same
    list its translations do. The canonical is always the page's own URL: a
    translation that canonicalises to the English page asks to be dropped from
    the index, which is the usual way a localized site ends up invisible.
    """
    desc = meta_description(description) or (
        "A searchable web edition of Stonetop, the Powered-by-the-Apocalypse "
        "game by Jeremy Strandberg."
    )
    # The home page is the site itself: no "Stonetop — Stonetop Web Edition".
    full = title if title == SITE_NAME else f"{title} — {EDITION_NAME}"
    base = SITE_BASE_URL.rstrip("/")
    url = base + "/" + path.lstrip("/")
    img = base + "/images/favicon.png"
    e = html.escape
    tags = [
        f'  <meta name="description" content="{e(desc)}">',
        # Nothing here is behind a paywall or a login, and the point of the
        # site is to be quoted: lift the default caps on how much of a page a
        # search engine or an AI summary may show.
        '  <meta name="robots" content="index, follow, max-snippet:-1,'
        ' max-image-preview:large, max-video-preview:-1">',
        f'  <link rel="canonical" href="{e(url)}">',
        f'  <meta property="og:site_name" content="{e(SITE_NAME)}">',
        f'  <meta property="og:title" content="{e(full)}">',
        f'  <meta property="og:description" content="{e(desc)}">',
        '  <meta property="og:type" content="article">',
        f'  <meta property="og:locale" content="{e(og_locale)}">',
        f'  <meta property="og:url" content="{e(url)}">',
        f'  <meta property="og:image" content="{e(img)}">',
        '  <meta name="twitter:card" content="summary">',
    ]
    for alt in alternates or []:
        # ``url`` overrides ``path`` where the address to index and the address
        # to link to differ: the home page is canonically the bare directory,
        # but a link has to say index.html or it breaks when the wiki is opened
        # off a disk.
        href = base + "/" + alt.get("url", alt["path"]).lstrip("/")
        tags.append(
            f'  <link rel="alternate" hreflang="{e(alt["hreflang"])}" '
            f'href="{e(href)}">'
        )
    if alternates:
        # x-default is what a reader who matches no listed language gets.
        # alternates[0] is the source language (English) by construction.
        first = alternates[0]
        default = base + "/" + first.get("url", first["path"]).lstrip("/")
        tags.append(
            f'  <link rel="alternate" hreflang="x-default" '
            f'href="{e(default)}">'
        )
    return "\n".join(tags)


def read_build_manifest(out: Path) -> list[str]:
    """Root-level files the previous build wrote (safe to delete and rewrite)."""
    try:
        text = (out / BUILD_MANIFEST).read_text(encoding="utf-8")
    except OSError:
        return []
    names = []
    for line in text.splitlines():
        line = line.strip()
        # Never let a stale manifest escape the wiki root.
        if line and not line.startswith("#") and "/" not in line and line != "..":
            names.append(line)
    return names


def write_build_manifest(out: Path, names: list[str]) -> None:
    header = [
        "# Files this build owns; the next build deletes and rewrites them.",
        "# Anything not listed here (CNAME, site-verification pages) is left alone.",
    ]
    (out / BUILD_MANIFEST).write_text(
        "\n".join(header + sorted(set(names))) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Structured data (JSON-LD)
#
# Two audiences, one block: a search engine deciding what a page is, and a
# crawler feeding a language model that will paraphrase it. Both want the
# same facts stated outright — this is the books' text, Jeremy Strandberg
# wrote it, it is CC BY-SA 4.0, it belongs to Book I or Book II of a tabletop
# RPG, and here is where it sits in the site. Saying so in the markup is the
# difference between being quoted with attribution and being quoted without.
# ---------------------------------------------------------------------------

GAME_AUTHOR = {"@type": "Person", "name": "Jeremy Strandberg"}
GAME_PUBLISHER = {
    "@type": "Organization",
    "name": "Lampblack & Brimstone",
    "url": "https://lampblackandbrimstone.com/",
}
GAME_DESCRIPTION = (
    "Stonetop is a hearth fantasy tabletop roleplaying game (RPG) by Jeremy "
    "Strandberg, Powered by the Apocalypse, in which 3-5 people play the "
    "heroes of one small, isolated village in an iron age that never was."
)
BOOK_NAMES = {
    "book1": "Stonetop (Book I)",
    "book2": "The Wider World and Other Wonders (Book II)",
}


def _jsonld_script(graph: list[dict]) -> str:
    """One ``<script type="application/ld+json">`` holding ``graph``."""
    doc = {"@context": "https://schema.org", "@graph": graph}
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    # Nothing may close the script element early; "<" never needs to be
    # literal inside JSON, so escape it whole rather than hunting for "</".
    text = text.replace("<", "\u003c")
    return (
        '  <script type="application/ld+json">' + text + "</script>"
    )


def _game_entity(base: str) -> dict:
    return {
        "@type": "Game",
        "@id": f"{base}/#game",
        "name": "Stonetop",
        "alternateName": "Stonetop RPG",
        "description": GAME_DESCRIPTION,
        "genre": ["Tabletop role-playing game", "Fantasy"],
        "author": GAME_AUTHOR,
        "publisher": GAME_PUBLISHER,
        "numberOfPlayers": {
            "@type": "QuantitativeValue",
            "minValue": 3,
            "maxValue": 5,
        },
    }


def _book_entity(book_id: str, base: str) -> dict:
    return {
        "@type": "Book",
        "@id": f"{base}/#{book_id}",
        "name": BOOK_NAMES.get(book_id, "Stonetop"),
        "author": GAME_AUTHOR,
        "publisher": GAME_PUBLISHER,
        "inLanguage": "en",
        "about": {"@id": f"{base}/#game"},
    }


def _website_entity(base: str) -> dict:
    return {
        "@type": "WebSite",
        "@id": f"{base}/#website",
        "name": EDITION_NAME,
        "url": f"{base}/",
        "description": (
            "A free, searchable web edition of both Stonetop rulebooks: "
            "moves, playbooks, gear, threats, places and arcana."
        ),
        "inLanguage": "en",
        "about": {"@id": f"{base}/#game"},
        "license": LICENSE_URL,
        "isAccessibleForFree": True,
        # The sidebar search reads ?q= on load (js/wiki.js), so this template
        # is a working address, not a claim.
        "potentialAction": {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": base + "/?q={search_term_string}",
            },
            "query-input": "required name=search_term_string",
        },
    }


def _breadcrumbs(trail: list[tuple[str, str]]) -> dict:
    return {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": i,
                "name": name,
                "item": item,
            }
            for i, (name, item) in enumerate(trail, 1)
        ],
    }


def page_structured_data(
    art: dict | None,
    *,
    title: str,
    description: str,
    url: str,
    lang: str,
    home_url: str,
    hub: tuple[str, str] | None = None,
    is_home: bool = False,
) -> str:
    """The JSON-LD for one page, in whatever language it is written in.

    ``art`` is the article this page renders (``None`` on a home page);
    ``hub`` is the (title, href) of the page's parent, where it has one — a
    playbook's chapter, an arcanum's index — written as the page itself
    writes it, so a language that has no translated hub points at the
    English one rather than at a 404.

    Every page carries the site, game and book entities by reference; the
    home page is where they are defined.
    """
    base = SITE_BASE_URL.rstrip("/")
    graph: list[dict] = []
    if is_home:
        page = {
            "@type": "CollectionPage",
            "@id": url + "#page",
            "url": url,
            "name": title,
            "description": description,
            "inLanguage": lang,
            "isPartOf": {"@id": f"{base}/#website"},
            "about": {"@id": f"{base}/#game"},
            "license": LICENSE_URL,
            "isAccessibleForFree": True,
        }
        graph += [
            _website_entity(base),
            _game_entity(base),
            _book_entity("book1", base),
            _book_entity("book2", base),
            page,
        ]
        return _jsonld_script(graph)

    if art is None:
        return ""

    book_id = art.get("book") or "book2"
    article = {
        "@type": "Article",
        "@id": url + "#article",
        "url": url,
        "mainEntityOfPage": url,
        "headline": title,
        "name": title,
        "inLanguage": lang,
        "author": GAME_AUTHOR,
        "publisher": GAME_PUBLISHER,
        "license": LICENSE_URL,
        "isAccessibleForFree": True,
        "about": {"@id": f"{base}/#game"},
        "isPartOf": [
            {"@id": f"{base}/#{book_id}"},
            {"@id": f"{base}/#website"},
        ],
    }
    if art.get("kind") == "bestiary":
        # The wiki's own compilation, not a chapter of either book: it is
        # made from both, and part of neither.
        article["isPartOf"] = {"@id": f"{base}/#website"}
        article["isBasedOn"] = [
            {"@id": f"{base}/#book1"},
            {"@id": f"{base}/#book2"},
        ]
    if description:
        article["description"] = description
    if art.get("start_page"):
        # Which printed pages this one is made of, so a citation can name them.
        article["pagination"] = (
            f'{art["start_page"]}-{art["end_page"]}'
            if art.get("end_page") and art["end_page"] != art["start_page"]
            else str(art["start_page"])
        )
    graph.append(article)

    trail = [("Stonetop", home_url)]
    if hub:
        trail.append(hub)
    trail.append((title, url))
    # A breadcrumb item has to be an address, and a relative one resolves
    # against the page it sits on, so make every item absolute off this
    # page's own URL.
    graph.append(
        _breadcrumbs([(name, _abs(url, item)) for name, item in trail])
    )
    return _jsonld_script(graph)


def _abs(page_url: str, href: str) -> str:
    """``href`` as written on the page at ``page_url``, made absolute."""
    from urllib.parse import urljoin

    return urljoin(page_url, href)


def write_sitemap(
    out: Path,
    articles: list[dict],
    *,
    base_url: str,
    extra: list[str] | None = None,
) -> None:
    """sitemap.xml covering every wiki page and translation."""
    base = base_url.rstrip("/")
    locs = [base + "/"]
    for art in articles:
        locs.append(base + "/" + art["slug"] + ".html")
    for href in extra or []:
        locs.append(base + "/" + href.lstrip("/"))
    locs = list(dict.fromkeys(locs))
    today = datetime.date.today().isoformat()
    rows = [
        "  <url><loc>" + html.escape(u) + "</loc>"
        "<lastmod>" + today + "</lastmod></url>"
        for u in locs
    ]
    doc = (
        ['<?xml version="1.0" encoding="UTF-8"?>']
        + ['<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        + rows
        + ["</urlset>", ""]
    )
    (out / "sitemap.xml").write_text("\n".join(doc), encoding="utf-8")
    print("  Sitemap: " + str(len(locs)) + " URLs")


LLMS_INTRO = (
    "Stonetop is a hearth fantasy tabletop roleplaying game (RPG) by Jeremy "
    "Strandberg, Powered by the Apocalypse, in which 3-5 people play the "
    "heroes of one small, isolated village in an iron age that never was. "
    "This site is a free, complete web edition of the game's two rulebooks: "
    "every move, playbook, monster, danger, place and arcanum, in English "
    "and twenty other languages."
)

LLMS_NOTES = [
    "The books' text is by Jeremy Strandberg and is released under CC BY-SA "
    "4.0 (https://creativecommons.org/licenses/by-sa/4.0/) — quote it freely, "
    "with attribution, under the same licence.",
    "The artwork is © Lucie Arnoux and is NOT published here: no page carries "
    "an illustration, and the Maps page is a text stub listing what the "
    "book's two map spreads label.",
    "Every page is static HTML and needs no JavaScript to read. Stat blocks, "
    "moves and tables are ordinary markup.",
    "A translation is the same slug under a language directory: "
    "/de/marshedge.html, /ja/marshedge.html. Slugs and section ids stay "
    "English in every language.",
    "Any page accepts ?q=<terms> and opens with the site search run over the "
    "full text of all pages.",
    "Book I holds the rules; Book II is the setting guide. A page's printed "
    "page range is given in its structured data (JSON-LD) as `pagination`.",
]


def _llms_line(base: str, art: dict, previews: dict) -> str:
    """One entry: the page's title, address, and what it covers."""
    slug = art["slug"]
    pv = previews.get(slug) or {}
    title = display_title(art)
    excerpt = strip_page_refs((pv.get("excerpt") or "").replace("\n", " "))
    excerpt = re.sub(r"\s+", " ", excerpt).strip()
    if len(excerpt) > 200:
        excerpt = excerpt[:199].rsplit(" ", 1)[0].rstrip(",.;:—-") + "…"
    line = f"- [{title}]({base}/{slug}.html)"
    return f"{line}: {excerpt}" if excerpt else line


def write_llms_txt(
    out: Path,
    articles: list[dict],
    previews: dict,
    *,
    base_url: str,
    languages: list[str] | None = None,
) -> None:
    """``llms.txt`` — the site, laid out for a language model.

    The llmstxt.org convention: one markdown file at the root that says what
    the site is and lists every page with a line about it, so a model reading
    it once knows what is here and can fetch the page it actually needs
    instead of guessing at URLs. No crawler is obliged to read it; it costs
    one file, and it is the only place the site states its own terms (the
    text is CC BY-SA, the art is not ours to give) in a form a model is
    likely to keep.
    """
    base = base_url.rstrip("/")
    lines = [
        "# Stonetop",
        "",
        f"> {LLMS_INTRO}",
        "",
    ]
    lines += [f"- {n}" for n in LLMS_NOTES]
    if languages:
        lines.append(
            "- Languages published: en, " + ", ".join(sorted(languages)) + "."
        )
    lines.append("")

    groups: list[tuple[str, list[dict]]] = []
    for art in articles:
        if art.get("kind") == "arcana":
            continue
        label = art.get("book_title") or art.get("book_label") or "Pages"
        if not groups or groups[-1][0] != label:
            groups.append((label, []))
        groups[-1][1].append(art)
    for label, arts in groups:
        lines.append(f"## {label}")
        lines.append("")
        lines += [_llms_line(base, a, previews) for a in arts]
        lines.append("")

    arcana = [a for a in articles if a.get("kind") == "arcana"]
    for kind, label in (("minor", "Minor Arcana"), ("major", "Major Arcana")):
        cards = [a for a in arcana if a.get("arcana_type") == kind]
        if not cards:
            continue
        # Minor and major are numbered from 1 apiece, so they are named
        # apart: "12" is a different card in each deck.
        lines += [
            f"## {label}",
            "",
            f"One page per card, numbered as the printed {label.lower()} "
            "cards are.",
            "",
        ]
        lines += [_llms_line(base, a, previews) for a in cards]
        lines.append("")

    lines += [
        "## Optional",
        "",
        f"- [Sitemap]({base}/sitemap.xml): every page, English and translated.",
        f"- [Source and issues]({GITHUB_PROJECT_URL}): the generator that "
        "builds this site from the books' PDFs.",
        "",
    ]
    (out / "llms.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"  llms.txt: {sum(len(a) for _l, a in groups) + len(arcana)} pages")


def write_robots(out: Path, *, base_url: str) -> None:
    lines = [
        "User-agent: *",
        "Allow: /",
        "Sitemap: " + base_url.rstrip("/") + "/sitemap.xml",
        "",
    ]
    (out / "robots.txt").write_text("\n".join(lines), encoding="utf-8")


PAGES_DIRNAME = "pages"


def page_override(slug: str) -> dict | None:
    """The hand-authored body for ``slug``, if there is one.

    ``pages/<slug>.txt`` is a sheet in the corpus format (``generator/sheet.py``)
    and comes back as ``{"kind": "sheet", "lines", "pages", "text"}``;
    ``pages/<slug>.html`` is a body written as HTML, ``{"kind": "html",
    "html"}``. Either replaces what the extractor makes of the slug's book
    pages; the shell, sidebar entry, search entry and hover preview are still
    built around it. A sheet marks itself with ``data-sheet`` on its root so
    it is treated as a handout."""
    txt = REPO_ROOT / PAGES_DIRNAME / f"{slug}.txt"
    if txt.is_file():
        text = txt.read_text(encoding="utf-8")
        lines, pages = parse_text(text, str(txt))
        return {"kind": "sheet", "lines": lines, "pages": pages, "text": text}
    path = REPO_ROOT / PAGES_DIRNAME / f"{slug}.html"
    if not path.is_file():
        return None
    return {"kind": "html", "html": path.read_text(encoding="utf-8").strip()}


_EXTRACTED_MARK_RE = re.compile(
    r'<!--\s*EXTRACTED\s+from="([^"]+)"(?:\s+to="([^"]+)")?'
    r'(?:\s+blocks="([^"]+)")?\s*-->'
)

# The opening line of an improvement as the sheet extraction sets it: a
# one-item check list whose label starts with the name in shouted bold, the
# blurb following on the same line.
_IMPROVEMENT_HEAD_RE = re.compile(
    r'<ul class="check-list" data-check-list="[^"]*"><li class="check-item">'
    r'<label for="[^"]*">(<input type="checkbox" class="wiki-check" id="[^"]*"'
    r' data-check-id="[^"]*">) <span><strong>([^<]+)</strong>\s*(.*?)</span>'
    r"</label></li></ul>\n?",
    re.S,
)
# The paragraph that opens a requirement list ("Requires 1 of the
# following:", "And then:"). Found by position — the paragraph right before a
# check list — so a translation is classified the same way as the English.
_LEAD_IN_RE = re.compile(r"<p>(.*?)</p>\n(?=<ul class=\"check-list\")")


def _lead_in(m: re.Match) -> str:
    inner = re.sub(r"</?strong>", "", m.group(1))
    return f'<p class="si-requires">{inner}</p>\n'


def _shouted(text: str) -> bool:
    """A label the book sets in full caps — or in a script with no case at
    all, which is what a translation of one comes back as."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    cased = [c for c in letters if c.upper() != c.lower()]
    if not cased:
        return len(text) <= 55
    return _is_all_caps_label(text)


def block_improvements(fragment: str) -> str:
    """Wrap each steading improvement of the sheet's extraction in a block.

    The steading playbook's improvement pages come out of the extractor as a
    run of check lists and paragraphs: the improvement's name is the first
    check box, in shouted bold, its blurb beside it, and the book rules some
    of them off with a hairline. The Homefront chapter and the Book II
    steadings set the same material as ``div.steading-improvement`` cards, so
    this makes the sheet match — one card per improvement, opened by an
    ``h3.si-title`` carrying the improvement's own check box (its check id is
    kept, so a box the table has ticked stays ticked), the blurb under it and
    the ``Requires …`` lead-ins marked. The card carries the id, which is
    what the section-link ``§`` copies and the hover preview captures. The
    hairlines go: the card is the rule now."""
    fragment = re.sub(r"\n?<hr>\n?", "\n", fragment)
    heads = [
        m for m in _IMPROVEMENT_HEAD_RE.finditer(fragment)
        if _shouted(m.group(2))
    ]
    if not heads:
        return fragment
    out: list[str] = [fragment[: heads[0].start()]]
    for k, m in enumerate(heads):
        end = heads[k + 1].start() if k + 1 < len(heads) else len(fragment)
        body = fragment[m.end():end]
        # A section heading ends the card, not the improvement before it.
        cut = re.search(r"<h[12]\b", body)
        tail = ""
        if cut:
            body, tail = body[: cut.start()], body[cut.start():]
        # The name is read back out of rendered HTML, so it is unescaped
        # first (an apostrophe arrives as an entity) and escaped once on the
        # way out. The id is the English improvement's, in every language.
        raw_name = html.unescape(m.group(2).strip())
        title = titlecase_label(raw_name)
        card_id = slugify_id(titlecase_label(T_english(raw_name)))
        box = m.group(1).replace(
            ">", f' aria-label="{html.escape(title)}: completed">', 1
        )
        blurb = m.group(3).strip()
        body = _LEAD_IN_RE.sub(_lead_in, body)
        card = [
            f'<div class="steading-improvement" id="{card_id}">',
            f'<h3 class="si-title">{box} {html.escape(title)}</h3>',
        ]
        if blurb:
            card.append(f'<p class="si-blurb">{blurb}</p>')
        card.append(body.strip())
        card.append("</div>\n")
        out.append("\n".join(card))
        out.append(tail)
    return "".join(out)


def extracted_slice(extracted: str, start: str, stop: str = "", blocks: str = "") -> str:
    """The extracted body from the heading with id ``start`` to its end — or,
    with ``stop``, up to (not including) that heading. ``blocks="improvements"``
    passes the slice through :func:`block_improvements` first."""
    cut = re.search(rf'<h[23]\s+id="{re.escape(start)}"', extracted)
    if not cut:
        return ""
    end = len(extracted)
    if stop:
        m = re.search(rf'<h[23]\s+id="{re.escape(stop)}"', extracted[cut.start():])
        if m:
            end = cut.start() + m.start()
    piece = extracted[cut.start():end]
    if blocks == "improvements":
        piece = block_improvements(piece)
    return piece


def apply_override(
    ov: dict,
    extracted: str,
    *,
    slug: str = "",
    link_fn=None,
    lines: list[str] | None = None,
) -> str:
    """Merge a hand-authored body with the extraction it replaces.

    A sheet (``pages/<slug>.txt``) is rendered by ``render_sheet``; its
    ``EXTRACT from [to blocks]`` line splices in the extracted body from the
    heading with that id (up to, not including, the second). ``lines`` are
    the sheet's lines to show when they are not the English ones — a
    translation — with ids still made from the English.

    An HTML body may carry ``<!--EXTRACTED from="content"-->`` for the same
    splice. The steading playbook is a sheet on its first pages and twelve
    pages of ordinary text after — the sheet is written by hand, the
    improvements keep their extraction, and with it the check-list ids the
    table has already ticked."""
    if ov["kind"] == "sheet":
        if link_fn is None:
            raise ValueError("a sheet needs link_fn")
        return render_sheet(
            lines if lines is not None else ov["lines"],
            slug,
            link_fn,
            extract_fn=lambda a, b, c: extracted_slice(extracted, a, b, c),
            id_lines=ov["lines"] if lines is not None else None,
        )

    def repl(m: re.Match) -> str:
        return extracted_slice(extracted, m.group(1), m.group(2) or "", m.group(3) or "")
    return _EXTRACTED_MARK_RE.sub(repl, ov["html"])


def override_sections(body: str) -> list[dict]:
    """The h2/h3 anchors of a hand-authored body, in the shape structure_html
    records — so the sidebar's deep links and the section index see them."""
    out: list[dict] = []
    # A heading with an id, or an improvement card: the card carries the id
    # and its h3.si-title the name (a card whose title is a blank to fill in
    # has no name and is not a section).
    pat = (
        r'<h([23])\s+id="([^"]+)"[^>]*>(.*?)</h\1>'
        r'|<div class="steading-improvement[^"]*" id="([^"]+)">\s*'
        r'<h3 class="si-title">(.*?)</h3>'
    )
    for m in re.finditer(pat, body, re.S):
        sid = m.group(2) or m.group(4)
        name = html.unescape(re.sub(r"<[^>]+>", "", m.group(3) or m.group(5))).strip()
        if not name:
            continue
        out.append(
            {
                "id": sid,
                "name": name,
                "norm": normalize_section_key(name),
                "caps": "",
            }
        )
    return out


def override_excerpt(ov: dict) -> str:
    """The first real paragraph of a hand-authored body."""
    if ov["kind"] == "sheet":
        return sheet_excerpt(ov["lines"])
    body = ov["html"]
    for m in re.finditer(r"<p(?:\s[^>]*)?>(.*?)</p>", body, re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if len(text) >= 40:
            return text if len(text) <= 320 else text[:319].rsplit(" ", 1)[0] + "…"
    return ""


def is_sheet_body(body: str) -> bool:
    """A character sheet or a follower sheet is a handout, not an article."""
    return 'class="pb-stats"' in body or "data-sheet" in body


def chapter_parts_html(art: dict) -> str:
    """Index of a split chapter's pages, appended to the hub's own opening."""
    kids = art.get("children") or []
    if not kids:
        return ""
    items = "".join(
        f'<li><a class="wiki-link" href="{html.escape(c["slug"])}.html" '
        f'data-slug="{html.escape(c["slug"])}">{html.escape(c["title"])}</a></li>'
        for c in kids
    )
    return (
        f'<div class="arcana-index"><h2>In this chapter</h2>'
        f"<ul>{items}</ul></div>"
    )


ARCANA_HUB_FALLBACK = {
    "title_minor": "Appendix C: Minor Arcana",
    "title_major": "Appendix D: Major Arcana",
    "lede_minor": (
        "Individual minor arcana from Book II. Each entry is its own page."
    ),
    "lede_major": (
        "Individual major arcana from Book II. Each entry is its own page."
    ),
    "all_minor": "All Minor Arcana",
    "all_major": "All Major Arcana",
}


def arcana_hub_strings(art: dict, ui: dict | None = None) -> dict:
    """The hub's own words, in one language.

    ``i18n/ui/<code>.json`` → ``arcana_hub``; anything missing falls back to
    English, so a language that has arcana translated but no hub strings yet
    still gets a page that leads to them.
    """
    which = "minor" if "minor" in (art.get("title") or "").lower() else "major"
    block = ((ui or {}).get("arcana_hub") or {})
    got = {**ARCANA_HUB_FALLBACK, **block}
    return {
        "title": got[f"title_{which}"],
        "lede": got[f"lede_{which}"],
        "all": got[f"all_{which}"],
    }


def arcana_hub_html(
    art: dict,
    *,
    ui: dict | None = None,
    translated: set[str] | None = None,
    page_titles: dict[str, str] | None = None,
    rel_prefix: str = "",
    english_only: str = "",
) -> str:
    """Index body for Minor/Major Arcana hub pages.

    In a language directory a card that is translated is linked beside this
    page and carries its translated name; one that is not links back up to the
    English page and is marked ``EN``, exactly as the sidebar does — the point
    of the hub is that every card is reachable, not that every card is done.
    """
    kids = art.get("children") or []
    words = arcana_hub_strings(art, ui)
    items = []
    for c in kids:
        slug = c["slug"]
        done = translated is None or slug in translated
        title = (page_titles or {}).get(slug) or c["title"]
        href = f"{slug}.html" if done else f"{rel_prefix}{slug}.html"
        mark = ""
        if not done:
            title = c["title"]
            mark = ' hreflang="en"'
            if english_only:
                mark = f' title="{html.escape(english_only)}"' + mark
        cls = "wiki-link" if done else "wiki-link nav-en"
        items.append(
            f'<li><a class="{cls}" href="{html.escape(href)}"'
            f'{mark} data-slug="{html.escape(slug)}">'
            f"{html.escape(title)}</a></li>"
        )
    return (
        f"<p>{html.escape(words['lede'])}</p>"
        f'<div class="arcana-index"><h2>{html.escape(words["all"])}</h2>'
        f"<ol>{''.join(items)}</ol></div>"
    )


def display_title(art: dict) -> str:
    """Title as shown to the reader — arcana carry their printed card number."""
    title = art.get("title") or ""
    num = art.get("number")
    return f"{num}. {title}" if num else title


def nav_label(art: dict) -> str:
    """
    Sidebar label.

    Book II files its entries alphabetically ignoring a leading "The" ("The
    Great Wood" sits between Gordin's Delve and Green Lords), so the sidebar
    drops it and reads in the order the book does.
    """
    if art.get("number"):
        return display_title(art)
    title = art.get("title") or ""
    if art.get("book") == "book2" and title[:4].lower() == "the ":
        rest = title[4:]
        # "The village of Stonetop" → "Village of Stonetop"
        return rest[:1].upper() + rest[1:]
    return title


def build_nav_items(
    articles: list[dict],
    *,
    href_prefix: str = "",
    current_slug: str | None = None,
    section_navs: dict[str, list[dict]] | None = None,
    root_prefix: str = "",
    locale: dict | None = None,
    translated_slugs: set[str] | None = None,
) -> list[str]:
    """
    Sidebar <li> items for every article, with a label row per book.

    Book labels are only emitted when the wiki holds more than one book;
    wiki.js hides a label whose articles are all filtered out.
    """
    section_navs = section_navs or {}
    # In a language directory the sidebar is mostly English, because most
    # pages are: a translated entry links to its sibling in this directory,
    # everything else links back up to the English page and says so.
    lang_pages = (locale or {}).get("pages") or {}
    translated_slugs = translated_slugs or set(lang_pages)
    book_labels = ((locale or {}).get("ui") or {}).get("books") or {}
    english_only = ((locale or {}).get("ui") or {}).get("english_only") or ""
    multi_book = len({a.get("book") for a in articles if a.get("book")}) > 1
    items: list[str] = []
    last_book: str | None = None
    # A split chapter's handouts are listed under their hub, not beside it.
    parts_by_hub: dict[str, list[dict]] = {}
    for art in articles:
        if art.get("hub_slug") and art.get("kind") == "article":
            parts_by_hub.setdefault(art["hub_slug"], []).append(art)
    for art in articles:
        if art.get("hub_slug") and art.get("kind") == "article":
            continue
        book = art.get("book")
        if multi_book and book != last_book:
            label = book_labels.get(book) or art.get("book_label") or book or ""
            if label:
                items.append(
                    f'<li class="nav-book-label">{html.escape(label)}</li>'
                )
            last_book = book

        parts = parts_by_hub.get(art["slug"]) or []
        classes: list[str] = []
        if art.get("kind") == "arcana":
            classes.append("nav-arcana")
        elif art.get("kind") == "arcana-hub" or art.get("children"):
            classes.append("nav-hub")
        secs = section_navs.get(art["slug"]) or []
        if secs or parts:
            classes.append("has-sections")
        if current_slug is not None and art["slug"] == current_slug:
            classes.append("current")
        cls_attr = f' class="{" ".join(classes)}"' if classes else ""
        art_slug = html.escape(art["slug"])
        page_tr = lang_pages.get(art["slug"])
        # A page is in this language if it is translated *or* generated here
        # (the arcana indexes are in no locale's ``pages``, yet every language
        # directory carries them) — ``translated_slugs`` names both.
        in_lang = art["slug"] in translated_slugs
        if in_lang:
            # The sibling in this same language directory.
            href = f"{art_slug}.html"
            slug_attr = ""
        else:
            href = f"{href_prefix}{art_slug}.html"
            slug_attr = ""
        label = (page_tr or {}).get("nav_label") or ""
        if not label and locale and art.get("kind") == "arcana-hub":
            label = arcana_hub_strings(art, locale.get("ui"))["title"]
        if label and art.get("number"):
            label = f"{art['number']}. {label}"  # an arcanum's card number
        label = html.escape(label or nav_label(art))
        if locale and not in_lang:
            slug_attr += ' class="nav-en"'
            if english_only:
                slug_attr += f' title="{html.escape(english_only)}" hreflang="en"'
        link = f'<a href="{href}"{slug_attr}>{label}</a>'
        if parts:
            sub = []
            for part in parts:
                p_cls = (
                    "nav-section current"
                    if part["slug"] == current_slug
                    else "nav-section"
                )
                # A translated part links to its sibling in this language
                # directory; an untranslated one links back up to the English
                # page and says so, exactly as top-level entries do.
                part_tr = lang_pages.get(part["slug"])
                p_slug = html.escape(part["slug"])
                if part["slug"] in translated_slugs:
                    p_href = f"{p_slug}.html"
                    p_attr = ""
                else:
                    p_href = f"{href_prefix}{p_slug}.html"
                    p_attr = ""
                    if locale:
                        p_attr = ' class="nav-en"'
                        if english_only:
                            p_attr += (
                                f' title="{html.escape(english_only)}"'
                                ' hreflang="en"'
                            )
                p_label = html.escape(
                    (part_tr or {}).get("nav_label") or nav_label(part)
                )
                sub.append(
                    f'<li class="{p_cls}">'
                    f'<a href="{p_href}"{p_attr}>{p_label}</a></li>'
                )
            items.append(
                f"<li{cls_attr}>{link}"
                f'<ul class="nav-sections">{"".join(sub)}</ul></li>'
            )
            continue
        if secs:
            # Nested deep links into chapter sections (from first-page TOC).
            # Section ids stay English so deep links survive a language
            # switch; only the label the reader sees is translated.
            sec_names = (page_tr or {}).get("sections") or {}
            sub = []
            for sec in secs:
                sid = html.escape(sec["id"])
                sname = html.escape(sec_names.get(sec["id"]) or sec["name"])
                sub.append(
                    f'<li class="nav-section">'
                    f'<a href="{href}#{sid}">{sname}</a></li>'
                )
            items.append(
                f"<li{cls_attr}>{link}"
                f'<ul class="nav-sections">{"".join(sub)}</ul></li>'
            )
        else:
            items.append(f"<li{cls_attr}>{link}</li>")
    return items


PLAYBOOK_SLUGS = frozenset(
    {
        "the-blessed",
        "the-fox",
        "the-heavy",
        "the-judge",
        "the-lightbearer",
        "the-marshal",
        "the-ranger",
        "the-seeker",
        "the-would-be-hero",
    }
)


def ui_script_html(locale: dict | None) -> str:
    """``window.WIKI_UI`` — the strings ``js/wiki.js`` needs, for this page.

    The search box, the hover previews and the feedback form are built in the
    browser, and ``js/wiki.js`` is chrome the build never writes, so their
    words cannot be generated into the markup the way the sidebar's are. They
    ride on the page instead: an inline object, English defaults living in
    ``wiki.js`` itself. Inline rather than a per-language file because the
    wiki is opened off a disk as often as it is served, and because it is a
    few hundred bytes.
    """
    if not locale:
        return ""  # English is what wiki.js already says
    ui = locale.get("ui") or {}
    keys = ("books", "page_words", "english_only")
    data = {k: ui[k] for k in keys if ui.get(k)}
    if ui.get("js"):
        data["js"] = ui["js"]
    # The arcana Location box lists the nine playbooks: their titles in this
    # language, from the translated pages themselves, so the list reads as
    # the sidebar does. The value saved stays English (wiki.js).
    playbooks = {
        slug: page["title"]
        for slug, page in (locale.get("pages") or {}).items()
        if slug in PLAYBOOK_SLUGS and page.get("title")
    }
    if playbooks:
        data["playbooks"] = playbooks
    if not data:
        return ""
    # </script> inside a string would close this one; JSON has no other way
    # to produce that sequence, so escaping the slash is enough.
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return "  <script>window.WIKI_UI=" + blob + ";</script>\n"


def page_shell(
    title: str,
    slug: str,
    body_html: str,
    articles: list[dict],
    rel_prefix: str = "",
    section_navs: dict[str, list[dict]] | None = None,
    content_class: str = "content",
    description: str = "",
    *,
    locale: dict | None = None,
    alternates: list[dict] | None = None,
    translated_slugs: set[str] | None = None,
) -> str:
    """One page, in one language.

    ``locale`` is a language entry from ``i18n/langs.json`` (with its ``ui``
    strings); ``None`` is English at the wiki root. A localized page lives one
    directory down, so its ``rel_prefix`` walks back up to the shared css/js
    and to every page not yet translated.
    """
    ui = (locale or {}).get("ui") or UI_FALLBACK
    code = (locale or {}).get("code") or "en"
    path = f"{code}/{slug}.html" if locale else f"{slug}.html"
    meta_html = social_meta_html(
        title,
        description,
        path,
        og_locale=(locale or {}).get("og_locale") or "en_US",
        alternates=alternates,
    )
    nav_html = "\n".join(
        build_nav_items(
            articles,
            current_slug=slug,
            section_navs=section_navs,
            root_prefix=rel_prefix,
            href_prefix=rel_prefix,
            locale=locale,
            translated_slugs=translated_slugs,
        )
    )
    switch = lang_switch_html(
        alternates or [], code, ui, rel_prefix=rel_prefix
    )
    # Structured data: what this page is, whose text it is, and where it sits.
    base = SITE_BASE_URL.rstrip("/")
    page_url = f"{base}/{path}"
    home_url = f"{base}/{code}/" if locale else f"{base}/"
    art = next((a for a in articles if a["slug"] == slug), None)
    hub = None
    hub_slug = (art or {}).get("hub_slug")
    if hub_slug:
        parent = next((a for a in articles if a["slug"] == hub_slug), None)
        if parent:
            # The hub as this page links it: a language that has not
            # translated the hub points back up at the English one.
            local_hub = not locale or hub_slug in (translated_slugs or set())
            hub_href = (
                f"{hub_slug}.html" if local_hub else f"{rel_prefix}{hub_slug}.html"
            )
            hub_name = ((locale or {}).get("titles") or {}).get(
                hub_slug, parent["title"]
            )
            hub = (hub_name, hub_href)
    jsonld = page_structured_data(
        art,
        title=title,
        description=meta_description(description),
        url=page_url,
        lang=code,
        home_url=home_url,
        hub=hub,
        is_home=slug == "index",
    )
    e = html.escape
    dir_attr = f' dir="{e((locale or {}).get("dir") or "ltr")}"' if locale else ""
    # A language directory needs its own data files no more than it needs its
    # own copy of wiki.js: both sit at the wiki root and are reached through
    # rel_prefix, so search and hover previews keep working from a translated
    # page and lead back to the English pages that are not translated yet.
    root_attr = f' data-wiki-root="{e(rel_prefix)}"' if rel_prefix else ""
    ui_script = ui_script_html(locale)
    # Every language directory that has pages gets its own home page
    # (``write_localized_index``), so the wiki title leads there, not up to
    # the English one.
    home_href = "index.html" if locale else f"{rel_prefix}index.html"
    doc_title = title if title == SITE_NAME else f"{title} — {EDITION_NAME}"

    return f"""<!DOCTYPE html>
<html lang="{e(code)}"{dir_attr}>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(doc_title)}</title>
{meta_html}
  <link rel="icon" href="{rel_prefix}images/favicon.svg" type="image/svg+xml">
  <link rel="alternate icon" href="{rel_prefix}images/favicon.ico" sizes="16x16 32x32 48x48 64x64">
  <link rel="stylesheet" href="{rel_prefix}css/wiki.css">
{jsonld}
{ANALYTICS_HTML}
</head>
<body{root_attr}>
  <a class="skip-link" href="#main">{e(ui["skip_to_content"])}</a>
  <button type="button" class="sidebar-toggle" id="sidebar-toggle" aria-label="{e(ui["toggle_nav"])}">☰</button>
  <div class="layout">
    <aside class="sidebar" id="sidebar">
      <div class="sidebar-head">
        <a class="wiki-title" href="{home_href}">{e(EDITION_NAME)}</a>
        <input type="search" id="nav-filter" class="nav-filter" placeholder="{e(ui["search_placeholder"])}" autocomplete="off" aria-label="{e(ui["search_label"])}">
        <div id="search-results" class="search-results" hidden></div>
      </div>
      <nav class="toc" aria-label="{e(ui["nav_label"])}">
        <ul id="nav-list">
          {nav_html}
        </ul>
      </nav>
      {sidebar_foot_html(ui, switch)}
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
{ui_script}  <script src="{rel_prefix}js/wiki.js"></script>
</body>
</html>
"""


def write_localized_data(
    lang_dir: Path,
    code: str,
    previews: dict,
    search_docs: list[dict],
    page_maps: dict,
    local: dict[str, dict],
    titles: dict[str, str],
) -> None:
    """``<lang>/js/previews-data.js`` and ``<lang>/js/search-index.js``.

    Hover previews and search were reading the English data from the wiki
    root on every localized page — right while a language held a handful of
    pages, wrong once it holds all of them: a reader searching in Portuguese
    matched English text and hovered an English card. Each language gets its
    own pair, the English entry kept for any page it has not translated, so
    the index still leads somewhere for those.
    """
    pv = {}
    for slug, entry in previews.items():
        got = local.get(slug)
        if not got:
            pv[slug] = entry
            continue
        pv[slug] = {**entry, **got}
    docs = []
    for doc in search_docs:
        got = local.get(doc["slug"])
        if not got:
            docs.append(doc)
            continue
        docs.append(
            {
                **doc,
                "title": got.get("title") or doc["title"],
                "excerpt": (got.get("excerpt") or "")[:280],
                "text": got.get("text") or doc["text"],
                # This page is a sibling, not one directory up.
                "local": 1,
            }
        )
    # "see page 18" resolves against this map; its titles are what the reader
    # is shown, so they follow the language too.
    maps = {}
    for bid, page_map in page_maps.items():
        maps[bid] = {
            num: (
                {**hit, "title": titles[hit["slug"]]}
                if hit.get("slug") in titles
                else hit
            )
            for num, hit in page_map.items()
        }
    js_dir = lang_dir / "js"
    js_dir.mkdir(parents=True, exist_ok=True)
    nl = "\n"
    (js_dir / "previews-data.js").write_text(
        "window.WIKI_PREVIEWS = "
        + json.dumps(pv, ensure_ascii=False, indent=2)
        + ";" + nl + "window.WIKI_PAGE_MAP = "
        + json.dumps(maps, ensure_ascii=False, indent=2)
        + ";" + nl,
        encoding="utf-8",
    )
    (js_dir / "search-index.js").write_text(
        "window.WIKI_SEARCH_INDEX = "
        + json.dumps(docs, ensure_ascii=False, separators=(",", ":"))
        + ";" + nl,
        encoding="utf-8",
    )


def write_localized_pages(
    out: Path,
    articles: list[dict],
    section_navs: dict[str, list[dict]],
    english_bodies: dict[str, str],
    source: dict,
    targets: list[dict],
    only_pages: set[str] | None = None,
    *,
    previews: dict | None = None,
    search_docs: list[dict] | None = None,
    sections_by_slug: dict[str, list[dict]] | None = None,
    page_maps: dict | None = None,
) -> list[str]:
    """Write ``<out>/<lang>/<slug>.html`` for every translated page.

    Returns the hrefs written, for the sitemap. Each language directory is
    rewritten from scratch, so a translation file that is deleted takes its
    page with it — unless ``only_pages`` names the slugs to write, in which
    case the rest of the directory is left as it is.
    """
    written: list[str] = []
    for locale in targets:
        code = locale["code"]
        lang_dir = out / code
        if only_pages is None and lang_dir.exists():
            shutil.rmtree(lang_dir)
        lang_dir.mkdir(parents=True, exist_ok=True)
        # The arcana indexes are generated into every language directory
        # (write_localized_arcana_hubs), so the sidebar links to them beside
        # this page rather than marking them English-only.
        ui = locale.get("ui") or {}
        translated = set(locale["pages"]) | arcana_hub_slugs(articles)
        stale = []
        # What this language's own previews / search index are made of.
        # Whether it gets them at all is known before the loop, and building
        # the material for a language that will not use it is the expensive
        # part — re-extracting every section block on every page.
        local_data: dict[str, dict] = {}
        local_titles: dict[str, str] = {}
        covered = bool(previews) and len(translated) >= DATA_COVERAGE * len(
            previews
        )
        for slug, page in sorted(locale["pages"].items()):
            if only_pages is not None and slug not in only_pages:
                continue
            english = english_bodies.get(slug)
            if english is None:
                print(f"  i18n: {code}/{slug} has no English page — skipped")
                continue
            if page.get("source_sha256") and page["source_sha256"] != (
                body_source_sha(english)
            ):
                stale.append(slug)
            title = page.get("title") or slug
            body = localize_body_links(page["body_html"], translated)
            # The body is built for the wiki root; a language directory sits
            # one level down, so its icons and images need the way back up.
            body = body.replace('src="images/', 'src="../images/')
            # Mirror the English page: a sheet's h1 carries pb-title and its
            # <main> the playbook class, so localized sheets keep the sheet
            # styling and widgets.
            h1_cls = "page-title"
            if 'class="pb-stats"' in body:
                h1_cls += " pb-title"
            # An arcanum carries its name on the card's face, as in English.
            if 'class="arcana-card' not in body:
                body = (
                    f'<h1 class="{h1_cls}">{html.escape(title)}</h1>\n' + body
                )
            navs = dict(section_navs)
            html_out = page_shell(
                title,
                slug,
                body,
                articles,
                rel_prefix="../",
                section_navs=navs,
                content_class=(
                    "content playbook" if is_sheet_body(body) else "content"
                ),
                description=page.get("description") or "",
                locale=locale,
                alternates=alternates_for(slug, source, targets),
                translated_slugs=translated,
            )
            (lang_dir / f"{slug}.html").write_text(html_out, encoding="utf-8")
            written.append(f"{code}/{slug}.html")
            if covered:
                local_titles[slug] = title
                entry = {
                    "title": title,
                    "excerpt": page.get("description") or "",
                    "sections": extract_section_html_blocks(
                        body, (sections_by_slug or {}).get(slug, [])
                    ),
                    "text": html_to_search_text(body),
                }
                # An arcanum's hover popup is the card itself, and the card is
                # the whole body here, so the translated card is the preview.
                english = previews.get(slug) or {}
                if english.get("kind") == "arcana":
                    entry["html"] = body
                local_data[slug] = entry
        if covered and search_docs is not None and local_data:
            # The arcana indexes are written after this pass, but they are
            # pages of this language too — without them a reader searching in
            # it would be sent up to the English index instead.
            for art in articles:
                if art.get("kind") != "arcana-hub":
                    continue
                words = arcana_hub_strings(art, ui)
                hub_body = arcana_hub_html(
                    art,
                    ui=ui,
                    translated=set(locale["pages"]),
                    page_titles=local_titles,
                    rel_prefix="../",
                    english_only=ui.get("english_only") or "",
                )
                local_titles[art["slug"]] = words["title"]
                local_data[art["slug"]] = {
                    "title": words["title"],
                    "excerpt": words["lede"],
                    "sections": {},
                    "text": words["title"] + " " + html_to_search_text(hub_body),
                }
            write_localized_data(
                lang_dir,
                code,
                previews,
                search_docs,
                page_maps or {},
                local_data,
                local_titles,
            )
        if stale:
            print(
                f"  i18n: {code} stale against the English text: "
                + ", ".join(stale)
            )
    if targets:
        print(
            f"  i18n: {len(written)} pages across {len(targets)} languages "
            + ", ".join(t["code"] for t in targets)
        )
    return written


HOME_INTRO_HTML = (
    "<p><strong>Stonetop</strong> is a hearth fantasy tabletop roleplaying "
    "game (RPG) by Jeremy Strandberg, set in an iron age that never was. "
    "Three to five of you sit down together — one as GM, the rest playing "
    "the heroes of a single small, isolated village at the world's end. You "
    "have kin there, a trade, neighbors who knew you as a child, and the "
    "dangers you face are the ones that threaten home — so "
    "between adventures the game turns to the "
    '<a href="{homefront}">Homefront</a>, where the village has a '
    "playbook of its own and grows, suffers and changes alongside the "
    "characters. That is what sets this TTRPG apart from dungeon-crawling "
    "fantasy RPGs: the place you defend is the place you live.</p>"
    "<p>Under the hood it is a Powered by the Apocalypse RPG built on "
    "<em>Dungeon World</em>'s bones: roll 2d6 plus a stat — 10+ and it "
    "goes your way, 7-9 and it costs you something, 6- and the GM makes a "
    "move. This wiki is a free, searchable web edition of the TTRPG's two "
    "rulebooks: every move, playbook, monster, danger, steading, deity and "
    "arcanum in the game. If the game is new to you, start with "
    '<a href="{welcome}">Welcome to Stonetop</a>, then '
    '<a href="{playing}">Playing the Game</a> — or '
    "browse the topics below.</p>"
)


def home_intro_html(template: str, href) -> str:
    """The home page's "what is Stonetop" intro, with its three links resolved.

    ``href`` takes a slug and returns the path to it from the home page being
    written — a localized home links to the translated page where there is
    one and up to the English page where there is not.
    """
    return template.format(
        homefront=html.escape(href("homefront")),
        welcome=html.escape(href("welcome-to-stonetop")),
        playing=html.escape(href("playing-the-game")),
    )


HOME_FALLBACK = {
    "title": "Stonetop",
    "topics": "Topics",
    "intro_html": HOME_INTRO_HTML,
    "lede_html": (
        "A static, hyperlinked wiki for <em>Stonetop</em> {books}. "
        "Page numbers are links; dice expressions roll on click; hover a "
        "link for a preview (full stat blocks when deep-linked)."
    ),
    "remembers_html": (
        "<strong>It remembers.</strong> Tick a checkbox, answer a question, "
        "fill in a blank, assign your stats — the wiki keeps all of it."
    ),
    "defects_html": (
        "These pages are extracted from the books' PDFs automatically, so "
        "<strong>expect defects</strong>. If you spot one, "
        '<a href="{issues}">open an issue on GitHub</a>.'
    ),
    "license_html": (
        "The books' <strong>text</strong> is by Jeremy Strandberg under "
        '<a href="{license}" rel="license">CC BY-SA 4.0</a>.'
    ),
}


def arcana_hub_slugs(articles: list[dict]) -> set[str]:
    """The slugs of the generated arcana index pages."""
    return {a["slug"] for a in articles if a.get("kind") == "arcana-hub"}


def write_localized_arcana_hubs(
    out: Path,
    articles: list[dict],
    section_navs: dict[str, list[dict]],
    source: dict,
    targets: list[dict],
) -> list[str]:
    """Write ``<out>/<lang>/appendix-[cd]-*-arcana.html`` — the arcana indexes.

    A hub is *generated*, not translated, so it is in no language's ``pages``
    and ``write_localized_pages`` never sees it — which is how pt-BR came to
    have all 82 cards translated and no index in Portuguese to reach them
    from. Written for every language that has any pages, on the same rule as
    the home page, since an index whose entries are mostly English still
    leads a reader to the ones that are not.
    """
    written: list[str] = []
    live = [t for t in targets if t.get("pages")]
    hubs = [a for a in articles if a.get("kind") == "arcana-hub"]
    if not live or not hubs:
        return written
    hub_slugs = {a["slug"] for a in hubs}
    for locale in live:
        code = locale["code"]
        lang_dir = out / code
        lang_dir.mkdir(parents=True, exist_ok=True)
        ui = locale.get("ui") or {}
        translated = set(locale["pages"]) | hub_slugs
        titles = locale.get("titles") or {}
        page_titles = {
            slug: (page.get("title") or titles.get(slug) or "")
            for slug, page in locale["pages"].items()
        }
        for art in hubs:
            slug = art["slug"]
            words = arcana_hub_strings(art, ui)
            body = arcana_hub_html(
                art,
                ui=ui,
                translated=set(locale["pages"]),
                page_titles=page_titles,
                rel_prefix="../",
                english_only=ui.get("english_only") or "",
            )
            head = f'<h1 class="page-title">{html.escape(words["title"])}</h1>'
            body = head + "\n" + body
            kids = len(art.get("children") or [])
            html_out = page_shell(
                words["title"],
                slug,
                body,
                articles,
                rel_prefix="../",
                section_navs=section_navs,
                description=words["lede"] or f"{kids} arcana.",
                locale=locale,
                alternates=alternates_for(slug, source, targets, have=live),
                translated_slugs=translated,
            )
            (lang_dir / f"{slug}.html").write_text(html_out, encoding="utf-8")
            written.append(f"{code}/{slug}.html")
    if written:
        print(f"  i18n: {len(written)} arcana indexes")
    return written


def home_alternates(source: dict, targets: list[dict]) -> list[dict]:
    """The home page's language cluster — English first, then every language
    that has a home page.

    The English home page is built by hand rather than through ``page_shell``
    (it is a card grid, not an article), which is how it went out with no
    hreflang and no language switcher while all twenty translations linked
    back up to it. One cluster, built here, is what keeps the set reciprocal.
    """
    live = [t for t in targets if t.get("pages")]
    if not live:
        return []
    return [
        {
            "hreflang": source.get("code") or "en",
            "path": "index.html",
            "url": "",  # canonically the bare directory
            "code": source.get("code") or "en",
            "endonym": source.get("endonym") or "English",
        }
    ] + [
        {
            "hreflang": t["code"],
            "path": f"{t['code']}/index.html",
            "code": t["code"],
            "endonym": t.get("endonym") or t["code"],
        }
        for t in live
    ]


def write_localized_index(
    out: Path,
    articles: list[dict],
    previews: dict,
    source: dict,
    targets: list[dict],
) -> list[str]:
    """Write ``<out>/<lang>/index.html`` — the home page, per language.

    A card carries the page's own title and description in this language
    where the page is translated, and the English ones where it is not; an
    untranslated card links back up to the English page, exactly as the
    sidebar does. The fixed prose comes from ``i18n/ui/<code>.json`` →
    ``home``, falling back to English.
    """
    written: list[str] = []
    live = [t for t in targets if t.get("pages")]
    if not live:
        return written
    home_alts = home_alternates(source, targets)
    for locale in live:
        code = locale["code"]
        lang_dir = out / code
        lang_dir.mkdir(parents=True, exist_ok=True)
        ui = locale.get("ui") or {}
        home = {**HOME_FALLBACK, **((ui.get("home") or {}))}
        book_labels = (ui.get("books") or {})
        titles = locale.get("titles") or {}
        translated = set(locale["pages"]) | arcana_hub_slugs(articles)

        books_present: list[tuple] = []
        for art in articles:
            key = (
                art.get("book"),
                art.get("book_title")
                or art.get("book_label")
                or art.get("book")
                or "",
            )
            if key not in books_present:
                books_present.append(key)
        multi_book = len(books_present) > 1

        sections: list[str] = []
        for book, label in books_present:
            cards = []
            for art in articles:
                if (
                    art.get("book") != book
                    or art.get("kind") == "arcana"
                    or art.get("hub_slug")
                ):
                    continue
                slug = art["slug"]
                page = locale["pages"].get(slug) or {}
                title = titles.get(slug) or art["title"]
                excerpt = page.get("description") or strip_page_refs(
                    (previews.get(slug, {}) or {}).get("excerpt") or ""
                )
                if art.get("kind") == "arcana-hub":
                    # A generated index: its words are the language's own
                    # (ui → arcana_hub), the same as on the page itself.
                    words = arcana_hub_strings(art, ui)
                    title, excerpt = words["title"], words["lede"]
                href = f"{slug}.html" if slug in translated else f"../{slug}.html"
                cards.append(
                    f'<a class="index-card" href="{html.escape(href)}">'
                    f'<p class="card-title">{html.escape(title)}</p>'
                    f'<p class="card-excerpt">{html.escape(excerpt)}</p></a>'
                )
            if not cards:
                continue
            heading = book_labels.get(book) or label
            if not multi_book:
                heading = ui.get("nav_label") or home["topics"]
            sections.append(
                f"<h2>{html.escape(heading)}</h2>"
                f'<div class="index-grid">{"".join(cards)}</div>'
            )

        labels = [
            f"<strong>{html.escape(book_labels.get(b) or lab)}</strong>"
            for b, lab in books_present
        ]
        lede_books = labels[0] if len(labels) == 1 else ", ".join(labels)
        hero = (
            '<div class="index-hero">'
            f'<p class="lede">{home["lede_html"].format(books=lede_books)}</p>'
            f'<p class="index-vtt">{home["remembers_html"]}</p>'
            f'<p class="index-note">'
            f'{home["defects_html"].format(issues=html.escape(ISSUES_URL))}</p>'
            f'<p class="index-license">'
            f'{home["license_html"].format(license=html.escape(LICENSE_URL))}'
            "</p></div>"
        )
        intro = home_intro_html(
            home["intro_html"],
            lambda slug: (
                f"{slug}.html" if slug in translated else f"../{slug}.html"
            ),
        )
        title = home["title"]
        body = (
            f'<h1 class="page-title">{html.escape(title)}</h1>'
            f'<div class="index-intro">{intro}</div>'
            + "".join(sections)
            + hero
        )
        html_out = page_shell(
            title,
            "index",
            body,
            articles,
            rel_prefix="../",
            description=strip_page_refs(home["lede_html"]).replace(
                "{books}", ""
            ),
            locale=locale,
            alternates=home_alts,
            translated_slugs=translated,
        )
        (lang_dir / "index.html").write_text(html_out, encoding="utf-8")
        written.append(f"{code}/index.html")
    if written:
        print(f"  i18n: {len(written)} home pages")
    return written


MAP_PIN_COLORS = [
    "#e2534a",  # red
    "#e08a3c",  # orange
    "#e6c34a",  # yellow
    "#5aa85a",  # green
    "#4a90d9",  # blue
    "#9b6dc4",  # purple
]


# ---------------------------------------------------------------------------
# Bestiary
#
# Not in either printed book. The books set their stat blocks where the
# creature lives — the swyn in the Great Wood, the guardians at Barrier Pass,
# the crinwin in Dangers — which is right for reading and slow at the table,
# where the question is "where is this thing's block". This is every stat
# block in both books, one line each, A-Z: the name, which links to the block
# (hover shows the whole thing), and the page it lives on. Filed in the
# sidebar after Book II's four appendices.
# ---------------------------------------------------------------------------

BESTIARY_SLUG = "bestiary"
BESTIARY_TITLE = "Bestiary"

_STAT_NAME_RE = re.compile(r'<h3 class="stat-name">(.*?)</h3>', re.S)
_ICON_RE = re.compile(r'<img class="book-icon"[^>]*>')


def bestiary_article(book2: dict) -> dict:
    """The bestiary's article: Book II's, after its last appendix.

    ``book2`` is any Book II article, whose book fields it copies so the
    sidebar, the home page and llms.txt file it with the appendices. It has
    no printed pages (``start_page`` 0, an empty range), so no "page N"
    reference resolves to it.
    """
    return {
        "title": BESTIARY_TITLE,
        "slug": BESTIARY_SLUG,
        "kind": "bestiary",
        "book": book2.get("book") or "book2",
        "book_label": book2.get("book_label") or "Book II",
        "book_title": book2.get("book_title") or "Book II",
        "start_page": 0,
        "end_page": -1,
    }


def _bestiary_key(name: str) -> str:
    """Sort key: "The Bear of Winter" files under B, as a reader expects."""
    n = re.sub(r"^(the|a|an)\s+", "", name.strip().lower())
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def bestiary_entries(articles: list[dict], previews: dict) -> list[dict]:
    """Every stat block the build found, in A-Z order.

    Read off the pages' deep-link blocks (``previews[slug]["sections"]``), so
    an entry names the block exactly as its page does. A creature printed in
    two places is listed twice, once per page, since the blocks differ.
    """
    entries: list[dict] = []
    for art in articles:
        slug = art["slug"]
        if slug == BESTIARY_SLUG:
            continue
        secs = (previews.get(slug) or {}).get("sections") or {}
        for sid, sec in secs.items():
            if sec.get("kind") != "stat-block":
                continue
            block = sec.get("html") or ""
            m = _STAT_NAME_RE.search(block)
            name_html = m.group(1) if m else html.escape(sec.get("name") or sid)
            icon = _ICON_RE.search(name_html)
            name = html.unescape(re.sub(r"<[^>]+>", "", name_html)).strip()
            if not name:
                continue
            entries.append(
                {
                    "slug": slug,
                    "id": sid,
                    "name": name,
                    "icon": icon.group(0) if icon else "",
                    "from": display_title(art),
                }
            )
    entries.sort(key=lambda e: (_bestiary_key(e["name"]), e["from"]))
    return entries


def bestiary_excerpt(count: int) -> str:
    return (
        f"Every stat block in both Stonetop books, A to Z — {count} "
        "creatures, spirits and people, each linked to its block and the "
        "page it lives on."
    )


def bestiary_html(entries: list[dict]) -> str:
    """The page body: a letter bar, then one line per stat block."""
    e = html.escape
    letters: list[str] = []
    groups: dict[str, list[dict]] = {}
    for ent in entries:
        key = _bestiary_key(ent["name"])
        letter = key[:1].upper() if key[:1].isalpha() else "#"
        if letter not in groups:
            groups[letter] = []
            letters.append(letter)
        groups[letter].append(ent)

    def letter_id(letter: str) -> str:
        return "letter-" + ("num" if letter == "#" else letter.lower())

    parts = [
        f'<h1 class="page-title">{e(BESTIARY_TITLE)}</h1>',
        f'<p class="lede">Every stat block in both books, A to Z — '
        f"{len(entries)} of them. Hover a name for the whole block; click it "
        "to go there.</p>",
        '<p class="be-note">Not in the printed books: the wiki assembles '
        "this list from their stat blocks.</p>",
        '<p class="be-letters">'
        + " ".join(f'<a href="#{letter_id(l)}">{e(l)}</a>' for l in letters)
        + "</p>",
    ]
    for letter in letters:
        parts.append(f'<h2 id="{letter_id(letter)}">{e(letter)}</h2>')
        items = []
        for ent in groups[letter]:
            href = f'{ent["slug"]}.html#{ent["id"]}'
            items.append(
                f'<li>{ent["icon"]}<a class="wiki-link" href="{e(href)}" '
                f'data-slug="{e(ent["slug"])}">{e(ent["name"])}</a> '
                f'<span class="be-from"><a class="wiki-link" '
                f'href="{e(ent["slug"])}.html" data-slug="{e(ent["slug"])}">'
                f'{e(ent["from"])}</a></span></li>'
            )
        parts.append('<ul class="bestiary">' + "".join(items) + "</ul>")
    return "\n".join(parts)


# What each of Book II's two map spreads labels, in reading order. The
# drawings are artwork and are never published (see the licence note on the
# home page), but the places they name are pages of this wiki, so the stub
# stands in for the spread: a gazetteer of the map, linked.
MAPS_SPREADS = [
    {
        "id": "the-vicinity",
        "name": "The Vicinity",
        "pages": "pages 8-9",
        "gloss": (
            "Stonetop and the country within a few days' walk of it — the "
            "bluff, the wood below it, the roads out."
        ),
        "places": [
            ("the-village-of-stonetop", "Stonetop"),
            ("the-great-wood", "The Great Wood"),
            ("the-flats", "The Flats"),
            ("the-foothills", "The Foothills"),
            ("the-stream", "The Stream"),
            ("the-maw", "The Maw"),
            ("red-groves", "The Red Groves"),
            ("the-ruined-tower", "The Ruined Tower"),
            ("the-makers-roads", "The Highway and the West Road"),
            ("gordins-delve", "Gordin's Delve"),
            ("barrier-pass", "Barrier Pass"),
            ("the-steplands", "The Steplands"),
            ("marshedge", "Marshedge"),
        ],
    },
    {
        "id": "the-worlds-end",
        "name": "The World's End",
        "pages": "pages 10-11",
        "gloss": (
            "The whole region: mountains and marsh, the lakes, the Manmarch, "
            "and the road south out of the world's end."
        ),
        "places": [
            ("the-village-of-stonetop", "Stonetop"),
            ("the-flats", "The Flats"),
            ("the-great-wood", "The Great Wood"),
            ("the-steplands", "The Steplands"),
            ("titan-bones", "Titan Bones"),
            ("three-coven-lake", "Three Coven Lake"),
            ("blackwater-lake", "Blackwater Lake"),
            ("huffel-peaks", "The Huffel Peaks"),
            ("the-whitefang-mountains", "The Whitefang Mountains (Tor's Fist)"),
            ("gordins-delve", "Gordin's Delve"),
            ("barrier-pass", "Barrier Pass"),
            ("marshedge", "Marshedge"),
            ("ferriers-fen", "Ferrier's Fen"),
            ("the-dread-river", "The Dread River"),
            ("north-manmarch", "North Manmarch"),
            ("south-manmarch", "South Manmarch"),
            ("lygos-and-the-south", "Lygos and points south"),
        ],
    },
]

MAPS_STUB_EXCERPT = (
    "Book II's two map spreads — The Vicinity (pp. 8-9) and The World's "
    "End (pp. 10-11) — and every place they label, linked to its entry."
)


def maps_stub_html(articles: list[dict]) -> str:
    """The Maps page without the maps.

    The spreads are Lucie Arnoux's artwork, so a published build has no images
    to show (``--maps`` is a local build). The chapter still exists in the
    book, and Book II sends the reader to it on its first page, so the page
    stands rather than 404s: what each spread covers, and the places it labels
    as links. Only slugs this build actually produced are linked.
    """
    have = {a["slug"] for a in articles}
    lic = (
        f' (<a href="{html.escape(LICENSE_URL)}" rel="license">CC BY-SA 4.0</a>)'
    )
    parts = [
        '<h1 class="page-title">Maps</h1>',
        '<p class="lede">Book II opens with two map spreads: '
        "<strong>The Vicinity</strong> (pages 8-9) and "
        "<strong>The World's End</strong> (pages 10-11).</p>",
        "<p>The maps are <strong>artwork</strong> — &copy; Lucie Arnoux — and "
        "this wiki publishes only the books' text" + lic + ", so the drawings "
        "are not reproduced here. Open the Book II PDF to pages 8-11 for them. "
        "What follows is what the spreads name, linked to its entry.</p>",
    ]
    for spread in MAPS_SPREADS:
        links = [
            f'<li><a href="{html.escape(slug)}.html">{html.escape(label)}</a></li>'
            for slug, label in spread["places"]
            if slug in have
        ]
        if not links:
            continue
        parts.append(
            f'<h2 id="{html.escape(spread["id"])}">'
            f'{html.escape(spread["name"])} ({html.escape(spread["pages"])})</h2>'
        )
        parts.append(f'<p>{spread["gloss"]}</p>')
        parts.append(f'<ul class="maps-places">{"".join(links)}</ul>')
    return "\n".join(parts)


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
        src = html.escape(f"images/{im['file']}")
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



def ensure_wiki_chrome(out: Path) -> None:
    """
    Confirm redistributable chrome already lives under *out*.

    The generator does not copy css/, js/wiki.js, or images/icons/ — those are
    part of the wiki folder (and the git tree). It only overwrites book-derived
    files (<slug>.html pages, previews-data.js, search-index.js, index.html).
    """
    required = [
        out / "css" / "wiki.css",
        out / "js" / "wiki.js",
        out / "images" / "icons" / "default.svg",
    ]
    missing = [p for p in required if not p.is_file()]
    if missing:
        raise SystemExit(
            "Wiki chrome missing from the output folder "
            "(css/, js/wiki.js, images/icons/ are not generated or copied):\n"
            + "\n".join(f"  {p}" for p in missing)
        )
    print("  wiki chrome: css/, js/wiki.js, images/icons/ (in place)")

def write_index_custom(
    articles: list[dict],
    previews: dict,
    out_path: Path,
    *,
    alternates: list[dict] | None = None,
) -> None:
    """The English home page.

    ``alternates`` is the home page's language cluster (``home_alternates``),
    which gives this page the hreflang set and the sidebar language switcher
    every other page gets from ``page_shell``.
    """
    nav_items = build_nav_items(articles)

    books_present = []
    for art in articles:
        key = (
            art.get("book"),
            art.get("book_title") or art.get("book_label") or art.get("book") or "",
        )
        if key not in books_present:
            books_present.append(key)
    multi_book = len(books_present) > 1

    sections: list[str] = []
    for book, label in books_present:
        cards = []
        for art in articles:
            if (
                art.get("book") != book
                or art.get("kind") == "arcana"
                or art.get("hub_slug")
            ):
                continue
            pv = previews.get(art["slug"], {})
            excerpt = strip_page_refs(pv.get("excerpt") or "")
            href = f'{art["slug"]}.html'
            cards.append(
                f'<a class="index-card" href="{html.escape(href)}">'
                f'<p class="card-title">{html.escape(art["title"])}</p>'
                f'<p class="card-excerpt">{html.escape(excerpt)}</p></a>'
            )
        if not cards:
            continue
        heading = html.escape(label) if multi_book else "Topics"
        sections.append(
            f"<h2>{heading}</h2>\n"
            f'<div class="index-grid">{"".join(cards)}</div>'
        )
    cards_html = "\n".join(sections)

    labels = [
        f"<strong>{html.escape(label)}</strong>"
        for _b, label in books_present
    ]
    if len(labels) > 1:
        lede_books = ", ".join(labels[:-1]) + " and " + labels[-1]
    else:
        lede_books = labels[0]
    issues_url = html.escape(ISSUES_URL)
    license_url = html.escape(LICENSE_URL)
    intro_html = home_intro_html(HOME_INTRO_HTML, lambda slug: f"{slug}.html")
    home_meta = social_meta_html(
        "Stonetop",
        "Stonetop is a hearth fantasy tabletop RPG by Jeremy Strandberg. A "
        "free, searchable wiki of both rulebooks: moves, playbooks, gear, "
        "threats, places and arcana.",
        "",
        alternates=alternates,
    )
    switch = lang_switch_html(
        alternates or [], "en", UI_FALLBACK, rel_prefix=""
    )
    # The home page is where the site, the game and the two books are
    # defined; every other page refers to them by @id.
    jsonld = page_structured_data(
        None,
        title=EDITION_NAME,
        description=meta_description(
            "Stonetop is a hearth fantasy tabletop RPG by Jeremy Strandberg. "
            "A free, searchable wiki of both rulebooks: moves, playbooks, "
            "gear, threats, places and arcana."
        ),
        url=SITE_BASE_URL.rstrip("/") + "/",
        lang="en",
        home_url=SITE_BASE_URL.rstrip("/") + "/",
        is_home=True,
    )

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stonetop — hearth fantasy tabletop RPG (TTRPG) web edition</title>
{home_meta}
  <link rel="icon" href="images/favicon.svg" type="image/svg+xml">
  <link rel="alternate icon" href="images/favicon.ico" sizes="16x16 32x32 48x48 64x64">
  <link rel="stylesheet" href="css/wiki.css">
{jsonld}
{ANALYTICS_HTML}
</head>
<body>
  <a class="skip-link" href="#main">Skip to content</a>
  <button type="button" class="sidebar-toggle" id="sidebar-toggle" aria-label="Toggle navigation">☰</button>
  <div class="layout">
    <aside class="sidebar" id="sidebar">
      <div class="sidebar-head">
        <a class="wiki-title" href="index.html">{html.escape(EDITION_NAME)}</a>
        <input type="search" id="nav-filter" class="nav-filter" placeholder="Search wiki…" autocomplete="off" aria-label="Search wiki">
        <div id="search-results" class="search-results" hidden></div>
      </div>
      <nav class="toc" aria-label="Topics">
        <ul id="nav-list">
          {''.join(nav_items)}
        </ul>
      </nav>
      {sidebar_foot_html(lang_switch=switch)}
    </aside>
    <div class="main-wrap">
      <div class="content-scroll" id="main">
        <main class="content"><h1 class="page-title">{html.escape(EDITION_NAME)}</h1>
        <div class="index-intro">{intro_html}</div>
        {cards_html}
        <div class="index-hero">
          <p class="lede">A static, hyperlinked wiki for <em>Stonetop</em>
          {lede_books}.
          Page numbers are links; dice expressions roll on click; hover a link for a preview
          (full stat blocks when deep-linked).</p>
          <p class="index-vtt"><strong>It remembers.</strong> Tick a checkbox, answer a question,
          fill in a blank, assign your stats — the wiki keeps all of it, so a playbook is a
          character sheet you can actually play off, and a danger's countdown or a steading's
          improvements stay marked between sessions. Every question mark and every fill-in-the-blank
          in the books takes a note, and the playbooks roll: click a stat to roll +that stat, or the
          damage die to roll damage. Mark a debility and its two stats roll with disadvantage on
          their own.</p>
          <p class="index-note">These pages are extracted from the books' PDFs automatically, so
          <strong>expect defects</strong> — mangled tables, dropped or duplicated text, wrong
          headings, broken links. If you spot one, or want to help fix them,
          <a href="{issues_url}">open an issue or a pull request on GitHub</a>.</p>
          <p class="index-license">The books' <strong>text</strong> is by
          Jeremy Strandberg under <a href="{license_url}" rel="license">CC BY-SA 4.0</a>.
          Dice sounds by <a href="https://opengameart.org/content/wooden-dice-on-wodden-table-roll">Wuzzy</a>,
          <a href="https://creativecommons.org/publicdomain/zero/1.0/" rel="license">CC0</a>.
          Free data sync provided by
          <a href="https://workers.cloudflare.com/">Cloudflare</a>.</p>
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

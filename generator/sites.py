"""
Adventure-site sheets kept under ``Stonetop_Wiki/sites/``: discovery,
indexing and the Sites hub.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from .text import html_to_search_text, slugify


# Adventure-site sheets live in the wiki output under sites/ (not copied).
SITES_OUT_DIRNAME = "sites"
SITES_BOOK_ID = "sites"
SITES_LABEL = "Sites"
# Book I has its own "Sites" chapter (p. 345), which claims the plain slug —
# the campaign hub takes a qualified one.
SITES_HUB_SLUG = "campaign-sites"


def resolve_sites_dir(out: Path) -> Path | None:
    """Return ``<out>/sites/`` if present (sheets are edited in place)."""
    candidate = out / SITES_OUT_DIRNAME
    if candidate.is_dir():
        return candidate
    return None


# ---------------------------------------------------------------------- Sites
#
# Optional table sheets (adventure sites) live in <out>/sites/ (edited in place;
# not copied). They link wiki pages as ../<slug>.html. The build indexes them for
# the sidebar, home page, Sites hub, full-text search, and back-links on
# referenced wiki pages. Drop a new .html (or a folder of variants) and rebuild.

# Shared chrome filenames, not site sheets
SITE_SKIP_STEMS = {"index", "site"}

# "Timber for the Inn — Gordin's Delve (One-Pager)" → variant "One-Pager"
_SITE_VARIANT_RE = re.compile(r"\(([^()]{2,40})\)\s*$")


def _site_strip_tags(fragment: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", fragment or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _site_find(raw: str, pattern: str) -> str:
    m = re.search(pattern, raw, re.I | re.S)
    return _site_strip_tags(m.group(1)) if m else ""


def read_site_file(path: Path, href: str) -> dict:
    """Title, variant label, excerpt, wiki refs and search text for one sheet."""
    raw = path.read_text(encoding="utf-8", errors="replace")

    doc_title = _site_find(raw, r"<title[^>]*>(.*?)</title>")
    variant = ""
    m = _SITE_VARIANT_RE.search(doc_title)
    if m:
        variant = m.group(1).strip()
        doc_title = _SITE_VARIANT_RE.sub("", doc_title).strip(" —-·")
    title = (
        _site_find(raw, r"<h1[^>]*>(.*?)</h1>")
        or doc_title
        or path.stem.replace("-", " ")
    )

    excerpt = (
        _site_find(raw, r'<p class="lede"[^>]*>(.*?)</p>')
        or _site_find(raw, r'<p class="meta"[^>]*>(.*?)</p>')
        or _site_find(raw, r"<p[^>]*>(.*?)</p>")
    )
    if len(excerpt) > 240:
        excerpt = excerpt[:237].rstrip(" ,;:—-") + "…"

    # Wiki pages the sheet points at → back-links on those pages.
    links: list[str] = []
    for slug in re.findall(r'data-slug="([^"]+)"', raw):
        if slug not in links:
            links.append(slug)

    # A sheet says for itself whether it has been run at a table. Only the
    # playtested ones are advertised on the home page; the rest are still
    # linked from the Sites hub, flagged.
    playtested = bool(
        re.search(r'<body\b[^>]*\bdata-playtested="true"', raw, re.I)
    )

    # Nav/sidebar chrome would swamp the search text with link labels.
    text = html_to_search_text(re.sub(r"(?is)<nav\b.*?</nav>", " ", raw))

    return {
        "path": path,
        "href": href,
        "title": title,
        "variant": variant,
        "excerpt": excerpt,
        "links": links,
        "playtested": playtested,
        "text": text,
    }


def _site_variant_label(sheet: dict, primary: dict) -> str:
    """Label for a variant link: the title's parenthetical, else the filename tail."""
    if sheet["variant"]:
        return sheet["variant"]
    stem = sheet["path"].stem
    base = primary["path"].stem
    if sheet is not primary and stem.lower().startswith(base.lower()):
        tail = stem[len(base):].strip("-_ ")
        if tail:
            return tail.replace("-", " ").replace("_", " ")
    return "Full write-up" if sheet is primary else stem.replace("-", " ")


def discover_sites(out: Path) -> list[dict]:
    """
    Site sheets under ``<out>/sites/``, in either shape:

      Vasilyas-Grove.html                          → one site
      Some-Site/Some-Site.html                     → one site whose folder
      Some-Site/…-One-Pager.html                     holds several variants

    Hrefs are relative to the wiki root (e.g. ``sites/Sealed-Cave.html``).
    """
    root = resolve_sites_dir(out)
    if root is None:
        return []

    def href_for(path: Path) -> str:
        rel = path.relative_to(root).as_posix()
        return f"{SITES_OUT_DIRNAME}/{rel}"

    groups: list[list[Path]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith("."):
            continue
        if entry.is_file():
            if (
                entry.suffix.lower() in (".html", ".htm")
                and entry.stem.lower() not in SITE_SKIP_STEMS
            ):
                groups.append([entry])
        elif entry.is_dir():
            files = [
                p
                for p in entry.rglob("*.htm*")
                if p.stem.lower() not in SITE_SKIP_STEMS
            ]
            # Primary = the sheet named after its folder, else the shortest name.
            files.sort(
                key=lambda p: (
                    p.stem.lower() != entry.name.lower(),
                    len(p.stem),
                    p.name.lower(),
                )
            )
            if files:
                groups.append(files)

    sites: list[dict] = []
    for paths in groups:
        sheets = [read_site_file(p, href_for(p)) for p in paths]
        primary = sheets[0]
        links: list[str] = []
        for sheet in sheets:
            for slug in sheet["links"]:
                if slug not in links:
                    links.append(slug)
        variants = (
            [
                {"label": _site_variant_label(s, primary), "href": s["href"]}
                for s in sheets
            ]
            if len(sheets) > 1
            else []
        )
        sites.append(
            {
                "title": primary["title"],
                # Sheets set apostrophes as ’ — slugify() only strips the
                # typewriter kind ("Vasilya’s" → vasilyas-grove, not vasilya-s-…).
                "slug": slugify(primary["title"].replace("’", "'")),
                "href": primary["href"],
                "excerpt": primary["excerpt"],
                "links": links,
                "playtested": primary["playtested"],
                "variants": variants,
                "text": "\n".join(s["text"] for s in sheets),
            }
        )
    return sites


def site_articles(sites: list[dict]) -> list[dict]:
    """Hub + one nav/index entry per site, in article shape."""
    if not sites:
        return []
    common = {
        "book": SITES_BOOK_ID,
        "book_label": SITES_LABEL,
        "book_title": SITES_LABEL,
        "start_page": 0,
        "end_page": 0,
    }
    arts: list[dict] = [
        {
            "title": SITES_LABEL,
            "slug": SITES_HUB_SLUG,
            "kind": "sites-hub",
            **common,
        }
    ]
    for site in sites:
        arts.append(
            {
                "title": site["title"],
                "slug": site["slug"],
                "kind": "site",
                "href": site["href"],
                "excerpt": site["excerpt"],
                "playtested": site["playtested"],
                "site": site,
                **common,
            }
        )
    return arts


def _site_wiki_refs(site: dict, titles_by_slug: dict[str, str]) -> list[tuple[str, str]]:
    """(slug, title) for the wiki pages a site sheet links to, known ones only."""
    links = site.get("links") or []
    return [(s, titles_by_slug[s]) for s in links if s in titles_by_slug]


def sites_hub_html(
    site_arts: list[dict],
    titles_by_slug: dict[str, str],
    *,
    root_prefix: str = "",
) -> str:
    """Body for the Sites hub page: a card per site."""
    cards: list[str] = []
    for art in site_arts:
        site = art["site"]
        href = html.escape(root_prefix + art["href"])
        slug = html.escape(art["slug"])
        # Linked either way — the hub is where you go looking. The tag is the
        # warning that this one has not been run at a table yet.
        flag = (
            ""
            if site.get("playtested")
            else ' <span class="tag danger">untested</span>'
        )
        parts = [
            f'<h2 id="{slug}"><a class="wiki-link" href="{href}" '
            f'data-slug="{slug}">{html.escape(art["title"])}</a>{flag}</h2>'
        ]
        if site["excerpt"]:
            parts.append(f'<p class="site-excerpt">{html.escape(site["excerpt"])}</p>')
        if site["variants"]:
            links = " · ".join(
                f'<a class="wiki-link" href="{html.escape(root_prefix + v["href"])}">'
                f'{html.escape(v["label"])}</a>'
                for v in site["variants"]
            )
            parts.append(f'<p class="site-variants">{links}</p>')
        refs = _site_wiki_refs(site, titles_by_slug)
        if refs:
            ref_links = " · ".join(
                f'<a class="wiki-link" href="{html.escape(s)}.html" '
                f'data-slug="{html.escape(s)}">{html.escape(t)}</a>'
                for s, t in refs
            )
            parts.append(
                f'<p class="site-refs"><span class="site-refs-label">Uses</span> '
                f"{ref_links}</p>"
            )
        cards.append(f'<section class="site-card">{"".join(parts)}</section>')

    return (
        "<p>Adventure sites — prep, room-by-room notes and stat "
        "blocks — under "
        f"<code>{html.escape(SITES_OUT_DIRNAME)}/</code>. Each links into "
        "the rulebook pages.</p>"
        f'<div class="site-list">{"".join(cards)}</div>'
    )

"""
The wiki shell around an article: page template, sidebar nav, hub pages,
hand-authored page overrides (``pages/``), the home page, sitemap and robots.
"""

from __future__ import annotations

import datetime
import html
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
from .structure import T_english
from .sites import SITES_BOOK_ID
from .text import _is_all_caps_label, normalize_section_key, slugify_id, titlecase_label

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
SITE_BASE_URL = "https://stonetop-wiki.github.io"


SITE_NAME = "Stonetop Wiki"

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
    full = f"{title} — {SITE_NAME}"
    base = SITE_BASE_URL.rstrip("/")
    url = base + "/" + path.lstrip("/")
    img = base + "/images/favicon.png"
    e = html.escape
    tags = [
        f'  <meta name="description" content="{e(desc)}">',
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
        href = base + "/" + alt["path"].lstrip("/")
        tags.append(
            f'  <link rel="alternate" hreflang="{e(alt["hreflang"])}" '
            f'href="{e(href)}">'
        )
    if alternates:
        # x-default is what a reader who matches no listed language gets.
        # alternates[0] is the source language (English) by construction.
        default = base + "/" + alternates[0]["path"].lstrip("/")
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


def write_sitemap(
    out: Path,
    articles: list[dict],
    *,
    base_url: str,
    extra: list[str] | None = None,
) -> None:
    """sitemap.xml covering every wiki page, site sheet, and translation."""
    base = base_url.rstrip("/")
    locs = [base + "/"]
    for art in articles:
        href = art.get("href") or (art["slug"] + ".html")
        locs.append(base + "/" + href)
        for var in (art.get("site") or {}).get("variants") or []:
            locs.append(base + "/" + var["href"])
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

    Adventures carry an `href` relative to the wiki root (they live outside
    the wiki root), so `root_prefix` walks back up from the page being rendered.

    Campaign sites are left out of the sidebar: they are table sheets for one
    group, not part of the books, and they are still reachable from the home
    page, the Sites hub, search, and their own back-links.
    """
    articles = [
        a for a in articles if a.get("kind") not in ("site", "sites-hub")
    ]
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
        elif art.get("kind") in ("arcana-hub", "sites-hub") or art.get(
            "children"
        ):
            classes.append("nav-hub")
        elif art.get("kind") == "site":
            classes.append("nav-site")
        secs = section_navs.get(art["slug"]) or []
        if secs or parts:
            classes.append("has-sections")
        if current_slug is not None and art["slug"] == current_slug:
            classes.append("current")
        cls_attr = f' class="{" ".join(classes)}"' if classes else ""
        art_slug = html.escape(art["slug"])
        page_tr = lang_pages.get(art["slug"])
        if art.get("href"):
            # Slug can't be read back out of a site sheet's file name.
            href = html.escape(root_prefix + art["href"])
            slug_attr = f' data-nav-slug="{art_slug}"'
        elif page_tr:
            # Translated: the sibling in this same language directory.
            href = f"{art_slug}.html"
            slug_attr = ""
        else:
            href = f"{href_prefix}{art_slug}.html"
            slug_attr = ""
        label = html.escape(
            (page_tr or {}).get("nav_label") or nav_label(art)
        )
        if locale and not page_tr and not art.get("href"):
            slug_attr += ' class="nav-en"'
            if english_only:
                slug_attr += f' title="{html.escape(english_only)}" hreflang="en"'
        link = f'<a href="{href}"{slug_attr}>{label}</a>'
        if art.get("kind") == "site" and art.get("site", {}).get("variants"):
            sub = "".join(
                f'<li class="nav-section"><a href="'
                f'{html.escape(root_prefix + v["href"])}">{html.escape(v["label"])}'
                f"</a></li>"
                for v in art["site"]["variants"]
            )
            items.append(
                f"<li{cls_attr}>{link}"
                f'<ul class="nav-sections">{sub}</ul></li>'
            )
            continue
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
                if part_tr:
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
    e = html.escape
    dir_attr = f' dir="{e((locale or {}).get("dir") or "ltr")}"' if locale else ""
    # A language directory needs its own data files no more than it needs its
    # own copy of wiki.js: both sit at the wiki root and are reached through
    # rel_prefix, so search and hover previews keep working from a translated
    # page and lead back to the English pages that are not translated yet.
    root_attr = f' data-wiki-root="{e(rel_prefix)}"' if rel_prefix else ""

    return f"""<!DOCTYPE html>
<html lang="{e(code)}"{dir_attr}>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)} — Stonetop Wiki</title>
{meta_html}
  <link rel="icon" href="{rel_prefix}images/favicon.svg" type="image/svg+xml">
  <link rel="alternate icon" href="{rel_prefix}images/favicon.ico" sizes="16x16 32x32 48x48 64x64">
  <link rel="stylesheet" href="{rel_prefix}css/wiki.css">
{ANALYTICS_HTML}
</head>
<body{root_attr}>
  <a class="skip-link" href="#main">{e(ui["skip_to_content"])}</a>
  <button type="button" class="sidebar-toggle" id="sidebar-toggle" aria-label="{e(ui["toggle_nav"])}">☰</button>
  <div class="layout">
    <aside class="sidebar" id="sidebar">
      <div class="sidebar-head">
        <a class="wiki-title" href="{rel_prefix}index.html">Stonetop Wiki</a>
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
  <script src="{rel_prefix}js/wiki.js"></script>
</body>
</html>
"""


def write_localized_pages(
    out: Path,
    articles: list[dict],
    section_navs: dict[str, list[dict]],
    english_bodies: dict[str, str],
    source: dict,
    targets: list[dict],
) -> list[str]:
    """Write ``<out>/<lang>/<slug>.html`` for every translated page.

    Returns the hrefs written, for the sitemap. Each language directory is
    rewritten from scratch, so a translation file that is deleted takes its
    page with it.
    """
    written: list[str] = []
    for locale in targets:
        code = locale["code"]
        lang_dir = out / code
        if lang_dir.exists():
            shutil.rmtree(lang_dir)
        lang_dir.mkdir(parents=True)
        translated = set(locale["pages"])
        stale = []
        for slug, page in sorted(locale["pages"].items()):
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
            # Mirror the English page: a sheet's h1 carries pb-title and its
            # <main> the playbook class, so localized sheets keep the sheet
            # styling and widgets.
            h1_cls = "page-title"
            if 'class="pb-stats"' in body:
                h1_cls += " pb-title"
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

def write_index_custom(articles: list[dict], previews: dict, out_path: Path) -> None:
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
            # An untested site is reachable from the Sites hub, where it is
            # flagged. The home page only offers what has been run at a table.
            if art.get("kind") == "site" and not art.get("playtested"):
                continue
            pv = previews.get(art["slug"], {})
            excerpt = strip_page_refs(pv.get("excerpt") or "")
            # Sites sit in sites/ — link to the sheet where it lives.
            href = art.get("href") or f'{art["slug"]}.html'
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
        if _b != SITES_BOOK_ID
    ]
    if len(labels) > 1:
        lede_books = ", ".join(labels[:-1]) + " and " + labels[-1]
    else:
        lede_books = labels[0]
    issues_url = html.escape(ISSUES_URL)
    license_url = html.escape(LICENSE_URL)
    home_meta = social_meta_html(
        "Stonetop Wiki",
        "A searchable web edition of Stonetop and The Wider World and Other "
        "Wonders by Jeremy Strandberg — moves, gear, threats, places, and "
        "arcana, plus table-ready adventure sites.",
        "",
    )

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stonetop Wiki</title>
{home_meta}
  <link rel="icon" href="images/favicon.svg" type="image/svg+xml">
  <link rel="alternate icon" href="images/favicon.ico" sizes="16x16 32x32 48x48 64x64">
  <link rel="stylesheet" href="css/wiki.css">
{ANALYTICS_HTML}
</head>
<body>
  <a class="skip-link" href="#main">Skip to content</a>
  <button type="button" class="sidebar-toggle" id="sidebar-toggle" aria-label="Toggle navigation">☰</button>
  <div class="layout">
    <aside class="sidebar" id="sidebar">
      <div class="sidebar-head">
        <a class="wiki-title" href="index.html">Stonetop Wiki</a>
        <input type="search" id="nav-filter" class="nav-filter" placeholder="Search wiki…" autocomplete="off" aria-label="Search wiki">
        <div id="search-results" class="search-results" hidden></div>
      </div>
      <nav class="toc" aria-label="Topics">
        <ul id="nav-list">
          {''.join(nav_items)}
        </ul>
      </nav>
      {sidebar_foot_html()}
    </aside>
    <div class="main-wrap">
      <div class="content-scroll" id="main">
        <main class="content"><h1 class="page-title">Stonetop Wiki</h1>
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

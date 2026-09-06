"""
The build: command line, the two phases (extract → corpus, corpus → wiki),
and the search/preview indexes.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
from pathlib import Path

try:
    import fitz  # PyMuPDF — only needed to extract from the PDFs
except ImportError:  # pragma: no cover
    fitz = None

from . import REPO_ROOT
from .arcana import major_arcana_html, minor_arcana_html
from .articles import (
    BOOKS,
    build_page_lookup,
    discover_articles,
    ensure_unique_slugs,
)
from .chrome import (
    SITE_BASE_URL,
    apply_override,
    arcana_hub_html,
    chapter_parts_html,
    display_title,
    ensure_wiki_chrome,
    is_sheet_body,
    maps_body_html,
    override_excerpt,
    override_sections,
    page_override,
    page_shell,
    read_build_manifest,
    write_build_manifest,
    write_index_custom,
    write_localized_pages,
    write_robots,
    write_sitemap,
)
from .corpus import (
    TEXT_KINDS,
    CorpusError,
    corpus_dir,
    has_book,
    read_book,
    write_book,
)
from .extract import (
    extract_article_lines,
    extract_major_arcana_rich,
    extract_minor_arcana_rich,
    index_book_tokens,
    prepare_map_images,
    split_chapter_toc,
)
from .i18n import alternates_for, load_locales, prune_language_dirs
from .sites import (
    SITES_BOOK_ID,
    SITES_OUT_DIRNAME,
    discover_sites,
    site_articles,
    sites_hub_html,
)
from .structure import (
    article_html,
    build_page_section_map,
    build_section_index,
    extract_section_html_blocks,
    match_toc_to_sections,
    set_page_sections,
    set_title_index,
)
from .text import heading_pages, html_to_search_text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Build a static Stonetop wiki. The books' text is extracted from "
            "the Book I / Book II 1-up PDFs into extracted/ (one plain-text "
            "file per article, checked in), and the wiki is built from that."
        )
    )
    p.add_argument(
        "-i",
        "--input",
        type=Path,
        default=Path.cwd(),
        help=(
            "Folder containing the Book I and/or Book II 1-up PDFs (optional "
            "subfolder Maps/ for campaign map sheets). Only read when "
            "extracting; a build from the corpus never opens a PDF. "
            "Default: current working directory."
        ),
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=(
            "Wiki folder. Static chrome (css/, js/wiki.js, images/icons/) and "
            "optional sites/ live here and are left in place; the build "
            "writes the page HTML, indexes, and map images. "
            "Default: <repo>/Stonetop_Wiki."
        ),
    )
    p.add_argument(
        "--corpus",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "The extracted text: <book>/articles.json and one <slug>.txt per "
            "article (format: generator/corpus.py). Default: <repo>/extracted."
        ),
    )
    p.add_argument(
        "--extract",
        action="store_true",
        help=(
            "Re-extract the text from the PDFs into the corpus, then build. "
            "Without it the corpus is built from as it is; a PDF is only "
            "opened for a book the corpus does not hold yet."
        ),
    )
    p.add_argument(
        "--extract-only",
        action="store_true",
        help="Extract into the corpus and stop without writing the wiki.",
    )
    p.add_argument(
        "--books",
        nargs="+",
        choices=[b["id"] for b in BOOKS],
        default=None,
        metavar="BOOK",
        help=(
            "Limit the run to these books (book1, book2) — handy while "
            "iterating, but the output wiki then only holds those books. "
            "Default: every book in the corpus, or every PDF found."
        ),
    )
    p.add_argument(
        "--base-url",
        default=SITE_BASE_URL,
        metavar="URL",
        help=(
            "Absolute site root used for sitemap.xml and robots.txt. "
            f"Default: {SITE_BASE_URL}"
        ),
    )
    p.add_argument(
        "--langs",
        nargs="+",
        default=None,
        metavar="LANG",
        help=(
            "Limit localized output to these language codes from "
            "i18n/langs.json (e.g. de fr ja), or 'none' to build English "
            "only. Default: every language that has at least one translated "
            "page."
        ),
    )
    p.add_argument(
        "--maps",
        action="store_true",
        help=(
            "Include the Maps page and its images (needs the Book II PDF). "
            "Off by default: the books' TEXT is CC BY-SA 4.0 but the artwork "
            "(maps included) is © Lucie Arnoux, so map images must not be "
            "published. Use this only for a local, unpublished build."
        ),
    )
    return p.parse_args(argv)


# ------------------------------------------------------------ extraction

def find_book_pdfs(input_dir: Path, books: list[dict]) -> list[dict]:
    """The books whose 1-up PDF is in ``input_dir``, with a ``path``."""
    return [
        {**book, "path": input_dir / book["filename"]}
        for book in books
        if (input_dir / book["filename"]).exists()
    ]


def extract_text(
    doc: fitz.Document, art: dict, icon_dir: Path
) -> tuple[list[str], list[int]] | None:
    """Marker lines and their PDF pages for one article.

    ``None`` for a kind that has no text of its own (a hub, the maps page).
    A chapter's printed table of contents is peeled off the front and kept
    on the article as ``toc_labels`` — the sidebar's deep links.
    """
    if art["kind"] not in TEXT_KINDS:
        return None
    pages: list[int] = []
    book_id = art.get("book") or "book2"
    if art["kind"] == "arcana":
        if art.get("arcana_type") == "minor":
            lines = extract_minor_arcana_rich(
                doc, art["start_page"], art.get("half") or "top", pages_out=pages
            )
        else:
            lines = extract_major_arcana_rich(
                doc, art["start_page"], art["end_page"], art["title"],
                pages_out=pages,
            )
        return lines, pages
    lines = extract_article_lines(
        doc,
        art["start_page"],
        art["end_page"],
        art["title"],
        icon_dir=icon_dir,
        book_style=book_id,
        pages_out=pages,
    )
    # Book I sets its chapter-opener TOCs in heading type.
    toc_labels, body = split_chapter_toc(
        lines, art["title"], allow_markers=(book_id == "book1")
    )
    pages = pages[len(lines) - len(body):]
    # A merged chapter has no printed TOC — it brings its own labels.
    art["toc_labels"] = toc_labels or art.get("toc_labels") or []
    return body, pages


def extract_books(
    found: list[dict], corpus: Path, icon_dir: Path
) -> tuple[dict[str, list[dict]], dict, dict[str, fitz.Document]]:
    """PDFs → article lists and text, written to the corpus.

    Returns ``(articles by book id, texts by slug, open documents)``.
    """
    if fitz is None:
        raise SystemExit("Extracting needs PyMuPDF: pip install -r requirements.txt")
    docs: dict[str, fitz.Document] = {}
    per_book: list[tuple[dict, fitz.Document, list[dict]]] = []
    for book in found:
        doc = fitz.open(str(book["path"]))
        docs[book["id"]] = doc
        per_book.append((book, doc, discover_articles(doc, book)))
    # Every page is a sibling of index.html, so slugs are settled across the
    # books before a text file is named after one.
    ensure_unique_slugs([a for _b, _d, arts in per_book for a in arts])
    # Both books inform the hyphenation decision — a compound wrapped in one
    # is often set in running text in the other ("person-like", "maker-ruin").
    index_book_tokens(docs.values())
    texts: dict[str, tuple[list[str], list[int]]] = {}
    by_book: dict[str, list[dict]] = {}
    for book, doc, arts in per_book:
        t0 = time.perf_counter()
        for art in arts:
            got = extract_text(doc, art, icon_dir)
            if got is not None:
                texts[art["slug"]] = got
        n = write_book(corpus, book, doc.page_count, arts, texts)
        by_book[book["id"]] = arts
        print(
            f"  {book['label']}: {n} text files → {corpus / book['id']} "
            f"({time.perf_counter() - t0:.1f}s)"
        )
    return by_book, texts, docs


# ------------------------------------------------------------------ build

def main(argv: list[str] | None = None) -> None:
    # A Windows console is often cp1252; the build's progress lines carry
    # arrows and ellipses, and a report must never crash the build.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    args = parse_args(argv)
    t_start = time.perf_counter()
    input_dir = args.input.expanduser().resolve()
    out = (
        args.output.expanduser().resolve()
        if args.output is not None
        else (REPO_ROOT / "Stonetop_Wiki").resolve()
    )
    corpus = (
        args.corpus.expanduser().resolve() if args.corpus else corpus_dir()
    )
    legacy_out = input_dir / "Book_II_Wiki"

    wanted = set(args.books) if args.books else None
    books = [b for b in BOOKS if wanted is None or b["id"] in wanted]
    extracting = args.extract or args.extract_only
    if extracting:
        to_extract = find_book_pdfs(input_dir, books)
        if not to_extract:
            raise SystemExit(
                f"No book PDFs found in {input_dir}\n"
                "Place the Book I and/or Book II 1-up PDFs in the input folder:\n"
                + "\n".join(f"  {b['filename']}" for b in books)
                + "\nor pass --input pointing at the folder that contains them."
            )
    else:
        # The corpus is the source. A book it lacks is extracted if its PDF
        # is at hand, and skipped with a note if not.
        missing = [b for b in books if not has_book(corpus, b["id"])]
        to_extract = find_book_pdfs(input_dir, missing)
        extracted_ids = {b["id"] for b in to_extract}
        for b in missing:
            if b["id"] not in extracted_ids:
                print(
                    f"  note: {b['label']} is not in {corpus} and its PDF "
                    f"({b['filename']}) is not in {input_dir} — skipping"
                )
    extracted_ids = {b["id"] for b in to_extract}
    from_corpus = [
        b for b in books
        if b["id"] not in extracted_ids and has_book(corpus, b["id"])
    ]
    in_play = [b for b in books if b["id"] in extracted_ids or b in from_corpus]
    if not in_play:
        raise SystemExit(
            f"Nothing to build: no extracted text under {corpus} and no book "
            f"PDFs in {input_dir}.\nPut the 1-up PDFs in the input folder and "
            "run with --extract, or clone a checkout that carries extracted/."
        )

    print("Building Stonetop wiki (" + ", ".join(b["label"] for b in in_play) + ")…")
    print(f"  input:  {input_dir}")
    print(f"  corpus: {corpus}")
    print(f"  output: {out}")

    # The category icons ship with the wiki; extraction resolves the books'
    # icon images against them, so the wiki folder is needed even to extract.
    ensure_wiki_chrome(out)
    icon_dir = out / "images" / "icons"

    by_book: dict[str, list[dict]] = {}
    texts: dict[str, tuple[list[str], list[int]]] = {}
    docs: dict[str, fitz.Document] = {}
    if to_extract:
        print("Extracting text from the PDFs…")
        by_book, texts, docs = extract_books(to_extract, corpus, icon_dir)
    for book in from_corpus:
        try:
            _manifest, arts, book_texts = read_book(corpus, book["id"])
        except CorpusError as e:
            raise SystemExit(f"Corpus error: {e}") from None
        by_book[book["id"]] = arts
        texts.update(book_texts)
        print(f"  {book['label']}: {len(book_texts)} text files from {corpus / book['id']}")
    # In BOOKS order whatever the source of each.
    articles: list[dict] = [a for b in books for a in by_book.get(b["id"], [])]
    book_ids = [b["id"] for b in books if b["id"] in by_book]

    if args.extract_only:
        print(f"Done. Extracted in {time.perf_counter() - t_start:.1f}s.")
        return

    # Translations are read before the pages are written: an English page has
    # to carry the same hreflang cluster its translations do, or Google
    # ignores the set (the cluster must be reciprocal).
    only_langs = args.langs
    if only_langs and len(only_langs) == 1 and only_langs[0].lower() == "none":
        only_langs = []
    lang_source, lang_targets = load_locales(only_langs)

    if legacy_out.is_dir() and not out.exists():
        try:
            legacy_out.rename(out)
            print(f"  Renamed {legacy_out.name} → {out.name}")
        except OSError as e:
            print(f"  note: could not rename legacy folder ({e}); building fresh")

    for sub in ("css", "js", "images", "images/icons", "images/maps"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    # Only wipe files THIS generator wrote last time. Pages now sit at the wiki
    # root, so a blind *.html sweep would also delete hand-added root files —
    # Google/Bing site-verification pages, CNAME, a hand-written robots.txt.
    # The manifest records what the build owns; everything else is left alone.
    for name in read_build_manifest(out):
        try:
            (out / name).unlink()
        except OSError:
            pass

    # The books' text is CC BY-SA 4.0, but "all artwork herein is
    # © 2026 by Lucie Arnoux" — maps are artwork. Drop the Maps page (and its
    # image extraction) unless a local build explicitly asks for it. The
    # images come off the PDF, so that page also needs the book at hand.
    maps_art = next((a for a in articles if a.get("kind") == "maps"), None)
    if maps_art and args.maps and maps_art["book"] not in docs:
        book = next(b for b in BOOKS if b["id"] == maps_art["book"])
        pdf = input_dir / book["filename"]
        if pdf.exists() and fitz is not None:
            docs[book["id"]] = fitz.open(str(pdf))
        else:
            print(f"  note: --maps needs {book['filename']} in {input_dir} — skipping maps")
            maps_art = None
    if not (args.maps and maps_art):
        articles = [a for a in articles if a.get("kind") != "maps"]

    # Site sheets already under <out>/sites/ — index only, no copy.
    sites = discover_sites(out)
    site_arts = site_articles(sites)
    articles.extend(site_arts)

    # Book pages claim their slugs first (a site named after a chapter
    # gets "-site" appended, not the other way round) …
    ensure_unique_slugs(articles)
    for art in site_arts:
        if art.get("site"):
            art["site"]["slug"] = art["slug"]
    # … and the campaign's own material trails the book material, so the
    # sidebar and home page lead with the wiki proper.
    if site_arts:
        articles = [
            a for a in articles if a.get("book") != SITES_BOOK_ID
        ] + site_arts

    print("Articles:")
    last_book = None
    for art in articles:
        if art.get("book") == SITES_BOOK_ID:
            continue  # listed separately below (no page range)
        if art.get("book") != last_book:
            last_book = art.get("book")
            print(f"  [{art.get('book_label') or last_book}]")
        if art.get("kind") == "arcana":
            continue
        print(
            f"  {art['start_page']:4d}-{art['end_page']:4d}  "
            f"{art['slug'][:42]:42s}  {art.get('kind') or 'article'}"
        )
    n_arc = sum(1 for a in articles if a.get("kind") == "arcana")
    print(f"  (+ {n_arc} individual arcana pages)")
    print(f"  Total pages: {len(articles)}")
    if sites:
        print(f"Sites ({SITES_OUT_DIRNAME}/):")
        for site in sites:
            extra = (
                f"  [{', '.join(v['label'] for v in site['variants'])}]"
                if site["variants"]
                else ""
            )
            print(f"  {site['href']}{extra}")

    # Page-number lookups stay per book: "page 270" means a different article
    # in Book I than in Book II.
    lookups = {
        bid: build_page_lookup([a for a in articles if a.get("book") == bid])
        for bid in book_ids
    }
    set_title_index(articles)

    # A translation of a page this build is not producing (a --books run that
    # leaves out its book) is dropped now, before anything writes an hreflang
    # link to a page that will not exist.
    built_slugs = {a["slug"] for a in articles}
    for locale in lang_targets:
        locale["pages"] = {
            slug: page
            for slug, page in locale["pages"].items()
            if slug in built_slugs
        }
    lang_targets = [t for t in lang_targets if t["pages"]]
    previews: dict[str, dict] = {}

    # Campaign maps + PDF map spreads (maps page only; Book II)
    map_images: list[dict] = []
    if maps_art and args.maps:
        print("Preparing maps…")
        map_images = prepare_map_images(
            docs[maps_art["book"]], maps_art, out / "images", input_dir
        )

    print("Indexing sections (for deep links)…")
    sections_by_slug: dict[str, list[dict]] = {}
    head_pages_by_slug: dict[str, list] = {}
    for art in articles:
        slug = art["slug"]
        book_id = art.get("book") or "book2"
        if art["kind"] not in TEXT_KINDS:  # sites, hubs, the maps page
            sections_by_slug[slug] = []
            continue
        lines, pages = texts[slug]
        lookup = lookups[book_id]
        common = dict(
            current_slug=slug,
            section_index=None,
            lookups=lookups,
            current_book=book_id,
        )
        if art["kind"] == "arcana" and art.get("arcana_type") == "minor":
            _body, _ex, sections = minor_arcana_html(
                lines, art["title"], lookup, articles, **common
            )
        elif art["kind"] == "arcana":
            _body, _ex, sections = major_arcana_html(
                lines, art["title"], lookup, articles, **common
            )
        else:
            # Which page each heading opened on, so an in-article "see page
            # 18" can name the section the reader is being sent to.
            head_pages_by_slug[slug] = heading_pages(lines, pages)
            _body, _ex, sections = article_html(
                lines, art["title"], lookup, articles, **common
            )
        ov = page_override(slug)
        if ov is not None:
            sections = override_sections(apply_override(ov, _body))
        sections_by_slug[slug] = sections

    section_navs: dict[str, list[dict]] = {}
    for art in articles:
        toc_labels = art.get("toc_labels") or []
        if not toc_labels:
            continue
        matched = match_toc_to_sections(
            toc_labels, sections_by_slug.get(art["slug"]) or []
        )
        if matched:
            section_navs[art["slug"]] = matched
    if section_navs:
        print(
            f"  Chapter section nav: {len(section_navs)} pages, "
            f"{sum(len(v) for v in section_navs.values())} deep links"
        )

    set_page_sections(
        build_page_section_map(sections_by_slug, head_pages_by_slug, articles)
    )

    # One section index per book, so a "page N" ref resolves to a section in
    # the book it was printed in.
    section_indexes = {
        bid: build_section_index(
            sections_by_slug, [a for a in articles if a.get("book") == bid]
        )
        for bid in book_ids
    }
    n_sec = sum(len(v) for v in sections_by_slug.values())
    print(f"  Indexed {n_sec} sections/monsters across {len(sections_by_slug)} pages")

    # Wiki page slug → sites that reference it (back-links), and the
    # titles the hub shows for the pages a site uses.
    titles_by_slug = {
        a["slug"]: a["title"]
        for a in articles
        if a.get("book") != SITES_BOOK_ID
    }
    print("Building pages…")
    search_docs: list[dict] = []
    # Each page's English body, kept so the localized pass can tell a
    # translation still matching its source from one gone stale.
    english_bodies: dict[str, str] = {}
    for art in articles:
        slug = art["slug"]
        book_id = art.get("book") or "book2"

        # Sites: sheets live outside the wiki — index and preview them,
        # but there is no page of our own to write.
        if art["kind"] in ("site", "sites-hub"):
            if art["kind"] == "site":
                site = art["site"]
                excerpt = site["excerpt"]
                search_text = site["text"]
            else:
                body = sites_hub_html(site_arts[1:], titles_by_slug)
                body = (
                    f'<h1 class="page-title">'
                    f'{html.escape(art["title"])}</h1>\n' + body
                )
                n = len(site_arts) - 1
                excerpt = (
                    f"{n} campaign adventure site{'' if n == 1 else 's'} — "
                    "prep, rooms, stat blocks — kept beside the wiki."
                )
                (out / f"{slug}.html").write_text(
                    page_shell(
                        art["title"],
                        slug,
                        body,
                        articles,
                        rel_prefix="",
                        section_navs=section_navs,
                        description=excerpt,
                        alternates=alternates_for(
                            slug, lang_source, lang_targets
                        ),
                    ),
                    encoding="utf-8",
                )
                search_text = html_to_search_text(body)
            previews[slug] = {
                "title": art["title"],
                "excerpt": excerpt,
                "image": None,
                "book": SITES_BOOK_ID,
                "sections": {},
            }
            doc_entry = {
                "slug": slug,
                "title": art["title"],
                "book": SITES_BOOK_ID,
                "excerpt": (excerpt or "")[:280],
                "text": f"{art['title']}\n{search_text}"[:80_000],
            }
            if art.get("href"):
                # Root-relative; wiki.js re-bases it for pages/ (see hrefFromRoot)
                doc_entry["href"] = art["href"]
            search_docs.append(doc_entry)
            continue

        lookup = lookups[book_id]
        section_index = section_indexes[book_id]
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
                rel_prefix="",
                section_navs=section_navs,
                content_class="content maps-page",
                description=excerpt,
                alternates=alternates_for(slug, lang_source, lang_targets),
            )
            excerpt = (
                "Maps of Stonetop, the vicinity, and the World's End — "
                "campaign sheets and PDF spreads."
            )
        elif art.get("kind") == "arcana-hub":
            body = arcana_hub_html(art)
            body = (
                f'<h1 class="page-title">'
                f'{html.escape(art["title"])}</h1>\n' + body
            )
            excerpt = (
                f"Index of {len(art.get('children') or [])} individual arcana entries."
            )
            page_html = page_shell(
                art["title"],
                slug,
                body,
                articles,
                rel_prefix="",
                section_navs=section_navs,
                description=excerpt,
                alternates=alternates_for(slug, lang_source, lang_targets),
            )
        else:
            lines, _pages = texts[slug]
            common = dict(
                current_slug=slug,
                section_index=section_index,
                lookups=lookups,
                section_indexes=section_indexes,
                current_book=book_id,
            )
            if art.get("kind") == "arcana" and art.get("arcana_type") == "minor":
                body, excerpt, _secs = minor_arcana_html(
                    lines, art["title"], lookup, articles, **common
                )
            elif art.get("kind") == "arcana":
                body, excerpt, _secs = major_arcana_html(
                    lines, art["title"], lookup, articles, **common
                )
            else:
                body, excerpt, _secs = article_html(
                    lines, art["title"], lookup, articles, **common
                )
            # A hand-authored body (pages/<slug>.html) replaces the extraction.
            ov = page_override(slug)
            if ov is not None:
                body = apply_override(ov, body)
                excerpt = override_excerpt(ov) or excerpt
            # A split chapter: the hub lists its parts, and each part links
            # back the way an arcanum does.
            if art.get("children") and art.get("kind") == "article":
                body = body + "\n" + chapter_parts_html(art)
            # A split chapter's parts (the playbooks and inserts) are
            # handouts: each opens with its own name and no way back to the
            # chapter — the sidebar already carries that.
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
                card_no = (
                    f'<span class="arcana-card-no">'
                    f'{html.escape(hub_title)} {art["number"]}</span>'
                    if art.get("number")
                    else ""
                )
                body = (
                    f'<p class="arcana-back"><a class="wiki-link" href="{hub}.html" '
                    f'data-slug="{hub}">← All {hub_title}</a>{card_no}</p>\n'
                    + body
                )
            # Every page opens with its own title. An arcanum is the exception:
            # the card carries its name on its face, in its own type.
            if art.get("kind") != "arcana" and body:
                cls = "page-title"
                if 'class="pb-stats"' in body:
                    cls += " pb-title"
                head = (
                    f'<h1 class="{cls}">'
                    f'{html.escape(art.get("title") or "")}</h1>'
                )
                # A back-link is chrome above the title, not part of the page.
                lead, sep, rest = body.partition("</p>\n")
                if sep and 'class="arcana-back"' in lead:
                    body = lead + sep + head + "\n" + rest
                else:
                    body = head + "\n" + body
            page_html = page_shell(
                display_title(art),
                slug,
                body,
                articles,
                rel_prefix="",
                section_navs=section_navs,
                description=excerpt,
                # A character sheet reads differently: a ticked box there
                # means a move you have, not one you are done with.
                content_class=(
                    "content playbook" if is_sheet_body(body) else "content"
                ),
                alternates=alternates_for(slug, lang_source, lang_targets),
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
            "title": display_title(art),
            "excerpt": excerpt,
            "image": thumb,
            "book": book_id,
            "sections": section_blocks,
        }
        if card_preview_html:
            # Card number lives outside the card on full pages (nav chrome);
            # bake it into the preview HTML so hover popups show it too.
            if art.get("number"):
                hub_title = (
                    "Minor Arcana"
                    if art.get("arcana_type") == "minor"
                    else "Major Arcana"
                )
                card_preview_html = (
                    f'<span class="arcana-preview-no">'
                    f"{html.escape(hub_title)} {art['number']}</span>\n"
                    + card_preview_html
                )
            previews[slug]["html"] = card_preview_html
            previews[slug]["kind"] = "arcana"
            if art.get("number") is not None:
                previews[slug]["number"] = art["number"]
                previews[slug]["arcana_type"] = art.get("arcana_type") or ""
        (out / f"{slug}.html").write_text(page_html, encoding="utf-8")
        english_bodies[slug] = body

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
                "title": display_title(art),
                "book": book_id,
                "excerpt": (excerpt or "")[:280],
                "text": combined,
            }
        )

    previews_json = json.dumps(previews, ensure_ascii=False, indent=2)

    page_maps: dict[str, dict] = {}
    for bid, book_lookup in lookups.items():
        page_map: dict[str, dict] = {}
        for pnum, art in book_lookup.items():
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
        page_maps[bid] = page_map
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

    page_files = ["index.html"] + [
        f"{a['slug']}.html" for a in articles if not a.get("href")
    ]
    # A language directory this build no longer produces is removed whole,
    # so dropping a language from langs.json takes its pages off the site.
    for code in prune_language_dirs(out, lang_targets):
        print(f"  i18n: removed stale {code}/")
    localized = write_localized_pages(
        out,
        articles,
        section_navs,
        english_bodies,
        lang_source,
        lang_targets,
    )
    write_sitemap(out, articles, base_url=args.base_url, extra=localized)
    write_robots(out, base_url=args.base_url)
    write_build_manifest(out, page_files + ["sitemap.xml", "robots.txt"])
    print(
        f"Done in {time.perf_counter() - t_start:.1f}s. "
        f"Open {out / 'index.html'}"
    )

"""
The build: command line, the two phases (extract → corpus, corpus → wiki),
and the search/preview indexes.
"""

from __future__ import annotations

import argparse
import html
import json
import re
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
    BESTIARY_SLUG,
    MAPS_STUB_EXCERPT,
    bestiary_article,
    bestiary_entries,
    bestiary_excerpt,
    bestiary_html,
    maps_body_html,
    maps_stub_html,
    override_excerpt,
    override_sections,
    page_override,
    page_shell,
    read_build_manifest,
    write_build_manifest,
    home_alternates,
    write_index_custom,
    write_llms_txt,
    write_localized_arcana_hubs,
    write_localized_index,
    write_localized_pages,
    write_robots,
    write_sitemap,
)
from . import blocks as blockdata
from .coverage import report as report_unshown
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
from .structure import (
    article_html,
    build_page_section_map,
    build_section_index,
    extract_section_html_blocks,
    match_toc_to_sections,
    set_page_sections,
    set_title_index,
    linkify_pages,
)
from .text import TAG_RE, heading_pages, html_to_search_text, strip_tags
from .translate import TextMemory, check_alignment, render_translated, tag_lines
from .reflinks import apply_reference_links


class BuildClock:
    """Per-phase wall-clock for the build report.

    ``phase(name)`` closes the running phase and opens the next; ``lap(bucket,
    key, t0)`` adds a page's or a language's cost to a named bucket, so the
    report can list the slowest pages and the cost of each language.
    """

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.phases: list[tuple[str, float]] = []
        self.buckets: dict[str, dict[str, float]] = {}
        self._name: str | None = None
        self._t0 = time.perf_counter()

    def phase(self, name: str | None) -> None:
        now = time.perf_counter()
        if self._name is not None:
            self.phases.append((self._name, now - self._t0))
        self._name, self._t0 = name, now

    def lap(self, bucket: str, key: str, t0: float) -> None:
        if self.enabled:
            b = self.buckets.setdefault(bucket, {})
            b[key] = b.get(key, 0.0) + (time.perf_counter() - t0)

    def report(self, total: float) -> None:
        self.phase(None)
        print("Profile:")
        for name, secs in self.phases:
            print(f"  {secs:7.2f}s  {100 * secs / total:5.1f}%  {name}")
        for bucket, laps in self.buckets.items():
            top = sorted(laps.items(), key=lambda kv: -kv[1])
            print(f"  {bucket} ({sum(laps.values()):.2f}s over {len(laps)}):")
            for key, secs in top[:12]:
                print(f"    {secs:7.2f}s  {key}")


def arcana_back_html(art: dict, ui: dict | None = None) -> str:
    """The link an arcanum's page opens with, back to its hub, and the
    number printed on the card — in the page's language when ``ui`` has it."""
    words = ((ui or {}).get("sheet") or {}).get("arcana") or {}
    minor = art.get("arcana_type") == "minor"
    hub_title = words.get("minor" if minor else "major") or (
        "Minor Arcana" if minor else "Major Arcana"
    )
    back = words.get("all_minor" if minor else "all_major") or f"All {hub_title}"
    hub = art["hub_slug"]
    card_no = (
        f'<span class="arcana-card-no">{html.escape(hub_title)} {art["number"]}</span>'
        if art.get("number")
        else ""
    )
    return (
        f'<p class="arcana-back"><a class="wiki-link" href="{hub}.html" '
        f'data-slug="{hub}">← {html.escape(back)}</a>{card_no}</p>\n'
    )


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
            "Wiki folder. Static chrome (css/, js/wiki.js, images/icons/) "
            "lives here and is left in place; the build writes the page "
            "HTML, indexes, and map images. "
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
        "--pages",
        nargs="+",
        default=None,
        metavar="SLUG",
        help=(
            "Write only these pages (e.g. marshedge the-maw), in English and "
            "in every language that translates them. Everything is still read "
            "and indexed, so links and deep links are right, but the site-wide "
            "files (index.html, the search index, hover previews, sitemap) and "
            "every other page are left as they are. Handy while translating."
        ),
    )
    p.add_argument(
        "--profile",
        action="store_true",
        help=(
            "Print where the build's time goes: each phase, the slowest "
            "pages to render, and the cost of each language."
        ),
    )
    p.add_argument(
        "--seed-blocks",
        action="store_true",
        help=(
            "Rewrite blocks.json from what this build produced, instead of "
            "checking against it. Run it after a change that is meant to "
            "alter the blocks, and read the diff."
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

def page_link_fn(lookup: dict[int, dict], common: dict):
    """``linkify_pages`` bound to one page, for the sheet renderer."""
    return lambda text: linkify_pages(
        text,
        lookup,
        common.get("current_slug"),
        common.get("section_index"),
        lookups=common.get("lookups"),
        section_indexes=common.get("section_indexes"),
        current_book=common.get("current_book"),
    )


def _recorded_previews(out: Path) -> dict:
    """The page previews the last full build wrote to ``js/previews-data.js``
    (empty when there is none yet)."""
    path = out / "js" / "previews-data.js"
    try:
        text = path.read_text(encoding="utf-8")
        start = text.index("=") + 1
        end = text.index(";\nwindow.WIKI_PAGE_MAP")
        return json.loads(text[start:end])
    except (OSError, ValueError):
        return {}


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
    clock = BuildClock(args.profile)
    clock.phase("read corpus / extract")
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
    clock.phase("load translations")
    lang_source, lang_targets = load_locales(only_langs)
    clock.phase("prepare")

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
    # --pages writes only the named pages and leaves the rest of the site
    # alone, so it must not sweep: the manifest lists every page the last
    # full build wrote, and wiping those would take the whole wiki out with
    # the three pages being rebuilt (and --pages doesn't rewrite the
    # manifest, so the next run would do it again).
    if not args.pages:
        for name in read_build_manifest(out):
            try:
                (out / name).unlink()
            except OSError:
                pass

    # The books' text is CC BY-SA 4.0, but "all artwork herein is
    # © 2026 by Lucie Arnoux" — maps are artwork. The map *images* are only
    # extracted when a local build asks for them (--maps, which needs the book
    # at hand). The Maps page itself always stands: Book II's first page sends
    # the reader to it, so without one both that reference and the chapter go
    # missing. With no images it renders as a stub (maps_stub_html) — what the
    # two spreads cover, and every place they label, linked.
    maps_art = next((a for a in articles if a.get("kind") == "maps"), None)
    draw_maps = bool(maps_art and args.maps)
    if draw_maps and maps_art["book"] not in docs:
        book = next(b for b in BOOKS if b["id"] == maps_art["book"])
        pdf = input_dir / book["filename"]
        if pdf.exists() and fitz is not None:
            docs[book["id"]] = fitz.open(str(pdf))
        else:
            print(
                f"  note: --maps needs {book['filename']} in {input_dir}"
                " — the Maps page falls back to its text stub"
            )
            draw_maps = False

    # The bestiary: the wiki's own index of every stat block, filed after
    # Book II's last page. Rendered after every other page, since
    # it is made of theirs.
    book2_last = max(
        (i for i, a in enumerate(articles) if a.get("book") == "book2"),
        default=None,
    )
    if book2_last is not None:
        articles.insert(book2_last + 1, bestiary_article(articles[book2_last]))

    ensure_unique_slugs(articles)

    # --pages: write these slugs and nothing else. The build still reads and
    # indexes every article, so cross-page links and deep links stay right.
    only_pages = set(args.pages) if args.pages else None
    if only_pages:
        unknown = only_pages - {a["slug"] for a in articles}
        if unknown:
            raise SystemExit(
                "Unknown page(s): " + ", ".join(sorted(unknown))
            )

    print("Articles:")
    last_book = None
    for art in articles:
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
        if wanted is None:
            # Every book is being built, so a translation with no page is an
            # orphan (a renamed slug, a typo in the file name), not a filter.
            for slug in sorted(set(locale["pages"]) - built_slugs):
                print(
                    f"  ERROR: {locale['code']}/{slug} NOT PUBLISHED: no English"
                    " page has that slug (renamed, or a misnamed translation file?)"
                )
        locale["pages"] = {
            slug: page
            for slug, page in locale["pages"].items()
            if slug in built_slugs
        }
    lang_targets = [t for t in lang_targets if t["pages"]]
    # What each language calls the pages it has, so a link from one
    # translated page to another reads in that language.
    for locale in lang_targets:
        titles: dict[str, str] = {}
        for s_slug, page in locale["pages"].items():
            t = page.get("title") or (
                ((page.get("corpus") or {}).get("meta") or {}).get("title")
            )
            if t:
                titles[s_slug] = t
        locale["titles"] = titles
    previews: dict[str, dict] = {}

    # Campaign maps + PDF map spreads (maps page only; Book II)
    map_images: list[dict] = []
    if draw_maps:
        print("Preparing maps…")
        map_images = prepare_map_images(
            docs[maps_art["book"]], maps_art, out / "images", input_dir
        )

    # What each page is supposed to hold (blocks.json). A seeding run
    # builds without it, to record what the renderer makes on its own.
    listed_blocks = {} if args.seed_blocks else blockdata.load()
    print("Indexing sections (for deep links)…")
    clock.phase("index sections (first render of every page)")
    sections_by_slug: dict[str, list[dict]] = {}
    head_pages_by_slug: dict[str, list] = {}
    for art in articles:
        slug = art["slug"]
        book_id = art.get("book") or "book2"
        if art["kind"] not in TEXT_KINDS:  # hubs, the maps page
            sections_by_slug[slug] = []
            continue
        lines, pages = texts[slug]
        lookup = lookups[book_id]
        t_page = time.perf_counter()
        tagged, _units = tag_lines(lines)
        common = dict(
            current_slug=slug,
            section_index=None,
            lookups=lookups,
            current_book=book_id,
        )
        if art["kind"] == "arcana" and art.get("arcana_type") == "minor":
            _body, _ex, sections = minor_arcana_html(
                tagged, art["title"], lookup, articles, **common
            )
        elif art["kind"] == "arcana":
            _body, _ex, sections = major_arcana_html(
                tagged, art["title"], lookup, articles, **common
            )
        else:
            # Which page each heading opened on, so an in-article "see page
            # 18" can name the section the reader is being sent to.
            head_pages_by_slug[slug] = heading_pages(lines, pages)
            _body, _ex, sections = article_html(
                tagged, art["title"], lookup, articles,
                # The same blocks the page will be built with, or a block
                # that exists only because blocks.json says so has no
                # section to be linked to.
                page_blocks=blockdata.for_slug(listed_blocks, book_id, slug),
                **common,
            )
        ov = page_override(slug)
        if ov is not None:
            sections = override_sections(
                apply_override(
                    ov, _body, slug=slug, link_fn=page_link_fn(lookup, common)
                )
            )
        sections_by_slug[slug] = sections
        clock.lap("index: slowest pages", slug, t_page)

    clock.phase("section navs / indexes")
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

    print("Building pages…")
    clock.phase("build english pages (second render)")
    search_docs: list[dict] = []
    n_unshown = 0
    n_drift = 0
    # What each page is supposed to hold (blocks.json), and what it held.
    built_blocks: dict[str, dict[str, list[dict]]] = {}
    # Each page's English body, kept so the localized pass can tell a
    # translation still matching its source from one gone stale.
    english_bodies: dict[str, str] = {}
    for art in articles:
        slug = art["slug"]
        if art.get("kind") == "bestiary":
            continue  # written below, once every stat block is known
        # --pages: only the named pages are rendered; the rest of the site
        # keeps the HTML it already has. The sidebar of every page written
        # still names this page's sections in each language, so take the
        # labels off the translation's headings without rendering it.
        if only_pages is not None and slug not in only_pages:
            secs = section_navs.get(slug)
            if secs and slug in texts:
                en_lines = texts[slug][0]
                for locale in lang_targets:
                    tr = locale["pages"].get(slug)
                    if not tr or "corpus" not in tr or "book" not in tr["corpus"]:
                        continue
                    tr_lines = tr["corpus"]["book"][0]
                    if check_alignment(en_lines, tr_lines):
                        continue
                    tm = TextMemory.from_lines(en_lines, tr_lines)
                    tr["sections"] = {
                        sec["id"]: tm.get(sec["name"]) or sec["name"] for sec in secs
                    }
            continue
        book_id = art.get("book") or "book2"

        lookup = lookups[book_id]
        section_index = section_indexes[book_id]
        t_page = time.perf_counter()
        body = ""
        excerpt = ""
        card_preview_html = None

        if art["kind"] == "maps":
            body = maps_body_html(map_images)
            drawn = bool(body)
            if drawn:
                excerpt = (
                    "Maps of Stonetop, the vicinity, and the World's End — "
                    "campaign sheets and PDF spreads."
                )
            else:
                body = maps_stub_html(articles)
                excerpt = MAPS_STUB_EXCERPT
            page_html = page_shell(
                art["title"],
                slug,
                body,
                articles,
                rel_prefix="",
                section_navs=section_navs,
                content_class="content maps-page" if drawn else "content",
                description=excerpt,
                alternates=alternates_for(slug, lang_source, lang_targets),
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
                # A hub is generated into every language directory rather than
                # translated, so its cluster is named rather than looked up.
                alternates=alternates_for(
                    slug,
                    lang_source,
                    lang_targets,
                    have=[t for t in lang_targets if t.get("pages")],
                ),
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
            # The lines carry provenance tags (see translate.tag_lines): the
            # English page is built from the same tagged lines its
            # translations are, so the two are analysed the same way.
            tagged, _units = tag_lines(lines)
            if art.get("kind") == "arcana" and art.get("arcana_type") == "minor":
                body, excerpt, _secs = minor_arcana_html(
                    tagged, art["title"], lookup, articles, **common
                )
            elif art.get("kind") == "arcana":
                body, excerpt, _secs = major_arcana_html(
                    tagged, art["title"], lookup, articles, **common
                )
            else:
                body, excerpt, _secs = article_html(
                    tagged, art["title"], lookup, articles,
                    page_blocks=blockdata.for_slug(
                        listed_blocks, book_id, slug
                    ),
                    **common,
                )
                # The works a page cites, linked (links/<slug>.json).
                body, unlinked = apply_reference_links(body, slug)
                if unlinked:
                    print(f"  note: {slug}: reference links not placed: " + "; ".join(unlinked))
            if TAG_RE.search(body) or TAG_RE.search(excerpt):
                # Text reached the HTML without going through T(). It is
                # English here, so nothing is lost but the tags — and a
                # translation of the same page would show English there.
                print(f"  note: {slug}: {len(TAG_RE.findall(body))} provenance tags leaked into the HTML")
                body, excerpt = strip_tags(body), strip_tags(excerpt)
            # Every line of the book should be on the page it built; what
            # is missing is a layout the extractor or the renderer got wrong,
            # and it is never silent (generator/coverage.py).
            # (not on a hand-authored sheet: pages/<slug>.txt replaces the
            # extraction by design)
            if page_override(slug) is None:
                n_unshown += report_unshown(slug, lines, body, title=art["title"])
            # Formatting sentinels are the renderer's private encoding; one
            # reaching the HTML shows up as a stray box in the reader's text.
            leaked = len(re.findall("[\x02-\x07]", body))
            if leaked:
                print(f"  WARNING: {slug}: {leaked} raw formatting marker(s) in the HTML")
            # …and every block it is supposed to hold is on it, with nothing
            # that is not listed (blocks.json).
            found = blockdata.found_in_html(body)
            built_blocks.setdefault(book_id, {})[slug] = found
            if listed_blocks:
                want = blockdata.for_slug(listed_blocks, book_id, slug)
                gone, new = blockdata.drift(want, found)
                for label, items in (("missing", gone), ("unlisted", new)):
                    if items:
                        n_drift += len(items)
                        print(
                            f"  WARNING: {slug}: {len(items)} {label} "
                            f"block(s): " + "; ".join(items[:3])
                            + (" …" if len(items) > 3 else "")
                        )
                for start in blockdata.missing_starts(want, lines):
                    n_drift += 1
                    print(
                        f"  WARNING: {slug}: blocks.json names a start line "
                        f"that is not in the corpus: {start[:60]}"
                    )
            # A hand-authored body (pages/<slug>.html) replaces the extraction.
            ov = page_override(slug)
            if ov is not None:
                body = apply_override(
                    ov, body, slug=slug, link_fn=page_link_fn(lookup, common)
                )
                excerpt = override_excerpt(ov) or excerpt
            clock.lap("build: slowest english renders", slug, t_page)
            # The same page in every language that has its corpus translated.
            for locale in lang_targets:
                tr = locale["pages"].get(slug)
                if not tr or "corpus" not in tr:
                    continue
                t_tr = time.perf_counter()
                if art.get("kind") not in ("article", "arcana"):
                    print(f"  i18n: {locale['code']}/{slug}: only articles, arcana and sheets render from a corpus translation yet")
                    del locale["pages"][slug]
                    continue
                page_tr, notes = render_translated(
                    tr["corpus"], locale["code"], art, lines, _pages, ov,
                    lookup, articles, common, ui=locale.get("ui"),
                    titles=locale.get("titles"),
                    page_blocks=blockdata.for_slug(listed_blocks, book_id, slug),
                )
                for note in notes:
                    print(f"  i18n: {note}")
                if page_tr is None:
                    print(
                        f"  ERROR: {locale['code']}/{slug} NOT PUBLISHED: the translation"
                        " does not align with the English (see the problems above);"
                        f" fix it and re-run: python i18n/corpus_xlate.py apply {locale['code']} {slug}"
                    )
                    del locale["pages"][slug]
                    continue
                if art.get("children") and art.get("kind") == "article":
                    page_tr["body_html"] += "\n" + chapter_parts_html(art)
                if art.get("kind") == "arcana" and art.get("hub_slug"):
                    page_tr["body_html"] = (
                        arcana_back_html(art, locale.get("ui")) + page_tr["body_html"]
                    )
                tr.update(page_tr)
                clock.lap("build: translated renders per language", locale["code"], t_tr)
                clock.lap("build: slowest translated renders", f"{locale['code']}/{slug}", t_tr)
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
                body = arcana_back_html(art) + body
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
            t_shell = time.perf_counter()
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
            clock.lap("build: page_shell (english)", "all pages", t_shell)

        t_page = time.perf_counter()
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
        if only_pages is None or slug in only_pages:
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
        clock.lap("build: previews + search text", "all pages", t_page)

    if n_unshown:
        print(f"  {n_unshown} line(s) of book text not shown, in total")
    if args.seed_blocks:
        # Keep the hand-written start/end entries: a block that exists
        # only because the file says so is not in a seeding build.
        built_blocks = blockdata.merge(blockdata.load(), built_blocks)
        path = blockdata.save(built_blocks)
        n_blocks = sum(
            len(v) for book in built_blocks.values() for v in book.values()
        )
        print(f"  Wrote {path.name}: {n_blocks} blocks — check `git diff`")
    elif n_drift:
        print(f"  {n_drift} block(s) differ from blocks.json, in total")

    bestiary = next((a for a in articles if a.get("kind") == "bestiary"), None)
    if bestiary:
        clock.phase("bestiary")
        known = previews
        if only_pages is not None:
            # A --pages run renders only the pages it names, and the bestiary
            # is made of every page's stat blocks: take the rest from what the
            # last full build recorded (previews-data.js is not rewritten by a
            # partial build, so it is still that build's).
            known = {**_recorded_previews(out), **previews}
        entries = bestiary_entries(articles, known)
        body = bestiary_html(entries)
        excerpt = bestiary_excerpt(len(entries))
        if only_pages is None or BESTIARY_SLUG in only_pages:
            (out / f"{BESTIARY_SLUG}.html").write_text(
                page_shell(
                    bestiary["title"],
                    BESTIARY_SLUG,
                    body,
                    articles,
                    rel_prefix="",
                    section_navs=section_navs,
                    description=excerpt,
                    alternates=alternates_for(
                        BESTIARY_SLUG, lang_source, lang_targets
                    ),
                ),
                encoding="utf-8",
            )
        previews[BESTIARY_SLUG] = {
            "title": display_title(bestiary),
            "excerpt": excerpt,
            "image": None,
            "book": bestiary.get("book"),
            "sections": {},
        }
        search_docs.append(
            {
                "slug": BESTIARY_SLUG,
                "title": display_title(bestiary),
                "book": bestiary.get("book"),
                "excerpt": excerpt[:280],
                # The names alone: the blocks' full text is already indexed
                # on the pages they come from, and a search should lead there.
                "text": bestiary["title"]
                + "\n"
                + " ".join(e["name"] for e in entries),
            }
        )
        print(f"  Bestiary: {len(entries)} stat blocks")

    clock.phase("write previews / search index / home")
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
    if only_pages is None:
        (out / "js" / "previews-data.js").write_text(
            "window.WIKI_PREVIEWS = "
            + previews_json
            + ";\nwindow.WIKI_PAGE_MAP = "
            + page_map_json
            + ";\n",
            encoding="utf-8",
        )
    search_json = json.dumps(search_docs, ensure_ascii=False, separators=(",", ":"))
    if only_pages is None:
        (out / "js" / "search-index.js").write_text(
            "window.WIKI_SEARCH_INDEX = " + search_json + ";\n",
            encoding="utf-8",
        )
        print(f"  Search index: {len(search_docs)} pages, {len(search_json)//1024} KB")
        write_index_custom(
            articles,
            previews,
            out / "index.html",
            alternates=home_alternates(lang_source, lang_targets),
        )

    page_files = ["index.html"] + [
        f"{a['slug']}.html" for a in articles
    ]
    # A language directory this build no longer produces is removed whole,
    # so dropping a language from langs.json takes its pages off the site.
    if only_pages is None:
        for code in prune_language_dirs(out, lang_targets):
            print(f"  i18n: removed stale {code}/")
    clock.phase("write localized pages")
    localized = write_localized_pages(
        out,
        articles,
        section_navs,
        english_bodies,
        lang_source,
        lang_targets,
        only_pages=only_pages,
        previews=previews,
        search_docs=search_docs,
        sections_by_slug=sections_by_slug,
        page_maps=page_maps,
    )
    if only_pages is None:
        # The home page lists every page, so it needs the whole run's
        # previews — a --pages run has only the pages it rebuilt.
        clock.phase("write localized home pages")
        localized += write_localized_index(
            out, articles, previews, lang_source, lang_targets
        )
        localized += write_localized_arcana_hubs(
            out, articles, section_navs, lang_source, lang_targets
        )
        clock.phase("sitemap / robots / llms.txt / manifest")
        write_sitemap(out, articles, base_url=args.base_url, extra=localized)
        write_robots(out, base_url=args.base_url)
        write_llms_txt(
            out,
            articles,
            previews,
            base_url=args.base_url,
            languages=[t["code"] for t in lang_targets],
        )
        write_build_manifest(
            out, page_files + ["sitemap.xml", "robots.txt", "llms.txt"]
        )
    if args.profile:
        clock.report(time.perf_counter() - t_start)
    print(
        f"Done in {time.perf_counter() - t_start:.1f}s. "
        f"Open {out / 'index.html'}"
    )

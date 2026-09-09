"""
Translations as checked-in data: ``i18n/langs.json``, UI strings, and one
translated page per ``i18n/pages/<code>/<slug>.json``.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
from pathlib import Path

from . import REPO_ROOT
from .translate import load_corpus_translations


# ------------------------------------------------------------------ i18n
#
# Translations are *data*, not something the build re-derives: they live in
# i18n/ beside this script, are checked in, and are keyed by page slug. The
# build reads them and lays each language out in its own directory under the
# wiki root — /de/welcome-to-the-worlds-end.html beside the English page at
# the root. Subdirectories (rather than subdomains or ccTLDs) keep every
# language on one domain, so links earned by the English pages lift the
# translations too, and GitHub Pages serves them with no configuration.
#
# Slugs and section ids are *not* translated: one URL shape across the site,
# deep links that survive a language switch, and — because the wiki keys a
# reader's ticked boxes and answers by slug — a checkbox that stays ticked
# when the same reader moves between a page and its translation.

I18N_DIRNAME = "i18n"
UI_FALLBACK = {
    "skip_to_content": "Skip to content",
    "toggle_nav": "Toggle navigation",
    "search_placeholder": "Search wiki…",
    "search_label": "Search wiki",
    "nav_label": "Topics",
    "credit": "Text from {work} by Jeremy Strandberg, {license}",
    "dice_sound": "Dice sound",
    "language": "Language",
    "translated_note": "",
    "view_original": "",
    "english_only": "",
    "books": {},
}


def i18n_dir() -> Path:
    return REPO_ROOT / I18N_DIRNAME


def load_locales(only: list[str] | None = None) -> tuple[dict, list[dict]]:
    """Read ``i18n/langs.json`` plus each language's UI strings and pages.

    Returns ``(source, targets)``. A target carries ``ui`` (chrome strings)
    and ``pages`` (translated page bodies keyed by slug).

    A language with no translated page is dropped. A directory of pages that
    are really still English is exactly the thin, machine-shaped content
    search engines discount — and it strands a reader inside a shell they
    cannot read. Partial coverage is fine and expected; empty coverage is not
    published at all.
    """
    root = i18n_dir()
    try:
        table = json.loads((root / "langs.json").read_text(encoding="utf-8"))
    except OSError:
        return {}, []
    source = table.get("source") or {}
    targets: list[dict] = []
    for lang in table.get("targets") or []:
        code = lang.get("code")
        if not code or (only is not None and code not in only):
            continue
        ui = dict(UI_FALLBACK)
        try:
            ui.update(
                json.loads(
                    (root / "ui" / f"{code}.json").read_text(encoding="utf-8")
                )
            )
        except OSError:
            print(f"  i18n: {code} has no UI strings — skipped")
            continue
        pages: dict[str, dict] = {}
        for path in sorted((root / "pages" / code).glob("*.json")):
            try:
                page = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"  i18n: {code}/{path.name} unreadable ({exc})")
                continue
            if page.get("slug") and page.get("body_html"):
                pages[page["slug"]] = page
        # Translations of the corpus (i18n/corpus/<code>/…): rendered by the
        # build from the English lines, so only their meta is known here.
        # One outranks a JSON page of the same slug.
        for slug, entry in load_corpus_translations(code).items():
            meta = entry.get("meta") or {}
            pages[slug] = {
                "slug": slug,
                "corpus": entry,
                "title": meta.get("title") or "",
                "nav_label": meta.get("nav_label") or meta.get("title") or "",
                "description": meta.get("description") or "",
            }
        if not pages:
            continue
        entry = dict(lang)
        entry["ui"] = ui
        entry["pages"] = pages
        targets.append(entry)
    return source, targets


def prune_language_dirs(out: Path, keeping: list[dict]) -> list[str]:
    """Delete language directories this build is not going to write.

    Only directories named by ``langs.json`` are touched, so nothing else at
    the wiki root — ``css/``, ``sites/``, a hand-added folder — is at risk.
    """
    root = i18n_dir()
    try:
        table = json.loads((root / "langs.json").read_text(encoding="utf-8"))
    except OSError:
        return []
    known = {t.get("code") for t in table.get("targets") or []}
    keep = {t["code"] for t in keeping}
    removed = []
    for code in sorted(known - keep):
        if code and (out / code).is_dir():
            shutil.rmtree(out / code)
            removed.append(code)
    return removed


def body_source_sha(body_html: str) -> str:
    """Fingerprint of an English page body, as stored in a translation file.

    A translation records the hash of the English it was made from. When the
    books are re-extracted and a page's text moves, the hash stops matching
    and the build says so — which is the difference between a stale
    translation nobody noticed and one on a list to redo.
    """
    return hashlib.sha256(body_html.encode("utf-8")).hexdigest()


def alternates_for(slug: str, source: dict, targets: list[dict]) -> list[dict]:
    """The hreflang cluster for one page: English first, then each language
    that actually has this page translated. Empty when nothing is translated —
    a page with one language needs no cluster."""
    have = [t for t in targets if slug in t["pages"]]
    if not have:
        return []
    alts = [
        {
            "hreflang": source.get("code") or "en",
            "path": f"{slug}.html",
            "code": source.get("code") or "en",
            "endonym": source.get("endonym") or "English",
        }
    ]
    for t in have:
        alts.append(
            {
                "hreflang": t["code"],
                "path": f"{t['code']}/{slug}.html",
                "code": t["code"],
                "endonym": t.get("endonym") or t["code"],
            }
        )
    return alts


def lang_switch_html(
    alternates: list[dict], current_code: str, ui: dict, *, rel_prefix: str
) -> str:
    """Sidebar language picker: plain links, sorted by endonym.

    Plain crawlable links, and no redirect on Accept-Language anywhere — a
    crawler arrives with ``Accept-Language: en`` from a US address, so a site
    that redirects readers by header shows the crawler nothing but English and
    the translations never get indexed. The reader chooses; the page never
    chooses for them.
    """
    if not alternates:
        return ""
    ordered = sorted(alternates, key=lambda a: a["endonym"].casefold())
    current = next(
        (a for a in alternates if a["code"] == current_code), alternates[0]
    )
    rows = []
    for alt in ordered:
        # Root pages sit one level up from a language directory; a language
        # page is a sibling of the directory the reader is already in.
        href = rel_prefix + alt["path"] if alt["code"] != current_code else ""
        if alt["code"] == current_code:
            rows.append(
                f'<li><span class="lang-current" aria-current="page" '
                f'lang="{html.escape(alt["code"])}">'
                f'{html.escape(alt["endonym"])}</span></li>'
            )
        else:
            rows.append(
                f'<li><a href="{html.escape(href)}" '
                f'hreflang="{html.escape(alt["code"])}" '
                f'lang="{html.escape(alt["code"])}">'
                f'{html.escape(alt["endonym"])}</a></li>'
            )
    label = html.escape(ui.get("language") or UI_FALLBACK["language"])
    # Written into the sidebar footer; wiki.js moves it up into the tools row
    # beside the dice-sound icon, where CSS compacts it to globe + code. Both
    # spans ship either way — the label is the control's accessible name once
    # the row hides it, and the code is what an icon-sized control can show.
    short = html.escape(current["code"].split("-")[0].upper())
    return (
        f'<details class="lang-switch">'
        f'<summary title="{label}">'
        f'<span class="lang-globe" aria-hidden="true">\U0001f310</span>'
        f'<span class="lang-code" aria-hidden="true">{short}</span>'
        f'<span class="lang-label">{label}</span>'
        f'<span class="lang-now">{html.escape(current["endonym"])}</span>'
        f"</summary>"
        f'<ul class="lang-list">{"".join(rows)}</ul>'
        f"</details>"
    )


def localize_body_links(body_html: str, translated_slugs: set[str]) -> str:
    """Re-base a translated body's wiki links for its language directory.

    A translated body stores its links the way the English page does
    (``href="the-golden-oak.html"``). From inside ``/de/`` that has to become
    ``../the-golden-oak.html`` — unless the target is itself translated into
    this language, in which case the sibling in the same directory is the
    better page to send the reader to.
    """

    def fix(m: re.Match) -> str:
        slug = m.group(1)
        if slug in translated_slugs:
            return m.group(0)
        return f'href="../{slug}.html"'

    return re.sub(r'href="([a-z0-9][a-z0-9-]*)\.html"', fix, body_html)

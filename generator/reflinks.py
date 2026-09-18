"""Reference links: a page's citations made into hyperlinks.

``links/<slug>.json`` is a table of the works a page cites — the
mediography's games, books, films, podcasts, blog posts — and where each one
lives. ``apply_reference_links`` wraps the first mention of each title in
the rendered article with a link. Titles are matched as text in the final
HTML, so a translation that keeps a title as printed (most do; these are
real works) is linked too, and one that localizes it is not — those are
returned so the build can name them.

An entry is ``{"title", "url", "site", "kind", "match"?, "note"?}``; see
``links/README.md``. ``match`` is the HTML to look for instead of the
escaped title, for a title that carries markup of its own
(``<em>Apocalypse World</em>: Crossing the Line``).
"""

from __future__ import annotations

import html
import json
import re

from . import REPO_ROOT

LINKS_DIR = REPO_ROOT / "links"

_cache: dict[str, list[dict]] = {}


def reference_links(slug: str) -> list[dict]:
    """The link table for ``slug``, or ``[]`` when the page has none."""
    if slug in _cache:
        return _cache[slug]
    path = LINKS_DIR / f"{slug}.json"
    entries: list[dict] = []
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = [e for e in data.get("links", []) if e.get("title") and e.get("url")]
    _cache[slug] = entries
    return entries


def _inside_anchor(body: str, pos: int) -> bool:
    """True when ``pos`` falls inside an ``<a …>…</a>`` already."""
    opened = body.rfind("<a ", 0, pos)
    return opened >= 0 and body.rfind("</a>", 0, pos) < opened


def _anchor(entry: dict, text: str) -> str:
    attrs = [
        f'href="{html.escape(entry["url"], quote=True)}"',
        'class="ref-link"',
        'target="_blank"',
        'rel="noopener"',
    ]
    if entry.get("site"):
        attrs.append(f'title="{html.escape(entry["site"], quote=True)}"')
    return f"<a {' '.join(attrs)}>{text}</a>"


def apply_reference_links(body: str, slug: str) -> tuple[str, list[str]]:
    """Wrap each cited title's first mention in ``body`` with its link.

    Returns ``(body, unmatched_titles)``.
    """
    entries = reference_links(slug)
    if not entries:
        return body, []
    missing: list[str] = []
    for entry in entries:
        needles = [entry.get("match") or html.escape(entry["title"], quote=True)]
        # Fallback phrases, for a translation that renders the title
        # ("Driftless" inside a Chinese sentence).
        needles += [html.escape(a, quote=True) for a in entry.get("also") or []]
        patterns = []
        for needle in needles:
            esc = re.escape(needle)
            # The italic name, the quoted essay title, then the bare phrase
            # — in that order, so a short name ("Rome") is taken where the
            # book sets it as a title and not from a passing mention.
            patterns += [
                rf"(<em>)({esc})(</em>)",
                rf"(&quot;)({esc})(?=[,.;:!?]?&quot;)",
                rf"(?<![\w&#;])({esc})(?![\w;])",
            ]
        done = False
        for pat in patterns:
            for m in re.finditer(pat, body):
                if _inside_anchor(body, m.start()):
                    continue
                groups = m.groups()
                if len(groups) == 3:
                    pre, text, post = groups
                    repl = pre + _anchor(entry, text) + post
                elif len(groups) == 2:
                    pre, text = groups
                    repl = pre + _anchor(entry, text)
                else:
                    repl = _anchor(entry, groups[0])
                body = body[: m.start()] + repl + body[m.end():]
                done = True
                break
            if done:
                break
        if not done:
            missing.append(entry["title"])
    return body, missing

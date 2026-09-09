"""
Marker lines → article HTML: headings, lists, value and roll tables, stat
blocks, steading improvements, playbook sheets, page-reference linking, and the
section index that deep links resolve against.
"""

from __future__ import annotations

import html
import json
import re

from .sites import SITES_BOOK_ID
from .text import (
    BARE_PAGE_RE,
    B_OFF,
    B_ON,
    CHECK_PART,
    DICE_RE,
    ENTRY_RE,
    HP_LINE_RE,
    LONE_VALUE_RE,
    MARKER_RE,
    M_B,
    M_B2,
    M_BC,
    M_BOX,
    M_C,
    M_C2,
    M_CX,
    M_CX2,
    M_E,
    M_ENDBOX,
    M_H2,
    M_H3,
    M_H4,
    M_HR,
    M_ICON,
    M_Q,
    M_STATS,
    M_STEP,
    M_TH,
    M_VA,
    M_VF,
    M_VR,
    M_VT,
    M_WRITE,
    PAGE_REF_RE,
    ROLL_HEADER_DICE_ONLY,
    ROLL_HEADER_RE,
    ROLL_HEADER_REV_RE,
    TAG_WORDS,
    THREAT_MOVES_RE,
    THREAT_TYPE_ICONS,
    VALUE_ROW_RE,
    _FMT_SET,
    _cancel_fmt_seam,
    _defmt,
    _is_all_caps_label,
    _is_item_tag_line,
    _is_pure_arcana_tag_line,
    _split_leading_fmt,
    _split_trailing_fmt,
    fmt_to_html,
    is_fully_pairwise_doubled,
    is_running_header,
    is_set_off_italic,
    italic_coverage,
    looks_like_heading,
    looks_like_inline_creature,
    looks_like_roll_header,
    looks_like_tag_line,
    looks_like_value_header,
    normalize_section_key,
    normalize_text,
    parse_page_nums,
    parse_value_row,
    should_join,
    slugify_id,
    smart_title,
    split_bold_prefix,
    strip_markers,
    titlecase_label,
    titlecase_name,
    undouble_words,
)


STAT_LABEL_RE = re.compile(
    r"(^|(?<=[\s>]))(HP(?=\s*\d)|Armor|Damage|Instinct|Special [Qq]ualit(?:y|ies)|Cost|Loyalty)\b(?!</strong>)"
)


def bold_stat_labels(html_text: str) -> str:
    """Set a stat block's labels (HP, Armor, Damage, Instinct, ...) in bold, the way
    the book prints them. The stat line is rendered de-tokenized, so the book's own
    bold runs are gone by here; the labels are found by shape instead — a capitalized
    keyword at the start of the line or after whitespace (the book breaks its lines
    wherever it likes). Case matters: "Damage" is a label, "+1d4 damage" is prose."""
    return STAT_LABEL_RE.sub(lambda m: f"{m.group(1)}<strong>{m.group(2)}</strong>", html_text)


# A translation memory (translate.TextMemory) consulted wherever text is
# emitted, so a page can be rendered from its English lines — every
# classifier seeing the text it was written for, every id English — and
# still read in another language. None while the English pages build.
_TM = None


def set_translation(tm) -> None:
    global _TM
    _TM = tm


def T(s: str) -> str:
    """``s`` in the language being rendered, when a translation has it."""
    if _TM is None or not s:
        return s
    return _TM.get(s)


def T_english(s: str) -> str:
    """The English behind a translated string, when one is being rendered —
    for an id made off text read back out of the HTML. ``s`` otherwise."""
    if _TM is None or not s:
        return s
    return _TM.reverse(s) or s


def render_rich_text(s: str, link_fn) -> str:
    """Linkify text (inline formatting sentinels become tags inside link_fn)."""
    return link_fn(s)


def book_icon_img_html(rel_path: str, *, rel_prefix: str = "") -> str:
    """HTML <img> for a book category icon (path under images/)."""
    if not rel_path:
        return ""
    src = f"{rel_prefix}images/{rel_path.lstrip('/')}"
    return (
        f'<img class="book-icon" src="{html.escape(src)}" alt="" '
        f'width="18" height="18" loading="lazy">'
    )


def dice_button(expr: str) -> str:
    # Normalize en/em dashes to ASCII for the roller; keep display as written
    e = expr.lower().replace("–", "-").replace("—", "-")
    e = re.sub(r"\s*([+\-])\s*", r"\1", e)  # d10 + 3 → d10+3
    return (
        f'<button type="button" class="dice-roll" data-dice="{html.escape(e)}" '
        f'title="Click to roll {html.escape(expr)} — Shift: advantage · Ctrl: disadvantage">{html.escape(expr)}</button>'
    )


def _strip_link_label_noise(label: str) -> str:
    """
    Drop list/prose glue that page-ref capture often includes:
    'or wolves', 'maybe even a kleztigr', 'and revenants'
    """
    s = label.strip()
    # Repeatedly peel leading connectors / hedges / articles
    lead = re.compile(
        r"^(?:"
        r"or|and|maybe|even|perhaps|possibly|just|like|including|"
        r"a|an|the|some|any|its|their"
        r")\s+",
        re.I,
    )
    while True:
        n = lead.sub("", s)
        if n == s:
            break
        s = n
    return s.strip(" ,;:-")


def _singular_forms(word: str) -> list[str]:
    """Every plausible English singular for the last word of a name.

    More than one reading can be plausible from the spelling alone —
    "magpies" is "magpie" + s, not "magp" + ies — so the caller tries them
    all against the sections it knows about rather than committing to one.
    """
    w = word.lower()
    out: list[str] = []

    def add(x: str) -> None:
        if x and x != w and x not in out:
            out.append(x)

    if len(w) <= 2:
        return out
    # vortices → vortex, indices → index, matrices → matrix
    if w.endswith("ices") and len(w) > 5:
        add(w[:-4] + "ex")
        add(w[:-4] + "ix")
    if w.endswith("s") and not w.endswith("ss"):
        add(w[:-1])  # the plain plural, under every other reading
    one = _singularize_word(word)
    if one:
        add(one)
    return out


def _singularize_word(word: str) -> str | None:
    """Best-effort English singular for the last word of a monster name."""
    w = word.lower()
    if len(w) <= 2:
        return None
    # wolves → wolf, knives → knife
    if w.endswith("ves") and len(w) > 4:
        return w[:-3] + "f"
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    # boxes/churches/dishes → strip es; NOT drakes/caves (vowel + consonant + es)
    if re.search(r"(?:s|ss|sh|ch|x|z)es$", w):
        return w[:-2]
    if w.endswith("oes") and len(w) > 3:
        return w[:-2]  # heroes → hero, potatoes → potato
    if w.endswith("es") and len(w) > 3:
        # drakes → drake, bears handled via plain -s below
        return w[:-1]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 2:
        return w[:-1]
    return None


def _section_match_keys(label: str) -> list[str]:
    """Generate normalized keys to match a link label to a section/monster id."""
    cleaned = _strip_link_label_noise(label)
    base = normalize_section_key(cleaned)
    if not base:
        return []
    keys: list[str] = [base]
    # Also try original without noise strip (already normalized)
    raw = normalize_section_key(label)
    if raw and raw not in keys:
        keys.append(raw)

    def add(k: str) -> None:
        k = k.strip()
        if k and k not in keys:
            keys.append(k)

    for candidate in list(keys):
        words = candidate.split()
        if not words:
            continue
        last = words[-1]
        stems = _singular_forms(last)
        for stem in stems:
            add(" ".join(words[:-1] + [stem]) if len(words) > 1 else stem)
        # pluralize if singular
        if not last.endswith("s"):
            add(" ".join(words[:-1] + [last + "s"]) if len(words) > 1 else last + "s")
        # drop trailing type words: "pack drake" ↔ "pack"
        stripped = re.sub(
            r"\s+(drake|bear|folk|spirit|ghost|wolf|boar|cougar)s?$",
            "",
            candidate,
        ).strip()
        if stripped and stripped != candidate:
            add(stripped)
        # last word alone (wolves → wolf already handled; "kleztigr")
        if len(words) > 1:
            add(last)
            for stem in stems:
                add(stem)
    # Trailing phrases: "they suffer the forest s wrath" → "forest s wrath"
    base_words = base.split()
    if len(base_words) >= 3:
        for k_len in (4, 3, 2):
            if len(base_words) > k_len:
                tail = " ".join(base_words[-k_len:])
                if len(tail) >= 8:
                    add(tail)
    return keys


# Words a reference can end on that name nothing, so they must never be
# matched against the tail or head of a section title.
_PARTIAL_STOPWORDS = {
    "them", "they", "this", "that", "these", "those", "your", "yours",
    "their", "theirs", "here", "there", "then", "than", "with", "from",
    "into", "onto", "upon", "also", "just", "only", "even", "both",
    "some", "such", "each", "every", "other", "more", "most", "none",
    "when", "what", "which", "where", "while", "they're", "it's",
}


def resolve_section_fragment(
    page: int | None,
    label: str | None,
    target_slug: str | None,
    section_index: dict | None,
) -> str | None:
    """
    Find a #fragment for a monster/section name on a target page/article.
    section_index keys:
      by_page_norm: {(page_int, norm): (slug, id)}
      by_slug_norm: {(slug, norm): id}
    """
    if not label or not section_index:
        return None
    keys = _section_match_keys(label)
    if not keys:
        return None

    by_page = section_index.get("by_page_norm") or {}
    by_slug = section_index.get("by_slug_norm") or {}

    def page_hit(key: str) -> str | None:
        # Only trust a by-page hit whose section lives on the link's target
        # article (one PDF page can map to several arcana slugs).
        entry = by_page.get((page, key))
        if entry and (target_slug is None or entry[0] == target_slug):
            return entry[1]
        return None

    for k in keys:
        if not k:
            continue
        if page is not None:
            hit = page_hit(k)
            if hit:
                return hit
        if target_slug and (target_slug, k) in by_slug:
            return by_slug[(target_slug, k)]
        # Compact form without spaces
        compact = k.replace(" ", "")
        if page is not None:
            hit = page_hit(compact)
            if hit:
                return hit
        if target_slug and (target_slug, compact) in by_slug:
            return by_slug[(target_slug, compact)]

    # A reference often carries only part of the printed name — the books
    # shorten one once it has been introduced ("Ferocedes" for the stat block
    # titled "Ferocedes Ogran"), and a label picked out of running prose can
    # start mid-name ("Together" out of "Pull Together"). Match on either end,
    # but only where exactly one section fits, so a shared word ("Fire Vortex"
    # / "Fire Elemental") never picks a side.
    if target_slug:
        # A lone word pulled out of a longer label is too thin to match on:
        # "in advance" reduces to "advance", which would claim "Advance
        # towards impending doom". A one-word label is the label itself.
        multiword = len(normalize_section_key(label).split()) > 1
        for k in keys:
            if not k or len(k) < 4:
                continue
            if multiword and " " not in k:
                continue
            if k in _PARTIAL_STOPWORDS:
                continue  # "them" is not a reference to anything
            fits = {
                sid
                for (slug_k, norm_k), sid in by_slug.items()
                if slug_k == target_slug
                and (norm_k.startswith(k + " ") or norm_k.endswith(" " + k))
            }
            if len(fits) == 1:
                return fits.pop()
    return None


class AnchorRegistry:
    """Assign unique fragment ids for headings and stat blocks on a page."""

    def __init__(self) -> None:
        self.used: set[str] = set()
        self.sections: list[dict] = []  # {id, name, norm}

    def add(self, name: str, *, caps_label: bool = False) -> str:
        """``caps_label`` marks a heading the book prints in full caps (a move).

        The name itself is retitled for display before it gets here, so the
        casing can no longer be recovered from the string — record it.
        """
        base = slugify_id(name)
        sid = base
        n = 2
        while sid in self.used:
            sid = f"{base}-{n}"
            n += 1
        self.used.add(sid)
        self.sections.append(
            {
                "id": sid,
                "name": name,
                "norm": normalize_section_key(name),
                "caps": caps_label,
            }
        )
        return sid


def _book_id_from_token(token: str) -> str | None:
    t = (token or "").strip().lower()
    if t in ("i", "1"):
        return "book1"
    if t in ("ii", "2"):
        return "book2"
    # Roman II is two I's — handle after single I
    if t == "ii" or t.replace(" ", "") == "ii":
        return "book2"
    return None


# Exact article/arcana title -> articles. The bold label in "**Title** (page N)"
# is more authoritative than the page number, which cannot disambiguate two
# articles printed on the same page (minor arcana are two cards per page, so a
# page resolves to only one of them). Populated by set_title_index().
# Values are lists because both books can hold the same title (e.g. "Threats");
# lookups prefer the article from the book being rendered.
_TITLE_INDEX: dict[str, list[dict]] = {}

# (slug, printed page) → the section that page falls in, for in-article page
# refs. Populated by set_page_sections(); see build_page_section_map().
_PAGE_SECTIONS: dict[tuple[str, int], dict] = {}


def set_page_sections(mapping: dict[tuple[str, int], dict]) -> None:
    _PAGE_SECTIONS.clear()
    _PAGE_SECTIONS.update(mapping)


def set_title_index(articles: list[dict]) -> None:
    _TITLE_INDEX.clear()
    for art in articles:
        # Site sheets live in sites/ — never a target for the
        # "page N" / bare-title linkers, which emit <slug>.html.
        if art.get("book") == SITES_BOOK_ID:
            continue
        t = (art.get("title") or "").strip()
        if not t:
            continue
        keys = [t.lower()]
        if t.lower().startswith("the "):
            keys.append(t[4:].lower())
        for key in keys:
            _TITLE_INDEX.setdefault(key, []).append(art)


def title_index_lookup(title: str, current_book: str | None = None) -> dict | None:
    """Article for an exact title, preferring the book we're rendering."""
    arts = _TITLE_INDEX.get((title or "").strip().lower())
    if not arts:
        return None
    if current_book:
        for art in arts:
            if art.get("book") == current_book:
                return art
    return arts[0]


def linkify_pages(
    text: str,
    lookup: dict[int, dict],
    current_slug: str | None,
    section_index: dict | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> str:
    """Escape text and turn page refs + dice into HTML.

    When text says ``Book II, page 270``, resolve against Book II's page map
    even if the current article is from Book I (and vice versa).
    """

    text = T(text)
    placeholders: list[str] = []

    def store(s: str) -> str:
        placeholders.append(s)
        return f"\x00{len(placeholders)-1}\x00"

    def resolve_lookup(book_id: str | None):
        if book_id and lookups and book_id in lookups:
            return lookups[book_id], (section_indexes or {}).get(book_id)
        return lookup, section_index

    def page_link(
        page: int,
        label: str | None = None,
        book_id: str | None = None,
    ) -> str:
        lk, sidx = resolve_lookup(book_id)
        art = lk.get(page)
        if not art:
            return html.escape(label or f"p. {page}")
        frag = resolve_section_fragment(page, label, art["slug"], sidx)
        # When the page path finds no in-article section for a distinctive
        # label, but that label uniquely names a section elsewhere, trust the
        # name over the page number. Fixes arcana discovery refs like
        # "A metal man (page 533)" where one printed page holds two arcana.
        if label and not frag:
            uniq = resolve_unique_section(label, sidx)
            if uniq and uniq[0] != art["slug"]:
                u_slug, u_sid, u_title = uniq
                if u_slug == current_slug:
                    return (
                        f'<a class="wiki-link" href="#{u_sid}" data-slug="{u_slug}" '
                        f'data-fragment="{u_sid}">'
                        f"{html.escape(label)}</a>"
                    )
                return (
                    f'<a class="wiki-link" href="{u_slug}.html#{u_sid}" '
                    f'data-slug="{u_slug}" data-fragment="{u_sid}">'
                    f"{html.escape(label)}</a>"
                )
        # Same article: in-page fragment if possible
        if art["slug"] == current_slug:
            if frag and label:
                return (
                    f'<a class="wiki-link" href="#{frag}" data-slug="{art["slug"]}" '
                    f'data-fragment="{frag}">'
                    f"{html.escape(label)}</a>"
                )
            # No label to hang the link on ("…use an adventure starter, see
            # page 22.") — name the section that page falls in, the way a
            # cross-article ref names the article it points to. Without this
            # the printed ref resolves to nothing and is dropped, leaving the
            # sentence to close on a bare ", .".
            sec = _PAGE_SECTIONS.get((art["slug"], page))
            if sec:
                return (
                    f'<a class="wiki-link" href="#{sec["id"]}" '
                    f'data-slug="{art["slug"]}" data-fragment="{sec["id"]}">'
                    f'{html.escape(label or sec["name"])}</a>'
                )
            return html.escape(label) if label else f"page {page}"
        text_out = label if label else art["title"]
        href = f"{art['slug']}.html"
        if frag:
            href = f"{href}#{frag}"
        return (
            f'<a class="wiki-link" href="{href}" data-slug="{art["slug"]}" '
            f'{f"data-fragment=\"{frag}\" " if frag else ""}'
            f">{html.escape(text_out)}</a>"
        )

    def links_for_pages(
        pages: list[int],
        label: str | None = None,
        book_id: str | None = None,
    ) -> str:
        if not pages:
            return html.escape(label) if label else ""
        if label:
            primary = page_link(pages[0], label, book_id=book_id)
            if len(pages) == 1:
                return primary
            extras = " · ".join(
                page_link(p, book_id=book_id) for p in pages[1:]
            )
            return f"{primary} ({extras})" if extras else primary
        return " · ".join(page_link(p, book_id=book_id) for p in pages)

    work = text

    # Cross-book first: "Makers' Roads (Book II, page 270)" / "Book I, page 245"
    # Match Roman numerals carefully: Book II before Book I.
    # Book/page refs are often italicised piecemeal ("(*Book II*, page 96)"), so
    # allow inline formatting sentinels between the parts.
    _FS = r"[\x04-\x07]*"

    def repl_book_page(m: re.Match) -> str:
        full = m.group(0)
        book_id = _book_id_from_token(m.group("book"))
        if not book_id:
            return full
        if book_id != current_book and (lookups is None or book_id not in lookups):
            # Ref into a book this wiki wasn't built with. Keep the printed
            # text, but consume it so the plain page-ref passes below don't
            # link "page 549" into the wrong book.
            return store(html.escape(full))
        # Keep the printed "Book II, " lead-in and link the page part, so the
        # sentence still reads as the book prints it.
        cut = full.lower().find("page")
        prefix, rest = (full[:cut], full[cut:]) if cut > 0 else ("", full)
        pages = parse_page_nums(m.group("pages"))
        # Re-emit sentinels swallowed by the linked part so bold/italic runs
        # stay balanced.
        kept = "".join(ch for ch in rest if ch in _FMT_SET)
        return store(
            html.escape(prefix)
            + links_for_pages(pages, book_id=book_id)
            + kept
        )

    work = re.sub(
        rf"Book{_FS}\s*{_FS}(?P<book>II|I|2|1){_FS}\s*[,:]?\s*{_FS}"
        rf"(?:see\s+)?(?:starting\s+on\s+)?"
        rf"pages?{_FS}\s+{_FS}(?P<pages>[\d,\s\-–—]+)",
        repl_book_page,
        work,
        flags=re.IGNORECASE,
    )

    # Bold cross-ref immediately followed by a page ref:
    # "**Mudslides** (page 376)" / "**Ghosts (**page 76)" → make the *bold*
    # text the (deep) link and drop the now-redundant "(page N)".
    def repl_bold_pageref(m: re.Match) -> str:
        inner = m.group("t")
        pages = parse_page_nums(m.group("pg"))
        poss = m.groupdict().get("poss") or ""
        clean = _defmt(inner).strip()
        clean = re.sub(r"[\s(]+$", "", clean).strip()
        if not pages or len(clean) < 2:
            return m.group(0)
        lk, sidx = resolve_lookup(None)
        # Prefer an exact title match over the page number (which can't tell
        # apart two arcana printed on the same page).
        art_by_title = title_index_lookup(clean, current_book)
        art = art_by_title or lk.get(pages[0])
        if not art:
            return m.group(0)
        # A title-resolved article is unrelated to `pages[0]`, so don't derive an
        # in-page fragment from that page.
        frag = (
            None
            if art_by_title
            else resolve_section_fragment(pages[0], clean, art["slug"], sidx)
        )
        disp_inner = re.sub(r"[\s(]+$", "", inner)
        # Keep possessive inside the link: "Stone Lords'"
        disp = html.escape(B_ON + disp_inner + B_OFF) + html.escape(poss)
        if art["slug"] == current_slug:
            # The bold text names the thing being referenced. Where the name
            # matched nothing, only a section that *opens* on the cited page
            # is worth linking; one that merely runs through it is a different
            # subject ("Ferocedes" is not "Star-mole").
            if not frag:
                sec = _PAGE_SECTIONS.get((art["slug"], pages[0]))
                if sec and sec.get("opens_here"):
                    frag = sec["id"]
            if frag:
                return store(
                    f'<a class="wiki-link" href="#{frag}" '
                    f'data-slug="{art["slug"]}" data-fragment="{frag}">'
                    f"{disp}</a>"
                )
            return store(disp)
        href = f'{art["slug"]}.html' + (f"#{frag}" if frag else "")
        frag_attr = f'data-fragment="{frag}" ' if frag else ""
        return store(
            f'<a class="wiki-link" href="{href}" data-slug="{art["slug"]}" '
            f"{frag_attr}>{disp}</a>"
        )

    # Allow optional possessive between bold close and the page ref:
    # "Stone Lords' (page 382)"
    work = re.sub(
        r"\x04(?P<t>[^\x04\x05]*?)\x05"
        r"(?P<poss>(?:'|’)s?)?"
        r"\s*\(?\s*(?:see\s+)?"
        r"pages?\s+(?P<pg>[\d,\s\-–—]+)\)",
        repl_bold_pageref,
        work,
        flags=re.IGNORECASE,
    )

    # Same, but with the ref trailing the phrase instead of bracketing it:
    # "use an **adventure starter**, see page 22." The explicit "see" keeps
    # this off unrelated neighbours ("**Danu**, page 30" stays as printed).
    work = re.sub(
        r"\x04(?P<t>[^\x04\x05]*?)\x05"
        r"(?P<poss>(?:'|’)s?)?"
        r"\s*,\s*see\s+pages?\s+(?P<pg>[\d,\s\-–—]+)",
        repl_bold_pageref,
        work,
        flags=re.IGNORECASE,
    )

    # Title (page N[, M]) — same-book
    def repl_title_page(m: re.Match) -> str:
        title = m.group(1).strip()
        pages = parse_page_nums(m.group(2))
        # Regex may over-capture preceding words (up to 7). Prefer the resolved
        # article title when the capture ends with it, e.g.
        # "certain snow-covered meadows of the Huffel Peaks (page 236)"
        # → "certain snow-covered meadows of the " + link("Huffel Peaks").
        prefix = ""
        if pages:
            lk, _ = resolve_lookup(None)
            art = lk.get(pages[0]) if lk else None
            if art:
                at = (art.get("title") or "").strip()
                if at:
                    low = title.lower()
                    for cand in (at, at[4:] if at.lower().startswith("the ") else at):
                        c_low = cand.lower()
                        if not c_low:
                            continue
                        if low == c_low:
                            break
                        if low.endswith(c_low) and low[: -len(c_low)].endswith(" "):
                            # Keep original casing; put over-captured words back outside the link
                            prefix = title[: -len(cand)].rstrip()
                            title = title[-len(cand) :]
                            break
        # Peel list glue: "or wolves", "maybe even a kleztigr"
        cleaned = _strip_link_label_noise(title)
        if cleaned and normalize_section_key(cleaned) != normalize_section_key(
            title
        ):
            t_words = title.split()
            c_words = cleaned.split()
            n = len(c_words)
            if 0 < n <= len(t_words) and normalize_section_key(
                " ".join(t_words[-n:])
            ) == normalize_section_key(cleaned):
                head = " ".join(t_words[:-n]).strip()
                title = " ".join(t_words[-n:])
                if head:
                    prefix = (prefix + " " + head).strip() if prefix else head
        link = links_for_pages(pages, title)
        if prefix:
            return store(html.escape(prefix) + " " + link)
        return store(link)

    work = re.sub(
        r"([A-Za-z][A-Za-z0-9'’\-]*(?:\s+[A-Za-z][A-Za-z0-9'’\-]*){0,6})\s+"
        r"\((?:see\s+)?pages?\s+([\d,\s\-–—]+)\)",
        repl_title_page,
        work,
    )

    def repl_paren(m: re.Match) -> str:
        pages = parse_page_nums(m.group(1))
        return store(links_for_pages(pages))

    work = PAGE_REF_RE.sub(repl_paren, work)

    def repl_bare(m: re.Match) -> str:
        pages = parse_page_nums(m.group(1))
        # The link carries a name, not a page number, so keep the printed
        # lead-in: "See page 87 for more on coins" → "See Coins for more…".
        lead = re.match(r"see\s+", m.group(0), re.I)
        prefix = html.escape(lead.group(0)) if lead else ""
        return store(prefix + links_for_pages(pages))

    work = BARE_PAGE_RE.sub(repl_bare, work)

    # Escape remaining text in segments between placeholders
    parts = re.split(r"(\x00\d+\x00)", work)
    out = []
    for part in parts:
        m = re.fullmatch(r"\x00(\d+)\x00", part)
        if m:
            out.append(placeholders[int(m.group(1))])
        else:
            esc = html.escape(part)
            esc = DICE_RE.sub(lambda mm: dice_button(mm.group(1)), esc)
            out.append(esc)
    # Inline bold/italic sentinels → tags (after escaping and link insertion)
    return fmt_to_html("".join(out))


def auto_link_titles(html_text: str, articles: list[dict], current_slug: str | None) -> str:
    """Link bare article titles in HTML text nodes (simple pass)."""
    # Build longest-first titles
    titles = []
    for art in articles:
        if art["slug"] == current_slug:
            continue
        titles.append((art["title"], art))
        if "," in art["title"]:
            titles.append((art["title"].split(",")[0].strip(), art))
        if art["title"].lower().startswith("the "):
            titles.append((art["title"][4:], art))
    titles.sort(key=lambda x: -len(x[0]))

    # Only operate outside tags
    chunks = re.split(r"(<[^>]+>)", html_text)
    for i, chunk in enumerate(chunks):
        if not chunk or chunk.startswith("<"):
            continue
        for title, art in titles:
            if len(title) < 4:
                continue
            # word-boundary-ish match, not already inside a link from previous
            pattern = re.compile(
                r"(?<![\\w/])(" + re.escape(title) + r")(?![\\w/])",
                re.IGNORECASE,
            )

            def repl(m, art=art, title=title):
                return (
                    f'<a class="wiki-link" href="{art["slug"]}.html" '
                    f'data-slug="{art["slug"]}">'
                    f"{m.group(1)}</a>"
                )

            chunk = pattern.sub(repl, chunk, count=1)  # at most one per title per chunk
        chunks[i] = chunk
    return "".join(chunks)


def render_roll_table(
    dice: str,
    label: str,
    entries: list[tuple[str, str]],
    lookup: dict,
    current_slug: str | None,
    section_index: dict | None = None,
    anchor_id: str | None = None,
    link_kw: dict | None = None,
) -> str:
    lkw = link_kw or {}
    rows = []
    for num, body in entries:
        rows.append(
            f"<tr><th scope=\"row\">{html.escape(num)}</th>"
            f"<td>{linkify_pages(body, lookup, current_slug, section_index, **lkw)}</td></tr>"
        )
    id_attr = f' id="{html.escape(anchor_id)}"' if anchor_id else ""
    return (
        f'<div class="roll-table"{id_attr}>'
        f'<div class="roll-table-head">'
        f"{dice_button(dice)}"
        f' <span class="roll-label">{html.escape(titlecase_name(label))}</span>'
        f"</div>"
        f'<table><tbody>{"".join(rows)}</tbody></table>'
        f"</div>"
    )


def render_value_table(
    title: str,
    rows: list[tuple[str, str]],
    lookup: dict,
    current_slug: str | None,
    section_index: dict | None = None,
    link_kw: dict | None = None,
    notes: list[str] | None = None,
) -> str:
    lkw = link_kw or {}
    body = []
    for item, val in rows:
        body.append(
            f"<tr><td>{linkify_pages(item, lookup, current_slug, section_index, **lkw)}</td>"
            f'<td class="val">{html.escape(val)}</td></tr>'
        )
    notes_html = "".join(
        f'<div class="value-note">{html.escape(nt)}</div>' for nt in (notes or [])
    )
    # The books set the head in two columns — the category on the left, the
    # word "value" over the value column — so split the title back apart and
    # put it in a header row, where it lines up with the values beneath it.
    head_name = smart_title(
        re.sub(r"\s*value\s*$", "", title, flags=re.I).strip()
    )
    return (
        f'<div class="value-table">'
        f"<table>"
        f'<thead><tr class="value-table-head">'
        f"<th>{html.escape(head_name)}</th>"
        f'<th class="vth-val">Value</th>'
        f"</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
        f"{notes_html}"
        f"</div>"
    )


def render_stat_block(
    name: str,
    lines: list[str],
    lookup: dict,
    current_slug: str | None,
    section_index: dict | None = None,
    anchor_id: str | None = None,
    link_kw: dict | None = None,
    check_id: str | None = None,
    icon_html: str = "",
    variant: str | None = None,
    tags: str = "",
) -> str:
    """Compact monster/enemy/threat block — minimal vertical space.

    ``variant="follower"`` marks a follower's block (an arcanum's bound spirit,
    a Green Lord's vine): same card, but its HP is the players' to manage, so
    the tracker keeps it in a store the whole table shares. ``tags`` is a tag
    line the caller has already told apart (an arcanum sets it in italics), for
    when the words alone would not give it away ("Spirit, primordial, …")."""
    lkw = link_kw or {}
    # Stat blocks are visually styled via CSS; parse/render on plain text so
    # inline formatting sentinels never break the HP/Damage/tag detection.
    name = _defmt(name)
    # Preserve embedded roll-table payloads; de-tokenized for everything else.
    norm_lines: list[str] = []
    for l in lines:
        if l.startswith("__ROLL_TABLE__") or l.startswith(M_C):
            norm_lines.append(l)
        else:
            norm_lines.append(_defmt(l))
    lines = norm_lines
    tags = _defmt(tags)
    stats: list[str] = []
    moves: list[str] = []
    other: list[str] = []
    checks: list[str] = []
    roll_tables: list[tuple[str, str, list[tuple[str, str]]]] = []
    seen_instinct = False
    in_questions = False

    for line in lines:
        if line.startswith("__ROLL_TABLE__"):
            payload = line[len("__ROLL_TABLE__") :]
            dice_s, _, rest = payload.partition("\x01")
            label_s, _, rest = rest.partition("\x01")
            ents: list[tuple[str, str]] = []
            for part in rest.split("\x02"):
                if not part:
                    continue
                num_s, _, body_s = part.partition("\x03")
                ents.append((num_s, body_s))
            roll_tables.append((dice_s, label_s, ents))
            continue
        if line.startswith(M_C):
            checks.append(line[len(M_C):].strip())
            continue
        low = line.lower().strip()
        if re.match(r"^questions\s*:?\s*$", low):
            in_questions = True
            other.append("Questions")
            continue
        if not tags and re.match(r"^Threat\b", line, re.I):
            tags = line
            continue
        # "Damage brick, makeshift club, rusty knife" / "d6 (hand, crude)":
        # a die opening the next line is the Damage wrapping, however much
        # the tags after it read like a creature's tag line.
        if (
            stats
            and "damage" in stats[-1].lower()
            and re.match(r"^\d{0,2}d\d+", low)
        ):
            stats[-1] = stats[-1] + " " + line
            continue
        # A stat line that stops on a comma or inside a parenthesis is a stat
        # line that wrapped — "Armor 5 (resilience, human hide)," / "1 vs.
        # bronze Damage obsidian knife …" — whatever the next line looks like,
        # short of a move bullet.
        if (
            stats
            and not line.startswith(("•", "·"))
            and (
                stats[-1].rstrip().endswith(",")
                or stats[-1].count("(") > stats[-1].count(")")
            )
        ):
            stats[-1] = stats[-1] + " " + line
            continue
        # The inserts print an HP box beside the stat line, and its label
        # lands at the end of it — "HP 6; Armor 0 HP" — or ahead of the next
        # stat: "Armor 1 (shield) HP Damage iron spear". The box's "Max. 6"
        # lands on a move the same way.
        if HP_LINE_RE.search(line):
            line = re.sub(
                r"\s+HP(?=\s*$|\s+(?:Damage|Instinct|Special|Cost)\b)", "", line
            )
            low = line.lower().strip()
        elif line.startswith(("•", "·")):
            # …and mid-move when the move wrapped under the box:
            # "Tend to the sick, injured, Max. 6 women in labor"
            line = re.sub(r"\s+Max\.?\s*\d+(?=\s|$)", "", line)
        # Don't absorb the next monster's identity
        if (
            tags
            and stats
            and looks_like_tag_line(line)
            and not low.startswith(("damage", "hp", "instinct", "cost"))
        ):
            # leftover identity of a following creature — stop via caller usually
            other.append(line)
            continue
        if looks_like_tag_line(line) and not tags:
            tags = line
        elif HP_LINE_RE.search(line) or low.startswith(
            (
                "damage",
                "special quality",
                "special qualities",
                "instinct",
                "armor",
                "cost",
            )
        ):
            if low.startswith("instinct"):
                seen_instinct = True
            # Damage lines often continue with bare "d6 (...)" on next line
            if (
                stats
                and re.match(r"^\d{0,2}d\d+", low)
                and stats[-1].lower().startswith("damage")
            ):
                stats[-1] = stats[-1] + " " + line
            else:
                stats.append(line)
        elif re.match(r"^\d{0,2}d\d+", low) and stats:
            stats[-1] = stats[-1] + " " + line
        elif line.startswith("•") or line.startswith("·"):
            item = line.lstrip("•· ").strip()
            # After flavor notes (or a Questions section), trailing bullets
            # are options/requirements — keep them in notes, not moves.
            if in_questions or (other and seen_instinct and moves):
                other.append("• " + item)
            else:
                moves.append(item)
        elif low.startswith("when "):
            other.append(line)
        else:
            # Continue a Damage line that wrapped mid-parenthetical. The
            # Damage may share its line with HP and Armor ("HP 16; Armor 0
            # Damage knife d8 (hand, messy," / "1 piercing, advantage)"), so
            # the test is that the last stat holds a Damage at all — and an
            # open parenthesis is proof the line broke inside the tags.
            if stats and "damage" in stats[-1].lower() and (
                stats[-1].count("(") > stats[-1].count(")")
                or line.startswith("(")
                or line[0:1].islower()
                or low.startswith(
                    ("maw ", "1 piercing", "piercing)", "advantage)", "messy,")
                )
                or stats[-1].endswith(",")
                or stats[-1].endswith("(")
                or re.match(r"^\d{0,2}d\d+", low)
            ):
                stats[-1] = stats[-1] + " " + line
                continue
            # After instinct, short action phrases are moves (not more stats)
            if seen_instinct or (
                stats and any(s.lower().startswith("instinct") for s in stats)
            ):
                seen_instinct = True
                if low.startswith("instinct"):
                    stats.append(line)
                    continue
                # Long / narrative prose after the block → flavor notes, not moves
                is_bullet = line.startswith("•") or line.startswith("·")
                is_prose = (
                    not is_bullet
                    and line[0:1].isupper()
                    and (
                        len(line) > 55
                        or bool(other)
                        or low.startswith(
                            ("a ", "an ", "the ", "she ", "he ", "they ", "then ")
                        )
                    )
                )
                if is_prose:
                    other.append(line)
                    continue
                if len(line) < 100:
                    moves.append(line.lstrip("•· ").strip())
                    continue
            if moves and not looks_like_heading(line) and line[0:1].islower():
                moves[-1] = moves[-1] + " " + line
            else:
                other.append(line)

    moves = [m for m in moves if m.strip()]
    # Drop accidental second-creature tag lines from other
    notes = []
    for o in other:
        if looks_like_tag_line(o) or HP_LINE_RE.search(o):
            continue
        notes.append(o)

    def lf(t: str) -> str:
        return linkify_pages(t, lookup, current_slug, section_index, **lkw)

    def rr(t: str) -> str:
        return render_rich_text(t, lf)

    id_attr = f' id="{html.escape(anchor_id)}"' if anchor_id else ""
    cls = "stat-block" + (f" {variant}" if variant else "")
    parts = [
        f'<div class="{cls}"{id_attr}>'
        f'<h3 class="stat-name">{icon_html}'
        f'{html.escape(titlecase_name(name))}</h3>'
    ]
    if tags:
        parts.append(f'<p class="stat-tags">{rr(tags)}</p>')
    if stats:
        # One stat to a line, the way the book sets them. The extractor
        # sometimes hands two on one line ("Armor 0 Damage gore") — a label
        # opening mid-line is where the book's line broke.
        rows: list[str] = []
        for s in stats:
            rows.extend(
                p.strip(" ;")
                for p in re.split(
                    r"\s+(?=(?:Damage|Instinct|Special [Qq]ualit(?:y|ies)|Cost)\b)",
                    s,
                )
                if p.strip(" ;")
            )
        compact = "<br>".join(bold_stat_labels(rr(r)) for r in rows)
        parts.append(f'<p class="stat-stats">{compact}</p>')
    if moves:
        parts.append('<ul class="stat-moves">')
        for mv in moves:
            parts.append(f"<li>{rr(mv)}</li>")
        parts.append("</ul>")
    for o in notes:
        if o.startswith("• "):
            # bullet note — keep as a compact list item style paragraph
            parts.append(f'<p class="stat-note">• {rr(o[2:])}</p>')
        else:
            parts.append(f'<p class="stat-note">{rr(o)}</p>')
    for dice_s, label_s, ents in roll_tables:
        rows_html = "".join(
            f'<tr><th scope="row">{html.escape(num_s)}</th>'
            f"<td>{rr(body_s)}</td></tr>"
            for num_s, body_s in ents
        )
        parts.append(
            f'<div class="roll-table roll-table-inline">'
            f'<div class="roll-table-head">'
            f"{dice_button(dice_s)}"
            f' <span class="roll-label">{html.escape(titlecase_name(label_s))}</span>'
            f"</div>"
            f"<table><tbody>{rows_html}</tbody></table>"
            f"</div>"
        )
    if checks:
        parts.append(render_check_list(checks, rr, check_id or "chk"))
    parts.append("</div>")
    return "".join(parts)


def match_toc_to_sections(
    toc_labels: list[str], sections: list[dict]
) -> list[dict]:
    """
    Match chapter TOC labels to real body section anchors.

    TOC text is often truncated by multi-column PDF extraction
    (\"The flow\" → body heading \"The flow of play\"). Prefer the body's
    full name and id for the sidebar.

    Returns list of {name, id} for sidebar deep links (document order).
    """
    if not toc_labels or not sections:
        return []

    used: set[str] = set()
    out: list[dict] = []

    for label in toc_labels:
        norm = normalize_section_key(label)
        if not norm or len(norm) < 2:
            continue
        # Move-style TOC labels (BOLSTER, TRADE &, DEATH'S) → prefer ALL-CAPS sections
        letters = [c for c in label if c.isalpha()]
        toc_caps = bool(letters) and (
            sum(1 for c in letters if c.isupper()) / len(letters) >= 0.75
        )
        best: dict | None = None
        best_score = 0
        for sec in sections:
            sid = sec.get("id") or ""
            if not sid or sid in used:
                continue
            sname = sec.get("name") or ""
            sn = sec.get("norm") or normalize_section_key(sname)
            if not sn:
                continue
            # Moves declare this: they are retitled for display, so the caps
            # can't be counted off the string any more. Fall back to counting
            # for sections that predate the flag.
            if "caps" in sec:
                sec_caps = bool(sec["caps"])
            else:
                sec_letters = [c for c in sname if c.isalpha()]
                sec_caps = bool(sec_letters) and (
                    sum(1 for c in sec_letters if c.isupper()) / len(sec_letters)
                    >= 0.6
                )
            # Don't let "TRADE &" latch onto prose like "Trade with Gordin's…"
            if toc_caps and not sec_caps:
                continue
            score = 0
            if sn == norm:
                score = 100
            elif sn.startswith(norm) and len(norm) >= 3:
                score = 80 + min(len(norm), 20)
            elif norm.startswith(sn) and len(sn) >= 4:
                score = 55 + min(len(sn), 15)
            elif len(norm) >= 6 and (
                sn.startswith(norm + " ") or f" {norm} " in f" {sn} "
            ):
                score = 45
            # Compact ALL-CAPS moves: "deaths door" vs "death s door"
            if score < 40:
                sn_c = sn.replace(" ", "")
                n_c = norm.replace(" ", "")
                if sn_c == n_c:
                    score = 95
                elif sn_c.startswith(n_c) and len(n_c) >= 4:
                    score = 70
            if toc_caps and sec_caps and score > 0:
                score += 10
            if score > best_score:
                best_score = score
                best = sec
        if best and best_score >= 40:
            used.add(best["id"])
            out.append({"name": best["name"], "id": best["id"]})

    return out


def _is_require_header(line: str) -> bool:
    """True for steading-improvement requirement section headers."""
    L = line.strip()
    if not L:
        return False
    # "Requires all of the following" / "Requires both:" / "Requires getting…"
    # Not prose like "requires ___: if you don't meet the requirements…"
    if re.match(
        r"^Requires?\s+("
        r"all\b|both\b|either\b|one\b|any\b|\d+\b|"
        r"getting\b|the\s+following\b"
        r")",
        L,
        re.I,
    ):
        return True
    # "And then, all of the following" / "And then each of these:"
    if re.match(
        r"^And then,?\s*(?:all |each )?of (?:the following|these)\b",
        L,
        re.I,
    ):
        return True
    # "And either of these, to germinate the seeds:"
    if re.match(
        r"^And (?:either|one|any) of (?:the following|these)\b",
        L,
        re.I,
    ):
        return True
    return False


def _is_require_end(line: str) -> bool:
    L = line.strip()
    if re.match(r"^When you (?:mark|meet) all the requirements\b", L, re.I):
        return True
    if re.match(r"^Henceforth\b", L, re.I):
        return True
    return False


def _is_require_item(line: str) -> bool:
    line = strip_markers(line)
    if not line or not line.strip():
        return False
    L = line.strip()
    if _is_require_header(L) or _is_require_end(L):
        return False
    if re.fullmatch(r"steading improvement", L, re.I):
        return False
    # Don't swallow the next major section
    if L.lower() in {
        "terrain",
        "questions",
        "hooks",
        "places",
        "dangers",
        "discoveries",
        "impressions",
        "lore",
        "names",
        "people",
        "secrets",
        "size",
        "population",
        "prosperity",
        "defenses",
        "resources",
    }:
        return False
    if ROLL_HEADER_RE.match(L) or ROLL_HEADER_DICE_ONLY.match(L):
        return False
    if HP_LINE_RE.search(L):
        return False
    if looks_like_tag_line(L):
        return False
    # Next improvement title in ALL CAPS after a complete block
    if _is_all_caps_label(L) and len(L.split()) <= 4:
        return False
    return len(L) < 220


def render_check_list(
    items: list[str],
    link_fn,
    list_id: str,
) -> str:
    """Interactive checkbox list for steading improvement requirements."""
    if not items:
        return ""
    rows = []
    for idx, item in enumerate(items):
        cid = f"{list_id}-{idx}"
        rows.append(
            f'<li class="check-item">'
            f'<label for="{html.escape(cid)}">'
            f'<input type="checkbox" class="wiki-check" id="{html.escape(cid)}" '
            f'data-check-id="{html.escape(cid)}"> '
            f"<span>{link_fn(item)}</span>"
            f"</label></li>"
        )
    return (
        f'<ul class="check-list" data-check-list="{html.escape(list_id)}">'
        f'{"".join(rows)}</ul>'
    )


def _field(
    key: str,
    cls: str,
    placeholder: str = "",
    aria: str = "",
    span: tuple[int, int] | None = None,
    default: str = "",
    sign: bool = False,
) -> str:
    """A write-in box that remembers what the reader typed.

    ``span`` makes it a spinbox over that range. The steppers are ours rather
    than the browser's: a stat is written "+1" and "+0" on the sheet, and a
    native number input will not hold a leading plus. ``default`` is what the
    box falls back to when it is emptied — nothing in the stat block is ever
    left blank.
    """
    ph = (
        f' placeholder="{html.escape(placeholder)}"' if placeholder else ""
    )
    lab = f' aria-label="{html.escape(aria)}"' if aria else ""
    dflt = f' data-default="{html.escape(default)}"' if default else ""
    val = f' value="{html.escape(default)}"' if default else ""
    box = (
        f'<input type="text" class="wiki-field {html.escape(cls)}" '
        f'data-field-key="{html.escape(key)}"{dflt}{val}{ph}{lab} '
        f'autocomplete="off"'
    )
    if span is None:
        return box + ">"
    box += (
        f' inputmode="numeric" role="spinbutton"'
        f' data-spin-min="{span[0]}" data-spin-max="{span[1]}"'
        f'{" data-spin-sign=\"1\"" if sign else ""}'
        f' aria-valuemin="{span[0]}" aria-valuemax="{span[1]}">'
    )
    return (
        f'<span class="pb-spin">{box}'
        f'<span class="pb-spin-btns" aria-hidden="true">'
        f'<button type="button" class="pb-spin-step" data-step="1" '
        f'tabindex="-1">\u25b4</button>'
        f'<button type="button" class="pb-spin-step" data-step="-1" '
        f'tabindex="-1">\u25be</button>'
        f"</span></span>"
    )


# What each numeric box on a sheet can hold, and what it starts at. Damage is
# missing on purpose: it holds a die expression ("d6", "d8+1"), not a number.
# HP has no entry either — its range and its start are the sheet's own cap.
PLAYBOOK_SPANS: dict[str, tuple[int, int]] = {
    "stat": (-3, 5),
    "Armor": (0, 10),
    "XP": (0, 99),
    "Level": (1, 10),
}
# A fresh sheet: no stat assigned yet, full health, first level.
PLAYBOOK_DEFAULTS: dict[str, str] = {
    "stat": "+0",
    "Armor": "0",
    "XP": "0",
    "Level": "1",
}


def render_playbook_stats(block: dict, slug: str, link_fn) -> str:
    """The stat block off the top of a sheet's second page.

    Six scores over three debilities over five tracks. The debility sits on a
    bracket under the pair of stats it dims, the way the sheet prints it, and
    ticking it marks both of them.
    """
    stats = block.get("stats") or []
    debs = block.get("debilities") or []
    tracks = block.get("tracks") or []
    # The name is clipped to the cell, so the abbreviation under it is what
    # carries the roll — it never truncates.
    cells = "".join(
        f'<div class="pb-stat" data-stat="{html.escape(abbr)}">'
        f'<span class="pb-stat-name" title="{html.escape(name)}">'
        f"{html.escape(name)}</span>"
        + _field(
            f"{slug}:stat-{abbr.lower()}",
            "pb-stat-box",
            aria=name,
            span=PLAYBOOK_SPANS["stat"],
            default=PLAYBOOK_DEFAULTS["stat"],
            sign=True,
        )
        + f'<button type="button" class="pb-stat-abbr pb-roll" '
        f'data-roll-stat="{html.escape(abbr)}" '
        f'title="Roll +{html.escape(abbr)} — Shift: advantage · Ctrl: disadvantage">'
        f"({html.escape(abbr)})</button></div>"
        for name, abbr in stats
    )
    debils = "".join(
        f'<label class="pb-debility" data-stats="{html.escape(" ".join(pair))}">'
        f'<input type="checkbox" class="wiki-check pb-debility-box" '
        f'id="deb-{html.escape(name)}" '
        f'data-check-id="deb-{html.escape(name)}">'
        f'<span class="pb-debility-name">{html.escape(name)}</span></label>'
        for name, pair in debs
    )
    row2 = []
    for label in tracks:
        # Damage and HP carry the sheet's own die and cap.
        printed = ""
        shown = label
        if label == "Damage":
            printed = block.get("die") or ""
        elif label == "HP":
            shown = block.get("hp") or "HP"
        span = PLAYBOOK_SPANS.get(label)
        default = PLAYBOOK_DEFAULTS.get(label, "")
        if label == "Damage":
            # A die, not a number — no steppers, but never blank either.
            default = printed
        elif label == "HP":
            # The sheet prints the cap in the label ("HP (max 18)"); a fresh
            # character is at it.
            cap = re.search(r"\d+", shown)
            top = int(cap.group(0)) if cap else 20
            span = (0, top)
            default = str(top)
        box = _field(
            f"{slug}:track-{label.lower()}",
            "pb-track-box",
            aria=shown,
            span=span,
            default=default,
        )
        if label == "Damage":
            # The damage box holds a die, so its label rolls it.
            name_html = (
                f'<button type="button" class="pb-track-name pb-roll" '
                f'data-roll-damage="{html.escape(printed)}" '
                f'title="Roll damage — Shift: advantage · Ctrl: disadvantage">{html.escape(shown)}</button>'
            )
        else:
            name_html = (
                f'<span class="pb-track-name">{html.escape(shown)}</span>'
            )
        row2.append(f'<div class="pb-track">{box}{name_html}</div>')
    gloss = block.get("gloss") or ""
    gloss_html = (
        f'<p class="pb-stats-gloss">{link_fn(gloss)}</p>' if gloss else ""
    )
    return (
        '<section class="pb-stats">'
        '<h2 id="stats">Stats</h2>'
        f"{gloss_html}"
        f'<div class="pb-stat-grid">{cells}</div>'
        f'<div class="pb-debilities">{debils}</div>'
        f'<div class="pb-track-grid">{"".join(row2)}</div>'
        "</section>"
    )


def render_playbook_write(label: str, key: str) -> str:
    """The sheet's ruled name box ("I am called…")."""
    return (
        '<div class="pb-write">'
        f'<span class="pb-write-label">{html.escape(label)}</span>'
        + _field(key, "pb-write-box", aria=label)
        + "</div>"
    )


def render_playbook_steps(steps: list[tuple[str, str]], link_fn) -> str:
    """The introductions walkthrough — a numbered step per plaque."""
    if not steps:
        return ""
    rows = "".join(
        f'<li class="pb-step"><span class="step-badge">{html.escape(num)}</span>'
        f"<div>{link_fn(text)}</div></li>"
        for num, text in steps
    )
    return f'<ol class="pb-steps">{rows}</ol>'


def render_sheet_checks(items: list[dict], link_fn, list_id: str) -> str:
    """A sheet's checkbox list — moves, possessions, appearance, names.

    Two things set it apart from an ordinary checklist: an item can hang
    under the one above it (Borrow Power under Spirit Tongue), and an item
    can come pre-marked, which on a sheet means a move you start with rather
    than one you may take. Those are printed, not chosen, so they are ticked
    and locked.
    """
    if not items:
        return ""
    rows = []
    for idx, item in enumerate(items):
        body = link_fn(item["text"])
        cont = "".join(
            f'<p class="li-cont">{link_fn(c)}</p>'
            for c in (item.get("cont") or [])
        )
        cls = "check-item"
        if item.get("sub"):
            cls += " is-sub"
        if item.get("fixed"):
            rows.append(
                f'<li class="{cls} is-fixed">'
                f'<span class="check-fixed" role="img" '
                f'aria-label="You start with this">'
                f'<input type="checkbox" checked disabled tabindex="-1"></span>'
                f"<span>{body}</span>{cont}</li>"
            )
            continue
        cid = f"{list_id}-{idx}"
        rows.append(
            f'<li class="{cls}">'
            f'<label for="{html.escape(cid)}">'
            f'<input type="checkbox" class="wiki-check" id="{html.escape(cid)}" '
            f'data-check-id="{html.escape(cid)}"> '
            f"<span>{body}</span>"
            f"</label>{cont}</li>"
        )
    return (
        f'<ul class="check-list sheet-checks" '
        f'data-check-list="{html.escape(list_id)}">{"".join(rows)}</ul>'
    )


def render_mark_track(n: int, list_id: str, label: str = "Progress") -> str:
    """Horizontal checkbox track for major-arcana progress marks (☐ ☐ ☐ …)."""
    if n <= 0:
        return ""
    steps = []
    for idx in range(n):
        cid = f"{list_id}-{idx}"
        steps.append(
            f'<label class="track-step" for="{html.escape(cid)}" title="Mark {idx + 1}">'
            f'<input type="checkbox" class="wiki-check" id="{html.escape(cid)}" '
            f'data-check-id="{html.escape(cid)}" aria-label="Mark {idx + 1}">'
            f"</label>"
        )
    return (
        f'<div class="arcana-track" data-check-list="{html.escape(list_id)}">'
        f'<span class="track-label">{html.escape(label)}</span>'
        f'<span class="track-steps">{"".join(steps)}</span>'
        f"</div>"
    )


def _benefit_continues(line: str) -> bool:
    """Prose that continues a steading-improvement benefit section."""
    L = line.strip()
    if not L:
        return False
    if _is_require_header(L) or _is_all_caps_label(L):
        return False
    if re.fullmatch(r"steading improvement", L, re.I):
        return False
    if ROLL_HEADER_RE.match(L) or ROLL_HEADER_DICE_ONLY.match(L):
        return False
    if re.match(
        r"^(When you|Henceforth|Also,|Stonetop gains|Every |The steading|"
        r"You can |You may |This |These )",
        L,
        re.I,
    ):
        return True
    # Longer prose that isn't a short section heading
    if len(L) > 50 and not (looks_like_heading(L) and len(L.split()) <= 5):
        return True
    return False


def _render_steading_block_rich(
    lines: list[str],
    start: int,
    link_fn,
    anchors: "AnchorRegistry",
    next_check_id,
    rich_fn,
) -> tuple[str, int]:
    """
    Render a steading-improvement block from the rich marker stream.

    Gathers the block's marker lines (checkbox titles/requirements plus the
    plain group-headers, blurb, and benefit paragraphs between them),
    converts them to the plain-text form that ``try_parse_improvement_block``
    understands, and delegates to it so multi-group requirements ("And then,
    all of the following…"), blurbs, and wrapped titles render correctly.

    Returns (html, next_index).
    """
    n = len(lines)
    i = start
    plain: list[str] = ["steading improvement"]
    while i < n:
        L = lines[i]
        if L.startswith(M_C):
            raw = L[len(M_C):]
            bp, full = split_bold_prefix(raw)
            if bp and _is_all_caps_label(bp):
                # Checkbox on a title line: emit the name, then any trailing
                # text (a blurb or a group-header) as its own line.
                plain.append(bp)
                rest = full[len(bp):].strip()
                if rest:
                    plain.append(rest)
            else:
                plain.append(strip_markers(L).strip())
            i += 1
            continue
        if L.startswith("\x02"):
            break  # heading / table / other structure ends the block
        plain.append(strip_markers(L).strip())
        i += 1

    parsed = try_parse_improvement_block(plain, 0, link_fn, anchors, next_check_id)
    if parsed is not None:
        html_block, consumed = parsed
        # Anything the parser did not consume → plain paragraphs
        tail = "".join(
            f"<p>{link_fn(t)}</p>" for t in plain[consumed:] if t.strip()
        )
        return html_block + tail, i

    # Fallback: render whatever we gathered as paragraphs
    body = "".join(f"<p>{link_fn(t)}</p>" for t in plain[1:] if t.strip())
    return f'<div class="steading-improvement">{body}</div>', i


def _render_artifact_block_rich(
    lines: list[str],
    start: int,
    title: str,
    link_fn,
    anchors: "AnchorRegistry",
) -> tuple[str, int]:
    """
    Render a tagged artifact / discovery as a card block.

    Layout (from the rich stream):
        M_H3  <title>            (already consumed → passed as ``title``)
        <arcana tag line>        ("magical", "hand, magical, +1 damage", …)
        <description prose …>
        [Something interesting: …]
        [Something useful: …]
    Prose lines are re-flowed into paragraphs, breaking before the "Something
    …" notes and at sentence boundaries. Returns (html, next_index).
    """
    n = len(lines)
    i = start + 1  # skip the heading line
    # One or more tag lines (PDF often wraps ", close, thrown," + "1 piercing").
    # De-tokenized for CSS-styled italic tags; strip stray lead commas.
    tag_bits: list[str] = []
    while i < n and not lines[i].startswith("\x02"):
        raw = lines[i]
        if not (
            _is_item_tag_line(raw)
            or _is_pure_arcana_tag_line(strip_markers(_defmt(raw)))
        ):
            break
        t = strip_markers(_defmt(raw)).strip().lstrip(", ").strip()
        t = re.sub(r"^[\x04\x06,\s]+", "", t).strip().rstrip(",")
        if t:
            tag_bits.append(t)
        i += 1
    tags = ", ".join(tag_bits)

    # Body keeps inline formatting sentinels for rendering
    body: list[str] = []
    while i < n and not lines[i].startswith("\x02"):
        b = lines[i].strip()
        if b:
            body.append(b)
        i += 1

    # Re-flow into paragraphs; decisions use de-tokenized text
    paras: list[tuple[str, bool]] = []  # (token_text, is_note)
    for L in body:
        dL = _defmt(L)
        if not dL:
            continue
        note = bool(
            re.match(r"^(Something (?:interesting|useful)\b|When you\b)", dL, re.I)
        )
        if not paras:
            paras.append((L, note))
        elif note:
            paras.append((L, True))
        elif _defmt(paras[-1][0]).rstrip().endswith((".", "!", "?")) and dL[:1].isupper():
            paras.append((L, False))
        else:
            prev, pn = paras[-1]
            body_pv, tail_pv = _split_trailing_fmt(prev.rstrip())
            lead_pv, rest_pv = _split_leading_fmt(L)
            if body_pv.endswith("-") and rest_pv[:1].islower():
                paras[-1] = (body_pv[:-1] + _cancel_fmt_seam(tail_pv, lead_pv) + rest_pv, pn)
            else:
                paras[-1] = (prev + " " + L, pn)

    disc_name = titlecase_name(title.rstrip(":"))
    hid = anchors.add(disc_name)
    parts = [
        f'<div class="discovery-block" id="{html.escape(hid)}">',
        f'<h3 class="discovery-name">{html.escape(disc_name)}</h3>',
    ]
    if tags:
        parts.append(f'<p class="discovery-tags">{link_fn(tags)}</p>')
    for text, note in paras:
        cls = ' class="discovery-note"' if note else ""
        parts.append(f"<p{cls}>{link_fn(text)}</p>")
    parts.append("</div>")
    return "".join(parts), i


def try_parse_improvement_block(
    lines: list[str],
    start: int,
    link_fn,
    anchors: AnchorRegistry,
    next_check_id,
) -> tuple[str, int] | None:
    """
    Parse a steading improvement (or similar) requirement block with checkboxes.

    Returns (html, next_index) or None if lines[start] is not a block start.
    """
    n = len(lines)
    if start >= n:
        return None
    line = lines[start].strip()
    starts_si = bool(re.fullmatch(r"steading improvement", line, re.I))
    starts_req = _is_require_header(line) and bool(
        re.match(r"^Requires?\b", line, re.I)
    )
    starts_caps = _is_all_caps_label(line) and any(
        _is_require_header(lines[start + k])
        and re.match(r"^Requires?\b", lines[start + k].strip(), re.I)
        for k in range(1, min(5, n - start))
    )
    if not (starts_si or starts_req or starts_caps):
        return None

    j = start
    kind = ""
    if starts_si:
        kind = "Steading improvement"
        j += 1

    title_parts: list[str] = []
    while (
        j < n
        and _is_all_caps_label(lines[j])
        and not _is_require_header(lines[j])
    ):
        title_parts.append(lines[j].strip())
        j += 1
        if len(title_parts) >= 4:
            break
    title = " ".join(title_parts)

    blurb = ""
    if (
        j < n
        and not _is_require_header(lines[j])
        and not _is_require_end(lines[j])
        and not _is_all_caps_label(lines[j])
        and len(lines[j]) < 160
        and any(
            _is_require_header(lines[j + k])
            and re.match(r"^Requires?\b", lines[j + k].strip(), re.I)
            for k in range(0, min(3, n - j))
        )
    ):
        if not _is_require_header(lines[j]):
            blurb = lines[j].strip()
            j += 1

    if j >= n or not (
        _is_require_header(lines[j])
        and re.match(r"^Requires?\b", lines[j].strip(), re.I)
    ):
        return None

    block_start = j
    title = titlecase_label(title)
    hid = anchors.add(title or "Steading improvement", caps_label=True)
    parts = [f'<div class="steading-improvement" id="{html.escape(hid)}">']
    if kind or starts_si:
        parts.append('<p class="si-kind">Steading improvement</p>')
    if title:
        parts.append(f'<h3 class="si-title">{html.escape(T(title))}</h3>')
    if blurb:
        parts.append(f'<p class="si-blurb">{link_fn(blurb)}</p>')

    n_checks = 0
    # Requirement groups (Requires… / And then, all of the following…)
    while j < n and _is_require_header(lines[j]):
        header = lines[j].strip().rstrip(":")
        j += 1
        if header.endswith("?"):
            parts.append(f'<p class="si-requires">{html.escape(header)}</p>')
        else:
            parts.append(f'<p class="si-requires">{html.escape(header)}:</p>')
        items: list[str] = []
        while j < n and _is_require_item(lines[j]):
            items.append(strip_markers(lines[j]).strip())
            j += 1
        if items:
            # Guard against runaway false positives (prose treated as items)
            if len(items) > 30:
                return None
            n_checks += len(items)
            parts.append(
                render_check_list(items, link_fn, next_check_id(title or "req"))
            )

    # Must have actually produced checklist items and advanced
    if n_checks == 0 or j <= block_start:
        return None

    # Benefits: When you mark… / Henceforth…
    while j < n and (
        re.match(r"^When you (?:mark|meet) all the requirements\b", lines[j], re.I)
        or re.match(r"^Henceforth\b", lines[j], re.I)
        or (
            parts
            and _benefit_continues(lines[j])
            and re.match(r"^(Also,|Stonetop gains|Every )", lines[j], re.I)
        )
    ):
        parts.append(f"<p>{link_fn(lines[j])}</p>")
        j += 1
        while j < n and _benefit_continues(lines[j]):
            # Stop if this looks like a new short section title
            L2 = lines[j].strip()
            if looks_like_heading(L2) and len(L2.split()) <= 4 and len(L2) < 40:
                if not re.match(
                    r"^(When |Henceforth|Also|Stonetop|Every |The steading)",
                    L2,
                    re.I,
                ):
                    break
            parts.append(f"<p>{link_fn(lines[j])}</p>")
            j += 1

    parts.append("</div>")
    if j <= start:
        return None
    return "\n".join(parts), j


def structure_html(
    lines: list[str],
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    anchors: AnchorRegistry | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, list[dict]]:
    """Turn cleaned lines into structured HTML (tables, stat blocks, lists).

    Returns (html, sections) where sections is [{id, name, norm}, ...].
    """
    lines = list(lines)  # this function rewrites entries in place
    out: list[str] = []
    i = 0
    n = len(lines)
    if anchors is None:
        anchors = AnchorRegistry()
    link_kw = {
        "lookups": lookups,
        "section_indexes": section_indexes,
        "current_book": current_book,
    }
    check_list_n = 0

    def peek(k=0):
        j = i + k
        return lines[j] if 0 <= j < n else ""

    def link(text: str) -> str:
        return linkify_pages(
            text, lookup, current_slug, section_index, **link_kw
        )

    def next_check_id(prefix: str = "req") -> str:
        nonlocal check_list_n
        check_list_n += 1
        base = slugify_id(prefix) if prefix else "req"
        # A tick is stored as "<page-slug>#<check-id>", so an id opening with
        # the page's own slug says it twice ("the-seeker#the-seeker-10-1");
        # the list number alone carries it ("the-seeker#10-1").
        if base == current_slug:
            return str(check_list_n)
        return f"{base}-{check_list_n}"

    rich_mode = any(l.startswith("\x02") for l in lines)
    forced_creature: set[int] = set()
    pending_icon: str | None = None  # images/… path from M_ICON

    def take_icon_html() -> str:
        nonlocal pending_icon
        if not pending_icon:
            return ""
        img = book_icon_img_html(pending_icon, rel_prefix="")
        pending_icon = None
        return img

    while i < n:
        line = lines[i]

        # --- Structural markers from rich PDF extraction ---
        if line.startswith("\x02"):
            if line == M_HR:
                # Collapse consecutive rules; skip under headings (CSS border);
                # drop hairlines after monster cards (layout between stat blocks).
                prev_html = out[-1] if out else ""
                under_heading = bool(re.search(r"</h[1-4]>\s*$", prev_html))
                prev_stat = bool(out and 'class="stat-block"' in out[-1])
                if under_heading or prev_stat or (out and out[-1] == "<hr>"):
                    i += 1
                    continue
                out.append("<hr>")
                pending_icon = None
                i += 1
                continue
            if line.startswith(M_ICON):
                # Keep the first of back-to-back duplicate icons
                path = line[len(M_ICON):].strip() or None
                if path:
                    pending_icon = path
                i += 1
                while i < n and lines[i].startswith(M_ICON):
                    i += 1
                continue
            if line == M_BOX:
                inner: list[str] = []
                j = i + 1
                while j < n and lines[j] != M_ENDBOX:
                    inner.append(lines[j])
                    j += 1
                inner_html, _ = structure_html(
                    inner,
                    article_title,
                    lookup,
                    articles,
                    current_slug,
                    section_index=section_index,
                    anchors=anchors,
                    lookups=lookups,
                    section_indexes=section_indexes,
                    current_book=current_book,
                )
                out.append(f'<div class="infobox">{inner_html}</div>')
                i = j + 1
                continue
            if line == M_ENDBOX:
                i += 1
                continue
            if line.startswith(M_H2):
                txt = line[len(M_H2):].strip()
                # A numbered step carries its plaque as "4 & 5\x03NPC…"
                plaque, _, rest = txt.partition("\x03")
                if rest:
                    txt = f"{plaque} {rest}"
                if (
                    is_running_header(txt, article_title, near_page_top=True)
                    or normalize_text(txt).lower()
                    == normalize_text(article_title).lower()
                ):
                    pending_icon = None
                    i += 1
                    continue
                hid = anchors.add(txt.rstrip(":"))
                ic = take_icon_html()
                label = (
                    f'<span class="step-badge">{html.escape(plaque)}</span> '
                    + html.escape(T(rest.rstrip(":")))
                    if rest
                    else html.escape(T(txt.rstrip(":")))
                )
                out.append(f'<h2 id="{html.escape(hid)}">{ic}{label}</h2>')
                i += 1
                continue
            if line.startswith(M_TH):
                txt = line[len(M_TH):].strip()
                # "steading improvement" label → custom improvement block
                if txt.lower() == "steading improvement":
                    pending_icon = None
                    block_html, i = _render_steading_block_rich(
                        lines, i + 1, link, anchors, next_check_id,
                        lambda t: render_rich_text(t, link),
                    )
                    out.append(block_html)
                    continue
                hid = anchors.add(txt)
                ic = take_icon_html()
                out.append(
                    f'<h3 id="{html.escape(hid)}" class="table-heading">'
                    f"{ic}{html.escape(T(txt))}</h3>"
                )
                i += 1
                continue
            if line.startswith(M_H4):
                txt = line[len(M_H4):].strip()
                pending_icon = None
                out.append(f"<h4>{html.escape(txt)}</h4>")
                i += 1
                continue
            if line.startswith(M_H3):
                bare = line[len(M_H3):].strip()
                # Creature/threat block: HP-carrying monsters, and also
                # HP-less threats (Fire, Gylglyd vines, The Forest's Wrath)
                # that lead with tags, Damage, Instinct, or "Threat (…)".
                creature = False
                for k in range(1, 6):
                    cand = peek(k)
                    if not cand:
                        break
                    if cand.startswith(M_H3):
                        continue
                    if cand.startswith("\x02"):
                        break
                    c = strip_markers(cand)
                    if c.startswith("• "):
                        continue
                    # A later named creature is not this heading's stat line
                    # (Sites: "Pryder's hallway (F)" then "Pryder (woods-wise…): HP").
                    if looks_like_inline_creature(c) and not c.lower().startswith(
                        bare[:8].lower()
                    ):
                        break
                    low = c.lower()
                    if (
                        HP_LINE_RE.search(c)
                        or re.match(r"^(damage|instinct|threat)\b", low)
                        or (k <= 3 and looks_like_tag_line(c))
                    ):
                        creature = True
                        break
                if creature:
                    forced_creature.add(i)
                    lines[i] = bare
                    # keep pending_icon for the stat-block name
                    continue
                # Artifact / tagged-discovery block: heading immediately
                # followed by an item tag line ("magical", "hand, magical,
                # +1 damage", ", immobile", ", beautiful, magical, Value 4").
                # Tags are italic in the book — use italic-aware detection.
                nxt = peek(1)
                if (
                    nxt
                    and not nxt.startswith("\x02")
                    and (
                        _is_item_tag_line(nxt)
                        or _is_pure_arcana_tag_line(strip_markers(nxt))
                    )
                ):
                    ic = take_icon_html()
                    block_html, i = _render_artifact_block_rich(
                        lines, i, bare, link, anchors
                    )
                    if ic and block_html.startswith("<"):
                        # Prefixed title inside the artifact card if present
                        block_html = block_html.replace(
                            f">{html.escape(bare)}",
                            f">{ic}{html.escape(bare)}",
                            1,
                        )
                    out.append(block_html)
                    continue
                # Hazard card: icon heading + prose until next rule/creature
                # (e.g. Vitrified horrors). Do not swallow following roll tables
                # (e.g. "Minor arcana" + "1d6 minor arcanum").
                ic = take_icon_html()
                body_parts: list[str] = []
                j = i + 1
                while j < n:
                    L2 = lines[j]
                    if L2 == M_HR or L2.startswith(
                        (M_ICON, M_H2, M_H3, M_VT, M_TH, M_BOX)
                    ):
                        break
                    if L2.startswith("\x02") and not L2.startswith(
                        (M_B, M_B2, M_Q, M_E, M_C)
                    ):
                        break
                    plain2 = _defmt(L2) if not L2.startswith("\x02") else strip_markers(L2)
                    if looks_like_roll_header(plain2):
                        break
                    if L2.startswith(M_C):
                        # A countdown printed inside the card ("He grows
                        # annoyed" … "Impending doom: He casts you out").
                        body_parts.append(CHECK_PART + L2[len(M_C):].strip())
                    elif L2.startswith((M_B, M_B2, M_Q, M_E)):
                        for pref in (M_B2, M_Q, M_E, M_B):
                            if L2.startswith(pref):
                                body_parts.append("• " + L2[len(pref):].strip())
                                break
                    else:
                        body_parts.append(_defmt(L2))
                    j += 1
                if ic:
                    haz_name = titlecase_name(bare.rstrip(":"))
                    hid = anchors.add(haz_name, caps_label=True)
                    parts_h = [
                        f'<div class="hazard-block" id="{html.escape(hid)}">',
                        f'<h3 class="stat-name">{ic}'
                        f"{html.escape(haz_name)}</h3>",
                    ]
                    bi = 0
                    while bi < len(body_parts):
                        if body_parts[bi].startswith(CHECK_PART):
                            items_c: list[str] = []
                            while bi < len(body_parts) and body_parts[
                                bi
                            ].startswith(CHECK_PART):
                                items_c.append(
                                    body_parts[bi][len(CHECK_PART):].strip()
                                )
                                bi += 1
                            parts_h.append(
                                render_check_list(
                                    items_c,
                                    lambda t: render_rich_text(t, link),
                                    next_check_id(current_slug or "chk"),
                                )
                            )
                            continue
                        if body_parts[bi].startswith("• "):
                            items_h: list[str] = []
                            while bi < len(body_parts) and body_parts[bi].startswith(
                                "• "
                            ):
                                items_h.append(body_parts[bi][2:].strip())
                                bi += 1
                            parts_h.append(
                                '<ul class="bullets">'
                                + "".join(
                                    f"<li>{render_rich_text(it, link)}</li>"
                                    for it in items_h
                                )
                                + "</ul>"
                            )
                        else:
                            parts_h.append(
                                f"<p>{render_rich_text(body_parts[bi], link)}</p>"
                            )
                            bi += 1
                    parts_h.append("</div>")
                    out.append("".join(parts_h))
                    i = j
                    continue
                hid = anchors.add(bare.rstrip(":"))
                out.append(
                    f'<h3 id="{html.escape(hid)}">'
                    f"{ic}{html.escape(T(bare.rstrip(':')))}</h3>"
                )
                i += 1
                continue
            if line.startswith(M_VA):
                frag = line[len(M_VA):].strip()
                prev = out[-1] if out else ""
                k = prev.rfind('</td><td class="val">')
                if 'class="value-table"' in prev and k != -1:
                    out[-1] = prev[:k] + " " + link(frag) + prev[k:]
                elif frag:
                    out.append(f"<p>{link(frag)}</p>")
                i += 1
                continue
            if line.startswith((M_VT, M_VR, M_VF)):
                vt_title: str | None = None
                if line.startswith(M_VT):
                    vt_title = line[len(M_VT):].strip()
                    i += 1
                rows_v: list[tuple[str, str]] = []
                notes_v: list[str] = []
                while i < n and lines[i].startswith((M_VR, M_VF)):
                    L = lines[i]
                    if L.startswith(M_VR):
                        body_, _, val_ = L[len(M_VR):].partition("\x03")
                        rows_v.append((body_.strip(), val_.strip()))
                    else:
                        notes_v.append(L[len(M_VF):].strip())
                    i += 1
                if (
                    vt_title is None
                    and rows_v
                    and out
                    and 'class="value-table"' in out[-1]
                ):
                    # continuation of the table we just rendered
                    row_html = "".join(
                        f"<tr><td>{link(item)}</td>"
                        f'<td class="val">{html.escape(val)}</td></tr>'
                        for item, val in rows_v
                    )
                    out[-1] = out[-1].replace(
                        "</tbody></table>", row_html + "</tbody></table>", 1
                    )
                    continue
                pretty = re.sub(r"\s+", " ", (vt_title or "Value")).strip()
                if not pretty.lower().endswith("value"):
                    pretty = pretty + " value"
                out.append(
                    render_value_table(
                        pretty.title(),
                        rows_v,
                        lookup,
                        current_slug,
                        section_index,
                        link_kw=link_kw,
                        notes=notes_v,
                    )
                )
                continue
            if line[:3] == M_B or line.startswith(M_B2):
                # bullet run, possibly with tier-2 items nested under entries
                items: list[tuple[int, str]] = []
                while i < n and (
                    lines[i][:3] == M_B
                    or lines[i].startswith(M_B2)
                    or (items and lines[i].startswith(M_BC))
                ):
                    L = lines[i]
                    if L.startswith(M_BC):
                        lvl_prev, body_prev = items[-1]
                        items[-1] = (
                            lvl_prev,
                            body_prev
                            + ""
                            + L[len(M_BC):].strip(),
                        )
                    elif L.startswith(M_B2):
                        items.append((2, L[len(M_B2):].strip()))
                    else:
                        items.append((1, L[3:].strip()))
                    i += 1
                parts = ['<ul class="bullets">']
                open_li = False
                open_sub = False
                for lvl, it in items:
                    head_it, *cont_it = it.split("")
                    h = render_rich_text(head_it, link) + "".join(
                        f'<p class="li-cont">{render_rich_text(c, link)}</p>'
                        for c in cont_it
                    )
                    if lvl == 1:
                        if open_sub:
                            parts.append("</ul>")
                            open_sub = False
                        if open_li:
                            parts.append("</li>")
                        parts.append(f"<li>{h}")
                        open_li = True
                    else:
                        if not open_li:
                            parts.append("<li>")
                            open_li = True
                        if not open_sub:
                            parts.append('<ul class="bullets">')
                            open_sub = True
                        parts.append(f"<li>{h}</li>")
                if open_sub:
                    parts.append("</ul>")
                if open_li:
                    parts.append("</li>")
                parts.append("</ul>")
                out.append("".join(parts))
                continue
            if line[:3] in (M_Q, M_E):
                kind3 = line[:3]
                cls = {M_Q: "questions", M_E: "ellipsis"}[kind3]
                items_q: list[list[str]] = []
                while i < n and (
                    lines[i][:3] == kind3
                    or (items_q and lines[i].startswith(M_BC))
                ):
                    if lines[i].startswith(M_BC):
                        items_q[-1].append(lines[i][len(M_BC):].strip())
                    else:
                        items_q.append([lines[i][3:].strip()])
                    i += 1
                out.append(
                    f'<ul class="{cls}">'
                    + "".join(
                        "<li>"
                        + render_rich_text(it[0], link)
                        + "".join(
                            f'<p class="li-cont">{render_rich_text(c, link)}</p>'
                            for c in it[1:]
                        )
                        + "</li>"
                        for it in items_q
                    )
                    + "</ul>"
                )
                continue
            if line.startswith(M_STATS):
                try:
                    block = json.loads(line[len(M_STATS):])
                except Exception:
                    block = None
                if block:
                    out.append(
                        render_playbook_stats(
                            block,
                            current_slug or "playbook",
                            lambda t: render_rich_text(t, link),
                        )
                    )
                i += 1
                continue
            if line.startswith(M_WRITE):
                label = line[len(M_WRITE):].strip()
                out.append(
                    render_playbook_write(
                        label,
                        f"{current_slug or 'playbook'}:write-{slugify_id(label)}",
                    )
                )
                i += 1
                continue
            if line.startswith(M_STEP):
                steps: list[tuple[str, str]] = []
                while i < n and lines[i].startswith(M_STEP):
                    num, _, txt = lines[i][len(M_STEP):].partition("\x03")
                    steps.append((num.strip(), txt.strip()))
                    i += 1
                out.append(
                    render_playbook_steps(
                        steps, lambda t: render_rich_text(t, link)
                    )
                )
                continue
            if line.startswith((M_C, M_C2, M_CX, M_CX2)):
                # A sheet's checkbox items carry state the plain list can't:
                # indent (a move that hangs under another) and a printed tick
                # (a move you start with).
                sheet_items: list[dict] = []
                while i < n and lines[i].startswith((M_C, M_C2, M_CX, M_CX2)):
                    cur = lines[i]
                    for marker, sub, fixed in (
                        (M_CX2, True, True),
                        (M_CX, False, True),
                        (M_C2, True, False),
                        (M_C, False, False),
                    ):
                        if cur.startswith(marker):
                            sheet_items.append(
                                {
                                    "text": cur[len(marker):].strip(),
                                    "sub": sub,
                                    "fixed": fixed,
                                }
                            )
                            break
                    i += 1
                # A move that hangs under another carries its body with it,
                # or the indent would apply to the name alone.
                if sheet_items and sheet_items[-1]["sub"]:
                    conts: list[str] = []
                    while i < n and lines[i] and not MARKER_RE.match(lines[i]):
                        conts.append(lines[i])
                        i += 1
                    if conts:
                        sheet_items[-1]["cont"] = conts
                if any(it["sub"] or it["fixed"] for it in sheet_items):
                    out.append(
                        render_sheet_checks(
                            sheet_items,
                            lambda t: render_rich_text(t, link),
                            next_check_id(current_slug or "chk"),
                        )
                    )
                else:
                    out.append(
                        render_check_list(
                            [it["text"] for it in sheet_items],
                            lambda t: render_rich_text(t, link),
                            next_check_id(current_slug or "chk"),
                        )
                    )
                continue
            # Unknown marker — strip and reprocess as plain text
            lines[i] = strip_markers(line)
            continue

        # Below here `line` is a non-marker line. Structural parsers run on the
        # de-tokenized text; `orig_line` keeps inline formatting for prose.
        orig_line = line
        line = _defmt(line)

        # --- Steading improvement / requirement checklists ---
        parsed = try_parse_improvement_block(
            lines, i, link, anchors, next_check_id
        )
        if parsed is not None:
            html_block, i = parsed
            out.append(html_block)
            continue
        # Lone "steading improvement" label with no following Requires — skip
        if re.fullmatch(r"steading improvement", line.strip(), re.I):
            i += 1
            continue

        # --- Value / price tables (Trade & Barter, services, etc.) ---
        # "goods value", "weapons armor value", or "weapons armor" + "value"/ "&"
        val_title = None
        if looks_like_value_header(line):
            val_title = re.sub(r"\s+", " ", line.strip())
            # drop trailing "value" for display? keep full
            i += 1
            if peek(0) in {"&", "and"}:
                i += 1
        elif (
            re.match(
                r"^(weapons?|goods|coin|food|services)(\s+\w+){0,3}$",
                line.strip(),
                re.I,
            )
            and peek(0).lower() in {"value", "&", "and"}
        ):
            # "weapons armor" then "value" or "&" then maybe "value" implied
            parts = [line.strip()]
            i += 1
            while i < n and lines[i].lower() in {"&", "and", "value"}:
                if lines[i].lower() == "value":
                    parts.append("value")
                i += 1
            val_title = " ".join(parts) if "value" in " ".join(parts).lower() else (
                " ".join(parts) + " value"
            )
        if val_title:
            rows: list[tuple[str, str]] = []
            pending_val: str | None = None
            while i < n:
                L = lines[i]
                if L.startswith("\x02"):
                    break
                # stop at new section/table/stat
                if (
                    looks_like_value_header(L)
                    or ROLL_HEADER_RE.match(L)
                    or ROLL_HEADER_DICE_ONLY.match(L)
                    or (
                        looks_like_heading(L)
                        and not VALUE_ROW_RE.match(L)
                        and not LONE_VALUE_RE.match(L)
                        and len(L) < 40
                    )
                    or looks_like_tag_line(L)
                    or HP_LINE_RE.search(L)
                ):
                    # don't stop on short goods names that look like headings
                    if looks_like_heading(L) and VALUE_ROW_RE.match(L):
                        pass
                    elif looks_like_value_header(L) or ROLL_HEADER_RE.match(L) or ROLL_HEADER_DICE_ONLY.match(L) or looks_like_tag_line(L) or HP_LINE_RE.search(L):
                        break
                    elif looks_like_heading(L) and L.lower() not in {
                        "block & tackle",
                        "wheelbarrow",
                        "cartload of timber (immobile)",
                    }:
                        # section headings like "Special items", "Moves"
                        if not re.search(r"\d|free|\+|armor|weapon|tool|cart|mirror|lock|silver|apartment|house|killing|guide|crew|prospector|servant|bodyguard|engineer|assassin", L, re.I):
                            if L[0].isupper() and len(L.split()) <= 4 and not VALUE_ROW_RE.match(L):
                                break

                # Lone value on its own line (column layout: value | item)
                if LONE_VALUE_RE.match(L):
                    pending_val = LONE_VALUE_RE.match(L).group(1)
                    i += 1
                    continue

                parsed = parse_value_row(L)
                if parsed:
                    item, val = parsed
                    i += 1
                    # continuations of item description (no value yet on next)
                    while (
                        i < n
                        and should_join(item, lines[i])
                        and not parse_value_row(lines[i])
                        and not LONE_VALUE_RE.match(lines[i])
                        and not looks_like_value_header(lines[i])
                    ):
                        item = item + " " + lines[i]
                        i += 1
                    rows.append((item, val))
                    pending_val = None
                    continue

                # pending value from previous lone digit + this is the item
                if pending_val and not looks_like_heading(L):
                    item = L
                    i += 1
                    while (
                        i < n
                        and should_join(item, lines[i])
                        and not VALUE_ROW_RE.match(lines[i])
                        and not LONE_VALUE_RE.match(lines[i])
                    ):
                        item = item + " " + lines[i]
                        i += 1
                    rows.append((item, pending_val))
                    pending_val = None
                    continue

                # item without trailing value yet; peek for lone value next
                if (
                    peek(1)
                    and LONE_VALUE_RE.match(peek(1))
                    and not looks_like_value_header(L)
                    and not L.startswith("When ")
                ):
                    item = L
                    i += 1
                    val = LONE_VALUE_RE.match(lines[i]).group(1)
                    i += 1
                    rows.append((item, val))
                    continue

                # Item with no price on the line (Hauberk, Spare parts) — include with "—"
                # only if clearly still inside the table (next line is a value row or header)
                if (
                    rows
                    and not looks_like_heading(L)
                    and not looks_like_value_header(L)
                    and not L.startswith("When ")
                    and (
                        (peek(1) and parse_value_row(peek(1)))
                        or (peek(1) and looks_like_value_header(peek(1)))
                        or (peek(1) and LONE_VALUE_RE.match(peek(1)))
                    )
                ):
                    rows.append((L, "—"))
                    i += 1
                    continue

                # soft-join continuation of last item (no value on wrapped line)
                if rows and should_join(rows[-1][0], L) and not parse_value_row(L):
                    item, val = rows[-1]
                    rows[-1] = (item + " " + L, val)
                    i += 1
                    continue

                break

            if rows:
                # Prettier title
                title = re.sub(r"\s+", " ", val_title)
                title = re.sub(r"\s*&\s*", " & ", title)
                if not title.lower().endswith("value"):
                    title = title + " value"
                out.append(
                    render_value_table(
                        title.title(),
                        rows,
                        lookup,
                        current_slug,
                        section_index,
                        link_kw=link_kw,
                    )
                )
                continue
            # fall through if no rows collected — reprocess line as normal
            # (i may have advanced; if no rows, emit title as heading)
            out.append(f"<h2>{html.escape(val_title)}</h2>")
            continue

        # --- Roll table header ---
        # Patterns: "1d12 theme", "theme"+"1d12", "1d12" alone, "label 1d12"
        # (roll tables are CSS-styled, so parse/render on de-tokenized text)
        dpeek1 = _defmt(peek(1))
        dice = label = None
        # "1d4 HP:" left standing alone is an insert's stat line, never a table
        if re.match(r"^\d{0,2}d\d+\s+(HP|hit points|damage|armor)\b[:.]?\s*$", line, re.I):
            out.append(f"<p>{link(line)}</p>")
            i += 1
            continue
        m = ROLL_HEADER_RE.match(line)
        m_rev = ROLL_HEADER_REV_RE.match(line)
        if m:
            dice, label = m.group(1), m.group(2).strip()
        elif m_rev and not ENTRY_RE.match(line):
            label, dice = m_rev.group(1).strip(), m_rev.group(2)
        elif (
            len(line) <= 40
            and not ENTRY_RE.match(line)
            and ROLL_HEADER_DICE_ONLY.match(dpeek1)
        ):
            # "theme" then "1d12"
            label = line
            dice = dpeek1
            i += 1  # consume dice line below after setting
        elif ROLL_HEADER_DICE_ONLY.match(line):
            dice = line
            # label may be previous short heading already emitted, or next line
            if out and re.search(r"<h2>[^<]{1,40}</h2>\s*$", out[-1]):
                label = re.sub(r"</?h2>", "", out[-1]).strip()
                out.pop()
            elif dpeek1 and len(dpeek1) < 40 and not ENTRY_RE.match(dpeek1):
                # only treat next as label if it does NOT look like entry start
                if not re.match(r"^\d+", dpeek1):
                    i += 1
                    label = dpeek1
                else:
                    label = "result"
            else:
                label = "result"
        if dice:
            i += 1  # move past header line(s); label+dice path already advanced once
            entries: list[tuple[str, str]] = []
            while i < n:
                # Hairlines under dice headers are decorative — skip them so
                # the entry list is not cut off (which broke roll tables).
                if lines[i] == M_HR:
                    i += 1
                    continue
                if lines[i].startswith(M_ICON):
                    i += 1
                    continue
                if lines[i].startswith("\x02"):
                    break
                cur = _defmt(lines[i])
                e = ENTRY_RE.match(cur)
                if e:
                    num = e.group(1) + (f"-{e.group(2)}" if e.group(2) else "")
                    body = e.group(3).strip()
                    i += 1
                    # continuations (also skip decorative HRs mid-entry)
                    while i < n:
                        if lines[i] == M_HR:
                            # Peek past hairlines: a following roll header means
                            # the next table starts — do not consume the HR/header.
                            j = i + 1
                            while j < n and (
                                lines[j] == M_HR or lines[j].startswith(M_ICON)
                            ):
                                j += 1
                            if j < n and looks_like_roll_header(_defmt(lines[j])):
                                break
                            i += 1
                            continue
                        if lines[i].startswith("\x02"):
                            break
                        nxt = _defmt(lines[i])
                        if ENTRY_RE.match(nxt):
                            break
                        # Next dice table (e.g. "1d6 signs" after size row 6)
                        # must not be glued into this row — that merged tables.
                        if (
                            looks_like_roll_header(nxt)
                            or looks_like_heading(nxt)
                            or looks_like_tag_line(nxt)
                            or looks_like_value_header(nxt)
                        ):
                            break
                        # A finished sentence plus a new capital is the next
                        # paragraph, not a wrap (Sites: Sajra's 6 + tactics).
                        if body.rstrip().endswith((".", "!", "?")) and nxt[:1].isupper():
                            break
                        # Always glue non-entry lines into the current row
                        # (wrapped descriptions; e.g. wonder #9's second sentence).
                        if nxt.endswith("-") and not nxt.endswith(("–", "—", "--")):
                            body = body.rstrip("-") + nxt.lstrip("-")
                        elif body.endswith(("–", "—")):
                            body = body + nxt
                        else:
                            body = body + " " + nxt
                        i += 1
                    entries.append((num, body))
                    continue
                # stop at next section/table/stat
                if (
                    looks_like_heading(cur)
                    or looks_like_roll_header(cur)
                    or looks_like_tag_line(cur)
                ):
                    break
                # orphan continuation of last entry (before next numbered row)
                if entries and not ENTRY_RE.match(cur):
                    if (
                        looks_like_roll_header(cur)
                        or looks_like_heading(cur)
                        or looks_like_tag_line(cur)
                    ):
                        break
                    if cur.endswith(":") and cur[0:1].isupper() and len(cur) < 60:
                        break
                    num, body = entries[-1]
                    entries[-1] = (num, body + " " + cur)
                    i += 1
                    continue
                break
            # Prefer a real label over placeholder "result"
            if (not label or label == "result") and out:
                # pull from immediately previous short h2
                if re.search(r"<h2>[^<]{1,40}</h2>\s*$", out[-1]):
                    label = re.sub(r"</?h2>", "", out[-1]).strip()
                    out.pop()

            # Merge with previous incomplete table (e.g. 1-7 left col, 8-12 right)
            if entries and out:
                prev = out[-1]
                dice_l = dice.lower()
                if 'class="roll-table"' in prev and f'data-dice="{dice_l}"' in prev:
                    last_num = None
                    pm = re.findall(r'<th scope="row">([\d\-]+)</th>', prev)
                    if pm:
                        try:
                            last_num = int(pm[-1].split("-")[-1])
                        except ValueError:
                            last_num = None
                    try:
                        first_new = int(entries[0][0].split("-")[0])
                    except ValueError:
                        first_new = -1
                    if last_num is not None and first_new == last_num + 1:
                        row_html = "".join(
                            f'<tr><th scope="row">{html.escape(num)}</th>'
                            f"<td>{link(body)}</td></tr>"
                            for num, body in entries
                        )
                        out[-1] = prev.replace(
                            "</tbody></table>", row_html + "</tbody></table>"
                        )
                        continue
            if entries:
                # Sanitize garbage labels
                if label and re.search(r"[)(]", label) and len(label) < 12:
                    label = "result"
                out.append(
                    render_roll_table(
                        dice,
                        label or "result",
                        entries,
                        lookup,
                        current_slug,
                        section_index,
                        anchor_id=anchors.add(label or dice) if label else None,
                        link_kw=link_kw,
                    )
                )
                continue
            # No entries found — fall through as heading
            out.append(
                f"<h2>{dice_button(dice)} "
                f'<span class="roll-label">'
                f'{html.escape(titlecase_name(label or ""))}</span></h2>'
            )
            continue

        # --- Stat block: Name + tag line + HP/Damage ---
        _not_creature = {
            "dangers",
            "discoveries",
            "hooks",
            "lore",
            "questions",
            "impressions",
            "origins",
            "nests",
            "sites",
            "terrain",
            "themes",
            "names",
            "wasps",
            "activities",
            "resources",
            "defenses",
            "places",
            "secrets",
            "moves",
            "people",
            "living conditions",
            "folks from elsewhere",
            "shoddy construction",
            "the delves",
            "chance encounters",
            "commonly available",
            "special items",
            "trade & barter",
            "trade and barter",
        }
        dp1, dp2 = _defmt(peek(1)), _defmt(peek(2))
        is_creature_start = i in forced_creature or (
            line.lower() not in _not_creature
            and not looks_like_value_header(line)
            and not VALUE_ROW_RE.match(line)
            and (
                looks_like_inline_creature(line)
                or (
                    len(line) <= 48
                    and not line.endswith(".")
                    and (
                        (
                            looks_like_tag_line(dp1)
                            and (
                                HP_LINE_RE.search(dp2)
                                or HP_LINE_RE.search(dp1)
                                or dp2.lower().startswith("damage")
                            )
                        )
                        # tags may be missing; name then HP
                        or (
                            HP_LINE_RE.search(dp1)
                            and not looks_like_heading(dp1)
                        )
                    )
                )
            )
        )
        if is_creature_start:
            name = line
            inline_rest = ""
            if looks_like_inline_creature(line) and ":" in line:
                name, inline_rest = line.split(":", 1)
                name = name.strip()
                inline_rest = inline_rest.strip()
            # "Ferocedes Ogran, ghostly" → name + leading tags
            extra_tags: list[str] = []
            m_name = re.match(
                r"^(.+?),\s*([a-z][\w\-]*(?:\s*,\s*[a-z][\w\-]*)*)$",
                name,
            )
            if m_name:
                tail = m_name.group(2)
                bits = [t.strip() for t in tail.split(",") if t.strip()]
                if bits and all(
                    b[0:1].islower() or b.lower() in TAG_WORDS for b in bits
                ):
                    name = m_name.group(1).strip()
                    extra_tags.extend(bits)
            creature_icon = take_icon_html()
            i += 1
            block_lines: list[str] = []
            if inline_rest:
                block_lines.append(inline_rest)
            tag_prefix = list(extra_tags)  # folded into first real tag line
            # Boundaries: horizontal rules and the next creature's icon/heading.
            # Trailing bullets, checklists, Questions, in-card roll tables, and
            # flavor all stay until one of those boundaries.
            while i < n:
                L = lines[i]
                if L == M_HR:
                    # Decorative HR mid-card before roll-table entries
                    j = i + 1
                    while j < n and lines[j] == M_HR:
                        j += 1
                    if j < n and ENTRY_RE.match(_defmt(lines[j])):
                        i = j
                        continue
                    break
                if L.startswith(M_ICON):
                    break
                if L.startswith(M_H2) or L.startswith(M_VT) or L.startswith(M_TH):
                    break
                if L.startswith(M_BOX) or L == M_ENDBOX:
                    break
                if L.startswith(M_C):
                    block_lines.append(L)  # keep marker for check-list render
                    i += 1
                    continue
                if L.startswith(M_B2):
                    block_lines.append("• " + L[len(M_B2) :].strip())
                    i += 1
                    continue
                if L.startswith(M_B):
                    block_lines.append("• " + L[len(M_B) :].strip())
                    i += 1
                    continue
                if L.startswith(M_Q):
                    block_lines.append("• " + L[len(M_Q) :].strip())
                    i += 1
                    continue
                if L.startswith(M_E):
                    block_lines.append("• " + L[len(M_E) :].strip())
                    i += 1
                    continue
                if L.startswith(M_H3):
                    bare_h = L[len(M_H3) :].strip()
                    bare_l = bare_h.rstrip(":").lower()
                    if bare_l == "questions" or bare_l.startswith("questions"):
                        block_lines.append(bare_h.rstrip(":") + ":")
                        i += 1
                        continue
                    nxt = _defmt(peek(1))
                    # Type subtitle: "Forge Lord" then real tag line
                    if len(bare_h.split()) <= 4 and (
                        looks_like_tag_line(nxt)
                        or HP_LINE_RE.search(nxt or "")
                        or re.match(
                            r"^(damage|instinct|threat)\b", nxt or "", re.I
                        )
                    ):
                        tag_prefix.append(bare_h)
                        i += 1
                        continue
                    if (
                        looks_like_tag_line(nxt)
                        or HP_LINE_RE.search(nxt or "")
                        or re.match(r"^(damage|instinct|threat)\b", nxt or "", re.I)
                    ):
                        break
                    if bare_l in _not_creature:
                        break
                    break
                if L.startswith("\x02"):
                    break
                plain = _defmt(L)
                # Next GM-note creature (Sites: Spirit of the spring after
                # Wynfor & Tiwlip) — don't swallow it into this card.
                if block_lines and looks_like_inline_creature(plain):
                    break
                # Room-key / labeled note, not this creature
                # ("Trapdoor (T):", "Critters: Ants, snails…").
                if block_lines and (
                    re.match(r"^[A-Z][A-Za-z][^:]{0,40}\([A-Z0-9]\):", plain)
                    or re.match(
                        r"^(Trapdoor|Critters|Tracks|Wasps|Burrows|Hallways)\b",
                        plain,
                    )
                ):
                    break
                if tag_prefix and (
                    looks_like_tag_line(plain)
                    or HP_LINE_RE.search(plain)
                    or re.match(r"^(damage|instinct|threat)\b", plain, re.I)
                ):
                    if looks_like_tag_line(plain):
                        plain = ", ".join(tag_prefix) + ", " + plain
                    else:
                        block_lines.append(", ".join(tag_prefix))
                    tag_prefix = []
                # In-card roll table header + rows (e.g. "1d6 current task")
                # "d6 (hand, crude)" is the Damage line wrapping after its
                # die, not a roll table called "(hand, crude)" — which is what
                # it read as, and, finding no entries, was dropped outright.
                m_roll = (
                    None
                    if re.match(r"^\d{0,2}d\d+[+\-]?\d*\s*\(", plain)
                    else ROLL_HEADER_RE.match(plain)
                )
                m_dice_only = ROLL_HEADER_DICE_ONLY.match(plain)
                if m_roll or (
                    m_dice_only
                    and len(plain.split()) <= 6
                    and not plain.lower().startswith("damage")
                ):
                    if m_roll:
                        dice_s, label_s = m_roll.group(1), m_roll.group(2).strip()
                    else:
                        dice_s, label_s = plain, "result"
                        # "1d6 current task"
                        m_lab = re.match(
                            r"^(\d{0,2}d(?:4|6|8|10|12|20))\s+(.+)$",
                            plain,
                            re.I,
                        )
                        if m_lab:
                            dice_s, label_s = m_lab.group(1), m_lab.group(2).strip()
                    i += 1
                    while i < n and lines[i] == M_HR:
                        i += 1
                    entries_c: list[tuple[str, str]] = []
                    while i < n:
                        if lines[i] == M_HR:
                            break
                        if lines[i].startswith("\x02"):
                            break
                        cur_e = _defmt(lines[i])
                        em = ENTRY_RE.match(cur_e)
                        if not em:
                            break
                        num_e = em.group(1) + (
                            f"-{em.group(2)}" if em.group(2) else ""
                        )
                        body_e = em.group(3).strip()
                        i += 1
                        while i < n and not lines[i].startswith("\x02"):
                            if lines[i] == M_HR:
                                break
                            nxt_e = _defmt(lines[i])
                            if ENTRY_RE.match(nxt_e):
                                break
                            if looks_like_roll_header(nxt_e) or looks_like_heading(
                                nxt_e
                            ):
                                break
                            if (
                                body_e.rstrip().endswith((".", "!", "?"))
                                and nxt_e[:1].isupper()
                            ):
                                break
                            body_e = body_e + " " + nxt_e
                            i += 1
                        entries_c.append((num_e, body_e))
                    if entries_c:
                        # stash as a renderable HTML fragment line
                        block_lines.append(
                            "__ROLL_TABLE__"
                            + dice_s
                            + "\x01"
                            + label_s
                            + "\x01"
                            + "\x02".join(f"{a}\x03{b}" for a, b in entries_c)
                        )
                    continue
                if plain.startswith("•") or plain.startswith("·"):
                    block_lines.append("• " + plain.lstrip("•· ").strip())
                else:
                    block_lines.append(plain)
                i += 1
            if tag_prefix:
                block_lines.insert(0, ", ".join(tag_prefix))
            # A Cost is what a follower has and a monster never does
            is_follower = any(
                re.search(r"(^|[;·] )Cost\b", _defmt(b)) for b in block_lines
            )
            out.append(
                render_stat_block(
                    name,
                    block_lines,
                    lookup,
                    current_slug,
                    section_index,
                    anchor_id=anchors.add(name),
                    link_kw=link_kw,
                    check_id=next_check_id(name),
                    icon_html=creature_icon,
                    variant="follower" if is_follower else None,
                )
            )
            continue

        # --- Custom move block (ASK AROUND, CAROUSE, RECRUIT, …) ---
        # An ALL-CAPS move name in body font followed by a "When …" trigger.
        if (
            rich_mode
            and not line.startswith("\x02")
            and _is_all_caps_label(line)
            and peek(1)
            and strip_markers(peek(1)).lstrip().startswith("When ")
        ):
            name = titlecase_label(line.strip())
            hid = anchors.add(name, caps_label=True)
            i += 1
            inner: list[str] = []
            while i < n:
                L = lines[i]
                # Next move / heading / table ends this block
                if L.startswith(("\x02H", "\x02TH", "\x02VT", "\x02BOX")):
                    break
                if (
                    not L.startswith("\x02")
                    and _is_all_caps_label(L)
                    and peek(1)
                    and strip_markers(peek(1)).lstrip().startswith("When ")
                ):
                    break
                inner.append(L)
                i += 1
            # Leading plain lines are the trigger; join them into one sentence
            # (keep inline formatting sentinels so link() renders bold/italic)
            k = 0
            trig: list[str] = []
            while (
                k < len(inner)
                and not inner[k].startswith("\x02")
                and not _defmt(inner[k]).startswith(("•", "·"))
            ):
                trig.append(inner[k].strip())
                k += 1
            trigger = " ".join(t for t in trig if t.strip())
            rest_html, _ = structure_html(
                inner[k:],
                article_title,
                lookup,
                articles,
                current_slug,
                section_index=section_index,
                anchors=anchors,
                lookups=lookups,
                section_indexes=section_indexes,
                current_book=current_book,
            )
            block = (
                f'<div class="move-block" id="{html.escape(hid)}">'
                f'<h3 class="move-name">{html.escape(name)}</h3>'
            )
            if trigger:
                block += f'<p class="move-trigger">{link(trigger)}</p>'
            block += rest_html + "</div>"
            out.append(block)
            continue

        # --- "GM moves for villains:" — the threat type's own icon ---
        m_threat = THREAT_MOVES_RE.match(strip_markers(line).strip())
        if m_threat:
            icon_name = THREAT_TYPE_ICONS.get(m_threat.group(1).lower())
            if icon_name:
                out.append(
                    '<p class="threat-moves">'
                    + book_icon_img_html(f"icons/{icon_name}.svg")
                    + f"{link(orig_line)}</p>"
                )
                i += 1
                continue

        # --- Heading ---
        # In rich mode real headings arrive as markers; only ALL-CAPS move
        # names (set in body font, e.g. "ASK AROUND") still need this path.
        if rich_mode and looks_like_heading(line) and not _is_all_caps_label(line):
            out.append(f"<p>{link(line)}</p>")
            i += 1
            continue
        if looks_like_heading(line) and (
            len(line) < 45
            or line.endswith(":")
            or line in {
                "Themes",
                "Hooks",
                "Lore",
                "Questions",
                "Names",
                "Dangers",
                "Discoveries",
                "Origins",
                "Nests",
                "Impressions",
                "Always",
                "Spring",
                "Summer",
                "Autumn",
                "Winter",
                "Sites",
                "Terrain",
                "Activities",
                "Resources",
                "Defenses",
                "Places",
            }
        ):
            # Don't treat article title / doubled running heads as h2
            cleaned_h = undouble_words(line)
            if is_fully_pairwise_doubled(line) or is_running_header(
                line, article_title, near_page_top=True
            ):
                i += 1
                continue
            if normalize_text(cleaned_h).lower() == normalize_text(article_title).lower():
                i += 1
                continue
            # Rich mode already turned real Avara heads into M_H2. ALL-CAPS
            # that remain are GM-note labels (DENIZENS, THE GREEN LORD'S TOMB)
            # — h3 so they don't steal the chapter TOC.
            if _is_all_caps_label(cleaned_h):
                level = "h3"
            else:
                level = "h2" if len(cleaned_h) < 40 else "h3"
            hid = anchors.add(cleaned_h.rstrip(":"))
            out.append(
                f'<{level} id="{html.escape(hid)}">'
                f"{html.escape(T(cleaned_h.rstrip(':')))}</{level}>"
            )
            i += 1
            continue

        # --- Bullet list run ---
        if line.startswith("•") or line.startswith("·"):
            items = []
            while i < n and _defmt(lines[i]).lstrip().startswith(("•", "·")):
                items.append(_defmt(lines[i]).lstrip("•· ").strip())
                i += 1
                # join soft wraps already done; also join if next is continuation
                while i < n and should_join(items[-1], lines[i]) and not _defmt(lines[i]).lstrip().startswith(("•", "·")):
                    items[-1] = items[-1] + " " + _defmt(lines[i])
                    i += 1
            if not items:
                i += 1  # never spin on a lone bullet glyph
                continue
            out.append("<ul>")
            for it in items:
                out.append(f"<li>{link(it)}</li>")
            out.append("</ul>")
            continue

        # --- Numbered standalone list that isn't a formal 1dN table ---
        # Its entries count upward. Prose that happens to wrap onto a number
        # ("on a 10+, ask the GM" / "3 questions …; on a 7-9, ask" / "1
        # question") runs the other way and is left as the sentence it is.
        _m0 = ENTRY_RE.match(line)
        _m1 = ENTRY_RE.match(_defmt(peek(1))) if peek(1) else None
        if _m0 and _m1 and int(_m0.group(1)) < int(_m1.group(1)):
            entries = []
            while i < n and ENTRY_RE.match(_defmt(lines[i])):
                e = ENTRY_RE.match(_defmt(lines[i]))
                assert e
                num = e.group(1) + (f"-{e.group(2)}" if e.group(2) else "")
                body = e.group(3).strip()
                i += 1
                while i < n and should_join(body, lines[i]) and not ENTRY_RE.match(_defmt(lines[i])):
                    body = body + " " + _defmt(lines[i])
                    i += 1
                entries.append((num, body))
            out.append('<div class="roll-table bare-numbered"><table><tbody>')
            for num, body in entries:
                out.append(
                    f"<tr><th scope=\"row\">{html.escape(num)}</th>"
                    f"<td>{link(body)}</td></tr>"
                )
            out.append("</tbody></table></div>")
            continue

        # --- Set-off italic passage (example of play, read-aloud text) ---
        if is_set_off_italic(orig_line):
            quoted = [orig_line]
            i += 1
            # Once a passage is running, a short italic paragraph belongs to
            # it ("And we go from there.") — length only decides whether an
            # italic line can open one.
            while i < n and (
                is_set_off_italic(lines[i])
                or (
                    not lines[i].startswith("\x02")
                    and italic_coverage(lines[i]) >= 0.9
                    and not _is_item_tag_line(lines[i])
                )
            ):
                quoted.append(lines[i])
                i += 1
            inner = "".join(f"<p>{link(q)}</p>" for q in quoted)
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        # --- Regular paragraph (preserve inline bold/italic formatting) ---
        out.append(f"<p>{link(orig_line)}</p>")
        i += 1

    html_body = "\n".join(out)
    return html_body, anchors.sections


def article_html(
    lines: list[str],
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, str, list[dict]]:
    """Article HTML from its extracted lines (``extract_article_lines`` or
    the corpus).

    Returns (body_html, excerpt_text, sections).
    """
    body, sections = structure_html(
        lines,
        article_title,
        lookup,
        articles,
        current_slug,
        section_index=section_index,
        lookups=lookups,
        section_indexes=section_indexes,
        current_book=current_book,
    )
    # Excerpt: whole prose paragraphs until we have at least ~50 words,
    # always ending on a paragraph boundary
    paras: list[str] = []
    words = 0
    in_box = False
    for line in lines:
        if line == "\x02BOX":
            in_box = True
            continue
        if line == "\x02ENDBOX":
            in_box = False
            continue
        # prose paragraphs only — skip headings, lists, tables, infobox
        if in_box or line.startswith("\x02"):
            continue
        clean = strip_markers(line).strip()
        if len(clean) < 40:
            continue
        if ENTRY_RE.match(clean) or looks_like_tag_line(clean):
            continue
        if HP_LINE_RE.search(clean) or ROLL_HEADER_RE.match(clean):
            continue
        paras.append(clean)
        words += len(clean.split())
        if words >= 50:
            break
    excerpt = "\n\n".join(paras)
    if not excerpt and lines:
        excerpt = strip_markers(lines[0])
    return body, excerpt, sections


def build_page_section_map(
    per_slug_sections: dict[str, list[dict]],
    head_pages_by_slug: dict[str, list],
    articles: list[dict],
) -> dict[tuple[str, int], dict]:
    """``(slug, printed_page) → the section that page falls in``.

    Extraction records which page each heading opened on; structuring turns
    those headings into anchored sections, in the same order. Walking the two
    together says which section is current on any page of the article, which
    is what an in-article "see page 18" needs in order to name its target.
    """
    out: dict[tuple[str, int], dict] = {}
    for art in articles:
        slug = art["slug"]
        sections = per_slug_sections.get(slug) or []
        heads = head_pages_by_slug.get(slug) or []
        if not sections or not heads:
            continue
        # Pair headings with sections in document order; a heading the
        # structurer dropped or renamed simply doesn't pair.
        marks: list[tuple[int, dict]] = []
        s = 0
        for page, text in heads:
            key = normalize_section_key(text)
            for j in range(s, len(sections)):
                sec = sections[j]
                if (sec.get("norm") or normalize_section_key(sec["name"])) == key:
                    marks.append((page, sec))
                    s = j + 1
                    break
        if not marks:
            continue
        # A page ref points at where the material starts, so a section
        # opening on that page wins over the one still running down it. The
        # two are not equally good evidence, though: "opens here" is close to
        # certain, "runs through here" is a guess. Callers that already have a
        # name to go on only accept the former.
        starts: dict[int, dict] = {}
        for page, sec in marks:
            starts.setdefault(page, sec)
        current: dict | None = None
        mi = 0
        for p in range(art.get("start_page") or 0, (art.get("end_page") or 0) + 1):
            while mi < len(marks) and marks[mi][0] <= p:
                current = marks[mi][1]
                mi += 1
            sec = starts.get(p)
            if sec:
                out[(slug, p)] = dict(sec, opens_here=True)
            elif current:
                out[(slug, p)] = dict(current, opens_here=False)
    return out


def build_section_index(
    per_slug_sections: dict[str, list[dict]],
    articles: list[dict],
) -> dict:
    """Build cross-page lookup for section/monster deep links."""
    by_slug_norm: dict[tuple[str, str], str] = {}
    by_page_norm: dict[tuple[int, str], tuple[str, str]] = {}
    title_by_slug = {a["slug"]: a.get("title") or a["slug"] for a in articles}
    # Distinctive (multi-word) section names that live on exactly one article, so
    # a page ref whose label names such a section can be resolved to the right
    # article even when the printed page holds several arcana (two per page) or
    # the page->article mapping is imperfect.
    name_owner: dict[str, str] = {}
    name_sid: dict[str, str] = {}
    name_multi: set[str] = set()
    for art in articles:
        slug = art["slug"]
        sections = per_slug_sections.get(slug) or []
        for sec in sections:
            norm = sec.get("norm") or normalize_section_key(sec["name"])
            sid = sec["id"]
            by_slug_norm[(slug, norm)] = sid
            # Also index without spaces for tight matches
            by_slug_norm[(slug, norm.replace(" ", ""))] = sid
            start = art.get("start_page") or 0
            end = art.get("end_page") or start
            for p in range(start, end + 1):
                by_page_norm[(p, norm)] = (slug, sid)
                by_page_norm[(p, norm.replace(" ", ""))] = (slug, sid)
            if " " in norm:  # multi-word only, to avoid hijacking title cross-refs
                if norm in name_multi:
                    pass
                elif norm in name_owner and name_owner[norm] != slug:
                    name_multi.add(norm)
                    name_owner.pop(norm, None)
                    name_sid.pop(norm, None)
                else:
                    name_owner[norm] = slug
                    name_sid[norm] = sid
    by_name_unique = {
        norm: (slug, name_sid[norm], title_by_slug.get(slug, slug))
        for norm, slug in name_owner.items()
    }
    return {
        "by_slug_norm": by_slug_norm,
        "by_page_norm": by_page_norm,
        "by_name_unique": by_name_unique,
    }


def resolve_unique_section(
    label: str | None, section_index: dict | None
) -> tuple[str, str, str] | None:
    """(slug, section_id, article_title) for a label that uniquely names a
    multi-word section anywhere in the book, else None."""
    if not label or not section_index:
        return None
    uniq = section_index.get("by_name_unique") or {}
    if not uniq:
        return None
    keys = _section_match_keys(label)
    for k in keys:
        if k in uniq:
            return uniq[k]
    # Some arcana discovery headings are extracted truncated (a wrapped title
    # like "Runes around a ruined hall" indexed as "Runes around"). Fall back to
    # the label's leading words (>= 2) so the hook still resolves.
    for k in keys:
        words = k.split()
        for j in range(len(words) - 1, 1, -1):
            pref = " ".join(words[:j])
            if pref in uniq:
                return uniq[pref]
    return None


def extract_section_html_blocks(body: str, section_meta: list[dict]) -> dict[str, dict]:
    """
    Pull full HTML for each deep-link target from a page body.

    Returns {id: {name, html, kind}} for stat-blocks, roll-tables, and
    heading sections (heading + following content until next same/higher heading).
    """
    name_by_id = {s["id"]: s.get("name") or s["id"] for s in (section_meta or [])}
    out: dict[str, dict] = {}

    # Self-contained blocks (stat blocks, tables, discoveries, steading
    # improvements, hazards, moves — anything with a class + id that deep-links).
    _block_classes = (
        "stat-block|roll-table|value-table|discovery-block|hazard-block|"
        "steading-improvement|move-block|infobox"
    )

    def _capture_div_block(start: int) -> str | None:
        depth = 0
        i = start
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
                    return body[start:i]
                continue
            i += 1
        return None

    for m in re.finditer(
        rf'<div\s+class="({_block_classes})"\s+id="([^"]+)"',
        body,
        re.I,
    ):
        kind, sid = m.group(1), m.group(2)
        html_block = _capture_div_block(m.start())
        if not html_block:
            continue
        out[sid] = {
            "name": name_by_id.get(sid, sid.replace("-", " ").title()),
            "html": html_block,
            "kind": kind,
        }

    for m in re.finditer(
        rf'<div\s+id="([^"]+)"\s+class="({_block_classes})"',
        body,
        re.I,
    ):
        sid, kind = m.group(1), m.group(2)
        if sid in out:
            continue
        html_block = _capture_div_block(m.start())
        if not html_block:
            continue
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

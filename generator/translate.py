"""
Translations of the corpus: the same marker file in another language.

A page's text lives in the corpus as marker lines (``extracted/<book>/
<slug>.txt``) and, for a hand-authored sheet, in ``pages/<slug>.txt`` — the
same format. A translation is that file with its **text** in another
language and everything else — tags, keys, ids, defaults, numbers — left
exactly as the English has it, line for line::

    i18n/corpus/<code>/book1/<slug>.txt     the book's text
    i18n/corpus/<code>/pages/<slug>.txt     the sheet

Nobody edits those by hand. ``extract_work`` writes a *work file* holding
only the translatable text — one line per corpus line, ``ref TAB tag TAB
text…`` — and ``apply_work`` folds a translated work file back over the
English skeleton, so the skeleton cannot drift: a translator never sees a
key, and a dropped line fails the apply.

How a translated page is rendered
---------------------------------
The book's HTML is made from the **English** lines, so every classifier in
``structure.py`` (what is a heading, a requirement list, a value table)
sees the text it was written for, and every id and link is the English
one. At the points where text becomes HTML, a :class:`TextMemory` built
from the aligned pair swaps each English line for its translation. A sheet
renders from the translated lines directly, with ids taken from the
English sheet (``render_sheet(id_lines=...)``).

Lines the renderer never emits whole (a heading it merged, a phrase it cut
out) come through untranslated; ``TextMemory.unused()`` names them after a
render so the build can say how much of a page reached the reader.
"""

from __future__ import annotations

import hashlib
import re
from html import unescape as html_unescape
from pathlib import Path

from . import REPO_ROOT
from .corpus import CorpusError, decode_line, encode_line, parse_text
from .text import (
    B_OFF,
    B_ON,
    I_OFF,
    I_ON,
    M_SEP,
    MARKERS,
    SHEET_MARKERS,
    _defmt,
    titlecase_label,
)

CORPUS_I18N_DIRNAME = "corpus"  # under i18n/
WORK_DIRNAME = "_work"

_FMT = (B_ON, B_OFF, I_ON, I_OFF)
_LETTER_RE = re.compile(r"[^\W\d_]")
_PLAQUE_RE = re.compile(r"^\d+(?:\s*&\s*\d+)*$")

# --------------------------------------------------------- which fields are text

# Sheet markers: the indexes of the payload sub-fields a translator changes.
# Everything else on the line is a key, an id, a count or a default.
SHEET_TEXT_FIELDS: dict[str, list[int] | str] = {
    "P": "all", "B": "all", "LI": "all", "NOTE": "all", "GLOSS": "all",
    "STATLINE": "all", "CKX": "all", "STATBLOCK": "all", "ROW": "all",
    "H2": [0, 1], "H3": [0, 1],
    "SHEET": [2],
    "FIELD": [1, 2],
    "FIELDS": [1, 4],
    "NOTES": [1, 3],
    "STAT": [1, 5, 6],
    "DMG": [1, 3],
    "TRACK": [1, 3],
    "CK": [1],
    "CKW": [2],
    "INV": [2],
    "INVW": [2, 3],
    "ITEM": [1, 2],
    "PLACE": [1],
    "TYPE": [1, 3],
    "COL": [1],
    "NAMEHEAD": [2, 3, 4],
}
# Book markers (the extractor's): text unless the field is a number.
BOOK_TEXT_TAGS = {
    "P", "H2", "H3", "H4", "B", "B2", "Q", "BC", "E", "C", "C2", "CX",
    "CX2", "TH", "VT", "VA", "VF", "WRITE",
}
BOOK_FIELD_TAGS = {"VR": [0], "STEP": [1]}


def _has_letters(s: str) -> bool:
    return bool(_LETTER_RE.search(_defmt(s)))


def text_field_indexes(tag: str, parts: list[str], *, sheet: bool) -> list[int]:
    """Which of ``parts`` a translator may change (empty fields excluded)."""
    if sheet and tag in SHEET_TEXT_FIELDS:
        spec = SHEET_TEXT_FIELDS[tag]
        idx = range(len(parts)) if spec == "all" else spec
    elif not sheet and tag in BOOK_TEXT_TAGS:
        idx = range(len(parts))
    elif not sheet and tag in BOOK_FIELD_TAGS:
        idx = BOOK_FIELD_TAGS[tag]
    elif sheet and tag == "P":
        idx = range(len(parts))
    else:
        return []
    out = []
    for i in idx:
        if i < len(parts) and parts[i].strip() and _has_letters(parts[i]):
            if tag in ("H2", "H3") and not sheet and _PLAQUE_RE.match(parts[i].strip()):
                continue  # a heading's plaque number
            out.append(i)
    return out


# ------------------------------------------------------------------ files

def i18n_corpus_dir(code: str) -> Path:
    return REPO_ROOT / "i18n" / CORPUS_I18N_DIRNAME / code


def work_dir() -> Path:
    return REPO_ROOT / "i18n" / WORK_DIRNAME / "corpus"


def english_sources(slug: str) -> dict[str, Path]:
    """The English files a translation of ``slug`` is made from:
    ``{"book": extracted/<book>/<slug>.txt, "sheet": pages/<slug>.txt}``,
    whichever exist."""
    out: dict[str, Path] = {}
    for bdir in sorted((REPO_ROOT / "extracted").glob("book*")):
        p = bdir / f"{slug}.txt"
        if p.is_file():
            out["book"] = p
            break
    p = REPO_ROOT / "pages" / f"{slug}.txt"
    if p.is_file():
        out["sheet"] = p
    return out


def translation_paths(code: str, slug: str) -> dict[str, Path]:
    """Where the translated files for ``slug`` go, mirroring the English."""
    src = english_sources(slug)
    out: dict[str, Path] = {}
    if "book" in src:
        out["book"] = i18n_corpus_dir(code) / src["book"].parent.name / f"{slug}.txt"
    if "sheet" in src:
        out["sheet"] = i18n_corpus_dir(code) / "pages" / f"{slug}.txt"
    return out


def _strip_comments(text: str) -> str:
    return "\n".join(
        ln for ln in text.split("\n") if ln and not ln.startswith("#")
    )


def source_fingerprint(sources: dict[str, str]) -> str:
    """SHA-256 of the English text a translation was made from — the file
    bodies (``book``, ``sheet``) with their comment lines dropped, so an
    edit to a header does not flag every translation stale."""
    h = hashlib.sha256()
    for kind in ("book", "sheet"):
        if kind in sources:
            h.update(kind.encode())
            h.update(_strip_comments(sources[kind]).encode("utf-8"))
    return h.hexdigest()


META_KEYS = ("title", "nav_label", "description")
_META_RE = re.compile(r"^#\s*(lang|source_sha256|title|nav_label|description):\s*(.*)$")


def read_meta(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for ln in text.split("\n"):
        if not ln.startswith("#"):
            if ln:
                break
            continue
        m = _META_RE.match(ln)
        if m:
            meta[m.group(1)] = m.group(2).strip()
    return meta


# ------------------------------------------------------------- work files

def _file_lines(text: str) -> list[str]:
    return text.split("\n")


def _split_file_line(raw: str) -> tuple[str, list[str]]:
    tag, *parts = raw.split("\t")
    return tag, parts


def extract_work(
    slug: str,
    *,
    book_range: tuple[int, int] | None = None,
    book_only: bool = False,
) -> str:
    """The work file for ``slug``: its translatable text, one line each.

    ``book_range`` limits the book lines to physical lines ``(first, last)``
    of the corpus file (1-based, inclusive) — the steading playbook's
    corpus, for one, holds pages the sheet replaces, and there is no point
    paying to translate them.
    """
    src = english_sources(slug)
    if not src:
        raise CorpusError(f"{slug}: no English source under extracted/ or pages/")
    out = [
        f"# {slug} — work file. Translate the text after the tag; keep the "
        "reference and the tag exactly, and keep <b>…</b> / <i>…</i> where they are.",
        "# Fields are tab-separated. A line with two text fields must come back with two.",
        "META\ttitle\t",
        "META\tnav_label\t",
        "META\tdescription\t",
    ]
    for kind, prefix in (("sheet", "S"), ("book", "B")):
        if kind not in src or (book_only and kind == "sheet"):
            continue
        for n, raw in enumerate(_file_lines(src[kind].read_text(encoding="utf-8")), 1):
            if not raw or raw.startswith("#") or raw.startswith("PAGE\t"):
                continue
            if kind == "book" and book_range and not (book_range[0] <= n <= book_range[1]):
                continue
            tag, parts = _split_file_line(raw)
            idx = text_field_indexes(tag, parts, sheet=(kind == "sheet"))
            if not idx:
                continue
            out.append(f"{prefix}{n}\t{tag}\t" + "\t".join(parts[i] for i in idx))
    return "\n".join(out) + "\n"


def apply_work(
    slug: str, code: str, work_text: str
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Fold a translated work file over the English skeleton.

    Returns ``(files, meta, problems)``: ``files`` maps ``"book"``/``"sheet"``
    to the translated file text, ready to write. A problem is a work line
    that names a line the English does not have, changes its tag, or comes
    back with the wrong number of text fields — the line is left English
    and reported.
    """
    src = english_sources(slug)
    texts = {k: p.read_text(encoding="utf-8") for k, p in src.items()}
    lines = {k: _file_lines(t) for k, t in texts.items()}
    meta: dict[str, str] = {}
    problems: list[str] = []
    for wn, raw in enumerate(work_text.split("\n"), 1):
        if not raw or raw.startswith("#"):
            continue
        cells = raw.split("\t")
        if cells[0] == "META":
            if len(cells) >= 3 and cells[2].strip():
                meta[cells[1].strip()] = cells[2].strip()
            continue
        ref, tag = cells[0], cells[1] if len(cells) > 1 else ""
        kind = {"S": "sheet", "B": "book"}.get(ref[:1])
        try:
            n = int(ref[1:])
        except ValueError:
            n = -1
        if kind not in lines or not (1 <= n <= len(lines[kind])):
            problems.append(f"work line {wn}: no such source line {ref!r}")
            continue
        en_raw = lines[kind][n - 1]
        en_tag, en_parts = _split_file_line(en_raw)
        if en_tag != tag:
            problems.append(f"work line {wn}: {ref} is {en_tag}, not {tag}")
            continue
        idx = text_field_indexes(en_tag, en_parts, sheet=(kind == "sheet"))
        given = cells[2:]
        if len(given) != len(idx):
            problems.append(
                f"work line {wn}: {ref} needs {len(idx)} text field(s), got {len(given)}"
            )
            continue
        new_parts = list(en_parts)
        for i, val in zip(idx, given):
            if val.strip():
                new_parts[i] = val
        # Must still decode: a bare '<' or an unknown escape is a mistake.
        candidate = tag + "\t" + "\t".join(new_parts)
        try:
            decode_line(candidate, f"{ref}")
        except CorpusError as e:
            problems.append(f"work line {wn}: {e}")
            continue
        lines[kind][n - 1] = candidate
    fp = source_fingerprint(texts)
    files: dict[str, str] = {}
    for kind, ls in lines.items():
        header = [
            f"# {slug} · {code} · translation of the English {kind} text.",
            "# Generated by i18n/corpus_xlate.py apply — edit the work file, not this.",
            f"# lang: {code}",
            f"# source_sha256: {fp}",
        ]
        for k in META_KEYS:
            if meta.get(k):
                header.append(f"# {k}: {meta[k]}")
        body = [ln for ln in ls if not ln.startswith("#")]
        files[kind] = "\n".join(header + body).rstrip("\n") + "\n"
    return files, meta, problems


# ------------------------------------------------------------ alignment

def _tag_of(line: str) -> str:
    if line.startswith("\x02"):
        head = line.split(" ", 1)[0]
        return head[1:]
    return "P"


def check_alignment(en: list[str], tr: list[str], where: str = "") -> list[str]:
    """Why ``tr`` is not a translation of ``en``: an empty list when it is."""
    out: list[str] = []
    if len(en) != len(tr):
        out.append(f"{where}: {len(tr)} lines, English has {len(en)}")
        return out
    for i, (a, b) in enumerate(zip(en, tr), 1):
        ta, tb = _tag_of(a), _tag_of(b)
        if ta != tb:
            out.append(f"{where}:{i}: {tb} where English has {ta}")
            continue
        if a.count(M_SEP) != b.count(M_SEP):
            out.append(f"{where}:{i}: {b.count(M_SEP) + 1} fields, English has {a.count(M_SEP) + 1}")
        for tok, name in ((B_ON, "bold"), (I_ON, "italic")):
            if a.count(tok) != b.count(tok):
                out.append(
                    f"{where}:{i}: {b.count(tok)} {name} runs, English has {a.count(tok)}"
                )
    return out


def coverage(en: list[str], tr: list[str], *, sheet: bool) -> tuple[int, int]:
    """``(translated, translatable)`` lines — a line is translated when any
    of its text fields differs from the English."""
    total = done = 0
    for a, b in zip(en, tr):
        tag = _tag_of(a)
        parts = (a.split(" ", 1)[1] if a.startswith("\x02") and " " in a else a).split(M_SEP)
        if not a.startswith("\x02"):
            parts = a.split(M_SEP)
        idx = text_field_indexes(tag, parts, sheet=sheet)
        if not idx:
            continue
        total += 1
        if a != b:
            done += 1
    return done, total


# --------------------------------------------------------- text memory

_BOLD_PREFIX_RE = re.compile(r"^\x04([^\x05]+)\x05\s*(.*)$", re.S)


def _payload(line: str) -> list[str]:
    if line.startswith("\x02"):
        head, sp, rest = line.partition(" ")
        return rest.split(M_SEP) if sp else []
    return line.split(M_SEP)


class TextMemory:
    """English text → its translation, consulted where the renderer emits text.

    Keys are matched loosely — inline formatting dropped, case folded, a
    trailing colon ignored — because the renderer passes a line on in
    several forms. What comes back keeps the shape of the query: the raw
    translation (sentinels and all) for a raw query, the plain one for a
    plain query, the query's colon, and title case where the query was
    retitled from a shouted label.
    """

    def __init__(self) -> None:
        self._index: dict[str, tuple[str, str, str]] = {}  # norm → (raw, plain, en_raw)
        self.hits: set[str] = set()
        self._derived: set[str] = set()  # sub-segments of a line, not lines
        self._reverse: dict[str, str] = {}  # norm(translation) → English, plain

    @staticmethod
    def norm(s: str) -> str:
        return _defmt(s).strip().rstrip(":").strip().casefold()

    def add(self, en: str, tr: str, *, derived: bool = False) -> None:
        en, tr = en.strip(), tr.strip()
        if not en or not tr or en == tr:
            return
        key = self.norm(en)
        if key and key not in self._index:
            self._index[key] = (tr, _defmt(tr), en)
            self._reverse.setdefault(self.norm(tr), _defmt(en).strip())
            if derived:
                self._derived.add(key)

    @classmethod
    def from_lines(cls, en: list[str], tr: list[str]) -> "TextMemory":
        tm = cls()
        for a, b in zip(en, tr):
            if a == b or _tag_of(a) != _tag_of(b):
                continue
            pa, pb = _payload(a), _payload(b)
            if len(pa) != len(pb):
                continue
            for fa, fb in zip(pa, pb):
                tm.add(fa, fb)
                ma, mb = _BOLD_PREFIX_RE.match(fa), _BOLD_PREFIX_RE.match(fb)
                if ma and mb:
                    tm.add(ma.group(1), mb.group(1), derived=True)
                    tm.add(ma.group(2), mb.group(2), derived=True)
        return tm

    def __len__(self) -> int:
        return len(self._index)

    def get(self, s: str) -> str:
        if not s or not self._index:
            return s
        key = self.norm(s)
        hit = self._index.get(key)
        if hit is None:
            return s
        raw, plain, en_raw = hit
        self.hits.add(key)
        out = raw if any(c in s for c in _FMT) else plain
        # The renderer may have cut the English line's colon off (a heading)
        # or kept it: give the translation the same treatment, and otherwise
        # leave its punctuation to the translator.
        q_colon = s.strip().endswith(":")
        en_colon = en_raw.strip().endswith(":")
        if en_colon and not q_colon and out.rstrip().endswith(":"):
            out = out.rstrip()[:-1]
        elif q_colon and not en_colon and not out.rstrip().endswith(":"):
            out = out.rstrip() + ":"
        en_plain = _defmt(en_raw).strip()
        if (
            _defmt(s).strip() != en_plain
            and _defmt(s).strip() == titlecase_label(en_plain)
            and out.upper() == out
        ):
            out = titlecase_label(out)
        # Keep the query's surrounding whitespace.
        lead = s[: len(s) - len(s.lstrip())]
        trail = s[len(s.rstrip()):]
        return lead + out + trail

    def reverse(self, s: str) -> str | None:
        """The English a translated string came from, plain — for the ids a
        renderer makes off text it reads back out of the HTML."""
        if not s or not self._reverse:
            return None
        return self._reverse.get(self.norm(html_unescape(s)))

    def unused(self) -> list[str]:
        """English lines whose translation the renderer never asked for."""
        return [
            en
            for k, (_r, _p, en) in self._index.items()
            if k not in self.hits and k not in self._derived
        ]


# ------------------------------------------------------ loading translations

def load_corpus_translations(code: str) -> dict[str, dict]:
    """Every translated file for ``code``: ``{slug: {"book": (lines, pages,
    text), "sheet": (...), "meta": {...}}}``."""
    root = i18n_corpus_dir(code)
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*/*.txt")):
        slug = path.stem
        kind = "sheet" if path.parent.name == "pages" else "book"
        try:
            text = path.read_text(encoding="utf-8")
            lines, pages = parse_text(text, str(path))
        except (OSError, CorpusError) as e:
            print(f"  i18n: {code}/{path.parent.name}/{path.name}: {e}")
            continue
        entry = out.setdefault(slug, {"meta": {}})
        entry[kind] = (lines, pages, text)
        entry["meta"].update(read_meta(text))
    return out


# ------------------------------------------------------------ rendering

def render_translated(
    tr: dict,
    code: str,
    art: dict,
    en_lines: list[str],
    en_pages: list[int],
    ov: dict | None,
    lookup: dict,
    articles: list[dict],
    common: dict,
) -> tuple[dict | None, list[str]]:
    """One page in one language, from its corpus translation.

    Returns ``(page, notes)`` in the shape ``write_localized_pages`` reads —
    ``body_html`` (no ``<h1>``), ``title``, ``nav_label``, ``description``,
    ``sections`` — or ``(None, problems)`` when the translation is not
    aligned with the English and cannot be trusted to render.
    """
    from .chrome import apply_override, override_sections
    from .corpus import dump_text
    from .sheet import sheet_excerpt
    from .structure import article_html, linkify_pages, set_translation

    slug = art["slug"]
    meta = tr.get("meta") or {}
    notes: list[str] = []
    where = f"{code}/{slug}"

    tm: TextMemory | None = None
    if "book" in tr:
        problems = check_alignment(en_lines, tr["book"][0], f"{where} (book)")
        if problems:
            return None, problems[:5]
        tm = TextMemory.from_lines(en_lines, tr["book"][0])
    sheet_lines: list[str] | None = None
    if ov is not None:
        if ov["kind"] != "sheet":
            notes.append(f"{where}: the override is HTML; it stays English")
        elif "sheet" in tr:
            problems = check_alignment(ov["lines"], tr["sheet"][0], f"{where} (sheet)")
            if problems:
                return None, problems[:5]
            sheet_lines = tr["sheet"][0]
        else:
            notes.append(f"{where}: sheet not translated (pages/{slug}.txt) — shown in English")

    def link_fn(text: str) -> str:
        return linkify_pages(
            text,
            lookup,
            common.get("current_slug"),
            common.get("section_index"),
            lookups=common.get("lookups"),
            section_indexes=common.get("section_indexes"),
            current_book=common.get("current_book"),
        )

    set_translation(tm)
    try:
        body, excerpt, secs = article_html(
            en_lines, art["title"], lookup, articles, **common
        )
        if ov is not None:
            body = apply_override(ov, body, slug=slug, link_fn=link_fn, lines=sheet_lines)
            secs = override_sections(body)
    finally:
        set_translation(None)

    sections: dict[str, str] = {}
    for sec in secs:
        name = sec.get("name") or ""
        sections[sec["id"]] = tm.get(name) if tm else name

    title = meta.get("title") or (tm.get(art["title"]) if tm else "") or art["title"]
    description = meta.get("description") or ""
    if not description:
        if sheet_lines:
            description = sheet_excerpt(sheet_lines)
        if not description and "book" in tr:
            description = sheet_excerpt(tr["book"][0])
        if not description:
            description = excerpt

    # Stale: the English moved under the translation.
    sources: dict[str, str] = {}
    if "book" in tr:
        sources["book"] = dump_text(en_lines, en_pages)
    if ov is not None and ov["kind"] == "sheet":
        sources["sheet"] = ov["text"]
    fp = source_fingerprint(sources)
    if meta.get("source_sha256") and meta["source_sha256"] != fp:
        notes.append(f"{where}: stale — the English text changed since it was translated")

    # How much of it reached the reader.
    parts = []
    if "book" in tr:
        done, total = coverage(en_lines, tr["book"][0], sheet=False)
        leaks = tm.unused() if tm else []
        parts.append(f"book {done}/{total} lines translated" + (f", {len(leaks)} not shown whole" if leaks else ""))
        if leaks:
            notes.append(f"{where}: not shown whole: " + "; ".join(_defmt(x)[:40] for x in leaks[:4]))
    if sheet_lines is not None:
        done, total = coverage(ov["lines"], sheet_lines, sheet=True)
        parts.append(f"sheet {done}/{total} lines translated")
    notes.append(f"{where}: " + ", ".join(parts))

    return {
        "slug": slug,
        "body_html": body,
        "title": title,
        "nav_label": meta.get("nav_label") or title,
        "description": description,
        "sections": sections,
        "source_sha256": None,
    }, notes


__all__ = [
    "TextMemory",
    "render_translated",
    "apply_work",
    "check_alignment",
    "coverage",
    "english_sources",
    "extract_work",
    "load_corpus_translations",
    "read_meta",
    "source_fingerprint",
    "text_field_indexes",
    "translation_paths",
    "work_dir",
]

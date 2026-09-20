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
import json
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
    TAG_MIN,
    TAG_RE,
    TAG_WORDS,
    _cancel_fmt_seam,
    _defmt,
    _split_leading_fmt,
    _split_trailing_fmt,
    has_tags,
    strip_tags,
    tag_char,
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
# A playbook's stat block is one JSON payload; its text is the gloss under
# the heading and the HP label with the sheet's cap. The rest of it — stat
# names, debilities, tracks — is the same on every sheet and is translated
# once per language, in i18n/ui/<code>.json (``sheet``).
STATS_TEXT_KEYS = ("gloss", "hp")


def _stats_block(payload: str) -> dict:
    try:
        block = json.loads(payload)
    except ValueError:
        return {}
    return block if isinstance(block, dict) else {}


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
    elif not sheet and tag == "STATS":
        # Counted as one line; its fields are spread by extract/apply_work.
        block = _stats_block(parts[0]) if parts else {}
        return [0] if any(_has_letters(str(block.get(k) or "")) for k in STATS_TEXT_KEYS) else []
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
            if kind == "book" and tag == "STATS":
                block = _stats_block(parts[0])
                out.append(f"{prefix}{n}	{tag}	" + "	".join(
                    str(block.get(k) or "") for k in STATS_TEXT_KEYS))
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
        given = cells[2:]
        if kind == "book" and en_tag == "STATS":
            if len(given) != len(STATS_TEXT_KEYS):
                problems.append(
                    f"work line {wn}: {ref} needs {len(STATS_TEXT_KEYS)} text field(s), got {len(given)}"
                )
                continue
            block = _stats_block(en_parts[0])
            for k, val in zip(STATS_TEXT_KEYS, given):
                if val.strip() and block.get(k):
                    block[k] = val.strip()
            lines[kind][n - 1] = tag + "	" + json.dumps(block, ensure_ascii=False)
            continue
        idx = text_field_indexes(en_tag, en_parts, sheet=(kind == "sheet"))
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


# --------------------------------------------------------- provenance tags

# Where in a field the tag goes: after its opening formatting sentinels and
# after whatever the renderer strips off the front of a line — a bullet, an
# ellipsis, inventory diamonds, a roll row's numbers, a roll head's dice —
# so every one of those strips still finds what it looks for at the start.
_TAG_AT_RE = re.compile(
    r"^(?:[\s…\.•·◇,、，\x04-\x07]|\d+(?:[-\u2013]\d+)?\s+|\d*d\d+\s+)*"
)


def tag_lines(lines: list[str]) -> tuple[list[str], list[tuple[int, int]]]:
    """``lines`` with every translatable field carrying its unit tag, and
    ``units[k] = (line, field)`` for tag ``k``.

    A tag is one character from Supplementary Private Use Area-A
    (``text.tag_char``), put after the field's opening sentinels and leading
    bullet or numbers (``_TAG_AT_RE``), so a bold label still opens with its
    ``\\x04`` and a bullet is still stripped. It rides through whatever
    the renderer does to the text — lines gathered into a paragraph, a line
    cut at a label, a bullet dropped — and says, where the text becomes HTML,
    which fields it was made from; :class:`TextMemory` then takes the same
    fields from the translation. ``_defmt`` and ``strip_markers`` drop tags,
    so no classifier ever sees one. The English pages are built from tagged
    lines too, which is what keeps their analysis and a translation's the
    same by construction (``T()`` strips the tags when nothing translates).
    """
    out: list[str] = []
    units: list[tuple[int, int]] = []
    for li, line in enumerate(lines):
        tag = _tag_of(line)
        if line.startswith("\x02"):
            head, sp, payload = line.partition(" ")
            if not sp:
                out.append(line)
                continue
            head += sp
        else:
            head, payload = "", line
        parts = payload.split(M_SEP)
        idx = text_field_indexes(tag, parts, sheet=False)
        if tag == "STATS":  # the stat block is one JSON payload
            out.append(line)
            continue
        # A line with no letters — a page number the extractor left behind,
        # a roll row's number on its own line — is nothing to translate,
        # but the renderer gathers it with its neighbours all the same, and
        # the gathering has to account for it.
        if not idx and tag in BOOK_TEXT_TAGS and len(parts) == 1 and parts[0].strip():
            idx = [0]
        if not idx:
            out.append(line)
            continue
        for f in idx:
            p = parts[f]
            at = _TAG_AT_RE.match(p).end()
            if at >= len(p):
                continue
            parts[f] = p[:at] + tag_char(len(units)) + p[at:]
            units.append((li, f))
        out.append(head + M_SEP.join(parts))
    return out, units


def _split_tagged(s: str, leads: list[str]) -> list[tuple[int | None, str]]:
    """``s`` cut at its tags: ``(unit, text)`` per piece, the first piece
    (unit ``None``) whatever came before the first tag. A field's tag sits
    after its opening sentinel and leading bullet or numbers (``leads[k]``),
    which therefore end the piece before; they are handed back to the piece
    they belong to, so it reads as its field does."""
    segs: list[tuple[int | None, str]] = []
    unit: int | None = None
    start = 0
    for m in TAG_RE.finditer(s):
        segs.append((unit, s[start:m.start()]))
        unit = ord(m.group()) - TAG_MIN
        start = m.end()
    segs.append((unit, s[start:]))
    for i in range(len(segs) - 1):
        u, t = segs[i]
        k = segs[i + 1][0]
        lead = leads[k] if k is not None and k < len(leads) else ""
        if lead and t.endswith(lead):
            j = len(t) - len(lead)
        else:
            j = len(t)
            while j > 0 and t[j - 1] in (B_ON, I_ON):
                j -= 1
        if j < len(t):
            segs[i] = (u, t[:j])
            segs[i + 1] = (k, t[j:] + segs[i + 1][1])
    return segs


# --------------------------------------------------------- text memory

_BULLET_RE = re.compile(r"^[•·]\s*")
_NAMED_MOVE_TR_RE = re.compile(r"^\x04[^\x05]+\x05\s*(?:\x06([^\x07]*)\x07\s*)?(.*)$", re.S)
_BOLD_SPLIT_RE = re.compile(r"\s+(?=\x04)")
_ROLL_ROW_RE = re.compile(r"^(\d+(?:[-–]\d+)?)\s+(.+)$", re.S)
# A move block sets its trigger apart from the words around it. The
# trigger can be several formatted runs in a row (the book broke the
# line), and the block shows them as one phrase.
_TRIGGER_RE = re.compile(r"((?:\x04[^\x05]*\x05\s*)+)")


def _trigger_segments(text: str) -> list[str]:
    """``text`` cut into its runs of formatting and the words between."""
    return [p for p in _TRIGGER_RE.split(text) if p.strip()]


_BOLD_PREFIX_RE = re.compile(r"^\x04([^\x05]+)\x05\s*(.*)$", re.S)
_DICE_HEAD_RE = re.compile(r"^\s*(\d*d\d+)\s+(.+)$")
_ELLIPSIS_RE = re.compile(r"^[\s…\.]+")
_MAX_EN_RE = re.compile(r"\s+Max\.?\s*\d+(?=\s|$)")
_MAX_TR_RE = re.compile(r"\s+(?:Max|Máx)\.?\s*\d+(?=\s|$)")
_MAX_HEAD_EN_RE = re.compile(r"^\s*Max\.?\s+\d+\s+(.+)$")
_MAX_HEAD_TR_RE = re.compile(r"^\s*\S+\s+\d+\s+(.+)$")
# A creature whose identity line opens with its name and closes with its
# tags ("Archer, observant, eager, rookie"): the block sets the name as the
# heading and the tags under it, so each is asked for on its own. The
# English pattern is `structure.py`'s (lowercase tag words); a translation's
# tags may open with an accented letter, so that side only has to agree on
# how many there are.
_CREATURE_EN_RE = re.compile(r"^(.+?),\s*([a-z][\w\-]*(?:\s*,\s*[a-z][\w\-]*)*)$")
_CREATURE_TR_RE = re.compile(
    r"^(.+?),\s*([^\W\d_][\w\- ]*(?:\s*,\s*[^\W\d_][\w\- ]*)*)$"
)


def _cf(s: str) -> str:
    """Formatting and tags dropped, case folded — the character-wise half of
    :meth:`TextMemory.norm`."""
    return _defmt(s).casefold()


def _norm_tail(s: str) -> str:
    """The rest of :meth:`TextMemory.norm`, over text ``_cf`` has been through."""
    s = s.strip()
    if s and s[0] in "•·":
        s = _BULLET_RE.sub("", s)
    return s.rstrip(":").strip()


def _payload(line: str) -> list[str]:
    if line.startswith("\x02"):
        head, sp, rest = line.partition(" ")
        return rest.split(M_SEP) if sp else []
    return line.split(M_SEP)


def _join_dehyphenated(parts) -> str:
    """Join lines the way a block does, putting a broken word back together
    — the hyphen may sit inside a formatting run ("…mar-</i></b>" + "ble"),
    and the seam's closing and opening sentinels cancel."""
    out = ""
    for part in parts:
        if not out:
            out = part
            continue
        body, tail = _split_trailing_fmt(out.rstrip())
        lead, rest = _split_leading_fmt(part)
        if body.endswith("-") and rest[:1].islower():
            out = body[:-1] + _cancel_fmt_seam(tail, lead) + rest
        else:
            out = out + " " + part
    return out


_CALLOUT_RE = re.compile(r"^(.*\S)\s+(\d{1,2})$")


def derive_variants(
    en: str, tr: str, tag: str = "P"
) -> tuple[list[tuple[str, str, bool]], bool]:
    """What a renderer asks for of a line beyond the line whole, paired with
    the same cut of its translation: ``(english, translation, shows_line)``,
    ``shows_line`` when a hit means the line reached the reader. Also whether
    the line is only ever shown in pieces (an arcanum's named move), so it
    is not reported as a leak. Works over a run of lines gathered into one
    the same way."""
    from .arcana import (
        _arcana_named_move,
        _arcana_strip_tag_seps,
        _arcana_tags_prose,
        _dedupe_arcana_title,
    )

    out: list[tuple[str, str, bool]] = []

    def add(a: str, b: str, shows: bool = True) -> None:
        if a and b and a.strip() and b.strip():
            out.append((a, b, shows))

    whole_derived = False
    # A label the line opens with, and the words after it ("Instinct" / "to …").
    ma, mb = _BOLD_PREFIX_RE.match(en), _BOLD_PREFIX_RE.match(tr)
    if ma and mb:
        add(ma.group(1), mb.group(1), False)
        add(ma.group(2), mb.group(2), False)
    # A stat block cuts a line where a bold label opens ("… Damage spear d8
    # …", "… Cost proof of honor …"); every run of consecutive pieces, since
    # a block may cut the line at the last label only.
    sa, sb = _BOLD_SPLIT_RE.split(en), _BOLD_SPLIT_RE.split(tr)
    if 1 < len(sa) == len(sb):
        for x in range(len(sa)):
            for y in range(x + 1, len(sa) + 1):
                ja, jb = " ".join(sa[x:y]), " ".join(sb[x:y])
                add(ja, jb)
                # The block shows a label ("Instinct") apart from the words
                # that follow it.
                pa, pb = _BOLD_PREFIX_RE.match(ja), _BOLD_PREFIX_RE.match(jb)
                if pa and pb:
                    add(pa.group(2), pb.group(2))
    # A move block sets its trigger apart from the words around it ("When
    # you" / "take time to catch your breath" / ", ...").
    ra, rb = _trigger_segments(en), _trigger_segments(tr)
    if 1 < len(ra) == len(rb):
        for xa, xb in zip(ra, rb):
            add(xa, xb)
    da, db = _defmt(en), _defmt(tr)
    # A stat block's heading drops a callout number the book set after it
    # (Book I's anatomy of a monster: "Crinwin 1").
    ca, cb = _CALLOUT_RE.match(da), _CALLOUT_RE.match(db)
    if ca and cb and ca.group(2) == cb.group(2):
        add(ca.group(1), cb.group(1))
    # A roll table's row drops the numbers it opens with ("4-5 Clearing,
    # meadow, sparse trees").
    na, nb = _ROLL_ROW_RE.match(da), _ROLL_ROW_RE.match(db)
    if na and nb and na.group(1) == nb.group(1):
        add(na.group(2), nb.group(2))
    # A roll table's head keeps the dice apart from the label it names
    # ("1d6 discovery" → the table is titled "discovery").
    ha, hb = _DICE_HEAD_RE.match(da), _DICE_HEAD_RE.match(db)
    if ha and hb and ha.group(1) == hb.group(1):
        add(ha.group(2), hb.group(2))
    # A checklist item loses its leading ellipsis ("… is sealed with wax").
    ea, eb = _ELLIPSIS_RE.sub("", en), _ELLIPSIS_RE.sub("", tr)
    if ea != en and eb:
        add(ea, eb)
    # A minor arcanum's title line is printed twice by the extractor
    # ("A giant's dormitory giant's dormitory"); the card shows it once.
    if tag == "H3":
        dd = _dedupe_arcana_title(da)
        if dd != da:
            add(dd, tr)
    # An insert's move wrapped under the HP box: the renderer drops the
    # box's cap out of the middle of the line ("Tend to the sick,
    # injured, Max. 6 women in labor"), and shows the move without its
    # bullet. A translation writes the cap in its own words ("Máx. 6").
    if da.lstrip().startswith(("•", "·")):
        ca, cb = _MAX_EN_RE.sub("", da), _MAX_TR_RE.sub("", db)
        if ca != da and cb:
            add(ca, cb)
            add(ca.lstrip("•· "), cb.lstrip("•· "))
    # A creature's identity line, name then tags: the block shows the name as
    # its heading and the tags under it, never the line whole.
    ca, cb = _CREATURE_EN_RE.match(da), _CREATURE_TR_RE.match(db)
    if ca and cb:
        bits_a = [t.strip() for t in ca.group(2).split(",") if t.strip()]
        bits_b = [t.strip() for t in cb.group(2).split(",") if t.strip()]
        if (
            len(bits_a) == len(bits_b)
            and all(b[0:1].islower() or b.lower() in TAG_WORDS for b in bits_a)
        ):
            add(ca.group(1).strip(), cb.group(1).strip())
            add(", ".join(bits_a), ", ".join(bits_b))
    # The HP box's cap set beside a special quality: "Max. 13 lacks organs".
    m2a, m2b = _MAX_HEAD_EN_RE.match(da), _MAX_HEAD_TR_RE.match(db)
    if m2a and m2b:
        add(m2a.group(1), m2b.group(1))
    # A line opening with inventory diamonds shows without them.
    s2a, s2b = _arcana_strip_tag_seps(en), _arcana_strip_tag_seps(tr)
    if s2a != en and s2b:
        add(s2a, s2b)
    ta, pra = _arcana_tags_prose(en)
    tb, prb = _arcana_tags_prose(tr)
    if ta and not pra:
        # A bare tag line: the card shows only its tags. The words that
        # tell a tag apart are English ("+1 damage"), so the translation
        # is taken whole rather than peeled.
        add(ta, db.strip(" ◇,、，"))
    elif ta and tb:
        add(ta, tb)
        if pra and prb:
            add(pra, prb)
    named = _arcana_named_move(en)
    if named:
        # A named move: the card shows its name, its tags and its trigger
        # apart. The translated name is not in capitals, so the translation
        # is cut by its formatting instead.
        whole_derived = True
        _name, tags_en, trigger_en = named
        mn = _NAMED_MOVE_TR_RE.match(tr)
        if mn and tags_en and mn.group(1):
            add(tags_en, mn.group(1).strip(" ()"))
            add(trigger_en, mn.group(2))
    return out, whole_derived


class TextMemory:
    """English text → its translation, consulted where the renderer emits text.

    Two ways in. Text still carrying its provenance tags (see ``tag_lines``)
    names the fields it was made from: each is matched against its own field
    — whole, or as one of the field's variants — and a text the renderer
    gathered from several fields (a paragraph, a stat block's joined note)
    is matched against the variants of that run, made on demand and kept.
    Text without tags (a piece cut out of a field, a fixed word) is matched
    by content, loosely — inline formatting dropped, case folded, a trailing
    colon ignored — because the renderer passes a line on in several forms.
    What comes back keeps the shape of the query: the raw translation
    (sentinels and all) for a raw query, the plain one for a plain query,
    the query's colon, and title case where the query was retitled from a
    shouted label.
    """

    def __init__(self) -> None:
        self._index: dict[str, tuple[str, str]] = {}  # norm → (raw, en_raw)
        self._units: dict[str, tuple[int, ...]] = {}  # key → the fields it shows
        self.hits: set[str] = set()
        self._unit_hits: set[int] = set()
        self._derived: set[str] = set()  # sub-segments of a line, not lines
        self._en: list[str] = []  # unit → English field, plain of tags
        self._tr: list[str] = []  # unit → its translation (English if none)
        self._unit_key: list[str] = []  # unit → norm of the English
        self._lead: list[str] = []  # unit → what its tag sits after
        self._runs: dict[tuple[int, ...], dict[str, tuple[str, str]]] = {}
        # norm(translation) → English, plain; made when first asked for.
        self._reverse: dict[str, str] = {}
        self._reverse_n = -1

    @staticmethod
    def norm(s: str) -> str:
        # A leading bullet glyph is dropped: the bullet-list renderer asks
        # for the item without it.
        return _norm_tail(_cf(s))

    def add(
        self,
        en: str,
        tr: str,
        *,
        derived: bool = False,
        units: tuple[int, ...] = (),
    ) -> None:
        en, tr = en.strip(), tr.strip()
        if not en or not tr or en == tr:
            return
        key = self.norm(en)
        if key and key not in self._index:
            self._index[key] = (tr, en)
            if derived:
                self._derived.add(key)
            if units:
                self._units[key] = units

    @classmethod
    def from_lines(cls, en: list[str], tr: list[str]) -> "TextMemory":
        tm = cls()
        _tagged, units = tag_lines(en)
        for k, (li, f) in enumerate(units):
            a, b = en[li], tr[li]
            pa = _payload(a)
            pb = _payload(b) if _tag_of(a) == _tag_of(b) else []
            ea = pa[f]
            eb = pb[f] if len(pa) == len(pb) else ea
            tm._en.append(ea)
            tm._tr.append(eb)
            tm._unit_key.append(tm.norm(ea))
            tm._lead.append(_TAG_AT_RE.match(ea).group())
            if ea == eb:
                continue
            tm.add(ea, eb, units=(k,))
            variants, whole_derived = derive_variants(ea, eb, _tag_of(a))
            for va, vb, shows in variants:
                tm.add(va, vb, derived=True, units=(k,) if shows else ())
            if whole_derived:
                tm._derived.add(tm.norm(ea))
        # A playbook's stat block is one JSON payload; its text is the gloss
        # and the HP label.
        for a, b in zip(en, tr):
            if _tag_of(a) == "STATS" == _tag_of(b):
                pa, pb = _payload(a), _payload(b)
                if pa and pb:
                    ba, bb = _stats_block(pa[0]), _stats_block(pb[0])
                    for key in STATS_TEXT_KEYS:
                        tm.add(str(ba.get(key) or ""), str(bb.get(key) or ""))
        return tm

    def __len__(self) -> int:
        return len(self._index)

    # ------------------------------------------------------------ lookups

    def get(self, s: str) -> str:
        if not s:
            return s
        if has_tags(s):
            out = self._resolve_tagged(s)
            if out is not None:
                return out
            s = strip_tags(s)
        if not self._index:
            return s
        return self._lookup(s)

    def _lookup(self, s: str) -> str:
        key = self.norm(s)
        hit = self._index.get(key)
        if hit is None:
            return s
        self.hits.add(key)
        self._unit_hits.update(self._units.get(key, ()))
        return self._adjust(s, *hit)

    def _adjust(self, s: str, raw: str, en_raw: str) -> str:
        """``raw`` in the shape of the query ``s`` it answers."""
        out = raw if any(c in s for c in _FMT) else _defmt(raw)
        if not _BULLET_RE.match(_defmt(s).strip()):
            out = _BULLET_RE.sub("", out, count=1)
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

    def _segment(self, k: int, seg: str) -> str | None:
        """``seg``, the text the renderer shows of field ``k``: the field
        whole, or one of its variants. ``None`` when it is neither."""
        if not seg.strip():
            return seg
        key = self.norm(seg)
        if k < len(self._en) and key == self._unit_key[k]:
            self._unit_hits.add(k)
            en, tr = self._en[k], self._tr[k]
            if en != tr:
                self.hits.add(key)
                return self._adjust(seg, tr, en)
            # The field is not translated (a move's name printed alone,
            # translated where it is used); the same words elsewhere may be,
            # and otherwise it is shown as it is.
            hit = self._index.get(key)
            return seg if hit is None else self._adjust(seg, *hit)
        hit = self._index.get(key)
        if hit is None:
            return None
        self.hits.add(key)
        self._unit_hits.update(self._units.get(key, ()))
        return self._adjust(seg, *hit)

    def _resolve_tagged(self, s: str) -> str | None:
        segs = _split_tagged(s, self._lead)
        units = [k for k, _ in segs if k is not None]
        if not units:
            return None
        head = segs[0][1]
        if not any(t.strip() for k, t in segs if k is not None):
            # A provenance suffix: the text was made from these fields, in
            # this order, and rendered plain (a stat block's).
            return self._run(head, units)
        parts: list[str] | None = []
        for k, t in segs:
            if k is None:
                if not t.strip():
                    parts.append(t)
                    continue
                # Opened inside a field: a piece cut out of it, by content.
                key = self.norm(t)
                hit = self._index.get(key)
                if hit is None:
                    parts = None
                    break
                self.hits.add(key)
                self._unit_hits.update(self._units.get(key, ()))
                parts.append(self._adjust(t, *hit))
            else:
                r = self._segment(k, t)
                if r is None:
                    parts = None
                    break
                parts.append(r)
        if parts is not None:
            return "".join(parts)
        return self._run(strip_tags(s), units, head=head)

    def _run(self, plain: str, units: list[int], head: str = "") -> str | None:
        """``plain``, gathered by the renderer from the fields ``units``, as
        the same gathering of their translations."""
        if not plain.strip():
            return None
        if head.strip():
            # The query opens inside the field before the first tagged one
            # (a stat block cut its joined lines at a label): find it.
            hk = self.norm(head)
            for back in range(1, 5):
                c = units[0] - back
                if c < 0:
                    break
                if hk and self._unit_key[c].endswith(hk):
                    units = [c] + units
                    break
        key = tuple(units)
        variants = self._runs.get(key)
        if variants is None:
            variants = self._runs[key] = self._run_variants(units)
        hit = variants.get(self.norm(plain))
        if hit is None:
            return None
        self._unit_hits.update(units)
        # Kept, so an id made off the translated text can be read back — as
        # a derived entry of these fields, not a line of its own.
        rk = self.norm(hit[1])
        self._index.setdefault(rk, hit)
        self._derived.add(rk)
        self._units.setdefault(rk, tuple(units))
        self.hits.add(rk)
        return self._adjust(plain, *hit)

    def _run_variants(self, units: list[int]) -> dict[str, tuple[str, str]]:
        en_parts = [self._en[k] for k in units if k < len(self._en)]
        tr_parts = [self._tr[k] for k in units if k < len(self._en)]
        out: dict[str, tuple[str, str]] = {}

        def put(a: str, b: str) -> None:
            if a.strip() and b.strip() and a.strip() != b.strip():
                out.setdefault(self.norm(a), (b.strip(), a.strip()))

        for join in (" ".join, _join_dehyphenated):
            je, jt = join(en_parts), join(tr_parts)
            put(je, jt)
            for va, vb, _shows in derive_variants(je, jt)[0]:
                put(va, vb)
        return out

    def reverse(self, s: str) -> str | None:
        """The English a translated string came from, plain — for the ids a
        renderer makes off text it reads back out of the HTML."""
        if not s or not self._index:
            return None
        if self._reverse_n != len(self._index):
            # First translation to land for a key wins, as the index does.
            rev: dict[str, str] = {}
            for raw, en_raw in self._index.values():
                rev.setdefault(self.norm(raw), _defmt(en_raw).strip())
            self._reverse, self._reverse_n = rev, len(self._index)
        return self._reverse.get(self.norm(html_unescape(s)))

    def unused(self) -> list[str]:
        """English lines whose translation the renderer never asked for."""
        return [
            en
            for k, (_r, en) in self._index.items()
            if k not in self.hits
            and k not in self._derived
            and not (self._unit_hits & set(self._units.get(k, ())))
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
    ui: dict | None = None,
    titles: dict[str, str] | None = None,
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
    from .arcana import major_arcana_html, minor_arcana_html
    from .structure import article_html, linkify_pages, set_translation
    from .reflinks import apply_reference_links

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
    render = article_html
    # The page renders from the English lines, each field tagged with its
    # unit, so what the renderer shows names the fields it was made from.
    tagged, _units = tag_lines(en_lines)
    if art.get("kind") == "arcana":
        render = minor_arcana_html if art.get("arcana_type") == "minor" else major_arcana_html
        if tm is not None and meta.get("title"):
            # The card sets its name on its face, from the English title.
            tm.add(art["title"], meta["title"], derived=True)
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

    set_translation(tm, ui, titles)
    try:
        body, excerpt, secs = render(
            tagged, art["title"], lookup, articles, **common
        )
        if ov is not None:
            body = apply_override(ov, body, slug=slug, link_fn=link_fn, lines=sheet_lines)
            secs = override_sections(body)
        if render is article_html:
            # The works the page cites, linked where the translation keeps
            # the title as printed (links/<slug>.json).
            body, unlinked = apply_reference_links(body, slug)
            if unlinked:
                notes.append(f"{where}: reference links not placed: " + "; ".join(unlinked))
    finally:
        set_translation(None)
    if has_tags(body):
        # Text that reached the HTML without going through T(): English,
        # and tagged. Reported, and the tags taken out.
        notes.append(f"{where}: {len(TAG_RE.findall(body))} provenance tags leaked into the HTML")
        body = strip_tags(body)

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
    "derive_variants",
    "tag_lines",
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

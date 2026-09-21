"""``blocks.json`` — what blocks each page is supposed to have.

The books set a stat block, a roll table or a hazard in type, not in tags,
so the renderer works them out from how a line looks: this line is short and
the next one carries HP, so the first is a creature's name. That reads a
layout the books use most of the time. Where they do something else it fails
quietly — Book II p. 142 wraps Yaarowslow the Many's name in plain capitals
and the block above simply ate it, name, tags, stats and all.

This file is the answer to "what is actually here". It is *data*, checked in
beside the corpus: for every page, the blocks it holds, in order. The build

* **forces** a block to start (and, where said, to end) at the lines named,
  so a layout the heuristics misread is settled by hand, once;
* **reports drift** — a block on the page that is not in the file, or a
  listed block the build failed to produce — so a fix to the extractor, or a
  new printing, cannot quietly take one away.

Seeded from a build (``--seed-blocks``) and then corrected by hand. Only
``kind`` and ``name`` are required; ``start``/``end`` are the override, and
are only worth writing where the renderer gets a block wrong.

    {
      "version": 1,
      "books": {
        "book2": {
          "fomoraij": [
            {"kind": "stat-block", "name": "\\"Typical\\" Fomoraij"},
            {"kind": "stat-block", "name": "Yaarowslow, the Many",
             "start": "YAAROWSLOW, THE MANY",
             "end": "> Regenerate wounds from anything but orichalcum"}
          ]
        }
      }
    }
"""

from __future__ import annotations

import html as _html
import json
import re
from pathlib import Path

from . import REPO_ROOT
from .text import strip_markers

BLOCKS_FILE = "blocks.json"

# The kinds a page can hold: every self-contained block the renderer makes,
# and the tables. Anything else in a page is prose, headings and lists.
KINDS = (
    "stat-block",
    "roll-table",
    "value-table",
    "hazard-block",
    "move-block",
    "discovery-block",
    "steading-improvement",
    "infobox",
)


def _key(text: str) -> str:
    """A line, or a name, as it is matched: letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "", strip_markers(text or "").lower())


def blocks_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else REPO_ROOT / BLOCKS_FILE


def load(path: str | Path | None = None) -> dict:
    """``{book: {slug: [block, …]}}``; empty when there is no file yet."""
    p = blocks_path(path)
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("books") or {}


def save(books: dict, path: str | Path | None = None) -> Path:
    p = blocks_path(path)
    p.write_text(
        json.dumps({"version": 1, "books": books}, ensure_ascii=False, indent=1)
        + "\n",
        encoding="utf-8",
    )
    return p


def for_slug(books: dict, book: str, slug: str) -> list[dict]:
    return list((books.get(book) or {}).get(slug) or [])


_FMT_PAIRS = (("", ""), ("", ""))


def _balanced(text: str) -> bool:
    return all(text.count(a) == text.count(b) for a, b in _FMT_PAIRS)


def cut_lines(blocks: list[dict], lines: list[str]) -> list[str]:
    """Split a line an ``end`` stops part-way through.

    The extractor hands a table's last row and the note printed under it as
    one line, because the book sets them as one ("12 Worm-like/slug-like/
    grub-like Combine 2-3 of the above …"). An ``end`` naming only the row
    cuts the line there: the row ends the table, and the rest goes on as the
    paragraph it is. A cut that would leave a bold or italic run hanging open
    is not made.
    """
    out = list(lines)
    at = 0
    for block in blocks:
        end = block.get("end")
        if not end:
            continue
        want = _key(end)
        if not want:
            continue
        for i in range(at, len(out)):
            key = _key(out[i])
            if key == want:
                at = i + 1
                break
            if not key.startswith(want):
                continue
            # Walk the raw line until it has given up the whole of `want`.
            seen, pos = 0, 0
            for pos, ch in enumerate(out[i]):
                if seen == len(want):
                    break
                if ch.isalnum():
                    seen += 1
            head, tail = out[i][:pos].rstrip(), out[i][pos:].strip()
            if not tail or not _balanced(head) or not _balanced(tail):
                at = i + 1
                break
            out[i : i + 1] = [head, tail]
            at = i + 2
            break
    return out


def line_marks(blocks: list[dict], lines: list[str]) -> tuple[dict, dict]:
    """Where the listed ``start``/``end`` lines are.

    Returns ``({line index: block}, {line index: block})`` — the lines a
    block is told to start on, and the lines it is told to end after. Both
    are matched in the order the blocks are listed, each after the last
    match, so the same words twice on a page still land on the right one,
    and either may be given without the other: a block the renderer finds by
    itself but runs on past its last line needs only an ``end``.
    """
    starts: dict[int, dict] = {}
    ends: dict[int, dict] = {}
    keys = [_key(l) for l in lines]
    at = 0

    def find(text: str) -> int | None:
        nonlocal at
        try:
            i = keys.index(_key(text), at)
        except ValueError:
            return None
        at = i + 1
        return i

    for block in blocks:
        if block.get("start"):
            i = find(block["start"])
            if i is not None:
                starts[i] = block
        if block.get("end"):
            j = find(block["end"])
            if j is not None:
                ends[j] = block
    return starts, ends


def missing_starts(blocks: list[dict], lines: list[str]) -> list[str]:
    """Listed ``start`` lines that are not in the page's corpus at all."""
    keys = {_key(l) for l in lines}
    return [
        b.get("start", "")
        for b in blocks
        if b.get("start") and _key(b["start"]) not in keys
    ]


def found_in_html(body_html: str) -> list[dict]:
    """The blocks a rendered page actually holds, in document order.

    Each kind names itself differently — a creature in an ``h3.stat-name``, a
    roll table in the label beside its dice, a value table in its header row
    — so the name is read per kind, from the markup that follows the opening
    div.
    """
    name_res = {
        "stat-block": r'<h3[^>]*class="stat-name"[^>]*>(.*?)</h3>',
        "hazard-block": r'<[^>]*class="stat-name"[^>]*>(.*?)</',
        "discovery-block": r'<h3[^>]*class="discovery-name"[^>]*>(.*?)</h3>',
        "move-block": r'<h3[^>]*class="move-name"[^>]*>(.*?)</h3>',
        "steading-improvement": r'<h3[^>]*class="si-title"[^>]*>(.*?)</h3>',
        "roll-table": r'<span class="roll-label">(.*?)</span>',
        "value-table": r'<tr class="value-table-head"><th>(.*?)</th>',
        "infobox": r"()",
    }
    out: list[dict] = []
    for m in re.finditer(
        # The class is the kind, alone or with more classes after a space
        # ("roll-table roll-table-inline") — never a longer word that
        # merely opens with it ("roll-table-head" is a table's header row,
        # not a table).
        r'<div class="(' + "|".join(KINDS) + r')(?:\s[^"]*)?"'
        r'(?:\s+id="([^"]*)")?',
        body_html,
    ):
        kind, block_id = m.group(1), m.group(2) or ""
        tail = body_html[m.end(): m.end() + 600]
        hit = re.search(name_res[kind], tail, re.S)
        name = (
            _html.unescape(re.sub(r"<[^>]+>", "", hit.group(1))).strip()
            if hit
            else ""
        )
        out.append({"kind": kind, "name": name, "id": block_id})
    return out


def drift(listed: list[dict], found: list[dict]) -> tuple[list[str], list[str]]:
    """``(listed but not built, built but not listed)``, by kind and name."""

    def bag(items: list[dict]) -> dict[tuple[str, str], list[str]]:
        out: dict[tuple[str, str], list[str]] = {}
        for it in items:
            key = (it.get("kind", ""), _key(it.get("name", "")))
            out.setdefault(key, []).append(it.get("name", ""))
        return out

    want, have = bag(listed), bag(found)
    gone, new = [], []
    for key, names in want.items():
        over = len(names) - len(have.get(key, []))
        gone += [f'{key[0]} "{n}"' for n in names[:max(0, over)]]
    for key, names in have.items():
        over = len(names) - len(want.get(key, []))
        new += [f'{key[0]} "{n}"' for n in names[:max(0, over)]]
    return gone, new


def merge(old: dict, new: dict) -> dict:
    """What this build found, keeping what was written by hand.

    Re-seeding must not throw away the entries that are the whole point of
    the file: a block only exists *because* its ``start`` is listed, so it is
    absent from a seeding run (which builds without the file) and would be
    lost every time. An entry carrying ``start`` or ``end`` is therefore
    carried over — onto the matching block where this build found one, and
    whole, at its old place, where it did not.
    """
    out: dict[str, dict[str, list[dict]]] = {}
    for book, pages in new.items():
        out[book] = {}
        for slug, found in pages.items():
            listed = ((old.get(book) or {}).get(slug)) or []
            by_hand = [b for b in listed if b.get("start") or b.get("end")]
            if not by_hand:
                out[book][slug] = found
                continue
            merged = [dict(b) for b in found]
            for i, hand in enumerate(by_hand):
                key = (hand.get("kind"), _key(hand.get("name", "")))
                match = next(
                    (
                        b
                        for b in merged
                        if (b.get("kind"), _key(b.get("name", ""))) == key
                    ),
                    None,
                )
                if match is not None:
                    for field in ("start", "end"):
                        if hand.get(field):
                            match[field] = hand[field]
                    continue
                # The build without the file never made this block: put it
                # back where the file had it.
                at = listed.index(hand)
                merged.insert(min(at, len(merged)), dict(hand))
            out[book][slug] = merged
    return out

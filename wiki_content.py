"""
Column-aware PDF extraction and HTML structuring for the Book II wiki.

Re-reads the 1-up PDF left-column-then-right so roll tables and enemy
stat blocks are not interwoven the way they are in the cleaned markdown.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict

import fitz

# Mid-page gutter for typical Stonetop 1-up pages (~396pt wide, 2 columns)
DEFAULT_GUTTER = 198

# Monster/creature tag words commonly used in Stonetop stat blocks
ORG_TAGS = {"horde", "group", "solitary"}
TAG_WORDS = ORG_TAGS | {
    "small",
    "large",
    "huge",
    "tiny",
    "hoarder",
    "cautious",
    "stealthy",
    "terrifying",
    "planar",
    "construct",
    "devious",
    "intelligent",
    "magical",
    "organized",
    "amorphous",
    "clever",
    "mindless",
    "divine",
    "legendary",
    "brutal",
    "fearless",
    "drunkard",
    "hardy",
    "craven",
    "meek",
    "opportunistic",
    "terrifying",
    "deceptive",
    "hoarder",
    "cautious",
    "stealthy",
    "planar",
    "construct",
    "arcane",
    "divine",
    "undead",
    "spirit",
    "elemental",
    "ancient",
    "wretched",
    "violent",
    "corrupted",
    "emanation",
}

# d6, 2d10, d10+3, 1d4-1 (optional spaces around +/-; avoid eating 1d4-1d6 ranges)
DICE_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(\d{0,2}d(?:4|6|8|10|12|20|100)"
    r"(?:\s*[+\-–—]\s*\d{1,3}(?![dD\d]))?)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
# (page 12), (pages 8-11), (page 282, 350), (see page 39)
PAGE_REF_RE = re.compile(
    r"\((?:see\s+)?pages?\s+([\d,\s\-–—]+)\)",
    re.IGNORECASE,
)
BARE_PAGE_RE = re.compile(
    r"(?<![\w/])(?:see\s+)?pages?\s+([\d,\s\-–—]+)(?![\w/])",
    re.IGNORECASE,
)
ROLL_HEADER_RE = re.compile(
    r"^(\d{0,2}d(?:4|6|8|10|12|20))\s+(.+)$", re.IGNORECASE
)
ROLL_HEADER_DICE_ONLY = re.compile(r"^(\d{0,2}d(?:4|6|8|10|12|20))$", re.IGNORECASE)
ENTRY_RE = re.compile(
    r"^(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?\s+(.+)$"
)
HP_LINE_RE = re.compile(r"\bHP\s*\d+", re.IGNORECASE)
# Trade/value tables: "goods value", "weapons armor value", "services value", etc.
VALUE_HEADER_RE = re.compile(
    r"^(?:"
    r"weapons?\s*(?:&\s*|and\s+)?armor|"
    r"goods|"
    r"coin|"
    r"food\s*(?:&\s*|and\s+)?lodging|"
    r"services"
    r")\s*value$",
    re.IGNORECASE,
)
# Item ending with a price: "Wheelbarrow 1", "... free", "... +2"
# Optional trailing parenthetical: "… iron 2 (immobile)"
VALUE_ROW_RE = re.compile(
    r"^(.+?)\s+(\d{1,2}|\+\d{1,2}|free)(?:\s+(\([^)]*\)))?\s*$",
    re.IGNORECASE,
)
LONE_VALUE_RE = re.compile(r"^(\d{1,2}|\+\d{1,2}|free)$", re.IGNORECASE)


def parse_value_row(line: str) -> tuple[str, str] | None:
    """Parse a trade/services line into (item, value) or None."""
    L = line.strip()
    if not L or looks_like_value_header(L):
        return None
    if L.lower().startswith(("on a ", "when ", "pick ", "roll ", "if the ")):
        return None

    # "… 0 for a week" / "… 1 or dangerous…" / "… 2 your own" / "… 3 trade opportunities"
    m = re.match(
        r"^(.+?)\s+(\d{1,2}|\+\d{1,2}|free)\s+"
        r"(for|or|your|trade)\b(.*)$",
        L,
        re.I,
    )
    if m:
        tail = (m.group(3) + m.group(4)).strip()
        item = f"{m.group(1).strip()} {tail}".strip()
        item = re.sub(r"\s*trade opportunities\s*$", "", item, flags=re.I).strip()
        return item, m.group(2)

    m = VALUE_ROW_RE.match(L)
    if m:
        item = m.group(1).strip()
        val = m.group(2)
        paren = m.group(3) or ""
        # Reject if the only digit was inside an earlier paren fragment
        # e.g. bad split of "(steel, 1 piercing) 1" is OK; item won't end with ','
        if item.endswith((",", "(", "/")):
            return None
        if paren:
            item = f"{item} {paren}".strip()
        return item, val

    return None


def normalize_text(s: str) -> str:
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    s = s.replace("\u00ad", "")  # soft hyphen
    s = s.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "")
    s = s.replace("ä", "•")  # PDF dingbat often extracted as ä
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def undouble_words(text: str) -> str:
    """'The The Ruined Ruined Tower Tower' → 'The Ruined Tower'."""
    words = text.split()
    out: list[str] = []
    i = 0
    while i < len(words):
        if i + 1 < len(words) and words[i].lower() == words[i + 1].lower():
            out.append(words[i])
            i += 2
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def is_fully_pairwise_doubled(line: str) -> bool:
    words = line.split()
    if len(words) < 2 or len(words) % 2 != 0:
        return False
    return all(
        words[i].lower() == words[i + 1].lower() for i in range(0, len(words), 2)
    )


def extract_page_lines(
    page: fitz.Page,
    gutter: float | None = None,
    article_title: str = "",
) -> list[str]:
    """Return reading-order lines: full left column, then full right column."""
    if gutter is None:
        gutter = page.rect.width * 0.5
        # Prefer classic Stonetop gutter when page is ~396 wide
        if 350 < page.rect.width < 450:
            gutter = DEFAULT_GUTTER

    words = page.get_text("words")
    if not words:
        return []

    cols: list[list] = [[], []]
    for w in words:
        cols[0 if w[0] < gutter else 1].append(w)

    lines_out: list[str] = []
    page_h = page.rect.height

    for col_words in cols:
        by_y: dict[float, list] = defaultdict(list)
        for w in col_words:
            y = round(w[1] * 2) / 2
            by_y[y].append(w)
        col_lines: list[tuple[float, str]] = []
        for y in sorted(by_y):
            ws = sorted(by_y[y], key=lambda t: t[0])
            text = normalize_text(" ".join(t[4] for t in ws))
            if not text:
                continue
            # Bottom-of-page numbers
            if text.isdigit() and len(text) <= 3 and y > page_h - 40:
                continue
            col_lines.append((y, text))
        # First ~3 lines of each column can be running headers
        for idx, (y, text) in enumerate(col_lines):
            near_top = idx < 3 and y < 100
            if is_running_header(text, article_title, near_page_top=near_top):
                continue
            # Collapse accidental pairwise doubles that slipped through
            if is_fully_pairwise_doubled(text):
                text = undouble_words(text)
                if is_running_header(text, article_title, near_page_top=True):
                    continue
            lines_out.append(text)
    return lines_out


def is_running_header(line: str, article_title: str, *, near_page_top: bool = False) -> bool:
    t = line.strip()
    if not t:
        return True
    # Pairwise-doubled running heads: "The The Ruined Ruined Tower Tower"
    if is_fully_pairwise_doubled(t):
        return True
    words = t.split()
    if len(words) >= 2 and len(words) % 2 == 0:
        half = len(words) // 2
        if [w.lower() for w in words[:half]] == [w.lower() for w in words[half:]]:
            return True
    if len(words) == 2 and words[0].lower() == words[1].lower():
        return True

    cleaned = undouble_words(t)
    at = re.sub(r"[^a-z0-9]+", "", article_title.lower())
    lt = re.sub(r"[^a-z0-9]+", "", t.lower())
    lc = re.sub(r"[^a-z0-9]+", "", cleaned.lower())
    # Exact / undoubled title as running head (any occurrence — it's never useful body)
    if at and (lt == at or lc == at or lt == at + at or lc == at + at):
        return True
    if near_page_top and at and (lc.startswith(at) or at.startswith(lc)) and len(lc) >= 6:
        return True
    if t.lower() in {"contents", "index"}:
        return True
    return False


def looks_like_heading(line: str) -> bool:
    if not line or len(line) > 55:
        return False
    # Running-header garbage should never become an <h2>
    if is_fully_pairwise_doubled(line):
        return False
    if line.endswith((".", ",", ";", ":")) and not line.endswith(":"):
        return False
    if ENTRY_RE.match(line):
        return False
    if HP_LINE_RE.search(line):
        return False
    if line.startswith(("•", "-", "–")):
        return False
    if ROLL_HEADER_RE.match(line) or ROLL_HEADER_DICE_ONLY.match(line):
        return False
    if looks_like_value_header(line) or VALUE_ROW_RE.match(line):
        return False
    if LONE_VALUE_RE.match(line):
        return False
    # Never treat cross-refs as headings — they must become links
    if re.search(r"\(pages?\s+[\d,\s\-–—]+\)", line, re.I):
        return False
    if re.search(r"\bpages?\s+\d", line, re.I):
        return False
    # Mostly Title Case / short label
    words = line.split()
    if len(words) > 8:
        return False
    # Common section words or short phrases without lowercase filler-only
    if line[0].isupper() or line[0].isdigit():
        # Small words (and/of/the…) don't make a title look like a sentence
        small = {
            "a", "an", "the", "and", "or", "of", "to", "in", "for", "from",
            "vs", "vs.", "on", "with", "by", "as", "at",
        }
        lowerish = sum(
            1
            for w in words
            if w and w[0].islower() and w.lower().strip(".,;:") not in small
        )
        if lowerish >= 3:
            return False
        return True
    return False


def parse_page_nums(spec: str) -> list[int]:
    """Parse '12', '8-11', '282, 350', '282, 350-352' into page numbers (range ends only for ranges)."""
    nums: list[int] = []
    for part in re.split(r"[,;]", spec):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            # link to start of range only (avoid huge lists)
            nums.append(a)
            if b != a:
                nums.append(b)
        elif part.isdigit():
            nums.append(int(part))
    # unique preserve order
    seen = set()
    out = []
    for n in nums:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def looks_like_tag_line(line: str) -> bool:
    """Horde, small, stealthy, … or Solitary, brutal, fearless, drunkard"""
    if not line or len(line) > 80:
        return False
    if HP_LINE_RE.search(line) or line.lower().startswith("damage"):
        return False
    parts = [p.strip() for p in re.split(r"[,;]", line) if p.strip()]
    if not parts:
        return False
    first = parts[0].lower().split()[0] if parts[0] else ""
    # Primary org type is enough (e.g. "Horde, hardy" or even "Solitary")
    if first in ORG_TAGS:
        return True
    hits = 0
    for p in parts:
        tok = p.lower().split()
        if not tok:
            continue
        if tok[0] in TAG_WORDS or any(t in TAG_WORDS for t in tok):
            hits += 1
    return hits >= 2


def looks_like_value_header(line: str) -> bool:
    t = re.sub(r"\s+", " ", line.strip().lower().replace("&", " "))
    if not t.endswith(" value"):
        return False
    head = t[: -len(" value")].strip()
    # Accept known price-table headers
    if head in {
        "goods",
        "coin",
        "services",
        "weapons",
        "weapon",
        "armor",
        "weapons armor",
        "weapon armor",
        "weapons and armor",
        "food",
        "food lodging",
        "food and lodging",
        "lodging",
    }:
        return True
    if VALUE_HEADER_RE.match(line.strip()):
        return True
    return False


def should_join(a: str, b: str) -> bool:
    # Never glue pure arcana tag lines to/from neighbors
    # ("A giant's dormitory" + "magical" + "In a ruin…")
    if _is_pure_arcana_tag_line(a) or _is_pure_arcana_tag_line(b):
        return False
    if not a or not b:
        return False
    # Split mid page-ref BEFORE heading heuristics — otherwise
    # "… (page" + "436), especially…" is rejected as a short "heading".
    if re.search(r"\(pages?\s*$", a, re.I) and re.match(r"^\d", b):
        return True
    if re.search(r"\(pages?\s+\d*$", a, re.I) and re.match(
        r"^[\d,\s\-–—)]", b
    ):
        return True
    # Separate list/paragraph entries that each carry their own page ref:
    # "…near the Dread River" + "Spirits of the wild (page 356)…"
    if (
        re.search(r"\(pages?\s+[\d,\s\-–—]+\)", a, re.I)
        and re.search(r"\(pages?\s+[\d,\s\-–—]+\)", b, re.I)
        and b[0:1].isupper()
        and not a.endswith((",", ";", ":", "—", "-"))
    ):
        return False
    if is_running_header(b, ""):
        return False
    if looks_like_heading(b) and len(b) < 40:
        return False
    if ROLL_HEADER_RE.match(b) or ROLL_HEADER_DICE_ONLY.match(b):
        return False
    if ENTRY_RE.match(b):
        return False
    if looks_like_tag_line(b) or HP_LINE_RE.search(b):
        return False
    if b.startswith(("•", "·")):
        return False
    if b.startswith(("When you", "When ", "If you", "On a ", "Choose", "Pick ")):
        # new mechanical paragraph — only join if a clearly mid-word
        if not a.endswith("-"):
            return False
    # Never glue a price-table header onto the previous item line
    if looks_like_value_header(b):
        return False
    if re.search(
        r"\b(goods|coin|services|weapons?|food(\s+and)?\s+lodging)\s+value\s*$",
        b,
        re.I,
    ):
        return False
    # Hyphenated line wrap
    if a.endswith("-") and not a.endswith("--"):
        return True
    # Ends with (page N) — still often mid-sentence
    if re.search(r"\(pages?\s+\d+\)$", a, re.I):
        return True
    # Soft wrap: previous doesn't end sentence, next continues lowercase
    if a[-1] in ".!?:;…\"":
        return False
    if a.endswith(")") and not re.search(r"\(pages?\s+\d+\)$", a, re.I):
        # closing paren mid-thought is often still a wrap
        if b[0].islower():
            return True
        return False
    if b[0].islower() or b[0] in "\"'(":
        return True
    # Damage/special lines often wrap mid-phrase with capital
    if a.endswith((",", ";")):
        return True
    # Mid-sentence wraps onto a capital word: "ways of the" + "Fen and…"
    # or "a dozen or so" + "Guards are stationed…".
    # Use last-word continuations that almost never end a sentence.
    # Intentionally exclude "with" so monster moves after "… over with" stay separate.
    cont_last = {
        "the",
        "a",
        "an",
        "of",
        "and",
        "or",
        "to",
        "for",
        "by",
        "as",
        "so",  # "or so"
        "its",
        "their",
        "his",
        "her",
        "our",
        "your",
        "my",
        "this",
        "that",
        "these",
        "those",
        "some",
        "any",
        "no",
        "not",
        "but",
        "than",
        "from",
        "into",
        "onto",
        "upon",
        "between",
        "among",
        "through",
        "over",
        "under",
        "near",
        "like",
        "pair",  # "a pair"
        "dozen",  # "a dozen"
        "few",
        "many",
        "most",
        "more",
        "less",
        "such",
        "each",
        "every",
        "other",
        "another",
        "both",
        "all",
        "what",  # "has what the"
        "which",
        "who",
        "whose",
        "where",
        "when",
        "how",
        "if",
        "unless",
        "until",
        "while",
        "though",
        "although",
        "because",
        "since",
        "whether",
        "nor",
        "yet",
        "across",
        "along",
        "around",
        "behind",
        "beside",
        "beyond",
        "during",
        "inside",
        "outside",
        "toward",
        "towards",
        "via",
        "vs",
        "per",
        "plus",
        "vs.",
    }
    m_last = re.search(r"([A-Za-z']+)\W*$", a)
    last = m_last.group(1).lower() if m_last else ""
    if last in cont_last and b[0:1].isupper():
        # Don't glue onto a pure short section title (Title Case, no mid-lowercase)
        b_words = b.split()
        lowerish = sum(1 for w in b_words if w and w[0].islower())
        if looks_like_heading(b) and lowerish == 0 and len(b_words) <= 4:
            return False
        return True
    # Only join on articles/prepositions when the next line continues mid-phrase
    # (lowercase start already handled). Avoid gluing monster moves onto
    # "Instinct to … with" / "… to".
    if b[0:1].islower() and last in cont_last | {"with", "like"}:
        return True
    # Soft wrap mid multi-word proper name onto capital + continuing prose:
    # "meadows of the Huffel" + "Peaks (page 236). The petals are edible..."
    # "runes of the Green" + "Lords (page 210). Fae magic..."
    # (last word is not a cont_last preposition, so the rule above misses it)
    if (
        b[0:1].isupper()
        and not looks_like_heading(a)
        and not looks_like_heading(b)
    ):
        b_words = b.split()
        lowerish = sum(1 for w in b_words if w and w[0:1].islower())
        # Name continuation straight into a page ref:
        # "Lords (page 210)" / "Peaks (page 236). The petals..."
        if re.match(
            r"^[A-Z][A-Za-z'’\-]*(?:\s+[A-Z][A-Za-z'’\-]*){0,3}\s+"
            r"\((?:see\s+)?pages?\s+\d",
            b,
        ):
            return True
        if lowerish >= 2 and len(b) >= 30:
            return True
        # Short wrap shard with a page ref and a little more prose
        # ("Lords (page 210). Fae magic (page 94)" — only one lowercase word)
        if lowerish >= 1 and re.search(r"\((?:see\s+)?pages?\s+\d", b, re.I):
            return True
    # "An (see below)" + "unnerving tableau"
    if a.lower().endswith("(see below)") or a.lower().endswith("see below)"):
        return True
    return False


def merge_wrapped_lines(lines: list[str]) -> list[str]:
    if not lines:
        return []
    out: list[str] = []
    buf = lines[0]
    for nxt in lines[1:]:
        if should_join(buf, nxt):
            if buf.endswith("-") and not buf.endswith("--"):
                # keep hyphen if next is capital (compound), else glue word
                if nxt and nxt[0].islower():
                    buf = buf[:-1] + nxt
                else:
                    buf = buf + nxt
            else:
                buf = buf + " " + nxt
        else:
            out.append(buf)
            buf = nxt
    out.append(buf)
    return out


def dice_button(expr: str) -> str:
    # Normalize en/em dashes to ASCII for the roller; keep display as written
    e = expr.lower().replace("–", "-").replace("—", "-")
    e = re.sub(r"\s*([+\-])\s*", r"\1", e)  # d10 + 3 → d10+3
    return (
        f'<button type="button" class="dice-roll" data-dice="{html.escape(e)}" '
        f'title="Click to roll {html.escape(expr)}">{html.escape(expr)}</button>'
    )


def slugify_id(text: str) -> str:
    s = text.lower().replace("'", "").replace("'", "").replace("`", "")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "section"


def normalize_section_key(name: str) -> str:
    """Normalize a section/monster name for matching link text."""
    s = name.lower().strip()
    s = re.sub(r"[\"'“”‘’]", "", s)
    s = re.sub(r"^(a|an|the)\s+", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


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
        stem = _singularize_word(last)
        if stem and stem != last:
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
            if stem:
                add(stem)
    return keys


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

    for k in keys:
        if not k:
            continue
        if page is not None and (page, k) in by_page:
            return by_page[(page, k)][1]
        if target_slug and (target_slug, k) in by_slug:
            return by_slug[(target_slug, k)]
        # Compact form without spaces
        compact = k.replace(" ", "")
        if page is not None and (page, compact) in by_page:
            return by_page[(page, compact)][1]
        if target_slug and (target_slug, compact) in by_slug:
            return by_slug[(target_slug, compact)]
    return None


class AnchorRegistry:
    """Assign unique fragment ids for headings and stat blocks on a page."""

    def __init__(self) -> None:
        self.used: set[str] = set()
        self.sections: list[dict] = []  # {id, name, norm}

    def add(self, name: str) -> str:
        base = slugify_id(name)
        sid = base
        n = 2
        while sid in self.used:
            sid = f"{base}-{n}"
            n += 1
        self.used.add(sid)
        self.sections.append(
            {"id": sid, "name": name, "norm": normalize_section_key(name)}
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
        # Same article: in-page fragment if possible
        if art["slug"] == current_slug:
            if frag and label:
                return (
                    f'<a class="wiki-link" href="#{frag}" data-slug="{art["slug"]}" '
                    f'data-fragment="{frag}" title="{html.escape(art["title"])}">'
                    f"{html.escape(label)}</a>"
                )
            return html.escape(label) if label else ""
        text_out = label if label else art["title"]
        href = f"{art['slug']}.html"
        if frag:
            href = f"{href}#{frag}"
        title_attr = art["title"]
        if label and frag:
            title_attr = f"{label} — {art['title']}"
        return (
            f'<a class="wiki-link" href="{href}" data-slug="{art["slug"]}" '
            f'{f"data-fragment=\"{frag}\" " if frag else ""}'
            f'title="{html.escape(title_attr)}">{html.escape(text_out)}</a>'
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
    def repl_book_page(m: re.Match) -> str:
        raw_book = m.group("book")
        # Regex only captures single char; re-scan for II
        full = m.group(0)
        book_m = re.search(r"Book\s*(II|I|2|1)\b", full, re.I)
        book_tok = book_m.group(1) if book_m else raw_book
        book_id = _book_id_from_token(book_tok)
        if not book_id:
            return m.group(0)
        title = (m.group("title") or "").strip() or None
        pages = parse_page_nums(m.group("pages"))
        return store(links_for_pages(pages, title, book_id=book_id))

    work = re.sub(
        r"(?P<title>(?:[A-Za-z][A-Za-z0-9'’\-]*(?:\s+[A-Za-z][A-Za-z0-9'’\-]*){0,6})\s+)?"
        r"\(?"
        r"(?:see\s+)?"
        r"Book\s*(?P<book>II|I|2|1)\s*[,:]?\s*"
        r"(?:starting\s+on\s+)?"
        r"pages?\s+(?P<pages>[\d,\s\-–—]+)"
        r"\)?",
        repl_book_page,
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
        return store(links_for_pages(pages))

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
    return "".join(out)


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
                    f'data-slug="{art["slug"]}" title="{html.escape(art["title"])}">'
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
        f' <span class="roll-label">{html.escape(label)}</span>'
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
) -> str:
    lkw = link_kw or {}
    body = []
    for item, val in rows:
        body.append(
            f"<tr><td>{linkify_pages(item, lookup, current_slug, section_index, **lkw)}</td>"
            f'<td class="val">{html.escape(val)}</td></tr>'
        )
    return (
        f'<div class="value-table">'
        f'<div class="value-table-head">{html.escape(title)}</div>'
        f"<table><tbody>{''.join(body)}</tbody></table>"
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
) -> str:
    """Compact monster/enemy block — minimal vertical space."""
    lkw = link_kw or {}
    tags = ""
    stats: list[str] = []
    moves: list[str] = []
    other: list[str] = []
    seen_instinct = False

    for line in lines:
        low = line.lower().strip()
        # Don't absorb the next monster's identity
        if (
            tags
            and stats
            and looks_like_tag_line(line)
            and not low.startswith(("damage", "hp", "instinct"))
        ):
            # leftover identity of a following creature — stop via caller usually
            other.append(line)
            continue
        if looks_like_tag_line(line) and not tags:
            tags = line
        elif HP_LINE_RE.search(line) or low.startswith(
            ("damage", "special quality", "special qualities", "instinct", "armor")
        ):
            if low.startswith("instinct"):
                seen_instinct = True
            # Damage lines often continue with bare "d6 (...)" on next line
            if (
                stats
                and re.match(r"^d\d+", low)
                and stats[-1].lower().startswith("damage")
            ):
                stats[-1] = stats[-1] + " " + line
            else:
                stats.append(line)
        elif re.match(r"^d\d+", low) and stats:
            stats[-1] = stats[-1] + " " + line
        elif line.startswith("•") or line.startswith("·"):
            moves.append(line.lstrip("•· ").strip())
        elif low.startswith("when "):
            other.append(line)
        else:
            # Continue a Damage line that wrapped mid-parenthetical
            if stats and stats[-1].lower().startswith("damage") and (
                line[0:1].islower()
                or low.startswith(
                    ("maw ", "1 piercing", "piercing)", "advantage)", "messy,")
                )
                or stats[-1].endswith(",")
                or stats[-1].endswith("(")
                or re.match(r"^d\d+", low)
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

    id_attr = f' id="{html.escape(anchor_id)}"' if anchor_id else ""
    parts = [
        f'<div class="stat-block"{id_attr}>'
        f'<h3 class="stat-name">{html.escape(name)}</h3>'
    ]
    if tags:
        parts.append(
            f'<p class="stat-tags">{linkify_pages(tags, lookup, current_slug, section_index, **lkw)}</p>'
        )
    if stats:
        compact = " · ".join(s for s in stats)
        parts.append(
            f'<p class="stat-stats">{linkify_pages(compact, lookup, current_slug, section_index, **lkw)}</p>'
        )
    if moves:
        parts.append('<ul class="stat-moves">')
        for mv in moves:
            parts.append(
                f"<li>{linkify_pages(mv, lookup, current_slug, section_index, **lkw)}</li>"
            )
        parts.append("</ul>")
    for o in notes:
        parts.append(
            f'<p class="stat-note">{linkify_pages(o, lookup, current_slug, section_index, **lkw)}</p>'
        )
    parts.append("</div>")
    return "".join(parts)


def _is_chapter_toc_prose(line: str) -> bool:
    """True if this looks like the first body paragraph after a chapter TOC."""
    if not line:
        return False
    L = line.strip()
    if len(L) >= 70:
        return True
    if len(L) >= 48:
        words = L.split()
        lowerish = sum(1 for w in words if w and w[0].islower())
        if lowerish >= 2:
            return True
        if L.endswith((".", "!", "?")) and lowerish >= 1:
            return True
    # Common chapter openers
    if re.match(
        r"^(This chapter|There are|Player characters|Sites are|Maybe you|"
        r"A threat is|Discoveries are|NPCs are|The homefront|Stonetop is|"
        r"The basic moves|When you|Introductions are|Most events|"
        r"Use this procedure|Thanks to|The following works)",
        L,
        re.I,
    ) and len(L) >= 35:
        return True
    return False


def _is_chapter_toc_garbage(line: str) -> bool:
    """Column-glued junk often trailing a TOC (e.g. 'If you want of play…')."""
    L = line.strip()
    if not L:
        return True
    words = L.split()
    lowerish = sum(1 for w in words if w and w[0].islower())
    # Multi-word soup without sentence punctuation
    if len(words) >= 5 and lowerish >= 2 and not L.endswith((".", "!", "?", ":")):
        return True
    if len(L) > 42 and lowerish >= 2 and not L.endswith((".", "!", "?", ":")):
        return True
    # Lone wrap shards
    if L.lower() in {"up", "plan", "door", "moves", "fly", "change"}:
        return True
    return False


def split_chapter_toc(
    lines: list[str], article_title: str
) -> tuple[list[str], list[str]]:
    """
    Peel a chapter-opener table of contents off the start of extracted lines.

    Book I chapter first pages list section titles in a multi-column TOC.
    Those short lines currently become spurious <h2>s; strip them here so
    they can be rendered as sidebar deep links instead.

    Returns (toc_labels, body_lines). If no TOC is detected, toc is empty
    and body_lines is the original list.
    """
    if not lines or len(lines) < 5:
        return [], lines

    prose_idx: int | None = None
    for i, line in enumerate(lines):
        if _is_chapter_toc_prose(line):
            prose_idx = i
            break
    # Need a cluster of short TOC lines before prose
    if prose_idx is None or prose_idx < 3:
        return [], lines

    at = normalize_text(article_title).lower() if article_title else ""
    toc: list[str] = []
    for line in lines[:prose_idx]:
        L = line.strip()
        if not L:
            continue
        if at and normalize_text(L).lower() == at:
            continue
        if is_running_header(L, article_title, near_page_top=True):
            continue
        if _is_chapter_toc_garbage(L):
            continue
        # Short label / ALL CAPS move name
        if looks_like_heading(L) or (
            len(L) <= 40
            and L[0:1].isupper()
            and not L.endswith((".", ";", ","))
        ):
            toc.append(L)
        elif len(L) > 55:
            # Unexpected long non-prose before prose_idx — abort
            return [], lines

    if len(toc) < 3:
        return [], lines

    body = lines[prose_idx:]
    # Drop column-glued junk that sat between TOC and real prose
    while body and _is_chapter_toc_garbage(body[0]):
        body = body[1:]
    if not body:
        return [], lines
    return toc, body


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
            sec_letters = [c for c in sname if c.isalpha()]
            sec_caps = bool(sec_letters) and (
                sum(1 for c in sec_letters if c.isupper()) / len(sec_letters) >= 0.6
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


def extract_article_lines(
    doc: fitz.Document,
    start_page: int,
    end_page: int,
    article_title: str,
) -> list[str]:
    raw: list[str] = []
    for pno in range(start_page - 1, end_page):
        if pno < 0 or pno >= doc.page_count:
            continue
        page_lines = extract_page_lines(doc[pno], article_title=article_title)
        raw.extend(page_lines)
    # Normalize orphan bullet dingbats: "text" / "•" → "• text"
    fixed: list[str] = []
    for line in raw:
        if line in {"•", "·", "ä"} and fixed:
            prev = fixed[-1]
            if not prev.startswith("•"):
                fixed[-1] = "• " + prev
            continue
        if line.startswith("•") or line.startswith("·"):
            fixed.append("• " + line.lstrip("•· ").strip())
        else:
            fixed.append(line)
    merged = merge_wrapped_lines(fixed)
    # Split lines where a value-table header was glued to the previous item:
    # "Hauberk… (2 armor…) goods value" → two lines
    split_out: list[str] = []
    tail_re = re.compile(
        r"^(?P<item>.+?)\s+(?P<header>"
        r"(?:goods|coin|services|weapons?\s+armor|food(?:\s+and)?\s+lodging)"
        r"\s+value)\s*$",
        re.I,
    )
    steading_tail = re.compile(
        r"^(?P<pre>.+?)\s+(?P<label>steading improvement)\s*$",
        re.I,
    )
    for line in merged:
        m = tail_re.match(line)
        if m and not looks_like_value_header(line):
            split_out.append(m.group("item").strip())
            split_out.append(m.group("header").strip())
            continue
        # "… first services value"
        m2 = re.match(
            r"^(?P<pre>.+?\bfirst)\s+(?P<header>services\s+value)\s*$",
            line,
            re.I,
        )
        if m2:
            split_out.append(m2.group("pre").strip())
            split_out.append(m2.group("header").strip())
            continue
        # "…wrong steading improvement" / "…season steading improvement"
        m3 = steading_tail.match(line)
        if m3 and not re.fullmatch(r"steading improvement", line, re.I):
            pre = m3.group("pre").strip()
            if pre and not re.search(r"\bthe\s+Palisade\b", pre, re.I):
                split_out.append(pre)
                split_out.append(m3.group("label").strip())
                continue
        split_out.append(line)
    return split_out


def _is_all_caps_label(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 3 or len(line) > 55:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= 0.85


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
    hid = anchors.add(title or "Steading improvement")
    parts = [f'<div class="steading-improvement" id="{html.escape(hid)}">']
    if kind or starts_si:
        parts.append('<p class="si-kind">Steading improvement</p>')
    if title:
        parts.append(f'<h3 class="si-title">{html.escape(title)}</h3>')
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
            items.append(lines[j].strip())
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
        return f"{base}-{check_list_n}"

    while i < n:
        line = lines[i]

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
        dice = label = None
        m = ROLL_HEADER_RE.match(line)
        m_rev = re.match(
            r"^(.{1,40}?)\s+(\d{0,2}d(?:4|6|8|10|12|20))$", line, re.I
        )
        if m:
            dice, label = m.group(1), m.group(2).strip()
        elif m_rev and not ENTRY_RE.match(line):
            label, dice = m_rev.group(1).strip(), m_rev.group(2)
        elif (
            len(line) <= 40
            and not ENTRY_RE.match(line)
            and ROLL_HEADER_DICE_ONLY.match(peek(1) or "")
        ):
            # "theme" then "1d12"
            label = line
            dice = peek(1)
            i += 1  # consume dice line below after setting
        elif ROLL_HEADER_DICE_ONLY.match(line):
            dice = line
            # label may be previous short heading already emitted, or next line
            if out and re.search(r"<h2>[^<]{1,40}</h2>\s*$", out[-1]):
                label = re.sub(r"</?h2>", "", out[-1]).strip()
                out.pop()
            elif peek(1) and len(peek(1)) < 40 and not ENTRY_RE.match(peek(1)):
                # only treat next as label if it does NOT look like entry start
                if not re.match(r"^\d+", peek(1)):
                    i += 1
                    label = lines[i]
                else:
                    label = "result"
            else:
                label = "result"
        if dice:
            i += 1  # move past header line(s); label+dice path already advanced once
            entries: list[tuple[str, str]] = []
            while i < n:
                e = ENTRY_RE.match(lines[i])
                if e:
                    num = e.group(1) + (f"-{e.group(2)}" if e.group(2) else "")
                    body = e.group(3).strip()
                    i += 1
                    # continuations
                    while i < n and should_join(body, lines[i]) and not ENTRY_RE.match(lines[i]):
                        if lines[i].endswith("-") or body.endswith("-"):
                            body = body.rstrip("-") + lines[i].lstrip("-")
                        else:
                            body = body + " " + lines[i]
                        i += 1
                    entries.append((num, body))
                    continue
                # stop at next section/table/stat
                if (
                    looks_like_heading(lines[i])
                    or ROLL_HEADER_RE.match(lines[i])
                    or ROLL_HEADER_DICE_ONLY.match(lines[i])
                    or looks_like_tag_line(lines[i])
                ):
                    break
                # orphan continuation of last entry
                if entries and (
                    lines[i][0:1].islower()
                    or (entries and not ENTRY_RE.match(lines[i]) and len(lines[i]) < 90)
                ):
                    # only glue if doesn't look like new prose paragraph
                    if not (
                        lines[i][0:1].isupper()
                        and len(lines[i].split()) > 8
                        and lines[i].endswith((".", "!", "?"))
                    ):
                        num, body = entries[-1]
                        entries[-1] = (num, body + " " + lines[i])
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
                f'<span class="roll-label">{html.escape(label or "")}</span></h2>'
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
        is_creature_start = (
            len(line) <= 48
            and not line.endswith(".")
            and line.lower() not in _not_creature
            and not looks_like_value_header(line)
            and not VALUE_ROW_RE.match(line)
            and (
                (
                    looks_like_tag_line(peek(1))
                    and (
                        HP_LINE_RE.search(peek(2))
                        or HP_LINE_RE.search(peek(1))
                        or (peek(2) or "").lower().startswith("damage")
                    )
                )
                # tags may be missing; name then HP
                or (
                    HP_LINE_RE.search(peek(1) or "")
                    and not looks_like_heading(peek(1))
                )
            )
        )
        if is_creature_start:
            name = line
            i += 1
            block_lines: list[str] = []
            while i < n:
                L = lines[i]
                low = L.lower()
                # Next creature: short title then tags/HP
                if block_lines and looks_like_heading(L) and not L.startswith("•"):
                    nxt = peek(1)
                    if looks_like_tag_line(L):
                        # this line is tags for a creature we already have — shouldn't
                        # happen as heading; treat as end if we already have tags+stats
                        if any(HP_LINE_RE.search(x) for x in block_lines):
                            break
                    if looks_like_tag_line(nxt) or HP_LINE_RE.search(nxt or ""):
                        break
                    if low in _not_creature or L in {
                        "Dangers",
                        "Discoveries",
                        "Hooks",
                        "Lore",
                        "Questions",
                        "Impressions",
                        "Wasps",
                        "Unstable nest",
                        "Secrets",
                        "Moves",
                        "Shoddy construction",
                        "The Delves",
                        "Living conditions",
                    }:
                        break
                    # prose note after monster (starts mid-sentence capital long)
                    if len(L) > 50:
                        # still part of flavor after block? include if after moves
                        pass
                # Don't treat damage dice "d6 (hand, crude)" as roll-table headers
                if ROLL_HEADER_DICE_ONLY.match(L):
                    break
                if ROLL_HEADER_RE.match(L) and not re.match(
                    r"^d\d+\s*\([^)]*\)\s*$", L, re.I
                ):
                    # "1d6 theme" is a table; "d6 (hand, crude)" is damage
                    if not re.match(r"^d\d+\s*\(", L, re.I):
                        break
                if looks_like_value_header(L):
                    break
                # Flavor paragraph after complete stat block (long prose)
                if (
                    block_lines
                    and any(x.lower().startswith("instinct") for x in block_lines)
                    and len(L) > 60
                    and not L.startswith("•")
                    and not low.startswith(("damage", "hp", "special", "armor"))
                    and L[0:1].isupper()
                    and not looks_like_tag_line(L)
                ):
                    # include one flavor blurb then stop after collecting contiguous flavor
                    block_lines.append(L)
                    i += 1
                    while i < n and should_join(block_lines[-1], lines[i]):
                        block_lines[-1] = block_lines[-1] + " " + lines[i]
                        i += 1
                    # more flavor paragraphs for named NPCs
                    while i < n:
                        L2 = lines[i]
                        if (
                            looks_like_heading(L2)
                            or looks_like_tag_line(L2)
                            or HP_LINE_RE.search(L2)
                            or ROLL_HEADER_RE.match(L2)
                            or looks_like_value_header(L2)
                        ):
                            break
                        if L2.startswith("•"):
                            break
                        if len(L2) < 40 and L2[0:1].isupper() and looks_like_heading(L2):
                            break
                        block_lines.append(L2)
                        i += 1
                    break
                block_lines.append(L)
                i += 1
            out.append(
                render_stat_block(
                    name,
                    block_lines,
                    lookup,
                    current_slug,
                    section_index,
                    anchor_id=anchors.add(name),
                    link_kw=link_kw,
                )
            )
            continue

        # --- Heading ---
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
            level = "h2" if len(cleaned_h) < 40 else "h3"
            hid = anchors.add(cleaned_h.rstrip(":"))
            out.append(
                f'<{level} id="{html.escape(hid)}">'
                f"{html.escape(cleaned_h.rstrip(':'))}</{level}>"
            )
            i += 1
            continue

        # --- Bullet list run ---
        if line.startswith("•") or line.startswith("·"):
            items = []
            while i < n and (lines[i].startswith("•") or lines[i].startswith("·")):
                items.append(lines[i].lstrip("•· ").strip())
                i += 1
                # join soft wraps already done; also join if next is continuation
                while i < n and should_join(items[-1], lines[i]) and not lines[i].startswith("•"):
                    items[-1] = items[-1] + " " + lines[i]
                    i += 1
            out.append("<ul>")
            for it in items:
                out.append(f"<li>{link(it)}</li>")
            out.append("</ul>")
            continue

        # --- Numbered standalone list that isn't a formal 1dN table ---
        if ENTRY_RE.match(line) and peek(1) and ENTRY_RE.match(peek(1)):
            entries = []
            while i < n and ENTRY_RE.match(lines[i]):
                e = ENTRY_RE.match(lines[i])
                assert e
                num = e.group(1) + (f"-{e.group(2)}" if e.group(2) else "")
                body = e.group(3).strip()
                i += 1
                while i < n and should_join(body, lines[i]) and not ENTRY_RE.match(lines[i]):
                    body = body + " " + lines[i]
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

        # --- Regular paragraph ---
        out.append(f"<p>{link(line)}</p>")
        i += 1

    html_body = "\n".join(out)
    return html_body, anchors.sections


def _region_lines(
    page: fitz.Page,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
) -> list[str]:
    """Extract reading-order lines from a rectangular region of a page."""
    words = page.get_text("words")
    in_region = []
    for w in words:
        cx = (w[0] + w[2]) / 2
        cy = (w[1] + w[3]) / 2
        if x0 <= cx < x1 and y0 <= cy < y1:
            in_region.append(w)
    if not in_region:
        return []
    by_y: dict[float, list] = defaultdict(list)
    for w in in_region:
        by_y[round(w[1] * 2) / 2].append(w)
    lines: list[str] = []
    for y in sorted(by_y):
        text = normalize_text(
            " ".join(t[4] for t in sorted(by_y[y], key=lambda z: z[0]))
        )
        if not text:
            continue
        if is_fully_pairwise_doubled(text):
            text = undouble_words(text)
        else:
            # still collapse local doubles
            text = undouble_words(text) if is_fully_pairwise_doubled(text) else text
            # partial doubles are common in arcana titles
            text = undouble_words(text)
        if text.lower() in {"front", "back"} or (
            text.isdigit() and len(text) <= 3
        ):
            continue
        if "appendix" in text.lower():
            continue
        lines.append(text)
    return lines


def _card_title_from_sides(left: list[str], right: list[str]) -> str:
    """Prefer the named power on the back (right); fall back to front discovery title."""
    tag_re = re.compile(
        r"^(,?\s*)?(magical|fragile|immobile|terrifying|crude|slow|beautiful|"
        r"warm|close|reach|awkward|indestructible|large|cumbersome)\b",
        re.I,
    )
    junk_title = re.compile(
        r"^(starts at|souls\b|lost memories|daylight|vitality|hold\b|max\b|"
        r"uses\b|pouch of|when |if you|you can|your |mark |spend |"
        r"roll |on a |the spell|alas,)",
        re.I,
    )

    def title_from(side: list[str]) -> str | None:
        parts: list[str] = []
        for i, line in enumerate(side[:5]):
            if not line or tag_re.match(line):
                if parts:
                    break
                continue
            if junk_title.match(line):
                if parts:
                    break
                continue
            if line.lower() in {"front", "back"} or re.fullmatch(r"\d+", line):
                continue
            parts.append(line.rstrip(",").strip())
            joined = " ".join(parts)
            nxt = side[i + 1] if i + 1 < len(side) else ""
            # Continue when the name clearly wraps onto the next line
            incomplete = (
                line.endswith((",", "-", "'s", "\u2019s"))
                or len(line.split()) <= 2
                or (
                    len(joined) < 28
                    and nxt
                    and nxt[0:1].isupper()
                    and not junk_title.match(nxt)
                    and not tag_re.match(nxt)
                    and not nxt.lower().startswith("when ")
                )
            )
            if not incomplete and len(joined) >= 6:
                break
            if len(parts) >= 3:
                break
        if not parts:
            return None
        title = undouble_words(" ".join(parts)).strip(" ,")
        # Strip form labels / tags accidentally glued to the name
        title = re.sub(
            r"\s+(Heat|Strain|Disturbance|Breath|Preparation|Charges?|Ken|"
            r"Sway|Authority|Loyalty|Charge|Ire|Readiness|Vitality|Daylight|"
            r"Souls)\s*:?\s*$",
            "",
            title,
            flags=re.I,
        )
        title = re.sub(r"\s+You have\b.*$", "", title, flags=re.I)
        title = re.sub(r"\s+As long as\b.*$", "", title, flags=re.I)
        title = re.sub(
            r"\s*,\s*(?:far|close|near|hand|reach|magical|forceful|loud|"
            r"reload|ignores|warm|fragile|crude).*$",
            "",
            title,
            flags=re.I,
        )
        title = re.sub(r"\s+raspy voice\b.*$", "", title, flags=re.I)
        title = title.strip(" ,:;")
        if junk_title.match(title) or len(title) < 3:
            return None
        return title

    # Prefer named power (back), then discovery object (front)
    for side in (right, left):
        t = title_from(side)
        if t:
            return t
    return "Minor Arcanum"


def extract_minor_arcana_card(
    doc: fitz.Document,
    page_1based: int,
    half: str,
) -> tuple[str, list[str]]:
    """
    Minor arcana PDF pages hold TWO cards (top + bottom), each with
    front (left) and back (right). Returns (title, lines) for one card.
    half is 'top' or 'bottom'.
    """
    page = doc[page_1based - 1]
    w, h = page.rect.width, page.rect.height
    mid_x = w * 0.5
    # Split near the front/back labels (~y=307 on most pages)
    mid_y = h * 0.50
    for winfo in page.get_text("words"):
        if winfo[4].lower() in {"front", "back"} and 270 < winfo[1] < 340:
            mid_y = winfo[1] - 2
            break

    if half == "top":
        y0, y1 = 45, mid_y
    else:
        y0, y1 = mid_y, h - 25

    left = _region_lines(page, 0, mid_x, y0, y1)
    right = _region_lines(page, mid_x, w, y0, y1)
    title = _card_title_from_sides(left, right)
    # Front then back reading order
    lines = left + right
    # Drop pure tag-only first lines already handled; merge wraps
    lines = merge_wrapped_lines(lines)
    return title, lines


def list_minor_arcana_cards(
    doc: fitz.Document, start_page: int, end_page: int
) -> list[dict]:
    """
    Enumerate all minor arcana cards in the page range (2 per page → 64 total).
    Each dict: title, start_page, end_page, half ('top'|'bottom').
    """
    cards: list[dict] = []
    for p in range(start_page, end_page + 1):
        for half in ("top", "bottom"):
            title, _ = extract_minor_arcana_card(doc, p, half)
            if not title or title == "Minor Arcanum":
                # try still include with page fallback
                title = f"Minor Arcanum (p. {p} {half})"
            cards.append(
                {
                    "title": title,
                    "start_page": p,
                    "end_page": p,
                    "half": half,
                }
            )
    return cards


_ARCANA_TAGS = {
    "magical",
    "fragile",
    "immobile",
    "terrifying",
    "crude",
    "slow",
    "beautiful",
    "warm",
    "close",
    "reach",
    "awkward",
    "indestructible",
    "implanted",
    "large",
    "cumbersome",
    "reload",
    "near",
    "far",
    "hand",
    "messy",
    "area",
    "dangerous",
    "forceful",
    "ignorearmor",
    "ap",
    "thrown",
    "twohanded",
    "worn",
    "applied",
}


def _is_pure_arcana_tag_line(line: str) -> bool:
    """True if the whole line is one or more arcana tags (e.g. 'magical', 'fragile, immobile')."""
    L = line.strip().lstrip(",").strip()
    if not L or len(L) > 70:
        return False
    parts = [p.strip() for p in re.split(r"[,;]", L) if p.strip()]
    if not parts:
        return False
    for p in parts:
        pl = p.lower().strip()
        if pl in _ARCANA_TAGS:
            continue
        # "+1 damage", "1 piercing", bare "+2"
        if re.match(
            r"^\+?\d+(\s+(damage|piercing|armor|uses|weight|readiness))?$",
            pl,
        ):
            continue
        # strip digits and recheck single tag token
        core = re.sub(r"[\d+\s]", "", pl)
        if core in _ARCANA_TAGS:
            continue
        return False
    return True


def _split_discovery_and_tags(line: str) -> tuple[str, str]:
    """'An old scroll case, fragile' → ('An old scroll case', 'fragile')."""
    L = line.strip().lstrip(",").strip()
    if not L:
        return "", ""
    # Whole line is tags
    if _is_pure_arcana_tag_line(L):
        parts = [p.strip() for p in re.split(r"[,;]", L) if p.strip()]
        return "", ", ".join(parts)
    # Trailing tags after comma
    tag_alt = "|".join(
        re.escape(t) for t in sorted(_ARCANA_TAGS, key=len, reverse=True)
    )
    m = re.match(
        r"^(.+?)[,\s]+((?:(?:" + tag_alt + r")[\s,]*)+)$",
        L,
        re.I,
    )
    if m:
        return m.group(1).strip(" ,"), re.sub(r"\s+", " ", m.group(2).strip(" ,"))
    # Space-separated trailing tags only (no following prose):
    # "A giant's dormitory magical"
    m2 = re.match(
        r"^(.+?)\s+(" + tag_alt + r"(?:\s*,\s*(?:" + tag_alt + r"))*)\s*$",
        L,
        re.I,
    )
    if m2 and len(m2.group(1).split()) <= 8:
        disc = m2.group(1).strip(" ,")
        # Don't strip tags off a sentence that merely ends with a tag word by chance
        if not disc.endswith((".", "!", "?", "…")):
            return disc, re.sub(r"\s+", " ", m2.group(2).strip(" ,"))
    return L, ""


def _peel_discovery_tag_prose(line: str) -> tuple[str, str, str]:
    """
    Split a mashed discovery line into (title, tags, prose).
    'A giant\\'s dormitory magical In a ruin…' →
    (\"A giant's dormitory\", 'magical', 'In a ruin…')
    """
    L = line.strip()
    if not L:
        return "", "", ""
    tag_alt = "|".join(
        re.escape(t) for t in sorted(_ARCANA_TAGS, key=len, reverse=True)
    )
    m = re.match(
        r"^(.{{2,50}}?)\s+({tags}(?:\s*,\s*(?:{tags}))*)\s+([A-Z].+)$".format(
            tags=tag_alt
        ),
        L,
        re.I,
    )
    if m and len(m.group(1).split()) <= 6:
        disc = m.group(1).strip(" ,")
        if not disc.endswith((".", "!", "?", "…")):
            return (
                disc,
                re.sub(r"\s+", " ", m.group(2).strip(" ,")),
                m.group(3).strip(),
            )
    disc, tags = _split_discovery_and_tags(L)
    return disc, tags, ""


def _is_unlock_intro(line: str) -> bool:
    low = line.lower()
    if re.search(
        r"(you can learn|you can unlock|to unlock|to learn|the manual reveals|"
        r"there is magic here|the pictograms|the notes reveal|to unlock the|"
        r"need one of the following|need one of these|you must\b|"
        r"but to learn|but you must|but need|but…|but\.\.\.)",
        low,
    ):
        return True
    if "but" in low and (
        "learn" in low or "unlock" in low or "must" in low or "secret" in low
    ):
        return True
    return False


def _is_unlock_item(line: str) -> bool:
    L = line.strip()
    if not L:
        return False
    if L.startswith(("…", "...", "•", "·")):
        return True
    # Bare requirement after "need one of the following"
    if len(L) < 160 and L[0:1].isupper() and not re.match(r"^When you\b", L, re.I):
        if re.match(
            r"^(A |An |The |Some |Risk |Spend |Get |Acquire |Dig |"
            r"Translate |Decipher |Study |Meditate |First )",
            L,
        ):
            return True
    return False


def _is_unlock_divider(line: str) -> bool:
    L = line.strip()
    low = L.lower()
    if re.match(r"^or\.+$", low) or low in {"or…", "or...", "or"}:
        return True
    if re.match(r"^and then\b", low):
        return True
    if re.match(r"^and either\b", low):
        return True
    if re.match(r"^either\b", low) and len(L) < 40:
        return True
    return False


def _is_tag_token(tok: str) -> bool:
    t = tok.lower().strip(",+;")
    if t in _ARCANA_TAGS:
        return True
    if re.match(r"^\+?\d+$", t):
        return True
    if t in {"piercing", "damage", "armor", "uses", "weight"}:
        return True
    # "+1" already; "1d4" not a tag
    return False


def _strip_power_line_tags(line: str) -> tuple[str, str]:
    """
    'The Broom's Lullaby magical, area, near' →
    (\"The Broom's Lullaby\", 'magical, area, near')
    """
    L = line.strip().lstrip(",").strip()
    if not L:
        return "", ""
    # Leading comma-tags only
    if L.startswith(",") or (looks_like_tag_line(L) and not HP_LINE_RE.search(L)):
        return "", L.lstrip(", ").strip()
    # Try split discovery/tags helper (works for trailing comma-tags)
    name, tags = _split_discovery_and_tags(L)
    if tags:
        return name, tags
    # Space-separated trailing tags: "Name magical, area, near" or "Name magical area near"
    # Normalize commas to spaces for token walk
    rough = re.sub(r"[,;]+", " ", L)
    words = rough.split()
    tag_i = len(words)
    while tag_i > 1 and _is_tag_token(words[tag_i - 1]):
        tag_i -= 1
    if tag_i < len(words) and tag_i >= 1:
        # Rebuild tags from original trailing portion when possible
        name = " ".join(words[:tag_i])
        tag_str = ", ".join(words[tag_i:])
        return name, tag_str
    return L, ""


def _line_matches_power_title(line: str, power_norm: str) -> bool:
    if not power_norm or not line:
        return False
    name, _tags = _strip_power_line_tags(line)
    ln = normalize_section_key(name)
    if not ln:
        return False
    if ln == power_norm:
        return True
    # Allow title without leading "The "
    if power_norm.startswith("the ") and ln == power_norm[4:]:
        return True
    if ln.startswith("the ") and power_norm == ln[4:]:
        return True
    return False


def _find_power_title_index(lines: list[str], article_title: str) -> int | None:
    """Index where the power name (back of card) begins."""
    power = (article_title or "").strip()
    power_norm = normalize_section_key(power)
    if power_norm:
        for i, line in enumerate(lines):
            if re.match(r"^When you\b", line, re.I):
                continue
            if line.strip().startswith(("…", "...", "•", "·")):
                continue
            if _is_unlock_intro(line) and not _line_matches_power_title(line, power_norm):
                continue
            # Title alone, or title + tags on one line
            if _line_matches_power_title(line, power_norm):
                # Prefer later occurrence (back of card) over mention in unlock prose
                # Only accept if line is short-ish or starts with the title
                name, _ = _strip_power_line_tags(line)
                if len(line) <= len(power) + 40 or line.lower().startswith(
                    name[:12].lower()
                ):
                    # Skip if this is long unlock prose containing the name
                    if len(line) > len(power) + 50 and not re.match(
                        r"^" + re.escape(name), line, re.I
                    ):
                        continue
                    return i
            if i + 1 < len(lines) and len(lines[i + 1]) < 48:
                joined = line + " " + lines[i + 1]
                if _line_matches_power_title(joined, power_norm):
                    return i
            # Multi-line title fragments
            name, _ = _strip_power_line_tags(line)
            ln = normalize_section_key(name)
            if (
                ln
                and len(ln) >= 4
                and power_norm.startswith(ln + " ")
                and i + 1 < len(lines)
                and not re.match(r"^When you\b", lines[i + 1], re.I)
                and len(lines[i + 1]) < 40
            ):
                jn = normalize_section_key(name + " " + lines[i + 1])
                if jn == power_norm:
                    return i

    # Fallback: line immediately before first post-unlock "When you"
    for i, line in enumerate(lines):
        if not re.match(r"^When you\b", line, re.I) or i == 0:
            continue
        for j in range(i - 1, max(-1, i - 5), -1):
            L = lines[j].strip()
            if not L or L.startswith(("…", "...")):
                continue
            if _is_unlock_intro(L):
                continue
            if _is_unlock_item(L) and L.startswith(("…", "...")):
                continue
            name, _ = _strip_power_line_tags(L)
            if len(name.split()) > 8:
                continue
            ln = normalize_section_key(name)
            if power_norm and (
                ln == power_norm
                or power_norm.startswith(ln)
                or ln in power_norm
            ):
                return j
            if not power_norm and len(name.split()) <= 5:
                return j
        # No title line — power starts at this When you
        return i
    return None


def _merge_orphan_bullets(lines: list[str]) -> list[str]:
    """'•' / 'Consume…' → '• Consume…'; join mid-bullet wraps."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        L = lines[i].strip()
        if L in {"•", "·", "-", "–"} and i + 1 < len(lines):
            nxt = lines[i + 1].strip().lstrip("•· ").strip()
            if nxt:
                # Cost/HP/etc. are stats, not bullet bodies
                if re.match(r"^(Cost|HP|Armor|Damage|Instinct|Loyalty)\b", nxt, re.I):
                    out.append(nxt)
                    i += 2
                    continue
                # Continue previous fragment ending with 'and' / 'or'
                if out and re.search(
                    r"\b(and|or|to|the|a|an|with|,)\s*$", out[-1].rstrip(), re.I
                ):
                    out[-1] = out[-1].rstrip() + " " + nxt
                else:
                    out.append("• " + nxt)
                i += 2
                continue
        if L.startswith("•") and len(L.lstrip("•· ").strip()) == 0:
            i += 1
            continue
        # Bullet line that continues previous incomplete bullet/prose
        if L.startswith(("•", "·")):
            body = L.lstrip("•· ").strip()
            if out and re.search(
                r"\b(and|or|to|the|a|an|with|,)\s*$", out[-1].rstrip(), re.I
            ):
                out[-1] = out[-1].rstrip() + " " + body
                i += 1
                continue
            out.append("• " + body)
            i += 1
            continue
        # Mid-wrap without bullet marker
        if (
            out
            and (
                out[-1].startswith("•")
                or re.search(r"\b(and|or)\s*$", out[-1].rstrip(), re.I)
            )
            and (
                re.search(r"\b(and|or|to|the|a|an|with|,)\s*$", out[-1].rstrip(), re.I)
                or out[-1].count("(") > out[-1].count(")")
            )
            and L
            and not re.match(r"^When you\b", L, re.I)
            and not HP_LINE_RE.search(L)
            and not re.match(r"^(Cost|HP|Armor|Damage|Instinct)\b", L, re.I)
        ):
            out[-1] = out[-1].rstrip() + " " + L
            i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def _is_track_line(line: str) -> bool:
    """Blaze: 4nil, 1d4, 1d6… or similar stepped tracks."""
    return bool(
        re.match(
            r"^[A-Za-z][A-Za-z\s]{0,20}:\s*.*\b(nil|\d*d\d+)\b",
            line.strip(),
            re.I,
        )
    )


def _render_track_line(line: str, link_fn) -> str:
    """Render 'Blaze: 4nil, 1d4, 1d6, 1d8, 1d10' as a stepped track."""
    m = re.match(r"^([^:]+):\s*(.+)$", line.strip())
    if not m:
        return f'<p class="arcana-track">{link_fn(line)}</p>'
    label, rest = m.group(1).strip(), m.group(2).strip()
    steps = [s.strip() for s in re.split(r"[,/|]", rest) if s.strip()]
    cells = []
    for s in steps:
        # dice in step
        cell = html.escape(s)
        cell = DICE_RE.sub(
            lambda mm: dice_button(mm.group(1)), cell
        )
        cells.append(f'<span class="track-step">{cell}</span>')
    return (
        f'<div class="arcana-track">'
        f'<span class="track-label">{html.escape(label)}</span>'
        f'<span class="track-steps">{"".join(cells)}</span>'
        f"</div>"
    )


def _looks_like_follower_name(line: str) -> bool:
    L = line.strip()
    if not L or len(L) > 50 or re.match(r"^When you\b", L, re.I):
        return False
    if HP_LINE_RE.search(L) or looks_like_tag_line(L):
        return False
    words = L.split()
    if len(words) > 6:
        return False
    # Title Case or Proper Name
    return L[0:1].isupper() and not L.endswith(".")


def structure_minor_arcana_html(
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
) -> tuple[str, list[dict]]:
    """
    Layout a minor arcanum as a card: discovery front (with unlock checkboxes)
    and power back (moves).
    """
    anchors = AnchorRegistry()
    link_kw = {
        "lookups": lookups,
        "section_indexes": section_indexes,
        "current_book": current_book,
    }

    def link(text: str) -> str:
        return linkify_pages(
            text, lookup, current_slug, section_index, **link_kw
        )

    lines = _merge_orphan_bullets(list(lines))
    power_name = (article_title or "").strip() or "Minor Arcanum"
    power_norm = normalize_section_key(power_name)
    power_idx = _find_power_title_index(lines, power_name)

    # If title wasn't found but unlock prose ends before "When you", split there
    if power_idx is None:
        for i, line in enumerate(lines):
            if re.match(r"^When you\b", line, re.I):
                power_idx = i
                break

    front = lines[:power_idx] if power_idx is not None else lines[:]
    back = lines[power_idx:] if power_idx is not None else []

    # --- Front: discovery + unlock ---
    unlock_at = None
    for i, line in enumerate(front):
        if _is_unlock_intro(line):
            unlock_at = i
            break
    if unlock_at is None:
        for i, line in enumerate(front):
            if line.strip().startswith(("…", "...")):
                unlock_at = i
                break

    desc_lines = front[:unlock_at] if unlock_at is not None else front
    unlock_lines = front[unlock_at:] if unlock_at is not None else []

    discovery = ""
    tags = ""
    desc_paras: list[str] = []
    if desc_lines:
        discovery, tags, peeled_prose = _peel_discovery_tag_prose(desc_lines[0])
        rest = desc_lines[1:]
        if peeled_prose:
            rest = [peeled_prose] + rest
        if rest:
            d2, t2 = _split_discovery_and_tags(rest[0])
            if not d2 and t2:
                tags = (tags + ", " + t2).strip(", ")
                rest = rest[1:]
            elif not discovery and d2:
                discovery = d2
                if t2:
                    tags = (tags + ", " + t2).strip(", ")
                rest = rest[1:]
            elif _is_pure_arcana_tag_line(rest[0]):
                tags = (tags + ", " + rest[0].strip(" ,")).strip(", ")
                rest = rest[1:]
        buf = ""
        for line in rest:
            if (
                _is_pure_arcana_tag_line(line) or looks_like_tag_line(line)
            ) and not tags:
                tags = line.strip(" ,")
                continue
            if _is_pure_arcana_tag_line(line):
                tags = (tags + ", " + line.strip(" ,")).strip(", ")
                continue
            if buf and (
                line[0:1].islower()
                or buf.endswith((",", "—", "-", "…", "..."))
                or len(buf) < 40
            ):
                buf = buf + " " + line
            else:
                if buf:
                    desc_paras.append(buf)
                buf = line
        if buf:
            desc_paras.append(buf)

    unlock_intro = ""
    groups: list[tuple[str | None, list[str]]] = []
    cur_header: str | None = None
    cur_items: list[str] = []
    leftover_from_unlock: list[str] = []  # power side that leaked past split

    def flush_group():
        nonlocal cur_items, cur_header
        if cur_items:
            groups.append((cur_header, cur_items))
            cur_items = []
            cur_header = None

    for i, line in enumerate(unlock_lines):
        L = line.strip()
        if not L or re.fullmatch(r"\d+", L):
            continue
        # Reached power title (maybe with tags)
        if _line_matches_power_title(L, power_norm):
            leftover_from_unlock = unlock_lines[i:]
            break
        # Unlock intros may start with "When you enter… You can learn… but…"
        # — treat those as unlock text, not power moves.
        if (
            (i == 0 or not cur_items)
            and _is_unlock_intro(L)
            and not L.startswith(("…", "..."))
        ):
            if not unlock_intro:
                unlock_intro = L
            else:
                flush_group()
                cur_header = L
            continue
        if re.match(r"^When you\b", L, re.I):
            leftover_from_unlock = unlock_lines[i:]
            break
        if _is_unlock_divider(L):
            flush_group()
            cur_header = L.rstrip(".:…")
            continue
        item = re.sub(r"^[\.…]+\s*", "", L)
        item = item.lstrip("•· ").strip()
        if not item:
            continue
        if re.match(r"^When you\b", item, re.I) and not _is_unlock_intro(L):
            leftover_from_unlock = unlock_lines[i:]
            break
        if (
            cur_items
            and item[0:1].islower()
            and not L.startswith(("…", "...", "•"))
        ):
            cur_items[-1] = cur_items[-1] + " " + item
        else:
            cur_items.append(item)
    flush_group()

    # If power side was empty but unlock leftover has the power content, use it
    if leftover_from_unlock and (
        not back
        or (
            len(back) <= 1
            and back
            and _line_matches_power_title(back[0], power_norm)
        )
    ):
        back = leftover_from_unlock
    elif leftover_from_unlock and not any(
        re.match(r"^When you\b", x, re.I) for x in back
    ):
        # Merge leftovers if back missing moves
        back = leftover_from_unlock + back

    # --- Back: power + moves ---
    move_lines = back[:]
    power_tags = ""
    # Strip power title line(s), capture tags
    while move_lines:
        name, tgs = _strip_power_line_tags(move_lines[0])
        ln = normalize_section_key(name)
        if ln and (
            ln == power_norm
            or power_norm.startswith(ln)
            or ln.startswith(power_norm)
            or _line_matches_power_title(move_lines[0], power_norm)
        ):
            if tgs:
                power_tags = tgs
            move_lines = move_lines[1:]
            # second half of split title
            if move_lines:
                name2, tgs2 = _strip_power_line_tags(move_lines[0])
                jn = normalize_section_key(name + " " + name2)
                if jn == power_norm or power_norm.startswith(jn):
                    if tgs2:
                        power_tags = (power_tags + ", " + tgs2).strip(", ")
                    move_lines = move_lines[1:]
            continue
        # Pure tag line after title
        if move_lines[0].strip().startswith(",") or (
            looks_like_tag_line(move_lines[0]) and not HP_LINE_RE.search(move_lines[0])
        ):
            tonly = move_lines[0].strip().lstrip(",").strip()
            power_tags = (power_tags + ", " + tonly).strip(", ")
            move_lines = move_lines[1:]
            continue
        break

    move_blocks: list[str] = []
    i = 0
    while i < len(move_lines):
        line = move_lines[i].strip()
        if not line or re.fullmatch(r"\d+", line):
            i += 1
            continue

        # Weapon/power track (Blaze, etc.)
        if _is_track_line(line):
            move_blocks.append(_render_track_line(line, link))
            i += 1
            continue

        # Follower / creature block
        if _looks_like_follower_name(line) and i + 1 < len(move_lines) and (
            looks_like_tag_line(move_lines[i + 1])
            or HP_LINE_RE.search(move_lines[i + 1])
        ):
            name = line
            i += 1
            block_lines: list[str] = []
            while i < len(move_lines):
                L2 = move_lines[i].strip()
                if re.match(r"^When you\b", L2, re.I):
                    break
                if (
                    _looks_like_follower_name(L2)
                    and i + 1 < len(move_lines)
                    and looks_like_tag_line(move_lines[i + 1])
                    and block_lines
                ):
                    break
                block_lines.append(move_lines[i])
                i += 1
            # Format follower
            hid = anchors.add(name)
            fb = [
                f'<div class="arcana-follower" id="{html.escape(hid)}">',
                f'<h3 class="arcana-sub">{html.escape(name)}</h3>',
            ]
            bullets: list[str] = []
            for bl in block_lines:
                b = bl.strip()
                if not b or b == "HP":
                    continue
                if b.startswith("•") or b.startswith("·"):
                    bullets.append(b.lstrip("•· ").strip())
                elif looks_like_tag_line(b):
                    fb.append(f'<p class="arcana-tags">{html.escape(b)}</p>')
                elif re.match(r"^Cost\b", b, re.I):
                    # Flush moves before cost
                    if bullets:
                        fb.append('<ul class="arcana-picks arcana-moves-list">')
                        for bu in bullets:
                            if bu:
                                fb.append(f"<li>{link(bu)}</li>")
                        fb.append("</ul>")
                        bullets = []
                    fb.append(f'<p class="arcana-stat">{link(b)}</p>')
                elif HP_LINE_RE.search(b) or re.match(
                    r"^(Damage|Instinct|Special|Armor)\b", b, re.I
                ):
                    fb.append(f'<p class="arcana-stat">{link(b)}</p>')
                elif re.match(
                    r"^(Make |Sense |Consume |Weave |Grow |Shape |Cast |"
                    r"Heap |Demand |Unnerve )",
                    b,
                ):
                    bullets.append(b)
                else:
                    # Short imperative phrases are follower moves
                    if len(b) < 80 and b[0:1].isupper() and not b.endswith("."):
                        bullets.append(b)
                    else:
                        fb.append(f"<p>{link(b)}</p>")
            if bullets:
                fb.append('<ul class="arcana-picks arcana-moves-list">')
                for b in bullets:
                    if b:
                        fb.append(f"<li>{link(b)}</li>")
                fb.append("</ul>")
            fb.append("</div>")
            move_blocks.append("\n".join(fb))
            continue

        if re.match(r"^When you\b", line, re.I) or re.match(
            r"^(Lost memories|Strain|Starts at)\b", line, re.I
        ):
            body_parts = [line]
            i += 1
            pick_items: list[str] = []
            in_pick = bool(
                re.search(r"\bpick\s+\d", line, re.I)
                or re.search(r"\bdesire:?\s*$", line, re.I)
            )
            while i < len(move_lines):
                L2 = move_lines[i].strip()
                if not L2:
                    i += 1
                    continue
                if re.match(r"^When you\b", L2, re.I):
                    break
                if _is_track_line(L2):
                    break
                if _looks_like_follower_name(L2) and i + 1 < len(move_lines) and (
                    looks_like_tag_line(move_lines[i + 1])
                    or HP_LINE_RE.search(move_lines[i + 1])
                ):
                    break
                # Standalone rule line after a complete When-you sentence
                if (
                    re.match(
                        r"^(Reduce |Increase |Clear |Mark |Also,|But |The |You cannot )",
                        L2,
                    )
                    and body_parts
                    and body_parts[-1].endswith((".", "!", "?"))
                    and not in_pick
                ):
                    break
                if re.match(r"^(Then,|Also,|But |If |While |Each )", L2):
                    if pick_items:
                        break
                    body_parts.append(L2)
                    i += 1
                    in_pick = bool(re.search(r"\bpick\s+\d", L2, re.I))
                    continue
                if in_pick and not re.match(r"^When you\b", L2, re.I):
                    if re.match(
                        r"^(It'?s |You |Regain |Clear |The |Act |Resist |Cast |"
                        r"Hold |Point |Cause |Mark |Its |Hard |Solid |Quick )",
                        L2,
                    ) or (len(L2) < 100 and L2[0:1].isupper()):
                        pick_items.append(L2)
                        i += 1
                        continue
                # Continuation of move prose
                if L2[0:1].islower() or not body_parts[-1].endswith((".", "!", "?")):
                    body_parts.append(L2)
                    i += 1
                    if re.search(r"\bpick\s+\d", L2, re.I):
                        in_pick = True
                    continue
                # New capital sentence — keep as part of move if short continuation
                if re.match(r"^(They |It |This |That |While |If |On a )", L2):
                    body_parts.append(L2)
                    i += 1
                    continue
                break
            para = " ".join(body_parts)
            block = f'<div class="arcana-move"><p>{link(para)}</p>'
            if pick_items:
                block += '<ul class="arcana-picks">'
                for pi in pick_items:
                    block += f"<li>{link(pi)}</li>"
                block += "</ul>"
            block += "</div>"
            move_blocks.append(block)
            continue

        # Bullet already merged
        if line.startswith("•") or line.startswith("·"):
            # collect consecutive bullets
            bullets = [line.lstrip("•· ").strip()]
            i += 1
            while i < len(move_lines):
                L2 = move_lines[i].strip()
                if L2.startswith("•") or L2.startswith("·"):
                    bullets.append(L2.lstrip("•· ").strip())
                    i += 1
                elif L2 and L2[0:1].islower() and bullets:
                    bullets[-1] = bullets[-1] + " " + L2
                    i += 1
                else:
                    break
            move_blocks.append(
                '<ul class="arcana-picks arcana-moves-list">'
                + "".join(f"<li>{link(b)}</li>" for b in bullets if b)
                + "</ul>"
            )
            continue

        if looks_like_heading(line) and len(line) < 40:
            hid = anchors.add(line)
            move_blocks.append(
                f'<h3 id="{html.escape(hid)}" class="arcana-sub">{html.escape(line)}</h3>'
            )
            i += 1
            continue

        move_blocks.append(f"<p>{link(line)}</p>")
        i += 1

    # --- Assemble card ---
    hid = anchors.add(power_name)
    parts = [f'<div class="arcana-card" id="{html.escape(hid)}">']

    parts.append('<div class="arcana-face arcana-front">')
    parts.append('<p class="arcana-face-label">Discovery</p>')
    if discovery:
        parts.append(f'<h3 class="arcana-discovery">{html.escape(discovery)}</h3>')
    if tags:
        parts.append(f'<p class="arcana-tags">{html.escape(tags)}</p>')
    for p in desc_paras:
        parts.append(f"<p>{link(p)}</p>")

    if unlock_intro or groups:
        parts.append('<div class="arcana-unlock">')
        if unlock_intro:
            parts.append(f'<p class="arcana-unlock-intro">{link(unlock_intro)}</p>')
        check_n = 0
        for header, items in groups:
            if header:
                parts.append(f'<p class="si-requires">{html.escape(header)}:</p>')
            if items:
                check_n += 1
                lid = f"arcana-{slugify_id(power_name)}-{check_n}"
                parts.append(render_check_list(items, link, lid))
        parts.append("</div>")
    parts.append("</div>")  # front

    parts.append('<div class="arcana-face arcana-back-face">')
    parts.append('<p class="arcana-face-label">Power</p>')
    parts.append(f'<h2 class="arcana-power-name">{html.escape(power_name)}</h2>')
    if power_tags:
        parts.append(f'<p class="arcana-tags">{html.escape(power_tags)}</p>')
    parts.extend(move_blocks)
    parts.append("</div>")  # back

    parts.append("</div>")  # card
    return "\n".join(parts), anchors.sections


def extract_major_arcana_blocks(
    doc: fitz.Document,
    start_page: int,
    end_page: int,
) -> list[str]:
    """
    Extract major-arcana card text using PDF text blocks (reading order).
    Much cleaner than word-stream extraction for two-page front/back cards.
    """
    paras: list[str] = []
    for pno in range(start_page - 1, end_page):
        if pno < 0 or pno >= doc.page_count:
            continue
        page = doc[pno]
        blocks = page.get_text("blocks")
        blocks = sorted(
            [b for b in blocks if str(b[4]).strip()],
            key=lambda b: (round(b[1] / 3) * 3, b[0]),
        )
        for b in blocks:
            raw = str(b[4])
            if not raw.strip():
                continue
            # Per-line first (preserve list items), then soft-wrap join
            raw_lines = [
                normalize_text(p) for p in raw.split("\n") if normalize_text(p)
            ]
            if not raw_lines:
                continue
            merged: list[str] = []
            for p in raw_lines:
                low = p.lower()
                if "appendix" in low:
                    continue
                if low in {"front", "back"} or re.fullmatch(r"\d{1,3}", low):
                    continue
                new_item = re.match(
                    r"^(When you|You |Your |Pick |During |Attack |Strike |"
                    r"Disengage |Name |Mysteries |Moves|Consequences|"
                    r"UNQUENCHED|A [A-Z])",
                    p,
                )
                if merged and not new_item and (
                    p[0:1].islower()
                    or p[0:1].isdigit()
                    or p[0:1] in "([+\""
                    or merged[-1].endswith(("-", "–", "—", ",", ";", "—"))
                    or not merged[-1].endswith((".", ":", "!", "?"))
                ):
                    merged[-1] = merged[-1] + " " + p
                else:
                    merged.append(p)
            for joined in merged:
                joined = re.sub(r"[ \t]+", " ", joined).strip()
                if not joined:
                    continue
                # "TITLE When you..." packed on one line
                m = re.match(
                    r"^([A-Z][A-Z0-9\s'\-]{2,40}?)\s+(When you\b.*)$", joined
                )
                if m and len(m.group(1).split()) <= 6:
                    paras.append(m.group(1).strip())
                    paras.append(m.group(2).strip())
                    continue
                paras.append(joined)
    return paras


def _is_mark_track_line(line: str) -> int:
    """
    Detect progress mark rows like 'l l l l l' (PDF dingbats as 'l').
    Returns number of marks, or 0 if not a track.
    """
    L = line.strip()
    # only l's and spaces (and maybe o/O/□)
    if re.fullmatch(r"[lLoO□☐○●•·\s]{2,}", L):
        n = len(re.findall(r"[lLoO□☐○●•·]", L))
        if 2 <= n <= 12:
            return n
    return 0


def structure_major_arcana_html(
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
) -> tuple[str, list[dict]]:
    """
    Layout a major arcanum: front moves + mark tracks, back mysteries +
    consequence checkboxes.
    """
    anchors = AnchorRegistry()
    link_kw = {
        "lookups": lookups,
        "section_indexes": section_indexes,
        "current_book": current_book,
    }

    def link(text: str) -> str:
        return linkify_pages(
            text, lookup, current_slug, section_index, **link_kw
        )

    power = (article_title or "Major Arcanum").strip()
    power_norm = normalize_section_key(power)

    # Drop leading title lines that match the power name
    i = 0
    tags = ""
    while i < len(lines):
        L = lines[i].strip()
        name, tgs = _strip_power_line_tags(L)
        ln = normalize_section_key(name)
        if ln == power_norm or (
            ln and power_norm.startswith(ln) and len(ln) >= 4
        ):
            if tgs:
                tags = tgs
            i += 1
            # next pure-tag line
            if i < len(lines) and (
                lines[i].strip().startswith(",")
                or looks_like_tag_line(lines[i])
            ):
                tags = (tags + ", " + lines[i].strip().lstrip(",")).strip(", ")
                i += 1
            continue
        if L.startswith(",") or (looks_like_tag_line(L) and not tags):
            tags = L.lstrip(", ").strip()
            i += 1
            continue
        break

    front_parts: list[str] = []
    mystery_parts: list[str] = []
    cons_items: list[str] = []
    section = "front"  # front | mysteries | consequences

    while i < len(lines):
        L = lines[i].strip()
        i += 1
        if not L:
            continue
        if re.match(r"^Mysteries of\b", L, re.I):
            section = "mysteries"
            continue
        if re.match(r"^Moves\b", L, re.I) and section != "front":
            continue
        if re.match(r"^Consequences\b", L, re.I):
            section = "consequences"
            continue
        if re.match(r"^(front|back)$", L, re.I):
            continue

        if section == "consequences":
            # New consequence starts with You/Your/Pick…; "When you" continues
            # Sub-effects like "Your instinct becomes…" stay under the prior item.
            if re.match(r"^When you\b", L) and cons_items:
                cons_items[-1] = cons_items[-1] + " " + L
            elif re.match(r"^Your instinct becomes\b", L, re.I) and cons_items:
                cons_items[-1] = cons_items[-1] + " " + L
            elif re.match(r"^(You |Your |Pick |Whenever )", L):
                cons_items.append(L)
            elif cons_items:
                cons_items[-1] = cons_items[-1] + " " + L
            else:
                cons_items.append(L)
            continue

        if section == "mysteries":
            mystery_parts.append(L)
            continue

        # Front
        front_parts.append(L)

    def render_front_or_mystery(parts: list[str], face: str) -> str:
        out: list[str] = []
        j = 0
        while j < len(parts):
            line = parts[j].strip()
            j += 1
            if not line:
                continue
            # Progress mark track (requirements toward unlocking mysteries)
            nmarks = _is_mark_track_line(line)
            if nmarks:
                lid = f"arcana-{slugify_id(power)}-{face}-marks"
                out.append(render_mark_track(nmarks, lid, label="Progress marks"))
                continue
            # ALL-CAPS move name
            letters = [c for c in line if c.isalpha()]
            if (
                letters
                and sum(1 for c in letters if c.isupper()) / len(letters) >= 0.75
                and len(line) < 50
                and not re.match(r"^When you\b", line, re.I)
            ):
                hid = anchors.add(line.title() if line.isupper() else line)
                out.append(
                    f'<h3 id="{html.escape(hid)}" class="arcana-sub">'
                    f"{html.escape(line)}</h3>"
                )
                continue
            # When-you move (+ optional spend/pick list)
            if re.match(r"^When you\b", line, re.I) or re.match(
                r"^During this battle\b", line, re.I
            ):
                body = [line]
                picks: list[str] = []
                in_pick = bool(
                    re.search(r"\b(pick|spend|do the following|hold)\b", line, re.I)
                )
                while j < len(parts):
                    L2 = parts[j].strip()
                    if not L2:
                        j += 1
                        continue
                    if re.match(r"^When you\b", L2, re.I):
                        break
                    if re.match(r"^During this battle\b", L2, re.I):
                        break
                    if _is_mark_track_line(L2):
                        break
                    letters2 = [c for c in L2 if c.isalpha()]
                    if (
                        letters2
                        and sum(1 for c in letters2 if c.isupper()) / len(letters2)
                        >= 0.75
                        and len(L2) < 50
                    ):
                        break
                    if re.match(r"^Consequences\b", L2, re.I):
                        break
                    # pick/spend options
                    if in_pick and (
                        re.match(
                            r"^(Attack |Strike |Disengage |Name |Roll |Hold |"
                            r"Cast |Spend |Ignore |Deal |Move |Cross )",
                            L2,
                        )
                        or (
                            len(L2) < 120
                            and L2[0:1].isupper()
                            and not re.match(r"^(When |During |Then |Also )", L2)
                        )
                    ):
                        # continuation of option?
                        if L2[0:1].islower() and picks:
                            picks[-1] = picks[-1] + " " + L2
                        else:
                            picks.append(L2)
                        j += 1
                        continue
                    if re.search(r"\b(do the following|spend \w+, 1-for-1)\b", L2, re.I):
                        body.append(L2)
                        in_pick = True
                        j += 1
                        continue
                    # prose continuation
                    body.append(L2)
                    j += 1
                    if re.search(r"\b(pick|following|1-for-1)\b", L2, re.I):
                        in_pick = True
                block = (
                    f'<div class="arcana-move"><p>{link(" ".join(body))}</p>'
                )
                if picks:
                    block += '<ul class="arcana-picks">'
                    for p in picks:
                        block += f"<li>{link(p)}</li>"
                    block += "</ul>"
                block += "</div>"
                out.append(block)
                continue
            # Description / other prose
            out.append(f"<p>{link(line)}</p>")
        return "\n".join(out)

    hid = anchors.add(power)
    parts = [
        f'<div class="arcana-card arcana-major" id="{html.escape(hid)}">',
        '<div class="arcana-face arcana-front">',
        '<p class="arcana-face-label">Front</p>',
        f'<h2 class="arcana-power-name">{html.escape(power)}</h2>',
    ]
    if tags:
        parts.append(f'<p class="arcana-tags">{html.escape(tags)}</p>')
    parts.append(render_front_or_mystery(front_parts, "front"))
    parts.append("</div>")  # front

    if mystery_parts or cons_items:
        parts.append('<div class="arcana-face arcana-back-face">')
        parts.append('<p class="arcana-face-label">Back — Mysteries</p>')
        if mystery_parts:
            parts.append(render_front_or_mystery(mystery_parts, "back"))
        if cons_items:
            lid = f"arcana-{slugify_id(power)}-cons"
            parts.append('<div class="arcana-unlock arcana-consequences">')
            parts.append('<p class="si-requires">Consequences</p>')
            parts.append(render_check_list(cons_items, link, lid))
            parts.append(
                '<p class="arcana-note"><em>Mark consequences as they apply.</em></p>'
            )
            parts.append("</div>")
        parts.append("</div>")  # back

    parts.append("</div>")  # card
    return "\n".join(parts), anchors.sections


def minor_arcana_html_from_pdf(
    doc: fitz.Document,
    page_1based: int,
    half: str,
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    lines: list[str] | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, str, list[dict]]:
    """Build HTML for a single minor arcanum card (front+back).

    Returns (body_html, excerpt, sections).
    """
    if lines is None:
        title, lines = extract_minor_arcana_card(doc, page_1based, half)
    else:
        title = article_title
    body, sections = structure_minor_arcana_html(
        lines,
        article_title or title,
        lookup,
        articles,
        current_slug,
        section_index=section_index,
        lookups=lookups,
        section_indexes=section_indexes,
        current_book=current_book,
    )
    excerpt = ""
    for line in lines:
        if len(line) >= 50 and not line.lower().startswith("when you"):
            excerpt = line
            break
    if not excerpt:
        for line in lines:
            if len(line) >= 40:
                excerpt = line
                break
    if not excerpt and lines:
        excerpt = lines[0]
    if len(excerpt) > 320:
        excerpt = excerpt[:319].rsplit(" ", 1)[0] + "…"
    return body, excerpt, sections


def major_arcana_html_from_pdf(
    doc: fitz.Document,
    start_page: int,
    end_page: int,
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    lines: list[str] | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, str, list[dict]]:
    """Build HTML for a major arcanum (two-page card)."""
    # Prefer block extraction; fall back to provided/legacy lines
    block_lines = extract_major_arcana_blocks(doc, start_page, end_page)
    if len(block_lines) >= 4:
        lines = block_lines
    elif lines is None:
        lines = extract_article_lines(doc, start_page, end_page, article_title)
    body, sections = structure_major_arcana_html(
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
    excerpt = ""
    for line in lines:
        if len(line) >= 50 and not line.lower().startswith("when you"):
            excerpt = line
            break
    if not excerpt and lines:
        excerpt = lines[0]
    if len(excerpt) > 320:
        excerpt = excerpt[:319].rsplit(" ", 1)[0] + "…"
    return body, excerpt, sections


def article_html_from_pdf(
    doc: fitz.Document,
    start_page: int,
    end_page: int,
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    lines: list[str] | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, str, list[dict]]:
    """
    Returns (body_html, excerpt_text, sections).
    """
    if lines is None:
        lines = extract_article_lines(doc, start_page, end_page, article_title)
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
    # Excerpt from first long-ish prose line
    excerpt = ""
    for line in lines:
        if len(line) >= 60 and not ENTRY_RE.match(line) and not looks_like_tag_line(line):
            if not HP_LINE_RE.search(line) and not ROLL_HEADER_RE.match(line):
                excerpt = line
                break
    if not excerpt and lines:
        excerpt = lines[0]
    if len(excerpt) > 320:
        excerpt = excerpt[:319].rsplit(" ", 1)[0] + "…"
    return body, excerpt, sections


def build_section_index(
    per_slug_sections: dict[str, list[dict]],
    articles: list[dict],
) -> dict:
    """Build cross-page lookup for section/monster deep links."""
    by_slug_norm: dict[tuple[str, str], str] = {}
    by_page_norm: dict[tuple[int, str], tuple[str, str]] = {}
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
    return {"by_slug_norm": by_slug_norm, "by_page_norm": by_page_norm}

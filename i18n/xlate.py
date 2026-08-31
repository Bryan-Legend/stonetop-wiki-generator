"""Segment-based translation harness for wiki page bodies.

The structure of a page body is never translated — only its visible text and
its title/aria-label attributes are. So a translation can be authored as a
numbered list of text segments, and the body rebuilt by re-injecting them
into the English markup. The markup then *cannot* drift.

    python i18n/xlate.py extract <slug> [...]
        Reads the built English page, writes i18n/_work/<slug>.segs.txt:
        meta stub + one numbered line per translatable segment.

    python i18n/xlate.py apply <code> <slug> [...]
        Reads i18n/_work/<code>/<slug>.segs.txt (same numbering, translated,
        meta filled in) and writes i18n/_work/<code>/<slug>.html — the full
        translated body work file that pack.py consumes.

Segments are written as plain text (no HTML entities); apply escapes them.
A segment left identical to the English is fine (names, "HP 6" fragments).
"""

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WIKI = ROOT.parent / "Stonetop_Wiki"
WORK = ROOT / "_work"

BODY_RE = re.compile(r'<main class="content[^"]*">\n {10}(.*?)\n {8}</main>', re.S)
H1_RE = re.compile(r'^<h1 class="page-title[^"]*">.*?</h1>\n?', re.S)
TAG_SPLIT = re.compile(r"(<[^>]+>)")
ATTR_RE = re.compile(r'\b(title|aria-label)="([^"]*)"')

# Not worth translating / must stay: stat abbreviations, dice, bare symbols.
SKIP_RES = [
    re.compile(r"^[^A-Za-z]*$"),                       # no letters at all
    re.compile(r"^\(?(?:STR|DEX|CON|INT|WIS|CHA|HP|XP)\)?$"),
    re.compile(r"^[0-9]*d[0-9]+(?:[+-][0-9]+)?$"),     # d6, 1d6, 2d4+1
    re.compile(r"^x$"),
]


def english_body(slug: str) -> str:
    text = (WIKI / f"{slug}.html").read_text(encoding="utf-8")
    m = BODY_RE.search(text)
    if not m:
        raise SystemExit(f"{slug}: no <main> body in built English page")
    return H1_RE.sub("", m.group(1), count=1)


def translatable(seg: str) -> bool:
    s = seg.strip()
    if not s:
        return False
    return not any(r.match(s) for r in SKIP_RES)


def walk(body: str):
    """Yield ("text"|"attr", value, context) for every translatable segment,
    in document order. Context identifies the piece for reassembly."""
    parts = TAG_SPLIT.split(body)
    for pi, part in enumerate(parts):
        if part.startswith("<"):
            for m in ATTR_RE.finditer(part):
                val = html.unescape(m.group(2))
                if translatable(val):
                    yield ("attr", val, (pi, m.group(1)))
        else:
            val = html.unescape(part)
            if translatable(val):
                yield ("text", val, (pi,))


def extract(slug: str) -> None:
    body = english_body(slug)
    lines = [
        "title: ",
        "nav_label: ",
        "description: ",
        "",
    ]
    n = 0
    for _, val, _ in walk(body):
        if "\n" in val:
            raise SystemExit(f"{slug}: segment with newline: {val!r}")
        n += 1
        lines.append(f"{n:04d}\t{val}")
    out = WORK / f"{slug}.segs.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{slug}: {n} segments -> {out.name}")


def parse_segs(path: Path):
    meta = {}
    segs = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(\d{4})\t(.*)$", line)
        if m:
            segs[int(m.group(1))] = m.group(2)
        else:
            km = re.match(r"^(title|nav_label|description):\s*(.*)$", line)
            if km:
                meta[km.group(1)] = km.group(2).strip()
    return meta, segs


def apply(code: str, slug: str) -> None:
    src = WORK / code / f"{slug}.segs.txt"
    meta, segs = parse_segs(src)
    for key in ("title", "nav_label", "description"):
        if not meta.get(key):
            raise SystemExit(f"{code}/{slug}: meta missing {key}")
    body = english_body(slug)
    parts = TAG_SPLIT.split(body)
    n = 0
    for kind, val, ctx in walk(body):
        n += 1
        if n not in segs:
            raise SystemExit(f"{code}/{slug}: segment {n:04d} missing")
        tr = segs[n]
        pi = ctx[0]
        if kind == "text":
            # Replace exactly the original text run inside this part.
            esc_orig = parts[pi]
            new = html.escape(tr, quote=False)
            parts[pi] = new
            if esc_orig.startswith(" ") or esc_orig.endswith(" "):
                # preserve leading/trailing whitespace of the original run
                lead = esc_orig[: len(esc_orig) - len(esc_orig.lstrip())]
                tail = esc_orig[len(esc_orig.rstrip()):]
                parts[pi] = lead + new.strip() + tail if tr.strip() else esc_orig
        else:
            attr = ctx[1]
            new = html.escape(tr, quote=True)
            parts[pi] = re.sub(
                rf'\b{attr}="[^"]*"',
                lambda m, new=new: f'{attr}="{new}"',
                parts[pi],
                count=1,
            )
    total = len(segs)
    if total != n:
        extra = sorted(set(segs) - set(range(1, n + 1)))
        raise SystemExit(
            f"{code}/{slug}: {total} segments supplied, {n} expected"
            + (f" (unexpected: {extra[:5]})" if extra else "")
        )
    out_body = "".join(parts)
    front = (
        "<!--i18n\n"
        f"title: {meta['title']}\n"
        f"nav_label: {meta['nav_label']}\n"
        f"description: {meta['description']}\n"
        "-->\n"
    )
    dest = WORK / code / f"{slug}.html"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(front + out_body + "\n", encoding="utf-8")
    print(f"{code}/{slug}: applied {n} segments -> {dest}")


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    if cmd == "extract":
        for slug in sys.argv[2:]:
            extract(slug)
    elif cmd == "apply":
        code = sys.argv[2]
        for slug in sys.argv[3:]:
            apply(code, slug)
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()

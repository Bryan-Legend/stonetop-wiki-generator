"""Pack translation work files into i18n/pages/<code>/<slug>.json.

A work file is raw HTML — no JSON escaping to get wrong — at
``i18n/_work/<code>/<slug>.html``:

    <!--i18n
    title: Der Waldläufer
    nav_label: Waldläufer
    description: Eine Seite für Suchergebnisse und Linkvorschauen.
    -->
    <p>…translated body, WITHOUT the page's <h1>…</p>

The packer computes ``source_sha256`` from the built English page, derives the
``sections`` map from the translated body's own <h2>/<h3> ids, and validates
that everything machine-readable — ids, hrefs, data-* attributes, the tag
histogram — is byte-identical to the English body. Only visible text may
differ; a slip in a data-field-key or a dropped checkbox fails the pack.

Usage:  python i18n/pack.py [code [slug]]
"""

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # i18n/
REPO = ROOT.parent
WIKI = REPO / "Stonetop_Wiki"
WORK = ROOT / "_work"

BODY_RE = re.compile(r'<main class="content[^"]*">\n {10}(.*?)\n {8}</main>', re.S)
META_RE = re.compile(r"^<!--i18n\n(.*?)\n-->\n?", re.S)
H1_RE = re.compile(r'^<h1 class="page-title[^"]*">.*?</h1>\n?', re.S)
SECTION_RE = re.compile(r'<h([23])\s+id="([^"]+)"[^>]*>(.*?)</h\1>', re.S)
# Attributes whose values scripts and styles key on: must not be translated.
# title and aria-label are visible/audible text and MAY be translated.
KEEP_NAMES = re.compile(
    r"(?:id|class|href|name|type|for|value|role|checked|disabled"
    r"|tabindex|inputmode|autocomplete|aria-hidden|aria-valuemin"
    r"|aria-valuemax|data-[a-z0-9-]+)$"
)
# Attributes are tokenized sequentially so a word inside a quoted value
# (placeholder="your master's name") can never read as an attribute.
ATTR_TOKEN = re.compile(r'\s*([a-zA-Z][a-zA-Z0-9:_-]*)(="[^"]*")?')


def keep_attrs(attrs: str) -> list[str]:
    out = []
    pos = 0
    while pos < len(attrs):
        m = ATTR_TOKEN.match(attrs, pos)
        if not m or m.end() == pos:
            break
        pos = m.end()
        if KEEP_NAMES.match(m.group(1)):
            out.append(m.group(1) + (m.group(2) or ""))
    return out


def english_body(slug: str) -> str:
    text = (WIKI / f"{slug}.html").read_text(encoding="utf-8")
    m = BODY_RE.search(text)
    if not m:
        raise SystemExit(f"{slug}: no <main> body in built English page")
    return m.group(1)


def strip_h1(body: str) -> str:
    return H1_RE.sub("", body, count=1)


def skeleton(body: str) -> list[str]:
    """Everything machine-readable, in order: tags with their keyed attrs."""
    out = []
    for m in re.finditer(r"<(/?)([a-zA-Z0-9]+)((?:[^>\"]|\"[^\"]*\")*)>", body):
        attrs = " ".join(keep_attrs(m.group(3)))
        out.append(f"<{m.group(1)}{m.group(2)} {attrs}".rstrip())
    return out


def diff_skeletons(en: list[str], tr: list[str], label: str) -> bool:
    if en == tr:
        return True
    print(f"  FAIL {label}: structure differs from English")
    if Counter(en) != Counter(tr):
        c_en, c_tr = Counter(en), Counter(tr)
        for k in (c_en - c_tr):
            print(f"    missing: {k}  (x{(c_en - c_tr)[k]})")
        for k in (c_tr - c_en):
            print(f"    extra:   {k}  (x{(c_tr - c_en)[k]})")
    else:
        for i, (a, b) in enumerate(zip(en, tr)):
            if a != b:
                print(f"    first reorder at tag #{i}: {a!r} vs {b!r}")
                break
    return False


def sections_of(body: str) -> dict[str, str]:
    out = {}
    for m in SECTION_RE.finditer(body):
        name = re.sub(r"<[^>]+>", "", m.group(3))
        name = re.sub(r"\s+", " ", name).strip()
        # Unescape the few entities headings use.
        for a, b in (("&amp;", "&"), ("&#x27;", "'"), ("&quot;", '"')):
            name = name.replace(a, b)
        out[m.group(2)] = name
    return out


def pack_one(code: str, path: Path) -> bool:
    slug = path.stem
    raw = path.read_text(encoding="utf-8")
    m = META_RE.match(raw)
    if not m:
        print(f"  FAIL {code}/{slug}: no <!--i18n --> front matter")
        return False
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    for key in ("title", "nav_label", "description"):
        if not meta.get(key):
            print(f"  FAIL {code}/{slug}: front matter missing {key}")
            return False
    body = raw[m.end():].strip("\n")
    if "<h1" in body.split("\n", 1)[0]:
        print(f"  FAIL {code}/{slug}: body must not carry the <h1>")
        return False

    en_full = english_body(slug)
    en = strip_h1(en_full)
    if not diff_skeletons(skeleton(en), skeleton(body), f"{code}/{slug}"):
        return False

    data = {
        "lang": code,
        "slug": slug,
        "source_sha256": hashlib.sha256(en_full.encode("utf-8")).hexdigest(),
        "title": meta["title"],
        "nav_label": meta["nav_label"],
        "description": meta["description"],
        "sections": sections_of(body),
        "body_html": body,
    }
    dest = ROOT / "pages" / code / f"{slug}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"  ok   {code}/{slug}")
    return True


def main() -> None:
    only_code = sys.argv[1] if len(sys.argv) > 1 else None
    only_slug = sys.argv[2] if len(sys.argv) > 2 else None
    failed = 0
    packed = 0
    for lang_dir in sorted(WORK.iterdir()):
        if not lang_dir.is_dir():
            continue
        code = lang_dir.name
        if only_code and code != only_code:
            continue
        for path in sorted(lang_dir.glob("*.html")):
            if only_slug and path.stem != only_slug:
                continue
            if pack_one(code, path):
                packed += 1
            else:
                failed += 1
    print(f"packed {packed}, failed {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

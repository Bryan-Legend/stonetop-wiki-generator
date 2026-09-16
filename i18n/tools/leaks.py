"""Find English that reached a localized page.

    python i18n/tools/leaks.py <code> <slug> [<slug> …]

Reads the built page out of ``Stonetop_Wiki/<code>/<slug>.html``, strips the
markup, and prints any text fragment that still reads as English (two or
more common English function words). The build's ``not shown whole`` report
says a *translation* went unused; this says the opposite and more useful
thing — what a reader of that language would actually see in English.

Known false positives, all correct as they stand: the titles of real books
and games (the mediography, *Sagas of the Icelanders*), a localized name
carrying its English in parentheses on first mention, and roll-table heads
the extractor title-cases on the English page too.
"""
import html as H
import re
import sys
import time

EN = re.compile(
    r"\b(the|you|your|and|of|with|when|roll|damage|mark|which|their|them|"
    r"they|is|are|to)\b",
    re.I,
)


def leaks(code: str, slug: str) -> list[str]:
    path = f"Stonetop_Wiki/{code}/{slug}.html"
    page = open(path, encoding="utf-8").read()
    body = page[page.find("<main"):page.find("</main>")]
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", body, flags=re.S)
    frags = [H.unescape(x).strip() for x in re.split(r"<[^>]+>", body)]
    return [f for f in frags if len(f) > 3 and len(EN.findall(f)) >= 2]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    t0 = time.perf_counter()
    code, slugs = sys.argv[1], sys.argv[2:]
    total = 0
    for slug in slugs:
        found = leaks(code, slug)
        total += len(found)
        print(slug, len(found))
        for f in found[:6]:
            print("   ", f[:150])
    ms = (time.perf_counter() - t0) * 1000
    print(f"{len(slugs)} page(s), {total} suspect fragment(s)  ({ms:.0f} ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

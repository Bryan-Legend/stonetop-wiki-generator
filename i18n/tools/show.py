"""Print an English work file for reading, one reference to a line.

    python i18n/tools/show.py <slug> [<slug> …] | sed -n '1,115p'

Drops the lines an arcanum's card never shows (the stray ``front`` /
``back``, the "Moves" heading, the "Mysteries of…" head), which a translator
leaves out of the work file — so what this prints is what comes back.
"""
import sys
import time

SKIP = ("front", "back", "moves")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    t0 = time.perf_counter()
    slugs = sys.argv[1:]
    n = 0
    for slug in slugs:
        print("=== " + slug)
        for line in open(f"i18n/_work/corpus/{slug}.work.txt", encoding="utf-8"):
            if line.startswith("#") or line.startswith("META"):
                continue
            ref, tag, *rest = line.rstrip("\n").split("\t")
            text = "\t".join(rest)
            if text.strip().lower() in SKIP or text.startswith("Mysteries of"):
                continue
            print(f"{ref}\t{tag}\t{text}")
            n += 1
    ms = (time.perf_counter() - t0) * 1000
    print(f"# {n} line(s) across {len(slugs)} slug(s)  ({ms:.0f} ms)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

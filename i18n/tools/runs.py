"""Check a translated work file against the English one it was made from.

    python i18n/tools/runs.py <slug> [<code>]        # default code: pt-BR

Reports, per reference, every line the translation is missing and every
line whose ``<b>`` / ``<i>`` run counts disagree with the English — the one
thing ``apply`` refuses to write a corpus file over. Quote both sides so the
fix is obvious without opening either file.

This is meant to replace counting formatting runs by eye, which is slow and
worse at it. Run it after every chunk; it costs milliseconds.
"""
import io
import sys
import time


def load(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in io.open(path, encoding="utf-8"):
        if line.startswith("#") or line.startswith("META"):
            continue
        fields = line.rstrip("\n").split("\t")
        if len(fields) >= 3:
            out[fields[0]] = "\t".join(fields[2:])
    return out


def main() -> int:
    t0 = time.perf_counter()
    slug = sys.argv[1]
    code = sys.argv[2] if len(sys.argv) > 2 else "pt-BR"
    en = load(f"i18n/_work/corpus/{slug}.work.txt")
    tr = load(f"i18n/_work/corpus/{code}/{slug}.work.txt")

    missing = [r for r in en if r not in tr]
    if missing:
        shown = ", ".join(missing[:25]) + (" …" if len(missing) > 25 else "")
        print(f"missing {len(missing)}: {shown}")

    bad = 0
    for ref, english in en.items():
        if ref not in tr:
            continue
        for tag in ("i", "b"):
            a, b = english.count(f"<{tag}>"), tr[ref].count(f"<{tag}>")
            if a != b:
                bad += 1
                print(f"{ref} <{tag}>: English {a}, {code} {b}")
                print(f"   EN: {english[:150]}")
                print(f"   {code}: {tr[ref][:150]}")

    ms = (time.perf_counter() - t0) * 1000
    done = len(en) - len(missing)
    status = "OK" if not (missing or bad) else f"{bad} mismatch(es)"
    print(f"{slug} [{code}]: {done}/{len(en)} refs, runs {status}  ({ms:.0f} ms)")
    return 1 if (missing or bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())

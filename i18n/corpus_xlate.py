"""Translate a page through its corpus text (see generator/translate.py).

    python i18n/corpus_xlate.py extract <slug> [--book-lines A-B] [--book-only]
        → i18n/_work/corpus/<slug>.work.txt — the page's translatable text,
          one line per corpus line. Copy it to i18n/_work/corpus/<code>/
          and translate the text after the tag.

    python i18n/corpus_xlate.py apply <code> <slug>
        → i18n/corpus/<code>/<book>/<slug>.txt and/or …/pages/<slug>.txt,
          the English skeleton with the translated text folded in. Reports
          any work line it could not place, and the coverage.

    python i18n/corpus_xlate.py check <code> [<slug>]
        → alignment and coverage of what is already applied.

    python i18n/corpus_xlate.py realign <code> <slug> [--old HEAD]
        → after a re-extraction moved the page's lines: re-keys the applied
          translation to the English as it is now, matching each line by its
          English text in the old English (``--old``, a git revision), writes
          the work file afresh and applies it. Lines the extractor changed
          are listed — they are English until translated in the work file.

A work line is ``ref TAB tag TAB text [TAB text…]``; ``S12`` is line 12 of
pages/<slug>.txt, ``B140`` line 140 of extracted/<book>/<slug>.txt. The
``META`` lines carry the page's title, sidebar label and description.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from generator.corpus import parse_text  # noqa: E402
from generator.translate import (  # noqa: E402
    META_KEYS,
    STATS_TEXT_KEYS,
    _file_lines,
    _split_file_line,
    _stats_block,
    apply_work,
    check_alignment,
    coverage,
    english_sources,
    extract_work,
    load_corpus_translations,
    read_meta,
    text_field_indexes,
    translation_paths,
    work_dir,
)


def cmd_extract(args: argparse.Namespace) -> int:
    rng = None
    if args.book_lines:
        a, b = args.book_lines.split("-")
        rng = (int(a), int(b))
    text = extract_work(args.slug, book_range=rng, book_only=args.book_only)
    out = work_dir() / f"{args.slug}.work.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    n = sum(1 for ln in text.split("\n") if ln and not ln.startswith(("#", "META")))
    print(f"{out}: {n} lines to translate")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    src = work_dir() / args.code / f"{args.slug}.work.txt"
    if not src.is_file():
        print(f"no work file at {src}")
        return 1
    files, meta, problems = apply_work(args.slug, args.code, src.read_text(encoding="utf-8"))
    for pr in problems:
        print("  problem:", pr)
    paths = translation_paths(args.code, args.slug)
    for kind, text in files.items():
        path = paths[kind]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(ROOT)}")
    for k in ("title", "nav_label", "description"):
        if not meta.get(k):
            print(f"  note: META {k} is empty")
    return cmd_check(argparse.Namespace(code=args.code, slug=args.slug))


def cmd_check(args: argparse.Namespace) -> int:
    trs = load_corpus_translations(args.code)
    if args.slug:
        trs = {k: v for k, v in trs.items() if k == args.slug}
    if not trs:
        print(f"{args.code}: nothing translated")
        return 1
    bad = 0
    for slug, entry in sorted(trs.items()):
        src = english_sources(slug)
        for kind in ("book", "sheet"):
            if kind not in entry:
                continue
            if kind not in src:
                print(f"  {args.code}/{slug} ({kind}): no English source — orphan")
                bad += 1
                continue
            en_lines, _ = parse_text(src[kind].read_text(encoding="utf-8"), str(src[kind]))
            probs = check_alignment(en_lines, entry[kind][0], f"{args.code}/{slug} ({kind})")
            for pr in probs[:10]:
                print("  ", pr)
            bad += len(probs)
            done, total = coverage(en_lines, entry[kind][0], sheet=(kind == "sheet"))
            print(f"  {args.code}/{slug} ({kind}): {done}/{total} lines translated"
                  + ("" if not probs else f", {len(probs)} alignment problem(s)"))
        meta = entry.get("meta") or {}
        missing = [k for k in ("title", "nav_label", "description") if not meta.get(k)]
        if missing:
            print(f"  {args.code}/{slug}: meta missing {', '.join(missing)}")
    return 1 if bad else 0


# ---------------------------------------------------------------- splits

def _plain(text: str) -> str:
    """Letters and digits only: how a split is recognised whatever the
    whitespace between its pieces."""
    import re
    return re.sub(r"[\W_]+", "", re.sub(r"<[^>]+>", "", text)).lower()


def _kind(ch: str) -> str:
    """The shape of the character a piece opens with."""
    if not ch:
        return ""
    if ch.isupper() or ch in "\"'“‘«「(『【":
        return "open"  # a capital, or an opening quote/bracket
    if ch.islower():
        return "lower"
    if ch.isdigit():
        return "digit"
    if "\u2e80" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af" or "\uf900" <= ch <= "\ufaff":
        return "cjk"  # a script without capitals: it can open any piece
    return "other"


def split_translation(en_pieces: list[str], tr: str) -> list[str] | None:
    """Cut one translated line into as many pieces as the English split into.

    The extractor used to glue a table's last row to the note under it, a
    column label to the row above it, a list item to the paragraph after it
    — one line, where the book has two. Translators kept that shape, and the
    seam is a single space in every language so far ("6 Evidentes e
    intactas Glifos …", "6 明显且完好 “隐藏”的符记…"). So the cut goes at a
    space: the one whose place in the line is nearest the English seam's,
    preferring one where the next word opens the way the English piece
    after the seam does (a capital or a quote, or a lowercase label).
    Returns ``None`` when the line has no space to cut at, or two are too
    close to call — that line is then reported and left in English.
    """
    if len(en_pieces) < 2:
        return [tr]
    tr = tr.strip()
    total = sum(len(e) for e in en_pieces) + len(en_pieces) - 1
    want = (len(en_pieces[0]) + 1) / total
    nxt = _kind(en_pieces[1].lstrip()[:1])
    # Where a seam can fall: at a space, or — in a script that sets no space
    # between sentences — just before an opening bracket or quote
    # ("…的存在/事物（page 264）。但…").
    openers = "(\uff08\u201c\u300c\u300e\u3010"
    cands = [
        (i, i + 1) for i, ch in enumerate(tr)
        if ch == " " and 0 < i < len(tr) - 1
    ] + [
        (i, i) for i, ch in enumerate(tr)
        if ch in openers and 0 < i and tr[i - 1] != " "
        and "\u2e80" <= tr[i - 1] <= "\u9fff"
    ]
    # never inside a table row's own number ("6 Evidentes")
    cands = [
        (i, t) for i, t in cands
        if not tr[:i].strip().replace("-", "").replace("\u2013", "").isdigit()
    ]
    if not cands:
        return None
    def runs(t: str) -> tuple[int, int]:
        return t.count("<b>"), t.count("<i>")

    def closed(t: str) -> bool:
        return t.count("<b>") == t.count("</b>") and t.count("<i>") == t.count("</i>")

    def depth(t: str) -> int:
        """Brackets left open, ASCII and full-width alike."""
        return (t.count("(") + t.count("\uff08")) - (t.count(")") + t.count("\uff09"))

    # The book's own formatting is the surest guide to the seam: each piece
    # carries the bold and italic runs its English does, and no cut falls
    # inside a run (the build checks both, and refuses a page that fails).
    en_runs = runs(en_pieces[0])
    en_depth = depth(en_pieces[0])
    scored = []
    for i, tail_at in cands:
        head = tr[:i]
        if not closed(head):
            continue
        if runs(head) != en_runs:
            continue  # the book's formatting says this is not the seam
        if depth(head) != en_depth:
            continue  # nor inside a bracket the English piece does not open
        cost = abs(i / len(tr) - want)
        got = _kind(tr[tail_at])
        if got != nxt and got != "cjk":
            cost += 0.25
        scored.append((cost, i, tail_at))
    if not scored:
        return None
    scored.sort()
    best_cost, best, tail_at = scored[0]
    if len(scored) > 1 and scored[1][0] - best_cost < 0.02 and abs(scored[1][1] - best) > 3:
        return None  # two seams equally likely: do not guess
    head, tail = tr[:best].rstrip(), tr[tail_at:].strip()
    if len(en_pieces) == 2 and runs(tail) != runs(en_pieces[1]):
        return None
    rest = split_translation(en_pieces[1:], tail)
    if rest is None:
        return None
    return [head] + rest


def cmd_realign(args: argparse.Namespace) -> int:
    """Re-key an applied translation to the English text as it is now."""
    import subprocess

    src = english_sources(args.slug)
    trp = translation_paths(args.code, args.slug)
    if not src:
        print(f"{args.slug}: no English source under extracted/ or pages/")
        return 1
    meta: dict[str, str] = {}
    work: list[str] = []
    changed: list[str] = []
    split_report: list[tuple[str, list[str]]] = []
    trunc_report: list[tuple[str, str]] = []
    unsplit: list[str] = []
    for kind, prefix in (("sheet", "S"), ("book", "B")):
        if kind not in src or kind not in trp or not trp[kind].is_file():
            continue
        tr_text = trp[kind].read_text(encoding="utf-8")
        meta.update({k: v for k, v in read_meta(tr_text).items() if k in META_KEYS})
        new_text = src[kind].read_text(encoding="utf-8")
        rel = src[kind].relative_to(ROOT).as_posix()
        try:
            old_text = subprocess.run(
                ["git", "show", f"{args.old}:{rel}"],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
            ).stdout
        except (subprocess.CalledProcessError, OSError):
            old_text = new_text
        old_body = [ln for ln in old_text.split("\n") if ln and not ln.startswith("#")]
        tr_body = [ln for ln in tr_text.split("\n") if ln and not ln.startswith("#")]
        if len(old_body) != len(tr_body):
            print(
                f"  {kind}: the translation has {len(tr_body)} lines, the English at "
                f"{args.old} {len(old_body)}; they never aligned, nothing to carry"
            )
            return 1
        # Every old English line, with its translation(s) in page order.
        pool: dict[str, list[str]] = {}
        for en_raw, tr_raw in zip(old_body, tr_body):
            pool.setdefault(en_raw, []).append(tr_raw)
        sheet = kind == "sheet"
        # A line the extractor now splits in two (or more): its translation
        # is cut at the matching seam, so the pieces keep their translation.
        new_body = [
            ln for ln in _file_lines(new_text)
            if ln and not ln.startswith("#") and not ln.startswith("PAGE\t")
        ]
        new_set = set(new_body)
        split_tr: dict[int, str] = {}
        for en_raw, tr_raw in zip(old_body, tr_body):
            if en_raw in new_set or en_raw.startswith("PAGE\t"):
                continue
            _tag, parts = _split_file_line(en_raw)
            if len(parts) != 1:
                continue
            want = _plain(parts[0])
            for j, ln in enumerate(new_body):
                if j in split_tr or ln in pool:
                    continue
                _t, p0 = _split_file_line(ln)
                if len(p0) != 1 or not _plain(p0[0]) or not want.startswith(_plain(p0[0])):
                    continue
                pieces, acc, k = [p0[0]], _plain(p0[0]), j + 1
                at = [j]
                while len(acc) < len(want) and k < len(new_body):
                    if new_body[k] == "PB":  # the break the extractor now marks
                        k += 1
                        continue
                    _t2, pk = _split_file_line(new_body[k])
                    if len(pk) != 1:
                        break
                    pieces.append(pk[0])
                    at.append(k)
                    acc += _plain(pk[0])
                    k += 1
                if acc != want or len(pieces) < 2:
                    # Not a split — but the line may have been cut short, the
                    # rest of it moved elsewhere (a stray half-line the
                    # extractor used to append to a table's last row). Keep
                    # the translation up to the matching seam.
                    head_plain = _plain(p0[0])
                    if len(head_plain) < len(want):
                        seen = 0
                        for cut_at, ch in enumerate(parts[0]):
                            if seen == len(head_plain):
                                break
                            if _plain(ch):
                                seen += 1
                        rest_en = parts[0][cut_at:].strip()
                        _tt, tparts = _split_file_line(tr_raw)
                        cut = split_translation(
                            [p0[0], rest_en], tparts[0] if tparts else ""
                        )
                        if cut is not None:
                            split_tr[j] = cut[0]
                            trunc_report.append((p0[0], cut[0]))
                            break
                    continue
                _tt, tparts = _split_file_line(tr_raw)
                cut = split_translation(pieces, tparts[0] if tparts else "")
                if cut is None:
                    unsplit.append(parts[0])
                    break
                for pos, piece in zip(at, cut):
                    split_tr[pos] = piece
                split_report.append((parts[0], cut))
                break
        body_pos: dict[int, int] = {}
        _b = 0
        for _n, _raw in enumerate(_file_lines(new_text), 1):
            if not _raw or _raw.startswith("#") or _raw.startswith("PAGE\t"):
                continue
            body_pos[_n] = _b
            _b += 1
        for n, raw in enumerate(_file_lines(new_text), 1):
            if not raw or raw.startswith("#") or raw.startswith("PAGE\t"):
                continue
            tag, parts = _split_file_line(raw)
            idx = text_field_indexes(tag, parts, sheet=sheet)
            if not idx:
                continue
            cands = pool.get(raw)
            if not cands:
                pos = body_pos.get(n)
                if pos is not None and pos in split_tr and len(idx) == 1:
                    work.append(f"{prefix}{n}\t{tag}\t{split_tr[pos]}")
                    continue
                changed.append(f"{prefix}{n}\t{tag}\t" + "\t".join(parts[i] for i in idx))
                continue
            _tr_tag, tr_parts = _split_file_line(cands.pop(0))
            if not sheet and tag == "STATS":
                block = _stats_block(tr_parts[0]) if tr_parts else {}
                work.append(f"{prefix}{n}\t{tag}\t" + "\t".join(
                    str(block.get(k) or "") for k in STATS_TEXT_KEYS))
                continue
            work.append(f"{prefix}{n}\t{tag}\t" + "\t".join(
                tr_parts[i] if i < len(tr_parts) else parts[i] for i in idx))
    out = work_dir() / args.code / f"{args.slug}.work.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    head = [
        f"# {args.slug} — work file, re-keyed by corpus_xlate.py realign against "
        f"the English at {args.old}.",
        "# Fields are tab-separated. A line with two text fields must come back with two.",
    ] + [f"META\t{k}\t{meta.get(k, '')}" for k in META_KEYS]
    out.write_text("\n".join(head + work) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out.relative_to(ROOT)}: {len(work)} lines carried over")
    _enc = sys.stdout.encoding or "utf-8"

    def _say(t: str) -> str:
        t = t if len(t) <= 100 else t[:97] + "..."
        return t.encode(_enc, "replace").decode(_enc)

    if split_report:
        print(f"  {len(split_report)} line(s) the extractor split, their translation cut to match:")
        for _en, cut in split_report:
            print("    " + _say(" | ".join(cut)))
    if trunc_report:
        print(f"  {len(trunc_report)} line(s) the extractor cut short, their translation cut to match:")
        for _en, tr in trunc_report:
            print("    " + _say(tr))
    if unsplit:
        print(f"  {len(unsplit)} split line(s) whose translation had no clear seam (left English):")
        for en in unsplit:
            print("    " + _say(en))
    if changed:
        print(f"  {len(changed)} line(s) the extractor changed stay English until translated:")
        enc = sys.stdout.encoding or "utf-8"
        for ln in changed:
            ln = ln if len(ln) <= 110 else ln[:107] + "..."
            print("    " + ln.encode(enc, "replace").decode(enc))
    return cmd_apply(argparse.Namespace(code=args.code, slug=args.slug))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("slug")
    e.add_argument("--book-lines", help="physical line range of the corpus file, e.g. 98-283")
    e.add_argument("--book-only", action="store_true")
    e.set_defaults(fn=cmd_extract)
    a = sub.add_parser("apply")
    a.add_argument("code")
    a.add_argument("slug")
    a.set_defaults(fn=cmd_apply)
    c = sub.add_parser("check")
    c.add_argument("code")
    c.add_argument("slug", nargs="?")
    c.set_defaults(fn=cmd_check)
    r = sub.add_parser("realign")
    r.add_argument("code")
    r.add_argument("slug")
    r.add_argument("--old", default="HEAD", help="git revision holding the English the translation aligns with")
    r.set_defaults(fn=cmd_realign)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

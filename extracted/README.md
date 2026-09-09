# The books' text, extracted

One plain-text file per wiki article, extracted from the Book I and Book II
1-up PDFs by the generator, plus one `articles.json` per book listing the
articles in build order. The wiki is built **from these files**, not from
the PDFs, so a checkout of this repository builds the whole wiki without
them. The text is CC BY-SA 4.0 (see the copyright page of either book);
the PDFs themselves are not redistributed.

```
extracted/
  book1/
    articles.json      the book's article list: titles, slugs, page ranges, kinds
    harm-healing.txt   one file per article, named by slug
    the-ranger.txt
  book2/
    ...
```

## Format

One line per extracted *marker line*: `TAG`, a tab, then the payload.

```
# Harm & Healing · Book I, pp. 237–250
PAGE	238
P	Player characters and their followers often find themselves in danger …
H2	Hit points and damage
B	Damage to their Hit Points (HP)
VR	Bronze knife	2
BOX
```

| Tag | Meaning |
|---|---|
| `P` | body text |
| `H2`, `H3`, `H4` | headings (a plaqued heading is `H2\t4 & 5\tNPC connections`) |
| `B`, `B2`, `Q`, `E`, `BC` | spiral bullet, nested bullet, question bullet, ellipsis item, a list item's further paragraph |
| `C`, `C2`, `CX`, `CX2` | checkbox item; indented; printed already ticked; both |
| `TH`, `VT`, `VR`, `VA`, `VF` | a Fell Type header; a value table's title, row (`item<tab>value`), row continuation, footnote |
| `BOX` … `ENDBOX` | a chapter's steading info box |
| `HR`, `MARK`, `ICON` | rule; progress-mark track (count); category icon (path) |
| `STATS`, `WRITE`, `STEP` | a playbook's stat block (JSON), write-in box, numbered step |
| `FACE` | which face of a major arcanum card follows (`front` / `back`) |
| `PAGE` | the PDF page the lines below sit on (not a marker; not part of the text) |

Inside a payload, bold is `<b>…</b>` and italic `<i>…</i>`; a literal
backslash, tab, newline or `<` is escaped `\\`, `\t`, `\n`, `\<`. Nothing
else is escaped. A tab inside a payload separates its sub-fields. A marker
that takes a payload always has its tab, even when empty; a bare marker
(`BOX`, `HR`) never does. Lines opening `#` and blank lines are ignored.

The full registry of markers is `MARKERS` in `generator/text.py`; the
reader and writer are `generator/corpus.py`, and `tests/test_corpus.py`
pins the round trip.

## These files are generated

```
python stonetop-wiki-generator.py --extract --input <folder with the PDFs>
```

rewrites every file here from the PDFs (and removes any an article no
longer names). A fix to the text belongs in the extractor, not in these
files: a hand edit is undone by the next extraction and shows up as a diff.
That diff is the point — after a change to the extractor, `git diff
extracted/` shows exactly which lines moved.

A translation is the same file with the same tag skeleton, checked line
for line against the English: `i18n/corpus/<code>/<book>/<slug>.txt`, made
with `i18n/corpus_xlate.py` (see `i18n/README.md`). The hand-authored sheets
under `pages/` use the same format with a few markers of their own
(`generator/sheet.py`).

# Stonetop Wiki Generator

Generate a **static, hyperlinked wiki** from the *Stonetop* PDFs.

**Read it online: <https://stonetop-wiki.github.io/>**

The wiki includes:

- Articles (moves, places, peoples, powers, …)
- Minor & major arcana as interactive cards (checkboxes for unlocks / progress / consequences)
- Full-text search, hover previews, and dice rollers
- Deep links between page references and monster/stat blocks
- Adventure sites under `Stonetop_Wiki/sites/` with dice rollers, hp trackers, & deep linking rich popups

> **The book text in this repository is published under CC BY-SA 4.0** — both books state
> *"All text herein is released under a CC BY-SA 4.0 license."*
>
> **Artwork is not.** The same page states *"All artwork herein is © 2026 by Lucie Arnoux."*
> Maps are artwork, so builds omit them by default and no illustration is committed here.
> The PDFs themselves are not redistributed — get them from
> [the official Stonetop store](https://plusoneexp.com/collections/stonetop).

## Requirements

- **Python 3.10+** (3.11+ recommended)
- To **build the wiki**: nothing else. The books' text is checked in under
  [`extracted/`](extracted/README.md), one plain-text file per article, and the
  wiki is built from that.
- To **re-extract the text**: the book **1-up** PDFs (2nd printing works well) and
  PyMuPDF (`pip install -r requirements.txt`).

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/Bryan-Legend/stonetop-wiki-generator.git
cd stonetop-wiki-generator

python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt   # PyMuPDF — only needed to extract from the PDFs
```

### 2. Build

```bash
python stonetop-wiki-generator.py
```

That reads `extracted/` and writes the wiki into `Stonetop_Wiki/` in about ten
seconds. No PDF is opened.

To **re-extract the text** — after a change to the extractor, or a new printing —
put the 1-up PDFs in an input folder (optional `Maps/` subfolder for campaign map
sheets), then:

```bash
python stonetop-wiki-generator.py --extract --input /path/to/folder-with-pdfs
```

Extraction takes about a minute and rewrites `extracted/`; `git diff extracted/`
then shows exactly which lines of text changed, before any HTML is looked at.

| Flag | Meaning | Default |
|------|---------|---------|
| `-o` / `--output` | Wiki folder. Chrome and sites stay in place; only book-derived files are written. | `Stonetop_Wiki/` |
| `--corpus DIR` | The extracted text to build from (and to write when extracting). | `extracted/` |
| `--extract` | Re-extract from the PDFs into the corpus, then build. Without it a PDF is only opened for a book the corpus lacks. | off |
| `--extract-only` | Extract and stop; write no wiki. | off |
| `-i` / `--input` | Folder containing the 1-up book PDFs. Optional: `Maps/`. Only read when extracting. | current working directory |
| `--books book1 book2` | Limit the run to the listed books (faster while iterating). | every book in the corpus |
| `--langs de fr ja` | Build only these translations (`none` for English only). See [Languages](#languages). | every language with a translated page |
| `--maps` | Include the Maps page and its images (needs the Book II PDF). **Local builds only** — map art is © Lucie Arnoux, not CC BY-SA. | off |

`python -m generator` is the same entry point.

### 3. Open it

```text
Stonetop_Wiki/index.html
```

Or serve locally (avoids some `file://` restrictions):

```bash
cd Stonetop_Wiki
python -m http.server 8000
# then visit http://localhost:8000
```
## How it is put together

```text
stonetop-wiki-generator.py   entry point (python -m generator is the same)
generator/                   the package
  text.py        markers, inline-format sentinels, line classifiers — shared by both phases
  extract.py     PDF → marker lines (the only module that needs PyMuPDF)
  articles.py    the BOOKS table, PDF outline → article list, chapter splits, arcana numbering
  corpus.py      marker lines ↔ extracted/ (the on-disk format, documented in the module)
  structure.py   marker lines → article HTML: headings, tables, stat blocks, playbook sheets, links
  arcana.py      marker lines → arcana card HTML
  chrome.py      page shell, sidebar, hub pages, pages/ overrides, home page, sitemap
  i18n.py        translations as data (i18n/)
  sites.py       adventure-site sheets under Stonetop_Wiki/sites/
  build.py       command line and the two phases
extracted/                   the books' text, one file per article — see extracted/README.md
tests/                       python -m unittest discover -s tests
```

**Two phases.** *Extract* reads a 1-up PDF's span fonts and vector drawings and
emits *marker lines* — plain strings, each opening with a marker for its role
(heading, bullet, checkbox, value-table row …) and carrying bold and italic
inline. *Build* turns those lines into HTML. The marker lines are written to
`extracted/` between the two, in a tab-separated text format made to be read,
diffed, and translated, so the second phase never needs the PDFs.

## Languages

The wiki is published in English plus twenty more languages, each in its own
directory under the wiki root:

```text
/welcome-to-the-worlds-end.html        English
/de/welcome-to-the-worlds-end.html     German
/ja/welcome-to-the-worlds-end.html     Japanese
```

Translations are checked-in data under [`i18n/`](i18n/README.md) — not
something the build re-derives — so a rebuild never disturbs one. Each page
gets a self-canonical, a reciprocal `hreflang` cluster with `x-default`,
translated chrome and metadata, and a crawlable language switcher in the
sidebar; nothing anywhere redirects on `Accept-Language`. Slugs and section
ids stay English in every language, which keeps deep links portable and keeps
a reader's ticked checkboxes shared between a page and its translations.

Pages with no translation yet stay English in that language's sidebar, marked
`EN`, and link back up to the English page.

Adding a page or a language: **[`i18n/README.md`](i18n/README.md)**. What is
translated and what never is: **[`i18n/GLOSSARY.md`](i18n/GLOSSARY.md)**.

## Sites

Drop HTML sheets in `Stonetop_Wiki/sites/` (or a subfolder of variants). Each build:

- lists them in a **Sites** group at the foot of the sidebar and home page
- builds a Sites hub and full-text search entries

Sheets should link wiki pages as `../<slug>.html` and set `data-wiki-root="../"`. Shared chrome: `site.css` and `site.js` in the same folder.

### Included play-tested sites:

- **[Vasilya’s Grove](https://stonetop-wiki.github.io/sites/Vasilyas-Grove.html)**
- **[Underfalls](https://stonetop-wiki.github.io/sites/Underfalls.html)**

## License

**Generator code** and the wiki chrome (CSS/JS/templates) are MIT — see [LICENSE](LICENSE).

**Book text** (the `<slug>.html` pages at the wiki root, plus the generated index and search
data) is from *Stonetop* and *Stonetop: The Wider World and Other Wonders*, written by
Jeremy Strandberg and published by Lampblack & Brimstone. Both books' copyright pages
(second printing, July 2026) state:

> All text herein is released under a CC BY-SA 4.0 license.
> Some concepts and procedures are derived from Dungeon World, by Sage LaTorra & Adam Koebel,
> released under a CC BY license.
> All artwork herein is © 2026 by Lucie Arnoux.

That text is reproduced here under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), reflowed from the PDFs into
HTML, and **this edition is shared under the same license**. Every generated page carries the
attribution and license link in its footer.

**Artwork is excluded.** Illustrations and maps remain © 2026 Lucie Arnoux and are not
redistributable, so `--maps` is off by default and `Stonetop_Wiki/images/maps/` is
gitignored. The only images shipped are category icons from
[game-icons.net](https://game-icons.net) (CC BY 3.0). The source PDFs are not redistributed.

Not affiliated with or endorsed by Lampblack & Brimstone.

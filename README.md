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
- Book **1-up** PDFs (2nd printing works well).

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

pip install -r requirements.txt
```

### 2. Point at your PDFs and build

Put the 1-up PDFs in an input folder (optional `Maps/` subfolder for campaign map sheets), then:

```bash
python stonetop-wiki-generator.py --input /path/to/folder-with-pdfs
```

| Flag | Meaning | Default |
|------|---------|---------|
| `-i` / `--input` | Folder containing the 1-up book PDFs. Optional: `Maps/`. | current working directory |
| `-o` / `--output` | Wiki folder. Chrome and sites stay in place; only book-derived files are written. | `Stonetop_Wiki/` |
| `--books book1 book2` | Build only the listed books (faster while iterating). | every book PDF found |
| `--langs de fr ja` | Build only these translations (`none` for English only). See [Languages](#languages). | every language with a translated page |
| `--maps` | Include the Maps page and its images. **Local builds only** — map art is © Lucie Arnoux, not CC BY-SA. | off |

```bash
# From a folder that holds the PDFs:
python /path/to/stonetop-wiki-generator/stonetop-wiki-generator.py --input .
```

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

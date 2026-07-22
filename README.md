# Stonetop Wiki Generator

Generate a **static, offline wiki** from the *Stonetop* Book II PDF (*The Wider World and Other Wonders*).

The wiki includes:

- Gazetteer pages (places, peoples, powers)
- Minor & major arcana as interactive cards (checkboxes for unlocks / progress / consequences)
- Full-text search, hover previews, and dice rollers
- Deep links between page references and monster/stat blocks

> **This repository does not include the Stonetop PDFs, artwork, or a pre-built wiki.**  
> You need a legal copy of the Book II **1-up** PDF from [the official Stonetop store](https://plusoneexp.com/collections/stonetop).

## Screenshots

![Wiki screenshot — multi-column gazetteer](docs/wiki-screenshot-1.png)

![Wiki screenshot — arcana and navigation](docs/wiki-screenshot-2.png)

## Requirements

- **Python 3.10+** (3.11+ recommended)
- The Book II PDF (**1-up**, 2nd printing works well):

  ```
  Book_II_-_The_Wider_World_and_Other_Wonders_(1-up)_-_2nd_printing.pdf
  ```

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

### 2. Point at your PDF and build

Put the Book II **1-up** PDF in some input folder (filename must match exactly), then:

```bash
python stonetop-wiki-generator.py --input /path/to/folder-with-pdf --output /path/to/Stonetop_Wiki
```

| Flag | Meaning | Default |
|------|---------|---------|
| `-i` / `--input` | Folder containing the Book II 1-up PDF. Optional subfolder: `Maps/` (campaign sheets). | current working directory |
| `-o` / `--output` | Folder to write the static wiki into. | `<input>/Stonetop_Wiki` |

Example layout:

```text
my-stonetop-stuff/
  Book_II_-_The_Wider_World_and_Other_Wonders_(1-up)_-_2nd_printing.pdf
  Maps/                          # optional campaign map sheets
  Stonetop_Wiki/                 # generated output
```

```bash
python stonetop-wiki-generator.py --input my-stonetop-stuff --output my-stonetop-stuff/Stonetop_Wiki
# or, from my-stonetop-stuff:
python /path/to/stonetop-wiki-generator/stonetop-wiki-generator.py --input . --output Stonetop_Wiki
```

### 3. Open it

Open in a browser:

```text
Stonetop_Wiki/index.html
```

Or serve locally (avoids some `file://` restrictions):

```bash
# Python
cd Stonetop_Wiki
python -m http.server 8000
# then visit http://localhost:8000
```

## Optional: campaign map sheets

If you own the separate campaign map images, put them under `Maps/` inside the **input** folder (any nesting is fine).

```text
Maps/
  Map 1 - Stonetop.jpg
  Map 2 - Vicinity.jpg
  …
```

## What gets generated

| Path | Contents |
|------|----------|
| `Stonetop_Wiki/index.html` | Home / topic index |
| `Stonetop_Wiki/pages/*.html` | One page per article / arcanum |
| `Stonetop_Wiki/css/wiki.css` | Styles |
| `Stonetop_Wiki/js/` | Search, checkboxes, dice, previews |
| `Stonetop_Wiki/images/maps/` | Map images |

Checkbox state for steading improvements and arcana is stored in your browser (`localStorage`).

## Project files

| File | Role |
|------|------|
| `stonetop-wiki-generator.py` | Single entry point — PDF extraction + static site build |
| `static/css/wiki.css` | Wiki styles (copied into the output on build) |
| `static/js/wiki.js` | Search, checkboxes, dice, previews (copied on build) |
| `requirements.txt` | Python dependencies |
| `docs/wiki-screenshot-*.png` | README screenshots |

## Troubleshooting

**`PDF not found`**  
Confirm `--input` points at the folder that holds the Book II 1-up PDF, and that the filename matches exactly:

```text
Book_II_-_The_Wider_World_and_Other_Wonders_(1-up)_-_2nd_printing.pdf
```

**Build is slow / uses a lot of RAM**  
Normal for a ~500-page PDF with map renders. Give it a minute.

**Search / previews don’t work over `file://`**  
Use a local static server (`python -m http.server`) as shown above.

## License

The **generator code** in this repository is MIT (see [LICENSE](LICENSE)).

*Stonetop* and its text/art are © their respective owners (Jeremy Strandberg / the Stonetop team). This project only helps **you** turn a PDF you own into a personal reference wiki. Do not redistribute the PDFs, the extracted text, or a built wiki that contains the book’s content.

## Credits

Built for table use with the *Stonetop* RPG. Not affiliated with the official Stonetop publishers.

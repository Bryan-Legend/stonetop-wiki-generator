# Localization

The wiki is published in English plus the twenty languages in `langs.json`.
Translations are **data, not output**: they are checked in here, keyed by page
slug, and the build lays them out under the wiki root. Nothing about a
translation is re-derived from the PDFs, so a rebuild never disturbs one.

```
i18n/
  langs.json           the twenty target languages (code, endonym, dir, og_locale)
  GLOSSARY.md          what is translated, what never is, and the fixed terms
  ui/<code>.json       chrome strings: nav, search box, footer, credit line
  pages/<code>/<slug>.json   one translated page
```

## Where the pages land

```
/welcome-to-the-worlds-end.html        English
/de/welcome-to-the-worlds-end.html     German
/ja/welcome-to-the-worlds-end.html     Japanese
```

**Subdirectories, one domain.** Not subdomains, not country domains: every
language then shares the authority the English pages have earned, and GitHub
Pages serves it with no configuration at all. Google treats all three URL
shapes as equally valid, so the tie-breaker is cost, and a directory costs
nothing.

**The slug is not translated**, and neither are section ids
(`#how-to-use-this-book`). One URL shape across the site; a deep link that
survives a language switch; and — because the wiki keys a reader's ticked
checkboxes and written answers by page slug — a steading improvement ticked on
the English page is still ticked on the German one.

This holds even where the prose is in another script: `/ja/marshedge.html`
reads マーシュエッジ throughout and still lives at the English path. Names and
paths are separate axes — see `GLOSSARY.md`, which does localize most names,
including into Cyrillic, kana, hangul and Chinese.

## What the build does with them

Per page, in every language that has it:

- `<html lang="de" dir="ltr">`, and `og:locale`.
- A **self-referential, reciprocal hreflang cluster** — every page in the set
  links to every other *and to itself*, plus `x-default` pointing at English.
  The English page carries the same cluster its translations do; a one-way set
  is ignored outright.
- A **self-canonical**. A translation that canonicalises to the English page
  is asking to be dropped from the index, and that is the usual way a
  localized site ends up invisible.
- Translated `<title>`, `meta description`, and Open Graph text.
- **Translated chrome**: the sidebar's search box, the skip link, the book
  labels, and the CC BY-SA credit line. A German body under an English shell
  reads as machine output to a person and scores like one.
- A **language switcher** in the sidebar footer: real `<a href>` links, always
  in the DOM.
- Sidebar entries for pages not yet translated stay in English, link back up to
  the English page, and are marked `EN`. Partial coverage is normal; a
  directory of pages that are secretly still English is not — that is the thin
  machine-translated content search engines discount.
- The localized URLs go into `sitemap.xml`.

**Nothing redirects on `Accept-Language`, ever.** Googlebot crawls from a US
address with `Accept-Language: en`; a site that redirects by header shows the
crawler nothing but English and its translations are never indexed. The reader
chooses, from the switcher.

## Adding a page

Write `pages/<code>/<slug>.json`:

```json
{
  "lang": "de",
  "slug": "welcome-to-the-worlds-end",
  "source_sha256": "ddff8c84…",
  "title": "Willkommen am Ende der Welt",
  "nav_label": "Willkommen am Ende der Welt",
  "description": "One sentence for search results and link previews.",
  "sections": { "how-to-use-this-book": "Wie du dieses Buch benutzt" },
  "body_html": "<p>…</p>\n<h2 id=\"how-to-use-this-book\">…</h2>\n<p>…</p>"
}
```

- `body_html` is the English page's body **without** its `<h1>` (the build adds
  one from `title`), keeping the same tags, the same `<h2 id>` values, and the
  same `<a class=\"wiki-link\" href=\"slug.html\" data-slug=\"slug\">` form.
  The build re-bases those hrefs: a link to a page translated into the same
  language stays a sibling, everything else gets `../`.
- `source_sha256` is the SHA-256 of the English body **as the build holds it**,
  `<h1>` included. Get it from a built page:

  ```bash
  python - <<'PY'
  import re, hashlib, pathlib
  t = pathlib.Path("Stonetop_Wiki/welcome-to-the-worlds-end.html").read_text(encoding="utf-8")
  body = re.search(r'<main class="content">\n {10}(.*?)\n {8}</main>', t, re.S).group(1)
  print(hashlib.sha256(body.encode()).hexdigest())
  PY
  ```

  When the books are re-extracted and that page's English text moves, the hash
  stops matching and the build prints the page under
  `i18n: de stale against the English text:` — which is the difference between
  a stale translation nobody noticed and one on a list to redo. It still
  publishes; it just says so.

Read `GLOSSARY.md` before translating anything. One rendering per term, per
language, everywhere — inconsistent terminology is what makes a translated
game wiki unusable.

## Adding a language

Add a row to `langs.json` (`code` is both the directory and the hreflang value,
so it must be a valid BCP-47 tag), write `ui/<code>.json`, and translate at
least one page. A language with no translated page is skipped entirely.

`dir` is carried per language, so an RTL language is a data edit rather than a
code change.

## Building

```bash
python stonetop-wiki-generator.py --input .            # every language
python stonetop-wiki-generator.py --input . --langs de fr ja
python stonetop-wiki-generator.py --input . --langs none   # English only
```

Each language directory is rewritten from scratch every build, and a language
dropped from `langs.json` has its directory removed.

## Licensing

The books' text is CC BY-SA 4.0, which permits translation as an adaptation.
Every translated page keeps the attribution and license notice (the sidebar
footer carries both, translated), and the translations are themselves
CC BY-SA 4.0.

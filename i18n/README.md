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
  corpus/<code>/<book>/<slug>.txt   one translated page: the book's text, same skeleton
  corpus/<code>/pages/<slug>.txt    …and its sheet, if it has one
  pages/<code>/<slug>.json   legacy: an HTML body; still published, never added to
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

## Translating a page through the corpus

The route to use for new work. The wiki is built from the books' extracted
text (`extracted/<book>/<slug>.txt`) and, for the fill-in sheets, from
`pages/<slug>.txt` — both marker files, `TAG<tab>payload` per line. A
translation is **the same file with its text in another language** and
everything else — tags, keys, ids, defaults, numbers — byte-identical:

```
i18n/corpus/<code>/book1/<slug>.txt     the book's text
i18n/corpus/<code>/pages/<slug>.txt     the sheet, if the page has one
```

Nobody writes those by hand. The tool produces a *work file* holding only
the translatable text, and folds the translated work file back over the
English skeleton, so the skeleton cannot drift:

```bash
python i18n/corpus_xlate.py extract <slug> [--book-lines A-B]
#   → i18n/_work/corpus/<slug>.work.txt
#     ref TAB tag TAB text [TAB text]     S12 = pages/<slug>.txt line 12
#                                          B140 = extracted/…/<slug>.txt line 140
#     META title / nav_label / description
#   --book-lines limits the book lines (the steading playbook's corpus holds
#   pages its sheet replaces; the improvements are lines 98–282).
#   Translate it into i18n/_work/corpus/<code>/<slug>.work.txt: same lines,
#   same order, same refs and tags; keep <b>/<i> around the same words and
#   [[slug#frag|label]] links (translate the label only).
python i18n/corpus_xlate.py apply <code> <slug>
#   writes the i18n/corpus files, reports any line it could not place, and
#   the coverage
python i18n/corpus_xlate.py check <code> [<slug>]
```

The build renders the page from the **English** lines — so every structural
decision is made on the text the generator was written for, and every id and
link stays English — and swaps each line for its translation where text
becomes HTML. A sheet renders from the translated lines with ids taken from
the English sheet. Per page the build prints `book N/M lines translated`,
`sheet N/M`, any translated line the renderer never asked for (English
leaked through, listed as `not shown whole`), and `stale` when the English
text changed since the translation was made (`source_sha256` in the file's
header). A corpus translation outranks a JSON page of the same slug. Arcana
pages cannot be rendered this way yet.

A class playbook's stat block is one `STATS` line of JSON; its work line
carries two text fields, the gloss and the HP label (`HP (max 18)`). What
every playbook prints alike — stat names, debilities, Damage/Armor/Level, the
Stats heading, the dice and roll tooltips, "You start with this" — is not in
any work file: it is translated once per language in `ui/<code>.json` under
`sheet`, and the renderer reads it through `UI()` in `generator/structure.py`.

## Strategy: depth before breadth

Nineteen of the twenty languages hold a handful of pages each — the shape a
site takes when you translate the *same* interesting pages into everything.
That is the worst shape to be in: twenty directories of mostly-English pages
is exactly the thin translated content search engines discount, and no reader
can use any of them to run a game.

**So: one language at a time, front to back.** Finish a language's Book II
(the setting articles, which are short and self-contained) and then its Book I
(the rules chapters, which are long), and only then start the next language.
A complete language is a site someone can play from; twenty partial ones are
twenty dead ends.

The order (Bryan, 2026-09-15): **pt-BR first**, then **zh-Hans**. Portuguese
because it is the largest under-served market for tabletop RPGs and shares the
Latin script, so nothing about names or layout needs rethinking; Simplified
Chinese next because it is the largest audience outright and the one where the
English pages help a reader least.

**pt-BR is complete** (2026-09-16): Book II 138/138, Book I 39/39, all six
sheets — 178 pages. A leak sweep over all of them turns up nothing but the
known false positives (real book and film titles in the mediography and
`first-adventure`, and a localized name carrying its English gloss).
Next: **zh-Hans**, same order — Book II first, then Book I.

### Working a long chapter

A Book I chapter runs 500–1400 work lines, which is more than one sitting.
The loop that holds up:

```bash
python i18n/corpus_xlate.py extract <slug>           # English work file
python i18n/tools/show.py <slug> | sed -n '1,115p'   # read a slice
#   translate the slice into i18n/_work/batch.txt
cat i18n/_work/batch.txt >> i18n/_work/corpus/pt-BR/<slug>.work.txt
python i18n/tools/runs.py <slug>                     # <1 s: missing refs, run counts
#   …repeat until the chapter is done, then:
python i18n/corpus_xlate.py apply pt-BR <slug>
python stonetop-wiki-generator.py                    # ~16 s, every language
python i18n/tools/leaks.py pt-BR <slug>              # expect: <slug> 0
```

**Let the tools do the checking.** The three helpers live in `i18n/tools/`
(tracked — `i18n/_work/` beside it is scratch and is not), and each prints
how long it took, so it is obvious when one stops being cheap:

| | |
|---|---|
| `tools/show.py <slug>` | the English work file, ready to read |
| `tools/runs.py <slug> [code]` | missing refs + `<b>`/`<i>` mismatches, both sides quoted |
| `tools/leaks.py <code> <slug>…` | English that reached the built page |

`runs.py` is the important one, and it runs in about a millisecond.
**Counting formatting runs by eye before writing a chunk is the single most
expensive thing in this loop, and the script does it better** — write the
chunk, run the script, fix what it names. Same for the rest: `apply` reports
coverage, the build reports `not shown whole`, `leaks.py` reports English
that reached the reader. Between them there is nothing left worth verifying
by hand.

**Read and write in big slices.** ~110 corpus lines a pass, not 40. Every
round trip costs a read, a write and an append; the translation itself is
the same work either way, so fewer, larger passes are strictly cheaper.

Three rules this loop exists to enforce:

- **Every line keeps the English line's `<b>` and `<i>` run counts exactly.**
  The book breaks a bold or italic passage at each typeset line, so one
  sentence often arrives as six `<i>…</i>` runs; the translation needs six
  too, split wherever Portuguese wants to break. A mismatch is an *alignment
  problem*; `runs.py` catches it before `apply` does. Where the prose won't
  divide the same way, merge or split runs on the translated side — the run
  boundaries carry no meaning, only the count does.
- **Work files, never corpus files.** `i18n/corpus/**` is written by `apply`.
  Fix the work file and re-apply; a hand edit is lost the next time.
- **`META title / nav_label / description` come back filled in.** The English
  work file leaves them blank (the title is the article's, not the book's), so
  they are easy to skip — and a page with no translated title shows an English
  one in the sidebar, the `<title>`, the search index and the home page card.

### Audit the chapter's terms before you commit

A long chapter reintroduces nearly every proper noun and move name in the game,
and a plausible-but-new rendering is both the easiest mistake to make and the
hardest to catch — the page reads perfectly well on its own, and only the rest
of the language disagrees with it. So count, don't trust your memory:

```bash
grep -rhoE "Construtores|Criadores" i18n/corpus/pt-BR/ | sort | uniq -c
```

The majority across the whole language wins; `TERMS.md` breaks ties; the fix
goes in the **work** file and the page is re-applied. Four terms drifted this
way while pt-BR was being written, all now corrected and recorded in
`TERMS.md`:

| Right | Wrong | Why it was easy to get wrong |
|---|---|---|
| **Construtores** (Makers) | *Criadores* | *Primeiros Criadores* really is *First Creators* — a different thing |
| **NPC** | *PdM* | the obvious calque, but the corpus keeps NPC in English like HP and XP |
| **À Beira da Morte** (Death's Door) | *Porta da Morte* | the literal reading; the move is named for the state, not the door |
| **Beira-Brejo** (Marshedge) | *Marshedge* | Latin-script languages keep *Stonetop*, but not every name |
| **FOR · DES · CON · INT · SAB · CAR** | *STR · DEX · WIS · CHA* | the stat abbreviation was English in 26 files and Portuguese in the rest — see below |

**When the drift is in the generator, fix the generator.** The stat
abbreviation was the awkward one: a playbook sheet printed `(STR)` in every
language, because `PLAYBOOK_STATS` in `generator/extract.py` is structural data
a translator never sees, so half the prose had been written to match the sheet
and half to match the language. Neither half was wrong on its own. The fix was
`i18n/ui/<code>.json` → `sheet.stat_abbr`, read by `structure.py` for the
printed abbreviation and the roll tooltip only — `data-roll-stat` and the field
key (`the-ranger:stat-str`) stay English, like every other id on the site, so a
score typed on the English sheet is still there on the Portuguese one. Then the
prose was swept to the majority form.

One caution about sweeping: `apply` reads the **work** file, so a page whose
work file is gone cannot be re-applied, and at that point the corpus file is the
only copy. Check which slugs still have a work file before planning a sweep.

Names carry the English in parentheses on **first mention per page**
(`Beira-Brejo (Marshedge)`), so a bare English name later in a page is fine and
a bare English name on first mention is not.

### Finish with a full build

Mid-chapter, when you do want to see a page, `--pages <slug> --langs pt-BR`
renders it in ~3 s instead of ~16 s — worth it for a spot check, never worth
it as the last build before a commit.

`--pages <slug>` is for a spot check mid-chapter. **It is not what you commit**:
a `--langs pt-BR` run deletes the other nineteen language directories, and a
`--pages` run leaves the home pages, sitemap, search index and hover previews
describing the site as it was. A full build is ~16 s since the provenance-tag
rewrite (2026-09-16), so there is no longer a reason to commit a partial one.

### Localized home pages

`write_localized_index()` in `generator/chrome.py` writes `<lang>/index.html`
for every language that has any page, so a reader landing on `/pt-BR/` gets
their own front door rather than the English one. Its prose lives in
`ui/<code>.json` under `home`: `intro_html` (`{homefront}`, `{welcome}`,
`{playing}`), `lede_html` (`{books}`), `remembers_html`, `defects_html`
(`{issues}`) and `license_html` (`{license}`). `and` is the word that joins
the book names in the lede. `book_titles` is the full index heading for each
book (`Book II — The Wider World`), distinct from the short sidebar label in
`books`. `doc_title` is the index page's `<title>`.
`edition`, beside `home`, is the name in the sidebar and the suffix on every
other page's title (`Marshedge — Stonetop Web Edition`). A language without
a `home` block falls back to English (`HOME_FALLBACK`). Cards use the page's
translated title and description where there is one, and link to
`../<slug>.html` where there isn't. Home pages are site-wide, so a `--pages`
run skips them.

### Two details that only show up in a translated page

- **"(page 200)" is a link**, and the linker has to recognise the word the
  translation actually used. `ui/<code>.json` → `page_words` lists them
  (`["página", "pág."]` for pt-BR); English is always understood as well.
  Without it every page reference in the chapter renders as plain text.
- **A page reference with no label is named after its target**, so on a
  translated page it should read as that page's title *in that language*.
  The build passes each locale's slug→title map down to the linker for this.

## Legacy JSON pages

`pages/<code>/<slug>.json` holds pages translated before the corpus route
existed: an HTML body with the English markup and translated text. The build
still publishes them (and prints them `stale` when the English body's hash
moves), but **the route is closed** — its tools are gone, and no page is
added to it or edited in it. To fix or refresh one, translate the page through
the corpus; the corpus translation outranks the JSON page of the same slug,
which can then be deleted. **pt-BR holds none left** — every Portuguese page
was retranslated through the corpus and `i18n/pages/pt-BR/` is gone.

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
python stonetop-wiki-generator.py            # every language
python stonetop-wiki-generator.py --langs de fr ja
python stonetop-wiki-generator.py --langs none   # English only
```

Each language directory is rewritten from scratch every build, and a language
dropped from `langs.json` has its directory removed.

## Licensing

The books' text is CC BY-SA 4.0, which permits translation as an adaptation.
Every translated page keeps the attribution and license notice (the sidebar
footer carries both, translated), and the translations are themselves
CC BY-SA 4.0.

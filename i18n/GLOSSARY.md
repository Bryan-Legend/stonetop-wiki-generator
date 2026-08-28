# Translation policy

One rendering per term, per language, everywhere. Inconsistent terminology is
what makes a translated game wiki unusable — a reader who learns that *Defy
Danger* is *Trotza el peligro* must meet that phrase on every page, or the
cross-references stop working in their head.

## Names

The rule is Tolkien's, from his *Guide to the Names in The Lord of the Rings*:
**translate what the fiction presents as ordinary speech, keep what it presents
as foreign.** It is why published D&D ships *Waterdeep* as *Aguas Profundas*
and *Tiefwasser* while leaving *Drizzt* alone, and it fits this setting almost
exactly — the World's End is described in plain words, its gods and peoples in
invented ones.

The reader's **script** decides more than the name does.

| | Latin script<br>(es, de, fr, pl, fi…) | Cyrillic · kana · hangul<br>(ru, uk, ja, ko) | Chinese |
|---|---|---|---|
| **Descriptive** — Marshedge, Great Wood, Golden Oak, World's End, Dread River, Flats, Foothills, Frozen Wastes, Barrow Builders, Forest Folk, Forge Lords, Green Lords, Hillfolk, Rime Lords, Stone Lords, Tempest Lords | translate | transliterate | translate (semantic) |
| **Opaque invented** — Danu, Helior, Aratis, Tor, Aals, Hec'tumel, Crinwin, Fomoraij, Ustrina, Vor Svetelik, Lygos, Huffel, Manmarch | keep | transliterate | transcribe (phonetic) |
| **Stonetop** — the village *and* the game | keep (it is the brand) | transliterate | translate (semantic) |

For Cyrillic, kana and hangul, transliteration is not a translation decision at
all — it is the script conversion those languages perform on every foreign name
as a matter of course, and leaving Latin letters mid-sentence reads as an
untranslated hole. Chinese is the exception that cannot transcribe
multisyllabic English gracefully, so descriptive names go semantic; *Stonetop*
is descriptive (stone + top), hence 石顶 / 石頂 rather than a phonetic string of
characters that means nothing.

Keeping **Stonetop** in Latin-script languages sidesteps the question of what
the *game* is called in Spanish — in those languages the village and the game
keep one name, and in the others they take one localized name together.

### Settled so far

| | Stonetop |
|---|---|
| ru · uk | Стоунтоп |
| ja | ストーントップ |
| ko | 스톤탑 |
| zh-Hans · zh-Hant | 石顶 · 石頂 |
| es · pt-BR · fr · it · de · nl · sv · da · nb · fi · cs · pl · hu · tr | Stonetop |

| | World's End | Golden Oak |
|---|---|---|
| es | el Fin del Mundo | Roble Dorado |
| pt-BR | o Fim do Mundo | Carvalho Dourado |
| de | das Ende der Welt | Goldene Eiche |
| fr | le Bout du Monde | Chêne Doré |
| it | la Fine del Mondo | Quercia Dorata |
| nl | het Einde van de Wereld | Gouden Eik |
| sv | Världens Ände | Gyllene Eken |
| da · nb | Verdens Ende | Den Gyldne Eg · Den Gylne Eika |
| fi | Maailman Ääri | Kultainen tammi |
| cs | Konec světa | Zlatý dub |
| pl | Kraniec Świata | Złoty Dąb |
| hu | a Világ Vége | Aranytölgy |
| tr | Dünyanın Sonu | Altın Meşe |
| ru · uk | Край Света · Край Світу | Золотой Дуб · Золотий Дуб |
| ja · ko | 世界の果て · 세상의 끝 | 黄金の樫 · 황금 참나무 |
| zh-Hans · zh-Hant | 世界尽头 · 世界盡頭 | 黄金橡树 · 黃金橡樹 |

### Gloss the first mention

A localized name carries the English in parentheses the **first time it appears
on a page**, and then stands alone:

```
ja  本書はストーントップ（Stonetop）の設定ガイドです。ストーントップは、…
ru  Это путеводитель по миру «Стоунтоп» (Stonetop) — ролевой игры…
```

Two reasons. The likeliest non-English reader of this wiki is playing from the
English PDF, and the gloss is what lets them cross-reference it. And a Japanese
player searching for this game types ストーントップ, not *Stonetop* — a page
carrying both matches both queries, where a page carrying one cannot rank for
the other.

### Real people

Author and designer names follow each language's own publishing convention:
Cyrillic transliterates (*Джереми Страндберг*), CJK keeps the Latin form in a
credit line, as those languages usually do for Western authors. Never
translated in any language.

### Not names

Domains and URLs (lampblackandbrimstone.com) stay exactly as written.

## Game terms

Fixed per language. Where an official localization of the term exists in a
PbtA game published in that language, follow it; otherwise pick once.

- **GM** / **PC** — use whatever that language's own RPG community says, not
  a calque of the English. What this wiki has settled on so far:

  | | GM | PC |
  |---|---|---|
  | es | DJ | PJ |
  | pt-BR | mestre | PJ |
  | de | SL | SC |
  | fr | MJ | PJ |
  | pl | MG | BG |
  | it | GM | PG |
  | ru | Ведущий | персонажи игроков |
  | uk | Ведучий | персонажі гравців |
  | ja · zh · ko | GM | PC · 玩家角色 |
  | nl | SL | spelerspersonages |
  | sv | spelledare | rollpersoner |
  | da · nb | spilleder | spilpersoner · spillfigurer |
  | fi | pelinjohtaja | pelaajahahmot |
  | cs | Vypravěč | postavy hráčů |
  | hu | mesélő | JK |
  | tr | OY | OK |

- **move**, **playbook**, **steading**, **arcanum / arcana**, **danger**,
  **discovery**, **expedition**, **follower**, **debility**, **harm**
- **hearth fantasy** — the game's own genre label; render it, do not borrow

## Anchors and slugs are not translated

This is a separate axis from names, and it does not move with them. A page
keeps its English slug (`welcome-to-the-worlds-end.html`) and its English
section ids (`#how-to-use-this-book`) in every language, *including* the ones
whose prose is entirely in another script. That keeps one URL shape across the
site, keeps deep links portable between languages, and keeps a reader's ticked
checkboxes and answered questions — which are stored per slug — shared between
a page and its translations. Search engines index the content, not the path.

## Licensing

The books' text is CC BY-SA 4.0, which explicitly permits translation as an
adaptation. Every translated page must keep the attribution and the license
notice (the sidebar footer carries both, translated), and the translation
itself is licensed CC BY-SA 4.0 in turn.

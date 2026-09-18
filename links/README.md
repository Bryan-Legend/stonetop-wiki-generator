# Reference links

`links/<slug>.json` is a table of the works a page cites and where each one
lives — for now only the mediography (Book I, pp. 598–599): its games, books,
films, series, podcasts and blog posts. The build (`generator/reflinks.py`)
wraps the first mention of each title in the rendered page with a link, in
English and in every translation that keeps the title as printed; a title a
translation localizes is left unlinked and named in the build's notes.

Each entry:

| field | meaning |
|---|---|
| `title` | the title exactly as the book prints it (the italic name, the quoted essay title, or the bare phrase) |
| `match` | optional: the HTML to look for instead, when the printed title carries markup (`<em>Apocalypse World</em>: Crossing the Line`) |
| `also` | optional: fallback phrases tried when the title is not found as printed, for a translation that renders it (`Driftless` inside a Chinese sentence) |
| `kind` | `rpg`, `book`, `film`, `tv`, `game`, `comic`, `album`, `podcast`, `blog`, `post`, `place` |
| `url` | where it lives: a film or series on the streaming service that carries it in the US, else its JustWatch page; a book on Amazon, or a free text for public-domain work; a podcast, blog or post at its own site; a game at its publisher, else DriveThruRPG or itch.io |
| `site` | the site's short name, shown as the link's tooltip |
| `note` | anything uncertain — a streaming window that may lapse, a page taken from search results because the site refused the fetch |

Streaming rights move; the film and series links were checked 2026-09-18.

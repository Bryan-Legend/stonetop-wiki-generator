# Campaign sync

One campaign's wiki state, shared by everyone at the table — ticked steading
improvements, danger countdowns, map pins, enemy HP — instead of each browser
keeping its own private copy in `localStorage`.

A Cloudflare Worker over D1, fronted by `window.StonetopStore` in
`Stonetop_Wiki/js/wiki.js`. **`localStorage` stays the truth the page renders
from; the network is a mirror.** With no campaign configured, with the Worker
down, or with the wiki opened off a disk with no connection, the wiki behaves
exactly as it did before any of this existed.

Design notes and the reasoning behind the choices: `../CAMPAIGN-SYNC-PLAN.md`.

---

## Deploying it

Once, from this folder:

```bash
npm install
npx wrangler login

npx wrangler d1 create stonetop-sync      # copy the database_id it prints
#   → paste it into wrangler.toml, replacing REPLACE_WITH_DATABASE_ID

npm run schema:remote                     # create the tables
npm run deploy                            # → https://sync.stonetop-wiki.workers.dev
```

That address is already set as `DEFAULT_ENDPOINT` in the campaign panel in
`Stonetop_Wiki/js/wiki.js`, so nobody has to type it. If the Worker is ever
renamed or moved, that is the one string to change — it is chrome, not
generated, so the wiki build never overwrites it.

`ALLOWED_ORIGINS` in `wrangler.toml` lists the browser origins that may call
the Worker. It ships with the published wiki. A page opened straight off a
disk sends `Origin: null`, which is answered **only for the GM token** — the
GM is the one who reads the wiki out of Dropbox.

## Starting a campaign

1. GM opens the wiki, clicks **Campaign** in the sidebar footer, and presses
   **Create campaign**. The player link lands on the clipboard.
2. GM sends that one link to the players. They click it once. The wiki reads
   the campaign out of the address bar, adopts its state, and strips the token
   back out of the URL.
3. To bring a second GM browser in — the laptop at the table, say — press
   **Copy GM link** on the browser that created the campaign.

There are no accounts and no login. **The token is the only secret**, so treat
a join link like a key: two per campaign, one for players (read and write the
shared stores) and one for the GM (everything).

## What is shared, and with whom

| Store | Scope | Who sees it |
|---|---|---|
| `stonetop-wiki-checks` | `shared` | everyone — steading improvements, danger clocks |
| `stonetop-wiki-map-pins` | `shared` | everyone |
| `stonetop-wiki-notes` | `shared` | everyone — answers, blanks, playbook write-in boxes |
| `<sheet>-hp` | `gm` | the GM's browsers only |

The HP scope is a pattern, not a list: every adventure-site sheet names its own
store in `data-hp-storage`, so a new sheet joins the sync without a deploy.

Anything not in that table stays private to the browser — the dice-sound
setting, scroll positions.
The registry lives in two places that must agree: `STORE_SCOPES` in
`src/index.js` and the same table in `wiki.js`. The Worker is the one that
enforces it; the client's copy only saves a doomed round trip.

## Resetting a store

End of an arc, a TPK, or a test that got out of hand. From the GM's browser
console:

```js
StonetopStore.reset("stonetop-wiki-checks")
```

Or with `curl`, holding the GM token:

```bash
curl -X DELETE "$ENDPOINT/v1/state/$CAMPAIGN/stonetop-wiki-checks" \
  -H "authorization: Bearer $GM_TOKEN"
```

Deletions travel as tombstones, so a browser that still holds the keys is told
they are gone rather than pushing them back.

## Looking at a campaign

```bash
npx wrangler d1 execute stonetop-sync --remote \
  --command "select store, k, v, seq from entries where campaign = 'stonetop-…' order by seq"
```

## Tests

Both suites need a local Worker. In one shell:

```bash
npm run dev            # wrangler dev --local on :8788
npm run schema:local   # once, to create the local tables
```

In another:

```bash
npm test               # both suites
npm run test:worker    # curl against the endpoints: auth, scopes, merge,
                       # tombstones, etags, CORS, the caps, the rate limit
npm run test:client    # two jsdom "browsers" running the real wiki.js and
                       # site.js against that Worker: a tick made in one
                       # repaints in the other, a player joins by link, HP
                       # never reaches the players, the dot goes quiet when
                       # the Worker disappears
```

The client suite is the one that catches the interesting failures. Two of the
bugs it found were subtle enough to be worth naming:

- **Two edits in the same poll window used to strand each other.** The Worker
  read a campaign's sequence number in one request and wrote it back in the
  next, so two patches arriving together both claimed the same number. Neither
  client could then see the other's row: it was on the server, and invisible.
  The block is now claimed inside the same transaction that writes the rows.
- **A push response's cursor is the campaign's, not this browser's.** Skipping
  ahead to it stepped over rows someone else had written in between, and those
  rows were never seen again.

## Costs

Free tier throughout: Workers 100k requests/day, D1 5 GB and 5M row-reads/day.
A four-hour session with four browsers polling every 5 s is roughly 12k
requests, nearly all of them 304s.

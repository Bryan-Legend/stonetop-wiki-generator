# Campaign sync

One campaign's wiki state, shared by everyone at the table — ticked steading
improvements, danger countdowns, map pins, answers, playbook sheets, HP —
instead of each browser keeping its own private copy in `localStorage`.

A Cloudflare Worker over **Durable Objects**, fronted by `window.StonetopStore`
in `Stonetop_Wiki/js/wiki.js`. **`localStorage` stays the truth the page renders
from; the network is a mirror.** With no campaign configured, with the Worker
down, or with the wiki opened off a disk with no connection, the wiki behaves
exactly as it did before any of this existed.

Design notes and the reasoning behind the choices: `../CAMPAIGN-SYNC-PLAN.md`.

---

## Shape

**One Durable Object per campaign** (`src/campaign.js`), holding that
campaign's SQLite storage, its two tokens, and its open WebSockets. There is no
shared database. That buys three things at once:

- **Push.** The object holds the sockets, so a patch is fanned out to the table
  the instant it lands rather than waited for by four browsers polling every
  five seconds. Measured round trip, write to push: **~8 ms**.
- **No shared write lock.** D1 is a single SQLite primary, and every campaign's
  writes queued through it. A thousand campaigns are now a thousand independent
  objects.
- **No sequence race.** An object handles one request at a time, so the
  read-then-write of the campaign's sequence number is simply safe. On D1 it
  had to be a transaction, and getting that wrong lost edits.

Sockets use the **Hibernation API**: the object is evicted from memory while
connections stay open and wakes only when something arrives, so a table sitting
idle with four tabs open costs nothing.

Writes still go over HTTP `POST`, where retry and backoff already live; the
socket is receive-only. Moving writes onto it would bill them at the 20:1
WebSocket rate, which is worth doing if volume ever justifies it.

| File | What it is |
|---|---|
| `src/index.js` | Router. Picks the campaign's object, adds CORS, and little else. |
| `src/campaign.js` | One campaign: storage, tokens, patches, sockets. |
| `src/ratelimit.js` | One object per address, counting campaign creations. |
| `src/shared.js` | The scope registry, limits, CORS and token helpers. |

## Deploying it

Once, from this folder:

```bash
npm install
npx wrangler login
npm run deploy          # → https://sync.stonetop-wiki.workers.dev
```

No database to create: the objects carry their own storage, and the migration
in `wrangler.toml` declares them as SQLite-backed classes — the only kind the
Workers Free plan allows, and the kind we want anyway.

That address is already set as `DEFAULT_ENDPOINT` in the campaign panel in
`Stonetop_Wiki/js/wiki.js`. If the Worker is ever renamed or moved, that is the
one string to change — it is chrome, not generated, so the wiki build never
overwrites it.

`ALLOWED_ORIGINS` in `wrangler.toml` lists the browser origins that may call
the Worker. A page opened straight off a disk sends `Origin: null`, which is
answered **only for the GM token**. A WebSocket handshake gets no CORS
treatment from the browser at all, so that same rule is applied by hand in
`campaign.js`.

## Starting a campaign

1. GM opens the wiki, clicks **Campaign** in the sidebar, presses **Create
   campaign**. The player link lands on the clipboard.
2. GM sends that one link to the players. They click it once. The wiki reads
   the campaign out of the address bar, adopts its state, and strips the token
   back out of the URL.
3. For a second GM browser — the laptop at the table — press **Copy GM link**
   on the browser that created the campaign.

No accounts, no login. **The token is the only secret**, so treat a join link
like a key. Two per campaign: player, and GM.

## What is shared, and with whom

| Store | Scope | Who sees it |
|---|---|---|
| `stonetop-wiki-checks` | `shared` | everyone — steading improvements, danger clocks |
| `stonetop-wiki-map-pins` | `shared` | everyone |
| `stonetop-wiki-notes` | `shared` | everyone — answers, blanks, playbook write-in boxes |
| `stonetop-wiki-playbook-hp` | `shared` | everyone — a character's own HP |
| `stonetop-wiki-follower-hp` | `shared` | everyone — a follower's HP, the players' to manage |
| `<sheet>-hp`, `stonetop-wiki-monster-hp` | `gm` | the GM's browsers only |

The `-hp` scope is a pattern, not a list: every adventure-site sheet names its
own store in `data-hp-storage`, so a new sheet joins the sync without a deploy.
A character's HP is listed ahead of that pattern to stay `shared` — the table
watches each other's health; only the enemies' is the GM's alone.

Anything not in that table stays private to the browser: the dice-sound
setting, scroll positions. The registry lives in two places that must agree —
`STORE_SCOPES` in `src/shared.js` and the same table in `wiki.js`. The Worker
is the one that enforces it, on both the HTTP path and the socket broadcast;
the client's copy only saves a doomed round trip.

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

## Tombstone compaction

A tombstone kept forever is a row spent on saying nothing, counted against
`MAX_ROWS`. So each write sweeps tombstones older than `TOMBSTONE_TTL_MS`
(30 days; `src/shared.js`, overridable as an env var) and records a watermark
of how far replay history is gone. A browser that syncs inside the window
replays deletions normally; one away longer gets the whole state back, flagged
`full`, and prunes local keys the snapshot no longer carries — keeping, and
re-pushing, any key it changed itself while it was away.

## Tests

The first three need a Worker. In one shell:

```bash
npm run dev            # wrangler dev --local on :8788
```

In another:

```bash
npm test               # all four suites
npm run test:worker    # curl over the endpoints: auth, scopes, merge,
                       # tombstones, etags, CORS, the caps
npm run test:socket    # the push path: a write over HTTP landing on someone
                       # else's socket, and the scope filter holding on the
                       # way out
npm run test:client    # two jsdom "browsers" running the real wiki.js and
                       # site.js against that Worker: a tick made in one
                       # repaints in the other (and provably over the socket,
                       # not the poll), a player joins by link, HP never
                       # reaches the players, the dot goes quiet when the
                       # Worker disappears
npm run test:compact   # tombstone compaction — spawns its own Worker on
                       # :8799 with TOMBSTONE_TTL_MS=0: the sweep, the
                       # watermark, and a stale jsdom browser pruning what
                       # was deleted while keeping its own unpushed edit
```

Any but the compaction suite can be pointed at production:

```bash
SYNC_ENDPOINT=https://sync.stonetop-wiki.workers.dev npm test
```

**Local state resets** by stopping wrangler and deleting `.wrangler/state` —
that is what clears both the campaigns and the creation rate limit. `.dev.vars`
raises `MAX_CREATES_PER_HOUR` locally so a test run is not fighting the limit
it is meant to be testing; the real limit lives in `src/shared.js`, and the
check for it is opt-in:

```bash
SYNC_TEST_RATELIMIT=1 bash test/worker.sh    # spends this address's allowance
```

The client suite is the one that catches the interesting failures. Four bugs it
has found, worth naming because none was obvious by reading:

- **Two edits in the same poll window used to strand each other.** The Worker
  read a campaign's sequence number in one request and wrote it back in the
  next, so two patches arriving together both claimed the same number. Neither
  client could then see the other's row: it was on the server, and invisible.
  (Gone entirely now — one object, one request at a time.)
- **A push response's cursor is the campaign's, not this browser's.** Skipping
  ahead to it stepped over rows someone else had written in between.
- **A default written on load beats what the campaign holds.** A site sheet
  seeded full HP into the store when it opened, and a local value the server
  has never heard of wins the merge — so opening a sheet healed everything the
  GM had wounded. Full health is now stored as *nothing at all*.
- **An unread request body kills the response.** Forwarding an oversized body
  to an object left a stream nobody read, and the runtime raises "Can't read
  from request stream after response has been sent". Oversized bodies are now
  turned away at the router, before any object sees them.

## Costs

At this table: free, comfortably. At 1,000 campaigns/day (4 browsers, 4-hour
sessions, ~500 writes each), push works out around **20–25× cheaper** than
polling — roughly the Workers subscription versus ~$120/month — because a
polling bill scales with time × users while a push bill scales with edits. The
figures behind that are in `../CAMPAIGN-SYNC-PLAN.md` §7.

# Campaign Sync — Cloudflare Worker plan

> **Status: built.** Rollout steps 1–4 are done and tested; step 5 (play-test one session
> before handing links to Mark, Doug, and Bryce) is the GM's, and step 6 (Durable Objects)
> is untouched. The Worker lives in `sync-worker/` — see its `README.md` for deploying it
> and starting a campaign. The client is `window.StonetopStore` in `Stonetop_Wiki/js/wiki.js`.
> No generator or rebuild work was needed, as §2 predicted. Where the build departed from
> what is written below, §8 says why.

Goal: let everyone at the table share one campaign's wiki state — ticked steading
improvements, danger countdowns, map pins, enemy HP — instead of each browser keeping its
own private copy in `localStorage`.

Approach: a small Cloudflare Worker backed by D1, fronted by a swappable storage layer
inside `js/wiki.js`. Local storage stays the source of truth for rendering; the network is
a mirror. If the Worker is down, unreachable, or never configured, the wiki behaves
exactly as it does today.

---

## 1. What we're syncing

Three flat JSON blobs, a few KB total.

| Store key | Shape | Written at | Scope |
|---|---|---|---|
| `stonetop-wiki-checks` | `{"marshedge#fire-2": true, …}` | `js/wiki.js:1006` | shared |
| `stonetop-wiki-map-pins` | `{mapId: [{x,y,color,label}, …]}` | `js/wiki.js:1105` | shared |
| `sealed-cave-hp`, `underfalls-hp`, `vasilyas-grove-hp` | `{enemyId: currentHP}` | `sites/site.js:101` | gm |

The per-site HP key comes from `<body data-hp-storage="…">`, so each new sheet adds a
key automatically — the registry must accept a pattern, not a fixed list.

**Scopes.** `shared` syncs to everyone. `gm` syncs only to browsers holding the GM token.
Enemy HP mid-fight is the one clearly spoiler-bearing store; steading improvements and
danger clocks are usually *better* visible to players (watching Marshedge's Fire countdown
fill up is the point).

Known wrinkle: `stonetop-wiki-checks` is a single blob mixing both kinds — steading
improvements and danger countdowns live side by side, keyed `slug#checkId`. v1 ships the
whole blob as `shared`. If a specific page needs hiding later, route per-key by slug prefix
against a small GM-only page list rather than splitting the store.

---

## 2. Why no generator or rebuild work is needed

Every page already loads `js/wiki.js`:

- generated pages — emitted by the page template at `stonetop-wiki-generator.py:8104`
  (`{rel_prefix}js/wiki.js`) and the index template at `:8629`;
- site sheets — hand-authored, loading `../js/wiki.js` *before* `site.js`
  (`Vasilyas-Grove.html:517-518`), both plain synchronous scripts, so load order holds.

`js/wiki.js` is chrome. The build never writes it — it only emits `<slug>.html`,
`index.html`, `js/previews-data.js`, and `js/search-index.js`.

So the sync layer goes **inside `js/wiki.js`**, exposed as `window.StonetopStore`, and
`site.js` consumes it. No template edits, no new `<script>` tag, no wiki rebuild, and
nothing that can be clobbered by the next `--input .` run.

---

## 3. Client: the storage layer

### 3.1 Extract `StonetopStore` (backend-agnostic, ship this first)

A single module near the top of `js/wiki.js`, before the existing IIFEs:

```js
window.StonetopStore = (function () {
  var mem = {};            // key -> parsed object
  var subs = {};           // key -> [fn]

  function get(key) {
    if (!(key in mem)) {
      try { mem[key] = JSON.parse(localStorage.getItem(key) || "{}") || {}; }
      catch (e) { mem[key] = {}; }
    }
    return mem[key];
  }

  function set(key, value, opts) {
    mem[key] = value;
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {}
    if (!(opts && opts.remote === false)) queuePush(key);   // no-op until §3.3
    notify(key);
  }

  function subscribe(key, fn) { (subs[key] || (subs[key] = [])).push(fn); }
  function notify(key) { (subs[key] || []).forEach(function (fn) { fn(get(key)); }); }

  return { get: get, set: set, subscribe: subscribe };
})();
```

Then rewrite the three call sites to use it:

- **Checks** (`js/wiki.js:995-1085`) — `loadState()` → `Store.get("stonetop-wiki-checks")`,
  `saveState(st)` → `Store.set("stonetop-wiki-checks", st)`. The existing
  "sync other visible boxes for the same page+id" fan-out becomes the `subscribe` handler,
  which also makes remote updates repaint for free.
- **Map pins** (`js/wiki.js:1090-1230`) — `persist()` → `Store.set("stonetop-wiki-map-pins", store)`.
  Six call sites (`:1166`, `:1180-1181`, `:1201`, `:1228`). Subscribe → re-render pins.
- **Site HP** (`sites/site.js:89-101`) — `save()` →
  `StonetopStore.set(STORAGE_KEY, state)`, guarded by
  `window.StonetopStore || localStorage-fallback` so the sheets still work if opened
  standalone. Two call sites (`:140`, `:186`). Subscribe → `paint(id)`.

At this point nothing has changed behaviourally. Commit here; it's independently useful.

### 3.2 Campaign identity

Config lives in its own `localStorage` key, never synced:

```json
{ "endpoint": "https://sync.stonetop-wiki.workers.dev",
  "campaign": "stonetop-a7f3c91b",
  "token":    "<player or GM token>",
  "role":     "gm" }
```

Set via a small panel in the wiki's existing settings/menu area, or by pasting a join URL —
`…/index.html#join=<base64 of {campaign,token,role}>` — which the wiki consumes on load,
stores, and strips from the address bar. That's the whole onboarding: GM sends players one
link, they click it once.

No accounts, no login screen. **The token is the only secret**, so it must be long and
random (16+ bytes, base64url). Two tokens per campaign: player (read + write `shared`) and
GM (read + write everything).

### 3.3 Sync loop

- **Push** — `set()` records the key as dirty and debounces ~400 ms, then `POST`s a *patch*,
  not the whole blob (see §4.3). Failures retry with backoff and never block the UI; the
  local write already happened.
- **Pull** — `GET /v1/state/:campaign?since=<cursor>` every 5 s while the tab is visible
  (`document.visibilityState`), plus once on `visibilitychange` → visible. Send
  `If-None-Match`; a 304 is nearly free.
- **Merge** — server returns per-key patches; apply to the local blob, write through to
  `localStorage`, and `notify()` so the UI repaints.
- **Backoff** — on repeated failure, widen the poll to 30 s, then 5 min, and surface a quiet
  "offline" dot rather than an error. Never alert.

v2 upgrade path: swap the poll for a WebSocket against a Durable Object. The client
interface (`get`/`set`/`subscribe`) does not change.

---

## 4. Worker

### 4.1 Storage choice

**D1**, not KV.

KV is eventually consistent — a write can take up to ~60 s to be visible from another
location. At a live table that means a player ticks a box and someone else's browser
doesn't see it for a minute, or briefly *un*-sees it. That's the wrong failure mode for
this. D1 is SQLite with a single primary: read-after-write is immediate, it's on the free
plan, and you can inspect campaign state with `wrangler d1 execute --command "select …"`
when something looks wrong.

Durable Objects are the natural v2 home (strong consistency *and* WebSocket fan-out, one
object per campaign). Confirm current free-plan availability for DOs before depending on
them — historically they required a paid plan, and that has moved.

### 4.2 Schema

```sql
CREATE TABLE entries (
  campaign TEXT NOT NULL,
  scope    TEXT NOT NULL,          -- 'shared' | 'gm'
  store    TEXT NOT NULL,          -- 'stonetop-wiki-checks', 'underfalls-hp', …
  k        TEXT NOT NULL,          -- key within the blob
  v        TEXT,                   -- JSON value; NULL = tombstone
  seq      INTEGER NOT NULL,       -- monotonic per campaign
  PRIMARY KEY (campaign, store, k)
);
CREATE INDEX entries_since ON entries (campaign, seq);

CREATE TABLE campaigns (
  campaign     TEXT PRIMARY KEY,
  player_token TEXT NOT NULL,
  gm_token     TEXT NOT NULL,
  seq          INTEGER NOT NULL DEFAULT 0,
  created      INTEGER NOT NULL
);
```

Storing one row per *key* rather than one row per blob is what makes concurrent edits safe.

### 4.3 Per-key merge, not last-write-wins

If two players tick different checkboxes within the same poll window and the client PUTs
whole blobs, one edit is silently lost. Since every store is a flat string-keyed map, the
Worker instead accepts a patch and merges server-side:

```json
POST /v1/state/stonetop-a7f3c91b
{ "patches": [
    { "store": "stonetop-wiki-checks",
      "set":   { "marshedge#fire-2": true },
      "del":   ["stonetop#granary-1"] } ] }
```

Each `set`/`del` touches one row and bumps `seq`. The response returns the new cursor.
`GET …?since=<cursor>` returns only rows above it. Deletions are tombstones (`v IS NULL`)
so they propagate instead of being resurrected by a peer that still has the key.

Tombstones are not kept forever — they would count against the campaign's row cap while
saying nothing. Each write sweeps tombstones older than `TOMBSTONE_TTL_MS` (30 days) and
records a `compacted` watermark; a cursor behind the watermark gets the whole state back,
flagged `full`, and the client prunes local keys the snapshot no longer carries — unless it
changed one itself while away, in which case its edit survives and is pushed back up. On the
same principle the *client* never stores a value equal to a field's own default (an
unchecked box, a stat at +0, HP at full): absent, the page falls back to the default anyway.

**Map pins are the exception.** They're arrays, so per-key merge only reaches `mapId`
granularity — concurrent edits to the *same* map still clobber. Acceptable for v1: pins are
GM-drawn, effectively single-writer. Fix when it bites by giving each pin an `id` at
creation and storing `{mapId: {pinId: pin}}`, with a migration that reads the old array
shape and assigns ids.

### 4.4 Endpoints

| Method | Path | Auth | Does |
|---|---|---|---|
| `POST` | `/v1/campaigns` | none, rate-limited | Create campaign; returns id + both tokens |
| `GET` | `/v1/state/:campaign?since=` | either token | Rows above cursor, scope-filtered by token |
| `POST` | `/v1/state/:campaign` | either token | Apply patches; GM token required for `gm` scope |
| `DELETE` | `/v1/state/:campaign/:store` | GM token | Reset one store (end of arc, TPK, testing) |

### 4.5 Security and abuse

- Compare tokens in constant time; return an indistinguishable 404 for both "no such
  campaign" and "bad token" so the endpoint can't be probed for valid campaign ids.
- A player token writing a `gm`-scope store is a 403, and `GET` never includes `gm` rows for
  a player token — the filter is server-side, not a client courtesy.
- **CORS**: allow the Pages origin. Note that opening the wiki from disk sends
  `Origin: null`; decide whether to allow it (convenient for local GM use) or require the
  hosted origin (tighter). Recommend allowing `null` only for the GM token.
- Cap request body (~64 KB), keys per patch (~500), and total rows per campaign (~20k).
- Rate-limit campaign creation hard — it's the only unauthenticated endpoint.
- The wiki is public at `stonetop-wiki.github.io`, so the campaign id must be unguessable
  on its own; never derive it from the campaign's name.

---

## 5. Rollout

1. **§3.1 refactor only.** `StonetopStore` wrapping `localStorage`, all six call-site groups
   migrated, zero behaviour change. Verify: ticks persist, pins persist, HP persists,
   preview-popup checkbox fan-out still works.
2. **Worker skeleton.** `wrangler init`, D1 binding, schema, `POST /v1/campaigns`, and the
   two state endpoints. Test with `curl` before any client work.
3. **Wire the client**, behind a flag: sync is inert unless a campaign config exists. Test
   two browser profiles side by side.
4. **Join-link UI** and the offline indicator.
5. **Play-test one session** with the GM browser only, watching for surprises, before
   handing links to Mark, Doug, and Bryce.
6. Optional v2: Durable Object + WebSocket; drop the poll.

## 6. Deliberate non-goals

- No CRDTs. Per-key merge over flat maps is enough for a three-player table.
- No user accounts or per-player identity — everyone shares a token per role.
- No history or undo. `DELETE` is the only reset.
- No offline queue beyond retry-with-backoff; `localStorage` already holds the truth, so a
  long outage just means a late push.

## 7. Costs

Free tier throughout: Workers 100k requests/day, D1 5 GB and 5M row-reads/day. A four-hour
session with four browsers polling every 5 s is roughly 12k requests, nearly all 304s.

---

## 8. As built — where this departed from the plan

**§4.3 needed one more guarantee than it stated.** "Each `set`/`del` touches one row and
bumps `seq`" is not enough if the bump is a read in one request and a write in the next:
two patches arriving together both read 2 and both write their row as 3, each client polls
from 3, and neither is ever told about the other's edit. The rows are on the server and
invisible. The block is now claimed *inside* the transaction that writes the rows —
`UPDATE campaigns SET seq = seq + n … RETURNING seq` first in the batch, every row
numbering itself from the campaign's new `seq`. Concurrent writers get disjoint, gapless
blocks. The two-browser test in `sync-worker/test/client.mjs` is what found this.

**A push response's cursor belongs to the campaign, not to the browser that pushed.**
Advancing to it after a push looks like a free optimisation — our own rows are already
local — but it steps over rows another browser wrote in between, and those are then never
seen. The client leaves its cursor where it was and reads its own rows back on the next
poll, where they merge to nothing.

**Joining adopts the campaign's state; creating seeds from the GM's browser.** §3.2 did not
say which way a first sync should resolve. A player who has been reading the wiki alone has
ticks of their own that are not the campaign's, so a join link clears the synced stores
before its first pull. A campaign created from the GM's browser keeps what that browser
holds — the prep *is* the campaign.

**The join payload carries the endpoint too** (`{endpoint, campaign, token, role}`), so one
link is the whole of onboarding and nothing has to be typed. Campaign ids are 8 random
bytes rather than the 4 in §3.2's example — the wiki is public, and the id is half the
secret.

**Site sheets stopped seeding default HP into the store.** An enemy nobody has touched is
now simply absent, and reads as unhurt. Writing the default back made it look like a local
edit, and a local edit the campaign has never heard of wins over what the campaign holds —
so opening a sheet healed everything the GM had already wounded.

**A failed or coalesced push retries.** §3.3 asked for it; the first cut dropped a push
that arrived while another was in flight, and never re-formed a failed one until the next
local edit. Both now re-queue, widening with the same backoff as the poll.

Not built: the `DELETE` reset has no button (it is `StonetopStore.reset(store)` from the
console, or `curl` — a control that wipes the table's state should take more than a
mis-click), and Durable Objects remain the v2 idea they were.

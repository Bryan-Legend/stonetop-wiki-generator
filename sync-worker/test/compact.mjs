/* Tombstone compaction, end to end.
 *
 * A deletion has to travel as a tombstone or a peer would push the key back —
 * but a tombstone kept forever is a row spent on saying nothing, counted
 * against MAX_ROWS. So the Worker sweeps tombstones past their TTL and keeps
 * a watermark of how far replay history is gone; a client whose cursor is
 * behind the watermark is handed the whole state, flagged `full`, and prunes
 * what the snapshot no longer carries — unless it changed a key itself, in
 * which case its edit survives and goes back up.
 *
 * The TTL is a month in production, so this suite cannot run against the
 * shared dev worker: it spawns its own `wrangler dev` with TOMBSTONE_TTL_MS=0
 * (every tombstone is expired the moment the next write sweeps), on its own
 * port and its own state directory.
 */

import { JSDOM, VirtualConsole } from "jsdom";
import { spawn, execSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import fs from "node:fs";

if (process.env.SYNC_ENDPOINT) {
  console.log("skip: compaction needs its own worker (TTL=0); SYNC_ENDPOINT is set");
  process.exit(0);
}

const ROOT = new URL("../../Stonetop_Wiki/", import.meta.url);
const WIKI_JS = fs.readFileSync(new URL("js/wiki.js", ROOT), "utf8");
const CWD = fileURLToPath(new URL("..", import.meta.url));
const WRANGLER = fileURLToPath(
  new URL("../node_modules/wrangler/bin/wrangler.js", import.meta.url)
);
const STATE = fileURLToPath(new URL("../.wrangler/state-compact", import.meta.url));
const PORT = 8799;
const B = `http://127.0.0.1:${PORT}`;

let pass = 0,
  fail = 0;
const ok = (n) => { console.log("  PASS  " + n); pass++; };
const bad = (n, d) => { console.log("  FAIL  " + n + "\n        " + d); fail++; };
const chk = (n, want, got) =>
  JSON.stringify(want) === JSON.stringify(got)
    ? ok(n)
    : bad(n, `expected ${JSON.stringify(want)} got ${JSON.stringify(got)}`);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 10000) {
  const stop = Date.now() + ms;
  for (;;) {
    if (await fn()) return true;
    if (Date.now() > stop) return false;
    await sleep(80);
  }
}

/* ---------- a worker of our own ---------- */

fs.rmSync(STATE, { recursive: true, force: true });
const worker = spawn(
  process.execPath,
  [
    WRANGLER, "dev", "--local",
    "--port", String(PORT),
    "--var", "TOMBSTONE_TTL_MS:0",
    "--persist-to", STATE,
  ],
  { cwd: CWD, stdio: ["ignore", "pipe", "pipe"], windowsHide: true }
);
worker.stdout.on("data", () => {});
worker.stderr.on("data", () => {});
function stopWorker() {
  try {
    if (process.platform === "win32") {
      execSync(`taskkill /pid ${worker.pid} /T /F`, { stdio: "ignore" });
    } else {
      worker.kill("SIGTERM");
    }
  } catch (e) {
    /* already gone */
  }
}
process.on("exit", stopWorker);

const up = await until(async () => {
  try {
    return (await (await fetch(B + "/")).json()).ok === true;
  } catch (e) {
    return false;
  }
}, 90000);
if (!up) {
  console.log("could not start `wrangler dev` for the compaction suite");
  process.exit(1);
}

/* ---------- the campaign ---------- */

const create = await (await fetch(B + "/v1/campaigns", { method: "POST" })).json();
const cid = create.campaign;
const H = (tok) => ({
  authorization: "Bearer " + tok,
  "content-type": "application/json",
});
const post = (tok, body) =>
  fetch(`${B}/v1/state/${cid}`, {
    method: "POST",
    headers: H(tok),
    body: JSON.stringify(body),
  }).then((r) => r.json());
const pull = async (tok, since) => {
  const r = await fetch(`${B}/v1/state/${cid}?since=${since}`, {
    headers: { authorization: "Bearer " + tok },
  });
  return r.status === 304 ? { status: 304 } : await r.json();
};
const keys = (out) => (out.rows || []).map((r) => r.k).sort();

console.log("== a tombstone is kept until the sweep ==");
// seq 1, 2: two live keys.
await post(create.player_token, {
  patches: [{ store: "stonetop-wiki-checks", set: { "marshedge#fire-1": true }, del: [] }],
});
await post(create.player_token, {
  patches: [{ store: "stonetop-wiki-checks", set: { "stonetop#granary-1": true }, del: [] }],
});
// seq 3: the first is deleted. The sweep runs before this patch lands, when
// there is nothing to sweep — so the tombstone it writes survives it.
await post(create.player_token, {
  patches: [{ store: "stonetop-wiki-checks", set: {}, del: ["marshedge#fire-1"] }],
});
{
  const out = await pull(create.player_token, 0);
  chk("the deletion is still a tombstone", ["marshedge#fire-1", "stonetop#granary-1"], keys(out));
  const tomb = out.rows.find((r) => r.k === "marshedge#fire-1");
  chk("with a null value", null, tomb.v);
  chk("and no full flag yet", undefined, out.full);
}

console.log("\n== the next write sweeps it ==");
// seq 4 — and with TOMBSTONE_TTL_MS=0 the sweep ahead of it purges seq 3.
await post(create.player_token, {
  patches: [{ store: "stonetop-wiki-checks", set: { "stonetop#pallisade-1": true }, del: [] }],
});
{
  const out = await pull(create.player_token, 0);
  chk("a cursor behind the watermark gets the whole state", true, out.full);
  chk("without the swept tombstone", ["stonetop#granary-1", "stonetop#pallisade-1"], keys(out));
  chk("at the campaign's cursor", 4, out.cursor);
}
{
  const out = await pull(create.player_token, 3);
  chk("a cursor at the watermark still gets a delta", undefined, out.full);
  chk("of just the new row", ["stonetop#pallisade-1"], keys(out));
}
chk(
  "a caught-up cursor is still 304",
  304,
  (await pull(create.player_token, 4)).status
);

/* ---------- a browser that slept through the sweep ---------- */

console.log("\n== a stale browser prunes, but keeps its own edit ==");
const PAGE = `<!doctype html><html><head>
<meta property="og:url" content="https://stonetop-wiki.github.io/marshedge.html">
</head><body>
<nav class="sidebar"><div class="sidebar-head">
  <a class="wiki-title" href="index.html">Stonetop Wiki</a>
  <input type="search" id="nav-filter" class="nav-filter">
  <div id="search-results" class="search-results" hidden></div>
</div><ul><li>nav</li></ul><div class="sidebar-foot"></div></nav>
<main class="content">
  <ul><li><input type="checkbox" class="wiki-check" data-check-id="fire-1"> one</li>
      <li><input type="checkbox" class="wiki-check" data-check-id="fire-2"> two</li></ul>
</main>
<div id="wiki-preview" class="wiki-preview" hidden></div>
<div id="dice-toast" class="dice-toast" hidden></div>
</body></html>`;

{
  /* This browser saw seq 1–2 and went to sleep: fire-1 came from the server
     (it is in the baseline), fire-2 it ticked itself and never pushed. While
     it slept, fire-1 was deleted and the tombstone swept. It resumes from its
     saved config — StonetopStore.connect() is the join path, which adopts the
     campaign and drops local state on purpose. */
  const seed = new Map([
    ["stonetop-wiki-sync", JSON.stringify({
      endpoint: B,
      campaign: cid,
      token: create.player_token,
      role: "player",
    })],
    ["stonetop-wiki-checks", JSON.stringify({
      "marshedge#fire-1": true,
      "stonetop#granary-1": true,
      "marshedge#fire-2": true,
    })],
    ["stonetop-wiki-sync-cursor", JSON.stringify({ cursor: 2, campaign: cid })],
    ["stonetop-wiki-sync-base", JSON.stringify({
      campaign: cid,
      stores: {
        "stonetop-wiki-checks": {
          "marshedge#fire-1": "true",
          "stonetop#granary-1": "true",
        },
      },
    })],
  ]);
  const vc = new VirtualConsole();
  vc.on("jsdomError", () => {});
  const dom = new JSDOM(PAGE, {
    url: "https://stonetop-wiki.github.io/marshedge.html",
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole: vc,
  });
  const w = dom.window;
  const box = new Map(seed);
  Object.defineProperty(w, "localStorage", {
    configurable: true,
    value: {
      get length() { return box.size; },
      key: (i) => Array.from(box.keys())[i] ?? null,
      getItem: (k) => (box.has(k) ? box.get(k) : null),
      setItem: (k, v) => box.set(k, String(v)),
      removeItem: (k) => box.delete(k),
      clear: () => box.clear(),
    },
  });
  w.fetch = (...a) => fetch(...a);
  Object.defineProperty(w.document, "visibilityState", {
    configurable: true,
    get: () => "visible",
  });
  w.eval(WIKI_JS);
  w.document.dispatchEvent(new w.Event("DOMContentLoaded"));

  const boxes = w.document.querySelectorAll("input.wiki-check");
  chk("before the sync, both boxes are ticked", [true, true],
      [boxes[0].checked, boxes[1].checked]);

  const state = () => JSON.parse(box.get("stonetop-wiki-checks") || "{}");
  const settled = await until(() =>
    !state()["marshedge#fire-1"] &&
    state()["marshedge#fire-2"] === true &&
    state()["stonetop#pallisade-1"] === true
  );
  chk("the deleted key is pruned; its own edit and the new row stay", true, settled);
  chk("the page repainted to match", [false, true],
      [boxes[0].checked, boxes[1].checked]);

  const pushed = await until(async () => {
    const out = await pull(create.gm_token, 0);
    return (out.rows || []).some(
      (r) => r.k === "marshedge#fire-2" && r.v === "true"
    );
  });
  chk("the kept edit was pushed back up", true, pushed);
  const out = await pull(create.gm_token, 0);
  chk("and the pruned key was not", false,
      (out.rows || []).some((r) => r.k === "marshedge#fire-1"));
}

console.log("\n" + pass + " passed, " + fail + " failed");
stopWorker();
process.exit(fail ? 1 : 0);

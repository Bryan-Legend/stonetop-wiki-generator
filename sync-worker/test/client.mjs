/* Two browsers at one table, against the local `wrangler dev` worker.
 *
 * Each "browser" is a jsdom window with its own localStorage, running the
 * real Stonetop_Wiki/js/wiki.js (and sites/site.js for a sheet). The wiki
 * page carries a couple of wiki checkboxes and a map strip; the sheet page
 * carries an enemy row. Then: tick a box in one, and see it reach the other. */

import { JSDOM, VirtualConsole } from "jsdom";
import fs from "node:fs";

// ../../Stonetop_Wiki — the chrome this suite is testing, read off disk.
const ROOT = new URL("../../Stonetop_Wiki/", import.meta.url);
const WIKI_JS = fs.readFileSync(new URL("js/wiki.js", ROOT), "utf8");
const SITE_JS = fs.readFileSync(new URL("sites/site.js", ROOT), "utf8");
// Defaults to the local `wrangler dev`. Point it at the deployed Worker to
// check a fresh deploy: SYNC_ENDPOINT=https://sync.stonetop-wiki.workers.dev
const B = process.env.SYNC_ENDPOINT || "http://127.0.0.1:8788";

let pass = 0,
  fail = 0;
const ok = (n) => { console.log("  PASS  " + n); pass++; };
const bad = (n, d) => { console.log("  FAIL  " + n + "\n        " + d); fail++; };
const chk = (n, want, got) =>
  JSON.stringify(want) === JSON.stringify(got)
    ? ok(n)
    : bad(n, `expected ${JSON.stringify(want)} got ${JSON.stringify(got)}`);

const WIKI_PAGE = `<!doctype html><html><head>
<meta property="og:url" content="https://stonetop-wiki.github.io/marshedge.html">
</head><body>
<nav class="sidebar">
  <div class="sidebar-head">
    <a class="wiki-title" href="index.html">Stonetop Wiki</a>
    <input type="search" id="nav-filter" class="nav-filter" placeholder="Search wiki…">
    <div id="search-results" class="search-results" hidden></div>
  </div>
  <ul><li>nav</li></ul>
  <div class="sidebar-foot"><a class="sidebar-github" href="#">GitHub</a>
  <button type="button" class="sound-toggle" id="sound-toggle"><span class="sound-label">Dice sound</span></button></div>
</nav>
<main class="content">
  <h2 id="fire">Fire</h2>
  <ul><li><input type="checkbox" class="wiki-check" data-check-id="fire-1"> one</li>
      <li><input type="checkbox" class="wiki-check" data-check-id="fire-2"> two</li></ul>
  <div class="maps-strip"><div class="map-canvas" data-map="campaign"></div></div>
  <button id="map-add"></button><button class="map-color" data-color="#e2534a"></button>
</main>
<div id="wiki-preview" class="wiki-preview" hidden></div>
<div id="dice-toast" class="dice-toast" hidden></div>
</body></html>`;

const SHEET_PAGE = `<!doctype html><html><head>
<meta property="og:url" content="https://stonetop-wiki.github.io/sites/Underfalls.html">
</head><body class="site-sheet" data-wiki-root="../" data-hp-storage="underfalls-hp">
<nav class="site-nav">
  <a class="nav-title" href="#top">Underfalls</a>
  <a class="nav-wiki-home" href="../index.html">← Wiki</a>
  <span class="nav-label">Prep</span>
  <div class="sidebar-foot"><a class="sidebar-github" href="#">GitHub</a></div>
</nav>
<div class="site-main">
  <div class="enemy-row" data-hp-id="ogre" data-hp-max="6">
    <span class="hp-boxes"></span><span class="hp-readout"></span>
  </div>
</div>
<div id="wiki-preview" class="wiki-preview" hidden></div>
<div id="dice-toast" class="dice-toast" hidden></div>
</body></html>`;

/** One browser: its own localStorage, its own window, the real scripts. */
function browser(html, url, opts = {}) {
  const vc = new VirtualConsole();
  vc.on("jsdomError", () => {}); // a failed <script src> for previews-data.js
  const dom = new JSDOM(html, {
    url,
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole: vc,
  });
  const w = dom.window;
  // jsdom shares one localStorage per origin, so give each browser its own.
  const box = new Map(opts.seed || []);
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
  if (opts.sheet) w.eval(SITE_JS);
  w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
  return { dom, w, box };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Poll until the predicate holds. Beats guessing at a delay. */
async function until(fn, ms = 8000) {
  const stop = Date.now() + ms;
  for (;;) {
    if (await fn()) return true;
    if (Date.now() > stop) return false;
    await sleep(80);
  }
}

function checks(b) {
  return JSON.parse(b.w.localStorage.getItem("stonetop-wiki-checks") || "{}");
}
function tick(b, i, on) {
  const box = b.w.document.querySelectorAll("input.wiki-check")[i];
  box.checked = on;
  box.dispatchEvent(new b.w.Event("change"));
}
async function rows(token) {
  const r = await fetch(`${B}/v1/state/${create.campaign}?since=0`, {
    headers: { authorization: "Bearer " + token },
  });
  return r.status === 304 ? [] : (await r.json()).rows;
}

const create = await (await fetch(B + "/v1/campaigns", { method: "POST" })).json();
if (!create.campaign) {
  console.log("could not create a campaign — is `wrangler dev` running?", create);
  process.exit(1);
}
console.log("campaign " + create.campaign + "\n");

const gmCfg = {
  endpoint: B,
  campaign: create.campaign,
  token: create.gm_token,
  playerToken: create.player_token,
  role: "gm",
};
const playerCfg = {
  endpoint: B,
  campaign: create.campaign,
  token: create.player_token,
  role: "player",
};

/* ------------------------------------------------------------------ */
console.log("== no campaign configured: the wiki as it always was ==");
{
  const b = browser(WIKI_PAGE, "https://stonetop-wiki.github.io/marshedge.html");
  const S = b.w.StonetopStore;
  chk("starts off", "off", S.status());
  chk("no config", null, S.config());

  tick(b, 1, true);
  chk("a tick still lands in localStorage", true, checks(b)["marshedge#fire-2"]);
  chk("keyed by page and check id", ["marshedge#fire-2"], Object.keys(checks(b)));

  tick(b, 0, true);
  const boxes = b.w.document.querySelectorAll("input.wiki-check");
  chk("both boxes read back", [true, true], [boxes[0].checked, boxes[1].checked]);

  tick(b, 0, false);
  chk("unticking removes the key", ["marshedge#fire-2"], Object.keys(checks(b)));

  /* A key from another page that happens to share a check id stays that
     page's alone — check ids are short ("fire-2", "10-1"), so the page slug
     in the key is what keeps pages apart. */
  const other = browser(WIKI_PAGE, "https://stonetop-wiki.github.io/marshedge.html", {
    seed: [["stonetop-wiki-checks", JSON.stringify({ "stonetop#fire-2": true })]],
  });
  const ob = other.w.document.querySelectorAll("input.wiki-check");
  chk("another page's tick never leaks across", false, ob[1].checked);

  // The tools row sits in the sidebar head, under the search box — not in the
  // footer, where it used to be.
  const tools = b.w.document.querySelector(".sidebar-tools");
  chk("the tools row exists", true, !!tools);
  chk(
    "and sits in the sidebar head",
    "sidebar-head",
    tools.parentNode.className
  );
  chk(
    "it comes after the search box",
    true,
    !!(tools.previousElementSibling &&
       tools.previousElementSibling.id === "search-results")
  );
  chk("the campaign button is in it", tools, b.w.document.getElementById("sync-toggle").parentNode);
  chk(
    "the dice-sound toggle moved in beside it",
    tools,
    b.w.document.getElementById("sound-toggle").parentNode
  );
  chk(
    "only one of it",
    1,
    b.w.document.querySelectorAll("#sound-toggle").length
  );

  // The panel: one button, no address to type.
  b.w.document.getElementById("sync-toggle").dispatchEvent(
    new b.w.MouseEvent("click", { bubbles: true })
  );
  const panel = b.w.document.querySelector(".sync-panel");
  chk("clicking it opens the panel", true, !!panel && !panel.hidden);
  const fieldLabels = [...panel.querySelectorAll(".sync-field > span")].map(
    (x) => x.textContent
  );
  chk("the worker address is not asked for", ["Join link"], fieldLabels);
  chk(
    "creating a campaign is one button",
    true,
    [...panel.querySelectorAll(".sync-action")].some(
      (x) => x.textContent === "Create campaign"
    )
  );

  chk("checks are shared", "shared", S.scopeOf("stonetop-wiki-checks"));
  chk("pins are shared", "shared", S.scopeOf("stonetop-wiki-map-pins"));
  chk("a new sheet's HP store is GM-only", "gm", S.scopeOf("kneeroot-hp"));
  chk("answers and playbook boxes travel", "shared", S.scopeOf("stonetop-wiki-notes"));
  chk("a character's own HP travels", "shared", S.scopeOf("stonetop-wiki-playbook-hp"));
  chk("a monster's HP is the GM's", "gm", S.scopeOf("stonetop-wiki-monster-hp"));
  chk("a follower's HP is the table's", "shared", S.scopeOf("stonetop-wiki-follower-hp"));
  chk("nor the dice-sound setting", null, S.scopeOf("stonetop-wiki-sound"));
}

/* ------------------------------------------------------------------ */
console.log("\n== the GM joins and ticks a box ==");
const gm = browser(WIKI_PAGE, "https://stonetop-wiki.github.io/marshedge.html");
gm.w.StonetopStore.connect(gmCfg);
await until(() => gm.w.StonetopStore.status() === "ok");
{
  tick(gm, 1, true);
  const reached = await until(async () => {
    const r = await rows(create.gm_token);
    return r.length === 1 && r[0].k === "marshedge#fire-2";
  });
  chk("the tick reaches the worker", true, reached);
  chk("status is ok", "ok", gm.w.StonetopStore.status());
  chk("the panel button exists", true, !!gm.w.document.getElementById("sync-toggle"));
  chk(
    "and its dot is lit",
    true,
    gm.w.document.getElementById("sync-toggle").classList.contains("is-on")
  );
}

/* ------------------------------------------------------------------ */
console.log("\n== a player joins by clicking the link ==");
{
  const link = gm.w.StonetopStore.joinLink("player");
  chk(
    "the link points at the published wiki root",
    true,
    link.startsWith("https://stonetop-wiki.github.io/index.html#join=")
  );
  const parsed = gm.w.StonetopStore.parseJoinLink(link);
  chk("it carries the player token", create.player_token, parsed.token);
  chk("and the player role", "player", parsed.role);

  // A player who has been reading the wiki on their own has ticks of their own.
  const before = browser(WIKI_PAGE, "https://stonetop-wiki.github.io/marshedge.html");
  tick(before, 0, true);
  chk("they had a tick of their own", ["marshedge#fire-1"], Object.keys(checks(before)));

  // Now they click the GM's link, on the same machine. The link's path is the
  // wiki root, but its hash joins from any page — consume it on the marshedge
  // page the fixture is, since a tick is keyed by the page it is on and no
  // longer reads across pages that happen to share a check id.
  const joined = browser(WIKI_PAGE, link.replace("index.html", "marshedge.html"), {
    seed: before.box,
  });
  const arrived = await until(() => checks(joined)["marshedge#fire-2"] === true);
  chk("the campaign's state arrives", true, arrived);
  chk("and replaces their own", ["marshedge#fire-2"], Object.keys(checks(joined)));
  chk("the token is out of the address bar", false, joined.w.location.hash.includes("join="));
  chk("they joined as a player", "player", joined.w.StonetopStore.config().role);

  const jb = joined.w.document.querySelectorAll("input.wiki-check");
  chk("the page repainted without a reload", [false, true], [jb[0].checked, jb[1].checked]);
}

/* ------------------------------------------------------------------ */
console.log("\n== the socket ==");
{
  const up = await until(() => gm.w.StonetopStore.pushing(), 15000);
  chk("the GM's browser is pushed to, not polling", true, up);
  chk("and reports itself connected", "ok", gm.w.StonetopStore.status());
}

console.log("\n== a tick travels between two browsers ==");
const player = browser(WIKI_PAGE, "https://stonetop-wiki.github.io/marshedge.html");
player.w.StonetopStore.connect(playerCfg);
await until(() => player.w.StonetopStore.status() === "ok");
{
  const t0 = Date.now();
  tick(player, 0, true);
  const seen = await until(
    () => gm.w.document.querySelectorAll("input.wiki-check")[0].checked === true,
    15000
  );
  const took = Date.now() - t0;
  chk("the player's tick repaints in the GM's browser", true, seen);
  /* The debounce before a push is 400ms and the poll is 5s. Landing well
     inside that is the proof it arrived over the socket rather than being
     waited for. */
  chk("it arrived over the socket, not the poll (" + took + "ms)", true, took < 3000);
}

/* ------------------------------------------------------------------ */
console.log("\n== two edits in the same poll window ==");
{
  // Different keys, at the same moment, in different browsers. Sending whole
  // blobs would lose one of these.
  tick(player, 0, false);
  tick(gm, 1, false);
  const settled = await until(
    () => Object.keys(checks(gm)).length === 0 && Object.keys(checks(player)).length === 0,
    20000
  );
  chk("neither delete is lost", true, settled);
}

/* ------------------------------------------------------------------ */
console.log("\n== map pins ==");
{
  gm.w.StonetopStore.set("stonetop-wiki-map-pins", {
    campaign: [{ x: 0.25, y: 0.4, color: "#e2534a", label: "Marshedge" }],
  });
  const arrived = await until(() => {
    const p = JSON.parse(player.w.localStorage.getItem("stonetop-wiki-map-pins") || "{}");
    return !!(p.campaign && p.campaign[0] && p.campaign[0].label === "Marshedge");
  }, 15000);
  chk("a pin the GM drops reaches the players", true, arrived);
  const drawn = player.w.document.querySelectorAll(".map-canvas .map-pin");
  chk("and is drawn on their map", 1, drawn.length);
  chk("with its label", "Marshedge", drawn[0].querySelector(".pin-label").textContent);
}

/* ------------------------------------------------------------------ */
console.log("\n== enemy HP is the GM's alone ==");
{
  const sheet = browser(SHEET_PAGE, "https://stonetop-wiki.github.io/sites/Underfalls.html", {
    sheet: true,
  });
  sheet.w.StonetopStore.connect(gmCfg);
  await until(() => sheet.w.StonetopStore.status() === "ok");

  const sheetTools = sheet.w.document.querySelector(".sidebar-tools");
  chk(
    "a site sheet puts the row under its way back to the wiki",
    "nav-wiki-home",
    sheetTools.previousElementSibling.className
  );

  const boxes = sheet.w.document.querySelectorAll(".hp-box");
  chk("the tracker drew six boxes", 6, boxes.length);
  boxes[2].dispatchEvent(new sheet.w.MouseEvent("click", { bubbles: true }));
  chk(
    "clicking the third box sets HP to 3",
    3,
    JSON.parse(sheet.w.localStorage.getItem("underfalls-hp")).ogre
  );

  const stored = await until(async () => {
    const r = await rows(create.gm_token);
    return r.some((x) => x.store === "underfalls-hp" && x.k === "ogre");
  });
  chk("the GM's worker holds it", true, stored);
  const forPlayers = await rows(create.player_token);
  chk(
    "the players are never sent it",
    [],
    forPlayers.filter((r) => r.store === "underfalls-hp").map((r) => r.k)
  );

  // A second GM browser on the same sheet picks it up.
  const other = browser(SHEET_PAGE, "https://stonetop-wiki.github.io/sites/Underfalls.html", {
    sheet: true,
  });
  other.w.StonetopStore.connect(gmCfg);
  const shown = await until(() => {
    const r = other.w.document.querySelector(".hp-readout");
    return !!r && r.textContent === "3/6";
  }, 15000);
  chk("the GM's other browser shows 3/6", true, shown);
}

/* ------------------------------------------------------------------ */
console.log("\n== a sheet opened on its own, with no wiki around it ==");
{
  /* The trackers live in wiki.js now — one implementation for the book pages
     and the sheets alike. A sheet loads it from ../js/wiki.js, so this only
     happens to a sheet copied out of the tree: site.js alone leaves the rows
     unbuilt, and must not throw doing it. */
  const vc = new VirtualConsole();
  const errors = [];
  vc.on("jsdomError", (e) => errors.push(e.message));
  const dom = new JSDOM(SHEET_PAGE, {
    url: "file:///D:/sheets/Underfalls.html",
    runScripts: "outside-only",
    virtualConsole: vc,
  });
  const w = dom.window;
  const box = new Map();
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
  let threw = null;
  try {
    w.eval(SITE_JS); // site.js alone — wiki.js never loaded
    w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
  } catch (e) {
    threw = e.message;
  }
  chk("site.js alone does not throw", null, threw);
  chk("the sidebar nav still works", true, !!w.document.querySelector(".site-nav"));
  chk("but there are no HP boxes without wiki.js", 0,
      w.document.querySelectorAll(".hp-box").length);
}

/* ------------------------------------------------------------------ */
console.log("\n== the worker going away ==");
{
  const lone = browser(WIKI_PAGE, "https://stonetop-wiki.github.io/marshedge.html");
  lone.w.StonetopStore.connect({
    endpoint: "http://127.0.0.1:9",
    campaign: "stonetop-nope",
    token: "nope",
    role: "player",
  });
  tick(lone, 1, true);
  chk("a tick still saves locally", true, checks(lone)["marshedge#fire-2"]);
  const offline = await until(() => lone.w.StonetopStore.status() === "offline", 25000);
  chk("the dot goes quiet rather than alerting", true, offline);
}

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);

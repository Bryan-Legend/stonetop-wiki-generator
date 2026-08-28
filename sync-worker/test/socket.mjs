/* The push path: a write over HTTP arriving on someone else's socket, and the
   scope filter holding on the way out.
     SYNC_ENDPOINT=https://sync.stonetop-wiki.workers.dev node test/socket.mjs */
const B = process.env.SYNC_ENDPOINT || "http://127.0.0.1:8788";
const W = B.replace(/^http/, "ws");
let bad = 0;
const say = (ok, m) => { console.log((ok ? "  ok    " : "  FAIL  ") + m); if (!ok) bad++; };

const made = await fetch(B + "/v1/campaigns", { method: "POST" });
const c = await made.json();
if (!c.campaign) {
  console.log(
    made.status === 429
      ? "  rate limited creating a campaign (5/hour per address). " +
        "Locally: stop wrangler, rm -rf .wrangler/state, start it again."
      : "  could not create a campaign — is the worker running? " + JSON.stringify(c)
  );
  process.exit(1);
}
const { campaign, gm_token: GM, player_token: PL } = c;
console.log("campaign " + campaign);

function open(token, since = 0) {
  const ws = new WebSocket(`${W}/v1/connect/${campaign}?since=${since}`, [
    "stonetop.v1",
    "tok." + token,
  ]);
  const frames = [];
  ws.addEventListener("message", (e) => frames.push(JSON.parse(e.data)));
  return new Promise((res, rej) => {
    ws.addEventListener("open", () => res({ ws, frames }));
    ws.addEventListener("error", rej);
    setTimeout(() => rej(new Error("timeout opening socket")), 8000);
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 6000) {
  const stop = Date.now() + ms;
  for (;;) { if (fn()) return true; if (Date.now() > stop) return false; await sleep(50); }
}
const patch = (token, patches) =>
  fetch(`${B}/v1/state/${campaign}`, {
    method: "POST",
    headers: { authorization: "Bearer " + token, "content-type": "application/json" },
    body: JSON.stringify({ patches }),
  }).then((r) => r.json());

// --- GM connects ---
const gm = await open(GM);
say(gm.ws.protocol === "stonetop.v1", "handshake echoes the protocol, not the token: " + JSON.stringify(gm.ws.protocol));
say(await until(() => gm.frames.length >= 1), "a catch-up frame arrives on connect");
say(gm.frames[0] && gm.frames[0].t === "rows" && gm.frames[0].rows.length === 0,
    "and it is empty for a fresh campaign");

// --- a write over HTTP is pushed down the socket ---
gm.frames.length = 0;
const t0 = Date.now();
await patch(PL, [{ store: "stonetop-wiki-checks", set: { "marshedge#fire-2": true } }]);
say(await until(() => gm.frames.length >= 1), "a player's write is pushed to the GM's socket");
const took = Date.now() - t0;
const f = gm.frames[0];
say(f && f.rows[0] && f.rows[0].k === "marshedge#fire-2", "with the row that changed");
say(typeof f.cursor === "number" && f.cursor > 0, "and a cursor: " + (f && f.cursor));
console.log(`  info  round trip write → push: ${took}ms`);

// --- a player must never be pushed a gm-scope row ---
const pl = await open(PL);
await until(() => pl.frames.length >= 1);
pl.frames.length = 0;
gm.frames.length = 0;
await patch(GM, [{ store: "underfalls-hp", set: { ogre: 3 } }]);
say(await until(() => gm.frames.length >= 1), "the GM is pushed their own enemy HP");
await sleep(600);
say(pl.frames.length === 0, "the player is pushed nothing at all: " + JSON.stringify(pl.frames));

// --- but a shared row reaches both ---
gm.frames.length = 0; pl.frames.length = 0;
await patch(GM, [{ store: "stonetop-wiki-playbook-hp", set: { "the-ranger": 12 } }]);
say(await until(() => pl.frames.length >= 1), "a character's HP reaches the player");
say(await until(() => gm.frames.length >= 1), "and the GM");

// --- a bad token is refused the socket ---
let refused = false;
try {
  const ws = new WebSocket(`${W}/v1/connect/${campaign}`, ["stonetop.v1", "tok.nope"]);
  await new Promise((res) => {
    ws.addEventListener("open", () => res());
    ws.addEventListener("error", () => { refused = true; res(); });
    ws.addEventListener("close", () => { refused = true; res(); });
    setTimeout(res, 5000);
  });
} catch (e) { refused = true; }
say(refused, "a wrong token cannot open a socket");

gm.ws.close(); pl.ws.close();
process.exit(bad ? 1 : 0);

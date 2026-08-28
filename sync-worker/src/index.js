/* Stonetop campaign sync — a Cloudflare Worker over D1.
 *
 * The wiki keeps its state in localStorage and renders from there; this is a
 * mirror the table shares. Every request is scoped to one campaign and
 * authorised by one of that campaign's two tokens (player or GM). There are
 * no accounts: the token is the only secret.
 *
 * Patches, not blobs. A client sends the keys it changed and the Worker
 * merges them into the campaign, so two people ticking different boxes in
 * the same poll window both land. See CAMPAIGN-SYNC-PLAN.md §4.3.
 */

/* ---------- What each store is, and who may see it ---------- */

/* Matched in order; the first hit wins. A store no rule names is refused —
 * the client only ever syncs stores it knows about, so an unknown one is a
 * bug or an abuse, not a new feature. The HP pattern is open-ended on
 * purpose: every new adventure-site sheet invents its own data-hp-storage
 * key, and none of them should need a deploy here. */
const STORE_SCOPES = [
  [/^stonetop-wiki-checks$/, "shared"],
  [/^stonetop-wiki-map-pins$/, "shared"],
  [/^[a-z0-9-]+-hp$/, "gm"],
];

const SCOPES_FOR_ROLE = { gm: ["shared", "gm"], player: ["shared"] };

/* Enemy HP mid-fight is the one clearly spoiler-bearing store. Steading
 * improvements and danger clocks are better seen by everyone — watching
 * Marshedge's Fire countdown fill up is the point. */
function scopeFor(store) {
  if (typeof store !== "string" || store.length > 120) return null;
  for (const [re, scope] of STORE_SCOPES) if (re.test(store)) return scope;
  return null;
}

/* ---------- Limits ---------- */

const MAX_BODY = 64 * 1024; // bytes of request body
const MAX_PATCH_KEYS = 500; // set + del entries in one POST
const MAX_ROWS = 20000; // rows a single campaign may hold
const MAX_GET_ROWS = 5000; // rows one pull returns; more → poll again
const MAX_CREATES_PER_HOUR = 5; // per address, on the one open endpoint

/* ---------- Small helpers ---------- */

function json(body, status, headers) {
  return new Response(JSON.stringify(body), {
    status: status || 200,
    headers: Object.assign(
      {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
      },
      headers || {}
    ),
  });
}

function b64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomToken() {
  return b64url(crypto.getRandomValues(new Uint8Array(24)));
}

/* Unguessable on its own: the wiki is public, so a campaign id that could be
 * derived from the campaign's name would hand out half the secret. */
function randomCampaignId() {
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  let hex = "";
  for (const b of bytes) hex += b.toString(16).padStart(2, "0");
  return "stonetop-" + hex;
}

/* Constant-time secret compare. Hashing first means unequal lengths cost the
 * same as equal ones, so neither timing nor length leaks. */
async function sameSecret(a, b) {
  const enc = new TextEncoder();
  const [ha, hb] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a == null ? "" : String(a))),
    crypto.subtle.digest("SHA-256", enc.encode(b == null ? "" : String(b))),
  ]);
  return crypto.subtle.timingSafeEqual(ha, hb);
}

function bearer(request) {
  const h = request.headers.get("authorization") || "";
  const m = h.match(/^Bearer\s+(.+)$/i);
  return m ? m[1].trim() : "";
}

/* ---------- CORS ----------
 *
 * A page opened off a disk sends `Origin: null`, which is how the GM reads
 * this wiki out of Dropbox. Allowing it for everyone would let any local file
 * that learned a token talk to the campaign, so it is answered only for the
 * GM token — and the GM token is the one the GM's own browser holds.
 *
 * A preflight cannot see the token (the browser does not send it), so OPTIONS
 * answers permissively and the real request enforces: when the origin is not
 * allowed for that token the response carries no allow-origin header at all,
 * and the browser drops it. */
const ALLOW_NULL_ORIGIN_GM = true;

function allowedOrigins(env) {
  return String(env.ALLOWED_ORIGINS || "")
    .split(/[\s,]+/)
    .filter(Boolean);
}

function corsFor(env, origin, role) {
  if (!origin) return {}; // curl, or same-origin — no CORS needed
  const ok =
    allowedOrigins(env).indexOf(origin) !== -1 ||
    (origin === "null" && ALLOW_NULL_ORIGIN_GM && role !== "player");
  if (!ok) return {};
  return { "access-control-allow-origin": origin, vary: "Origin" };
}

function preflight(env, request) {
  const origin = request.headers.get("origin") || "";
  const ok =
    allowedOrigins(env).indexOf(origin) !== -1 ||
    (origin === "null" && ALLOW_NULL_ORIGIN_GM);
  if (!ok) return new Response(null, { status: 403 });
  return new Response(null, {
    status: 204,
    headers: {
      "access-control-allow-origin": origin,
      "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
      "access-control-allow-headers":
        "authorization, content-type, if-none-match",
      "access-control-max-age": "86400",
      vary: "Origin",
    },
  });
}

/* ---------- Auth ----------
 *
 * "No such campaign" and "wrong token" answer identically, so the endpoint
 * cannot be probed for which campaign ids exist. */
async function authorize(env, campaign, token) {
  const row = await env.DB.prepare(
    "SELECT campaign, player_token, gm_token, seq FROM campaigns WHERE campaign = ?"
  )
    .bind(campaign)
    .first();
  if (!row) {
    // Still spend the comparison, so a missing campaign is not the fast path.
    await sameSecret(token, randomToken());
    return null;
  }
  if (await sameSecret(token, row.gm_token)) return { role: "gm", campaign: row };
  if (await sameSecret(token, row.player_token))
    return { role: "player", campaign: row };
  return null;
}

async function readBody(request) {
  const declared = parseInt(request.headers.get("content-length") || "0", 10);
  if (declared > MAX_BODY) return { error: "body too large" };
  const text = await request.text();
  if (text.length > MAX_BODY) return { error: "body too large" };
  if (!text.trim()) return { value: {} };
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object")
      return { error: "body must be an object" };
    return { value };
  } catch (e) {
    return { error: "body is not JSON" };
  }
}

/* ---------- POST /v1/campaigns ---------- */

async function createCampaign(env, request) {
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  const bucket = Math.floor(Date.now() / 3600000);
  const used = await env.DB.prepare(
    "SELECT n FROM creates WHERE ip = ? AND window = ?"
  )
    .bind(ip, bucket)
    .first();
  if (used && used.n >= MAX_CREATES_PER_HOUR) {
    return {
      status: 429,
      body: { error: "too many campaigns created; try again later" },
    };
  }

  const campaign = randomCampaignId();
  const playerToken = randomToken();
  const gmToken = randomToken();
  await env.DB.batch([
    env.DB.prepare(
      "INSERT INTO campaigns (campaign, player_token, gm_token, seq, created) " +
        "VALUES (?, ?, ?, 0, ?)"
    ).bind(campaign, playerToken, gmToken, Date.now()),
    env.DB.prepare(
      "INSERT INTO creates (ip, window, n) VALUES (?, ?, 1) " +
        "ON CONFLICT(ip, window) DO UPDATE SET n = n + 1"
    ).bind(ip, bucket),
    // Yesterday's buckets are of no further use.
    env.DB.prepare("DELETE FROM creates WHERE window < ?").bind(bucket - 24),
  ]);

  return {
    status: 201,
    body: { campaign, player_token: playerToken, gm_token: gmToken, cursor: 0 },
  };
}

/* ---------- GET /v1/state/:campaign?since= ---------- */

async function getState(env, request, campaign, auth) {
  const url = new URL(request.url);
  const since = Math.max(
    0,
    parseInt(url.searchParams.get("since") || "0", 10) || 0
  );
  const seq = auth.campaign.seq;
  const etag = 'W/"' + campaign + "." + auth.role + "." + seq + '"';

  // Nothing new: the common case at a table where no one has touched a box in
  // the last five seconds. Cheap for us, nearly free for them.
  if (since >= seq || request.headers.get("if-none-match") === etag) {
    return { status: 304, headers: { etag }, body: null };
  }

  const scopes = SCOPES_FOR_ROLE[auth.role];
  const placeholders = scopes.map(() => "?").join(", ");
  const { results } = await env.DB.prepare(
    "SELECT store, k, v, seq FROM entries " +
      "WHERE campaign = ? AND seq > ? AND scope IN (" +
      placeholders +
      ") ORDER BY seq LIMIT " +
      (MAX_GET_ROWS + 1)
  )
    .bind(campaign, since, ...scopes)
    .all();

  const all = results || [];
  const more = all.length > MAX_GET_ROWS;
  const rows = more ? all.slice(0, MAX_GET_ROWS) : all;
  // With rows still waiting, the cursor stops at the last one handed over, so
  // the next pull resumes exactly where this one stopped.
  const cursor = more ? rows[rows.length - 1].seq : seq;

  return {
    status: 200,
    headers: more ? {} : { etag },
    body: {
      cursor,
      more,
      rows: rows.map((r) => ({ store: r.store, k: r.k, v: r.v })),
    },
  };
}

/* ---------- POST /v1/state/:campaign ---------- */

async function postState(env, request, campaign, auth) {
  const parsed = await readBody(request);
  if (parsed.error) return { status: 413, body: { error: parsed.error } };
  const patches = parsed.value.patches;
  if (!Array.isArray(patches))
    return { status: 400, body: { error: "patches must be an array" } };

  const allowed = SCOPES_FOR_ROLE[auth.role];
  const ops = [];
  for (const patch of patches) {
    if (!patch || typeof patch !== "object") {
      return { status: 400, body: { error: "each patch must be an object" } };
    }
    const scope = scopeFor(patch.store);
    if (!scope)
      return { status: 400, body: { error: "unknown store: " + patch.store } };
    if (allowed.indexOf(scope) === -1) {
      // Not a courtesy the client could waive: the filter lives here.
      return {
        status: 403,
        body: { error: "GM token required for " + patch.store },
      };
    }
    const set = patch.set && typeof patch.set === "object" ? patch.set : {};
    for (const k of Object.keys(set)) {
      ops.push({ scope, store: patch.store, k, v: JSON.stringify(set[k]) });
    }
    for (const k of Array.isArray(patch.del) ? patch.del : []) {
      if (typeof k === "string")
        ops.push({ scope, store: patch.store, k, v: null });
    }
  }
  if (ops.length > MAX_PATCH_KEYS) {
    return { status: 413, body: { error: "too many keys in one patch" } };
  }
  if (!ops.length) {
    return { status: 200, body: { cursor: auth.campaign.seq, applied: 0 } };
  }

  const count = await env.DB.prepare(
    "SELECT COUNT(*) AS n FROM entries WHERE campaign = ?"
  )
    .bind(campaign)
    .first();
  if ((count ? count.n : 0) + ops.length > MAX_ROWS) {
    return { status: 507, body: { error: "campaign is full" } };
  }

  const seq = await applyOps(env, campaign, ops);
  return { status: 200, body: { cursor: seq, applied: ops.length } };
}

/* ---------- Numbering rows ----------
 *
 * Sequence numbers must never be handed out twice. Reading the campaign's seq
 * in one request and writing it back in the next loses the race outright: two
 * patches arriving together both read 2, both write their row as 3, and each
 * client then polls from 3 and is never told about the other's edit. The row
 * is on the server; it is simply invisible for the rest of the session.
 *
 * So the block is claimed inside the same transaction that writes the rows.
 * One batch is one transaction: the UPDATE goes first and RETURNING hands back
 * the top of the block, and every row numbers itself from the campaign's new
 * seq, which the statements after it in that transaction can already see.
 * Concurrent writers get disjoint, gapless blocks, and a reader sees either
 * all of a patch or none of it.
 */
async function applyOps(env, campaign, ops) {
  const stmts = [
    env.DB.prepare(
      "UPDATE campaigns SET seq = seq + ? WHERE campaign = ? RETURNING seq"
    ).bind(ops.length, campaign),
  ];
  ops.forEach((op, i) => {
    // Counted back from the top of the block, so the ops keep their order.
    const back = ops.length - 1 - i;
    stmts.push(
      op.v === null && op.update
        ? env.DB.prepare(
            "UPDATE entries SET v = NULL, " +
              "seq = (SELECT seq FROM campaigns WHERE campaign = ?) - ? " +
              "WHERE campaign = ? AND store = ? AND k = ?"
          ).bind(campaign, back, campaign, op.store, op.k)
        : env.DB.prepare(
            "INSERT INTO entries (campaign, scope, store, k, v, seq) VALUES " +
              "(?, ?, ?, ?, ?, (SELECT seq FROM campaigns WHERE campaign = ?) - ?) " +
              "ON CONFLICT(campaign, store, k) DO UPDATE SET " +
              "v = excluded.v, seq = excluded.seq, scope = excluded.scope"
          ).bind(campaign, op.scope, op.store, op.k, op.v, campaign, back)
    );
  });
  const out = await env.DB.batch(stmts);
  const claimed = out[0] && out[0].results && out[0].results[0];
  return claimed ? claimed.seq : 0;
}

/* ---------- DELETE /v1/state/:campaign/:store ---------- */

async function deleteStore(env, campaign, auth, store) {
  if (auth.role !== "gm")
    return { status: 403, body: { error: "GM token required" } };
  const scope = scopeFor(store);
  if (!scope) return { status: 400, body: { error: "unknown store: " + store } };

  const { results } = await env.DB.prepare(
    "SELECT k FROM entries WHERE campaign = ? AND store = ? AND v IS NOT NULL"
  )
    .bind(campaign, store)
    .all();
  const keys = (results || []).map((r) => r.k);
  if (!keys.length)
    return { status: 200, body: { cursor: auth.campaign.seq, cleared: 0 } };

  // Tombstones rather than deletes: a browser that still holds these keys has
  // to be told they are gone, or it would push them back on its next patch.
  const seq = await applyOps(
    env,
    campaign,
    keys.map((k) => ({ scope, store, k, v: null, update: true }))
  );

  return { status: 200, body: { cursor: seq, cleared: keys.length } };
}

/* ---------- Router ---------- */

export default {
  /* A thrown error would otherwise leave with no CORS headers, and the
     browser would report it as a blocked origin rather than as the outage it
     is — the client would show "origin not allowed" while the truth was that
     D1 hiccuped. Answering in JSON, with the headers, lets the client see a
     plain failure and back off. */
  async fetch(request, env) {
    const origin = request.headers.get("origin") || "";
    try {
      return await handle(request, env);
    } catch (e) {
      return json({ error: "sync worker error" }, 500, corsFor(env, origin, "gm"));
    }
  },
};

async function handle(request, env) {
  const url = new URL(request.url);
  const origin = request.headers.get("origin") || "";
  const path = url.pathname.replace(/\/+$/, "") || "/";

  if (request.method === "OPTIONS") return preflight(env, request);

  if (path === "/" || path === "/v1") {
    return json(
      { ok: true, service: "stonetop-sync" },
      200,
      corsFor(env, origin, null)
    );
  }

  if (path === "/v1/campaigns") {
    if (request.method !== "POST") {
      return json(
        { error: "method not allowed" },
        405,
        corsFor(env, origin, "gm")
      );
    }
    const out = await createCampaign(env, request);
    return json(out.body, out.status, corsFor(env, origin, "gm"));
  }

  const route = path.match(
    /^\/v1\/state\/([A-Za-z0-9._-]{1,64})(?:\/([A-Za-z0-9._-]{1,120}))?$/
  );
  if (!route) return json({ error: "not found" }, 404, corsFor(env, origin, null));

  const campaign = route[1];
  const store = route[2] || "";
  const auth = await authorize(env, campaign, bearer(request));
  // Indistinguishable from a campaign that does not exist — see authorize().
  if (!auth) return json({ error: "not found" }, 404, corsFor(env, origin, null));

  const cors = corsFor(env, origin, auth.role);
  // An origin this token may not use gets no allow-origin header and the
  // browser drops the response. A direct caller (curl) has no origin at all.
  if (origin && !cors["access-control-allow-origin"]) {
    return json({ error: "origin not allowed" }, 403);
  }

  let out;
  if (request.method === "GET" && !store) {
    out = await getState(env, request, campaign, auth);
  } else if (request.method === "POST" && !store) {
    out = await postState(env, request, campaign, auth);
  } else if (request.method === "DELETE" && store) {
    out = await deleteStore(env, campaign, auth, store);
  } else {
    out = { status: 405, body: { error: "method not allowed" } };
  }

  const headers = Object.assign({}, cors, out.headers || {});
  if (out.status === 304) return new Response(null, { status: 304, headers });
  return json(out.body, out.status, headers);
}

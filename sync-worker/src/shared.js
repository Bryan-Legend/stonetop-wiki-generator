/* Pieces the router and both Durable Objects need.
 *
 * The scope registry is the important one: it decides what travels and to
 * whom, and it must stay in step with the copy in Stonetop_Wiki/js/wiki.js.
 * This is the one that is enforced; the client's copy only saves a doomed
 * round trip.
 */

/* Matched in order; the first hit wins. A store no rule names is refused —
 * the client only ever syncs stores it knows about, so an unknown one is a bug
 * or an abuse, not a new feature. The HP pattern is open-ended on purpose:
 * every new adventure-site sheet invents its own data-hp-storage key, and none
 * of them should need a deploy here. */
export const STORE_SCOPES = [
  [/^stonetop-wiki-checks$/, "shared"],
  [/^stonetop-wiki-map-pins$/, "shared"],
  [/^stonetop-wiki-notes$/, "shared"],
  // A character's own HP, ahead of the catch-all below.
  [/^stonetop-wiki-playbook-hp$/, "shared"],
  // A follower's HP is the players' to manage.
  [/^stonetop-wiki-follower-hp$/, "shared"],
  [/^[a-z0-9-]+-hp$/, "gm"],
];

export const SCOPES_FOR_ROLE = { gm: ["shared", "gm"], player: ["shared"] };

/* Enemy HP mid-fight is the spoiler-bearing one. Ticked improvements, danger
 * clocks, answers and a character's own health are better seen by everyone. */
export function scopeFor(store) {
  if (typeof store !== "string" || store.length > 120) return null;
  for (const [re, scope] of STORE_SCOPES) if (re.test(store)) return scope;
  return null;
}

/* ---------- Limits ---------- */

export const MAX_BODY = 64 * 1024; // bytes of request body
/* How long a deletion's tombstone row is kept before it is compacted away.
 * A browser that syncs inside this window replays the deletion normally; one
 * that has been away longer is handed the whole state instead (see
 * rowsSince). Tests shrink it via the TOMBSTONE_TTL_MS env var. */
export const TOMBSTONE_TTL_MS = 30 * 24 * 60 * 60 * 1000;
export const MAX_PATCH_KEYS = 500; // set + del entries in one POST
export const MAX_ROWS = 20000; // rows a single campaign may hold
export const MAX_GET_ROWS = 5000; // rows one pull returns; more → ask again
export const MAX_CREATES_PER_HOUR = 5; // per address, on the one open endpoint
export const MAX_SOCKETS = 32; // live connections to one campaign

/* ---------- Small helpers ---------- */

export function json(body, status, headers) {
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

export function randomToken() {
  return b64url(crypto.getRandomValues(new Uint8Array(24)));
}

/* Unguessable on its own: the wiki is public, so a campaign id that could be
 * derived from the campaign's name would hand out half the secret. */
export function randomCampaignId() {
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  let hex = "";
  for (const b of bytes) hex += b.toString(16).padStart(2, "0");
  return "stonetop-" + hex;
}

/* Constant-time secret compare. Hashing first means unequal lengths cost the
 * same as equal ones, so neither timing nor length leaks. */
export async function sameSecret(a, b) {
  const enc = new TextEncoder();
  const [ha, hb] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a == null ? "" : String(a))),
    crypto.subtle.digest("SHA-256", enc.encode(b == null ? "" : String(b))),
  ]);
  return crypto.subtle.timingSafeEqual(ha, hb);
}

/* The token reaches us one of two ways. An ordinary request carries it in an
 * Authorization header; a WebSocket handshake cannot set one from a browser,
 * so it rides in the subprotocol list instead — which keeps it out of the URL,
 * and so out of request logs and referrers. */
export function bearer(request) {
  const h = request.headers.get("authorization") || "";
  const m = h.match(/^Bearer\s+(.+)$/i);
  if (m) return m[1].trim();
  const proto = request.headers.get("sec-websocket-protocol") || "";
  for (const part of proto.split(",")) {
    const p = part.trim();
    if (p.startsWith("tok.")) return p.slice(4);
  }
  return "";
}

export async function readBody(request) {
  const declared = parseInt(request.headers.get("content-length") || "0", 10);
  /* Belt and braces. The router turns an oversized body away before it ever
     reaches an object — it has to, because forwarding one leaves a stream
     nobody reads and the runtime objects to that. This catches a body with no
     content-length to judge it by, which is read and then measured. */
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
 * and the browser drops it.
 *
 * A WebSocket handshake is not subject to CORS, so the Origin check there is
 * ours to make explicitly — see the connect path in campaign.js. */
export const ALLOW_NULL_ORIGIN_GM = true;

export function allowedOrigins(env) {
  return String(env.ALLOWED_ORIGINS || "")
    .split(/[\s,]+/)
    .filter(Boolean);
}

export function originAllowed(env, origin, role) {
  if (!origin) return true; // curl, or same-origin
  if (allowedOrigins(env).indexOf(origin) !== -1) return true;
  return origin === "null" && ALLOW_NULL_ORIGIN_GM && role !== "player";
}

export function corsFor(env, origin, role) {
  if (!origin) return {}; // curl, or same-origin — no CORS needed
  if (!originAllowed(env, origin, role)) return {};
  return { "access-control-allow-origin": origin, vary: "Origin" };
}

export function preflight(env, request) {
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

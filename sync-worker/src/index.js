/* Stonetop campaign sync — a Cloudflare Worker over Durable Objects.
 *
 * The wiki keeps its state in localStorage and renders from there; this is a
 * mirror the table shares. Every request is scoped to one campaign and
 * authorised by one of that campaign's two tokens (player or GM). There are no
 * accounts: the token is the only secret.
 *
 * Patches, not blobs. A client sends the keys it changed and the campaign
 * merges them, so two people ticking different boxes at the same moment both
 * land. Changes are pushed back over a WebSocket; the client keeps its poll as
 * a fallback for a dropped socket.
 *
 * The router does almost nothing: it works out which campaign a request is
 * for, hands it to that campaign's Durable Object, and puts the CORS headers
 * on the way out. All the state, the tokens and the sockets live in the object
 * — see campaign.js.
 */

import { Campaign } from "./campaign.js";
import { RateLimiter } from "./ratelimit.js";
import {
  MAX_BODY,
  bearer,
  corsFor,
  json,
  preflight,
  randomCampaignId,
} from "./shared.js";

export { Campaign, RateLimiter };

/** The object that is this campaign. Ids are random, so the name is enough. */
function campaignStub(env, campaign) {
  return env.CAMPAIGN.get(env.CAMPAIGN.idFromName(campaign));
}

/* The token travels to the object out of band. It cannot stay in the
   Authorization header because a WebSocket handshake has none to give from a
   browser, and it must not sit in the client-facing URL, so the router moves
   it into an internal query parameter on the request it makes to the object. */
function internal(request, token, path) {
  const url = new URL(request.url);
  url.pathname = path;
  url.searchParams.set("__t", token);
  return new Request(url, request);
}

async function createCampaign(env, request) {
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  const limiter = env.RATE_LIMIT.get(env.RATE_LIMIT.idFromName("ip:" + ip));
  const allowed = await limiter.fetch("https://rate.limit/");
  if (!allowed.ok) {
    return json({ error: "too many campaigns created; try again later" }, 429);
  }
  const campaign = randomCampaignId();
  return campaignStub(env, campaign).fetch(
    "https://campaign/create?campaign=" + encodeURIComponent(campaign),
    { method: "POST" }
  );
}

/** Copy a Durable Object's answer out, with this origin's CORS on it. */
function relay(res, cors) {
  const headers = new Headers(res.headers);
  headers.delete("x-stonetop-role");
  for (const [k, v] of Object.entries(cors)) headers.set(k, v);
  return new Response(res.status === 304 ? null : res.body, {
    status: res.status,
    headers,
  });
}

async function handle(request, env) {
  const url = new URL(request.url);
  const origin = request.headers.get("origin") || "";
  const path = url.pathname.replace(/\/+$/, "") || "/";

  if (request.method === "OPTIONS") return preflight(env, request);

  if (path === "/" || path === "/v1") {
    return json(
      { ok: true, service: "stonetop-sync", push: true },
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
    return relay(await createCampaign(env, request), corsFor(env, origin, "gm"));
  }

  const route = path.match(
    /^\/v1\/(state|connect)\/([A-Za-z0-9._-]{1,64})(?:\/([A-Za-z0-9._-]{1,120}))?$/
  );
  if (!route)
    return json({ error: "not found" }, 404, corsFor(env, origin, null));

  const kind = route[1];
  const campaign = route[2];
  const store = route[3] || "";
  const stub = campaignStub(env, campaign);
  const token = bearer(request);

  /* Turned away here rather than inside the object. Forwarding it would hand
     the object a copy of this stream: the object discards its copy, this one
     is never read, and the runtime raises "Can't read from request stream
     after response has been sent" over the one left behind. Refusing at the
     edge also spares the object an invocation it was never going to use. */
  if (
    request.method === "POST" &&
    parseInt(request.headers.get("content-length") || "0", 10) > MAX_BODY
  ) {
    return json({ error: "body too large" }, 413, corsFor(env, origin, "gm"));
  }

  if (kind === "connect") {
    /* Straight through: the answer is a 101 with a socket attached, and
       nothing may be added to it. The object checks the origin itself, since a
       handshake gets no CORS treatment from the browser. */
    return stub.fetch(internal(request, token, "/connect"));
  }

  const res = await stub.fetch(
    internal(
      request,
      token,
      store ? "/state/" + encodeURIComponent(store) : "/state"
    )
  );

  /* The object cannot be asked for the caller's role until it has checked the
     token, so the allow-origin header is decided here, from what it answered.
     A 404 carries no role — that is the indistinguishable "no such campaign or
     wrong token", and gets the unauthenticated treatment. */
  const role = res.headers.get("x-stonetop-role");
  const cors = corsFor(env, origin, role);
  if (origin && role && !cors["access-control-allow-origin"]) {
    return json({ error: "origin not allowed" }, 403);
  }
  return relay(res, cors);
}

export default {
  /* A thrown error would otherwise leave with no CORS headers, and the browser
     would report it as a blocked origin rather than as the outage it is.
     Answering in JSON, with the headers, lets the client see a plain failure
     and back off. */
  async fetch(request, env) {
    const origin = request.headers.get("origin") || "";
    try {
      return await handle(request, env);
    } catch (e) {
      return json(
        { error: "sync worker error" },
        500,
        corsFor(env, origin, "gm")
      );
    }
  },
};

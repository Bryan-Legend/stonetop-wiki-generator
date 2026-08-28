/* One campaign, one Durable Object.
 *
 * This replaces the shared D1 database. Every campaign now carries its own
 * SQLite storage inside its own object, which buys three things at once:
 *
 *   - Push. The object holds the open WebSockets, so a patch can be fanned out
 *     to the table the instant it lands instead of being waited for by four
 *     browsers asking every five seconds.
 *   - No shared write lock. D1 is one SQLite primary; every campaign's writes
 *     queued through it. A thousand campaigns are now a thousand independent
 *     objects, each with its own storage.
 *   - No sequence race. A Durable Object handles one request at a time, so the
 *     read-then-write of the campaign's sequence number — which had to become a
 *     transaction to be safe on D1 — is simply safe here.
 *
 * Sockets use the Hibernation API: the object is evicted from memory while the
 * connections stay open and wakes only when something arrives, so a table
 * sitting idle with four tabs open costs nothing.
 */

import {
  MAX_GET_ROWS,
  MAX_PATCH_KEYS,
  MAX_ROWS,
  MAX_SOCKETS,
  SCOPES_FOR_ROLE,
  json,
  originAllowed,
  randomToken,
  readBody,
  sameSecret,
  scopeFor,
} from "./shared.js";

export class Campaign {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.sql = ctx.storage.sql;
    this.sql.exec(
      "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
    );
    this.sql.exec(
      "CREATE TABLE IF NOT EXISTS entries (" +
        "scope TEXT NOT NULL, store TEXT NOT NULL, k TEXT NOT NULL, " +
        "v TEXT, seq INTEGER NOT NULL, PRIMARY KEY (store, k))"
    );
    this.sql.exec("CREATE INDEX IF NOT EXISTS entries_seq ON entries (seq)");
  }

  /* ---------- meta ---------- */

  meta(key) {
    const row = this.sql
      .exec("SELECT v FROM meta WHERE k = ?", key)
      .toArray()[0];
    return row ? row.v : null;
  }

  setMeta(key, value) {
    this.sql.exec(
      "INSERT INTO meta (k, v) VALUES (?, ?) " +
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
      key,
      String(value)
    );
  }

  get seq() {
    return parseInt(this.meta("seq") || "0", 10) || 0;
  }

  exists() {
    return !!this.meta("gm_token");
  }

  /* ---------- lifecycle ---------- */

  create(campaign) {
    if (this.exists()) return null; // an id collision, or a repeated call
    const playerToken = randomToken();
    const gmToken = randomToken();
    this.setMeta("campaign", campaign);
    this.setMeta("player_token", playerToken);
    this.setMeta("gm_token", gmToken);
    this.setMeta("seq", 0);
    this.setMeta("created", Date.now());
    return { campaign, player_token: playerToken, gm_token: gmToken, cursor: 0 };
  }

  /* "No such campaign" and "wrong token" answer identically, so the endpoint
     cannot be probed for which campaign ids exist. */
  async authorize(token) {
    if (!this.exists()) {
      // Still spend the comparison, so a missing campaign is not the fast path.
      await sameSecret(token, randomToken());
      return null;
    }
    if (await sameSecret(token, this.meta("gm_token"))) return "gm";
    if (await sameSecret(token, this.meta("player_token"))) return "player";
    return null;
  }

  /* ---------- reading ---------- */

  rowsSince(role, since) {
    const scopes = SCOPES_FOR_ROLE[role];
    const holes = scopes.map(() => "?").join(", ");
    const all = this.sql
      .exec(
        "SELECT store, k, v, seq FROM entries WHERE seq > ? AND scope IN (" +
          holes +
          ") ORDER BY seq LIMIT " +
          (MAX_GET_ROWS + 1),
        since,
        ...scopes
      )
      .toArray();
    const more = all.length > MAX_GET_ROWS;
    const rows = more ? all.slice(0, MAX_GET_ROWS) : all;
    return {
      more,
      // With rows still waiting, the cursor stops at the last one handed over.
      cursor: more ? rows[rows.length - 1].seq : this.seq,
      rows: rows.map((r) => ({ store: r.store, k: r.k, v: r.v })),
    };
  }

  /* ---------- writing ---------- */

  /** Apply ops, bump the sequence, and tell everyone who is listening. */
  applyOps(ops) {
    let seq = this.seq;
    const written = [];
    for (const op of ops) {
      seq += 1;
      this.sql.exec(
        "INSERT INTO entries (scope, store, k, v, seq) VALUES (?, ?, ?, ?, ?) " +
          "ON CONFLICT(store, k) DO UPDATE SET " +
          "v = excluded.v, seq = excluded.seq, scope = excluded.scope",
        op.scope,
        op.store,
        op.k,
        op.v,
        seq
      );
      written.push({ scope: op.scope, store: op.store, k: op.k, v: op.v });
    }
    this.setMeta("seq", seq);
    this.broadcast(written, seq);
    return seq;
  }

  /* ---------- sockets ---------- */

  /** Every listener hears only what its own token is allowed to see. */
  broadcast(written, cursor, except) {
    const sockets = this.ctx.getWebSockets();
    if (!sockets.length) return;
    for (const ws of sockets) {
      if (ws === except) continue;
      let role = "player";
      try {
        const att = ws.deserializeAttachment();
        if (att && att.role) role = att.role;
      } catch (e) {
        /* an attachment we cannot read means we assume the lesser role */
      }
      const allowed = SCOPES_FOR_ROLE[role];
      const rows = written
        .filter((r) => allowed.indexOf(r.scope) !== -1)
        .map((r) => ({ store: r.store, k: r.k, v: r.v }));
      if (!rows.length) continue;
      try {
        ws.send(JSON.stringify({ t: "rows", cursor, rows }));
      } catch (e) {
        /* a socket going away mid-send is the close handler's problem */
      }
    }
  }

  async connect(request, role) {
    if (this.ctx.getWebSockets().length >= MAX_SOCKETS) {
      return json({ error: "too many connections" }, 429);
    }
    const url = new URL(request.url);
    const since = Math.max(
      0,
      parseInt(url.searchParams.get("since") || "0", 10) || 0
    );

    const pair = new WebSocketPair();
    const [client, server] = [pair[0], pair[1]];
    /* Hibernation: the object may be evicted while this stays open, so what we
       need to know about the connection is serialised onto it rather than kept
       in memory. */
    server.serializeAttachment({ role });
    this.ctx.acceptWebSocket(server);

    /* Catch up before anything new arrives, so the client's first frame is a
       complete picture rather than a delta onto a state it does not have. */
    const out = this.rowsSince(role, since);
    try {
      server.send(
        JSON.stringify({ t: "rows", cursor: out.cursor, rows: out.rows })
      );
    } catch (e) {
      /* nothing to do — the client will fall back to polling */
    }

    return new Response(null, {
      status: 101,
      webSocket: client,
      /* Echo the protocol, never the token that rode beside it. */
      headers: { "sec-websocket-protocol": "stonetop.v1" },
    });
  }

  /* The client has nothing it must say; writes go over HTTP, where retry and
     backoff already live. A keepalive is answered and otherwise ignored. */
  webSocketMessage(ws, message) {
    if (typeof message !== "string" || message.length > 256) return;
    try {
      if (JSON.parse(message).t === "ping") ws.send('{"t":"pong"}');
    } catch (e) {
      /* not ours */
    }
  }

  webSocketClose(ws, code, reason, wasClean) {
    try {
      ws.close(code === 1006 ? 1000 : code, reason);
    } catch (e) {
      /* already gone */
    }
  }

  webSocketError() {
    /* The socket is closed for us; the client reconnects or falls back. */
  }

  /* ---------- routing ---------- */

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    const origin = request.headers.get("origin") || "";
    const token = url.searchParams.get("__t") || "";

    /* The body is read here, before anything below can answer.
       A bad token, a disallowed origin and an over-cap payload all reply
       without needing it, and a subrequest whose stream is still unread when
       the answer goes out raises "Can't read from request stream after
       response has been sent" — as does draining it afterwards, which is why
       this cannot be a finally. */
    const body =
      request.method === "POST" && path !== "/create"
        ? await readBody(request)
        : null;

    /* Creation is called by the router with a freshly minted id; there is no
       token yet, and the object is empty by definition. */
    if (path === "/create") {
      const made = this.create(url.searchParams.get("campaign") || "");
      return made
        ? json(made, 201)
        : json({ error: "campaign exists" }, 409);
    }

    const role = await this.authorize(token);
    // Indistinguishable from a campaign that does not exist.
    if (!role) return json({ error: "not found" }, 404);

    /* A WebSocket handshake is not subject to CORS — no preflight, no
       allow-origin to withhold — so the same rule the ordinary requests get
       from the browser has to be applied here by hand. */
    if (!originAllowed(this.env, origin, role)) {
      return json({ error: "origin not allowed" }, 403);
    }

    if (path === "/connect") {
      if (request.headers.get("upgrade") !== "websocket") {
        return json({ error: "expected a websocket upgrade" }, 426);
      }
      /* A 101 carries a socket and nothing may be added to it, so this one
         goes back untouched. */
      return this.connect(request, role);
    }

    /* The router needs the role to decide the allow-origin header, and only
       we can know it. It strips this again on the way out. */
    const answer = await this.route(request, url, path, role, body);
    const out = new Response(answer.body, answer);
    out.headers.set("x-stonetop-role", role);
    return out;
  }

  async route(request, url, path, role, body) {
    if (path === "/state" && request.method === "GET") {
      const since = Math.max(
        0,
        parseInt(url.searchParams.get("since") || "0", 10) || 0
      );
      const seq = this.seq;
      const etag = 'W/"' + role + "." + seq + '"';
      if (since >= seq || request.headers.get("if-none-match") === etag) {
        return new Response(null, { status: 304, headers: { etag } });
      }
      const out = this.rowsSince(role, since);
      return json(out, 200, out.more ? {} : { etag });
    }

    if (path === "/state" && request.method === "POST") {
      return this.patch(body, role);
    }

    if (path.startsWith("/state/") && request.method === "DELETE") {
      return this.reset(role, decodeURIComponent(path.slice(7)));
    }

    return json({ error: "method not allowed" }, 405);
  }


  patch(parsed, role) {
    if (!parsed) return json({ error: "no body" }, 400);
    if (parsed.error) return json({ error: parsed.error }, 413);
    const patches = parsed.value.patches;
    if (!Array.isArray(patches))
      return json({ error: "patches must be an array" }, 400);

    const allowed = SCOPES_FOR_ROLE[role];
    const ops = [];
    for (const patch of patches) {
      if (!patch || typeof patch !== "object")
        return json({ error: "each patch must be an object" }, 400);
      const scope = scopeFor(patch.store);
      if (!scope) return json({ error: "unknown store: " + patch.store }, 400);
      if (allowed.indexOf(scope) === -1) {
        // Not a courtesy the client could waive: the filter lives here.
        return json({ error: "GM token required for " + patch.store }, 403);
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
    if (ops.length > MAX_PATCH_KEYS)
      return json({ error: "too many keys in one patch" }, 413);
    if (!ops.length) return json({ cursor: this.seq, applied: 0 });

    const count =
      this.sql.exec("SELECT COUNT(*) AS n FROM entries").toArray()[0].n || 0;
    if (count + ops.length > MAX_ROWS)
      return json({ error: "campaign is full" }, 507);

    return json({ cursor: this.applyOps(ops), applied: ops.length });
  }

  /** Wipe one store across the campaign (end of an arc, a TPK, a test). */
  reset(role, store) {
    if (role !== "gm") return json({ error: "GM token required" }, 403);
    const scope = scopeFor(store);
    if (!scope) return json({ error: "unknown store: " + store }, 400);
    const keys = this.sql
      .exec("SELECT k FROM entries WHERE store = ? AND v IS NOT NULL", store)
      .toArray()
      .map((r) => r.k);
    if (!keys.length) return json({ cursor: this.seq, cleared: 0 });
    /* Tombstones rather than deletes: a browser that still holds these keys
       has to be told they are gone, or it would push them back. */
    const cursor = this.applyOps(
      keys.map((k) => ({ scope, store, k, v: null }))
    );
    return json({ cursor, cleared: keys.length });
  }
}

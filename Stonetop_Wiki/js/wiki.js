/* Stonetop Wiki (Book I & Book II) — dice rolls + hover previews */
(function () {
  "use strict";

  /* ---------- StonetopStore: the table's shared state ----------------------
   *
   * Everything the wiki remembers — ticked steading improvements, danger
   * countdowns, map pins, enemy HP — has always lived in this browser's
   * localStorage, one private copy per person. This wraps those stores in one
   * small interface (get / set / subscribe) and, when a campaign is
   * configured, mirrors them through a Cloudflare Worker so everyone at the
   * table sees the same board.
   *
   * localStorage stays the truth the page renders from; the network is the
   * mirror. With no campaign configured — or with the Worker down, or the
   * wiki opened off a disk with no connection — every one of these calls is
   * exactly the localStorage it replaced, and the wiki behaves as it always
   * did. Nothing here ever blocks a click: the local write has already
   * happened by the time a push is even queued.
   *
   * See sync-worker/ and CAMPAIGN-SYNC-PLAN.md.
   * --------------------------------------------------------------------- */
  (function () {
    var CONFIG_KEY = "stonetop-wiki-sync"; // endpoint + campaign + token; never synced
    var CURSOR_KEY = "stonetop-wiki-sync-cursor";
    var BASE_KEY = "stonetop-wiki-sync-base";

    /* Which stores travel, and who may see them. Kept in step with the
       Worker's own table (sync-worker/src/index.js) — the Worker is the one
       that enforces it; this copy only saves a doomed round trip.

       The HP pattern is open-ended because every adventure-site sheet names
       its own store in data-hp-storage, so a new sheet joins the sync without
       anyone editing a list. Enemy HP mid-fight is the one clearly
       spoiler-bearing store; ticked improvements and danger clocks are better
       seen by everyone, and so are the answers and write-in boxes — a
       playbook filled in at the table is the table's, not one browser's.
       Anything not named here — the dice-sound setting, scroll positions —
       stays private to the browser. */
    var STORE_SCOPES = [
      [/^stonetop-wiki-checks$/, "shared"],
      [/^stonetop-wiki-map-pins$/, "shared"],
      [/^stonetop-wiki-notes$/, "shared"],
      /* A character's own HP, ahead of the catch-all below — the table watches
         each other's health; only the enemies' is the GM's alone. */
      [/^stonetop-wiki-playbook-hp$/, "shared"],
      /* A follower's HP is the players' to manage — a bound spirit, a Green
         Lord's vine — so it travels with the character sheets, not the enemies. */
      [/^stonetop-wiki-follower-hp$/, "shared"],
      [/^[a-z0-9-]+-hp$/, "gm"],
    ];

    var POLL_OK = 5000; // no socket, tab visible, and the Worker answering
    var POLL_IDLE = 60000; // socket up — a safety net, not the channel
    var POLL_SLOW = 30000; // after a few failures
    var POLL_COLD = 300000; // after many — a quiet dot, never an alert
    var PUSH_DEBOUNCE = 400;

    var mem = {}; // store -> parsed object, the copy the page renders from
    var baseline = {}; // store -> { key: JSON of the value the server last agreed }
    var subs = {}; // store -> [fn]
    var statusSubs = [];
    var known = {}; // every syncable store this browser has seen
    var cfg = null;
    var cursor = 0;
    var etag = "";
    var failures = 0;
    var pushTimer = null;
    var pollTimer = null;
    var socket = null;
    var socketTimer = null;
    var socketFailures = 0;
    var pushing = false;
    var pulling = false;
    var state = "off"; // off | ok | offline
    var joined = false; // a join link was consumed on this load

    function has(o, k) {
      return Object.prototype.hasOwnProperty.call(o, k);
    }

    function readJSON(key, fallback) {
      try {
        var raw = localStorage.getItem(key);
        if (!raw) return fallback;
        var v = JSON.parse(raw);
        return v && typeof v === "object" ? v : fallback;
      } catch (e) {
        return fallback;
      }
    }

    function writeJSON(key, value) {
      try {
        localStorage.setItem(key, JSON.stringify(value));
      } catch (e) {
        /* private mode, or a full quota — the page still works */
      }
    }

    function scopeOf(store) {
      for (var i = 0; i < STORE_SCOPES.length; i++) {
        if (STORE_SCOPES[i][0].test(store)) return STORE_SCOPES[i][1];
      }
      return null;
    }

    /** True if this browser's role may both read and write that store. */
    function syncs(store) {
      var scope = scopeOf(store);
      if (!scope) return false;
      if (scope === "gm") return !!(cfg && cfg.role === "gm");
      return true;
    }

    /* ---------- The interface the wiki uses ---------- */

    function get(store) {
      if (!has(mem, store)) {
        mem[store] = readJSON(store, {}) || {};
        if (scopeOf(store)) known[store] = true;
      }
      return mem[store];
    }

    /**
     * Write a store. The local write is immediate and unconditional; the push
     * is queued behind it and may never happen. opts.remote === false writes
     * locally only — used when applying what the server just told us, so an
     * incoming change does not bounce straight back out.
     */
    function set(store, value, opts) {
      mem[store] = value && typeof value === "object" ? value : {};
      if (scopeOf(store)) known[store] = true;
      writeJSON(store, mem[store]);
      if (!(opts && opts.remote === false)) queuePush();
      notify(store);
    }

    function subscribe(store, fn) {
      (subs[store] || (subs[store] = [])).push(fn);
    }

    function notify(store) {
      (subs[store] || []).forEach(function (fn) {
        try {
          fn(get(store));
        } catch (e) {
          /* one bad listener must not stop the others */
        }
      });
    }

    /* ---------- Campaign identity ---------- */

    function loadConfig() {
      var c = readJSON(CONFIG_KEY, null);
      if (!c || !c.endpoint || !c.campaign || !c.token) return null;
      c.endpoint = String(c.endpoint).replace(/\/+$/, "");
      c.role = c.role === "gm" ? "gm" : "player";
      return c;
    }

    function saveConfig(c) {
      cfg = c;
      if (c) writeJSON(CONFIG_KEY, c);
      else {
        try {
          localStorage.removeItem(CONFIG_KEY);
        } catch (e) {}
      }
      setState(c ? "ok" : "off");
    }

    function setCursor(n) {
      cursor = n || 0;
      writeJSON(CURSOR_KEY, { cursor: cursor, campaign: cfg ? cfg.campaign : "" });
    }

    function loadCursor() {
      var c = readJSON(CURSOR_KEY, null);
      // A cursor belongs to one campaign; joining another starts from nothing.
      cursor = c && cfg && c.campaign === cfg.campaign ? c.cursor || 0 : 0;
    }

    function saveBaseline() {
      writeJSON(BASE_KEY, { campaign: cfg ? cfg.campaign : "", stores: baseline });
    }

    function loadBaseline() {
      var b = readJSON(BASE_KEY, null);
      baseline =
        b && cfg && b.campaign === cfg.campaign && b.stores ? b.stores : {};
    }

    /* ---------- Diffing local against what the server last agreed ----------
     *
     * Every store is a flat map, so a patch is the keys whose value no longer
     * matches the baseline, plus the keys that have gone. Sending the keys
     * rather than the blob is what keeps two people ticking two different
     * boxes in the same five seconds from overwriting each other. */

    function stable(v) {
      try {
        return JSON.stringify(v);
      } catch (e) {
        return "";
      }
    }

    function diffFor(store) {
      var cur = get(store);
      var base = baseline[store] || (baseline[store] = {});
      var set_ = {};
      var del = [];
      var sent = {};
      var n = 0;
      var k;
      for (k in cur) {
        if (!has(cur, k)) continue;
        var s = stable(cur[k]);
        if (base[k] !== s) {
          set_[k] = cur[k];
          sent[k] = s;
          n++;
        }
      }
      for (k in base) {
        if (has(base, k) && !has(cur, k)) {
          del.push(k);
          n++;
        }
      }
      return n ? { store: store, set: set_, del: del, sent: sent } : null;
    }

    function collectPatches() {
      var out = [];
      Object.keys(known).forEach(function (store) {
        if (!syncs(store)) return;
        var d = diffFor(store);
        if (d) out.push(d);
      });
      return out;
    }

    function commitPatches(patches) {
      patches.forEach(function (p) {
        var base = baseline[p.store] || (baseline[p.store] = {});
        Object.keys(p.sent).forEach(function (k) {
          base[k] = p.sent[k];
        });
        p.del.forEach(function (k) {
          delete base[k];
        });
      });
      saveBaseline();
    }

    /* ---------- Talking to the Worker ---------- */

    function api(path, init) {
      var opts = init || {};
      opts.headers = Object.assign(
        { authorization: "Bearer " + cfg.token },
        opts.headers || {}
      );
      opts.cache = "no-store";
      return fetch(cfg.endpoint + path, opts);
    }

    function setState(next) {
      if (state === next) return;
      state = next;
      statusSubs.forEach(function (fn) {
        try {
          fn(state);
        } catch (e) {}
      });
    }

    function failed() {
      failures++;
      // Quiet, and only once it looks like more than a dropped packet.
      if (failures >= 2) setState("offline");
    }

    function succeeded() {
      failures = 0;
      setState("ok");
    }

    function queuePush(delay) {
      if (!cfg) return;
      clearTimeout(pushTimer);
      pushTimer = setTimeout(push, delay || PUSH_DEBOUNCE);
    }

    function push() {
      if (!cfg) return;
      // A push already in flight will look for anything new when it lands, so
      // this one only has to make sure it does.
      if (pushing) return queuePush();
      var patches = collectPatches();
      if (!patches.length) return;
      pushing = true;
      var body = JSON.stringify({
        patches: patches.map(function (p) {
          return { store: p.store, set: p.set, del: p.del };
        }),
      });
      api("/v1/state/" + encodeURIComponent(cfg.campaign), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: body,
      })
        .then(function (res) {
          if (!res.ok) throw new Error("push " + res.status);
          return res.json();
        })
        .then(function () {
          commitPatches(patches);
          // The cursor deliberately stays where it was. What comes back from
          // a push is the campaign's sequence number, not this browser's
          // place in it: skipping ahead to it would step over rows someone
          // else wrote in between, and those rows would never be seen again.
          // Reading our own rows back on the next poll costs one round trip
          // and merges to nothing, since they already match the baseline.
          succeeded();
        })
        .catch(function () {
          // The local write already happened; nothing was committed, so the
          // same patch re-forms from the same diff on the next attempt.
          failed();
        })
        .then(function () {
          pushing = false;
          // Whatever is still unsent — an edit made while this was in flight,
          // or the whole patch if it failed — goes on the next attempt, which
          // widens with the same backoff as the poll.
          if (collectPatches().length) {
            queuePush(failures ? pollDelay() : PUSH_DEBOUNCE);
          }
        });
    }

    function pull() {
      if (!cfg || pulling) return Promise.resolve();
      pulling = true;
      var headers = etag ? { "if-none-match": etag } : {};
      return api(
        "/v1/state/" + encodeURIComponent(cfg.campaign) + "?since=" + cursor,
        { headers: headers }
      )
        .then(function (res) {
          if (res.status === 304) {
            etag = res.headers.get("etag") || etag;
            succeeded();
            return null;
          }
          if (!res.ok) throw new Error("pull " + res.status);
          etag = res.headers.get("etag") || "";
          return res.json();
        })
        .then(function (out) {
          if (out) {
            applyRows(out.rows || [], out.full);
            setCursor(out.cursor || cursor);
            succeeded();
            // The server had more than one page of rows waiting.
            if (out.more) {
              pulling = false;
              return pull();
            }
          }
        })
        .catch(function () {
          failed();
        })
        .then(function () {
          pulling = false;
        });
    }

    /**
     * Merge the server's rows into the local blobs.
     *
     * A key this browser has changed since its last agreed value is left
     * alone — that edit is still queued to push, and it wins. Everything else
     * is written through to localStorage and announced, so the page repaints
     * without anyone reloading. A null value is a tombstone: something was
     * deleted, and saying so is the only way to stop a peer that still holds
     * the key from putting it back.
     *
     * `full` means the rows are the campaign's whole state, not a delta — the
     * Worker compacts tombstones after a while, and a browser away longer
     * than that gets the snapshot instead. A local key the snapshot no longer
     * carries was deleted while this browser was away, so it goes — unless
     * this browser changed it since the server last agreed, in which case the
     * edit is kept and pushed as new.
     */
    function applyRows(rows, full) {
      var touched = {};
      var seen = {};
      rows.forEach(function (r) {
        if (!r || !r.store || typeof r.k !== "string") return;
        if (!syncs(r.store)) return;
        (seen[r.store] || (seen[r.store] = {}))[r.k] = true;
        var cur = get(r.store);
        var base = baseline[r.store] || (baseline[r.store] = {});
        var localS = has(cur, r.k) ? stable(cur[r.k]) : undefined;
        var pendingLocal = localS !== base[r.k];
        if (r.v === null || r.v === undefined) {
          delete base[r.k];
          if (!pendingLocal && has(cur, r.k)) {
            delete cur[r.k];
            touched[r.store] = true;
          }
        } else {
          var val;
          try {
            val = JSON.parse(r.v);
          } catch (e) {
            return;
          }
          // Re-serialised from the parsed value, so it compares equal to what
          // this browser would write for the same thing.
          base[r.k] = stable(val);
          if (!pendingLocal) {
            cur[r.k] = val;
            touched[r.store] = true;
          }
        }
      });
      if (full) {
        var stores = {};
        Object.keys(known).forEach(function (s) {
          stores[s] = true;
        });
        Object.keys(baseline).forEach(function (s) {
          stores[s] = true;
        });
        Object.keys(stores).forEach(function (store) {
          if (!syncs(store)) return;
          var cur = get(store);
          var base = baseline[store] || (baseline[store] = {});
          var present = seen[store] || {};
          Object.keys(cur).forEach(function (k) {
            if (has(present, k)) return;
            if (base[k] === stable(cur[k])) {
              // Unchanged since last agreed — the deletion wins.
              delete cur[k];
              touched[store] = true;
            }
            // Either way it is no longer something the server holds.
            delete base[k];
          });
          Object.keys(base).forEach(function (k) {
            if (!has(present, k) && !has(cur, k)) delete base[k];
          });
        });
        // Kept local edits now differ from the baseline again; send them.
        queuePush();
      }
      Object.keys(touched).forEach(function (store) {
        writeJSON(store, mem[store]);
        notify(store);
      });
      if (rows.length || full) saveBaseline();
    }

    /* ---------- The socket ----------------------------------------------
     *
     * The campaign pushes changes down this as they land, so a tick crosses
     * the table in the time it takes to travel rather than in the time it
     * takes the next poll to come round. Everything it delivers goes through
     * the same applyRows() the poll used — the merge never knew or cared how
     * the rows arrived.
     *
     * The poll is kept, and it is not vestigial: it covers the socket being
     * refused, dropped, or never available, and it is what runs while the
     * socket is trying to come back. A browser that can only poll is a slower
     * browser, not a broken one.
     *
     * A browser cannot set an Authorization header on a handshake, so the
     * token rides in the subprotocol list — which, unlike a query parameter,
     * keeps it out of the URL and so out of logs and referrers.
     * ------------------------------------------------------------------- */

    function socketUrl() {
      return (
        cfg.endpoint.replace(/^http/, "ws") +
        "/v1/connect/" +
        encodeURIComponent(cfg.campaign) +
        "?since=" +
        cursor
      );
    }

    function openSocket() {
      if (!cfg || socket || !window.WebSocket) return;
      var ws;
      try {
        ws = new WebSocket(socketUrl(), ["stonetop.v1", "tok." + cfg.token]);
      } catch (e) {
        return; // the poll carries on
      }
      socket = ws;

      ws.onopen = function () {
        if (socket !== ws) return;
        socketFailures = 0;
        succeeded();
        // Push anything this browser is still holding back.
        queuePush();
        // The socket is the live channel now; the poll drops to a heartbeat.
        schedule();
      };

      ws.onmessage = function (ev) {
        if (socket !== ws) return;
        var msg;
        try {
          msg = JSON.parse(ev.data);
        } catch (e) {
          return;
        }
        if (!msg || msg.t !== "rows") return;
        applyRows(msg.rows || [], msg.full);
        if (typeof msg.cursor === "number") setCursor(msg.cursor);
        succeeded();
      };

      ws.onerror = function () {
        /* onclose always follows; the reconnect is decided there. */
      };

      ws.onclose = function () {
        if (socket !== ws) return;
        socket = null;
        socketFailures++;
        if (!cfg) return;
        /* Fall back to the poll at its usual pace and try the socket again,
           widening the gap so a Worker that is genuinely down is not hammered
           by four browsers. A pull happens on the way back regardless, so
           nothing that arrived while it was down is missed. */
        clearTimeout(socketTimer);
        socketTimer = setTimeout(
          openSocket,
          Math.min(30000, 1000 * Math.pow(2, Math.min(socketFailures, 5)))
        );
        schedule();
      };
    }

    function closeSocket() {
      clearTimeout(socketTimer);
      var ws = socket;
      socket = null;
      if (!ws) return;
      try {
        ws.close();
      } catch (e) {}
    }

    function socketLive() {
      return !!socket && socket.readyState === 1;
    }

    /* ---------- The poll ---------- */

    function pollDelay() {
      /* With the socket up this is only a safety net — something missed while
         the socket was down, or a socket that has gone quiet without saying
         so. Once a minute is plenty. */
      if (socketLive()) return POLL_IDLE;
      if (failures === 0) return POLL_OK;
      return failures < 4 ? POLL_SLOW : POLL_COLD;
    }

    function schedule() {
      clearTimeout(pollTimer);
      if (!cfg) return;
      pollTimer = setTimeout(tick, pollDelay());
    }

    function tick() {
      if (!cfg) return;
      if (document.visibilityState === "hidden") {
        // Nobody is looking. Wait for the tab to come back rather than
        // spending a request every five seconds on an idle window.
        schedule();
        return;
      }
      pull().then(schedule, schedule);
    }

    function start() {
      if (!cfg) {
        setState("off");
        return;
      }
      loadCursor();
      loadBaseline();
      registerLocalStores();
      setState("ok");
      socketFailures = 0;
      pull().then(started, function () {
        /* Even a failed first pull is worth a socket: the endpoint may be
           reachable over one and not the other. */
        started();
      });
    }

    function started() {
      // Anything this browser holds that the campaign has not heard about yet
      // — the GM's own prep, most often — goes up after the first pull, so the
      // campaign's state is merged with it rather than replaced.
      queuePush();
      openSocket();
      schedule();
    }

    function stop() {
      clearTimeout(pollTimer);
      clearTimeout(pushTimer);
      closeSocket();
      cursor = 0;
      etag = "";
      baseline = {};
      failures = 0;
      socketFailures = 0;
      setState("off");
    }

    /** Every syncable store this browser already holds, whatever page it is on. */
    function registerLocalStores() {
      try {
        for (var i = 0; i < localStorage.length; i++) {
          var k = localStorage.key(i);
          if (k && scopeOf(k)) known[k] = true;
        }
      } catch (e) {}
    }

    /* ---------- Joining ---------- */

    function b64urlEncode(text) {
      var bytes = new TextEncoder().encode(text);
      var s = "";
      for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
      return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    }

    function b64urlDecode(text) {
      var s = String(text).replace(/-/g, "+").replace(/_/g, "/");
      while (s.length % 4) s += "=";
      var bin = atob(s);
      var bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      return new TextDecoder().decode(bytes);
    }

    /**
     * The whole of onboarding: the GM sends one link, a player clicks it once.
     * Joining adopts the campaign's state rather than merging this browser's
     * into it — whatever ticks and pins were sitting in a player's copy from
     * reading the wiki on their own are not the campaign's.
     */
    function adopt() {
      Object.keys(known).forEach(function (store) {
        if (!scopeOf(store)) return;
        mem[store] = {};
        writeJSON(store, {});
        notify(store);
      });
      baseline = {};
      cursor = 0;
      etag = "";
      saveBaseline();
      setCursor(0);
    }

    /** The campaign a join link names, or null if that is not one. */
    function parseJoinLink(link) {
      var m = String(link || "").match(/[#&]join=([A-Za-z0-9\-_]+)/);
      if (!m) return null;
      var parsed;
      try {
        parsed = JSON.parse(b64urlDecode(m[1]));
      } catch (e) {
        return null;
      }
      if (!parsed || !parsed.endpoint || !parsed.campaign || !parsed.token)
        return null;
      return {
        endpoint: String(parsed.endpoint).replace(/\/+$/, ""),
        campaign: String(parsed.campaign),
        token: String(parsed.token),
        role: parsed.role === "gm" ? "gm" : "player",
      };
    }

    function consumeJoinHash() {
      var parsed = parseJoinLink(location.hash || "");
      if (!parsed) return false;
      registerLocalStores();
      saveConfig(parsed);
      adopt();
      // Take the token back out of the address bar before it reaches a
      // screenshot, a bookmark, or the next person to borrow the laptop.
      try {
        history.replaceState(null, "", location.pathname + location.search);
      } catch (e) {
        /* some browsers refuse replaceState on file:// */
      }
      return true;
    }

    /** A link that carries everything a browser needs to join this campaign. */
    function joinLink(role) {
      if (!cfg) return "";
      var payload = {
        endpoint: cfg.endpoint,
        campaign: cfg.campaign,
        token: role === "gm" ? cfg.token : cfg.playerToken || cfg.token,
        role: role === "gm" ? "gm" : "player",
      };
      var base = publishedRoot();
      return base + "#join=" + b64urlEncode(JSON.stringify(payload));
    }

    /* Where the players will actually open the wiki. A page states its
       published address in og:url; off a disk the address bar is a path into
       someone's Dropbox, which is no use in a link. */
    function publishedRoot() {
      var og = document.querySelector('meta[property="og:url"]');
      var stated = og ? og.getAttribute("content") || "" : "";
      var here = stated || String(location.href).split("#")[0];
      var prefix =
        (document.body && document.body.getAttribute("data-wiki-root")) || "";
      if (prefix && prefix.slice(-1) !== "/") prefix += "/";
      try {
        return new URL(prefix + "index.html", here).href;
      } catch (e) {
        return here;
      }
    }

    /* ---------- Creating a campaign (the GM, once) ---------- */

    function createCampaign(endpoint) {
      var base = String(endpoint || "").replace(/\/+$/, "");
      return fetch(base + "/v1/campaigns", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      })
        .then(function (res) {
          if (!res.ok) throw new Error("create " + res.status);
          return res.json();
        })
        .then(function (out) {
          registerLocalStores();
          saveConfig({
            endpoint: base,
            campaign: out.campaign,
            token: out.gm_token,
            playerToken: out.player_token,
            role: "gm",
          });
          // A campaign made from the GM's own browser starts from what that
          // browser already holds — the prep is the campaign.
          baseline = {};
          cursor = 0;
          etag = "";
          saveBaseline();
          setCursor(0);
          start();
          return out;
        });
    }

    function connect(c) {
      registerLocalStores();
      saveConfig({
        endpoint: String(c.endpoint || "").replace(/\/+$/, ""),
        campaign: String(c.campaign || ""),
        token: String(c.token || ""),
        playerToken: c.playerToken || "",
        role: c.role === "gm" ? "gm" : "player",
      });
      adopt();
      start();
    }

    function disconnect() {
      saveConfig(null);
      stop();
    }

    /** Wipe one store across the whole campaign (end of an arc, a TPK, a test). */
    function reset(store) {
      if (!cfg) return Promise.reject(new Error("not connected"));
      return api(
        "/v1/state/" +
          encodeURIComponent(cfg.campaign) +
          "/" +
          encodeURIComponent(store),
        { method: "DELETE" }
      ).then(function (res) {
        if (!res.ok) throw new Error("reset " + res.status);
        return pull();
      });
    }

    window.StonetopStore = {
      get: get,
      set: set,
      subscribe: subscribe,
      status: function () {
        return state;
      },
      /* True while changes are being pushed rather than polled for. */
      pushing: function () {
        return socketLive();
      },
      onStatus: function (fn) {
        statusSubs.push(fn);
        fn(state);
      },
      config: function () {
        return cfg
          ? {
              endpoint: cfg.endpoint,
              campaign: cfg.campaign,
              role: cfg.role,
              hasPlayerToken: !!cfg.playerToken,
            }
          : null;
      },
      justJoined: function () {
        return joined;
      },
      connect: connect,
      disconnect: disconnect,
      createCampaign: createCampaign,
      joinLink: joinLink,
      parseJoinLink: parseJoinLink,
      reset: reset,
      pull: function () {
        return pull();
      },
      scopeOf: scopeOf,
      /** Every syncable store this browser holds, whatever page it is on. */
      stores: function () {
        registerLocalStores();
        return Object.keys(known).sort();
      },
      /** True if this browser's role may both read and write that store. */
      syncs: syncs,
    };

    /* A join link is consumed before anything else reads a store, so the
       adopted campaign — not this browser's leftovers — is what the page
       binds to. */
    cfg = loadConfig();
    joined = consumeJoinHash();

    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && cfg) {
        failures = 0;
        socketFailures = 0;
        if (!socket) openSocket();
        tick();
      }
    });

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
  })();

  const SCRIPT_BASE = (function () {
    const scripts = document.getElementsByTagName("script");
    for (let i = scripts.length - 1; i >= 0; i--) {
      const src = scripts[i].src || "";
      if (src.indexOf("wiki.js") !== -1) {
        return src.replace(/js\/wiki\.js.*$/, "");
      }
    }
    return "";
  })();

  /**
   * Prefix to reach the wiki root from the current document.
   *
   * Wiki pages sit at the wiki root beside index.html, so they need no prefix.
   * Site sheets live in sites/ and declare data-wiki-root="../".
   */
  function wikiRootPrefix() {
    var root =
      (document.body && document.body.getAttribute("data-wiki-root")) || "";
    if (root && root.slice(-1) !== "/") root += "/";
    return root;
  }

  let previews = null;
  let previewsPromise = null;

  function loadPreviews() {
    if (previews) return Promise.resolve(previews);
    if (typeof window.WIKI_PREVIEWS === "object" && window.WIKI_PREVIEWS) {
      previews = window.WIKI_PREVIEWS;
      return Promise.resolve(previews);
    }
    if (previewsPromise) return previewsPromise;
    // Load as a script so previews work over file:// (fetch of JSON often fails there).
    // Missing previews-data.js (e.g. a site sheet opened without a built wiki) → empty {}.
    // Site sheets load wiki.js from ../js/ and set data-wiki-root to the wiki root.
    previewsPromise = new Promise(function (resolve) {
      function finish(obj) {
        previews =
          typeof obj === "object" && obj ? obj : {};
        resolve(previews);
      }
      function trySrc(src, next) {
        const s = document.createElement("script");
        s.src = src;
        s.onload = function () {
          var obj =
            typeof window.WIKI_PREVIEWS === "object" && window.WIKI_PREVIEWS
              ? window.WIKI_PREVIEWS
              : null;
          if (obj && Object.keys(obj).length) finish(obj);
          else if (next) next();
          else finish({});
        };
        s.onerror = function () {
          if (next) next();
          else finish({});
        };
        document.head.appendChild(s);
      }
      var sources = [SCRIPT_BASE + "js/previews-data.js"];
      var root =
        document.body && document.body.getAttribute("data-wiki-root");
      if (root) {
        if (root.slice(-1) !== "/") root += "/";
        var alt = root + "js/previews-data.js";
        if (sources.indexOf(alt) === -1) sources.push(alt);
      }
      var i = 0;
      function step() {
        if (i >= sources.length) {
          finish({});
          return;
        }
        var src = sources[i++];
        trySrc(src, step);
      }
      step();
    });
    return previewsPromise;
  }

  /* ---------- Dice ---------- */
  /* Shift held on a roll button rolls with advantage, Ctrl (or Cmd) with
     disadvantage: one extra die, the lowest or highest set aside. */
  const ROLL_KEYS_NOTE = "Shift: advantage · Ctrl: disadvantage";
  function rollModeOf(e) {
    if (!e) return "";
    if (e.shiftKey && !(e.ctrlKey || e.metaKey)) return "adv";
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey) return "dis";
    return "";
  }
  /* Note the hotkeys on every roll button's tooltip — at load, and again
     on injected HTML (hover previews), so built pages need no rebuild. */
  function noteRollKeys(root) {
    (root || document).querySelectorAll(".dice-roll, .pb-roll").forEach(function (b) {
      var t = b.getAttribute("title") || "";
      if (t.indexOf(ROLL_KEYS_NOTE) >= 0) return;
      b.setAttribute("title", (t ? t + " — " : "") + ROLL_KEYS_NOTE);
    });
  }
  window.noteRollKeys = noteRollKeys;

  /** Parse/roll NdS, NdS+M, NdS-M (e.g. d10+3, 1d4-1, 2d6).
      mode "adv"/"dis" rolls one die more and drops the lowest/highest. */
  function rollDice(expr, mode) {
    const cleaned = String(expr || "")
      .toLowerCase()
      .replace(/[–—]/g, "-")
      .replace(/\s+/g, "");
    const m = cleaned.match(/^(\d*)d(\d+)([+-]\d+)?$/);
    if (!m) return null;
    const n = m[1] === "" ? 1 : parseInt(m[1], 10);
    const sides = parseInt(m[2], 10);
    const mod = m[3] ? parseInt(m[3], 10) : 0;
    if (!n || n > 99 || !sides) return null;
    let total = 0;
    const parts = [];
    const extra = mode === "adv" || mode === "dis" ? 1 : 0;
    for (let i = 0; i < n + extra; i++) {
      parts.push(1 + Math.floor(Math.random() * sides));
    }
    const dropped = [];
    if (extra) {
      let at = 0;
      for (let i = 1; i < parts.length; i++) {
        if (mode === "adv" ? parts[i] < parts[at] : parts[i] > parts[at]) at = i;
      }
      dropped.push(parts.splice(at, 1)[0]);
    }
    for (let i = 0; i < parts.length; i++) total += parts[i];
    total += mod;
    const diceExpr = (n === 1 ? "" : String(n)) + "d" + sides;
    const modExpr = mod === 0 ? "" : mod > 0 ? "+" + mod : String(mod);
    return {
      total: total,
      parts: parts,
      dropped: dropped,
      mod: mod,
      expr: diceExpr + modExpr,
      n: n,
      sides: sides,
      note: extra ? (mode === "adv" ? "advantage" : "disadvantage") : "",
    };
  }

  const toast = document.getElementById("dice-toast");
  let toastTimer = null;

  /* The one toast: a roll's result, or a copied section link. */
  function showToast(inner, ms) {
    if (!toast) return;
    toast.innerHTML = inner;
    toast.hidden = false;
    // force reflow
    void toast.offsetWidth;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toast.classList.remove("show");
      setTimeout(function () {
        toast.hidden = true;
      }, 250);
    }, ms || 4400);
  }

  /* The clipboard, wherever the wiki is opened from. navigator.clipboard is
     refused outside a secure context, and off a disk this wiki is exactly
     that — so a refusal falls back to a hidden textarea and execCommand.
     Used by the section links and by the campaign join links. */
  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (err) {
      ok = false;
    }
    document.body.removeChild(ta);
    return ok ? Promise.resolve() : Promise.reject(new Error("copy refused"));
  }

  function copyText(text) {
    var api =
      navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText(text)
        : Promise.reject(new Error("no clipboard"));
    return api.catch(function () {
      return legacyCopy(text);
    });
  }

  function showDiceResult(result) {
    if (window.stonetopRollSound) {
      window.stonetopRollSound(
        (result.parts || []).length + (result.dropped || []).length
      );
    }
    if (!toast) return;
    var detail = "";
    if (result.parts.length > 1 || result.mod) {
      var bits = result.parts.join(" + ");
      if (result.mod) {
        bits +=
          result.mod > 0
            ? " + " + result.mod
            : " − " + Math.abs(result.mod);
      }
      detail = " (" + bits + ")";
    }
    // A die discarded for advantage/disadvantage is still shown, but out of
    // the sum — inside it, a struck 6 reads as another die that was counted.
    var aside = (result.dropped || []).length
      ? "dropped " + result.dropped.join(", ")
      : "";
    if (result.note) {
      aside = aside ? result.note + " · " + aside : result.note;
    }
    var note = aside
      ? ' <span class="roll-note">' + escapeHtml(aside) + "</span>"
      : "";
    showToast(
      '<span class="label">' +
        result.expr +
        "</span>" +
        detail +
        ' → <span class="result">' +
        result.total +
        "</span>" +
        note
    );
  }

  // When a roll-table's own dice button is rolled, highlight the row whose
  // number (or range, e.g. "5-6") contains the total.
  function highlightRollRow(btn, total) {
    var head = btn.closest(".roll-table-head");
    if (!head) return;
    var table = head.parentNode;
    if (!table || !table.classList || !table.classList.contains("roll-table")) {
      return;
    }
    var rows = table.querySelectorAll("tbody > tr");
    var matched = null;
    for (var i = 0; i < rows.length; i++) {
      rows[i].classList.remove("roll-hit");
      var th = rows[i].querySelector('th[scope="row"]');
      if (!th) continue;
      var m = th.textContent.trim().match(/^(\d+)(?:\s*[-–—]\s*(\d+))?$/);
      if (!m) continue;
      var lo = parseInt(m[1], 10);
      var hi = m[2] ? parseInt(m[2], 10) : lo;
      if (total >= lo && total <= hi) matched = rows[i];
    }
    if (matched) {
      // Retrigger the flash animation on repeat rolls.
      void matched.offsetWidth;
      matched.classList.add("roll-hit");
    }
  }

  document.addEventListener("click", function (e) {
    const btn = e.target.closest(".dice-roll");
    if (!btn) return;
    e.preventDefault();
    const result = rollDice(btn.getAttribute("data-dice"), rollModeOf(e));
    if (!result) return;
    btn.classList.remove("rolling");
    void btn.offsetWidth;
    btn.classList.add("rolling");
    showDiceResult(result);
    highlightRollRow(btn, result.total);
  });

  /* ------------------------------------------------------------------ *
   * Section links
   *
   * Every heading in the article carries a § that copies a link to that
   * section. Clicking the heading itself does the same — but not when the
   * click ends a selection: headings are read aloud and copied as text at
   * the table, and a drag across one must stay a drag.
   *
   * What lands on the clipboard is the published URL, which a page states
   * in og:url, rather than the address bar's — the wiki is opened off a
   * disk as often as it is served, and a link is copied to be pasted
   * somewhere else, where a path into someone's Dropbox is no use.
   * ------------------------------------------------------------------ */
  (function () {
    var body = document.body;
    var root =
      document.querySelector("main.content") ||
      document.querySelector(".site-main") ||
      (body && body.classList.contains("site-sheet") ? body : null);
    if (!root) return;
    // The home page's headings label card grids, not sections of an article.
    if (root.querySelector(".index-grid")) return;

    var pageUrl = (function () {
      var og = document.querySelector('meta[property="og:url"]');
      var stated = og ? og.getAttribute("content") || "" : "";
      return stated || String(location.href).split("#")[0];
    })();

    function idSlug(text) {
      return String(text || "")
        .toLowerCase()
        .replace(/[’']/g, "")
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 60);
    }

    /* What the link points at: the heading's own id, else the block it opens
       (a sheet numbers its rooms on the <article>, not on the <h2>), else one
       made from its words. Ids are handed out in document order, so a heading
       gets the same one on every load and a copied link keeps working. */
    function anchorId(h) {
      if (h.id) return h.id;
      var block = h.parentNode;
      if (block && block !== root && block.id && block.firstElementChild === h) {
        // A sheet numbers its rooms on the <article>, not on the <h2> — but
        // the container the whole article sits in is not this heading's id.
        return block.id;
      }
      // Sheets repeat a heading per room ("Exits"). Numbering those in
      // document order would shift every link below a room added later, so
      // name them for the block they sit in: room-3-exits.
      var ctx = block && block.closest ? block.closest("[id]") : null;
      var base = idSlug(h.textContent) || "section";
      if (ctx && ctx !== root && ctx.id) base = ctx.id + "-" + base;
      var id = base;
      for (var n = 2; document.getElementById(id); n++) id = base + "-" + n;
      h.id = id;
      return id;
    }

    function copyLink(h) {
      var id = anchorId(h);
      var url = pageUrl + "#" + id;
      copyText(url).then(
        function () {
          h.classList.remove("link-copied");
          void h.offsetWidth;
          h.classList.add("link-copied");
          showToast(
            '<span class="label">Link copied</span> ' +
              '<span class="roll-note">#' +
              escapeHtml(id) +
              "</span>",
            2600
          );
        },
        function () {
          // Nothing reached the clipboard — show the link to be taken by hand.
          showToast(
            '<span class="label">Copy this link</span> ' +
              '<span class="roll-note">' +
              escapeHtml(url) +
              "</span>",
            9000
          );
        }
      );
      // Put the section in the address bar without jumping the page to it.
      try {
        history.replaceState(null, "", "#" + id);
      } catch (err) {
        /* Some browsers refuse replaceState on file:// — the copy still stands. */
      }
    }

    /* A hub card's heading is already a link to the thing itself, and an
       arcanum's title belongs to its card face, not to a section. The § this
       adds is an <a> of our own — it must not disqualify its own heading. */
    function linkable(h) {
      if (h.querySelector("a:not(.section-link)")) return false;
      /* A stat block on a card face is a section of its own — a bound
         spirit is looked up and linked to like any monster. */
      if (h.classList.contains("stat-name")) return true;
      return !h.closest(".arcana-card");
    }

    var heads = root.querySelectorAll("h2, h3");
    for (var i = 0; i < heads.length; i++) {
      var h = heads[i];
      if (!linkable(h)) continue;
      var mark = document.createElement("a");
      mark.className = "section-link";
      mark.href = "#" + anchorId(h);
      mark.textContent = "§";
      mark.title = "Copy link to this section";
      mark.setAttribute(
        "aria-label",
        "Copy link to " + (h.textContent || "").trim()
      );
      h.appendChild(mark);
    }

    root.addEventListener("click", function (e) {
      var mark = e.target.closest(".section-link");
      var h = mark ? mark.parentNode : e.target.closest("h2, h3");
      if (!h || !root.contains(h) || !linkable(h)) return;
      if (!mark) {
        // Inside the heading, everything else keeps its own click …
        if (
          e.target.closest(
            "a, button, input, textarea, .wiki-q, .wiki-blank, .wiki-answer"
          )
        ) {
          return;
        }
        // … and a selection ending on the heading stays a selection.
        var sel = window.getSelection ? window.getSelection() : null;
        if (sel && !sel.isCollapsed) return;
      }
      e.preventDefault();
      copyLink(h);
    });
  })();

  /* ---------- The sound of the dice ----------
     Four takes of wooden dice thrown on a wooden table, by Wuzzy, CC0 (see
     audio/CREDITS.md). One is picked at random and detuned a little each
     time, so a run of rolls never sounds like the same recording twice.

     They are decoded through Web Audio where that works: it starts on the
     frame you ask rather than whenever the element gets round to it, and two
     rolls can overlap. But fetch() cannot read a file:// URL, and the wiki is
     just as often opened from disk as served — so off a disk it falls back to
     <audio> elements, which load local files perfectly well. (previews-data.js
     dodges the same rule by loading as a script rather than as JSON.)

     Nothing loads until the reader first touches the page, and a browser will
     not make a sound before then anyway — which a roll is. */
  (function () {
    var KEY = "stonetop-wiki-sound";
    var FILES = ["dice-1.mp3", "dice-2.mp3", "dice-3.mp3", "dice-4.mp3"];
    var VOLUME = 0.25; // both paths read this, so they never drift apart
    var ctx = null;
    var buffers = null;
    var loading = false;
    var els = null; // <audio> fallback, for file:// and for a failed decode
    var last = -1;
    var on = true;
    // Off a disk there is no point trying Web Audio: fetch refuses file://.
    var fromDisk = location.protocol === "file:";
    try {
      on = localStorage.getItem(KEY) !== "off";
    } catch (e) {}

    function audio() {
      if (ctx) return ctx;
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      try {
        ctx = new AC();
      } catch (e) {
        ctx = null;
      }
      return ctx;
    }

    function load(ac) {
      if (buffers || loading) return;
      loading = true;
      var base = SCRIPT_BASE || wikiRootPrefix();
      var got = [];
      var pending = FILES.length;
      FILES.forEach(function (name, i) {
        fetch(base + "audio/" + name)
          .then(function (r) {
            return r.ok ? r.arrayBuffer() : Promise.reject();
          })
          .then(function (data) {
            return new Promise(function (ok, no) {
              // Safari still wants the callback form.
              var p = ac.decodeAudioData(data, ok, no);
              if (p && p.then) p.then(ok, no);
            });
          })
          .then(function (buf) {
            got[i] = buf;
          })
          .catch(function () {})
          .then(function () {
            if (--pending === 0) {
              buffers = got.filter(Boolean);
              loading = false;
              // Nothing decoded — a stricter browser, or a bad path. The
              // elements will manage where the fetch would not.
              if (!buffers.length) loadElements();
            }
          });
      });
    }

    function audioUrl(name) {
      return (SCRIPT_BASE || wikiRootPrefix()) + "audio/" + name;
    }

    /** The fallback: one preloaded element per take, cloned to play. */
    function loadElements() {
      if (els) return;
      els = [];
      FILES.forEach(function (name) {
        try {
          var a = new Audio(audioUrl(name));
          a.preload = "auto";
          a.load();
          els.push(a);
        } catch (e) {}
      });
    }

    function pick(n) {
      var i = Math.floor(Math.random() * n);
      if (n > 1 && i === last) i = (i + 1) % n;
      last = i;
      return i;
    }

    function playElement() {
      loadElements();
      if (!els.length) return;
      // A clone per roll, so two rolls in quick succession do not cut each
      // other off. The file is in the cache by now, so it starts at once.
      var a = els[pick(els.length)].cloneNode();
      a.volume = VOLUME;
      // Vary the pitch with the speed, the way a real throw does — the
      // default is to hold the pitch, which sounds like a tape edit.
      a.preservesPitch = false;
      a.mozPreservesPitch = false;
      a.webkitPreservesPitch = false;
      a.playbackRate = 0.94 + Math.random() * 0.14;
      var p = a.play();
      if (p && p.catch) p.catch(function () {});
    }

    function play() {
      if (!on) return;
      if (fromDisk) {
        playElement();
        return;
      }
      var ac = audio();
      if (!ac) {
        playElement();
        return;
      }
      if (ac.state === "suspended" && ac.resume) ac.resume();
      if (!buffers) {
        load(ac);
        playElement(); // don't lose the first roll of a session
        return;
      }
      if (!buffers.length) {
        playElement();
        return;
      }
      var i = pick(buffers.length);

      var src = ac.createBufferSource();
      src.buffer = buffers[i];
      // A throw is never quite the same twice.
      src.playbackRate.value = 0.94 + Math.random() * 0.14;
      var vol = ac.createGain();
      vol.gain.value = VOLUME;
      src.connect(vol);
      vol.connect(ac.destination);
      src.start();
    }

    window.stonetopRollSound = play;

    /* Warm the samples on the first hint of interest, so the first roll of a
       session lands with the rest. */
    function warm() {
      if (!on) return;
      if (fromDisk) {
        loadElements();
        return;
      }
      if (buffers || loading) return;
      var ac = audio();
      if (ac) load(ac);
      else loadElements();
    }
    ["pointerdown", "keydown"].forEach(function (ev) {
      document.addEventListener(ev, warm, { once: true, passive: true });
    });

    var btn = document.getElementById("sound-toggle");
    function paint() {
      if (!btn) return;
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.classList.toggle("is-off", !on);
      btn.title = on ? "Dice sound on" : "Dice sound off";
    }
    paint();
    if (btn) {
      btn.addEventListener("click", function () {
        on = !on;
        try {
          localStorage.setItem(KEY, on ? "on" : "off");
        } catch (e) {}
        paint();
        if (on) play();
      });
    }
  })();

  /* ---------- Campaign panel ----------------------------------------------
   *
   * The sidebar's second footer control, beside the dice-sound toggle: a dot
   * saying whether this browser is sharing state with the table, and a small
   * panel behind it to set that up. Built here rather than emitted by the
   * generator, so it appears on every page — book chapters, arcana cards, and
   * the hand-authored site sheets alike — without a rebuild.
   *
   * The GM presses "Create campaign" once and sends the players the link it
   * copies. That is the whole of onboarding: they click it, the wiki reads
   * the campaign out of the address bar, adopts its state, and strips the
   * token back out of the URL.
   * --------------------------------------------------------------------- */
  (function () {
    var Store = window.StonetopStore;
    if (!Store) return;

    /* The deployed Worker, so nobody has to type it. An endpoint the reader
       has already used is remembered either way, and overrides this. */
    var DEFAULT_ENDPOINT = "https://sync.stonetop-wiki.workers.dev";

    /* The tools sit under the search box, at the top of the sidebar, where a
       glance finds them — whether the table is sharing state is the sort of
       thing you want to see without scrolling the topic list to its end.

       A site sheet has no search box; there the row goes under the sheet's
       title and its way back to the wiki. The old footer is the last resort,
       so a page with neither still gets the control. */
    function toolsHost() {
      var head = document.querySelector(".sidebar-head");
      if (head) return { parent: head, before: null };
      var nav = document.querySelector(".site-nav");
      if (nav) {
        var after =
          nav.querySelector(".nav-wiki-home") || nav.querySelector(".nav-title");
        return { parent: nav, before: after ? after.nextSibling : nav.firstChild };
      }
      var foot = document.querySelector(".sidebar-foot");
      return foot ? { parent: foot, before: null } : null;
    }

    /* The language list drops out of the sidebar and onto the body, the way
       the campaign panel does and for the same reason: the sidebar scrolls
       (overflow: auto), so anything positioned inside it is clipped at its
       edge — and the list is wider than the sidebar besides. On mobile the
       sidebar is transformed, which would make it the containing block for a
       fixed child and bring the clipping straight back, so the body is the
       only place this works in both layouts.

       <details> keeps the state and the keyboard behaviour; once its list is
       elsewhere in the DOM the element can no longer show and hide it, so
       that is done here off the toggle event. */
    function floatLangMenu(details) {
      var summary = details.querySelector("summary");
      var list = details.querySelector(".lang-list");
      if (!summary || !list) return;
      list.id = list.id || "lang-list";
      list.classList.add("lang-list-float");
      list.hidden = true;
      document.body.appendChild(list);
      summary.setAttribute("aria-controls", list.id);
      // A click inside must not count as a click outside.
      list.addEventListener("click", function (e) {
        e.stopPropagation();
      });

      // Measured after it is shown, so its real height is known, and clamped
      // into the viewport so a short window keeps all of it on screen.
      function place() {
        var r = summary.getBoundingClientRect();
        var w = list.offsetWidth;
        var h = list.offsetHeight;
        var left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
        var top = Math.min(r.bottom + 6, window.innerHeight - h - 8);
        list.style.left = Math.max(8, left) + "px";
        list.style.top = Math.max(8, top) + "px";
      }

      function sync() {
        if (!details.open) {
          list.hidden = true;
          return;
        }
        list.hidden = false;
        place();
      }

      details.addEventListener("toggle", sync);
      window.addEventListener("resize", function () {
        if (details.open) place();
      });
      window.addEventListener(
        "scroll",
        function () {
          if (details.open) place();
        },
        true
      );
      document.addEventListener("click", function (e) {
        if (details.open && !details.contains(e.target)) details.open = false;
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && details.open) {
          details.open = false;
          summary.focus();
        }
      });
      sync();
    }

    var host = toolsHost();
    if (!host) return;

    var tools = document.createElement("div");
    tools.className = "sidebar-tools";
    host.parent.insertBefore(tools, host.before);

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "sync-toggle";
    btn.id = "sync-toggle";
    btn.setAttribute("aria-expanded", "false");
    btn.innerHTML =
      '<span class="sync-dot" aria-hidden="true"></span>' +
      '<span class="sync-label">Campaign</span>';
    tools.appendChild(btn);

    /* The dice-sound toggle is generated into the sidebar footer, where it sat
       alone and adrift. Moved rather than re-made, so it keeps its id and the
       handler already bound to it — and so a rebuild, which puts it back in the
       footer, is picked up and moved again rather than leaving two of them. */
    var sound = document.getElementById("sound-toggle");
    if (sound) tools.appendChild(sound);

    /* The language switcher rides up with it, for the same reasons and by the
       same means: generated into the footer, moved here, so a rebuild is
       picked up rather than leaving two. Only pages that have a translation
       carry one. */
    var langs = document.querySelector(".sidebar-foot .lang-switch");
    if (langs) {
      tools.appendChild(langs);
      floatLangMenu(langs);
    }

    var panel = null;

    function label(text, node) {
      var l = document.createElement("label");
      l.className = "sync-field";
      var span = document.createElement("span");
      span.textContent = text;
      l.appendChild(span);
      l.appendChild(node);
      return l;
    }

    function input(placeholder, value) {
      var i = document.createElement("input");
      i.type = "text";
      i.spellcheck = false;
      i.autocomplete = "off";
      i.placeholder = placeholder;
      i.value = value || "";
      return i;
    }

    function action(text, kind) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "sync-action" + (kind ? " is-" + kind : "");
      b.textContent = text;
      return b;
    }

    function note(text) {
      var p = document.createElement("p");
      p.className = "sync-note";
      p.textContent = text;
      return p;
    }

    function say(text, ms) {
      showToast('<span class="label">' + escapeHtml(text) + "</span>", ms || 3200);
    }

    /* A link is copied to be pasted into a chat window, so a failed clipboard
       shows it instead of swallowing it. */
    function copyLink(link, what) {
      copyText(link).then(
        function () {
          say(what + " copied");
        },
        function () {
          showToast(
            '<span class="label">Copy this link</span> ' +
              '<span class="roll-note">' +
              escapeHtml(link) +
              "</span>",
            12000
          );
        }
      );
    }

    /* ---------- Export and import ----------------------------------------
     *
     * A campaign's state is a few flat maps, so a backup is those maps in a
     * file. What it deliberately does not carry is the endpoint or either
     * token: a file people mail around should not be a key to the campaign.
     *
     * Importing merges rather than replaces. Every key in the file wins over
     * what is held for that key, and anything the file does not mention is
     * left alone — so restoring a backup over a live campaign adds to it and
     * cannot silently empty it. Into a fresh campaign, merging over nothing is
     * exactly a restore.
     * ------------------------------------------------------------------- */

    function exportName(cfg) {
      var day = new Date().toISOString().slice(0, 10);
      return "stonetop-" + (cfg ? cfg.campaign : "local") + "-" + day + ".json";
    }

    function exportData() {
      var cfg = Store.config();
      var out = {
        stonetop: "campaign-export",
        version: 1,
        exported: new Date().toISOString(),
        campaign: cfg ? cfg.campaign : null,
        stores: {},
      };
      var keys = 0;
      Store.stores().forEach(function (name) {
        var data = Store.get(name);
        var n = Object.keys(data).length;
        if (!n) return;
        out.stores[name] = data;
        keys += n;
      });
      if (!keys) {
        say("Nothing to export yet");
        return;
      }
      var text = JSON.stringify(out, null, 2);
      var url = URL.createObjectURL(
        new Blob([text], { type: "application/json" })
      );
      var a = document.createElement("a");
      a.href = url;
      a.download = exportName(cfg);
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () {
        URL.revokeObjectURL(url);
      }, 2000);
      say(keys + " saved to " + a.download);
    }

    function importData(text) {
      var doc;
      try {
        doc = JSON.parse(text);
      } catch (e) {
        return { error: "that file is not JSON" };
      }
      if (!doc || doc.stonetop !== "campaign-export" || !doc.stores) {
        return { error: "that is not a campaign export" };
      }
      var cfg = Store.config();
      var added = 0;
      var touched = 0;
      var skipped = [];
      Object.keys(doc.stores).forEach(function (name) {
        var incoming = doc.stores[name];
        if (!incoming || typeof incoming !== "object") return;
        /* A store the wiki does not know, or one this role may not write —
           a player's browser has no business holding the enemies' HP. */
        if (!Store.scopeOf(name) || (cfg && !Store.syncs(name))) {
          skipped.push(name);
          return;
        }
        var cur = Store.get(name);
        var n = 0;
        Object.keys(incoming).forEach(function (k) {
          cur[k] = incoming[k];
          n++;
        });
        if (!n) return;
        Store.set(name, cur);
        touched++;
        added += n;
      });
      return { added: added, stores: touched, skipped: skipped };
    }

    /* The two buttons, on the panel whether a campaign is joined or not: a
       browser keeping its own copy is exactly the one worth backing up. */
    function addDataTools(body) {
      body.appendChild(note("Campaign data"));

      var row = document.createElement("div");
      row.className = "sync-row";

      var save = action("Export");
      save.title = "Save this campaign's state to a file";
      save.addEventListener("click", exportData);
      row.appendChild(save);

      var picker = document.createElement("input");
      picker.type = "file";
      picker.accept = "application/json,.json";
      picker.hidden = true;
      picker.addEventListener("change", function () {
        var file = picker.files && picker.files[0];
        if (!file) return;
        var reader = new FileReader();
        reader.onload = function () {
          var out = importData(String(reader.result || ""));
          picker.value = "";
          if (out.error) {
            say(out.error);
            return;
          }
          if (!out.added) {
            say("Nothing in that file to import");
            return;
          }
          var msg = "Imported " + out.added + " across " + out.stores + " stores";
          if (out.skipped.length) msg += " · skipped " + out.skipped.join(", ");
          say(msg, 6000);
        };
        reader.onerror = function () {
          say("Could not read that file");
        };
        reader.readAsText(file);
      });

      var load = action("Import");
      load.title = "Merge a saved file into this campaign";
      load.addEventListener("click", function () {
        picker.click();
      });
      row.appendChild(load);

      body.appendChild(row);
      body.appendChild(picker);
    }

    function buildConnected(body, cfg) {
      var head = document.createElement("p");
      head.className = "sync-head";
      head.textContent =
        cfg.role === "gm" ? "Sharing as GM" : "Sharing with the table";
      body.appendChild(head);

      var id = document.createElement("p");
      id.className = "sync-id";
      id.textContent = cfg.campaign;
      body.appendChild(id);

      if (cfg.role === "gm" && cfg.hasPlayerToken) {
        var player = action("Copy player link");
        player.addEventListener("click", function () {
          copyLink(Store.joinLink("player"), "Player link");
        });
        body.appendChild(player);
      }

      var mine = action(
        cfg.role === "gm" ? "Copy GM link" : "Copy my link"
      );
      mine.addEventListener("click", function () {
        copyLink(Store.joinLink(cfg.role), cfg.role === "gm" ? "GM link" : "Link");
      });
      body.appendChild(mine);

      if (cfg.role === "gm" && !cfg.hasPlayerToken) {
        body.appendChild(
          note(
            "This browser joined with a GM link, so it does not hold the " +
              "player token. Copy the player link from the browser that " +
              "created the campaign."
          )
        );
      }

      var off = action("Stop sharing", "quiet");
      off.addEventListener("click", function () {
        Store.disconnect();
        render();
        say("Sharing stopped — this browser keeps its own copy");
      });
      body.appendChild(off);

      body.appendChild(
        note(
          "Ticked improvements, danger clocks, answers, sheets and a " +
            "character's HP are shared with everyone. Enemy HP is the GM's " +
            "alone."
        )
      );

      addDataTools(body);
    }

    function buildDisconnected(body) {
      var head = document.createElement("p");
      head.className = "sync-head";
      head.textContent = "Share this campaign";
      body.appendChild(head);

      /* With the Worker's address compiled in there is nothing to decide, so
         the field is not shown — one button is the whole of setting up. It
         comes back on its own if DEFAULT_ENDPOINT is ever emptied, which is
         the only case where anyone would have something to type. */
      var endpoint = null;
      if (!DEFAULT_ENDPOINT) {
        endpoint = input("https://sync.stonetop-wiki.workers.dev", "");
        body.appendChild(label("Sync worker", endpoint));
      }

      var create = action("Create campaign", "primary");
      create.addEventListener("click", function () {
        var url = endpoint ? endpoint.value.trim() : DEFAULT_ENDPOINT;
        if (!url) {
          say("Enter the sync worker's address first");
          return;
        }
        create.disabled = true;
        create.textContent = "Creating…";
        Store.createCampaign(url).then(
          function () {
            create.disabled = false;
            create.textContent = "Create campaign";
            render();
            copyLink(Store.joinLink("player"), "Player link");
          },
          function () {
            create.disabled = false;
            create.textContent = "Create campaign";
            say("Could not reach the sync worker");
          }
        );
      });
      body.appendChild(create);

      body.appendChild(note("Or paste the link the GM sent you:"));
      var link = input("https://…#join=…", "");
      body.appendChild(label("Join link", link));

      var join = action("Join");
      join.addEventListener("click", function () {
        var parsed = Store.parseJoinLink(link.value.trim());
        if (!parsed) {
          say("That does not look like a join link");
          return;
        }
        Store.connect(parsed);
        render();
        say("Joined " + parsed.campaign);
      });
      body.appendChild(join);

      addDataTools(body);
    }

    function render() {
      if (!panel) return;
      var body = panel.querySelector(".sync-body");
      body.innerHTML = "";
      var cfg = Store.config();
      if (cfg) buildConnected(body, cfg);
      else buildDisconnected(body);
    }

    function ensurePanel() {
      if (panel) return panel;
      panel = document.createElement("div");
      panel.className = "sync-panel";
      panel.hidden = true;
      panel.innerHTML = '<div class="sync-body"></div>';
      document.body.appendChild(panel);
      // A click inside must not count as a click outside.
      panel.addEventListener("click", function (e) {
        e.stopPropagation();
      });
      return panel;
    }

    /* Dropped from under the button rather than pinned to a corner: the button
       moved to the top of the sidebar, and a panel anchored to the bottom of
       the viewport would have no visible relationship to it. Measured after it
       is shown, so its real height is known, and clamped into the viewport so
       a narrow window keeps all of it on screen. */
    function place() {
      var r = btn.getBoundingClientRect();
      var w = panel.offsetWidth;
      var h = panel.offsetHeight;
      var left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
      var top = Math.min(r.bottom + 8, window.innerHeight - h - 8);
      panel.style.left = Math.max(8, left) + "px";
      panel.style.top = Math.max(8, top) + "px";
    }

    function open() {
      ensurePanel();
      render();
      panel.hidden = false;
      place();
      btn.setAttribute("aria-expanded", "true");
    }

    function close() {
      if (!panel) return;
      panel.hidden = true;
      btn.setAttribute("aria-expanded", "false");
    }

    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (panel && !panel.hidden) close();
      else open();
    });
    document.addEventListener("click", close);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
    });

    /* The dot, and nothing louder. A table mid-session does not need a dialog
       telling it the wifi dropped; the state will catch up when it comes back. */
    Store.onStatus(function (state) {
      btn.classList.toggle("is-on", state === "ok");
      btn.classList.toggle("is-offline", state === "offline");
      btn.title =
        state === "ok"
          ? "Sharing this campaign with the table"
          : state === "offline"
          ? "Sync worker unreachable — changes are saved here and will catch up"
          : "This browser keeps its own copy";
    });

    if (Store.justJoined()) {
      var cfg = Store.config();
      say("Joined " + (cfg ? cfg.campaign : "the campaign"), 5000);
    }
  })();

  /* ---------- Hover previews ---------- */
  const bubble = document.getElementById("wiki-preview");
  let hideTimer = null;
  let activeLink = null;
  let pageMap = null;

  // Native title tooltips fight the custom popup; deep links use data-slug /
  // data-fragment / href only. Strip titles on wiki links (incl. injected HTML).
  function stripWikiLinkTitles(root) {
    var scope = root || document;
    var links = scope.querySelectorAll
      ? scope.querySelectorAll("a.wiki-link[title]")
      : [];
    for (var i = 0; i < links.length; i++) {
      links[i].removeAttribute("title");
    }
  }
  stripWikiLinkTitles(document);
  if (typeof MutationObserver === "function") {
    var titleStripObs = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var nodes = muts[i].addedNodes;
        for (var j = 0; j < nodes.length; j++) {
          var n = nodes[j];
          if (n.nodeType !== 1) continue;
          if (n.matches && n.matches("a.wiki-link[title]")) {
            n.removeAttribute("title");
          }
          if (n.querySelectorAll) stripWikiLinkTitles(n);
        }
      }
    });
    titleStripObs.observe(document.documentElement, {
      childList: true,
      subtree: true,
    });
  }

  function loadPageMap() {
    if (pageMap) return Promise.resolve(pageMap);
    if (typeof window.WIKI_PAGE_MAP === "object" && window.WIKI_PAGE_MAP) {
      pageMap = window.WIKI_PAGE_MAP;
      return Promise.resolve(pageMap);
    }
    return loadPreviews().then(function () {
      pageMap = window.WIKI_PAGE_MAP || {};
      return pageMap;
    });
  }

  function hidePreview() {
    if (!bubble) return;
    bubble.classList.remove("visible");
    activeLink = null;
    setTimeout(function () {
      if (!bubble.classList.contains("visible")) bubble.hidden = true;
    }, 150);
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hidePreview, 280);
  }

  function cancelHide() {
    clearTimeout(hideTimer);
    hideTimer = null;
  }

  function positionPreview(link) {
    if (!bubble) return;
    const rect = link.getBoundingClientRect();
    const margin = 10;
    const bw = bubble.offsetWidth || 520;
    const bh = bubble.offsetHeight || 320;
    // Nudge right so the bubble doesn't sit on the link / neighbors
    // and block hover to adjacent targets.
    let left = rect.left + 20;
    // Overlap the link slightly so the cursor can cross into the bubble
    let top = rect.bottom + 2;

    if (left + bw > window.innerWidth - margin) {
      left = window.innerWidth - bw - margin;
    }
    if (left < margin) left = margin;

    if (top + bh > window.innerHeight - margin) {
      top = rect.top - bh - 2;
    }
    if (top < margin) top = margin;

    bubble.style.left = left + "px";
    bubble.style.top = top + "px";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** Turn "(page 12)" / "pages 8-11" / "Book II, page 270" in excerpt text into wiki links. */
  function linkifyPageRefs(text, map, book) {
    var rawMap = map || {};
    function resolveBookMap(raw, bookId) {
      if (!raw) return {};
      if (raw.book1 || raw.book2) {
        return raw[bookId || "book2"] || {};
      }
      return raw;
    }
    function bookIdFromToken(tok) {
      var t = String(tok || "").toLowerCase();
      if (t === "i" || t === "1") return "book1";
      if (t === "ii" || t === "2") return "book2";
      return null;
    }
    var defaultBook = book || "book2";
    var mapDefault = resolveBookMap(rawMap, defaultBook);
    var escaped = escapeHtml(text || "No summary available.");

    function pageHref(slug) {
      return wikiRootPrefix() + slug + ".html";
    }

    function normSection(name) {
      return String(name || "")
        .toLowerCase()
        .replace(/[\"'“”‘’]/g, "")
        .replace(/^(a|an|the)\s+/, "")
        .replace(/[^a-z0-9]+/g, " ")
        .replace(/\s+/g, " ")
        .trim();
    }

    function resolveFragment(info, label) {
      if (!info || !info.sections || !label) return "";
      var keys = [normSection(label)];
      var k0 = keys[0];
      if (k0.endsWith("s") && k0.length > 3) keys.push(k0.slice(0, -1));
      else if (k0) keys.push(k0 + "s");
      for (var i = 0; i < keys.length; i++) {
        if (info.sections[keys[i]]) return info.sections[keys[i]];
      }
      return "";
    }

    function linkForPage(num, label, bookId) {
      var m = bookId ? resolveBookMap(rawMap, bookId) : mapDefault;
      var info = m[String(num)];
      if (!info || !info.slug) {
        return escapeHtml(label || "page " + num);
      }
      var frag = resolveFragment(info, label);
      var href = pageHref(info.slug) + (frag ? "#" + frag : "");
      // No native title tooltip — hover uses the wiki-preview popup instead.
      return (
        '<a class="wiki-link" href="' +
        href +
        '" data-slug="' +
        escapeHtml(info.slug) +
        '"' +
        (frag ? ' data-fragment="' + escapeHtml(frag) + '"' : "") +
        ">" +
        escapeHtml(label || info.title || "page " + num) +
        "</a>"
      );
    }

    // Cross-book: Title (Book II, page 270) / Book I, page 245
    escaped = escaped.replace(
      /(?:([A-Za-z][A-Za-z0-9'’\-]*(?:\s+[A-Za-z][A-Za-z0-9'’\-]*){0,6})\s+)?\(?Book\s*(II|I|2|1)\s*[,:]?\s*(?:starting\s+on\s+)?pages?\s+([\d,\s\-–—]+)\)?/gi,
      function (_m, title, bookTok, spec) {
        var bid = bookIdFromToken(bookTok);
        var nums = String(spec).match(/\d+/g) || [];
        if (!bid || !nums.length) return _m;
        var primary = linkForPage(nums[0], title || null, bid);
        if (nums.length === 1) return primary;
        var extras = nums
          .slice(1)
          .map(function (n) {
            return linkForPage(n, null, bid);
          })
          .join(" · ");
        return primary + " (" + extras + ")";
      }
    );

    // Title (page 12) / Title (pages 8-11) / Title (page 282, 350)
    escaped = escaped.replace(
      /([A-Za-z][A-Za-z0-9'’\-]*(?:\s+[A-Za-z][A-Za-z0-9'’\-]*){0,6})\s+\((?:see\s+)?pages?\s+([\d,\s\-–—]+)\)/gi,
      function (_m, title, spec) {
        var nums = String(spec).match(/\d+/g) || [];
        if (!nums.length) return _m;
        var primary = linkForPage(nums[0], title);
        if (nums.length === 1) return primary;
        var extras = nums
          .slice(1)
          .map(function (n) {
            return linkForPage(n);
          })
          .join(" · ");
        return primary + " (" + extras + ")";
      }
    );

    // Remaining (page 12) / (pages 8-11)
    escaped = escaped.replace(
      /\((?:see\s+)?pages?\s+([\d,\s\-–—]+)\)/gi,
      function (_m, spec) {
        var nums = String(spec).match(/\d+/g) || [];
        if (!nums.length) return _m;
        return nums.map(function (n) { return linkForPage(n); }).join(" · ");
      }
    );

    // Bare "page 12" / "pages 8-11" (avoid matching mid-word / already-linked)
    escaped = escaped.replace(
      /(^|[^A-Za-z0-9_\/])((?:see\s+)?pages?\s+)([\d,\s\-–—]+)(?![A-Za-z0-9_\/])/gi,
      function (_m, pre, _label, spec) {
        var nums = String(spec).match(/\d+/g) || [];
        if (!nums.length) return _m;
        return pre + nums.map(function (n) { return linkForPage(n); }).join(" · ");
      }
    );

    return escaped;
  }

  function showMissingPreview(link, message) {
    if (!bubble) return;
    activeLink = link;
    bubble.classList.remove("pv-arcana");
    var title =
      (link.textContent || "").replace(/\s+/g, " ").trim() ||
      link.getAttribute("data-slug") ||
      "Wiki link";
    var msg =
      message ||
      "Preview data is missing. Build the Stonetop wiki to enable hover previews.";
    bubble.innerHTML =
      '<p class="pv-title">' +
      escapeHtml(title) +
      "</p>" +
      '<div class="pv-body"><p class="pv-excerpt pv-missing">' +
      escapeHtml(msg) +
      "</p></div>";
    bubble.hidden = false;
    positionPreview(link);
    void bubble.offsetWidth;
    bubble.classList.add("visible");
  }

  function showPreview(link, data, map) {
    if (!bubble || !data) return;
    activeLink = link;
    var frag = link.getAttribute("data-fragment") || "";
    // Support href="#id" / "page.html#id" even without data-fragment
    if (!frag) {
      var href = link.getAttribute("href") || "";
      var hash = href.indexOf("#");
      if (hash !== -1) frag = href.slice(hash + 1);
    }

    var section =
      frag && data.sections && data.sections[frag] ? data.sections[frag] : null;

    // Arcana are one card with two faces (Discovery + Power). A per-face
    // section extracts incomplete (the power side comes out empty), so always
    // show the whole stored card — that lists both faces and carries its own
    // titles, so there is nothing to duplicate.
    var isArcana =
      data.kind === "arcana" ||
      (data.html && data.html.indexOf('class="arcana-card"') !== -1);

    if (isArcana && data.html) {
      bubble.classList.add("pv-arcana");
      bubble.innerHTML =
        '<div class="pv-body pv-full pv-card">' + data.html + "</div>";
    } else if (section && section.html) {
      // Deep-link target (stat block, item block, or section body). The block
      // already begins with its own title, so don't repeat it as a pv-title.
      bubble.classList.remove("pv-arcana");
      bubble.innerHTML =
        '<div class="pv-body pv-full">' + section.html + "</div>";
    } else if (data.html) {
      // Full page body for arcana cards (and anything that stores html)
      bubble.classList.add("pv-arcana");
      bubble.innerHTML =
        '<div class="pv-body pv-full pv-card">' + data.html + "</div>";
    } else {
      bubble.classList.remove("pv-arcana");
      let thumb = "";
      if (data.image) {
        thumb =
          '<img class="pv-thumb" src="' +
          SCRIPT_BASE +
          "images/" +
          data.image +
          '" alt="">';
      }
      bubble.innerHTML =
        '<p class="pv-title">' +
        escapeHtml(data.title || "") +
        "</p>" +
        '<div class="pv-body">' +
        thumb +
        '<p class="pv-excerpt">' +
        linkifyPageRefs(
          data.excerpt || "No summary available.",
          map,
          data.book || "book2"
        ) +
        "</p></div>";
    }
    // Bind checkboxes in dynamically injected preview HTML (arcana unlocks, etc.)
    var previewSlug =
      link.getAttribute("data-slug") ||
      (function () {
        var h = link.getAttribute("href") || "";
        var m = h.replace(/\\/g, "/").match(/([^\/#]+)\.html/i);
        return m ? m[1] : "";
      })();
    if (previewSlug && typeof window.bindWikiChecks === "function") {
      window.bindWikiChecks(bubble, previewSlug);
    }
    /* A stat block in a popup gets the same HP track as on its own page. */
    if (previewSlug && typeof window.bindHpTrackers === "function") {
      window.bindHpTrackers(bubble, previewSlug);
      noteRollKeys(bubble);
    }
    bubble.hidden = false;
    positionPreview(link);
    void bubble.offsetWidth;
    bubble.classList.add("visible");
  }

  document.addEventListener("mouseover", function (e) {
    // Ignore links inside the preview bubble for *switching* target,
    // but still cancel hide so the bubble stays interactive.
    if (bubble && bubble.contains(e.target)) {
      cancelHide();
      return;
    }
    const link = e.target.closest("a.wiki-link");
    if (!link) return;
    // No hover previews for sidebar navigation links
    if (link.closest(".sidebar, #sidebar, nav.toc")) return;
    // Only trigger from main content links with a slug
    const slug = link.getAttribute("data-slug");
    if (!slug) return;
    cancelHide();
    Promise.all([loadPreviews(), loadPageMap()])
      .then(function (pair) {
        var data = pair[0];
        var map = pair[1];
        if (!link.matches(":hover") && !(bubble && bubble.matches(":hover")))
          return;
        if (!data || typeof data !== "object" || !data[slug]) {
          var empty = !data || typeof data !== "object" || !Object.keys(data).length;
          showMissingPreview(
            link,
            empty
              ? "Preview data is missing. Build the Stonetop wiki to enable hover previews."
              : "No preview entry for this link."
          );
          return;
        }
        showPreview(link, data[slug], map);
      })
      .catch(function () {
        if (!link.matches(":hover") && !(bubble && bubble.matches(":hover")))
          return;
        showMissingPreview(
          link,
          "Preview data is missing. Build the Stonetop wiki to enable hover previews."
        );
      });
  });

  document.addEventListener("mouseout", function (e) {
    if (bubble && bubble.contains(e.target)) {
      var relB = e.relatedTarget;
      if (relB && bubble.contains(relB)) return;
      if (relB && activeLink && activeLink.contains(relB)) return;
      scheduleHide();
      return;
    }
    const link = e.target.closest("a.wiki-link");
    if (!link) return;
    const related = e.relatedTarget;
    if (related && (link.contains(related) || (bubble && bubble.contains(related)))) {
      return;
    }
    scheduleHide();
  });

  if (bubble) {
    bubble.addEventListener("mouseenter", cancelHide);
    bubble.addEventListener("mouseleave", function (e) {
      var rel = e.relatedTarget;
      if (rel && activeLink && activeLink.contains(rel)) return;
      scheduleHide();
    });
  }

  document.addEventListener("scroll", function () {
    if (activeLink && bubble && bubble.classList.contains("visible")) {
      positionPreview(activeLink);
    }
  }, true);

  window.addEventListener("blur", hidePreview);

  /* ---------- Sidebar search (titles + full page text) ---------- */
  const filter = document.getElementById("nav-filter");
  const navList = document.getElementById("nav-list");
  const searchResults = document.getElementById("search-results");
  let searchIndex = null;
  let searchIndexPromise = null;

  function loadSearchIndex() {
    if (searchIndex) return Promise.resolve(searchIndex);
    if (typeof window.WIKI_SEARCH_INDEX === "object" && window.WIKI_SEARCH_INDEX) {
      searchIndex = window.WIKI_SEARCH_INDEX;
      return Promise.resolve(searchIndex);
    }
    if (searchIndexPromise) return searchIndexPromise;
    searchIndexPromise = new Promise(function (resolve) {
      const s = document.createElement("script");
      s.src = SCRIPT_BASE + "js/search-index.js";
      s.onload = function () {
        searchIndex = window.WIKI_SEARCH_INDEX || [];
        resolve(searchIndex);
      };
      s.onerror = function () {
        searchIndex = [];
        resolve(searchIndex);
      };
      document.head.appendChild(s);
    });
    return searchIndexPromise;
  }

  function pageHrefFromSlug(slug) {
    return wikiRootPrefix() + slug + ".html";
  }

  /** Hrefs from the index are wiki-root relative; re-base for the caller. */
  function hrefFromRoot(href) {
    return wikiRootPrefix() + href;
  }

  function escapeHtmlSearch(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function queryTerms(q) {
    return String(q || "")
      .toLowerCase()
      .split(/\s+/)
      .filter(function (t) {
        return t.length > 0;
      });
  }

  function haystackHasTerms(hay, terms) {
    if (!terms.length) return true;
    for (var i = 0; i < terms.length; i++) {
      if (hay.indexOf(terms[i]) === -1) return false;
    }
    return true;
  }

  function makeSnippet(text, terms, radius) {
    radius = radius || 42;
    if (!text) return "";
    var low = text.toLowerCase();
    var best = -1;
    for (var i = 0; i < terms.length; i++) {
      var at = low.indexOf(terms[i]);
      if (at !== -1 && (best === -1 || at < best)) best = at;
    }
    if (best < 0) {
      var head = text.slice(0, radius * 2);
      return escapeHtmlSearch(head) + (text.length > head.length ? "…" : "");
    }
    var start = Math.max(0, best - radius);
    var end = Math.min(text.length, best + radius + 12);
    var slice = text.slice(start, end);
    var esc = escapeHtmlSearch(slice);
    // Highlight each term (case-insensitive)
    for (var t = 0; t < terms.length; t++) {
      var term = terms[t];
      if (!term) continue;
      var re = new RegExp(
        "(" + term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")",
        "gi"
      );
      esc = esc.replace(re, "<mark>$1</mark>");
    }
    return (start > 0 ? "…" : "") + esc + (end < text.length ? "…" : "");
  }

  function positionSearchResults() {
    var resultsEl = searchResults;
    if (!resultsEl || resultsEl.hidden || !filter) return;
    var r = filter.getBoundingClientRect();
    var gap = 6;
    var maxW = Math.min(36 * 16, window.innerWidth - 20);
    var width = Math.max(r.width, maxW);
    // Prefer aligning under the search box; keep fully on-screen
    var left = r.left;
    if (left + width > window.innerWidth - 10) {
      left = Math.max(10, window.innerWidth - width - 10);
    }
    if (left < 10) left = 10;
    var top = r.bottom + gap;
    var maxH = Math.min(window.innerHeight * 0.5, 28 * 16);
    if (top + 120 > window.innerHeight) {
      // Flip above the box if near the bottom
      top = Math.max(10, r.top - gap - Math.min(maxH, 240));
    }
    resultsEl.style.left = left + "px";
    resultsEl.style.top = top + "px";
    resultsEl.style.width = width + "px";
    resultsEl.style.maxHeight = maxH + "px";
  }

  function runSearch(q) {
    if (!filter || !navList) return;
    var terms = queryTerms(q);
    var resultsEl = searchResults;

    if (!terms.length) {
      if (resultsEl) {
        resultsEl.hidden = true;
        resultsEl.innerHTML = "";
      }
      navList.querySelectorAll("li").forEach(function (li) {
        li.classList.remove("hidden");
      });
      return;
    }

    loadSearchIndex().then(function (index) {
      // Re-check query in case it changed while loading
      var current = filter.value.trim();
      if (queryTerms(current).join(" ") !== terms.join(" ")) return;

      var hits = [];
      var matchSlugs = {};
      for (var i = 0; i < index.length; i++) {
        var doc = index[i];
        var titleHay = (doc.title || "").toLowerCase();
        var textHay = (doc.text || "").toLowerCase();
        var titleHit = haystackHasTerms(titleHay, terms);
        var textHit = haystackHasTerms(textHay, terms);
        if (!titleHit && !textHit) continue;
        matchSlugs[String(doc.slug).toLowerCase()] = true;
        var rank = titleHit ? 0 : 1;
        // Prefer denser title matches
        if (titleHit && titleHay === terms.join(" ")) rank = -1;
        hits.push({
          slug: doc.slug,
          href: doc.href || null,
          title: doc.title,
          book: doc.book,
          titleHit: titleHit,
          textHit: textHit,
          rank: rank,
          snippet: textHit
            ? makeSnippet(doc.text || "", terms)
            : escapeHtmlSearch(doc.excerpt || doc.title || ""),
        });
      }
      hits.sort(function (a, b) {
        if (a.rank !== b.rank) return a.rank - b.rank;
        return (a.title || "").localeCompare(b.title || "");
      });

      // Filter sidebar nav: keep items whose title matches OR slug is a content hit
      navList.querySelectorAll(":scope > li").forEach(function (li) {
        if (li.classList.contains("nav-book-label")) {
          li.classList.remove("hidden");
          return;
        }
        var a = li.querySelector(":scope > a");
        var href = a ? a.getAttribute("href") || "" : "";
        var slugMatch = href.match(/([^\/]+)\.html/i);
        // Site sheets carry their index slug explicitly — their file name
        // doesn't have to match it.
        var slug = (a && a.getAttribute("data-nav-slug")) || "";
        if (!slug) slug = slugMatch ? slugMatch[1].toLowerCase() : "";
        var titleText = (li.textContent || "").toLowerCase();
        var titleMatch = haystackHasTerms(titleText, terms);
        var contentMatch = !!(slug && matchSlugs[slug]);
        var show = titleMatch || contentMatch;
        li.classList.toggle("hidden", !show);

        li.querySelectorAll("li.nav-section").forEach(function (sub) {
          var st = (sub.textContent || "").toLowerCase();
          var parentHit = titleMatch || contentMatch;
          var subHit = haystackHasTerms(st, terms);
          sub.classList.toggle("hidden", !subHit && !parentHit);
          if (subHit) li.classList.remove("hidden");
        });
      });

      // Hide book labels with no visible children after filter
      var labels = navList.querySelectorAll(":scope > li.nav-book-label");
      labels.forEach(function (lab) {
        var next = lab.nextElementSibling;
        var any = false;
        while (next && !next.classList.contains("nav-book-label")) {
          if (!next.classList.contains("hidden")) {
            any = true;
            break;
          }
          next = next.nextElementSibling;
        }
        lab.classList.toggle("hidden", !any);
      });

      if (!resultsEl) return;
      if (!hits.length) {
        resultsEl.hidden = false;
        resultsEl.innerHTML =
          '<p class="search-empty">No pages match “' +
          escapeHtmlSearch(current) +
          '”.</p>';
        positionSearchResults();
        return;
      }
      var maxShow = 40;
      var html = [];
      html.push(
        '<p class="search-results-meta">' +
          hits.length +
          (hits.length === 1 ? " page" : " pages") +
          (hits.length > maxShow ? " (showing " + maxShow + ")" : "") +
          "</p>"
      );
      for (var h = 0; h < hits.length && h < maxShow; h++) {
        var hit = hits[h];
        var where =
          hit.book === "book1"
            ? "Book I"
            : hit.book === "book2"
              ? "Book II"
              : hit.book === "sites"
                ? "Site"
                : "";
        if (hit.titleHit && hit.textHit) where += (where ? " · " : "") + "title + text";
        else if (hit.titleHit) where += (where ? " · " : "") + "title";
        else where += (where ? " · " : "") + "in text";
        html.push(
          '<a class="search-hit" href="' +
            (hit.href ? hrefFromRoot(hit.href) : pageHrefFromSlug(hit.slug)) +
            '">' +
            '<span class="search-hit-title">' +
            escapeHtmlSearch(hit.title) +
            "</span>" +
            (where
              ? '<span class="search-hit-where">' +
                escapeHtmlSearch(where) +
                "</span>"
              : "") +
            '<span class="search-hit-snippet">' +
            hit.snippet +
            "</span></a>"
        );
      }
      resultsEl.hidden = false;
      resultsEl.innerHTML = html.join("");
      positionSearchResults();
    });
  }

  if (filter && navList) {
    var searchTimer = null;
    filter.addEventListener("input", function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () {
        runSearch(filter.value.trim());
      }, 120);
    });
    window.addEventListener("resize", positionSearchResults);
    window.addEventListener(
      "scroll",
      function () {
        if (searchResults && !searchResults.hidden) positionSearchResults();
      },
      true
    );
    // Prefetch index so first search is snappy
    loadSearchIndex();
  }

  const toggle = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("sidebar");
  if (toggle && sidebar) {
    toggle.addEventListener("click", function () {
      sidebar.classList.toggle("open");
      document.body.classList.toggle("sidebar-open", sidebar.classList.contains("open"));
    });
    document.addEventListener("click", function (e) {
      if (!sidebar.classList.contains("open")) return;
      if (sidebar.contains(e.target) || toggle.contains(e.target)) return;
      sidebar.classList.remove("open");
      document.body.classList.remove("sidebar-open");
    });
  }

  /* Keep sidebar scroll position across in-wiki navigations */
  (function () {
    var KEY = "stonetop-wiki-sidebar-scroll";
    var el = document.getElementById("sidebar");
    if (!el) return;

    function saveScroll() {
      try {
        sessionStorage.setItem(KEY, String(el.scrollTop));
      } catch (e) {}
    }

    /* The page you are on has to be findable in the nav. The saved position
       belongs to the page you came from, so it can leave the current entry
       off-screen entirely — reveal it when it is, and otherwise leave the
       sidebar where the reader left it. */
    function revealCurrent() {
      var li = el.querySelector(".toc li.current");
      if (!li) return;
      var mark = li.querySelector("a") || li;
      var box = el.getBoundingClientRect();
      var r = mark.getBoundingClientRect();
      if (!r.height) return; // hidden (filtered, or off-canvas on mobile)
      var head = el.querySelector(".sidebar-head");
      // The head is sticky, so it covers the top of the scrolled content
      var inset = head ? head.getBoundingClientRect().height : 0;
      var top = r.top - box.top + el.scrollTop;
      var seenTop = el.scrollTop + inset;
      var seenBottom = el.scrollTop + el.clientHeight;
      if (top >= seenTop && top + r.height <= seenBottom) return;
      var room = el.clientHeight - inset - r.height;
      var target = top - inset - (room > 0 ? room / 2 : 0);
      var most = el.scrollHeight - el.clientHeight;
      el.scrollTop = Math.max(0, Math.min(target, most));
    }

    function restoreScroll() {
      var y = null;
      try {
        var raw = sessionStorage.getItem(KEY);
        if (raw != null && raw !== "") {
          var n = parseInt(raw, 10);
          if (isFinite(n) && n >= 0) y = n;
        }
      } catch (e) {}

      function settle() {
        if (y != null) el.scrollTop = y;
        revealCurrent();
      }
      settle();
      // Layout/fonts can shift — re-apply after paint
      requestAnimationFrame(settle);
      setTimeout(settle, 50);
    }

    restoreScroll();

    var saveTimer = null;
    el.addEventListener(
      "scroll",
      function () {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(saveScroll, 80);
      },
      { passive: true }
    );

    // Save immediately when following a nav link (before unload)
    el.addEventListener("click", function (e) {
      var a = e.target.closest("a[href]");
      if (!a || !el.contains(a)) return;
      saveScroll();
    });

    window.addEventListener("pagehide", saveScroll);
    window.addEventListener("beforeunload", saveScroll);
  })();

  /* ---------- Persistent requirement checkboxes ----------
     Steading improvements and danger countdowns. Shared with the whole table
     when a campaign is configured — watching Marshedge's Fire fill up is the
     point — so these go through StonetopStore rather than localStorage. */
  (function () {
    var KEY = "stonetop-wiki-checks";
    var Store = window.StonetopStore;

    function loadState() {
      return Store.get(KEY);
    }
    function saveState(state) {
      Store.set(KEY, state);
    }

    /** Page slug from a path or href, e.g. .../minor-ice-weaving.html → minor-ice-weaving */
    function slugFromPath(path) {
      var m = String(path || "")
        .replace(/\\/g, "/")
        .match(/([^\/#]+)\.html/i);
      return m ? m[1] : "";
    }

    function storageKey(pageSlug, checkId) {
      return pageSlug + "#" + checkId;
    }

    /** True if this check is marked under pageSlug. */
    function isChecked(state, pageSlug, checkId) {
      return !!state[storageKey(pageSlug, checkId)];
    }

    /**
     * Restore + bind wiki checkboxes under root for a logical page slug.
     * Safe to call on dynamically injected preview HTML.
     */
    function bindWikiChecks(root, pageSlug) {
      if (!root || !pageSlug) return;
      var state = loadState();
      root.querySelectorAll("input.wiki-check[data-check-id]").forEach(function (box) {
        var id = box.getAttribute("data-check-id");
        if (!id) return;
        // Already bound for this slug — still refresh checked state
        var full = storageKey(pageSlug, id);
        box.checked = isChecked(state, pageSlug, id);
        box.setAttribute("data-check-page", pageSlug);
        if (box.getAttribute("data-check-bound") === pageSlug) return;
        box.setAttribute("data-check-bound", pageSlug);
        box.addEventListener("change", function () {
          var st = loadState();
          if (box.checked) st[full] = true;
          else delete st[full];
          saveState(st);
          repaint();
        });
      });
    }

    /* Every bound box, re-read from the store. This is the fan-out that used
       to run only over the same page+id (a box shown both on the page and in
       a hover popup) — written this way it also repaints a tick that arrived
       from someone else at the table. */
    function repaint() {
      var state = loadState();
      document
        .querySelectorAll("input.wiki-check[data-check-id][data-check-page]")
        .forEach(function (box) {
          box.checked = isChecked(
            state,
            box.getAttribute("data-check-page"),
            box.getAttribute("data-check-id")
          );
        });
    }

    Store.subscribe(KEY, repaint);
    window.bindWikiChecks = bindWikiChecks;

    // Bind checks on the current page at load
    var currentSlug = slugFromPath(location.pathname);
    if (currentSlug) bindWikiChecks(document, currentSlug);
    noteRollKeys(document);
  })();

  /* ---------- Map pins + labels (Maps page) ---------- */
  (function () {
    var strip = document.querySelector(".maps-strip");
    if (!strip) return;
    var KEY = "stonetop-wiki-map-pins";
    var Store = window.StonetopStore;

    var store = Store.get(KEY);
    function persist() {
      Store.set(KEY, store);
    }
    function pinsFor(mapId) {
      return store[mapId] || (store[mapId] = []);
    }

    var adding = false;
    var dragging = false;
    var activeColor = "#e2534a";
    var addBtn = document.getElementById("map-add");

    function setAdding(on) {
      adding = on;
      if (addBtn) addBtn.setAttribute("aria-pressed", on ? "true" : "false");
      document.body.classList.toggle("pins-adding", on);
    }

    function renderPin(canvas, mapId, pin) {
      var el = document.createElement("div");
      el.className = "map-pin";
      el.style.left = pin.x * 100 + "%";
      el.style.top = pin.y * 100 + "%";
      el.style.setProperty("--pin-color", pin.color);

      var dot = document.createElement("button");
      dot.type = "button";
      dot.className = "pin-dot";
      dot.title = "Drag to move";

      // A contenteditable span grows to fit its text, so the full label is
      // always visible when you are not editing (an <input> cropped it).
      var label = document.createElement("span");
      label.className = "pin-label";
      label.contentEditable = "true";
      label.setAttribute("role", "textbox");
      label.setAttribute("data-placeholder", "label");
      label.textContent = pin.label || "";

      var del = document.createElement("button");
      del.type = "button";
      del.className = "pin-del";
      del.title = "Delete pin";
      del.textContent = "×";

      el.appendChild(dot);
      el.appendChild(label);
      el.appendChild(del);
      canvas.appendChild(el);

      // Keep clicks on the pin from reaching the canvas (add/open handlers).
      el.addEventListener("mousedown", function (e) {
        e.stopPropagation();
      });
      el.addEventListener("click", function (e) {
        e.stopPropagation();
      });
      el.addEventListener("dblclick", function (e) {
        e.stopPropagation();
      });
      label.addEventListener("input", function () {
        pin.label = label.textContent;
        persist();
      });
      label.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          label.blur();
        }
      });
      del.addEventListener("click", function (e) {
        e.stopPropagation();
        e.preventDefault();
        var arr = pinsFor(mapId);
        var idx = arr.indexOf(pin);
        if (idx >= 0) arr.splice(idx, 1);
        if (arr.length === 0) delete store[mapId];
        persist();
        el.remove();
        // Deleting exits add/edit mode so the next click doesn't drop a pin.
        setAdding(false);
      });
      dot.addEventListener("mousedown", function (e) {
        e.preventDefault();
        e.stopPropagation();
        dragging = true;
        function move(ev) {
          var r = canvas.getBoundingClientRect();
          var x = (ev.clientX - r.left) / r.width;
          var y = (ev.clientY - r.top) / r.height;
          pin.x = Math.min(1, Math.max(0, x));
          pin.y = Math.min(1, Math.max(0, y));
          el.style.left = pin.x * 100 + "%";
          el.style.top = pin.y * 100 + "%";
        }
        function up() {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
          dragging = false;
          persist();
        }
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
      return el;
    }

    var canvases = strip.querySelectorAll(".map-canvas");

    /* Draw every map's pins from scratch. Called once at load, and again
       whenever pins arrive from someone else at the table — but never while a
       pin is being dragged or its label typed into, since redrawing would
       take the element out from under the hand holding it. */
    function renderAll(next) {
      if (next) store = next;
      if (dragging) return;
      var active = document.activeElement;
      if (active && active.classList && active.classList.contains("pin-label"))
        return;
      canvases.forEach(function (canvas) {
        canvas.querySelectorAll(".map-pin").forEach(function (el) {
          el.remove();
        });
        var mapId = canvas.getAttribute("data-map");
        (store[mapId] || []).forEach(function (pin) {
          renderPin(canvas, mapId, pin);
        });
      });
    }

    canvases.forEach(function (canvas) {
      var mapId = canvas.getAttribute("data-map");
      (store[mapId] || []).forEach(function (pin) {
        renderPin(canvas, mapId, pin);
      });
      canvas.addEventListener("click", function (e) {
        if (!adding) return;
        // Never treat a click on an existing pin (e.g. its × delete button)
        // as a request to add a new one.
        if (e.target.closest(".map-pin")) return;
        var r = canvas.getBoundingClientRect();
        var pin = {
          x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
          y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
          color: activeColor,
          label: "",
        };
        pinsFor(mapId).push(pin);
        persist();
        var el = renderPin(canvas, mapId, pin);
        var inp = el.querySelector(".pin-label");
        if (inp) inp.focus();
      });
      // Open the full-resolution campaign map on double-click.
      canvas.addEventListener("dblclick", function () {
        var full = canvas.getAttribute("data-full");
        if (full && !adding) window.open(full, "_blank");
      });
    });

    // Toolbar wiring
    if (addBtn) {
      addBtn.addEventListener("click", function () {
        setAdding(!adding);
      });
    }
    var swatches = Array.prototype.slice.call(
      document.querySelectorAll(".map-color")
    );
    function selectColor(btn) {
      activeColor = btn.getAttribute("data-color");
      swatches.forEach(function (s) {
        s.setAttribute("aria-pressed", s === btn ? "true" : "false");
      });
    }
    swatches.forEach(function (btn) {
      btn.addEventListener("click", function () {
        selectColor(btn);
        // Picking a colour is a strong signal you want to place a pin.
        if (!adding) setAdding(true);
      });
    });
    if (swatches.length) selectColor(swatches[0]);

    Store.subscribe(KEY, renderAll);
  })();

  /* Scroll to #fragment targets inside the horizontal multi-column pane */
  function scrollToFragment() {
    var id = (location.hash || "").replace(/^#/, "");
    if (!id) return;
    var el = document.getElementById(id);
    if (!el) return;
    var scroll = document.querySelector(".content-scroll");
    var elRect, scRect;
    if (scroll && scroll.scrollWidth > scroll.clientWidth + 2) {
      // Multi-column pane: place the element near the left of the viewport
      elRect = el.getBoundingClientRect();
      scRect = scroll.getBoundingClientRect();
      scroll.scrollLeft += elRect.left - scRect.left - 24;
    } else if (scroll && scroll.scrollHeight > scroll.clientHeight + 2) {
      // One column, scrolling vertically (phones)
      elRect = el.getBoundingClientRect();
      scRect = scroll.getBoundingClientRect();
      scroll.scrollTop += elRect.top - scRect.top - 16;
    } else {
      el.scrollIntoView({ block: "start", inline: "nearest" });
    }
  }
  window.addEventListener("DOMContentLoaded", scrollToFragment);
  window.addEventListener("hashchange", scrollToFragment);
  // Also handle in-page fragment clicks
  document.addEventListener("click", function (e) {
    var a = e.target.closest("a[href*='#']");
    if (!a) return;
    var href = a.getAttribute("href") || "";
    // same-page fragment only needs delayed scroll after navigation
    if (href.charAt(0) === "#") {
      setTimeout(scrollToFragment, 0);
    }
  });

  /* Horizontal wheel scroll on the multi-column content area */
  document.querySelectorAll(".content-scroll").forEach(function (el) {
    el.addEventListener(
      "wheel",
      function (e) {
        // Index page uses vertical flow — leave native scroll alone
        if (el.scrollWidth <= el.clientWidth + 2) return;
        // Map vertical wheel (and trackpad) to horizontal scroll
        var dx = e.deltaX;
        var dy = e.deltaY;
        if (Math.abs(dy) >= Math.abs(dx) && dy !== 0) {
          e.preventDefault();
          el.scrollLeft += dy;
        } else if (dx !== 0) {
          // already horizontal gesture — keep default unless we need to force
          e.preventDefault();
          el.scrollLeft += dx;
        }
      },
      { passive: false }
    );
  });

  /* ---------- Clickable questions & blanks (GM answer notes) ----------
     Every "?" in the article body, and every run of underscores the book sets
     as a fill-in-the-blank, becomes a click target. Clicking a "?" opens a
     note box under the question; clicking a blank turns it into an inline
     field you type the answer into. Both persist in localStorage, keyed by a
     hash of the containing block's text, so a wiki rebuild keeps the answers. */
  (function () {
    var KEY = "stonetop-wiki-notes";
    var Store = window.StonetopStore;
    /* A question mark, or two-plus underscores (the book's blank line). */
    var TARGET_RE = /\?|_{2,}/g;
    /* Never rewrite type that is already interactive, code-ish, or chrome. */
    var SKIP_SEL =
      "a,button,input,textarea,select,script,style,code,pre,kbd," +
      ".sidebar,nav,.toc,.search-results,.wiki-preview,.dice-toast," +
      ".wiki-note,.wiki-q,.wiki-blank,.wiki-answer,.map-pin,.site-map";
    var HOST_SEL =
      "li,td,th,dd,dt,blockquote,figcaption,p,h1,h2,h3,h4,h5,h6,div,section,article";
    /* Hosts a note is appended inside of; anything else gets it as a sibling. */
    var HOST_INSIDE = /^(LI|TD|TH|DD|BLOCKQUOTE|DIV|SECTION|ARTICLE|FIGCAPTION)$/;

    function loadAll() {
      return Store.get(KEY);
    }
    function saveAll(o) {
      Store.set(KEY, o);
    }
    function setNote(key, val) {
      var s = loadAll();
      if (val && val.trim()) s[key] = val;
      else delete s[key];
      saveAll(s);
    }
    function getNote(key) {
      var v = loadAll()[key];
      return typeof v === "string" ? v : "";
    }

    function pageSlug() {
      var m = String(location.pathname || "")
        .replace(/\\/g, "/")
        .match(/([^\/#?]+)\.html/i);
      var slug = m ? m[1] : "index";
      /* Site sheets live in their own folder and can share a slug with a
         book page, so qualify them. */
      if (/\/sites\//i.test(location.pathname)) slug = "sites/" + slug;
      return slug;
    }

    /* djb2 - stable across rebuilds as long as the block's words don't change. */
    function hash(str) {
      var h = 5381,
        i = str.length;
      while (i) h = ((h * 33) ^ str.charCodeAt(--i)) >>> 0;
      return h.toString(36);
    }
    function normalize(s) {
      return String(s || "").replace(/\s+/g, " ").trim().slice(0, 400);
    }

    var root =
      document.querySelector("main.content") ||
      document.querySelector("main") ||
      (document.body && document.body.classList.contains("site-sheet")
        ? document.body
        : null);
    if (!root) return;

    var slug = pageSlug();
    var notes = loadAll();
    var blockHashes = new WeakMap();
    var blockCounts = new WeakMap();

    function blockOf(node) {
      var el = node.parentElement;
      while (el && el !== root && !el.matches(HOST_SEL)) el = el.parentElement;
      return el || root;
    }
    function hashOf(block) {
      var h = blockHashes.get(block);
      if (!h) {
        h = hash(normalize(block.textContent));
        blockHashes.set(block, h);
      }
      return h;
    }
    function nextKey(block, kind) {
      var n = blockCounts.get(block) || 0;
      blockCounts.set(block, n + 1);
      return slug + "#" + hashOf(block) + ":" + kind + n;
    }
    function sel(key) {
      return '[data-note-key="' + key.replace(/["\\]/g, "\\$&") + '"]';
    }

    /* ----- markers ----- */

    function makeMarker(kind, raw, key) {
      var s = document.createElement("span");
      s.className = kind === "q" ? "wiki-q" : "wiki-blank";
      s.setAttribute("data-note-key", key);
      s.setAttribute("role", "button");
      s.setAttribute("tabindex", "0");
      var val = notes[key];
      if (kind === "q") {
        s.textContent = raw;
        s.title = val ? "Your note - click to edit" : "Click to answer or note";
        if (val) s.classList.add("has-note");
      } else {
        s.setAttribute("data-blank", raw);
        if (val) {
          s.textContent = val;
          s.classList.add("is-filled");
          s.title = "Click to edit";
        } else {
          s.textContent = raw;
          s.title = "Click to fill in";
        }
      }
      return s;
    }

    /* ----- blanks: edit in place ----- */

    function editBlank(span) {
      if (span.classList.contains("is-editing")) return;
      var key = span.getAttribute("data-note-key");
      var blank = span.getAttribute("data-blank") || "___";
      var before = span.classList.contains("is-filled") ? span.textContent : "";
      var input = document.createElement("input");
      input.type = "text";
      input.className = "wiki-blank-input";
      input.value = before;
      span.textContent = "";
      span.classList.add("is-editing");
      span.appendChild(input);

      function size() {
        input.size = Math.max(input.value.length + 1, blank.length + 1, 6);
      }
      size();
      input.focus();
      input.select();

      var done = false;
      function finish(save) {
        if (done) return;
        done = true;
        var val = save ? input.value.trim() : before;
        span.classList.remove("is-editing");
        span.textContent = val || blank;
        span.classList.toggle("is-filled", !!val);
        span.title = val ? "Click to edit" : "Click to fill in";
        if (save) setNote(key, val);
      }
      input.addEventListener("input", size);
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          finish(true);
        } else if (e.key === "Escape") {
          e.preventDefault();
          finish(false);
        }
      });
      input.addEventListener("blur", function () {
        finish(true);
      });
    }

    /* ----- question marks: a note box while you type, inline text after -----
       The box is only ever the editor. As soon as it loses focus the answer
       collapses into the sentence itself, set in wine italics beside the
       question it answers. */

    function autoGrow(ta) {
      ta.style.height = "auto";
      ta.style.height = Math.max(ta.scrollHeight, 22) + "px";
    }

    function answerOf(key) {
      return root.querySelector(".wiki-answer" + sel(key));
    }

    /** Show the saved answer inline after its question mark (or clear it). */
    function renderAnswer(mark, key) {
      var val = getNote(key).trim();
      var el = answerOf(key);
      if (!val) {
        if (el) el.remove();
        mark.classList.remove("has-note");
        mark.title = "Click to answer or note";
        return null;
      }
      if (!el) {
        el = document.createElement("span");
        el.className = "wiki-answer";
        el.setAttribute("data-note-key", key);
        el.setAttribute("role", "button");
        el.setAttribute("tabindex", "0");
        mark.parentNode.insertBefore(el, mark.nextSibling);
      }
      el.textContent = val;
      el.title = "Your note - click to edit";
      mark.classList.add("has-note");
      mark.title = "Your note - click to edit";
      return el;
    }

    function createNote(mark, key, focus) {
      var host = mark.closest(HOST_SEL) || root;
      /* The inline answer and its editor never show at once. */
      var inline = answerOf(key);
      if (inline) inline.remove();
      var box = document.createElement("div");
      box.className = "wiki-note";
      box.setAttribute("data-note-key", key);

      var ta = document.createElement("textarea");
      ta.className = "wiki-note-input";
      ta.rows = 1;
      ta.placeholder = "Answer / note...";
      ta.value = getNote(key);

      var del = document.createElement("button");
      del.type = "button";
      del.className = "wiki-note-del";
      del.title = "Delete note";
      del.setAttribute("aria-label", "Delete note");
      del.textContent = "\u00d7";

      box.appendChild(ta);
      box.appendChild(del);
      if (HOST_INSIDE.test(host.tagName)) host.appendChild(box);
      else host.parentNode.insertBefore(box, host.nextSibling);
      autoGrow(ta);

      ta.addEventListener("input", function () {
        autoGrow(ta);
        setNote(key, ta.value);
        mark.classList.toggle("has-note", !!ta.value.trim());
      });
      ta.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
          e.preventDefault();
          ta.blur();
        }
      });
      /* Taking the box out from under a focused textarea fires blur mid-removal,
         so the teardown has to be idempotent. */
      var closed = false;
      function closeEditor() {
        if (closed) return;
        closed = true;
        box.remove();
        renderAnswer(mark, key);
      }
      box.close = closeEditor;

      ta.addEventListener("blur", closeEditor);
      /* Keep the textarea's blur from removing the box before the click lands. */
      del.addEventListener("mousedown", function (e) {
        e.preventDefault();
      });
      del.addEventListener("click", function (e) {
        e.stopPropagation();
        setNote(key, "");
        closeEditor();
      });
      if (focus) ta.focus();
      return box;
    }

    /** Click a question mark (or its answer): open the editor, or close it. */
    function toggleNote(mark) {
      var key = mark.getAttribute("data-note-key");
      var open = root.querySelector(".wiki-note" + sel(key));
      if (open) {
        if (open.close) open.close();
        else open.remove();
        return;
      }
      createNote(mark, key, true);
    }

    function markFor(key) {
      return root.querySelector(".wiki-q" + sel(key));
    }

    /* ----- walk the article and wrap every target ----- */

    function collectTextNodes() {
      var out = [];
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode: function (n) {
          var v = n.nodeValue;
          if (!v || (v.indexOf("?") < 0 && v.indexOf("__") < 0))
            return NodeFilter.FILTER_REJECT;
          var el = n.parentElement;
          while (el && el !== root) {
            if (el.matches(SKIP_SEL)) return NodeFilter.FILTER_REJECT;
            el = el.parentElement;
          }
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      var n;
      while ((n = walker.nextNode())) out.push(n);
      return out;
    }

    collectTextNodes().forEach(function (tn) {
      var text = tn.nodeValue;
      var block = blockOf(tn);
      var frag = document.createDocumentFragment();
      var last = 0;
      var m;
      TARGET_RE.lastIndex = 0;
      while ((m = TARGET_RE.exec(text))) {
        if (m.index > last)
          frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        var kind = m[0] === "?" ? "q" : "b";
        frag.appendChild(makeMarker(kind, m[0], nextKey(block, kind)));
        last = m.index + m[0].length;
      }
      if (!last) return;
      if (last < text.length)
        frag.appendChild(document.createTextNode(text.slice(last)));
      tn.parentNode.replaceChild(frag, tn);
    });

    /* Answers saved earlier read as part of the page, not as open editors. */
    root.querySelectorAll(".wiki-q.has-note").forEach(function (q) {
      renderAnswer(q, q.getAttribute("data-note-key"));
    });

    /** The question mark, its inline answer, and a blank all open an editor. */
    function activate(el) {
      if (el.classList.contains("wiki-blank")) {
        editBlank(el);
        return;
      }
      var mark = el.classList.contains("wiki-answer")
        ? markFor(el.getAttribute("data-note-key"))
        : el;
      if (mark) toggleNote(mark);
    }

    root.addEventListener("click", function (e) {
      var el = e.target.closest && e.target.closest(".wiki-q,.wiki-blank,.wiki-answer");
      if (!el || !root.contains(el)) return;
      if (el.classList.contains("is-editing")) return;
      e.preventDefault();
      e.stopPropagation();
      activate(el);
    });

    root.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.key !== " ") return;
      var el = e.target.closest && e.target.closest(".wiki-q,.wiki-blank,.wiki-answer");
      if (!el || el.classList.contains("is-editing")) return;
      e.preventDefault();
      activate(el);
    });

    /* An answer typed at the other end of the table. Anything open in front of
       someone — a blank being edited, a note box with the cursor in it — is
       left alone; the rest is re-read from the store. */
    Store.subscribe(KEY, function (saved) {
      root.querySelectorAll(".wiki-blank[data-note-key]").forEach(function (span) {
        if (span.classList.contains("is-editing")) return;
        var key = span.getAttribute("data-note-key");
        var val = typeof saved[key] === "string" ? saved[key] : "";
        var blank = span.getAttribute("data-blank") || "___";
        span.textContent = val || blank;
        span.classList.toggle("is-filled", !!val);
        span.title = val ? "Click to edit" : "Click to fill in";
      });
      root.querySelectorAll(".wiki-q[data-note-key]").forEach(function (q) {
        var key = q.getAttribute("data-note-key");
        if (root.querySelector(".wiki-note" + sel(key))) return; // open editor
        renderAnswer(q, key);
      });
    });
  })();

  /* ---------- Playbook sheets: write-in boxes and debilities ----------
     The sheet's boxes (stats, damage, HP, the name box) keep what you type
     in the same store the answer notes use — it is the same kind of thing,
     the reader's own hand over the book's. The three debilities are ordinary
     wiki checkboxes, so they persist with every other check on the page; all
     this adds is the link the sheet draws between a debility and the pair of
     stats it dims. */
  (function () {
    var KEY = "stonetop-wiki-notes";
    var Store = window.StonetopStore;

    function loadAll() {
      return Store.get(KEY);
    }
    /* A value back at the sheet's own default is not worth a row: absent, the
       box falls back to it anyway, and the campaign store stays small. */
    function saveField(key, val, def) {
      var s = loadAll();
      if (val && val.trim() && val !== def) s[key] = val;
      else delete s[key];
      Store.set(KEY, s);
    }

    /* A box on a sheet is never left blank: emptied, it falls back to what
       the sheet starts at (no stat assigned, full health, first level). */
    function fallback(input) {
      return input.getAttribute("data-default") || "";
    }

    /** Written the way the sheet writes it: a stat always carries its sign. */
    function format(input, n) {
      if (input.getAttribute("data-spin-sign") && n >= 0) return "+" + n;
      return String(n);
    }

    function bounds(input) {
      var lo = parseInt(input.getAttribute("data-spin-min"), 10);
      var hi = parseInt(input.getAttribute("data-spin-max"), 10);
      return [isFinite(lo) ? lo : -99, isFinite(hi) ? hi : 99];
    }

    /** Settle what was typed into a number in range — or the default. */
    function normalize(input, save) {
      var raw = String(input.value || "").replace(/[\u2212\u2013\u2014]/g, "-");
      var n = parseInt(raw.replace(/[^\d-]/g, ""), 10);
      if (!isFinite(n)) {
        input.value = fallback(input);
      } else {
        var b = bounds(input);
        n = Math.max(b[0], Math.min(b[1], n));
        input.value = format(input, n);
      }
      input.setAttribute("aria-valuenow", input.value);
      if (save)
        saveField(
          input.getAttribute("data-field-key"),
          input.value,
          fallback(input)
        );
    }

    function step(input, by) {
      var raw = String(input.value || "").replace(/[\u2212\u2013\u2014]/g, "-");
      var n = parseInt(raw.replace(/[^\d-]/g, ""), 10);
      if (!isFinite(n)) n = 0;
      var b = bounds(input);
      input.value = format(input, Math.max(b[0], Math.min(b[1], n + by)));
      input.setAttribute("aria-valuenow", input.value);
      saveField(input.getAttribute("data-field-key"), input.value, fallback(input));
    }

    /* ----- Where an arcanum is -----
       Under the card, a Location box: which PC holds it, or where in the
       world it lies. The nine playbooks are offered as a list and anything
       else can be typed. It lives in the notes store under <slug>:location,
       shared with the table, and each playbook page reads the store back to
       list the arcana held by that class. Runtime-only: nothing is generated. */
    var PLAYBOOKS = [
      ["the-blessed", "The Blessed"],
      ["the-fox", "The Fox"],
      ["the-heavy", "The Heavy"],
      ["the-judge", "The Judge"],
      ["the-lightbearer", "The Lightbearer"],
      ["the-marshal", "The Marshal"],
      ["the-ranger", "The Ranger"],
      ["the-seeker", "The Seeker"],
      ["the-would-be-hero", "The Would-Be Hero"],
    ];
    var LOC_SUFFIX = ":location";

    function pageSlug() {
      var m = String(location.pathname || "")
        .replace(/\\/g, "/")
        .match(/([^\/#?]+)\.html/i);
      return m ? m[1] : "index";
    }

    function esc(t) {
      return String(t).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }

    (function () {
      var cards = document.querySelectorAll(".content .arcana-card");
      if (cards.length !== 1) return; // an arcanum's own page, not a hub
      var card = cards[0];
      var key = pageSlug() + LOC_SUFFIX;
      var OTHER = "—";
      var wrap = document.createElement("div");
      wrap.className = "arcana-location";
      /* A real <select>, not a datalist: a datalist filters its options by
         what is typed, so once a name is chosen the list shows that one
         entry and nothing else. The last choice opens a box for anywhere
         else — a place in the world, an NPC. The box is the field that is
         saved; the select is a way of filling it. */
      wrap.innerHTML =
        '<div class="fs-field"><span class="fs-label">Location</span>' +
        '<select class="wiki-field arcana-location-pick" aria-label="Who holds it">' +
        '<option value="">— unknown —</option>' +
        PLAYBOOKS.map(function (p) {
          return '<option value="' + esc(p[1]) + '">' + esc(p[1]) + "</option>";
        }).join("") +
        '<option value="' + OTHER + '">Elsewhere / someone else…</option>' +
        "</select>" +
        '<input type="text" class="wiki-field arcana-location-box" ' +
        'data-field-key="' + esc(key) + '" ' +
        'placeholder="Where it lies, or who holds it" autocomplete="off" ' +
        'aria-label="Location" hidden></div>';
      card.parentNode.insertBefore(wrap, card.nextSibling);
      var pick = wrap.querySelector("select");
      var box = wrap.querySelector("input");

      function isPlaybook(v) {
        return PLAYBOOKS.some(function (p) {
          return p[1] === v;
        });
      }
      /* The select follows the box: a saved playbook name selects itself,
         anything else opens the box with it. */
      function reflect() {
        var v = String(box.value || "").trim();
        if (!v) {
          pick.value = "";
          box.hidden = true;
        } else if (isPlaybook(v)) {
          pick.value = v;
          box.hidden = true;
        } else {
          pick.value = OTHER;
          box.hidden = false;
        }
      }
      pick.addEventListener("change", function () {
        if (pick.value === OTHER) {
          if (isPlaybook(box.value) || !box.value) box.value = "";
          box.hidden = false;
          box.focus();
          return;
        }
        box.value = pick.value;
        box.hidden = true;
        box.dispatchEvent(new Event("input", { bubbles: true }));
      });
      box.addEventListener("blur", function () {
        if (!box.value) reflect();
      });
      /* After the field binding below has filled the box from the store. */
      setTimeout(reflect, 0);
      Store.subscribe(KEY, function () {
        if (document.activeElement !== box) setTimeout(reflect, 0);
      });
    })();

    (function () {
      var title = document.querySelector("h1.pb-title");
      var stats = document.querySelector(".pb-stats");
      if (!title || !stats) return;
      var slug = pageSlug();
      var name = title.textContent.trim().toLowerCase();
      var bare = name.replace(/^the\s+/, "");

      function held(v) {
        v = String(v || "").trim().toLowerCase();
        if (!v) return false;
        return v === name || v === bare || v === slug || v === "the " + bare;
      }

      function titleOf(arcSlug, previews) {
        var p = previews && previews[arcSlug];
        if (p && p.title) return String(p.title).replace(/^\d+\.\s*/, "");
        return arcSlug
          .replace(/^(minor|major)-/, "")
          .replace(/-/g, " ")
          .replace(/\b\w/g, function (c) {
            return c.toUpperCase();
          });
      }

      var box = document.createElement("section");
      box.className = "pb-arcana";
      box.innerHTML =
        '<h2 id="arcana">Arcana</h2>' +
        '<ul class="pb-arcana-list"></ul>' +
        '<p class="fs-muted pb-arcana-empty">None held. Set an arcanum’s ' +
        "Location to “" + esc(title.textContent.trim()) + "” to list it here.</p>";
      stats.parentNode.insertBefore(box, stats.nextSibling);
      var list = box.querySelector("ul");
      var empty = box.querySelector(".pb-arcana-empty");

      function render() {
        var saved = loadAll() || {};
        var slugs = Object.keys(saved)
          .filter(function (k) {
            return k.slice(-LOC_SUFFIX.length) === LOC_SUFFIX && held(saved[k]);
          })
          .map(function (k) {
            return k.slice(0, -LOC_SUFFIX.length);
          })
          .sort();
        var paint = function (previews) {
          list.innerHTML = slugs
            .map(function (a) {
              return (
                '<li><a class="wiki-link" href="' + esc(a) + '.html" data-slug="' + esc(a) + '">' +
                esc(titleOf(a, previews)) + "</a></li>"
              );
            })
            .join("");
          empty.hidden = slugs.length > 0;
        };
        if (!slugs.length) return paint(null);
        loadPreviews().then(paint, function () { paint(null); });
      }
      render();
      Store.subscribe(KEY, render);
    })();

    /* A textarea grows to hold what is in it — a one-line box on the sheet
       opens out as the list gets long, and comes up the right size when the
       text arrives from the store; the corner resizer is off (Chrome cannot
       drag it inside a multicol layout), so this is how a box gets taller. */
    function fit(el) {
      if (!el || el.tagName !== "TEXTAREA") return;
      el.style.height = "auto";
      var h = el.scrollHeight;
      if (h > 0) el.style.height = h + 2 + "px";
    }

    var fields = document.querySelectorAll(
      "input.wiki-field[data-field-key], textarea.wiki-field[data-field-key]"
    );
    if (fields.length) {
      var saved = loadAll();
      fields.forEach(function (input) {
        var key = input.getAttribute("data-field-key");
        if (!key) return;
        var spin = input.hasAttribute("data-spin-min");
        if (typeof saved[key] === "string") input.value = saved[key];
        if (!input.value) input.value = fallback(input);
        if (spin) normalize(input, false);
        fit(input);

        input.addEventListener("input", function () {
          /* Save what is being typed; settle it when the box is left. */
          saveField(key, input.value, fallback(input));
          fit(input);
        });
        input.addEventListener("blur", function () {
          if (spin) normalize(input, true);
          else if (!input.value) {
            input.value = fallback(input);
            saveField(key, input.value, fallback(input));
          }
        });
        if (!spin) return;
        input.addEventListener("keydown", function (e) {
          if (e.key === "ArrowUp") {
            e.preventDefault();
            step(input, 1);
          } else if (e.key === "ArrowDown") {
            e.preventDefault();
            step(input, -1);
          } else if (e.key === "Enter") {
            normalize(input, true);
          }
        });
      });
    }

    document.addEventListener("click", function (e) {
      var btn = e.target.closest && e.target.closest(".pb-spin-step");
      if (!btn) return;
      e.preventDefault();
      var input = btn.closest(".pb-spin").querySelector("input.wiki-field");
      if (input) step(input, parseInt(btn.getAttribute("data-step"), 10) || 0);
    });
    /* mousedown default would blur the box mid-step */
    document.addEventListener("mousedown", function (e) {
      if (e.target.closest && e.target.closest(".pb-spin-step")) {
        e.preventDefault();
      }
    });

    /* ----- rolling off the sheet -----
       A stat rolls 2d6 plus what is in its box. A debility means the roll is
       made with disadvantage — an extra die, the highest discarded — so a
       marked debility rolls the stats it brackets that way without the
       reader having to remember. */

    function d6() {
      return 1 + Math.floor(Math.random() * 6);
    }

    /** The debility marked against this stat, if any. */
    function debilityOn(abbr) {
      var found = null;
      document.querySelectorAll(".pb-debility[data-stats]").forEach(function (l) {
        if (found) return;
        var names = (l.getAttribute("data-stats") || "").split(/\s+/);
        var box = l.querySelector("input[type=checkbox]");
        if (names.indexOf(abbr) >= 0 && box && box.checked) {
          var name = l.querySelector(".pb-debility-name");
          found = name ? name.textContent.trim() : "debilitated";
        }
      });
      return found;
    }

    function rollStat(btn, mode) {
      var cell = btn.closest(".pb-stat");
      if (!cell) return;
      var abbr = cell.getAttribute("data-stat") || "";
      var input = cell.querySelector("input.wiki-field");
      var mod = parseInt((input && input.value) || "0", 10);
      if (!isFinite(mod)) mod = 0;
      var deb = debilityOn(abbr);
      // A debility is disadvantage; Shift (advantage) held against it cancels
      // out to a plain roll, Ctrl on top of it is still one die dropped.
      var note = "";
      if (deb && mode === "adv") {
        mode = "";
        note = "advantage cancels " + deb;
      } else if (deb) {
        mode = "dis";
        note = "disadvantage — " + deb;
      } else if (mode) {
        note = mode === "adv" ? "advantage" : "disadvantage";
      }
      var rolls = [d6(), d6()];
      var dropped = [];
      if (mode) {
        rolls.push(d6());
        var at = 0;
        for (var i = 1; i < rolls.length; i++) {
          if (mode === "adv" ? rolls[i] < rolls[at] : rolls[i] > rolls[at]) at = i;
        }
        dropped = rolls.splice(at, 1);
      }
      var total = rolls[0] + rolls[1] + mod;
      showDiceResult({
        expr: "roll +" + abbr,
        parts: rolls,
        dropped: dropped,
        mod: mod,
        total: total,
        note: note,
      });
    }

    function rollDamage(btn, mode) {
      var track = btn.closest(".pb-track");
      var input = track && track.querySelector("input.wiki-field");
      var expr = ((input && input.value) || "").trim();
      if (!expr) expr = btn.getAttribute("data-roll-damage") || "";
      var result = rollDice(expr, mode);
      if (!result) return;
      result.expr = "damage " + result.expr;
      showDiceResult(result);
    }

    document.addEventListener("click", function (e) {
      if (!e.target.closest) return;
      var btn = e.target.closest(".pb-roll");
      if (!btn) {
        // Anywhere in the box rolls it — everything but the box you type in
        // and the steppers beside it.
        if (e.target.closest("input, .pb-spin-step, a")) return;
        var cell = e.target.closest(".pb-stat, .pb-track");
        btn = cell && cell.querySelector(".pb-roll");
        if (!btn) return;
      }
      e.preventDefault();
      btn.classList.remove("rolling");
      void btn.offsetWidth;
      btn.classList.add("rolling");
      if (btn.hasAttribute("data-roll-stat")) rollStat(btn, rollModeOf(e));
      else rollDamage(btn, rollModeOf(e));
    });

    /* The sheet's own boxes, when someone else fills them in. A box being
       typed into is left alone. */
    if (fields.length) {
      Store.subscribe(KEY, function (saved) {
        fields.forEach(function (input) {
          if (input === document.activeElement) return;
          var key = input.getAttribute("data-field-key");
          if (!key) return;
          input.value =
            typeof saved[key] === "string" && saved[key]
              ? saved[key]
              : fallback(input);
          if (input.hasAttribute("data-spin-min")) normalize(input, false);
          fit(input);
        });
      });
    }

    /* A marked debility dims the two stats the sheet brackets under it. */
    var debs = document.querySelectorAll(".pb-debility[data-stats]");
    if (!debs.length) return;

    function linkedCells(label) {
      var names = (label.getAttribute("data-stats") || "").split(/\s+/);
      var cells = [];
      names.forEach(function (n) {
        if (!n) return;
        var cell = document.querySelector(
          '.pb-stat[data-stat="' + n.replace(/["\\]/g, "\\$&") + '"]'
        );
        if (cell) cells.push(cell);
      });
      return cells;
    }

    function sync(label) {
      var box = label.querySelector("input[type=checkbox]");
      var on = !!(box && box.checked);
      label.classList.toggle("is-on", on);
      linkedCells(label).forEach(function (cell) {
        cell.classList.toggle("is-debilitated", on);
      });
    }

    function syncAll() {
      debs.forEach(sync);
    }

    debs.forEach(function (label) {
      var box = label.querySelector("input[type=checkbox]");
      if (!box) return;
      box.addEventListener("change", function () {
        sync(label);
      });
      /* bindWikiChecks restores the saved state after this module runs. */
      setTimeout(function () {
        sync(label);
      }, 0);
      sync(label);
    });

    /* A debility ticked at the other end of the table. The checkbox itself is
       repainted by the checks module, but setting .checked in script fires no
       change event — so the dimming it brackets has to be re-applied here or
       the stats stay bright until the page is reloaded. That module subscribes
       before this one, so the boxes are already right by the time this runs. */
    if (window.StonetopStore) {
      window.StonetopStore.subscribe("stonetop-wiki-checks", syncAll);
    }

  })();

  /* ---------- One measure for every page ----------
     .content is a column-fill:auto multicol sized width:max-content. The
     browser picks the column count off its own estimate of that width and
     then stretches the columns to fill the box, so the measure drifts above
     the --col-w the design asks for — a short insert lands as a single 691px
     column, a playbook as four 450px ones. Size the box to an exact multiple
     of the intended column instead, and the text reads the same width
     everywhere. */
  (function () {
    var main = document.querySelector("main.content");
    if (!main || main.classList.contains("maps-page")) return;
    // The home page flows down its own grid, and an arcanum is one card at a
    // fixed size. Neither is an article set to a measure.
    if (main.querySelector(".index-hero, .arcana-card")) return;

    function fit() {
      var cs = window.getComputedStyle(main);
      var colw = parseFloat(cs.columnWidth);
      var gap = parseFloat(cs.columnGap) || 0;
      if (!isFinite(colw) || colw <= 0) return;
      // The box is border-box, so its padding is inside the width we set. Left
      // out, the last column has nowhere to go and the rest stretch to cover.
      var pad =
        (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
      function widthFor(n) {
        return n * (colw + gap) - gap + pad + "px";
      }

      // Lay the article out as one ordinary column to learn its true height.
      var style = main.style;
      var keep = [style.width, style.height, style.columnWidth, style.columnCount];
      style.width = widthFor(1);
      style.height = "auto";
      style.columnWidth = "auto";
      style.columnCount = "1";
      var need = main.scrollHeight;
      style.columnWidth = keep[2];
      style.columnCount = keep[3];
      style.height = keep[1];

      var box = main.clientHeight || main.getBoundingClientRect().height;
      if (!box) {
        style.width = keep[0];
        return;
      }
      var n = Math.max(1, Math.ceil(need / box));
      style.width = widthFor(n);
      // Blocks that may not break (stat blocks, move cards) take more room in
      // columns than they do in one flow, so the estimate runs short. Add the
      // columns the overflow actually asks for, rather than one at a time.
      for (var i = 0; i < 6; i++) {
        var over = main.scrollWidth - main.clientWidth;
        if (over <= 2) break;
        n += Math.max(1, Math.ceil(over / (colw + gap)));
        style.width = widthFor(n);
      }
    }

    fit();
    var t = null;
    window.addEventListener("resize", function () {
      clearTimeout(t);
      t = setTimeout(fit, 120);
    });
  })();

  /* ---------- HP trackers -------------------------------------------------
   *
   * The row of little boxes the adventure-site sheets use, brought to the
   * book pages: click a box to set HP to that number, click the one you are
   * already on to take a point off, click the readout to go back to full.
   *
   * Two places want it. A playbook's HP box stops being the current total and
   * becomes the *maximum* — the number the sheet prints is only where you
   * start, and it climbs with level — while the boxes underneath are what you
   * actually mark off in a fight. And every monster stat block trades its
   * printed "HP 24;" for a track of its own, since a stat block read at the
   * table is a thing being fought, not a thing being looked up.
   *
   * Full health is stored as nothing at all. An untouched enemy is absent
   * from the store, which keeps it small and — the lesson the site sheets
   * taught — stops a freshly opened page pushing a default over what the
   * campaign already holds.
   * --------------------------------------------------------------------- */
  (function () {
    var Store = window.StonetopStore;
    if (!Store) return;

    /* A character's HP is their own and the table watches it; a monster's is
       the GM's. An adventure-site sheet names its own store in
       data-hp-storage, so the third one is not known until the page says. */
    var PLAYBOOK_STORE = "stonetop-wiki-playbook-hp";
    var MONSTER_STORE = "stonetop-wiki-monster-hp";
    /* Followers are the players' own: shared scope, so the table sees them. */
    var FOLLOWER_STORE = "stonetop-wiki-follower-hp";
    var NOTES_STORE = "stonetop-wiki-notes";

    var trackers = [];
    var watched = {};

    function slugOf() {
      var m = String(location.pathname || "")
        .replace(/\\/g, "/")
        .match(/([^\/#?]+)\.html/i);
      return m ? m[1] : "index";
    }

    function current(t) {
      var v = Store.get(t.store)[t.key];
      if (typeof v !== "number" || v < 0 || v > t.max) return t.max;
      return v;
    }

    function paint(t) {
      var cur = current(t);
      var kids = t.boxes.children;
      for (var i = 0; i < kids.length; i++) {
        var n = i + 1;
        kids[i].classList.toggle("is-filled", n <= cur);
        kids[i].classList.toggle("is-empty", n > cur);
        kids[i].setAttribute("aria-pressed", n <= cur ? "true" : "false");
      }
      if (!t.readout) return;
      t.readout.textContent = cur + "/" + t.max;
      t.readout.classList.toggle("is-down", cur === 0);
      t.readout.classList.toggle("is-full", cur === t.max);
    }

    function setHp(t, value) {
      var v = Math.max(0, Math.min(t.max, value | 0));
      var state = Store.get(t.store);
      if (v === t.max) delete state[t.key];
      else state[t.key] = v;
      Store.set(t.store, state);
    }

    /* The boxes are rebuilt whenever the maximum moves — a playbook's does,
       every time its owner levels. */
    function render(t) {
      t.boxes.innerHTML = "";
      for (var i = 1; i <= t.max; i++) {
        var box = document.createElement("button");
        box.type = "button";
        box.className = "hp-box";
        box.title = "Set HP to " + i;
        box.setAttribute("aria-label", t.label + ": set HP to " + i);
        box.addEventListener(
          "click",
          (function (n) {
            return function () {
              var cur = current(t);
              /* Clicking the box you are on takes a point off, so one click is
                 the commonest thing that happens in a fight. */
              setHp(t, n === cur && cur > 0 ? cur - 1 : n);
            };
          })(i)
        );
        t.boxes.appendChild(box);
      }
      paint(t);
    }

    /**
     * A tracker over one store key.
     *
     * The book pages have nowhere to put one, so it builds its own wrapper.
     * A site sheet already prints the row — an .hp-boxes to fill and an
     * .hp-readout beside it — so those are adopted where they stand and no
     * wrapper is made. More than one row may carry the same id (an enemy that
     * appears in two rooms): each becomes its own tracker over the same key,
     * and they repaint together because every one of them reads the store.
     */
    function build(opts) {
      var el = null;
      var boxes = opts.boxes;
      var readout = opts.readout;

      if (!boxes) {
        el = document.createElement("div");
        el.className = "hp-track";
        el.setAttribute("role", "group");
        el.setAttribute("aria-label", opts.label + " HP");

        boxes = document.createElement("span");
        boxes.className = "hp-boxes";

        readout = document.createElement("button");
        readout.type = "button";
        readout.className = "hp-readout";

        el.appendChild(boxes);
        el.appendChild(readout);
      }
      if (readout) readout.title = "Click to reset to full";

      var t = {
        store: opts.store,
        key: opts.key,
        max: opts.max,
        label: opts.label,
        el: el,
        boxes: boxes,
        readout: readout,
      };
      if (readout) {
        readout.addEventListener("click", function () {
          setHp(t, t.max);
        });
      }
      watch(t.store);
      render(t);
      trackers.push(t);
      return t;
    }

    /* One subscription per store, whichever stores this page turns out to use. */
    function watch(store) {
      if (watched[store]) return;
      watched[store] = true;
      Store.subscribe(store, function () {
        trackers.forEach(function (t) {
          if (t.store === store) paint(t);
        });
      });
    }

    var slug = slugOf();

    /* ----- The playbook sheet ----- */
    (function () {
      var stats = document.querySelector(".pb-stats");
      if (!stats) return;
      var grid = stats.querySelector(".pb-track-grid");
      var maxBox = stats.querySelector(
        'input.wiki-field[data-field-key$=":track-hp"]'
      );
      if (!grid || !maxBox) return;

      /* The printed "HP (max 18)" was the starting total wearing the cap as a
         label. It is the cap now, and it is meant to be edited — so the
         ceiling comes off the spinner. */
      var cell = maxBox.closest(".pb-track");
      var name = cell && cell.querySelector(".pb-track-name");
      if (name) name.textContent = "Max HP";
      maxBox.setAttribute("aria-label", "Max HP");
      maxBox.setAttribute("data-spin-min", "1");
      maxBox.setAttribute("data-spin-max", "99");
      maxBox.setAttribute("aria-valuemin", "1");
      maxBox.setAttribute("aria-valuemax", "99");

      function readMax() {
        var n = parseInt(String(maxBox.value).replace(/[^0-9-]/g, ""), 10);
        return n > 0 ? n : 1;
      }

      var t = build({
        store: PLAYBOOK_STORE,
        key: slug,
        max: readMax(),
        label: "Character",
      });
      grid.parentNode.insertBefore(t.el, grid.nextSibling);

      function syncMax() {
        var m = readMax();
        if (m === t.max) return;
        t.max = m;
        render(t);
      }
      maxBox.addEventListener("input", syncMax);
      maxBox.addEventListener("change", syncMax);
      /* Max HP lives in the notes store, so it can also arrive from another
         browser — which sets the box's value without firing either event. */
      Store.subscribe(NOTES_STORE, syncMax);
    })();

    /* ----- Follower sheets (the Followers, Crew and Animal Companion inserts) -----
       A sheet names its follower on the root (data-hp-key) and prints a
       Max HP box; the tracker goes into the sheet's own mount and lives in
       the followers' store, which the whole table shares. */
    document
      .querySelectorAll(".follower-sheet[data-hp-key]")
      .forEach(function (sheet) {
        var maxBox = sheet.querySelector(
          'input.wiki-field[data-field-key$="max-hp"]'
        );
        var mount = sheet.querySelector(".fs-hp-mount");
        if (!maxBox || !mount) return;
        function readMax() {
          var n = parseInt(String(maxBox.value).replace(/[^0-9-]/g, ""), 10);
          return n > 0 ? n : 1;
        }
        var t = build({
          store: FOLLOWER_STORE,
          key: slug + "#" + sheet.getAttribute("data-hp-key"),
          max: readMax(),
          label: sheet.getAttribute("data-label") || "Follower",
        });
        mount.appendChild(t.el);
        function syncMax() {
          var m = readMax();
          if (m === t.max) return;
          t.max = m;
          render(t);
        }
        maxBox.addEventListener("input", syncMax);
        maxBox.addEventListener("change", syncMax);
        Store.subscribe(NOTES_STORE, syncMax);
      });

    /* ----- Adventure-site sheets -----
       The sheets print their enemy rows in the page and name their store on
       the body. They used to run their own copy of everything above; this is
       all that is left of it. */
    (function () {
      var store =
        document.body && document.body.getAttribute("data-hp-storage");
      if (!store) return;
      document
        .querySelectorAll(".enemy-row[data-hp-id][data-hp-max]")
        .forEach(function (row) {
          var id = row.getAttribute("data-hp-id");
          var max = parseInt(row.getAttribute("data-hp-max"), 10) || 0;
          var boxes = row.querySelector(".hp-boxes");
          if (!id || !max || !boxes) return;
          build({
            store: store,
            key: id,
            max: max,
            label: id,
            boxes: boxes,
            readout: row.querySelector(".hp-readout"),
          });
        });
    })();

    /* ----- Monster and NPC stat blocks -----
     *
     * Bound over the page at load, and again over the hover popup every time
     * one is injected — a stat block read in a popup is being fought just as
     * much as one read on its own page.
     *
     * The key is the *previewed* page's slug, not the page being read, so the
     * track in the popup and the track on the page it came from are one track:
     * wound something in a popup and the page has it wounded too. */
    function bindStatBlocks(root, pageSlug) {
      if (!root || !pageSlug) return;
      /* The popup replaces its contents on every hover, and the trackers it
         was holding went with them. */
      trackers = trackers.filter(function (t) {
        return t.boxes.isConnected;
      });
      root.querySelectorAll(".stat-block").forEach(function (block, i) {
        if (block.getAttribute("data-hp-bound")) return;
        var line = block.querySelector("p.stat-stats");
        if (!line) return;
        /* "HP 14; Armor 4 (resilience) · Damage …" — the number sits in the
           leading text node, ahead of the dice buttons, so only that node is
           touched and every listener already bound in the line survives.
           Most blocks put a semicolon after the number and a handful a comma,
           so the separator goes with it — left behind, it opened the line on
           its own punctuation.
           "HP 0 of 6" is not this pattern at all: that is a site sheet's own
           current-of-max, which has a real tracker of its own already. */
        var first = line.firstChild;
        /* The build sets the label in bold — "<strong>HP</strong> 14;" — so the
           number then sits in the text node after it, and the label element goes
           with the number once the tracker has taken its place. */
        var label = null;
        if (
          first &&
          first.nodeType === 1 &&
          first.tagName === "STRONG" &&
          /^\s*HP\s*$/i.test(first.textContent)
        ) {
          label = first;
          first = first.nextSibling;
        }
        if (!first || first.nodeType !== 3) return;
        var m = first.nodeValue.match(
          label ? /^\s*(\d+)(?!\s*of\b)\s*[;,]?\s*/ : /^\s*HP\s+(\d+)(?!\s*of\b)\s*[;,]?\s*/i
        );
        if (!m) return;
        var max = parseInt(m[1], 10);
        if (!(max > 0) || max > 200) return;
        first.nodeValue = first.nodeValue.slice(m[0].length);
        if (label) label.parentNode.removeChild(label);
        block.setAttribute("data-hp-bound", pageSlug);

        var name = block.querySelector(".stat-name");
        var t = build({
          store: block.classList.contains("follower") ? FOLLOWER_STORE : MONSTER_STORE,
          key: pageSlug + "#" + (block.id || "stat-" + i),
          max: max,
          label: name ? name.textContent.trim() : "Enemy",
        });
        line.parentNode.insertBefore(t.el, line);
      });
    }

    window.bindHpTrackers = bindStatBlocks;
    bindStatBlocks(document, slug);
  })();

  /* ---------- Feedback: a link at the foot of every page ------------------
   *
   * "Send feedback" under the article opens a small form that mails the GM
   * through FormSubmit.co. The page's published address (og:url, else the
   * address bar) rides along as a hidden field, so a note about a mangled
   * table says which table without the reader having to. Runtime-only: it is
   * built here so it reaches book pages, arcana cards and site sheets alike,
   * and no rebuild can clobber it. Posts go to the AJAX endpoint (JSON back,
   * page stays put). Off a disk nothing can be sent — see below.
   * --------------------------------------------------------------------- */
  (function () {
    var ALIAS = "92a6f91f6240f66ca4a4813fd4169213";
    var ENDPOINT = "https://formsubmit.co/" + ALIAS;
    var AJAX = "https://formsubmit.co/ajax/" + ALIAS;

    var body = document.body;
    if (!body) return;
    var host =
      document.querySelector("main.content") ||
      document.querySelector(".site-main") ||
      (body.classList.contains("site-sheet") ? body : null);
    if (!host || document.querySelector(".page-feedback")) return;

    var pageUrl = (function () {
      var og = document.querySelector('meta[property="og:url"]');
      var stated = og ? og.getAttribute("content") || "" : "";
      return stated || String(location.href).split("#")[0];
    })();
    var pageTitle = (document.title || "").trim();

    var foot = document.createElement("footer");
    foot.className = "page-feedback";
    var link = document.createElement("a");
    link.href = "#feedback";
    link.className = "page-feedback-link";
    link.textContent = "Send feedback about this page";
    foot.appendChild(link);
    host.appendChild(foot);

    var overlay = null;
    var form, msg, status, send;

    function esc(t) {
      return String(t).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }

    function build() {
      if (overlay) return;
      overlay = document.createElement("div");
      overlay.className = "feedback-overlay";
      overlay.hidden = true;
      overlay.innerHTML =
        '<form class="feedback-panel" method="POST" action="' + ENDPOINT + '" role="dialog" aria-modal="true" aria-labelledby="feedback-head">' +
        '<button type="button" class="feedback-close" aria-label="Close">×</button>' +
        '<h2 class="sync-head" id="feedback-head">Send feedback</h2>' +
        '<p class="feedback-page">About <span class="feedback-page-title">' + esc(pageTitle || pageUrl) + "</span></p>" +
        '<input type="hidden" name="Page" value="' + esc(pageUrl) + '">' +
        '<input type="hidden" name="Title" value="' + esc(pageTitle) + '">' +
        '<input type="hidden" name="_subject" value="Stonetop Wiki feedback: ' + esc(pageTitle || pageUrl) + '">' +
        '<input type="hidden" name="_template" value="table">' +
        '<input type="hidden" name="_captcha" value="false">' +
        '<input type="hidden" name="_next" value="' + esc(pageUrl) + '">' +
        '<input type="text" name="_honey" class="feedback-honey" tabindex="-1" autocomplete="off">' +
        '<label class="sync-field"><span>What should change?</span>' +
        '<textarea name="Feedback" rows="6" required placeholder="A wrong number, a missing move, a table that came out mangled…"></textarea></label>' +
        '<label class="sync-field"><span>Email (optional, for a reply)</span>' +
        '<input type="email" name="email" autocomplete="email"></label>' +
        '<div class="feedback-actions">' +
        '<button type="submit" class="sync-action is-primary">Send</button>' +
        '<button type="button" class="sync-action is-quiet feedback-cancel">Cancel</button>' +
        "</div>" +
        '<p class="sync-note feedback-status" aria-live="polite"></p>' +
        "</form>";
      body.appendChild(overlay);
      form = overlay.querySelector("form");
      msg = form.querySelector("textarea");
      status = form.querySelector(".feedback-status");
      send = form.querySelector('button[type="submit"]');

      overlay.addEventListener("click", function (e) {
        if (e.target === overlay) close();
      });
      overlay.querySelector(".feedback-close").addEventListener("click", close);
      overlay.querySelector(".feedback-cancel").addEventListener("click", close);
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && !overlay.hidden) close();
      });
      form.addEventListener("submit", onSubmit);
    }

    function open() {
      build();
      status.textContent = "";
      overlay.hidden = false;
      setTimeout(function () {
        msg.focus();
      }, 0);
    }

    function close() {
      if (overlay) overlay.hidden = true;
    }

    function onSubmit(e) {
      if (!window.fetch || !window.FormData) return; // plain post; the page reloads
      e.preventDefault();
      var data = {};
      new FormData(form).forEach(function (v, k) {
        if (k !== "_next") data[k] = v;
      });
      send.disabled = true;
      status.textContent = "Sending…";
      fetch(AJAX, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(data),
      })
        .then(function (r) {
          return r.json();
        })
        .then(function (res) {
          // FormSubmit answers 200 with success:"false" for a refused post.
          if (String(res && res.success) !== "true") throw new Error((res && res.message) || "refused");
          send.disabled = false;
          msg.value = "";
          close();
          showToast("Feedback sent — thank you.", 3200);
        })
        .catch(function (err) {
          send.disabled = false;
          status.textContent = "Couldn’t send (" + (err && err.message ? err.message : "network error") + "). Try again, or email Bryan directly.";
        });
    }

    /* FormSubmit attributes a post by its referring site and refuses one
       from a page browsed as a file — there is no site to attribute it to.
       Off a disk, then, the link goes to the published copy of this same
       page with #feedback, which opens the form there on arrival. */
    var offDisk = location.protocol === "file:" || !/^https?:/.test(pageUrl);
    if (offDisk && /^https?:/.test(pageUrl)) {
      link.href = pageUrl + "#feedback";
      link.target = "_blank";
      link.rel = "noopener";
      link.title = "Opens the published page — feedback can't be sent from a page opened off a disk";
      return;
    }
    if (offDisk) {
      foot.remove();
      return;
    }

    link.addEventListener("click", function (e) {
      e.preventDefault();
      open();
    });
    if (location.hash === "#feedback") {
      if (history.replaceState) history.replaceState(null, "", location.pathname + location.search);
      open();
    }
  })();

  // Prefetch previews
  loadPreviews();
})();

/* Shared Stonetop adventure sheet behavior.
 * Sheets live under Stonetop_Wiki/adventures/. Expect body.adventure with:
 *   data-wiki-root="../"   (wiki root = parent of adventures/)
 *   data-hp-storage="unique-key-for-localStorage"
 *
 * Wiki hover popups come from wiki.js + previews-data.js. When those data
 * files are absent, wiki.js still shows a popup explaining that preview
 * data is missing.
 */
(function () {
  var body = document.body;
  var WIKI =
    (body && body.getAttribute("data-wiki-root")) || "../";
  if (WIKI.slice(-1) !== "/") WIKI += "/";

  var bubble = document.getElementById("wiki-preview");
  // Rewrite paths inside preview HTML when the bubble exists.
  if (bubble && typeof MutationObserver === "function") {
    function rewrite() {
      try {
        bubble.querySelectorAll("img[src]").forEach(function (img) {
          var s = img.getAttribute("src") || "";
          if (s.indexOf("../images/") === 0)
            img.setAttribute("src", WIKI + s.replace(/^\.\.\//, ""));
          else if (s.indexOf("images/") === 0)
            img.setAttribute("src", WIKI + s);
        });
        bubble.querySelectorAll("a.wiki-link[href]").forEach(function (a) {
          var h = a.getAttribute("href") || "";
          if (
            /^https?:/i.test(h) ||
            h.indexOf(WIKI) === 0 ||
            h.charAt(0) === "#"
          )
            return;
          var m = h.match(/^([^\/#]+\.html)(#.*)?$/i);
          if (m)
            a.setAttribute("href", WIKI + m[1] + (m[2] || ""));
        });
      } catch (e) {
        /* ignore rewrite failures */
      }
    }
    new MutationObserver(rewrite).observe(bubble, {
      childList: true,
      subtree: true,
    });
  }

  /* ---- Sidebar jump: mark nav only (no section outline) ---- */
  function clearNavCurrent() {
    document.querySelectorAll(".adv-nav a.is-current").forEach(function (a) {
      a.classList.remove("is-current");
    });
  }

  function markNavCurrent(hash) {
    clearNavCurrent();
    if (!hash || hash === "#") return;
    var id = hash.replace(/^#/, "");
    if (!id) return;
    document
      .querySelectorAll('.adv-nav a[href="#' + id + '"]')
      .forEach(function (a) {
        a.classList.add("is-current");
      });
  }

  document.querySelectorAll('.adv-nav a[href^="#"]').forEach(function (a) {
    a.addEventListener("click", function () {
      var href = a.getAttribute("href") || "";
      if (bubble) {
        bubble.classList.remove("visible");
        bubble.hidden = true;
      }
      setTimeout(function () {
        markNavCurrent(href);
      }, 0);
    });
  });
  window.addEventListener("hashchange", function () {
    markNavCurrent(location.hash);
  });
  if (location.hash) {
    markNavCurrent(location.hash);
  }

  /* ---- HP trackers ---- */
  var STORAGE_KEY =
    (body && body.getAttribute("data-hp-storage")) ||
    "stonetop-adventure-hp";
  var state = {};
  try {
    state = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") || {};
  } catch (e) {
    state = {};
  }

  function save() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) {}
  }

  function rowsFor(id) {
    return document.querySelectorAll('.enemy-row[data-hp-id="' + id + '"]');
  }

  function paint(id) {
    var rows = rowsFor(id);
    if (!rows.length) return;
    var max = parseInt(rows[0].getAttribute("data-hp-max"), 10) || 0;
    var cur = state[id];
    if (typeof cur !== "number" || cur < 0 || cur > max) cur = max;
    state[id] = cur;
    rows.forEach(function (row) {
      var boxes = row.querySelectorAll(".hp-box");
      boxes.forEach(function (box, i) {
        var n = i + 1;
        box.classList.toggle("is-filled", n <= cur);
        box.classList.toggle("is-empty", n > cur);
        box.setAttribute("aria-pressed", n <= cur ? "true" : "false");
        box.title = "Set HP to " + n;
      });
      var readout = row.querySelector(".hp-readout");
      if (readout) {
        readout.textContent = cur + "/" + max;
        readout.classList.toggle("is-down", cur === 0);
        readout.classList.toggle("is-full", cur === max);
      }
    });
  }

  function setHp(id, value) {
    var row = document.querySelector('.enemy-row[data-hp-id="' + id + '"]');
    if (!row) return;
    var max = parseInt(row.getAttribute("data-hp-max"), 10) || 0;
    var v = Math.max(0, Math.min(max, value | 0));
    state[id] = v;
    save();
    paint(id);
  }

  function initTrackers() {
    var rows = document.querySelectorAll(".enemy-row[data-hp-id][data-hp-max]");
    var seen = {};
    rows.forEach(function (row) {
      var id = row.getAttribute("data-hp-id");
      var max = parseInt(row.getAttribute("data-hp-max"), 10) || 0;
      var boxHost = row.querySelector(".hp-boxes");
      var readout = row.querySelector(".hp-readout");
      if (!boxHost) return;
      boxHost.innerHTML = "";
      for (var i = 1; i <= max; i++) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "hp-box";
        btn.setAttribute("data-hp-n", String(i));
        btn.setAttribute("aria-label", "Set " + id + " HP to " + i);
        btn.addEventListener(
          "click",
          (function (n) {
            return function () {
              var cur = state[id];
              if (typeof cur !== "number") cur = max;
              if (n === cur && cur > 0) setHp(id, cur - 1);
              else setHp(id, n);
            };
          })(i)
        );
        boxHost.appendChild(btn);
      }
      if (readout) {
        readout.title = "Click to reset to full";
        readout.style.cursor = "pointer";
        readout.addEventListener("click", function () {
          setHp(id, max);
        });
      }
      if (!seen[id]) {
        seen[id] = true;
        if (typeof state[id] !== "number") state[id] = max;
      }
      paint(id);
    });
    save();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTrackers);
  } else {
    initTrackers();
  }
})();

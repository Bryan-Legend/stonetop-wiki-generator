/* Shared Stonetop adventure-site sheet behavior.
 * Sheets live under Stonetop_Wiki/sites/. Expect body.site-sheet with:
 *   data-wiki-root="../"   (wiki root = parent of sites/)
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
    document.querySelectorAll(".site-nav a.is-current").forEach(function (a) {
      a.classList.remove("is-current");
    });
  }

  function markNavCurrent(hash) {
    clearNavCurrent();
    if (!hash || hash === "#") return;
    var id = hash.replace(/^#/, "");
    if (!id) return;
    document
      .querySelectorAll('.site-nav a[href="#' + id + '"]')
      .forEach(function (a) {
        a.classList.add("is-current");
      });
  }

  document.querySelectorAll('.site-nav a[href^="#"]').forEach(function (a) {
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

  /* ---- HP trackers ----
     wiki.js bindStatBlocks turns printed HP in a .stat-block into a tracker.
     A heading "4 Suarachan Hunters" repeats it four times. */

  /* Room-card wine wash on map nodes. Same stops as --card-wash in wiki.css. */
  var SVG_NS = "http://www.w3.org/2000/svg";
  function ensureNodeWash(svg) {
    if (svg.querySelector("#site-node-wash")) return;
    var defs = svg.querySelector("defs");
    if (!defs) {
      defs = document.createElementNS(SVG_NS, "defs");
      svg.insertBefore(defs, svg.firstChild);
    }
    var grad = document.createElementNS(SVG_NS, "linearGradient");
    grad.setAttribute("id", "site-node-wash");
    grad.setAttribute("x1", "0");
    grad.setAttribute("y1", "0");
    grad.setAttribute("x2", "0");
    grad.setAttribute("y2", "1");
    [
      ["0%", "#6e3331", "0.42"],
      ["28%", "#5e3033", "0.31"],
      ["55%", "#6c3034", "0.18"],
      ["100%", "#482b33", "0"],
    ].forEach(function (s) {
      var stop = document.createElementNS(SVG_NS, "stop");
      stop.setAttribute("offset", s[0]);
      stop.setAttribute("stop-color", s[1]);
      stop.setAttribute("stop-opacity", s[2]);
      grad.appendChild(stop);
    });
    defs.appendChild(grad);
  }
  /* Node labels: 10 user-units at viewBox width 420 (Drowned Choir). Scale so
     other maps render the same on-screen size. Rect nodes: 120×32 — width from
     Drowned Choir, height fits two lines at that size (lh 1.05 + padding). */
  var MAP_LABEL_AT_420 = 10;
  var MAP_NODE_W = 120;
  var MAP_NODE_H = 32;
  function scaleMapLabels(svg) {
    var raw = svg.getAttribute("viewBox") || "";
    var parts = raw.trim().split(/[\s,]+/);
    var w = parseFloat(parts[2]);
    if (!w) return;
    svg.style.setProperty("--map-label-fs", (MAP_LABEL_AT_420 * w / 420) + "px");
  }
  function sizeMapNodes(svg) {
    svg.querySelectorAll("rect.map-node, rect.map-rubble").forEach(function (r) {
      var w = parseFloat(r.getAttribute("width"));
      var h = parseFloat(r.getAttribute("height"));
      var x = parseFloat(r.getAttribute("x"));
      var y = parseFloat(r.getAttribute("y"));
      if (!w || !h) return;
      var cx = x + w / 2;
      var cy = y + h / 2;
      r.setAttribute("width", MAP_NODE_W);
      r.setAttribute("height", MAP_NODE_H);
      r.setAttribute("x", cx - MAP_NODE_W / 2);
      r.setAttribute("y", cy - MAP_NODE_H / 2);
    });
    svg.querySelectorAll("rect.map-rubble").forEach(function (r) {
      var n = r.nextElementSibling;
      if (!n || n.tagName.toLowerCase() !== "rect") return;
      var fill = n.getAttribute("fill") || "";
      if (fill.indexOf("hatch") === -1) return;
      n.setAttribute("x", r.getAttribute("x"));
      n.setAttribute("y", r.getAttribute("y"));
      n.setAttribute("width", MAP_NODE_W);
      n.setAttribute("height", MAP_NODE_H);
      n.setAttribute("rx", r.getAttribute("rx") || "6");
    });
  }
  function paintNodeWash() {
    document.querySelectorAll(".site-map-svg").forEach(function (svg) {
      scaleMapLabels(svg);
      sizeMapNodes(svg);
      ensureNodeWash(svg);
      svg.querySelectorAll(".map-node, .map-rubble").forEach(function (node) {
        var parent = node.parentNode;
        if (!parent || parent.querySelector(":scope > .map-node-wash")) return;
        var wash = node.cloneNode(false);
        wash.setAttribute("class", "map-node-wash");
        wash.removeAttribute("filter");
        wash.removeAttribute("stroke");
        wash.removeAttribute("stroke-width");
        node.after(wash);
      });
    });
  }

  function readMapLabel(text) {
    var parts = [];
    text.childNodes.forEach(function (n) {
      if (n.nodeType === 3) {
        var s = n.textContent.replace(/\s+/g, " ").trim();
        if (s) parts.push(s);
      } else if (n.nodeType === 1 && n.tagName.toLowerCase() === "tspan") {
        parts.push(n.textContent);
      }
    });
    var out = "";
    parts.forEach(function (p) {
      p = p.replace(/^\s+|\s+$/g, "");
      if (!p) return;
      if (!out) out = p;
      else if (out.slice(-1) === "-") out += p;
      else out += " " + p;
    });
    return out;
  }

  function wrapMapLabel(name, maxW, widthOf) {
    var lines = [];
    var cur = "";
    function flush() {
      if (cur) lines.push(cur);
      cur = "";
    }
    function piecesOf(word) {
      if (widthOf(word) <= maxW) return [word];
      var dash = word.lastIndexOf("-");
      if (dash > 0) {
        var a = word.slice(0, dash + 1);
        var b = word.slice(dash + 1);
        if (widthOf(a) <= maxW) return [a].concat(piecesOf(b));
      }
      return [word];
    }
    name.split(/\s+/).forEach(function (word) {
      piecesOf(word).forEach(function (piece) {
        var trial = !cur ? piece : cur.slice(-1) === "-" ? cur + piece : cur + " " + piece;
        if (!cur || widthOf(trial) <= maxW) cur = trial;
        else {
          flush();
          cur = piece;
        }
      });
    });
    flush();
    return lines;
  }

  function layoutMapLabels(svg) {
    var probe = document.createElementNS(SVG_NS, "text");
    probe.setAttribute("class", "map-label");
    probe.setAttribute("opacity", "0");
    probe.setAttribute("pointer-events", "none");
    svg.appendChild(probe);
    function widthOf(s) {
      probe.textContent = s;
      return probe.getComputedTextLength();
    }
    var fs = parseFloat(svg.style.getPropertyValue("--map-label-fs")) || 10;
    var lh = fs * 1.05;
    probe.setAttribute("x", "0");
    probe.setAttribute("y", "0");
    probe.textContent = "Hgyp";
    var ink = probe.getBBox();
    var inkMid = ink.y + ink.height / 2;
    svg.querySelectorAll("a").forEach(function (a) {
      var node = a.querySelector(".map-node, .map-rubble");
      var text = a.querySelector(".map-label");
      if (!node || !text) return;
      var name = readMapLabel(text);
      if (!name) return;
      var box = node.getBBox();
      var cx = box.x + box.width / 2;
      var cy = box.y + box.height / 2;
      var maxW = Math.max(12, box.width - Math.min(12, box.width * 0.12));
      var lines = wrapMapLabel(name, maxW, widthOf);
      if (!lines.length) return;
      while (text.firstChild) text.removeChild(text.firstChild);
      text.setAttribute("x", cx);
      text.setAttribute("y", cy);
      text.setAttribute("text-anchor", "middle");
      var start = cy - inkMid - ((lines.length - 1) * lh) / 2;
      lines.forEach(function (line, i) {
        var ts = document.createElementNS(SVG_NS, "tspan");
        ts.setAttribute("x", cx);
        ts.setAttribute("y", start + i * lh);
        ts.textContent = line;
        text.appendChild(ts);
      });
      var wash = a.querySelector(".map-node-wash");
      if (wash) wash.after(text);
      else node.after(text);
    });
    svg.removeChild(probe);
  }

  function layoutAllMapLabels() {
    document.querySelectorAll(".site-map-svg").forEach(layoutMapLabels);
  }

  paintNodeWash();
  function afterFonts() {
    layoutAllMapLabels();
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(afterFonts);
  else afterFonts();

})();

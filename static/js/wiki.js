/* Stonetop Book II Wiki — dice rolls + hover previews */
(function () {
  "use strict";

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
    previewsPromise = new Promise(function (resolve) {
      const s = document.createElement("script");
      s.src = SCRIPT_BASE + "js/previews-data.js";
      s.onload = function () {
        previews = window.WIKI_PREVIEWS || {};
        resolve(previews);
      };
      s.onerror = function () {
        previews = {};
        resolve(previews);
      };
      document.head.appendChild(s);
    });
    return previewsPromise;
  }

  /* ---------- Dice ---------- */
  /** Parse/roll NdS, NdS+M, NdS-M (e.g. d10+3, 1d4-1, 2d6). */
  function rollDice(expr) {
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
    for (let i = 0; i < n; i++) {
      const v = 1 + Math.floor(Math.random() * sides);
      parts.push(v);
      total += v;
    }
    total += mod;
    const diceExpr = (n === 1 ? "" : String(n)) + "d" + sides;
    const modExpr = mod === 0 ? "" : mod > 0 ? "+" + mod : String(mod);
    return {
      total: total,
      parts: parts,
      mod: mod,
      expr: diceExpr + modExpr,
      n: n,
      sides: sides,
    };
  }

  const toast = document.getElementById("dice-toast");
  let toastTimer = null;

  function showDiceResult(result) {
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
    toast.innerHTML =
      '<span class="label">' +
      result.expr +
      "</span>" +
      detail +
      ' → <span class="result">' +
      result.total +
      "</span>";
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
    }, 2200);
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
    const result = rollDice(btn.getAttribute("data-dice"));
    if (!result) return;
    btn.classList.remove("rolling");
    void btn.offsetWidth;
    btn.classList.add("rolling");
    showDiceResult(result);
    highlightRollRow(btn, result.total);
  });

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
    let left = rect.left;
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
      var path = location.pathname.replace(/\\/g, "/");
      if (/\/pages\/[^/]+\.html$/i.test(path)) {
        return slug + ".html";
      }
      return "pages/" + slug + ".html";
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
    Promise.all([loadPreviews(), loadPageMap()]).then(function (pair) {
      var data = pair[0];
      var map = pair[1];
      if (!data[slug]) return;
      if (!link.matches(":hover") && !(bubble && bubble.matches(":hover"))) return;
      showPreview(link, data[slug], map);
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
    var path = location.pathname.replace(/\\/g, "/");
    if (/\/pages\/[^/]+\.html$/i.test(path)) {
      return slug + ".html";
    }
    return "pages/" + slug + ".html";
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
        matchSlugs[doc.slug] = true;
        var rank = titleHit ? 0 : 1;
        // Prefer denser title matches
        if (titleHit && titleHay === terms.join(" ")) rank = -1;
        hits.push({
          slug: doc.slug,
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
        var slug = slugMatch ? slugMatch[1] : "";
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
              : "";
        if (hit.titleHit && hit.textHit) where += (where ? " · " : "") + "title + text";
        else if (hit.titleHit) where += (where ? " · " : "") + "title";
        else where += (where ? " · " : "") + "in text";
        html.push(
          '<a class="search-hit" href="' +
            pageHrefFromSlug(hit.slug) +
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

    function restoreScroll() {
      try {
        var raw = sessionStorage.getItem(KEY);
        if (raw == null || raw === "") return;
        var y = parseInt(raw, 10);
        if (!isFinite(y) || y < 0) return;
        el.scrollTop = y;
        // Layout/fonts can shift — re-apply after paint
        requestAnimationFrame(function () {
          el.scrollTop = y;
        });
        setTimeout(function () {
          el.scrollTop = y;
        }, 50);
      } catch (e) {}
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

  /* ---------- Persistent requirement checkboxes ---------- */
  (function () {
    var KEY = "stonetop-wiki-checks";

    function loadState() {
      try {
        return JSON.parse(localStorage.getItem(KEY) || "{}") || {};
      } catch (e) {
        return {};
      }
    }
    function saveState(state) {
      try {
        localStorage.setItem(KEY, JSON.stringify(state));
      } catch (e) {}
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

    /** True if this check is marked under pageSlug (or any legacy path key). */
    function isChecked(state, pageSlug, checkId) {
      if (state[storageKey(pageSlug, checkId)]) return true;
      var suffix = "#" + checkId;
      for (var k in state) {
        if (
          Object.prototype.hasOwnProperty.call(state, k) &&
          state[k] &&
          k.endsWith(suffix)
        ) {
          return true;
        }
      }
      return false;
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
          else {
            delete st[full];
            // Clear legacy keys for same check id
            var suffix = "#" + id;
            Object.keys(st).forEach(function (k) {
              if (k.endsWith(suffix)) delete st[k];
            });
          }
          saveState(st);
          // Sync any other visible boxes for the same page+id (page + popup)
          document
            .querySelectorAll(
              'input.wiki-check[data-check-id="' +
                id.replace(/"/g, '\\"') +
                '"][data-check-page="' +
                pageSlug.replace(/"/g, '\\"') +
                '"]'
            )
            .forEach(function (other) {
              if (other !== box) other.checked = box.checked;
            });
        });
      });
    }

    window.bindWikiChecks = bindWikiChecks;

    // Bind checks on the current page at load
    var currentSlug = slugFromPath(location.pathname);
    if (currentSlug) bindWikiChecks(document, currentSlug);
  })();

  /* ---------- Map pins + labels (Maps page) ---------- */
  (function () {
    var strip = document.querySelector(".maps-strip");
    if (!strip) return;
    var KEY = "stonetop-wiki-map-pins";

    function load() {
      try {
        return JSON.parse(localStorage.getItem(KEY) || "{}") || {};
      } catch (e) {
        return {};
      }
    }
    var store = load();
    function persist() {
      try {
        localStorage.setItem(KEY, JSON.stringify(store));
      } catch (e) {}
    }
    function pinsFor(mapId) {
      return store[mapId] || (store[mapId] = []);
    }

    var adding = false;
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
          persist();
        }
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
      return el;
    }

    var canvases = strip.querySelectorAll(".map-canvas");
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
  })();

  /* Scroll to #fragment targets inside the horizontal multi-column pane */
  function scrollToFragment() {
    var id = (location.hash || "").replace(/^#/, "");
    if (!id) return;
    var el = document.getElementById(id);
    if (!el) return;
    el.classList.add("target-highlight");
    var scroll = document.querySelector(".content-scroll");
    if (scroll) {
      // Place the element near the left of the scroll viewport
      var elRect = el.getBoundingClientRect();
      var scRect = scroll.getBoundingClientRect();
      var delta = elRect.left - scRect.left - 24;
      scroll.scrollLeft += delta;
      // Vertical alignment within the column pane
      var vDelta = elRect.top - scRect.top - 16;
      if (Math.abs(vDelta) > 8) {
        // columns are fixed height; element is already in-flow vertically per column
      }
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

  // Prefetch previews
  loadPreviews();
})();

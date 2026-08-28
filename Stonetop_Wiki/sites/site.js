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
     Moved wholesale into ../js/wiki.js, which every sheet already loads.
     It finds .enemy-row[data-hp-id][data-hp-max] on this page, reads the
     store off the body's data-hp-storage, and fills the .hp-boxes the sheet
     prints — the same rows, the same clicks, the same store key. The book
     pages grew trackers of their own (playbooks and monster stat blocks),
     and one copy of the logic beats three. */

})();

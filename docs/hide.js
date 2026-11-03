/* Hide as mislabelled. Adds a button to each listing in the report. A hidden listing stays hidden in this
   browser until it is restored. Nothing is sent anywhere: the list is kept in this browser's local storage. */
(function () {
  "use strict";

  var STORE_KEY = "mtgHiddenListings:v1";
  var KEEP_MS = 90 * 24 * 60 * 60 * 1000;   /* forget a hidden listing that has not appeared on any page for 90 days */
  var TOUCH_MS = 24 * 60 * 60 * 1000;       /* a listing seen again is remembered for another 90 days */
  var UNDO_MS = 8000;

  var root = document.getElementById("main") || document.querySelector("main");
  if (!root) { return; }

  var hidden = Object.create(null);   /* key -> the time it was hidden or last seen */
  var canSave = true;
  var items = [];                     /* every element that can be hidden */
  var groups = [];                    /* a table or a list, so a message can show when all its rows are hidden */
  var showAll = false;
  var undoState = null;
  var message = "";
  var timer = 0;

  /* ------------------------------------------------------------------ the saved list */
  function copy(obj) {
    var c = Object.create(null);
    Object.keys(obj).forEach(function (k) { c[k] = obj[k]; });
    return c;
  }
  function readStore() {
    var out = Object.create(null), raw;
    try { raw = window.localStorage.getItem(STORE_KEY); } catch (e) { canSave = false; return out; }
    if (!raw) { return out; }
    try {
      var data = JSON.parse(raw);
      if (data && typeof data === "object" && !Array.isArray(data)) {
        Object.keys(data).forEach(function (k) {
          if (typeof data[k] === "number" && isFinite(data[k])) { out[k] = data[k]; }
        });
      }
    } catch (e) { /* a damaged list is treated as empty */ }
    return out;
  }
  function writeStore() {
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify(hidden));
      canSave = true;
    } catch (e) { canSave = false; }
  }

  /* A listing is its store address without the query, the page section or a trailing slash. */
  function keyOf(href) {
    try {
      var u = new URL(href, window.location.href);
      if (u.protocol !== "http:" && u.protocol !== "https:") { return ""; }
      return u.hostname.toLowerCase().replace(/^www\./, "") + u.pathname.replace(/\/+$/, "");
    } catch (e) { return ""; }
  }

  function make(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text) { n.textContent = text; }
    return n;
  }

  /* ------------------------------------------------------------------ finding the listings */
  function addItem(el, link, holder, group, withButton) {
    var key = keyOf(link.getAttribute("href"));
    if (!key) { return; }
    var it = { el: el, key: key, title: (link.textContent || "").replace(/\s+/g, " ").trim(), btn: null };
    if (withButton) {
      var row = make("div", "hide-row");
      var btn = make("button", "hide-btn");
      btn.type = "button";
      btn.setAttribute("data-hide-key", key);
      row.appendChild(btn);
      holder.appendChild(row);
      it.btn = btn;
    }
    items.push(it);
    if (group) { group.items.push(it); }
  }
  function makeGroup(anchor) {
    var g = { items: [], anchor: anchor, note: make("div", "hide-empty",
      "Every listing in this section is hidden as mislabelled. Press Show hidden above to see them.") };
    g.note.hidden = true;
    return g;
  }

  /* the Outliers table: the first column is the item, and its first link is the listing */
  Array.prototype.forEach.call(root.querySelectorAll("table"), function (table) {
    var head = table.querySelector("th");
    if (!head || head.textContent.trim().toLowerCase() !== "item") { return; }
    var wrap = table.parentNode && table.parentNode.classList && table.parentNode.classList.contains("tablewrap") ? table.parentNode : table;
    var group = makeGroup(wrap);
    Array.prototype.forEach.call(table.querySelectorAll("tr"), function (tr) {
      var cell = tr.cells && tr.cells[0];
      if (!cell || cell.tagName !== "TD") { return; }
      var link = cell.querySelector("a[href]");
      if (link) { addItem(tr, link, cell, group, true); }
    });
    if (group.items.length) { groups.push(group); }
  });

  /* the half price list: a list that follows a heading containing "half price" */
  Array.prototype.forEach.call(root.querySelectorAll("h3"), function (h) {
    if (!/half price/i.test(h.textContent)) { return; }
    for (var n = h.nextElementSibling; n && n.tagName !== "H3"; n = n.nextElementSibling) {
      if (n.tagName !== "UL") { continue; }
      var group = makeGroup(n);
      Array.prototype.forEach.call(n.children, function (li) {
        var link = li.tagName === "LI" ? li.querySelector("a[href]") : null;
        if (link) { addItem(li, link, li, group, true); }
      });
      if (group.items.length) { groups.push(group); }
    }
  });

  /* the "New today" and price drop lists in the note at the top: hidden together with their listing, no button */
  Array.prototype.forEach.call(root.querySelectorAll('div[style*="border:1px solid #999"] li'), function (li) {
    var link = li.querySelector("a[href]");
    if (link) { addItem(li, link, li, null, false); }
  });

  if (!items.some(function (it) { return it.btn; })) { return; }
  groups.forEach(function (g) { g.anchor.parentNode.insertBefore(g.note, g.anchor.nextSibling); });

  /* ------------------------------------------------------------------ the bar above the report */
  var bar = make("div", "hidebar");
  bar.id = "hidebar";
  bar.hidden = true;
  var barText = make("span", "hb-text");
  barText.setAttribute("role", "status");
  barText.setAttribute("aria-live", "polite");
  var undoBtn = make("button", "hb-btn primary", "Undo");
  var toggleBtn = make("button", "hb-btn", "Show hidden");
  var restoreBtn = make("button", "hb-btn", "Restore all");
  [undoBtn, toggleBtn, restoreBtn].forEach(function (b) { b.type = "button"; });
  bar.appendChild(barText);
  bar.appendChild(undoBtn);
  bar.appendChild(toggleBtn);
  bar.appendChild(restoreBtn);
  root.insertBefore(bar, root.firstChild);

  function render() {
    var onPage = 0, counted = Object.create(null);
    items.forEach(function (it) {
      var h = !!hidden[it.key];
      it.el.classList.toggle("is-hidden", h);
      if (h && it.btn && !counted[it.key]) { counted[it.key] = 1; onPage += 1; }
      if (it.btn) {
        it.btn.textContent = h ? "Restore" : "Mislabelled";
        it.btn.setAttribute("aria-label", (h ? "Restore: " : "Hide as mislabelled: ") + it.title);
        it.btn.title = h ? "Show this listing in the report again"
          : "Hide this listing because it is not the product it is labelled as. It stays hidden in this browser until you restore it.";
      }
    });
    if (onPage === 0) { showAll = false; }
    groups.forEach(function (g) {
      var allHidden = !g.items.some(function (it) { return !hidden[it.key]; }) && !showAll;
      g.anchor.classList.toggle("hide-whole", allHidden);   /* a table with every row hidden is hidden too, not left as a bare header */
      g.note.hidden = !allHidden;
    });
    document.documentElement.classList.toggle("show-hidden", showAll);

    var parts = [];
    if (message) { parts.push(message); }
    if (onPage > 0) { parts.push(onPage + (onPage === 1 ? " listing is" : " listings are") + " hidden as mislabelled on this page."); }
    if (onPage > 0 && !canSave) { parts.push("This browser is not saving the list, so it will be forgotten when you leave."); }
    barText.textContent = parts.join(" ");
    bar.hidden = !(onPage > 0 || message);
    undoBtn.hidden = !undoState;
    toggleBtn.hidden = onPage === 0;
    toggleBtn.textContent = showAll ? "Hide them again" : "Show hidden";
    restoreBtn.hidden = Object.keys(hidden).length === 0;
  }

  /* ------------------------------------------------------------------ actions */
  function arm(ms) {
    clearTimeout(timer);
    timer = setTimeout(function () { undoState = null; message = ""; render(); }, ms);
  }
  function commit(next, text) {
    undoState = { before: copy(hidden) };
    hidden = next;
    message = text;
    writeStore();
    arm(UNDO_MS);
    render();
  }
  function visibleButton(it) { return it.btn && !hidden[it.key]; }
  function focusAfterHiding(it) {
    var at = items.indexOf(it), i, target = null;
    for (i = at + 1; i < items.length && !target; i += 1) { if (visibleButton(items[i])) { target = items[i].btn; } }
    for (i = at - 1; i >= 0 && !target; i -= 1) { if (visibleButton(items[i])) { target = items[i].btn; } }
    (target || (undoBtn.hidden ? toggleBtn : undoBtn)).focus();
  }
  function focusAny() {
    var target = null;
    items.some(function (it) { if (visibleButton(it)) { target = it.btn; return true; } return false; });
    (target || document.body).focus();
  }

  root.addEventListener("click", function (event) {
    var btn = event.target && event.target.closest ? event.target.closest(".hide-btn") : null;
    if (!btn) { return; }
    var key = btn.getAttribute("data-hide-key");
    var item = items.filter(function (it) { return it.btn === btn; })[0];
    if (!item) { return; }
    var next = copy(hidden);
    if (hidden[key]) {
      delete next[key];
      commit(next, "Restored: " + item.title + ".");
    } else {
      next[key] = Date.now();
      commit(next, "Hidden: " + item.title + ".");
      if (!showAll) { focusAfterHiding(item); }
    }
  });
  toggleBtn.addEventListener("click", function () { showAll = !showAll; render(); });
  restoreBtn.addEventListener("click", function () {
    var n = Object.keys(hidden).length;
    commit(Object.create(null), "Restored " + n + (n === 1 ? " listing." : " listings."));
    focusAny();
  });
  undoBtn.addEventListener("click", function () {
    if (!undoState) { return; }
    var before = undoState.before, current = hidden, changed = "";
    Object.keys(before).concat(Object.keys(current)).some(function (k) {
      if (!!before[k] !== !!current[k]) { changed = k; return true; }
      return false;
    });
    hidden = before;
    undoState = null;
    message = "Undone.";
    writeStore();
    arm(4000);
    render();
    var back = items.filter(function (it) { return it.key === changed && visibleButton(it); })[0];
    if (back) { back.btn.focus(); } else { focusAny(); }
  });
  window.addEventListener("storage", function (event) {
    if (event.key === STORE_KEY || event.key === null) { hidden = readStore(); render(); }
  });

  /* ------------------------------------------------------------------ start up */
  hidden = readStore();
  var now = Date.now(), changed = false, onThisPage = Object.create(null);
  items.forEach(function (it) { onThisPage[it.key] = 1; });
  Object.keys(hidden).forEach(function (k) {
    if (onThisPage[k]) {
      if (now - hidden[k] > TOUCH_MS) { hidden[k] = now; changed = true; }
    } else if (now - hidden[k] > KEEP_MS) {
      delete hidden[k];
      changed = true;
    }
  });
  if (changed) { writeStore(); }
  render();
}());

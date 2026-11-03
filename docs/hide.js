(function () {
  "use strict";

  var KEY = "mtg-hidden-listings:v1";
  var KEEP_MS = 90 * 24 * 60 * 60 * 1000;
  var SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">';
  var ICON_HIDE = SVG + '<path d="M3 3l18 18"/><path d="M10.6 6.1A9.8 9.8 0 0 1 12 6c5 0 8.5 4 9.5 6-.4.8-1.2 2-2.4 3.1"/><path d="M6.5 7.6C4.4 9 3 11 2.5 12c1 2 4.5 6 9.5 6 1.5 0 2.9-.4 4.1-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';
  var ICON_BACK = SVG + '<path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/></svg>';

  var main = document.getElementById("main") || document.querySelector("main");
  if (!main) { return; }

  var memory = null;
  var pruned = false;
  var hidden = load();
  var showAll = false;
  var undo = null;
  var keepBar = false;   // after Restore all the bar stays so the restore can be undone
  var groups = [];
  var noteItems = [];
  var noteLists = [];
  var everyKey = [];

  // ---- storage: kept in the browser, with a memory fallback when the browser blocks it
  function load() {
    var raw = null, data = null, out = Object.create(null), now = Date.now();
    try { raw = window.localStorage.getItem(KEY); } catch (e) { raw = memory; }
    try { data = raw ? JSON.parse(raw) : null; } catch (e) { data = null; }
    if (data && typeof data === "object" && !Array.isArray(data)) {
      Object.keys(data).forEach(function (k) {
        var t = data[k];
        if (typeof t === "number" && isFinite(t) && now - t <= KEEP_MS) { out[k] = t; } else { pruned = true; }
      });
    }
    return out;
  }
  function save() {
    var raw = JSON.stringify(hidden);
    try { window.localStorage.setItem(KEY, raw); } catch (e) { memory = raw; }
  }

  // ---- a listing is identified by its store address, without the query, the tags or the price
  function keyOf(a) {
    if (!a) { return ""; }
    try {
      var u = new URL(a.getAttribute("href") || "", window.location.href);
      if (u.protocol !== "http:" && u.protocol !== "https:") { return ""; }
      return (u.hostname.replace(/^www\./i, "") + u.pathname.replace(/\/+$/, "")).toLowerCase();
    } catch (e) { return ""; }
  }
  function firstLink(el) { return el.querySelector("a[href]"); }

  function setState(btn, isHidden) {
    var want = isHidden ? "restore" : "hide";
    if (btn.getAttribute("data-state") === want) { return; }
    btn.setAttribute("data-state", want);
    btn.setAttribute("data-tip", isHidden ? "Restore item" : "Hide item");
    btn.setAttribute("aria-label", isHidden ? "Restore this listing" : "Hide this listing as mislabelled");
    btn.innerHTML = isHidden ? ICON_BACK : ICON_HIDE;
  }
  function makeButton() {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "hide-btn";
    setState(btn, false);
    return btn;
  }
  function makeEmpty(after) {
    var p = document.createElement("p");
    p.className = "hide-empty";
    p.hidden = true;
    p.textContent = "Every listing in this section is hidden as mislabelled. Use Show hidden above to review them.";
    after.parentNode.insertBefore(p, after.nextSibling);
    return p;
  }

  // ---- the Outliers table: one extra narrow column at the right
  Array.prototype.forEach.call(main.querySelectorAll("table"), function (table) {
    var head = table.rows[0];
    if (!head || !head.cells.length || head.cells[0].textContent.trim().toLowerCase() !== "item") { return; }
    var wrap = table.closest(".tablewrap") || table;
    var group = { wrap: wrap, items: [], empty: null };
    var th = document.createElement("th");
    th.className = "hide-th";
    var sr = document.createElement("span");
    sr.className = "hide-sr";
    sr.textContent = "Hide";
    th.appendChild(sr);
    head.appendChild(th);
    for (var i = 1; i < table.rows.length; i++) {
      var row = table.rows[i];
      if (!row.cells.length) { continue; }
      var key = keyOf(firstLink(row.cells[0]));
      var last = row.cells[row.cells.length - 1];
      var td = document.createElement("td");
      td.className = "hide-cell";
      var style = last.getAttribute("style");
      if (style) { td.setAttribute("style", style); }   // keeps the NEW and RECENT tint across the new cell
      var btn = null;
      if (key) {
        btn = makeButton();
        td.appendChild(btn);
        group.items.push({ key: key, el: row, btn: btn });
        everyKey.push(key);
      }
      row.appendChild(td);
    }
    if (group.items.length) { group.empty = makeEmpty(wrap); groups.push(group); }
  });

  // ---- the half price list: a button at the right of each card
  Array.prototype.forEach.call(main.querySelectorAll("h3"), function (h) {
    if (!/half price/i.test(h.textContent)) { return; }
    var n = h.nextElementSibling;
    while (n && n.tagName !== "UL" && n.tagName !== "H3") { n = n.nextElementSibling; }
    if (!n || n.tagName !== "UL") { return; }
    var group = { wrap: n, items: [], empty: null };
    Array.prototype.forEach.call(n.children, function (li) {
      if (li.tagName !== "LI") { return; }
      var key = keyOf(firstLink(li));
      if (!key) { return; }
      li.classList.add("has-hide");
      var btn = makeButton();
      li.appendChild(btn);
      group.items.push({ key: key, el: li, btn: btn });
      everyKey.push(key);
    });
    if (group.items.length) { group.empty = makeEmpty(n); groups.push(group); }
  });

  if (!groups.length) { return; }

  // ---- the list of new deals in the note at the top follows the same hidden list
  Array.prototype.forEach.call(main.querySelectorAll('div[style*="border:1px solid #999"] li'), function (li) {
    var key = keyOf(firstLink(li));
    if (!key) { return; }
    noteItems.push({ key: key, el: li });
    everyKey.push(key);
    if (li.parentNode && noteLists.indexOf(li.parentNode) === -1) { noteLists.push(li.parentNode); }
  });

  // ---- the bar
  var bar = document.createElement("div");
  bar.className = "hide-bar";
  bar.hidden = true;
  bar.setAttribute("role", "group");
  bar.setAttribute("aria-label", "Hidden listings");
  bar.innerHTML = '<span class="hide-msg" role="status" aria-live="polite"></span>' +
    '<button type="button" class="hide-link" data-act="undo">Undo</button>' +
    '<button type="button" class="hide-link" data-act="toggle" aria-pressed="false">Show hidden</button>' +
    '<button type="button" class="hide-link" data-act="clear">Restore all</button>';
  main.insertBefore(bar, main.firstChild);
  var msg = bar.querySelector(".hide-msg");
  var undoBtn = bar.querySelector('[data-act="undo"]');
  var toggleBtn = bar.querySelector('[data-act="toggle"]');
  var clearBtn = bar.querySelector('[data-act="clear"]');

  function apply() {
    var seen = Object.create(null), count = 0;
    everyKey.forEach(function (k) { if (hidden[k] && !seen[k]) { seen[k] = true; count += 1; } });
    if (count === 0) { showAll = false; }
    groups.forEach(function (g) {
      var visible = 0;
      g.items.forEach(function (it) {
        var h = !!hidden[it.key];
        it.el.classList.toggle("is-hidden", h);
        it.el.classList.toggle("hide-off", h && !showAll);
        if (!h || showAll) { visible += 1; }
        setState(it.btn, h);
      });
      var none = visible === 0;
      g.wrap.classList.toggle("hide-off", none);
      g.empty.hidden = !none;
    });
    noteItems.forEach(function (it) {
      var h = !!hidden[it.key];
      it.el.classList.toggle("is-hidden", h);
      it.el.classList.toggle("hide-off", h && !showAll);
    });
    noteLists.forEach(function (ul) {
      var allOff = Array.prototype.every.call(ul.children, function (li) { return li.classList.contains("hide-off"); });
      ul.classList.toggle("hide-off", allOff);
      var before = ul.previousElementSibling;
      if (before && before.tagName === "P") { before.classList.toggle("hide-off", allOff); }
    });
    if (count > 0) {
      msg.textContent = count + (count === 1 ? " listing is" : " listings are") + " hidden as mislabelled on this page";
    } else if (undo && keepBar) {
      msg.textContent = "Restored. Nothing is hidden on this page.";
    } else {
      msg.textContent = "";
    }
    bar.hidden = count === 0 && !(undo && keepBar);
    undoBtn.hidden = !undo;
    toggleBtn.hidden = count === 0;
    clearBtn.hidden = count === 0;
    toggleBtn.textContent = showAll ? "Hide them again" : "Show hidden";
    toggleBtn.setAttribute("aria-pressed", showAll ? "true" : "false");
  }

  function snapshot() {
    var copy = Object.create(null);
    Object.keys(hidden).forEach(function (k) { copy[k] = hidden[k]; });
    undo = copy;
  }

  function usable(candidate, item) { return candidate.btn && candidate.key !== item.key && !hidden[candidate.key]; }
  function neighbour(group, item) {
    var i = group.items.indexOf(item), n = group.items.length, j;
    for (j = i + 1; j < n; j++) { if (usable(group.items[j], item)) { return group.items[j].btn; } }
    for (j = i - 1; j >= 0; j--) { if (usable(group.items[j], item)) { return group.items[j].btn; } }
    return null;
  }

  function onItem(btn) {
    var item = null, group = null;
    groups.some(function (g) {
      return g.items.some(function (it) {
        if (it.btn === btn) { item = it; group = g; return true; }
        return false;
      });
    });
    if (!item) { return; }
    snapshot();
    keepBar = false;
    var next = null;
    if (hidden[item.key]) {
      delete hidden[item.key];
    } else {
      next = neighbour(group, item);
      hidden[item.key] = Date.now();
    }
    save();
    apply();
    // the row that held focus may have disappeared, so move focus to the next listing
    if (next) { next.focus(); } else if (hidden[item.key] && !bar.hidden) { toggleBtn.focus(); }
  }

  function onAction(act) {
    if (act === "toggle") {
      showAll = !showAll;
      apply();
    } else if (act === "undo") {
      if (!undo) { return; }
      hidden = undo;
      undo = null;
      keepBar = false;
      save();
      apply();
      if (!toggleBtn.hidden) { toggleBtn.focus(); }
    } else if (act === "clear") {
      snapshot();
      keepBar = true;
      hidden = Object.create(null);
      showAll = false;
      save();
      apply();
      undoBtn.focus();
    }
  }

  main.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest) { return; }
    var btn = t.closest(".hide-btn");
    if (btn && main.contains(btn)) { onItem(btn); return; }
    var act = t.closest("[data-act]");
    if (act && bar.contains(act)) { onAction(act.getAttribute("data-act")); }
  });

  // another tab changed the list
  window.addEventListener("storage", function (e) {
    if (e.key === KEY || e.key === null) { hidden = load(); undo = null; apply(); }
  });

  // a listing that is still on the page counts as seen today, so it is not forgotten
  var now = Date.now(), touched = false;
  everyKey.forEach(function (k) { if (hidden[k]) { hidden[k] = now; touched = true; } });
  if (touched || pruned) { save(); }
  apply();
})();

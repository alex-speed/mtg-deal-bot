#!/usr/bin/env python3
"""
add_hide_button.py: adds a small hide button to every listing on the published report pages, so a
listing that is mislabelled (for example a single deck priced against a display) can be hidden without
stopping the bot from finding mislabelled listings.

Safe to run in production? Yes. It only writes files inside the docs folder of your own repository
(hide.css, hide.js and one block in the head of each report page). It makes no web requests and sends
nothing anywhere.

Run it after build_site.py every time, because build_site.py rebuilds every page from scratch:
    python build_site.py
    python add_hide_button.py

What the reader sees
    A small round button in a narrow column at the right of the Outliers table, and at the right of
    each item in the half price list. Hovering it shows "Hide item". Pressing it hides that listing.
    A slim bar then appears above the report with Undo, Show hidden and Restore all.
    While hidden listings are shown, the same button reads "Restore item".

How it remembers
    Hidden listings are kept in the browser (localStorage), by store address, ignoring the price, the
    tags and any tracking text. A listing that has not appeared on any page for 90 days is forgotten.
    The list is per browser, so it is not shared between a phone and a PC. Nothing is sent to GitHub.

Usage:
    python add_hide_button.py                 read and write docs
    python add_hide_button.py --docs site     another folder
"""
import argparse
import hashlib
import sys
from pathlib import Path

START = "<!-- hide-listings:start -->"
END = "<!-- hide-listings:end -->"
REPORT_MARK = "<!-- report:start -->"

# ---------------------------------------------------------------------------------------------------
# The stylesheet. It uses the theme's colours when they exist and falls back to its own otherwise.
# ---------------------------------------------------------------------------------------------------

CSS = r"""
/* hide button column and bar (add_hide_button.py) */
.hide-off{display:none !important}
.hide-sr{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}

table th.hide-th{position:relative;width:1%;padding:0;border-left:0}   /* relative, so the hidden label stays inside the scrolling panel */
table td.hide-cell{width:1%;padding:calc(.72rem - 2px) .8rem .2rem .2rem;text-align:right;vertical-align:top;white-space:nowrap}

.hide-btn{position:relative;display:inline-grid;place-items:center;width:28px;height:28px;margin:0;padding:0;
  border:1px solid rgba(160,150,255,.28);border-radius:50%;background:transparent;color:#9aa4cf;cursor:pointer;
  font:inherit;line-height:1;transition:color .15s,border-color .15s,background .15s}
.hide-btn svg{display:block;pointer-events:none}
.hide-btn:hover{color:#fff;border-color:var(--gold,#f4c24f);background:rgba(244,194,79,.12)}
.hide-btn:focus-visible{outline:2px solid var(--gold,#f4c24f);outline-offset:2px}
.hide-btn:active{background:rgba(244,194,79,.22)}

/* the tooltip sits to the left of the button so the scrolling table panel never clips it */
.hide-btn::after{content:attr(data-tip);position:absolute;right:calc(100% + 8px);top:50%;transform:translateY(-50%);
  padding:.32rem .6rem;border:1px solid rgba(160,150,255,.35);border-radius:6px;background:#0b1030;color:#e9edfc;
  font:600 12px/1.2 var(--sans,"Segoe UI",system-ui,sans-serif);letter-spacing:.02em;white-space:nowrap;pointer-events:none;
  box-shadow:0 8px 22px rgba(0,0,0,.45);opacity:0;visibility:hidden;z-index:6;
  transition:opacity .1s ease,visibility 0s linear .1s}
.hide-btn:focus-visible::after{opacity:1;visibility:visible;transition:opacity .1s ease,visibility 0s}
@media (hover:hover){
  .hide-btn:hover::after{opacity:1;visibility:visible;transition:opacity .12s ease .3s,visibility 0s linear .3s}
}

/* half price list: the button sits at the right of the card */
li.has-hide{padding-right:3.2rem}
li.has-hide > .hide-btn{position:absolute;right:.8rem;top:.62rem}

/* a listing that is hidden but shown again for review */
tr.is-hidden > td:not(.hide-cell){opacity:.55}
tr.is-hidden a,li.is-hidden a{text-decoration:line-through}
li.is-hidden{border-style:dashed}
li.is-hidden a{opacity:.7}

/* the bar that appears once something is hidden */
.hide-bar{display:flex;flex-wrap:wrap;align-items:center;gap:.35rem 1rem;margin:0 0 1.2rem;padding:.55rem .95rem;
  border:1px solid var(--edge,rgba(160,150,255,.24));border-radius:12px;background:rgba(14,20,58,.7);
  color:var(--muted,#a9b2d5);font-size:.86rem}
.hide-bar .hide-msg{margin-right:auto}
.hide-link{appearance:none;-webkit-appearance:none;margin:0;padding:.2rem .1rem;border:0;border-bottom:1px solid rgba(185,198,255,.3);
  background:none;color:var(--link,#b9c6ff);font:inherit;cursor:pointer}
.hide-link:hover{color:#fff;border-bottom-color:var(--gold,#f4c24f)}
.hide-link:focus-visible{outline:2px solid var(--gold,#f4c24f);outline-offset:3px;border-radius:3px}
.hide-empty{margin:.4rem 0 1.3rem;padding:.8rem 1rem;border:1px dashed rgba(160,150,255,.3);border-radius:12px;color:var(--muted,#a9b2d5)}
.hide-bar[hidden],.hide-empty[hidden],.hide-link[hidden]{display:none !important}

@media (pointer:coarse){
  .hide-btn{width:36px;height:36px}
  .hide-btn svg{width:17px;height:17px}
  table td.hide-cell{padding-top:.25rem}
  li.has-hide{padding-right:3.6rem}
  li.has-hide > .hide-btn{top:.4rem}
  .hide-link{padding:.5rem .3rem}
}
@media (prefers-reduced-motion:reduce){
  .hide-btn,.hide-btn::after{transition:none}
}
@media (max-width:720px){
  table td.hide-cell{padding-right:.6rem}
}
@media print{
  .hide-btn,.hide-cell,.hide-th,.hide-bar{display:none !important}
}
"""

# ---------------------------------------------------------------------------------------------------
# The script. Plain browser JavaScript, no libraries. The pages only allow scripts from their own site.
# ---------------------------------------------------------------------------------------------------

JS = r"""
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
"""


# ---------------------------------------------------------------------------------------------------
# The page changes
# ---------------------------------------------------------------------------------------------------


def digest(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def head_block(prefix):
    return (f'{START}\n<link rel="stylesheet" href="{prefix}hide.css?v={digest(CSS)}">\n'
            f'<script src="{prefix}hide.js?v={digest(JS)}" defer></script>\n{END}')


def inject(doc, prefix):
    """Puts the block in the head of a page, or replaces the block that is already there."""
    block = head_block(prefix)
    a, z = doc.find(START), doc.find(END)
    if a != -1 and z > a:
        return doc[:a] + block + doc[z + len(END):]
    i = doc.lower().rfind("</head>")
    if i == -1:
        return None
    return doc[:i] + block + "\n" + doc[i:]


def report_pages(docs):
    pages = []
    front = docs / "index.html"
    if front.is_file():
        pages.append((front, ""))
    archive = docs / "archive"
    if archive.is_dir():
        pages.extend((p, "../") for p in sorted(archive.glob("*.html")))
    return pages


def write_if_changed(path, text):
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Add the hide button to the published report pages.")
    ap.add_argument("--docs", default="docs")
    args = ap.parse_args(argv)
    docs = Path(args.docs)
    if not docs.is_dir():
        print(f"{docs} was not found, so nothing was changed.")
        return 0
    write_if_changed(docs / "hide.css", CSS.lstrip("\n"))
    write_if_changed(docs / "hide.js", JS.lstrip("\n"))
    done, skipped = 0, []
    for path, prefix in report_pages(docs):
        doc = path.read_text(encoding="utf-8", errors="replace")
        if REPORT_MARK not in doc:
            continue          # the list of earlier reports has no report to add buttons to
        new = inject(doc, prefix)
        if new is None:
            skipped.append(path.name)
            continue
        write_if_changed(path, new)
        done += 1
    print(f"Hide button added to {done} report page(s)" + (f"; no head found in {', '.join(skipped)}" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

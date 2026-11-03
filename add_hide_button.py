#!/usr/bin/env python3
"""
add_hide_button.py: adds a "Mislabelled" button to every listing on the published report pages.

Safe to run in production? Yes. It only reads and writes files inside the docs folder of this
repository. It makes no web requests and changes nothing outside the repository.

What it does
    Run it straight after build_site.py. It writes two files, hide.css and hide.js, into the docs
    folder, then adds one link tag and one script tag to the head of every report page (the front
    page and each dated page). It leaves the archive list and any other page alone.

    In the browser, each row of the Outliers table and each item in the half price list gets a
    Mislabelled button. Pressing it hides that listing, for example a single deck that a store has
    titled as a display. The bot still finds mislabelled listings. The button only hides one for you.

    The list of hidden listings is kept in this browser's local storage, so nothing is sent anywhere.
    A bar at the top of the report shows how many are hidden, with Show hidden, Undo and Restore all.
    A listing is identified by its store address with the query part removed, so a changed price,
    colour of the row or tracking text does not bring it back. A listing that has not appeared on any
    page for 90 days is forgotten.

Because build_site.py rebuilds every page on each run, run this after it each time. Running it twice
in a row changes nothing.

Usage:
    python add_hide_button.py                 add the buttons to docs
    python add_hide_button.py --out site      another output folder
"""
import argparse
import hashlib
import re
import sys
from pathlib import Path

START = "<!-- hide-listings:start -->"
END = "<!-- hide-listings:end -->"
BLOCK_RE = re.compile(re.escape(START) + r".*?" + re.escape(END) + r"\n?", re.S)
HEAD_END_RE = re.compile(r"</head>", re.I)

CSS = r"""/* Hide as mislabelled: the buttons, the bar above the report and the hidden state.
   It uses the page theme's colours when they are defined, and sensible ones when they are not. */
.hide-row{margin-top:.6rem}
.hide-btn,.hb-btn{display:inline-flex;align-items:center;justify-content:center;min-height:32px;padding:.25rem .9rem;
  border:1px solid var(--edge,rgba(160,150,255,.35));border-radius:999px;background:transparent;color:var(--muted,#a9b2d5);
  font:inherit;font-size:.78rem;letter-spacing:.03em;line-height:1.2;cursor:pointer;
  transition:color .15s,border-color .15s,background .15s}
.hide-btn:hover,.hb-btn:hover{color:#fff;border-color:var(--gold,#f4c24f);background:rgba(244,194,79,.12)}
.hide-btn:focus-visible,.hb-btn:focus-visible{outline:2px solid var(--gold,#f4c24f);outline-offset:3px}
.hb-btn{min-height:36px;font-size:.84rem;color:var(--text,#e9edfc)}
.hb-btn.primary{border-color:rgba(244,194,79,.6);background:rgba(244,194,79,.12)}
.hb-btn[hidden],.hidebar[hidden],.hide-empty[hidden]{display:none}

.is-hidden,.hide-whole{display:none !important}
html.show-hidden tr.is-hidden{display:table-row !important}
html.show-hidden li.is-hidden{display:block !important}
html.show-hidden .is-hidden{opacity:.75}
html.show-hidden tr.is-hidden > td:first-child a:first-of-type,
html.show-hidden li.is-hidden > a:first-of-type{text-decoration:line-through}

.hidebar{display:flex;flex-wrap:wrap;align-items:center;gap:.6rem .8rem;margin:0 0 1.4rem;padding:.75rem 1.1rem;
  border:1px solid var(--edge,rgba(160,150,255,.35));border-left:3px solid var(--violet,#a58bff);border-radius:var(--r,14px);
  background:var(--panel,rgba(14,20,58,.92));color:var(--text,#e9edfc);font-size:.9rem}
.hb-text{margin-right:auto;min-width:12ch}
.hide-empty{margin:.4rem 0 1.3rem;padding:.8rem 1rem;border:1px dashed var(--edge,rgba(160,150,255,.35));
  border-radius:12px;color:var(--muted,#a9b2d5);font-size:.9rem}

@media (pointer:coarse){.hide-btn,.hb-btn{min-height:44px}}
@media print{.hide-row,.hidebar,.hide-empty{display:none !important}}
"""

JS = r"""/* Hide as mislabelled. Adds a button to each listing in the report. A hidden listing stays hidden in this
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
"""


def digest(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def is_report(doc):
    """True for a page that holds a report. The archive list and other pages are left alone."""
    return "<!-- report:start -->" in doc or "Outliers (far cheaper" in doc


def block(prefix):
    return (f"{START}\n"
            f'<link rel="stylesheet" href="{prefix}hide.css?v={digest(CSS)}">\n'
            f'<script src="{prefix}hide.js?v={digest(JS)}" defer></script>\n'
            f"{END}\n")


def inject(doc, prefix):
    """Returns the page with the tags added before the end of its head, or None when it has no head."""
    clean = BLOCK_RE.sub("", doc)
    m = HEAD_END_RE.search(clean)
    if not m:
        return None
    return clean[:m.start()] + block(prefix) + clean[m.start():]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Add the Mislabelled buttons to the published report pages.")
    ap.add_argument("--out", default="docs")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if not out.is_dir():
        print(f"{args.out} was not found, so no Mislabelled buttons were added.")
        return 0
    (out / "hide.css").write_text(CSS, encoding="utf-8")
    (out / "hide.js").write_text(JS, encoding="utf-8")
    pages = changed = skipped = 0
    for path in sorted(out.rglob("*.html")):
        doc = path.read_text(encoding="utf-8", errors="replace")
        if not is_report(doc):
            continue
        pages += 1
        depth = len(path.relative_to(out).parts) - 1
        new = inject(doc, "../" * depth)
        if new is None:
            skipped += 1
            print(f"{path.relative_to(out).as_posix()}: no head tag found, so it was not changed")
            continue
        if new != doc:
            path.write_text(new, encoding="utf-8")
            changed += 1
    print(f"Mislabelled buttons: {pages} report page(s) found, {changed} updated"
          + (f", {skipped} skipped" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

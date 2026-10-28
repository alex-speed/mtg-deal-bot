#!/usr/bin/env python3
"""
deal_history.py: remembers the deals the sealed product bot has already reported, so a report can
either show only the deals that are new, or show every deal with the newer ones tagged.

Safe to run in production? Yes. It reads and writes one small JSON file next to the bot script and
makes no web requests. It changes nothing in your tenant or on any server.

How a bot uses it (two lines, placed directly below the store_config lines and above the line
if __name__ == "__main__":  ):

    import deal_history
    deal_history.install(globals(), "seen-sealed.json")

Nothing changes unless the environment variable ONLY_NEW is set. Its value picks the behaviour:

    ONLY_NEW=highlight   show every current deal and tag it by how long ago it was first found:
                           NEW          first found today (gold row, red badge in an email)
                           RECENT       first found in the last RECENT_DAYS days, before today
                                        (lavender row, purple badge)
                           PRICE DROP   the price has fallen (orange row and badge)
                           no tag       older deals
                         NEW, PRICE DROP and RECENT deals are sorted to the top of their table, in that
                         order. The history file is updated after the bot finishes without an error.
    ONLY_NEW=1           show only deals not reported before (the older behaviour), plus price drops.
    (not set)            nothing changes and the history file is left alone.

A deal is one listing (store and page address). A deal is a price drop when its price falls by at
least PRICE_DROP_PERCENT percent and at least PRICE_DROP_MIN_DOLLARS below the lowest price reported
so far. A deal that has not appeared for HISTORY_GRACE_DAYS days is forgotten, so it counts as new if
it comes back. Dates are Adelaide dates.

Nothing is tagged on the day the history file is created, because every deal would be new. The deals
seen that day become the baseline, and they never get a NEW or RECENT tag. To start again from
nothing, delete the history file.
"""
import copy
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlsplit

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Australia/Adelaide")
except Exception:  # no time zone database: use UTC dates instead
    _TZ = timezone.utc

HISTORY_GRACE_DAYS = 10        # forget a deal that has not appeared for this many days
PRICE_DROP_PERCENT = 5.0       # a price drop is a fall of at least this many percent
PRICE_DROP_MIN_DOLLARS = 2.0   # and at least this many dollars
HIGHLIGHT_DAYS = 1             # how many days a deal stays NEW (1 = the day it was first found)
RECENT_DAYS = 3                # a deal stays RECENT until it is this many days old
MAX_LIST_IN_NOTE = 20          # most new deals listed in the note at the top of the report
TRUE_WORDS = {"1", "true", "yes", "on"}
FORMAT_VERSION = 1

NEW_COLOUR = "#fff3b0"
DROP_COLOUR = "#ffe0b2"
RECENT_COLOUR = "#e8e4ff"
_BADGE_STYLE = "color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;font-weight:bold"
_BLOCK_RE = re.compile(r"<tr\b[^>]*>.*?</tr>|<li\b[^>]*>.*?</li>", re.S | re.I)
_HREF_RE = re.compile(r'href="([^"]*)"')
# The bot adds " (NEW)" after a store name when the store itself published the listing recently. The tags
# here replace that, so it is removed in highlight mode. It is only matched straight after the store name.
_STORE_NEW_RE = re.compile(r" \(NEW\)(?=</td>|</li>| <small>|:)")


def _today():
    return datetime.now(_TZ).date()


def read_mode(env_var="ONLY_NEW"):
    """Returns 'highlight', 'hide' or None from the environment variable."""
    value = os.environ.get(env_var, "").strip().lower()
    if value == "highlight":
        return "highlight"
    if value in TRUE_WORDS:
        return "hide"
    return None


def deal_key(kind, item):
    """One identity per listing: the list it is in, the store, and the page address without any query."""
    url = item.get("url") or ""
    if url:
        parts = urlsplit(url)
        where = parts.netloc.lower() + parts.path.rstrip("/")
    else:
        where = "title:" + (item.get("title") or "").strip().lower()
    return f"{kind}|{item.get('store', '')}|{where}"


def _money(x):
    return f"${x:,.2f}"


_RANK = {"new": 0, "drop": 1, "recent": 2}


class History:
    def __init__(self, path, mode="hide"):
        self.path = path
        self.mode = mode
        self.loaded = {}          # what the file held when it was read
        self.created = None       # the date the history began (a string), or None when unknown
        self.deals = {}           # what will be written by save()
        self.first_run = False
        self.problem = None
        self.applied = False
        self.active = False       # True when tagging is switched on for this run
        self.new = 0
        self.hidden = 0
        self.older = 0
        self.drops = []
        self.marks = {}           # url -> details, for the deals to tag
        self.total_current = 0

    # ---------------------------------------------------------------- reading
    def load(self):
        self.loaded, self.problem, self.first_run, self.created = {}, None, False, None
        if not os.path.exists(self.path):
            self.first_run = True
            return self
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            deals = data.get("deals") if isinstance(data, dict) else None
            if not isinstance(deals, dict):
                raise ValueError("the file does not hold a deals list")
        except (OSError, ValueError) as exc:  # includes JSON errors
            self.problem = f"the history file could not be read ({exc}), so every deal is treated as new"
            return self
        for key, entry in deals.items():
            if isinstance(entry, dict) and isinstance(entry.get("price"), (int, float)):
                self.loaded[str(key)] = entry
        created = data.get("created")
        if isinstance(created, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", created):
            self.created = created
        elif self.loaded:
            # a history file from before this field existed: the earliest date it knows is the baseline
            firsts = [str(e.get("first_seen")) for e in self.loaded.values()
                      if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(e.get("first_seen") or ""))]
            self.created = min(firsts) if firsts else None
        return self

    # ---------------------------------------------------------------- filtering and marking
    def apply(self, outliers, unverified):
        """
        Returns (outliers, unverified).
        Mode hide: already reported deals are removed.
        Mode highlight: every deal is kept, tagged NEW, PRICE DROP or RECENT where it applies, and sorted
        so the tagged ones come first. Safe to call twice.
        """
        today = _today()
        today_s = today.isoformat()
        since = (today - timedelta(days=max(HIGHLIGHT_DAYS, 1) - 1)).isoformat()          # NEW starts here
        recent_since = (today - timedelta(days=max(RECENT_DAYS, 0))).isoformat()          # RECENT starts here
        highlight = self.mode == "highlight"
        # tagging needs a history that began on an earlier day, otherwise every deal looks new
        self.active = bool(highlight and not self.first_run and not self.problem
                           and self.created and self.created < today_s)
        baseline = self.created or ""
        deals = copy.deepcopy(self.loaded)
        self.new, self.hidden, self.older, self.drops, self.marks, self.total_current = 0, 0, 0, [], {}, 0
        shown = {"outlier": [], "unverified": []}
        rank = {}

        def mark(item, kind, was=None):
            url = item.get("url") or ""
            info = {"kind": kind, "title": item.get("title", ""), "store": item.get("store", ""),
                    "price": float(item["price"]), "was": was, "url": url}
            old = self.marks.get(url)
            if old is None or _RANK[kind] < _RANK[old["kind"]]:
                self.marks[url] = info
            return _RANK[kind]

        for kind, items in (("outlier", outliers), ("unverified", unverified)):
            for item in items:
                self.total_current += 1
                key = deal_key(kind, item)
                price = float(item["price"])
                prev = deals.get(key)
                if prev is None:
                    self.new += 1
                    shown[kind].append(item)
                    deals[key] = {"title": item.get("title", ""), "store": item.get("store", ""), "price": price,
                                  "first_seen": today_s, "last_seen": today_s, "reported": today_s}
                    if self.active:
                        rank[id(item)] = mark(item, "new")
                    elif highlight:
                        self.older += 1
                    continue
                prev_price = float(prev["price"])
                dropped = prev_price - price
                is_drop = dropped >= PRICE_DROP_MIN_DOLLARS and price <= prev_price * (1 - PRICE_DROP_PERCENT / 100.0)
                if is_drop:
                    shown[kind].append(item)
                    self.drops.append({"title": item.get("title", ""), "store": item.get("store", ""),
                                       "url": item.get("url", ""), "was": prev_price, "now": price})
                    prev["price"] = price          # remember the lowest price reported
                    prev["reported"] = today_s
                    prev["drop_date"] = today_s
                    prev["drop_from"] = prev_price
                    if self.active:
                        rank[id(item)] = mark(item, "drop", prev_price)
                elif highlight:
                    shown[kind].append(item)
                    first_seen = str(prev.get("first_seen") or "")
                    drop_date = str(prev.get("drop_date") or "")
                    if self.active and first_seen >= since and first_seen > baseline:
                        rank[id(item)] = mark(item, "new")       # first found earlier today
                    elif self.active and drop_date >= since and price <= prev_price \
                            and isinstance(prev.get("drop_from"), (int, float)):
                        rank[id(item)] = mark(item, "drop", float(prev["drop_from"]))
                    elif self.active and recent_since <= first_seen < since and first_seen > baseline:
                        rank[id(item)] = mark(item, "recent")    # first found in the last few days
                    else:
                        self.older += 1
                else:
                    self.hidden += 1
                prev["last_seen"] = today_s
                prev["title"] = item.get("title", prev.get("title", ""))
        self.deals = deals
        self.applied = True
        out = []
        for kind in ("outlier", "unverified"):
            items = shown[kind]
            if highlight:
                items = sorted(items, key=lambda it: rank.get(id(it), 3))   # stable: keeps the bot's own order
            out.append(items)
        return out[0], out[1]

    # ---------------------------------------------------------------- the report
    def _badge(self, info):
        if info["kind"] == "drop":
            was = f", was {_money(info['was'])}" if info.get("was") else ""
            return f'<span style="background:#e67e00;{_BADGE_STYLE}">PRICE DROP{was}</span> '
        if info["kind"] == "recent":
            return f'<span style="background:#6a5acd;{_BADGE_STYLE}">RECENT</span> '
        return f'<span style="background:#c00;{_BADGE_STYLE}">NEW</span> '

    def highlight_html(self, html):
        """Tags the rows and list items for new, recent and price drop deals. Everything else is left alone."""
        if self.mode != "highlight":
            return html
        html = _STORE_NEW_RE.sub("", html)
        if not self.active or not self.marks:
            return html
        by_href = {escape(url): info for url, info in self.marks.items() if url}
        colours = {"new": NEW_COLOUR, "drop": DROP_COLOUR, "recent": RECENT_COLOUR}

        def fix(match):
            block = match.group(0)
            href = _HREF_RE.search(block)
            info = by_href.get(href.group(1)) if href else None
            if not info:
                return block
            colour = colours[info["kind"]]
            badge = self._badge(info)
            end = block.index(">") + 1
            opening, rest = block[:end], block[end:]
            if "style=" not in opening:
                opening = opening[:-1] + f' style="background:{colour}">'
            if block[:3].lower() == "<tr":
                rest = rest.replace("<td>", f'<td style="background:{colour}">')
                first_td = rest.lower().find("<td")
                if first_td >= 0:
                    at = rest.index(">", first_td) + 1
                    rest = rest[:at] + badge + rest[at:]
            else:
                rest = badge + rest
            return opening + rest

        return _BLOCK_RE.sub(fix, html)

    def note_html(self):
        if not self.applied:
            return ""
        if self.mode == "highlight":
            return self._note_highlight()
        parts = ['<div style="border:1px solid #999;padding:6px 10px;margin-bottom:12px">']
        parts.append("<p><b>Only deals not reported before are shown.</b> "
                     f"{self.new} new, {len(self.drops)} price drop(s), "
                     f"{self.hidden} already reported earlier and hidden.</p>")
        if self.problem:
            parts.append(f"<p>Note: {escape(self.problem)}.</p>")
        elif self.first_run:
            parts.append("<p>This is the first run with history, so every current deal counts as new. "
                         "Later reports will be shorter.</p>")
        if self.drops:
            parts.append("<p>Price drops since the last report:</p><ul>")
            for d in self.drops:
                label = escape(d["title"])
                if d["url"]:
                    label = f'<a href="{escape(d["url"])}">{label}</a>'
                parts.append(f"<li>{label} at {escape(d['store'])}: {_money(d['was'])} down to {_money(d['now'])}</li>")
            parts.append("</ul>")
        parts.append("</div>")
        return "\n".join(parts)

    def _note_highlight(self):
        news = [m for m in self.marks.values() if m["kind"] == "new"]
        recents = [m for m in self.marks.values() if m["kind"] == "recent"]
        drops = [m for m in self.marks.values() if m["kind"] == "drop"]
        parts = ['<div style="border:1px solid #999;padding:6px 10px;margin-bottom:12px">']
        if self.active:
            parts.append(f"<p><b>All {self.total_current} current deals are shown.</b> "
                         f"{len(news)} new (yellow, red NEW badge), "
                         f"{len(recents)} recent, found in the last {RECENT_DAYS} days (lavender, purple RECENT badge), "
                         f"{len(drops)} price drop(s) (orange badge), {self.older} older with no tag. "
                         "New deals and price drops are listed first in each table, then recent ones.</p>")
        else:
            why = ("the history could not be read" if self.problem else
                   "the history started today" if (self.first_run or self.created == _today().isoformat())
                   else "the history has no start date")
            parts.append(f"<p><b>All {self.total_current} current deals are shown.</b> "
                         f"Nothing is tagged yet because {why}. "
                         "From the next day's report, new deals will be tagged.</p>")
            if self.problem:
                parts.append(f"<p>Note: {escape(self.problem)}.</p>")
        if news:
            parts.append("<p>New today:</p><ul>")
            for m in news[:MAX_LIST_IN_NOTE]:
                label = escape(m["title"])
                if m["url"]:
                    label = f'<a href="{escape(m["url"])}">{label}</a>'
                parts.append(f"<li>{label} at {escape(m['store'])}: {_money(m['price'])}</li>")
            if len(news) > MAX_LIST_IN_NOTE:
                parts.append(f"<li>and {len(news) - MAX_LIST_IN_NOTE} more, tagged below</li>")
            parts.append("</ul>")
        if drops:
            parts.append("<p>Price drops:</p><ul>")
            for m in drops[:MAX_LIST_IN_NOTE]:
                label = escape(m["title"])
                if m["url"]:
                    label = f'<a href="{escape(m["url"])}">{label}</a>'
                was = f"{_money(m['was'])} down to " if m.get("was") else ""
                parts.append(f"<li>{label} at {escape(m['store'])}: {was}{_money(m['price'])}</li>")
            parts.append("</ul>")
        parts.append("</div>")
        return "\n".join(parts)

    # ---------------------------------------------------------------- writing
    def save(self):
        """Writes the history file. Forgets deals not seen for HISTORY_GRACE_DAYS days."""
        if not self.applied:
            return False
        today_s = _today().isoformat()
        cutoff = (_today() - timedelta(days=HISTORY_GRACE_DAYS)).isoformat()
        kept = {k: v for k, v in self.deals.items() if str(v.get("last_seen") or today_s) >= cutoff}
        self.created = self.created or today_s
        data = {"version": FORMAT_VERSION, "created": self.created, "deals": kept}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, self.path)
        self.deals = kept
        return True

    def summary_line(self):
        if self.mode == "highlight":
            n_new = sum(1 for m in self.marks.values() if m["kind"] == "new")
            n_recent = sum(1 for m in self.marks.values() if m["kind"] == "recent")
            n_drop = sum(1 for m in self.marks.values() if m["kind"] == "drop")
            return (f"Deal history (highlight): {n_new} new, {n_recent} recent, {n_drop} price drop(s), "
                    f"{self.older} older, {len(self.deals)} deals remembered")
        return (f"Deal history: {self.new} new, {len(self.drops)} price drop(s), {self.hidden} hidden "
                f"(already reported), {len(self.deals)} deals remembered")


# -------------------------------------------------------------------------------------------------
# Plugging it into a bot
# -------------------------------------------------------------------------------------------------


def install(namespace, filename="seen-sealed.json", env_var="ONLY_NEW"):
    """
    Wraps the bot's analyse(), build_html() and main() (found in `namespace`, pass globals()).
    Does nothing unless the environment variable is set. Returns the History object, or None when it
    is switched off or cannot be installed.
    """
    mode = read_mode(env_var)
    if mode is None:
        return None
    needed = [n for n in ("analyse", "build_html", "main") if not callable(namespace.get(n))]
    if needed:
        print("deal_history: not installed because the bot has no " + ", ".join(needed) + " function. "
              "Place the two lines directly above  if __name__ == \"__main__\":", flush=True)
        return None
    base = os.path.dirname(os.path.abspath(namespace.get("__file__") or "."))
    path = filename if os.path.isabs(filename) else os.path.join(base, filename)
    hist = History(path, mode).load()

    original_analyse = namespace["analyse"]
    original_build = namespace["build_html"]
    original_main = namespace["main"]

    def analyse(*args, **kwargs):
        result = original_analyse(*args, **kwargs)
        if not (isinstance(result, tuple) and len(result) == 2 and all(isinstance(x, list) for x in result)):
            print("deal_history: analyse() did not return two lists, so nothing was filtered", flush=True)
            return result
        return hist.apply(result[0], result[1])

    def build_html(*args, **kwargs):
        html = hist.highlight_html(original_build(*args, **kwargs))
        note = hist.note_html()
        return note + "\n" + html if note else html

    def main(*args, **kwargs):
        code = original_main(*args, **kwargs)
        if code in (None, 0) and hist.applied:
            hist.save()
            print(hist.summary_line(), flush=True)
        return code

    namespace["analyse"], namespace["build_html"], namespace["main"] = analyse, build_html, main
    state = "new file" if hist.first_run else f"{len(hist.loaded)} deals remembered"
    print(f"Deal history on ({mode}): {os.path.basename(path)}, {state}", flush=True)
    return hist


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)

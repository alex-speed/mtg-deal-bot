#!/usr/bin/env python3
"""
deal_history.py: remembers the deals the sealed product bot has already reported, so each report
shows only the deals that are new since the last one.

Safe to run in production? Yes. It reads and writes one small JSON file next to the bot script and
makes no web requests. It changes nothing in your tenant or on any server.

How a bot uses it (two lines, placed directly below the store_config lines and above the line
if __name__ == "__main__":  ):

    import deal_history
    deal_history.install(globals(), "seen-sealed.json")

Nothing changes unless the environment variable ONLY_NEW is set to 1. When it is set:
  - the bot's analyse() result is filtered, so deals already reported are hidden
  - the report starts with a short note saying how many were hidden, and lists price drops
  - after the bot finishes without an error, the history file is updated

A deal counts as already reported when the same listing (store and page address) was shown in an
earlier report. A deal is shown again when its price falls by at least PRICE_DROP_PERCENT percent
and at least PRICE_DROP_MIN_DOLLARS below the lowest price reported so far. A deal that has not
appeared for HISTORY_GRACE_DAYS days is forgotten, so it counts as new if it comes back.

To start again from nothing, delete the history file.
"""
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlsplit

HISTORY_GRACE_DAYS = 10        # forget a deal that has not appeared for this many days
PRICE_DROP_PERCENT = 5.0       # show a reported deal again if its price falls by this much
PRICE_DROP_MIN_DOLLARS = 2.0   # and by at least this many dollars
TRUE_WORDS = {"1", "true", "yes", "on"}
FORMAT_VERSION = 1


def _today():
    return datetime.now(timezone.utc).date()


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


class History:
    def __init__(self, path):
        self.path = path
        self.loaded = {}          # what the file held when it was read
        self.deals = {}           # what will be written by save()
        self.first_run = False
        self.problem = None
        self.applied = False
        self.new = 0
        self.hidden = 0
        self.drops = []
        self.total_current = 0

    # ---------------------------------------------------------------- reading
    def load(self):
        self.loaded, self.problem, self.first_run = {}, None, False
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
        return self

    # ---------------------------------------------------------------- filtering
    def apply(self, outliers, unverified):
        """Returns (outliers, unverified) with already reported deals removed. Safe to call twice."""
        today = _today().isoformat()
        deals = copy.deepcopy(self.loaded)
        self.new, self.hidden, self.drops, self.total_current = 0, 0, [], 0
        shown = {"outlier": [], "unverified": []}
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
                                  "first_seen": today, "last_seen": today, "reported": today}
                    continue
                prev_price = float(prev["price"])
                dropped = prev_price - price
                if dropped >= PRICE_DROP_MIN_DOLLARS and price <= prev_price * (1 - PRICE_DROP_PERCENT / 100.0):
                    shown[kind].append(item)
                    self.drops.append({"title": item.get("title", ""), "store": item.get("store", ""),
                                       "url": item.get("url", ""), "was": prev_price, "now": price})
                    prev["price"] = price          # remember the lowest price reported
                    prev["reported"] = today
                else:
                    self.hidden += 1
                prev["last_seen"] = today
                prev["title"] = item.get("title", prev.get("title", ""))
        self.deals = deals
        self.applied = True
        return shown["outlier"], shown["unverified"]

    # ---------------------------------------------------------------- the report note
    def note_html(self):
        if not self.applied:
            return ""
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

    # ---------------------------------------------------------------- writing
    def save(self):
        """Writes the history file. Forgets deals not seen for HISTORY_GRACE_DAYS days."""
        if not self.applied:
            return False
        cutoff = (_today() - timedelta(days=HISTORY_GRACE_DAYS)).isoformat()
        kept = {k: v for k, v in self.deals.items() if str(v.get("last_seen") or _today().isoformat()) >= cutoff}
        data = {"version": FORMAT_VERSION, "deals": kept}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, self.path)
        self.deals = kept
        return True

    def summary_line(self):
        return (f"Deal history: {self.new} new, {len(self.drops)} price drop(s), {self.hidden} hidden "
                f"(already reported), {len(self.deals)} deals remembered")


# -------------------------------------------------------------------------------------------------
# Plugging it into a bot
# -------------------------------------------------------------------------------------------------


def install(namespace, filename="seen-sealed.json", env_var="ONLY_NEW"):
    """
    Wraps the bot's analyse(), build_html() and main() (found in `namespace`, pass globals()) so the report
    shows only new deals. Does nothing unless the environment variable is set to 1.
    Returns the History object, or None when it is switched off or cannot be installed.
    """
    if os.environ.get(env_var, "").strip().lower() not in TRUE_WORDS:
        return None
    needed = [n for n in ("analyse", "build_html", "main") if not callable(namespace.get(n))]
    if needed:
        print("deal_history: not installed because the bot has no " + ", ".join(needed) + " function. "
              "Place the two lines directly above  if __name__ == \"__main__\":", flush=True)
        return None
    base = os.path.dirname(os.path.abspath(namespace.get("__file__") or "."))
    path = filename if os.path.isabs(filename) else os.path.join(base, filename)
    hist = History(path).load()

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
        html = original_build(*args, **kwargs)
        note = hist.note_html()
        return note + "\n" + html if note else html

    def main(*args, **kwargs):
        code = original_main(*args, **kwargs)
        if code in (None, 0) and hist.applied:
            hist.save()
            print(hist.summary_line(), flush=True)
        return code

    namespace["analyse"], namespace["build_html"], namespace["main"] = analyse, build_html, main
    print(f"Deal history on: {os.path.basename(path)}" + (" (new file)" if hist.first_run else
                                                         f", {len(hist.loaded)} deals remembered"), flush=True)
    return hist


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)

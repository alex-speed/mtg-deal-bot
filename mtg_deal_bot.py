#!/usr/bin/env python3
"""
MTG deal bot (multi-store version).

Reads product listings from many Australian game stores, matches the same product across
stores, flags items that are far cheaper than every other store, and emails an HTML report
that also shows which stores worked.

Safe to run in production? It only makes read-only HTTP GET requests to public web pages
and sends one email. It does not change any system. It is a personal project, so do not
run it from a work server or a work tenant unless that is allowed.

Usage:
    python mtg_deal_bot.py --dry-run     # builds report.html, sends nothing
    python mtg_deal_bot.py               # builds the report and emails it
"""
import argparse
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape, unescape

import requests

# ---------------------------------------------------------------------------
# 1. Settings you are expected to edit
# ---------------------------------------------------------------------------

# Shopify stores: (store name, site address, [collection handles to try]).
# A handle is the last part of a collection URL, for example
#   https://goldfieldsgames.com.au/collections/magic-the-gathering  ->  "magic-the-gathering"
# The bot tries every handle. A handle that does not exist is skipped quietly as long as
# another handle for the same store works. Handles marked "seen" were taken from live pages
# in earlier searches. Handles marked "guess" are common names and may not exist.
SHOPIFY_STORES = [
    ("Goldfields", "https://goldfieldsgames.com.au",
     ["magic-the-gathering", "mtg-commander-decks", "mtg-booster-packs-boxes"]),          # seen
    ("Gameology", "https://www.gameology.com.au",
     ["magic-the-gathering-booster-boxes", "magic-the-gathering",
      "trading-card-game-clearance", "mtg-lorwyn-eclipsed", "magic-reality-fracture"]),   # seen
    ("Good Games", "https://www.goodgames.com.au",
     ["magic-the-gathering-marvel-super-heroes", "magic-the-gathering-reality-fracture",
      "magic-the-gathering-tarkir-dragonstorm", "magic-the-gathering"]),                  # first 3 seen
    ("Behold Games", "https://beholdgames.com.au",
     ["on-sale/magic-the-gathering", "magic-the-gathering-foundations-commander",
      "magic-the-gathering"]),                                                            # first 2 seen
    ("Games Empire", "https://gamesempire.com.au",
     ["clearance-sale/magic-the-gathering", "mid-year-sale/magic-the-gathering",
      "magic-the-gathering"]),                                                            # first 2 seen
    ("Combat Company", "https://thecombatcompany.com",
     ["magic-the-gathering-clearance", "magic-the-gathering"]),                           # first seen
    ("Tactical TCG", "https://www.tacticaltcg.com.au", ["magic-sealed"]),                 # seen
    ("Tabletop Commons", "https://tabletopcommons.com.au",
     ["magic-the-gathering-sealed"]),                                                     # seen
    ("AWNO Games", "https://www.awnogames.com.au",
     ["magic-the-gathering/sealedbox", "magic-the-gathering"]),                           # seen
    ("Cherry Collectables", "https://www.cherrycollectables.com.au",
     ["magic-the-gathering"]),                                                            # seen
    ("Collector Smith", "https://www.thecollectorsmith.com.au", ["mtg-cards"]),           # seen
    ("Aussie Hobbies", "https://www.aussiehobbies.com.au", ["magic-the-gathering"]),      # guess
    ("Gap Games", "https://www.gapgames.com.au", ["magic-the-gathering"]),                # guess
    ("Pokebox", "https://www.pokebox.com.au", ["magic-the-gathering"]),                   # guess
    ("Mighty Toys", "https://mightytoys.com.au", ["magic-the-gathering"]),                # guess
    ("GB Toys", "https://gbtoys.com.au", ["magic-the-gathering"]),                        # guess
    ("Mint Collectables", "https://mintcollectables.com.au", ["magic-the-gathering"]),    # guess
]

# WooCommerce stores: (store name, site address). The bot uses WooCommerce's public Store API
# and searches for "magic the gathering". This part has not been tested against live sites.
WOO_STORES = [
    ("Games World", "https://www.gamesworld.com.au"),
    ("JToys", "https://www.jtoys.com.au"),
    ("TC Game Australia", "https://tcgameaustralia.com"),
    ("Mind Games", "https://www.m-g.com.au"),
]

# Titles containing any of these words are ignored (whole words, plural allowed).
EXCLUDE_WORDS = [
    "teenage mutant", "tmnt", "ninja turtles",
    "sleeve", "playmat", "play-mat", "binder", "deck box", "deck case", "dice", "zipfolio",
    "token set",
]

# A title must contain at least one of these to count as a sealed product (this drops singles).
SEALED_WORDS = ["booster", "bundle", "deck", "draft night", "jumpstart", "scene box", "beginner",
                "prerelease", "starter", "display", "box", "kit", "pack"]

REQUIRE_MTG_WORD = True        # title must also contain "magic" or "mtg"
MIN_GAP_VS_OTHERS = 0.25       # flag if price is 25% or more below the cheapest other store
MIN_DISCOUNT_UNVERIFIED = 0.45 # no comparison available: list if 45%+ off its own "was" price
NEW_LISTING_DAYS = 3           # tag products published within this many days as NEW
MAX_PAGES = 4                  # pages of 250 products per collection (Shopify)
REQUEST_DELAY = 1.0            # seconds to wait between requests, to be polite to small shops
TIMEOUT_SECONDS = 20
USER_AGENT = "Mozilla/5.0 (personal price checker)"

# ---------------------------------------------------------------------------
# 2. Fetching and cleaning product data
# ---------------------------------------------------------------------------

_EXCLUDE_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in EXCLUDE_WORDS) + r")s?\b", re.I)
_SEALED_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in SEALED_WORDS) + r")s?\b", re.I)
_MTG_RE = re.compile(r"\b(?:magic|mtg)\b", re.I)


def wanted(title):
    """True if the title looks like a sealed MTG product we care about."""
    if _EXCLUDE_RE.search(title):
        return False
    if not _SEALED_RE.search(title):
        return False
    if REQUIRE_MTG_WORD and not _MTG_RE.search(title):
        return False
    return True


def http_get_json(url, params=None):
    time.sleep(REQUEST_DELAY)
    resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def parse_shopify(store, base, p):
    title = p.get("title", "")
    if not wanted(title):
        return None
    in_stock = [v for v in p.get("variants", []) if v.get("available")]
    if not in_stock:
        return None
    cheapest = min(in_stock, key=lambda v: float(v["price"]))
    price = float(cheapest["price"])
    was = float(cheapest["compare_at_price"]) if cheapest.get("compare_at_price") else 0.0
    if price <= 0:
        return None
    return make_item(store, title, f"{base}/products/{p.get('handle', '')}", price, was, p.get("published_at"))


def parse_woo(store, p):
    title = unescape(p.get("name", ""))
    if not wanted(title) or not p.get("is_in_stock"):
        return None
    prices = p.get("prices") or {}
    try:
        minor = 10 ** int(prices.get("currency_minor_unit", 2))
        price = int(prices["price"]) / minor
        regular = int(prices.get("regular_price") or 0) / minor
    except (KeyError, ValueError, TypeError):
        return None
    if price <= 0:
        return None
    return make_item(store, title, p.get("permalink", ""), price, regular if regular > price else 0.0, None)


def make_item(store, title, url, price, was, published):
    return {"store": store, "title": title, "url": url, "price": price,
            "was": was if was > price else 0.0, "published": published}


def fetch_all():
    """Returns (items, stats). stats is {store: {...}} used for the report."""
    items, seen, stats = [], set(), {}

    def add(item, st):
        if item and item["url"] and item["url"] not in seen:
            seen.add(item["url"])
            items.append(item)
            st["products"] += 1

    for store, base, handles in SHOPIFY_STORES:
        st = stats[store] = {"kind": "Shopify", "tried": len(handles), "ok": 0, "products": 0, "errors": []}
        for handle in handles:
            for page in range(1, MAX_PAGES + 1):
                url = f"{base}/collections/{handle}/products.json"
                try:
                    products = http_get_json(url, {"limit": 250, "page": page}).get("products", [])
                except Exception as exc:
                    st["errors"].append(f"{handle}: {exc}")
                    break
                if page == 1:
                    st["ok"] += 1
                if not products:
                    break
                for p in products:
                    add(parse_shopify(store, base, p), st)
                if len(products) < 250:
                    break

    for store, base in WOO_STORES:
        st = stats[store] = {"kind": "WooCommerce", "tried": 1, "ok": 0, "products": 0, "errors": []}
        for page in range(1, MAX_PAGES + 1):
            try:
                products = http_get_json(f"{base}/wp-json/wc/store/v1/products",
                                         {"search": "magic the gathering", "per_page": 100, "page": page})
            except Exception as exc:
                st["errors"].append(str(exc))
                break
            if page == 1:
                st["ok"] += 1
            for p in products:
                add(parse_woo(store, p), st)
            if len(products) < 100:
                break
    return items, stats


# ---------------------------------------------------------------------------
# 3. Matching the same product across stores
# ---------------------------------------------------------------------------

# Words describing the kind of product. Two listings only match if these are identical,
# so a Play Booster Box never matches a Play Booster Pack.
TYPE_WORDS = {
    "bundle", "booster", "box", "commander", "deck", "draft", "night", "collector", "play",
    "jumpstart", "scene", "beginner", "prerelease", "pack", "display", "gift", "starter", "kit",
    "theme", "set",
}
# Words that carry no meaning for matching.
NOISE_WORDS = {"magic", "mtg", "the", "gathering", "tcg", "wizards", "of", "coast", "card", "game",
               "trading", "single", "and", "per", "sealed", "new", "english", "version",
               "marvel", "universes", "beyond"}
_PLURALS = {"boosters": "booster", "packs": "pack", "decks": "deck", "bundles": "bundle", "boxes": "box"}


def title_tokens(title):
    t = title.lower().replace("superheroes", "super heroes")
    t = t.replace("spider-man", "spiderman").replace("spider man", "spiderman")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    out = set()
    for w in t.split():
        w = _PLURALS.get(w, w)
        if len(w) == 1 and w.isalpha():
            continue
        if w.isdigit() and 10 <= int(w) <= 99:   # "30 packs", "24 boosters"
            continue
        out.add(w)
    # "Play Booster Display" and "Play Booster Box" are the same thing
    if "booster" in out and "display" in out and "commander" not in out:
        out.discard("display")
        out.add("box")
    return out


def prepare(items):
    for it in items:
        tok = title_tokens(it["title"])
        it["type"] = frozenset(tok & TYPE_WORDS)
        it["name"] = tok - TYPE_WORDS - NOISE_WORDS


def same_product(a, b):
    if a["type"] != b["type"] or not a["name"] or not b["name"]:
        return False
    return len(a["name"] & b["name"]) / len(a["name"] | b["name"]) >= 0.7


# ---------------------------------------------------------------------------
# 4. Finding the outliers
# ---------------------------------------------------------------------------


def is_new(item):
    if not item["published"]:
        return False
    try:
        published = datetime.fromisoformat(item["published"])
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published >= datetime.now(timezone.utc) - timedelta(days=NEW_LISTING_DAYS)


def analyse(items):
    prepare(items)
    groups = defaultdict(list)
    for it in items:
        groups[it["type"]].append(it)

    outliers, unverified = [], []
    for it in items:
        others = [o for o in groups[it["type"]] if o["store"] != it["store"] and same_product(it, o)]
        if others:
            cheapest_other = min(others, key=lambda o: o["price"])
            gap = 1 - it["price"] / cheapest_other["price"]
            if gap >= MIN_GAP_VS_OTHERS:
                outliers.append({
                    **it, "gap": gap,
                    "next_store": cheapest_other["store"], "next_price": cheapest_other["price"],
                    "median_others": statistics.median(o["price"] for o in others),
                    "n_stores": len({o["store"] for o in others}),
                    "new": is_new(it),
                })
        elif it["was"] and (1 - it["price"] / it["was"]) >= MIN_DISCOUNT_UNVERIFIED:
            unverified.append({**it, "discount": 1 - it["price"] / it["was"], "new": is_new(it)})
    outliers.sort(key=lambda r: r["gap"], reverse=True)
    unverified.sort(key=lambda r: r["discount"], reverse=True)
    return outliers, unverified


# ---------------------------------------------------------------------------
# 5. Building and sending the email
# ---------------------------------------------------------------------------


def store_summary(stats):
    """Returns (working store count, total store count, problem lines)."""
    working = sum(1 for s in stats.values() if s["ok"] and s["products"])
    problems = []
    for name, s in stats.items():
        if s["ok"] == 0:
            first = s["errors"][0] if s["errors"] else "no response"
            problems.append(f"{name}: no collection could be read (first error: {first})")
        elif s["products"] == 0:
            problems.append(f"{name}: readable, but no in-stock sealed MTG products matched the filters")
    return working, len(stats), problems


def build_html(outliers, unverified, stats, items_checked):
    working, total, problems = store_summary(stats)

    def link(r):
        tag = " (NEW)" if r["new"] else ""
        return f'<a href="{escape(r["url"])}">{escape(r["title"])}</a> at {escape(r["store"])}{tag}'

    parts = [f"<p>Checked {items_checked} in-stock sealed products from {working} of {total} stores "
             f"on {datetime.now().strftime('%a %d %b %Y')}.</p>"]

    parts.append("<h3>Outliers (far cheaper than every other store I matched)</h3>")
    if outliers:
        parts.append('<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">'
                     "<tr><th>Item</th><th>Price</th><th>Next cheapest</th><th>Gap</th>"
                     "<th>Median elsewhere</th><th>Other stores</th></tr>")
        for r in outliers:
            parts.append(
                f"<tr><td>{link(r)}</td><td>${r['price']:.2f}</td>"
                f"<td>${r['next_price']:.2f} ({escape(r['next_store'])})</td>"
                f"<td>{r['gap']:.0%}</td><td>${r['median_others']:.2f}</td><td>{r['n_stores']}</td></tr>")
        parts.append("</table>")
        parts.append("<p>A gap based on only one other store is weak evidence. Higher 'Other stores' counts are more reliable.</p>")
    else:
        parts.append("<p>None today.</p>")

    parts.append("<h3>Around half price, but no other store to compare against</h3>")
    if unverified:
        parts.append("<ul>")
        for r in unverified:
            parts.append(f"<li>{link(r)}: ${r['price']:.2f}, was ${r['was']:.2f} ({r['discount']:.0%} off). "
                         "The 'was' price may be inflated.</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>None today.</p>")

    parts.append("<h3>Store check</h3>")
    parts.append('<table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse">'
                 "<tr><th>Store</th><th>Type</th><th>Collections read</th><th>Products kept</th></tr>")
    for name, s in stats.items():
        parts.append(f"<tr><td>{escape(name)}</td><td>{s['kind']}</td>"
                     f"<td>{s['ok']} of {s['tried']}</td><td>{s['products']}</td></tr>")
    parts.append("</table>")

    if problems:
        parts.append("<h3>Problems</h3><ul>" + "".join(f"<li>{escape(p)}</li>" for p in problems) + "</ul>")

    parts.append("<p>Matching products across stores by title is approximate. Check the item pages before buying, "
                 "and add shipping to the price.</p>")
    return "\n".join(parts)


def send_mail(subject, html):
    """Send via Microsoft Graph using an app registration (client credentials)."""
    tenant, client_id = os.environ["GRAPH_TENANT_ID"], os.environ["GRAPH_CLIENT_ID"]
    secret, sender, recipient = os.environ["GRAPH_CLIENT_SECRET"], os.environ["MAIL_FROM"], os.environ["MAIL_TO"]

    token = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"client_id": client_id, "client_secret": secret,
              "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
        timeout=TIMEOUT_SECONDS,
    )
    token.raise_for_status()
    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
        json={"message": {"subject": subject,
                          "body": {"contentType": "HTML", "content": html},
                          "toRecipients": [{"emailAddress": {"address": recipient}}]},
              "saveToSentItems": False},
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="write report.html instead of emailing")
    args = parser.parse_args()

    items, stats = fetch_all()
    outliers, unverified = analyse(items)
    working, total, _ = store_summary(stats)
    html = build_html(outliers, unverified, stats, len(items))
    subject = f"MTG deals: {len(outliers)} outlier(s), {len(unverified)} unverified, {working}/{total} stores OK"

    if args.dry_run:
        with open("report.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(subject, "-> report.html written")
    else:
        send_mail(subject, html)
        print(subject, "-> email sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())

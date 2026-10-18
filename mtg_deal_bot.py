#!/usr/bin/env python3
"""
MTG deal bot.

Reads product feeds from Australian game stores that run on Shopify, matches the same
product across stores, flags items that are far cheaper than every other store, and
emails an HTML report.

Safe to run in production? It only makes read-only HTTP GET requests to public web pages
and sends one email. It does not change any system. It is still a personal project, so
do not run it from a work server or a work tenant unless that is allowed.

Usage:
    python mtg_deal_bot.py --dry-run     # builds report.html, sends nothing
    python mtg_deal_bot.py               # builds the report and emails it
"""
import argparse
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from html import escape

import requests

# ---------------------------------------------------------------------------
# 1. Settings you are expected to edit
# ---------------------------------------------------------------------------

# (store name, collection URL). Each must be a Shopify collection. Check that
# <url>/products.json opens in your browser before adding a new one.
COLLECTIONS = [
    ("Goldfields", "https://goldfieldsgames.com.au/collections/magic-the-gathering"),
    ("Goldfields", "https://goldfieldsgames.com.au/collections/mtg-commander-decks"),
    ("Gameology", "https://www.gameology.com.au/collections/magic-the-gathering-booster-boxes"),
]

# Anything with these words in the title is ignored (case-insensitive).
EXCLUDE_WORDS = [
    "teenage mutant", "tmnt",
    "sleeve", "playmat", "play-mat", "binder", "deck box", "dice", "zipfolio", "deck case",
    "collector booster box", "case",
]

MIN_GAP_VS_OTHERS = 0.25      # flag if price is 25% or more below the cheapest other store
MIN_DISCOUNT_UNVERIFIED = 0.45  # no comparison available: list if 45%+ off its own "was" price
NEW_LISTING_DAYS = 3          # tag products published within this many days as NEW
MAX_PAGES = 5                 # pages of 250 products per collection
TIMEOUT_SECONDS = 20
USER_AGENT = "Mozilla/5.0 (personal price checker)"

# ---------------------------------------------------------------------------
# 2. Fetching and cleaning product data
# ---------------------------------------------------------------------------


def fetch_items(problems):
    """Download products from every collection. Failures are added to `problems`."""
    items = []
    seen = set()  # the same product can sit in more than one collection
    for store, url in COLLECTIONS:
        base = "/".join(url.split("/")[:3])  # https://domain
        for page in range(1, MAX_PAGES + 1):
            feed = f"{url.rstrip('/')}/products.json?limit=250&page={page}"
            try:
                resp = requests.get(feed, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
                resp.raise_for_status()
                products = resp.json().get("products", [])
            except Exception as exc:  # network error, 403, bad JSON, and so on
                problems.append(f"{store}: could not read {feed} ({exc})")
                break
            if not products:
                break
            for p in products:
                item = parse_product(store, base, p)
                if item and item["url"] not in seen:
                    seen.add(item["url"])
                    items.append(item)
    return items


def parse_product(store, base, p):
    """Turn one Shopify product record into a simple dict. Returns None if unusable."""
    title = p.get("title", "")
    if any(word in title.lower() for word in EXCLUDE_WORDS):
        return None
    in_stock = [v for v in p.get("variants", []) if v.get("available")]
    if not in_stock:
        return None
    cheapest = min(in_stock, key=lambda v: float(v["price"]))
    price = float(cheapest["price"])
    was = float(cheapest["compare_at_price"]) if cheapest.get("compare_at_price") else 0.0
    if price <= 0:
        return None
    return {
        "store": store,
        "title": title,
        "url": f"{base}/products/{p.get('handle', '')}",
        "price": price,
        "was": was if was > price else 0.0,
        "published": p.get("published_at"),
    }


# ---------------------------------------------------------------------------
# 3. Matching the same product across stores
# ---------------------------------------------------------------------------

# Words that describe the kind of product. Two listings only match if these are identical,
# so a Play Booster Box never matches a Play Booster Pack.
TYPE_WORDS = {
    "bundle", "booster", "box", "commander", "deck", "draft", "night", "collector", "play",
    "jumpstart", "scene", "beginner", "prerelease", "pack", "display", "gift", "starter", "kit",
    "theme", "set",
}
# Words that carry no meaning for matching.
NOISE_WORDS = {"magic", "mtg", "the", "gathering", "tcg", "wizards", "of", "coast", "card", "game",
               "trading", "single", "a", "and", "x"}


def tokens(title):
    t = title.lower().replace("superheroes", "super heroes").replace("spider-man", "spiderman")
    return set(re.findall(r"[a-z0-9]+", t))


def same_product(a, b):
    ta, tb = tokens(a["title"]), tokens(b["title"])
    if (ta & TYPE_WORDS) != (tb & TYPE_WORDS):
        return False
    na, nb = ta - TYPE_WORDS - NOISE_WORDS, tb - TYPE_WORDS - NOISE_WORDS
    if not na or not nb:
        return False
    return len(na & nb) / len(na | nb) >= 0.7


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
    outliers, unverified = [], []
    for item in items:
        others = [o for o in items if o["store"] != item["store"] and same_product(item, o)]
        if others:
            cheapest_other = min(others, key=lambda o: o["price"])
            gap = 1 - item["price"] / cheapest_other["price"]
            if gap >= MIN_GAP_VS_OTHERS:
                outliers.append({
                    **item,
                    "gap": gap,
                    "next_store": cheapest_other["store"],
                    "next_price": cheapest_other["price"],
                    "median_others": statistics.median(o["price"] for o in others),
                    "new": is_new(item),
                })
        elif item["was"] and (1 - item["price"] / item["was"]) >= MIN_DISCOUNT_UNVERIFIED:
            unverified.append({**item, "discount": 1 - item["price"] / item["was"], "new": is_new(item)})
    outliers.sort(key=lambda r: r["gap"], reverse=True)
    unverified.sort(key=lambda r: r["discount"], reverse=True)
    return outliers, unverified


# ---------------------------------------------------------------------------
# 5. Building and sending the email
# ---------------------------------------------------------------------------


def build_html(outliers, unverified, problems, items_checked):
    def link(r):
        tag = " (NEW)" if r["new"] else ""
        return f'<a href="{escape(r["url"])}">{escape(r["title"])}</a> at {escape(r["store"])}{tag}'

    parts = [f"<p>Checked {items_checked} in-stock products on {datetime.now().strftime('%a %d %b %Y')}.</p>"]

    parts.append("<h3>Outliers (far cheaper than every other store I matched)</h3>")
    if outliers:
        parts.append('<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">'
                     "<tr><th>Item</th><th>Price</th><th>Next cheapest</th><th>Gap</th><th>Median elsewhere</th></tr>")
        for r in outliers:
            parts.append(
                f"<tr><td>{link(r)}</td><td>${r['price']:.2f}</td>"
                f"<td>${r['next_price']:.2f} ({escape(r['next_store'])})</td>"
                f"<td>{r['gap']:.0%}</td><td>${r['median_others']:.2f}</td></tr>")
        parts.append("</table>")
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

    problems = []
    items = fetch_items(problems)
    outliers, unverified = analyse(items)
    html = build_html(outliers, unverified, problems, len(items))
    subject = f"MTG deals: {len(outliers)} outlier(s), {len(unverified)} unverified"
    if problems:
        subject += " (with problems)"

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

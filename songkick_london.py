#!/usr/bin/env python3
"""Scrape Songkick's London metro-area listings for every artist playing on one day.

Walks https://www.songkick.com/metro-areas/24426-uk-london filtered to a single day
(filters[minDate]/filters[maxDate]; the unfiltered listing only shows ~10 per day)
page by page until a page returns no events, pulling each event's schema.org JSON-LD
(MusicEvent) block, with an HTML fallback for the artist names. Standard library only.

Usage:
    python3 songkick_london.py                     # today's gigs -> london_events.csv, london_artists.txt
    python3 songkick_london.py --date 2026-11-01 --delay 2
"""

import argparse
import csv
import datetime
import html
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://www.songkick.com/metro-areas/24426-uk-london"
DEFAULT_UA = "london-gigs-scraper/1.0 (personal research; contact: you@example.com)"

LD_JSON_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I
)
# Fallback: <p class="artists ..."> ... <strong>Artist</strong> ...</p>
ARTISTS_P_RE = re.compile(r'<p class="artists[^"]*">(.*?)</p>', re.S | re.I)
STRONG_RE = re.compile(r"<strong[^>]*>(.*?)</strong>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")


def fetch(url, user_agent, retries=4):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        },
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ""
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After") or 0) or 2 ** (attempt + 2)
                print(f"  HTTP {e.code}, retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 2))
                continue
            raise
    return ""


def _iter_ld_objects(data):
    """JSON-LD may be a single object, a list, or wrapped in @graph."""
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld_objects(item)
    elif isinstance(data, dict):
        if "@graph" in data:
            yield from _iter_ld_objects(data["@graph"])
        else:
            yield data


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_events(page_html):
    """Return a list of event dicts from one listings page."""
    events = []
    for raw in LD_JSON_RE.findall(page_html):
        try:
            data = json.loads(html.unescape(raw.strip()))
        except json.JSONDecodeError:
            continue
        for obj in _iter_ld_objects(data):
            types = _as_list(obj.get("@type"))
            if not any(t.endswith("Event") for t in types if isinstance(t, str)):
                continue
            location = obj.get("location") or {}
            if isinstance(location, list):
                location = location[0] if location else {}
            address = location.get("address") or {}
            if isinstance(address, dict):
                locality = address.get("addressLocality", "")
            else:
                locality = str(address)
            performers = [
                ((p.get("name") or "") if isinstance(p, dict) else str(p)).strip()
                for p in _as_list(obj.get("performer"))
            ]
            events.append(
                {
                    "name": obj.get("name", ""),
                    "date": (obj.get("startDate") or "")[:10],
                    "venue": location.get("name", ""),
                    "locality": locality,
                    "url": (obj.get("url") or "").split("?")[0],
                    "performers": [p for p in performers if p],
                }
            )
    if events:
        return events

    # Fallback when the page has no JSON-LD: scrape the artist lines directly.
    for block in ARTISTS_P_RE.findall(page_html):
        names = [html.unescape(TAG_RE.sub("", s)).strip() for s in STRONG_RE.findall(block)]
        if names:
            events.append(
                {"name": "", "date": "", "venue": "", "locality": "", "url": "", "performers": names}
            )
    return events


def london_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    except Exception:
        return datetime.date.today().isoformat()


def scrape(target_date, max_pages, delay, user_agent):
    all_events, seen = [], set()
    sk_date = datetime.date.fromisoformat(target_date).strftime("%m/%d/%Y")
    page = 1
    while max_pages is None or page <= max_pages:
        query = urllib.parse.urlencode(
            {"filters[minDate]": sk_date, "filters[maxDate]": sk_date, "page": page}
        )
        url = f"{BASE_URL}?{query}"
        print(f"Fetching page {page}: {url}", file=sys.stderr)
        events = parse_events(fetch(url, user_agent))
        if not events:
            print("  no events found, stopping", file=sys.stderr)
            break

        if not any(ev["date"] for ev in events):
            print("  page has no event dates (HTML fallback), can't filter by day, stopping", file=sys.stderr)
            break

        new = matched = 0
        for ev in events:
            key = ev["url"] or (ev["date"], ev["venue"], tuple(ev["performers"]))
            if key in seen:
                continue
            seen.add(key)
            new += 1
            if ev["date"] == target_date:
                all_events.append(ev)
                matched += 1
        print(f"  {len(events)} events ({matched} on {target_date}), {len(all_events)} total", file=sys.stderr)
        # The date check guards against Songkick ignoring the filter (listings are
        # in date order); it also serves the last page again for out-of-range pages.
        if any(ev["date"] > target_date for ev in events) or new == 0:
            break

        page += 1
        time.sleep(delay + random.uniform(0, delay / 2))
    return all_events


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=london_today(), help="YYYY-MM-DD to fetch (default: today, London time)")
    ap.add_argument("--max-pages", type=int, default=None, help="stop after N pages (default: all)")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests (default: 1.5)")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    ap.add_argument("--events-csv", default="london_events.csv")
    ap.add_argument("--artists-out", default="london_artists.txt")
    args = ap.parse_args()

    try:
        datetime.date.fromisoformat(args.date)
    except ValueError:
        ap.error(f"--date must be YYYY-MM-DD, got {args.date!r}")

    events = scrape(args.date, args.max_pages, args.delay, args.user_agent)

    with open(args.events_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "artist", "venue", "locality", "event_name", "url"])
        for ev in events:
            for artist in ev["performers"]:
                w.writerow([ev["date"], artist, ev["venue"], ev["locality"], ev["name"], ev["url"]])

    artists = sorted({a for ev in events for a in ev["performers"]}, key=str.casefold)
    with open(args.artists_out, "w", encoding="utf-8") as f:
        f.write("\n".join(artists) + "\n")

    print(
        f"Done: {len(events)} events on {args.date}, {len(artists)} unique artists -> "
        f"{args.events_csv}, {args.artists_out}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Weekly Sortlist fuel extraction for the outreach bot.

Pulls agency leads from Sortlist directory pages (free, public) and
appends the new ones to the Sheet Directory tab, where bridge.py
picks them up unchanged.

Why Sortlist: a 2026-11-10 hunt found its listing pages embed full
agency records (name, website, city, team_size) in a
<script id="__NEXT_DATA__"> JSON block. One category x country page
holds 20-100 agencies; Belgium "Web Application" alone lists ~494,
US "Web Design" 932, Germany "Web Design" 1,187. Across the
categories x countries below this is tens of thousands of agencies
with websites - same order as Overture, and a different pool (only
~13 of 26 on the Belgium test page were already in our Sheet).

Design (measured 2026-11-10):
- Listing URL: https://www.sortlist.com/s/<category>/<country>?page=N
  (UK pages live on https://www.sortlist.co.uk instead; some pairs
  exist only as /i/s/<category>/<country>, which serves the same
  listing).
  Each page embeds agency records in __NEXT_DATA__ under
  props.pageProps.data.{organicAgencies,paidAgencies}: JSON:API
  collections where data[] items link via
  relationships.agency.data.id to included[] records of type
  "agency" carrying name, website, team_size and addresses
  (addresses[0].address.en is "City, Country").
- Pagination quirk (verified): page 1 returns 20 organic items and
  its meta.pagination.last is UI-based (20/page, e.g. last=25 for
  494 records). Pages 2+ return 100 items each and their meta.last
  is API-based (e.g. last=5 -> 6 pages total). So the walk fetches
  page 1, then page 2 to learn the real total (last+1), then pages
  3..total. It stops early on an empty page or a 404, and caps at
  60 pages per pair. Pages never overlap (verified: 0 shared names
  across pages 1-3), and domains are deduped across pages anyway.
- Pairs come from Sortlist's own directory sitemaps
  (sitemap.xml -> */directory*.xml.gz, both .com and .co.uk):
  only category x country pairs that exist in the sitemap are
  fetched, so no requests are wasted on 404s (e.g. there is no
  country-level /s/video/belgium-be or /s/seo/germany-de page, and
  no UK pages on .com at all). If both sitemap fetches fail, the
  script falls back to the hardcoded category x country cartesian
  product and treats 404 as an empty pair.
- Rows are filtered by country (US + EU-27 + GB + CH + NO, country
  level only - city pages are subsets of the country page) and
  Pakistan/India are explicitly excluded (standing rule: never
  target Pakistan/India).
- Dedupe is against the live Sheet Directory + Queue websites, using
  the same domain normalisation as bridge.py, so a domain already
  known or already contacted is never re-appended.
- Rows use the Directory format bridge.py expects: place_key
  "sortlist:<domain>", category "design agency", niche mapped from
  the Sortlist category, query "sortlist import".

Usage (weekly workflow, 6 shards):
    python scripts/sortlist_extract.py --shard 0 --shards 6
    python scripts/sortlist_extract.py --dry-run --categories web-design --countries belgium-be --max-pages 3
    python scripts/sortlist_extract.py --dry-run --categories web-design,seo --countries united-states-us,germany-de

Secrets: SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON (same as the other
scripts). --dry-run reads the Sheet (for dedupe) but writes nothing.
Dry-run prints the first 5 rows it would append, so run it manually
only; the scheduled run prints counts only, never lead data (public
repo logs are public).
"""
import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sheets  # noqa: E402

CHROME_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SITEMAP_INDEXES = (
    "https://www.sortlist.com/sitemap.xml",
    "https://www.sortlist.co.uk/sitemap.xml",
)

DIRECTORY_HEADERS = [
    "name", "category", "rating", "reviews", "address", "postcode",
    "town", "phone", "website", "hours", "maps_url", "place_key",
    "query", "locality", "niche", "scraped_at",
]

# Sortlist URL slug -> our 22 Directory niches (see
# scripts/niche_priority.json). Slugs are the /s/ URL slugs, verified
# against Sortlist's directory sitemaps 2026-11-10 (each has at least
# one country-level page). "design" is Graphic Design, "video" is
# Video Production, "ux-design" is Ergonomy (UX/UI), "app-development"
# is Mobile App. web-development and web-application are separate
# directory taxonomies (208 vs 696 locations) - both are kept.
CATEGORIES = {
    "web-design": "web design agency",
    "web-development": "web development company",
    "web-application": "web development company",
    "digital-marketing": "digital agency",
    "seo": "SEO agency",
    "branding": "branding agency",
    "design": "graphic design agency",
    "social-media": "social media agency",
    "content-marketing": "content marketing agency",
    "ecommerce": "ecommerce agency",
    "app-development": "mobile app agency",
    "advertising": "advertising agency",
    "video": "video production agency",
    "ux-design": "UI UX agency",
    "software-development": "software development agency",
    "product-design": "product design agency",
    "wordpress-development": "wordpress agency",
    "shopify-development": "shopify agency",
    "animation": "motion design agency",
}

# Sortlist country slug -> (ISO code, country name). UK pages live on
# sortlist.co.uk; every other country lives on www.sortlist.com.
# Slugs verified against the directory sitemaps 2026-11-10.
COUNTRIES = {
    "united-states-us": ("US", "United States"),
    "united-kingdom-gb": ("GB", "United Kingdom"),
    "germany-de": ("DE", "Germany"),
    "france-fr": ("FR", "France"),
    "netherlands-nl": ("NL", "Netherlands"),
    "belgium-be": ("BE", "Belgium"),
    "spain-es": ("ES", "Spain"),
    "italy-it": ("IT", "Italy"),
    "switzerland-ch": ("CH", "Switzerland"),
    "austria-at": ("AT", "Austria"),
    "sweden-se": ("SE", "Sweden"),
    "denmark-dk": ("DK", "Denmark"),
    "norway-no": ("NO", "Norway"),
    "ireland-ie": ("IE", "Ireland"),
    "poland-pl": ("PL", "Poland"),
    "portugal-pt": ("PT", "Portugal"),
}

# Host per country: UK lives on sortlist.co.uk, the rest on .com.
COUNTRY_HOST = {"GB": "www.sortlist.co.uk"}
DEFAULT_HOST = "www.sortlist.com"

IN_SCOPE_COUNTRIES = set(code for code, _ in COUNTRIES.values())
EXCLUDED_COUNTRIES = {"PK", "IN"}  # standing rule: never target Pakistan/India

# Same skip list as bridge.py: a "website" that is really a social /
# directory profile is not a business website.
SKIP_DOMAINS = [
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com",
    "x.com", "youtube.com", "clutch.co", "upwork.com", "fiverr.com",
]

APPEND_CHUNK = 500  # rows per Sheet append call (60 writes/min limit)
FETCH_DELAY = 0.75  # seconds between page fetches (be polite)
FETCH_TIMEOUT = 30
MAX_PAGES_PER_PAIR = 60  # safety cap; real pairs are far smaller
MAX_RETRIES = 2  # extra attempts on 429/5xx


def domain_of(url):
    """Host, lowercased, no www. Same as bridge.py, except Sortlist
    sometimes stores a bare domain (no scheme), which urlparse would
    read as a path, so a scheme is added first when missing."""
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    try:
        return urllib.parse.urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def full_url(url):
    """Website as a full URL (bridge fetches https://<domain>, and a
    schemeless value would give it an empty domain)."""
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    return u


def http_get(url, timeout=FETCH_TIMEOUT):
    """GET with the full Chrome UA. Returns (status, body_text).

    Plain urllib's default UA gets blocked by Sortlist (same lesson
    as bridge/Groq: Cloudflare/WAFs refuse bare script UAs).
    Retries 429/5xx once with a longer pause. A 404 is returned,
    not raised - a missing listing page just means an empty pair.
    """
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": CHROME_UA,
                              "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                # Decompress on the Content-Encoding header OR on the
                # gzip magic bytes: Sortlist serves its sitemaps as
                # .gz files whose bodies are gzip even when the
                # header does not say so (sitemap.xml 301s to
                # /sitemaps/2/sitemap.xml.gz).
                if (r.headers.get("Content-Encoding") == "gzip"
                        or raw[:2] == b"\x1f\x8b"):
                    try:
                        raw = gzip.decompress(raw)
                    except (OSError, EOFError):
                        pass
                return r.status, raw.decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < MAX_RETRIES:
                time.sleep(FETCH_DELAY * (attempt + 2))
                continue
            return e.code, ""
        except Exception as e:  # timeout / connection reset
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(FETCH_DELAY * (attempt + 2))
                continue
            return None, str(e)
    return None, str(last_err) if last_err else ""


def sitemap_pairs(work_dir):
    """(host, prefix, category, country_slug) pairs from Sortlist's
    directory sitemaps. prefix is "" for /s/... URLs and "i/" for
    /i/s/... URLs (both forms serve the same listing; some pairs
    exist only in one form). Only pairs whose listing URL exists in
    a sitemap are returned, so no fetch is wasted on a 404.

    Falls back to the hardcoded cartesian product when both sitemap
    indexes fail (the caller treats 404 as an empty pair then).
    """
    os.makedirs(work_dir, exist_ok=True)
    pairs = set()
    sitemap_ok = False
    for index_url in SITEMAP_INDEXES:
        status, body = http_get(index_url)
        if status != 200 or not body:
            print(f"sortlist: sitemap index {index_url} -> {status}",
                  flush=True)
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall(".//s:loc", ns):
            sm_url = (loc.text or "").strip()
            if "directory" not in sm_url:
                continue
            # Host in the cache name: .com and .co.uk both have a
            # directory.xml.gz, and sharing one cache file would
            # silently drop the UK sitemap (found 2026-11-10).
            host = urllib.parse.urlparse(sm_url).netloc
            fname = host + "__" + sm_url.rsplit("/", 1)[-1]
            dest = os.path.join(work_dir, fname)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                with open(dest, "rb") as fh:
                    raw = fh.read()
            else:
                status2, body2 = http_get(sm_url)
                if status2 != 200 or not body2:
                    continue
                # http_get already gunzips; store the plain XML.
                raw = body2.encode("utf-8")
                with open(dest, "wb") as fh:
                    fh.write(raw)
            try:
                sm_root = ET.fromstring(raw.decode("utf-8", "ignore"))
            except ET.ParseError:
                continue
            sitemap_ok = True
            for url_loc in sm_root.findall(".//s:loc", ns):
                u = (url_loc.text or "").strip()
                m = re.match(r"https://([^/]+)/(i/)?s/([^/]+)/([^/?]+)$", u)
                if not m:
                    continue
                host, prefix, cat, country = (
                    m.group(1), m.group(2) or "", m.group(3), m.group(4))
                if cat in CATEGORIES and country in COUNTRIES:
                    pairs.add((host, prefix, cat, country))
    if sitemap_ok and pairs:
        print(f"sortlist: {len(pairs)} pairs from sitemaps", flush=True)
        return sorted(pairs)
    print("sortlist: sitemap fetch failed, falling back to hardcoded "
          "category x country list (404 = empty pair)", flush=True)
    fallback = set()
    for cat in CATEGORIES:
        for country, (code, _name) in COUNTRIES.items():
            host = COUNTRY_HOST.get(code, DEFAULT_HOST)
            fallback.add((host, "", cat, country))
    return sorted(fallback)


def parse_listing(html):
    """Extract agency records + pagination from a listing page.

    Returns (agencies, pagination) where agencies are dicts with
    name/website/city/team_size, and pagination is the organic
    section's meta.pagination dict (may be empty).
    """
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S)
    if not m:
        return [], {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return [], {}
    page_data = data.get("props", {}).get("pageProps", {}).get("data", {})
    agencies = []
    pagination = {}
    for section in ("organicAgencies", "paidAgencies"):
        coll = page_data.get(section, {}) or {}
        if section == "organicAgencies":
            pagination = (coll.get("meta", {}) or {}).get("pagination", {}) or {}
        included = {}
        for item in coll.get("included", []) or []:
            if item.get("type") == "agency":
                included[item.get("id")] = item.get("attributes", {}) or {}
        for item in coll.get("data", []) or []:
            attrs = None
            rel = (item.get("relationships", {}) or {}).get("agency", {})
            rel_data = (rel or {}).get("data", {}) or {}
            if rel_data.get("id") in included:
                attrs = included[rel_data["id"]]
            elif isinstance(item.get("attributes"), dict) and \
                    item["attributes"].get("name"):
                # Defensive: some builds inline the agency attributes.
                attrs = item["attributes"]
            if not attrs:
                continue
            city = ""
            for addr in attrs.get("addresses", []) or []:
                a = (addr or {}).get("address", {}) or {}
                city = a.get("en", "") or next(iter(a.values()), "")
                if city:
                    break
            agencies.append({
                "name": attrs.get("name", "") or "",
                "website": attrs.get("website", "") or
                           attrs.get("website_url", "") or "",
                "city": city,
                "team_size": attrs.get("team_size"),
            })
    return agencies, pagination


def walk_pair(host, prefix, category, country_slug, max_pages, stats):
    """Fetch every listing page for one (category, country) pair.

    Page 1 returns 20 items and UI-based pagination; pages 2+
    return 100 items each with API-based pagination (last+1 is the
    real page count). Stops on an empty page, a 404, or the cap.
    Returns the pair's agency records.
    """
    out = []
    page = 1
    total_pages = None
    while page <= max_pages:
        url = (f"https://{host}/{prefix}s/{category}/{country_slug}"
               f"?page={page}")
        status, html = http_get(url)
        if status == 404:
            if page == 1:
                stats["pairs_404"] += 1
            break
        if status != 200 or not html:
            stats["fetch_errors"] += 1
            print(f"sortlist: fetch failed {url} -> {status}", flush=True)
            break
        agencies, meta = parse_listing(html)
        if not agencies:
            break
        out.extend(agencies)
        stats["pages"] += 1
        last = (meta or {}).get("last")
        if page == 1:
            if not last:
                break
            page = 2
            time.sleep(FETCH_DELAY)
            continue
        total_pages = (last or 0) + 1
        if page >= total_pages:
            break
        page += 1
        time.sleep(FETCH_DELAY)
    return out


def build_directory_rows(matched, seen_domains, max_new, now):
    """Dedupe + filter matched rows into Directory rows.

    matched entries carry category (Sortlist slug), country code and
    country name. Returns (rows, stats).
    """
    rows = []
    stats = {"matched": len(matched), "no_domain": 0, "skip_domain": 0,
             "excluded_pk_in": 0, "already_known": 0, "dup_in_batch": 0,
             "new": 0}
    batch_domains = set()
    for m in matched:
        domain = domain_of(m["website"])
        if not domain:
            stats["no_domain"] += 1
            continue
        if any(d in domain for d in SKIP_DOMAINS):
            stats["skip_domain"] += 1
            continue
        if m["country"] in EXCLUDED_COUNTRIES:
            stats["excluded_pk_in"] += 1
            continue
        if domain in seen_domains:
            stats["already_known"] += 1
            continue
        if domain in batch_domains:
            stats["dup_in_batch"] += 1
            continue
        batch_domains.add(domain)
        niche = CATEGORIES.get(m["category_slug"], "web design agency")
        rows.append([
            m["name"], "design agency", "", "",
            m["city"], "", "", "",
            full_url(m["website"]), "", "",
            f"sortlist:{domain}",
            "sortlist import", m["country_name"], niche, now,
        ])
        stats["new"] += 1
        if len(rows) >= max_new:
            break
    return rows, stats


def main():
    global FETCH_DELAY
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=6)
    ap.add_argument("--max-new", type=int, default=5000,
                    help="max rows this shard appends (default 5000)")
    ap.add_argument("--dry-run", action="store_true",
                    help="read the Sheet for dedupe but append nothing")
    ap.add_argument("--work-dir", default="sortlist_work",
                    help="sitemap cache dir (repo-relative; never /tmp)")
    ap.add_argument("--categories", default="",
                    help="test mode: comma-separated Sortlist category "
                    "slugs, e.g. web-design,seo (default: all)")
    ap.add_argument("--countries", default="",
                    help="test mode: comma-separated Sortlist country "
                    "slugs, e.g. belgium-be,germany-de (default: all)")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="test mode: cap pages per pair (0 = walk to "
                    "the pair's real end, max 60)")
    ap.add_argument("--delay", type=float, default=FETCH_DELAY,
                    help="seconds between page fetches (default 0.75)")
    ap.add_argument("--no-log", action="store_true",
                    help="do not write the Log tab line")
    ap.add_argument("--seen-domains-file", default="",
                    help="test mode: one domain per line to use as the "
                    "already-known set instead of reading the Sheet")
    args = ap.parse_args()

    FETCH_DELAY = args.delay
    now = datetime.now(timezone.utc).isoformat()
    os.makedirs(args.work_dir, exist_ok=True)
    max_pages = args.max_pages or MAX_PAGES_PER_PAIR

    pairs = sitemap_pairs(args.work_dir)
    if args.categories:
        wanted = {c.strip() for c in args.categories.split(",") if c.strip()}
        bad = wanted - set(CATEGORIES)
        if bad:
            raise SystemExit(f"unknown categories: {sorted(bad)}")
        pairs = [p for p in pairs if p[2] in wanted]
    if args.countries:
        wanted = {c.strip() for c in args.countries.split(",") if c.strip()}
        bad = wanted - set(COUNTRIES)
        if bad:
            raise SystemExit(f"unknown countries: {sorted(bad)}")
        pairs = [p for p in pairs if p[3] in wanted]
        for c in wanted:
            code = COUNTRIES[c][0]
            if code in EXCLUDED_COUNTRIES:
                raise SystemExit(f"country {c} is excluded (PK/IN rule)")
    if not pairs:
        print("sortlist: no pairs to run", flush=True)
        return 0
    my_pairs = pairs[args.shard::args.shards]
    print(f"sortlist: pairs={len(pairs)} shard={args.shard}/{args.shards} "
          f"my_pairs={len(my_pairs)}", flush=True)

    stats = {"pages": 0, "pairs_404": 0, "fetch_errors": 0}
    matched = []
    for host, prefix, category, country_slug in my_pairs:
        code, country_name = COUNTRIES[country_slug]
        if code in EXCLUDED_COUNTRIES:
            continue
        recs = walk_pair(host, prefix, category, country_slug,
                         max_pages, stats)
        for r in recs:
            r["category_slug"] = category
            r["country"] = code
            r["country_name"] = country_name
        matched.extend(recs)
        print(f"sortlist: {category}/{country_slug} "
              f"pages done, pair total={len(recs)} "
              f"running matched={len(matched)}", flush=True)
        time.sleep(FETCH_DELAY)
    print(f"sortlist: matched={len(matched)} pages={stats['pages']} "
          f"pairs_404={stats['pairs_404']} "
          f"fetch_errors={stats['fetch_errors']}", flush=True)

    seen_domains = set()
    if args.seen_domains_file:
        with open(args.seen_domains_file, encoding="utf-8") as fh:
            for line in fh:
                d = line.strip().lower()
                if d:
                    seen_domains.add(d)
    else:
        for tab_range, col in (("Directory!I:I", 0), ("Queue!B:B", 0)):
            for row in sheets.read_values(tab_range)[1:]:
                if row:
                    d = domain_of(row[col])
                    if d:
                        seen_domains.add(d)
    print(f"sortlist: seen domains in Sheet: {len(seen_domains)}", flush=True)

    rows, build_stats = build_directory_rows(
        matched, seen_domains, args.max_new, now)
    build_stats.update(stats)
    print(f"sortlist: {build_stats}", flush=True)

    appended = 0
    if rows and not args.dry_run:
        for i in range(0, len(rows), APPEND_CHUNK):
            sheets.append_values("Directory!A:P", rows[i:i + APPEND_CHUNK])
        appended = len(rows)
    elif rows:
        print("sortlist: DRY RUN - would append these 5 sample rows "
              "(full row = name, category, ..., website, place_key, "
              "query, locality, niche, scraped_at):", flush=True)
        for r in rows[:5]:
            print(f"  {r}", flush=True)

    summary = (f"shard={args.shard}/{args.shards} "
               f"pairs={len(my_pairs)} pages={stats['pages']} "
               f"matched={build_stats['matched']} "
               f"new={build_stats['new']} "
               f"already_known={build_stats['already_known']} "
               f"excluded_pk_in={build_stats['excluded_pk_in']} "
               f"skip_domain={build_stats['skip_domain']} "
               f"pairs_404={stats['pairs_404']} "
               f"fetch_errors={stats['fetch_errors']} "
               f"appended={appended}")
    print(f"sortlist tick done: {summary}", flush=True)
    if not args.dry_run and not args.no_log:
        try:
            sheets.append_values("Log!A:D", [[now, "sortlist", summary, appended]])
        except Exception:
            pass
    if build_stats["matched"] == 0 and (
            stats["fetch_errors"] > 0 or not pairs):
        print("sortlist: nothing matched and there were fetch errors - "
              "likely blocked; exiting non-zero", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

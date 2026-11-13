#!/usr/bin/env python3
"""Weekly urlscan.io fuel extraction for the outreach bot.

Pulls agency leads from the urlscan.io search API (free, no key) and
appends the new ones to the Sheet Directory tab, where bridge.py
picks them up unchanged.

Why urlscan: its search index holds page titles of recently scanned
sites, and agency homepages usually title themselves with both the
service and the location, e.g. "Tampa Web Design Agency" or
"Digital Marketing Agency UK". That gives us industry + location in
one free query, no scraping, no homepage fetch.

Design (verified 2026-11-12):
- Search API: GET https://urlscan.io/api/v1/search/?q=<query>&size=100
  returns JSON {total, results:[{page:{domain,url,title}, sort, _id}]}.
  Free, NO key. Rate limit for unauthenticated search: 30/minute
  per IP (X-Rate-Limit-Limit: 30, window: minute). This script
  makes at most 1 request per 2 seconds and STOPS cleanly on HTTP
  429 (no retry loop) with a partial summary.
- Pagination: `search_after` takes the sort value of the LAST
  (oldest) result, comma-separated "<13-digit-ts>,<uuid>" (the
  result's `sort` field is [ts, uuid]). Verified live: a 108-total
  query walked 100 + 8 + 0 and terminated on the empty page.
  `has_more` is NOT reliable (it stays False until total exceeds
  10,000), so the walk stops on an empty page or when the
  cumulative unique results reach the reported total.
- Retrieval cap: the API only reports an exact total up to 10,000
  results per query (docs: "The API search will only indicate an
  exact count of results up to 10,000"). None of our phrases is
  near that (largest measured: "digital marketing agency" = 1,487).
  If a phrase ever totals 10,000, the walk still stops at 10,000
  retrieved; the Log line records total_hits=10000 so the cap is
  visible.
- Measured totals 2026-11-12: page.title:"web design agency" = 108
  (78 distinct domains in the first 100 results); "digital
  marketing agency" = 1,487; "branding agency" = 109 (78 distinct
  domains in the first 100). Titles often carry the location too.
- Junk filter: subdomains of framer.website / pages.dev / edgeone.dev
  / vercel.app / netlify.app / webflow.io (preview/staging hosts),
  bare-IP hostnames, and social/directory domains (same skip list
  as bridge.py / cc_extract.py). Dedupe www vs bare domain.
- Location: from the page title when present (US state/city, UK,
  EU country/city, AU/CA/NZ words); if the title carries no
  location, a row is imported ONLY when the domain's TLD is an
  in-scope country TLD (.co.uk, .com.au, .ca, .de, .fr, .nl, .es,
  .it, .se, .pl, .ie, .at, .be, .dk, .fi, .no, .nz) - otherwise
  skipped as unproven (same as the Clutch no-location leak).
  Pakistan/India are explicitly excluded (standing rule).
- Rows use the Directory format bridge.py expects: place_key
  "urlscan:<domain>", category from the matched phrase, locality
  from the detected location, niche from the phrase, query
  "urlscan import". Dedupe is against the live Sheet Directory +
  Queue websites, using the same domain normalisation as
  bridge.py, so a domain already known or already contacted is
  never re-appended.
- Never prints lead data to stdout/logs - counts and the phrase
  list only (public repo logs are public). Dry-run reads the Sheet
  for dedupe but appends nothing and writes no Log line.

Usage (weekly workflow, single job, no shards):
    python scripts/urlscan_extract.py --dry-run
    python scripts/urlscan_extract.py --phrases "web design agency,branding agency" --max-new 10 --no-log

Secrets: SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON (same as the other
scripts). --dry-run reads the Sheet (for dedupe) but writes nothing.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sheets  # noqa: E402

SEARCH_URL = "https://urlscan.io/api/v1/search/"
USER_AGENT = "ovatic-outreach-bot/1.0"
PAGE_SIZE = 100
REQUEST_DELAY = 2.0  # seconds between search requests (limit: 30/min)

DIRECTORY_HEADERS = [
    "name", "category", "rating", "reviews", "address", "postcode",
    "town", "phone", "website", "hours", "maps_url", "place_key",
    "query", "locality", "niche", "scraped_at",
]

# Phrase -> (niche, category). The phrase is the urlscan query
# (page.title:"<phrase>") and also the Directory category/niche.
DEFAULT_PHRASES = [
    ("web design agency", "web design", "web design agency"),
    ("digital marketing agency", "digital marketing",
     "digital marketing agency"),
    ("branding agency", "branding", "branding agency"),
    ("creative agency", "graphic design", "creative agency"),
    ("web development company", "web development",
     "web development company"),
    ("seo agency", "SEO", "seo agency"),
    ("social media marketing agency", "social media",
     "social media marketing agency"),
    ("ecommerce agency", "ecommerce", "ecommerce agency"),
    ("shopify agency", "shopify", "shopify agency"),
    ("wordpress agency", "wordpress", "wordpress agency"),
    ("ui ux design agency", "UI UX", "ui ux design agency"),
    ("app development company", "mobile app",
     "app development company"),
]
PHRASE_MAP = {p: (n, c) for p, n, c in DEFAULT_PHRASES}

# Same skip list as bridge.py / cc_extract.py: a "website" that is
# really a social / directory profile is not a business website.
SKIP_DOMAINS = [
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com",
    "x.com", "youtube.com", "clutch.co", "upwork.com", "fiverr.com",
]

# Preview/staging hosts: a subdomain here is not the agency's own
# site (e.g. overtopmedia-preview.pages.dev, *.vercel.app).
JUNK_SUFFIXES = (
    "framer.website", "pages.dev", "edgeone.dev", "vercel.app",
    "netlify.app", "webflow.io",
)

APPEND_CHUNK = 500  # rows per Sheet append call (60 writes/min limit)
MAX_PAGES_PER_PHRASE = 100  # safety stop: 100 pages x 100 = 10,000 cap

# TLD (registry suffix) -> (country code, country name). Only
# in-scope regions: US has no useful TLD (.com/.net/.org/.io carry
# no location), so US is proven by title text only. Switzerland
# (.ch) is NOT in scope (not EU, per the Clutch classification rule).
TLD_COUNTRIES = {
    "co.uk": ("GB", "United Kingdom"),
    "uk": ("GB", "United Kingdom"),
    "com.au": ("AU", "Australia"),
    "ca": ("CA", "Canada"),
    "nz": ("NZ", "New Zealand"),
    "de": ("DE", "Germany"),
    "fr": ("FR", "France"),
    "nl": ("NL", "Netherlands"),
    "es": ("ES", "Spain"),
    "it": ("IT", "Italy"),
    "se": ("SE", "Sweden"),
    "pl": ("PL", "Poland"),
    "ie": ("IE", "Ireland"),
    "at": ("AT", "Austria"),
    "be": ("BE", "Belgium"),
    "dk": ("DK", "Denmark"),
    "fi": ("FI", "Finland"),
    "pt": ("PT", "Portugal"),
    "gr": ("GR", "Greece"),
    "cz": ("CZ", "Czech Republic"),
    "sk": ("SK", "Slovakia"),
    "si": ("SI", "Slovenia"),
    "hr": ("HR", "Croatia"),
    "hu": ("HU", "Hungary"),
    "ro": ("RO", "Romania"),
    "bg": ("BG", "Bulgaria"),
    "ee": ("EE", "Estonia"),
    "lv": ("LV", "Latvia"),
    "lt": ("LT", "Lithuania"),
    "lu": ("LU", "Luxembourg"),
    "mt": ("MT", "Malta"),
    "cy": ("CY", "Cyprus"),
    "eu": ("EU", "Europe"),
}

# Location word -> (country code, country name, locality label).
# Word-boundary matched against the page title. Titles are short,
# so a location word in a title is a strong signal (unlike homepage
# body text, where "Paris Hilton" or "Washington Post" can misfire).
LOC_WORDS = {}
for _w in ["alabama", "alaska", "arizona", "arkansas", "california",
           "colorado", "connecticut", "delaware", "florida", "georgia",
           "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
           "kentucky", "louisiana", "maine", "maryland",
           "massachusetts", "michigan", "minnesota", "mississippi",
           "missouri", "montana", "nebraska", "nevada", "hampshire",
           "jersey", "mexico", "carolina", "dakota", "ohio", "oklahoma",
           "oregon", "pennsylvania", "island", "tennessee", "texas",
           "utah", "vermont", "virginia", "washington", "wisconsin",
           "wyoming"]:
    LOC_WORDS[_w] = ("US", "United States", _w.title())
for _w, _loc in [("new york", "New York"), ("los angeles", "Los Angeles"),
                 ("chicago", "Chicago"), ("houston", "Houston"),
                 ("phoenix", "Phoenix"), ("seattle", "Seattle"),
                 ("denver", "Denver"), ("boston", "Boston"),
                 ("austin", "Austin"), ("miami", "Miami"),
                 ("atlanta", "Atlanta"), ("dallas", "Dallas"),
                 ("portland", "Portland"), ("san francisco", "San Francisco"),
                 ("san diego", "San Diego"), ("united states", "United States"),
                 ("usa", "United States"), ("tampa", "Tampa"),
                 ("charlotte", "Charlotte"), ("orlando", "Orlando"),
                 ("nashville", "Nashville"), ("sacramento", "Sacramento")]:
    LOC_WORDS[_w] = ("US", "United States", _loc)
for _w, _loc in [("united kingdom", "United Kingdom"), ("england", "England"),
                 ("scotland", "Scotland"), ("wales", "Wales"),
                 ("london", "London"), ("manchester", "Manchester"),
                 ("birmingham", "Birmingham"), ("leeds", "Leeds"),
                 ("bristol", "Bristol"), ("edinburgh", "Edinburgh"),
                 ("glasgow", "Glasgow"), ("liverpool", "Liverpool"),
                 ("sheffield", "Sheffield"), ("newcastle", "Newcastle"),
                 ("nottingham", "Nottingham"), ("leicester", "Leicester"),
                 ("cardiff", "Cardiff"), ("belfast", "Belfast"),
                 ("uk", "United Kingdom")]:
    LOC_WORDS[_w] = ("GB", "United Kingdom", _loc)
for _w, _c in [("germany", ("DE", "Germany")), ("berlin", ("DE", "Germany")),
               ("munich", ("DE", "Germany")), ("hamburg", ("DE", "Germany")),
               ("france", ("FR", "France")), ("paris", ("FR", "France")),
               ("netherlands", ("NL", "Netherlands")),
               ("amsterdam", ("NL", "Netherlands")),
               ("rotterdam", ("NL", "Netherlands")),
               ("spain", ("ES", "Spain")), ("madrid", ("ES", "Spain")),
               ("barcelona", ("ES", "Spain")),
               ("italy", ("IT", "Italy")), ("milan", ("IT", "Italy")),
               ("poland", ("PL", "Poland")), ("warsaw", ("PL", "Poland")),
               ("belgium", ("BE", "Belgium")), ("brussels", ("BE", "Belgium")),
               ("austria", ("AT", "Austria")), ("vienna", ("AT", "Austria")),
               ("sweden", ("SE", "Sweden")), ("stockholm", ("SE", "Sweden")),
               ("denmark", ("DK", "Denmark")),
               ("copenhagen", ("DK", "Denmark")),
               ("finland", ("FI", "Finland")), ("helsinki", ("FI", "Finland")),
               ("norway", ("NO", "Norway")), ("oslo", ("NO", "Norway")),
               ("ireland", ("IE", "Ireland")), ("dublin", ("IE", "Ireland")),
               ("portugal", ("PT", "Portugal")), ("lisbon", ("PT", "Portugal")),
               ("greece", ("GR", "Greece")), ("athens", ("GR", "Greece"))]:
    LOC_WORDS[_w] = (_c[0], _c[1], _w.title())
for _w, _loc in [("australia", "Australia"), ("sydney", "Sydney"),
                 ("melbourne", "Melbourne"), ("brisbane", "Brisbane"),
                 ("perth", "Perth"), ("adelaide", "Adelaide"),
                 ("canberra", "Canberra"), ("victoria", "Victoria"),
                 ("queensland", "Queensland"),
                 ("new south wales", "New South Wales")]:
    LOC_WORDS[_w] = ("AU", "Australia", _loc)
for _w, _loc in [("canada", "Canada"), ("toronto", "Toronto"),
                 ("vancouver", "Vancouver"), ("montreal", "Montreal"),
                 ("calgary", "Calgary"), ("ottawa", "Ottawa"),
                 ("edmonton", "Edmonton"), ("ontario", "Ontario"),
                 ("quebec", "Quebec"), ("british columbia", "British Columbia")]:
    LOC_WORDS[_w] = ("CA", "Canada", _loc)
for _w, _loc in [("new zealand", "New Zealand"), ("auckland", "Auckland"),
                 ("wellington", "Wellington"), ("christchurch", "Christchurch"),
                 ("hamilton", "Hamilton")]:
    LOC_WORDS[_w] = ("NZ", "New Zealand", _loc)
LOC_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in LOC_WORDS) + r")\b", re.I)

# Pakistan / India exclusion (standing rule: never target PK/IN).
# Checked BEFORE the in-scope test: any PK/IN signal excludes.
EXCLUDED_TLDS = ("pk", "in", "com.pk", "co.in")
EXCLUDED_WORDS = re.compile(
    r"\b(pakistan|islamabad|karachi|lahore|rawalpindi|faisalabad|"
    r"multan|peshawar|india|delhi|mumbai|bangalore|bengaluru|chennai|"
    r"hyderabad|kolkata|pune|ahmedabad|jaipur|lucknow)\b", re.I)

IN_SCOPE_COUNTRIES = {
    "US", "GB",
    "DE", "FR", "NL", "ES", "IT", "SE", "PL", "IE", "AT", "BE", "DK",
    "FI", "PT", "GR", "CZ", "SK", "SI", "HR", "HU", "RO", "BG", "EE",
    "LV", "LT", "LU", "MT", "CY", "NO", "EU",
    "AU", "CA", "NZ",
}

IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$|^[0-9a-f:]+$", re.I)


def domain_of(host):
    """Registered domain, lowercased, no www. Same normalisation as
    bridge.py / cc_extract.py: a bare domain gets a scheme first so
    urlparse reads it as a host, not a path."""
    h = (host or "").strip()
    if h and "://" not in h:
        h = "https://" + h
    try:
        return urllib.parse.urlparse(h).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def tld_country(host):
    """(code, country name) from the domain's registry suffix, or
    None when the suffix carries no location (.com/.net/.org/.io)."""
    h = host.lower()
    for suffix in sorted(TLD_COUNTRIES, key=len, reverse=True):
        if h == suffix or h.endswith("." + suffix):
            return TLD_COUNTRIES[suffix]
    return None


def is_junk(domain):
    """Preview/staging host, bare IP, or social/directory domain."""
    if not domain or "." not in domain:
        return True
    if IP_RE.match(domain):
        return True
    if any(domain == d or domain.endswith("." + d) for d in SKIP_DOMAINS):
        return True
    if any(domain == s or domain.endswith("." + s) for s in JUNK_SUFFIXES):
        return True
    return False


def search_page(query, search_after=None):
    """One urlscan search request. Returns (status, data_or_None).

    status is "ok", "rate_limited" (HTTP 429 - caller must stop),
    or "error" (other HTTP/network failure - caller logs and moves
    on). Sleeps REQUEST_DELAY before each request so the 30/min
    limit is never approached.
    """
    time.sleep(REQUEST_DELAY)
    params = {"q": query, "size": PAGE_SIZE}
    if search_after:
        params["search_after"] = search_after
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return "ok", json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return "rate_limited", None
        print(f"urlscan: search HTTP {e.code} for {query!r}", flush=True)
        return "error", None
    except Exception as e:
        print(f"urlscan: search failed for {query!r}: {e}", flush=True)
        return "error", None


def walk_phrase(phrase):
    """Walk all pages of one title phrase. Returns (results, stats,
    rate_limited). results is a list of (domain, title) in the order
    returned. stats counts requests/pages/hits. Stops on an empty
    page, on reaching the reported total, or on the 10,000-result
    retrieval cap (100 pages)."""
    query = f'page.title:"{phrase}"'
    results = []
    seen_ids = set()
    stats = {"requests": 0, "total_hits": 0, "pages": 0}
    search_after = None
    rate_limited = False
    while stats["pages"] < MAX_PAGES_PER_PHRASE:
        status, data = search_page(query, search_after)
        stats["requests"] += 1
        if status == "rate_limited":
            print("urlscan: HTTP 429 - stopping cleanly (partial run)",
                  flush=True)
            rate_limited = True
            break
        if status != "ok" or not data:
            break
        page_results = data.get("results", [])
        stats["pages"] += 1
        if stats["pages"] == 1:
            stats["total_hits"] = data.get("total", 0) or 0
        if not page_results:
            break
        new = 0
        for r in page_results:
            rid = r.get("_id")
            if rid and rid in seen_ids:
                continue
            if rid:
                seen_ids.add(rid)
            page = r.get("page") or {}
            domain = page.get("domain") or ""
            title = page.get("title") or ""
            if domain:
                results.append((domain, title))
                new += 1
        if new == 0:
            break
        if len(seen_ids) >= stats["total_hits"] and stats["total_hits"]:
            break
        last_sort = page_results[-1].get("sort")
        if not last_sort or len(last_sort) != 2:
            break
        search_after = f"{last_sort[0]},{last_sort[1]}"
    return results, stats, rate_limited


# Phrase-level agency match used by name_from_title: a title
# segment that IS a service phrase is not a brand name.
AGENCY_TITLE_RE = re.compile(
    r"web design|website design|digital marketing|marketing agency|"
    r"design agency|branding agency|brand design|web development|"
    r"software development|app development|mobile app|ui.?ux|"
    r"user experience|ecommerce|e-commerce|shopify|wordpress|"
    r"seo agency|search engine optimization|social media|"
    r"graphic design|creative agency|product design|"
    r"video production|content marketing|advertising agency", re.I)


def name_from_title(title, domain):
    """Company name from the page title, falling back to the domain.
    First segment before | - : is usually the brand; if that segment
    is a service phrase, the last segment is the brand (unless it is
    a service phrase too)."""
    if title:
        segs = [s.strip() for s in re.split(r"\s*[|\-–—:]\s*", title)
                if s.strip()]
        pick = segs[0] if segs else ""
        if (pick and len(segs) > 1 and AGENCY_TITLE_RE.search(pick)
                and not AGENCY_TITLE_RE.search(segs[-1])):
            pick = segs[-1]
        if pick and pick.lower() not in ("home", "welcome", "homepage"):
            return pick[:100]
    base = domain.split(".")[0].replace("-", " ").replace("_", " ")
    return base.title()[:100] or domain


def classify_result(domain, title):
    """Classify one (domain, title) pair. Returns (status, locality,
    country_code, name). Status is one of in_scope, no_location,
    excluded_pk_in, junk. Location comes from the title first; a
    title with no location still qualifies on an in-scope country
    TLD alone. Pakistan/India signals exclude before anything else.
    """
    domain = domain_of(domain)
    if is_junk(domain):
        return ("junk", "", "", "")
    if any(domain.endswith("." + t) or domain == t
           for t in EXCLUDED_TLDS):
        return ("excluded_pk_in", "", "", "")
    if EXCLUDED_WORDS.search(title or ""):
        return ("excluded_pk_in", "", "", "")
    country_code = country_name = locality = ""
    m = LOC_RE.search(title or "")
    if m:
        # .get, not []: IGNORECASE matching can match a word (e.g. with
        # long-s) whose .lower() is not a LOC_WORDS key. A miss falls
        # through to the TLD check below instead of crashing the run.
        hit = LOC_WORDS.get(m.group(1).lower())
        if hit:
            code, cname, loc = hit
            country_code, country_name = code, cname
            locality = f"{loc}, {cname}" if loc != cname else cname
    if not country_code:
        tld = tld_country(domain)
        if tld:
            country_code, country_name = tld
            locality = country_name
    if not country_code:
        return ("no_location", "", "", "")
    if country_code not in IN_SCOPE_COUNTRIES:
        return ("no_location", "", "", "")
    name = name_from_title(title, domain)
    return ("in_scope", locality, country_code, name)


def build_directory_rows(candidates, seen_domains, max_new, now):
    """Dedupe + filter classified in-scope candidates into Directory
    rows. candidates are (domain, title, niche, category, locality,
    code, name). Returns (rows, stats). All candidates are counted
    (no early break), but only the first max_new become rows; the
    rest are counted as capped, so the stats stay honest when the
    cap truncates. First phrase wins when a domain matches several
    phrases."""
    rows = []
    stats = {"in_scope": len(candidates), "no_domain": 0,
             "skip_domain": 0, "already_known": 0, "dup_in_batch": 0,
             "capped": 0, "new": 0}
    batch_domains = set()
    for domain, _title, niche, category, locality, _code, name in candidates:
        domain = domain_of(domain)
        if not domain:
            stats["no_domain"] += 1
            continue
        if any(d in domain for d in SKIP_DOMAINS):
            stats["skip_domain"] += 1
            continue
        if domain in seen_domains:
            stats["already_known"] += 1
            continue
        if domain in batch_domains:
            stats["dup_in_batch"] += 1
            continue
        batch_domains.add(domain)
        if len(rows) >= max_new:
            stats["capped"] += 1
            continue
        rows.append([
            name, category, "", "",
            "", "", "", "",
            f"https://{domain}", "", "",
            f"urlscan:{domain}",
            "urlscan import", locality, niche or "web design", now,
        ])
        stats["new"] += 1
    return rows, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phrases", default="",
                    help="comma-separated title phrases (empty = "
                    "default 12-phrase list)")
    ap.add_argument("--max-new", type=int, default=5000,
                    help="max rows appended in total (default 5000)")
    ap.add_argument("--dry-run", action="store_true",
                    help="read the Sheet for dedupe but append nothing")
    ap.add_argument("--no-log", action="store_true",
                    help="do not write the Log tab line")
    ap.add_argument("--seen-domains-file", default="",
                    help="test mode: one domain per line to use as the "
                    "already-known set instead of reading the Sheet")
    args = ap.parse_args()

    if args.phrases.strip():
        phrases = [p.strip() for p in args.phrases.split(",") if p.strip()]
    else:
        phrases = [p for p, _, _ in DEFAULT_PHRASES]
    unknown = [p for p in phrases if p not in PHRASE_MAP]
    if unknown:
        print(f"urlscan: unknown phrase(s) (no niche mapping): {unknown}",
              flush=True)
        print("urlscan: known phrases: "
              + ", ".join(p for p, _, _ in DEFAULT_PHRASES), flush=True)
        return 2

    now = datetime.now(timezone.utc).isoformat()
    print(f"urlscan: phrases={len(phrases)} max_new={args.max_new} "
          f"dry_run={args.dry_run}", flush=True)

    seen_domains = set()
    if args.seen_domains_file:
        with open(args.seen_domains_file, encoding="utf-8") as fh:
            for line in fh:
                d = domain_of(line.strip())
                if d:
                    seen_domains.add(d)
    else:
        for tab_range in ("Directory!I:I", "Queue!B:B"):
            for row in sheets.read_values(tab_range)[1:]:
                if row:
                    d = domain_of(row[0])
                    if d:
                        seen_domains.add(d)
    print(f"urlscan: seen domains in Sheet: {len(seen_domains)}",
          flush=True)

    per_phrase = []
    candidates = []
    phrase_domains = {}
    rate_limited = False
    for phrase in phrases:
        niche, category = PHRASE_MAP[phrase]
        results, stats, hit_limit = walk_phrase(phrase)
        if hit_limit:
            rate_limited = True
        pstats = {"phrase": phrase, "searched": stats["requests"],
                  "total_hits": stats["total_hits"],
                  "distinct_domains": 0, "new_appended": 0,
                  "dupes": 0, "skipped_no_loc": 0,
                  "skipped_junk": 0, "skipped_pk_in": 0}
        seen_this_phrase = set()
        phrase_domains[phrase] = []
        for domain, title in results:
            domain = domain_of(domain)
            if not domain or domain in seen_this_phrase:
                continue
            seen_this_phrase.add(domain)
            status, locality, code, name = classify_result(domain, title)
            if status == "junk":
                pstats["skipped_junk"] += 1
            elif status == "excluded_pk_in":
                pstats["skipped_pk_in"] += 1
            elif status == "no_location":
                pstats["skipped_no_loc"] += 1
            else:
                pstats["distinct_domains"] += 1
                phrase_domains[phrase].append(domain)
                candidates.append((domain, title, niche, category,
                                   locality, code, name))
        per_phrase.append(pstats)
        print(f"urlscan: phrase={phrase!r} searched={pstats['searched']} "
              f"total_hits={pstats['total_hits']} "
              f"distinct={pstats['distinct_domains']} "
              f"no_loc={pstats['skipped_no_loc']} "
              f"junk={pstats['skipped_junk']} "
              f"pk_in={pstats['skipped_pk_in']}", flush=True)
        if rate_limited:
            print("urlscan: stopped after 429 - remaining phrases "
                  "skipped (partial run)", flush=True)
            break

    rows, build_stats = build_directory_rows(
        candidates, seen_domains, args.max_new, now)
    # Per-phrase dupes, computed honestly: a distinct in-scope domain
    # is a dupe only when it is already in the Sheet or it already
    # appeared under an earlier phrase. Domains merely cut off by
    # --max-new are "capped", not dupes (build_stats counts them).
    appended_domains = {domain_of(r[8]) for r in rows}
    earlier = set()
    for p in per_phrase:
        dupes = 0
        for d in phrase_domains[p["phrase"]]:
            if d in seen_domains or d in earlier:
                dupes += 1
            earlier.add(d)
        p["dupes"] = dupes
        p["new_appended"] = sum(
            1 for d in phrase_domains[p["phrase"]]
            if d in appended_domains)

    appended = 0
    if rows and not args.dry_run:
        for i in range(0, len(rows), APPEND_CHUNK):
            sheets.append_values("Directory!A:P", rows[i:i + APPEND_CHUNK])
        appended = len(rows)
    elif rows:
        print("urlscan: DRY RUN - would append "
              f"{len(rows)} rows, counts only (no lead data in logs)",
              flush=True)

    summary_parts = [
        f"phrases={len(per_phrase)}",
        f"rate_limited={int(rate_limited)}",
        f"new={build_stats['new']}",
        f"already_known={build_stats['already_known']}",
        f"dup_in_batch={build_stats['dup_in_batch']}",
        f"capped={build_stats['capped']}",
        f"appended={appended}",
    ]
    for p in per_phrase:
        summary_parts.append(
            f"[{p['phrase']}: searched={p['searched']} "
            f"total_hits={p['total_hits']} "
            f"distinct={p['distinct_domains']} "
            f"new={p['new_appended']} dupes={p['dupes']} "
            f"no_loc={p['skipped_no_loc']} junk={p['skipped_junk']}]")
    summary = " ".join(summary_parts)
    print(f"urlscan tick done: {summary}", flush=True)
    if not args.dry_run and not args.no_log:
        try:
            sheets.append_values(
                "Log!A:D", [[now, "urlscan-extract", summary, appended]])
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Monthly Common Crawl fuel extraction for the outreach bot.

Pulls agency leads from the Common Crawl URL Index (free, open,
monthly crawls) and appends the new ones to the Sheet Directory tab,
where bridge.py picks them up unchanged.

Why Common Crawl: every monthly crawl indexes billions of URLs.
The URL Index (columnar Parquet, ~300 files per crawl) has no
industry/location metadata, so discovery is split in two:
  1. URL Index filter - registered domains that contain an agency
     keyword (design/agency/studio/digital/media/creative/marketing/
     web) AND have a status-200 contact page (/contact, /contact-us,
     /kontakt). Measured 2026-11-12 on crawl CC-MAIN-2026-17:
     484 such hosts in one file, ~75k-145k per crawl.
  2. Homepage classification - curl the homepage, strip tags, and
     require BOTH an agency signal and an in-scope location signal.
     Eyeball sample of 30 keyword hosts: 4 genuine in-scope agencies
     (13%) + 5 agency-no-location (17%). Extrapolated: ~8k-20k new
     in-scope agency domains per crawl -> bridge ~40% -> send ~30%
     -> ~1k-2.5k delivered per monthly crawl. Bridge is the final
     gate (it checks the real contact form), so classifier precision
     of ~10-15% is acceptable; recall matters more.
  Clutch still holds ~240k unsampled profiles (~48k agencies), so
  Common Crawl is diversification, not the emergency fuel source.

Design (verified 2026-11-12):
- Crawl discovery: the newest crawl whose URL Index Parquet is
  published on Hugging Face is found at runtime via the HF buckets
  API (list crawl= dirs under cc-index/table/cc-main/warc, newest
  first, first one with files under subset=warc). Verified newest:
  CC-MAIN-2026-43 (300 files, zstd Parquet). File names differ per
  crawl (different UUIDs, .gz vs .zstd), so they are NEVER hardcoded
  - they are listed from the API at runtime. Fallback if discovery
  fails: CC-MAIN-2026-43, then CC-MAIN-2026-17 (research crawl).
- Index schema is identical across crawls (33 columns incl.
  url_host_registered_domain, url_path, fetch_status,
  url_host_registry_suffix). One file is ~7M rows; a filtered
  DuckDB query takes ~15-35s per file. Files are sharded by index
  (file i goes to shard i % shards), 50 files per shard at 6 shards.
- DuckDB + httpfs reads the HF resolve URLs directly. On the GitHub
  runner there is no egress proxy, so httpfs works with default
  settings. In the dev cell the credentialed egress proxy breaks
  httpfs URL parsing, so proxy env vars are popped at import time
  (no-op on the runner).
- Homepage fetch uses plain curl (no browser UA - a browser UA gets
  403 from some readers; plain curl is the proven route), 10s
  timeout, https then http, one fetch per host. Fetches run in a
  thread pool (default 10 workers) with a 3s delay per worker
  between fetches - per-host politeness is satisfied by design
  (each host is fetched once). ~12k hosts per shard take ~60-90 min.
- Agency signal: title/H1 contains a web/design/marketing/agency
  phrase, OR >=2 DISTINCT agency phrases in body text. A single
  footer "web design by" credit does NOT count (credit lines are
  stripped before counting, and one phrase alone never qualifies).
- Location signal: in-scope US/UK/EU/AU/CA/NZ, from the TLD
  (.co.uk/.uk, .com.au, .ca, EU-27 TLDs, .nz) or from location
  words in the homepage text (US states/cities, UK cities,
  EU countries/cities, AU/CA/NZ places). Hosts with no location
  signal are NOT imported (unproven, same as the Clutch leak).
  Pakistan/India are explicitly excluded (standing rule): .pk/.in
  TLDs and PK/IN place words exclude a host even if it also
  mentions an in-scope place.
- Rows use the Directory format bridge.py expects: place_key
  "cc:<domain>", category from the matched agency terms, locality
  from the detected location, niche from the matched niche,
  query "commoncrawl import". Dedupe is against the live Sheet
  Directory + Queue websites, using the same domain normalisation
  as bridge.py, so a domain already known or already contacted is
  never re-appended.
- Never prints lead data to stdout/logs - counts only (public repo
  logs are public). Dry-run reads the Sheet for dedupe but appends
  nothing and writes no Log line.

Usage (monthly workflow, 6 shards):
    python scripts/cc_extract.py --shard 0 --shards 6
    python scripts/cc_extract.py --crawl CC-MAIN-2026-43 --dry-run --max-files 2
    python scripts/cc_extract.py --dry-run --max-files 1 --max-new 10 --no-log

Secrets: SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON (same as the other
scripts). --dry-run reads the Sheet (for dedupe) but writes nothing.
"""
import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

# Pop proxy vars BEFORE importing duckdb: in the dev cell the egress
# proxy URL (with credentials) breaks httpfs URL parsing; on the
# GitHub runner there is no proxy at all, so this is a no-op there.
# Never SET proxy vars - httpfs works with default settings.
for _v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_v, None)

import duckdb  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sheets  # noqa: E402

HF_API_BASE = "https://huggingface.co/api/buckets/commoncrawl/commoncrawl/tree"
HF_RESOLVE_BASE = "https://huggingface.co/buckets/commoncrawl/commoncrawl/resolve"
INDEX_PREFIX = "cc-index/table/cc-main/warc"
# Newest crawl whose URL Index was verified 2026-11-12: 300 files,
# zstd Parquet, schema identical to 2026-17, part-00150 gave 335
# keyword hosts with a contact page (vs 484 in 2026-17).
FALLBACK_CRAWL = "CC-MAIN-2026-43"
RESEARCH_CRAWL = "CC-MAIN-2026-17"  # the crawl the research was done on

DIRECTORY_HEADERS = [
    "name", "category", "rating", "reviews", "address", "postcode",
    "town", "phone", "website", "hours", "maps_url", "place_key",
    "query", "locality", "niche", "scraped_at",
]

CONTACT_PATHS = (
    "/contact", "/contact/", "/contact-us", "/contact-us/",
    "/kontakt", "/kontakt/",
)
HOST_KEYWORDS = (
    "design", "agency", "studio", "digital", "media", "creative",
    "marketing", "web",
)

# Same skip list as bridge.py / sortlist_extract.py: a "website" that
# is really a social / directory profile is not a business website.
SKIP_DOMAINS = [
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com",
    "x.com", "youtube.com", "clutch.co", "upwork.com", "fiverr.com",
]

APPEND_CHUNK = 500  # rows per Sheet append call (60 writes/min limit)
FETCH_DELAY = 3.0   # seconds between fetches, per worker
FETCH_TIMEOUT = 10  # seconds per homepage fetch
WORKERS = 10        # concurrent homepage fetches
MAX_RETRIES = 2     # extra attempts on HF API 429/5xx

# Agency phrase -> (niche, category). Title/H1 match on any phrase
# qualifies alone; body text needs >=2 DISTINCT phrases.
AGENCY_PHRASES = [
    ("web design", "web design", "web design agency"),
    ("website design", "web design", "web design agency"),
    ("design agency", "web design", "web design agency"),
    ("digital marketing", "digital marketing", "digital marketing agency"),
    ("marketing agency", "digital marketing", "digital marketing agency"),
    ("branding agency", "branding", "branding agency"),
    ("brand design", "branding", "branding agency"),
    ("web development", "web development", "web development company"),
    ("website development", "web development", "web development company"),
    ("software development", "software development", "software development agency"),
    ("app development", "mobile app", "mobile app agency"),
    ("mobile app", "mobile app", "mobile app agency"),
    ("ui/ux", "UI UX", "UI UX agency"),
    ("ux/ui", "UI UX", "UI UX agency"),
    ("user experience", "UI UX", "UI UX agency"),
    ("ecommerce", "ecommerce", "ecommerce agency"),
    ("e-commerce", "ecommerce", "ecommerce agency"),
    ("shopify", "shopify", "shopify agency"),
    ("wordpress", "wordpress", "wordpress agency"),
    ("seo agency", "SEO", "SEO agency"),
    ("search engine optimization", "SEO", "SEO agency"),
    ("social media marketing", "social media", "social media agency"),
    ("social media agency", "social media", "social media agency"),
    ("graphic design", "graphic design", "graphic design agency"),
    ("creative agency", "graphic design", "graphic design agency"),
    ("product design", "product design", "product design agency"),
    ("video production", "video production", "video production agency"),
    ("content marketing", "content marketing", "content marketing agency"),
    ("advertising agency", "advertising", "advertising agency"),
]
AGENCY_RE = re.compile(
    "|".join(re.escape(p) for p, _, _ in AGENCY_PHRASES), re.I)

# Footer credit lines ("Web design by X", "Developed by X") are
# stripped before counting body phrases: a single credit is not an
# agency signal.
CREDIT_RE = re.compile(
    r"(website|web|site)\s+(design(ed)?|developed|built|created)\s+by\b"
    r"|\bdesigned\s+by\b|\bdeveloped\s+by\b|\bbuilt\s+by\b",
    re.I)

# TLD (registry suffix) -> (country code, country name). Only
# in-scope regions: US has no useful TLD (.com/.net/.org/.io carry
# no location), so US is proven by text only. Switzerland (.ch) is
# NOT in scope (not EU, per the Clutch classification rule).
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
# Word-boundary matched against homepage text. Loose single words
# (paris, georgia, victoria, washington) can false-positive, but the
# agency signal is also required and bridge.py is the final gate.
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
                 ("usa", "United States")]:
    LOC_WORDS[_w] = ("US", "United States", _loc)
for _w, _loc in [("united kingdom", "United Kingdom"), ("england", "England"),
                 ("scotland", "Scotland"), ("wales", "Wales"),
                 ("london", "London"), ("manchester", "Manchester"),
                 ("birmingham", "Birmingham"), ("leeds", "Leeds"),
                 ("bristol", "Bristol"), ("edinburgh", "Edinburgh"),
                 ("glasgow", "Glasgow"), ("liverpool", "Liverpool"),
                 ("sheffield", "Sheffield"), ("newcastle", "Newcastle"),
                 ("nottingham", "Nottingham"), ("leicester", "Leicester"),
                 ("cardiff", "Cardiff"), ("belfast", "Belfast")]:
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


def domain_of(url):
    """Host, lowercased, no www. Same as bridge.py, except a bare
    domain (no scheme) gets a scheme first so urlparse reads it as
    a host, not a path."""
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    try:
        return urllib.parse.urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def hf_api_get(path):
    """GET one HF buckets API listing via curl. Returns parsed JSON
    (a list) or None on failure. Retries 429/5xx.

    curl (not urllib) is used on purpose: curl honours the proxy
    env vars when present (the dev cell needs its egress proxy for
    the HF API) and goes direct when absent (the GitHub runner has
    no proxy). Retries 429/5xx."""
    url = f"{HF_API_BASE}/{path}"
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", "60", "-w", "\n%{http_code}", url],
                capture_output=True, timeout=90)
            out = r.stdout.decode("utf-8", "ignore")
            body, _, code = out.rpartition("\n")
            status = int(code.strip()) if code.strip().isdigit() else 0
            if status == 200:
                return json.loads(body)
            last_err = f"HTTP {status}"
            if status in (429, 500, 502, 503) and attempt < MAX_RETRIES:
                time.sleep(5 * (attempt + 1))
                continue
            print(f"cc: HF API {path} -> {last_err}", flush=True)
            return None
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(5 * (attempt + 1))
                continue
            print(f"cc: HF API {path} failed: {last_err}", flush=True)
            return None
    print(f"cc: HF API {path} failed: {last_err}", flush=True)
    return None


def discover_latest_crawl():
    """Newest crawl whose URL Index Parquet is published on HF.

    Lists crawl= dirs newest-first; the first one with at least one
    file under subset=warc wins. Falls back to the newest verified
    crawl, then the research crawl, if discovery fails entirely.
    """
    listing = hf_api_get(f"{INDEX_PREFIX}?recursive=false&limit=200")
    if isinstance(listing, list):
        crawls = sorted(
            (it["path"].split("crawl=", 1)[1]
             for it in listing
             if it.get("type") == "directory"
             and it.get("path", "").startswith(INDEX_PREFIX + "/crawl=")),
            reverse=True)
        for crawl in crawls:
            files = list_index_files(crawl)
            if files:
                return crawl
        print("cc: no crawl dir had index files, using fallback",
              flush=True)
    return FALLBACK_CRAWL


def list_index_files(crawl):
    """Sorted HF resolve URLs of the crawl's URL-index Parquet files
    (subset=warc). File names differ per crawl (UUID + .gz/.zstd),
    so they are always listed, never constructed."""
    listing = hf_api_get(
        f"{INDEX_PREFIX}/crawl={crawl}/subset=warc"
        "?recursive=false&limit=1000")
    if not isinstance(listing, list):
        return []
    paths = sorted(
        it["path"] for it in listing
        if it.get("type") == "file"
        and it.get("path", "").endswith(".parquet"))
    return [f"{HF_RESOLVE_BASE}/{p}" for p in paths]


def scan_file(con, file_url, part_no):
    """Distinct keyword hosts with a status-200 contact page in one
    index file. Returns (hosts, error_or_None). Counts only."""
    contact = "(" + ",".join(f"'{p}'" for p in CONTACT_PATHS) + ")"
    kw = " OR ".join(
        f"url_host_registered_domain ILIKE '%{k}%'" for k in HOST_KEYWORDS)
    # URL is from the HF API listing (trusted); quote-escape it.
    safe_url = file_url.replace("'", "''")
    q = (
        "SELECT url_host_registered_domain "
        f"FROM read_parquet('{safe_url}') "
        "WHERE fetch_status = 200 "
        f"AND url_path IN {contact} "
        f"AND ({kw}) "
        "GROUP BY 1"
    )
    try:
        rows = con.execute(q).fetchall()
        return [r[0] for r in rows if r[0]], None
    except Exception as e:
        print(f"cc: file part-{part_no:05d} scan failed: {e}",
              flush=True)
        return [], str(e)


def fetch_homepage(host):
    """Plain curl, no browser UA, 10s timeout, https then http.
    Returns raw HTML bytes or b'' when unreachable."""
    for scheme in ("https://", "http://"):
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", str(FETCH_TIMEOUT),
                 "-L", "--max-redirs", "3", scheme + host],
                capture_output=True, timeout=FETCH_TIMEOUT + 10)
            if r.returncode == 0 and r.stdout and len(r.stdout) > 500:
                return r.stdout
        except Exception:
            continue
    return b""


def visible_text(html):
    """Visible text: script/style removed, tags stripped, whitespace
    collapsed. Capped so a huge homepage cannot blow up memory."""
    t = re.sub(rb"<(script|style)[^>]*>.*?</\1>", b" ", html,
               flags=re.S | re.I)
    t = re.sub(rb"<[^>]+>", b" ", t)
    t = re.sub(rb"\s+", b" ", t)
    return t.decode("utf-8", "replace")[:20000]


def title_and_h1(html):
    """<title> + all <h1> texts, joined. The strongest agency and
    location signals live here."""
    parts = []
    m = re.search(rb"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        parts.append(re.sub(rb"<[^>]+>", b"", m.group(1)).decode(
            "utf-8", "replace"))
    for h in re.findall(rb"<h1[^>]*>(.*?)</h1>", html, re.S | re.I):
        parts.append(re.sub(rb"<[^>]+>", b"", h).decode(
            "utf-8", "replace"))
    return " ".join(p.strip() for p in parts if p.strip())


def tld_country(host):
    """(code, country name) from the registered domain's suffix, or
    None when the suffix carries no location (.com/.net/.org/.io)."""
    h = host.lower()
    for suffix in sorted(TLD_COUNTRIES, key=len, reverse=True):
        if h == suffix or h.endswith("." + suffix):
            return TLD_COUNTRIES[suffix]
    return None


def classify_host(host):
    """Classify one host. Returns (status, niche, category,
    locality, country_code, name). Status is one of in_scope,
    no_location, not_agency, unreachable, excluded_pk_in.

    Agency signal: a phrase in title/H1, or >=2 DISTINCT phrases
    in body text (footer credits stripped first). Location signal:
    in-scope TLD, or an in-scope location word in the text.
    Pakistan/India signals exclude before anything else.
    """
    if any(host.lower().endswith("." + t) or host.lower() == t
           for t in EXCLUDED_TLDS):
        return ("excluded_pk_in", "", "", "", "", "")
    html = fetch_homepage(host)
    if not html:
        return ("unreachable", "", "", "", "", "")
    text = visible_text(html)
    if EXCLUDED_WORDS.search(text):
        return ("excluded_pk_in", "", "", "", "", "")
    heading = title_and_h1(html)

    # Agency signal.
    niche = category = ""
    heading_hits = [
        (p, n, c) for p, n, c in AGENCY_PHRASES
        if re.search(re.escape(p), heading, re.I)]
    if heading_hits:
        niche, category = heading_hits[0][1], heading_hits[0][2]
    else:
        body = CREDIT_RE.sub(" ", text)
        body_hits = [
            (p, n, c) for p, n, c in AGENCY_PHRASES
            if re.search(re.escape(p), body, re.I)]
        distinct = {p for p, _, _ in body_hits}
        if len(distinct) < 2:
            return ("not_agency", "", "", "", "", "")
        niche, category = body_hits[0][1], body_hits[0][2]

    # Location signal: TLD first, then text words.
    country_code = country_name = locality = ""
    tld = tld_country(host)
    if tld:
        country_code, country_name = tld
        locality = country_name
    m = LOC_RE.search(text)
    if m:
        code, cname, loc = LOC_WORDS[m.group(1).lower()]
        # A text location beats a bare .eu TLD; otherwise the TLD
        # (usually a country suffix) is the more precise signal.
        if not tld or tld[0] == "EU":
            country_code, country_name, locality = code, cname, loc
            if loc != cname:
                locality = f"{loc}, {cname}"
        elif not locality:
            locality = country_name
    if not country_code:
        return ("no_location", "", "", "", "", "")
    if country_code not in IN_SCOPE_COUNTRIES:
        return ("no_location", "", "", "", "", "")
    if not locality:
        locality = country_name
    name = name_from_heading(heading, host)
    return ("in_scope", niche, category, locality, country_code, name)


def name_from_heading(heading, host):
    """Company name from <title>/H1, falling back to the domain.
    First segment before | - :: is usually the brand; if that
    segment is a service phrase, the last segment is the brand.
    Generic titles fall back to the humanised domain."""
    if heading:
        segs = [s.strip() for s in re.split(r"\s*[|\-–—:]\s*", heading)
                if s.strip()]
        pick = segs[0] if segs else ""
        if pick and AGENCY_RE.search(pick) and len(segs) > 1:
            pick = segs[-1]
        if pick and pick.lower() not in (
                "home", "welcome", "homepage", "index"):
            return pick[:100]
    base = host.split(".")[0].replace("-", " ").replace("_", " ")
    return base.title()[:100] or host


def build_directory_rows(classified, seen_domains, max_new, now):
    """Dedupe + filter classified in-scope hosts into Directory rows.
    classified entries are (host, niche, category, locality, code,
    name). Returns (rows, stats)."""
    rows = []
    stats = {"in_scope": len(classified), "no_domain": 0,
             "skip_domain": 0, "already_known": 0, "dup_in_batch": 0,
             "new": 0}
    batch_domains = set()
    for host, niche, category, locality, code, name in classified:
        domain = domain_of(host)
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
        rows.append([
            name, category, "", "",
            "", "", "", "",
            f"https://{domain}", "", "",
            f"cc:{domain}",
            "commoncrawl import", locality, niche or "web design", now,
        ])
        stats["new"] += 1
        if len(rows) >= max_new:
            break
    return rows, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=6)
    ap.add_argument("--crawl", default="",
                    help="crawl id, e.g. CC-MAIN-2026-43 "
                    "(empty = newest with a published URL Index)")
    ap.add_argument("--max-new", type=int, default=5000,
                    help="max rows this shard appends (default 5000)")
    ap.add_argument("--max-files", type=int, default=0,
                    help="test mode: cap index files scanned by this "
                    "shard (0 = all files for the shard)")
    ap.add_argument("--dry-run", action="store_true",
                    help="read the Sheet for dedupe but append nothing")
    ap.add_argument("--delay", type=float, default=FETCH_DELAY,
                    help="seconds between homepage fetches, per worker "
                    "(default 3.0)")
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help="concurrent homepage fetches (default 10)")
    ap.add_argument("--no-log", action="store_true",
                    help="do not write the Log tab line")
    ap.add_argument("--seen-domains-file", default="",
                    help="test mode: one domain per line to use as the "
                    "already-known set instead of reading the Sheet")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).isoformat()
    crawl = args.crawl.strip() or discover_latest_crawl()
    print(f"cc: crawl={crawl} shard={args.shard}/{args.shards}",
          flush=True)

    files = list_index_files(crawl)
    if not files:
        # Discovery may have returned a crawl whose index is not
        # published yet; fall back to the newest verified crawl.
        for fallback in (FALLBACK_CRAWL, RESEARCH_CRAWL):
            if fallback != crawl:
                print(f"cc: no index files for {crawl}, "
                      f"trying {fallback}", flush=True)
                crawl = fallback
                files = list_index_files(crawl)
                if files:
                    break
    if not files:
        print("cc: no URL Index files found for any crawl - "
              "likely blocked; exiting non-zero", flush=True)
        return 1
    my_files = files[args.shard::args.shards]
    if args.max_files:
        my_files = my_files[:args.max_files]
    print(f"cc: crawl={crawl} files={len(files)} "
          f"my_files={len(my_files)}", flush=True)

    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs; LOAD httpfs;")
    except Exception as e:
        print(f"cc: httpfs load failed: {e}", flush=True)
        return 1

    stats = {"files_scanned": 0, "file_errors": 0, "keyword_hosts": 0}
    hosts = set()
    for i, file_url in enumerate(my_files):
        part_no = args.shard + i * args.shards
        found, err = scan_file(con, file_url, part_no)
        if err:
            stats["file_errors"] += 1
            continue
        stats["files_scanned"] += 1
        stats["keyword_hosts"] += len(found)
        hosts.update(found)
        print(f"cc: file {i + 1}/{len(my_files)} "
              f"hosts={len(found)} running={len(hosts)}", flush=True)
    print(f"cc: scan done: {stats}", flush=True)
    if not hosts:
        print("cc: no keyword hosts found", flush=True)
        if stats["file_errors"]:
            print("cc: scan had errors and found nothing - "
                  "likely blocked; exiting non-zero", flush=True)
            return 1
        return 0

    # Homepage classification, threaded. Each worker sleeps `delay`
    # between its own fetches; a host is fetched once, so per-host
    # politeness holds by design.
    delay = args.delay

    def _classify(host):
        result = classify_host(host)
        time.sleep(delay)
        return host, result

    counts = {"in_scope": 0, "no_location": 0, "not_agency": 0,
              "unreachable": 0, "excluded_pk_in": 0}
    classified = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.workers) as pool:
        for host, result in pool.map(_classify, sorted(hosts)):
            status = result[0]
            counts[status] = counts.get(status, 0) + 1
            if status == "in_scope":
                classified.append((host,) + result[1:])
    print(f"cc: classified={len(hosts)} {counts}", flush=True)

    seen_domains = set()
    if args.seen_domains_file:
        with open(args.seen_domains_file, encoding="utf-8") as fh:
            for line in fh:
                d = line.strip().lower()
                if d:
                    seen_domains.add(d)
    else:
        for tab_range in ("Directory!I:I", "Queue!B:B"):
            for row in sheets.read_values(tab_range)[1:]:
                if row:
                    d = domain_of(row[0])
                    if d:
                        seen_domains.add(d)
    print(f"cc: seen domains in Sheet: {len(seen_domains)}", flush=True)

    rows, build_stats = build_directory_rows(
        classified, seen_domains, args.max_new, now)
    print(f"cc: {build_stats}", flush=True)

    appended = 0
    if rows and not args.dry_run:
        for i in range(0, len(rows), APPEND_CHUNK):
            sheets.append_values("Directory!A:P", rows[i:i + APPEND_CHUNK])
        appended = len(rows)
    elif rows:
        print("cc: DRY RUN - would append "
              f"{len(rows)} rows, counts only (no lead data in logs)",
              flush=True)

    summary = (f"crawl={crawl} shard={args.shard}/{args.shards} "
               f"files_scanned={stats['files_scanned']} "
               f"file_errors={stats['file_errors']} "
               f"keyword_hosts={stats['keyword_hosts']} "
               f"classified={len(hosts)} "
               f"in_scope={counts['in_scope']} "
               f"no_location={counts['no_location']} "
               f"not_agency={counts['not_agency']} "
               f"unreachable={counts['unreachable']} "
               f"excluded_pk_in={counts['excluded_pk_in']} "
               f"new={build_stats['new']} "
               f"already_known={build_stats['already_known']} "
               f"dup_in_batch={build_stats['dup_in_batch']} "
               f"appended={appended}")
    print(f"cc tick done: {summary}", flush=True)
    if not args.dry_run and not args.no_log:
        try:
            sheets.append_values(
                "Log!A:D", [[now, "cc-extract", summary, appended]])
        except Exception:
            pass
    if stats["keyword_hosts"] == 0 and stats["file_errors"] > 0:
        print("cc: nothing matched and there were scan errors - "
              "likely blocked; exiting non-zero", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

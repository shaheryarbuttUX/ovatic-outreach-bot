"""New-business watcher: company registries -> NewBusiness + Directory.

Why this exists: CT logs catch businesses the day their website goes
live. Registries catch them earlier, the day they incorporate, even
before they have a website. The two sources cover different moments
of the same birth.

Sources:
1. Companies House, UK (works today, no key): the public search page
   at find-and-update.company-information.service.gov.uk returns real
   results with company number, name and incorporation date (verified
   2026-11-05). When a COMPANIES_HOUSE_API_KEY secret is present the
   official API is used instead: more reliable, paginated, structured.
2. OpenCorporates, US + EU (needs a free token): the public API now
   returns 401 without one (verified 2026-11-05), and its website is
   reCAPTCHA-gated, so without an OPENCORPORATES_TOKEN secret this
   source is honestly logged as skipped, never faked. A free token
   takes two minutes to create at opencorporates.com.

Domain rule, the important one: a company name is NEVER turned into
a guessed domain. A website is only accepted when it is verified:
Bing search finds a candidate AND the candidate homepage's own
title/H1 contains the company name, or a name-derived candidate
domain serves a homepage containing the company name. Unverified
companies still land in the NewBusiness tab (name, number, date,
town) for review, but they are NOT copied to Directory and never
reach Queue. Only verified-website rows go to Directory with
place_key "reg:<company_number>", where the existing Bridge picks
them up exactly like Maps rows.

Output tabs: NewBusiness (all new companies) and Directory (only
verified-website ones). Dedupe is against NewBusiness numbers +
Directory + Queue + Sent + Pulled. Counts only in logs, never lead
names or domains (public repo logs are public).

Usage in GitHub Actions:
  python scripts/new_business_registry.py --max-new 60 --days 90
"""

import argparse
import base64
import datetime
import html as html_mod
import json
import os
import re
import time
import urllib.parse
import urllib.request

NEWBUSINESS_HEADERS = [
    "company", "company_number", "incorporated", "status", "locality",
    "niche", "source", "website", "domain_verified", "found_at",
]
DIRECTORY_HEADERS = [
    "name", "category", "rating", "reviews", "address", "postcode",
    "town", "phone", "website", "hours", "maps_url", "place_key",
    "query", "locality", "niche", "scraped_at",
]

QUERIES = [
    ("web design agency", "web design agency"),
    ("marketing agency", "marketing agency"),
    ("branding agency", "branding agency"),
    ("digital agency", "digital agency"),
    ("seo agency", "SEO agency"),
    ("web development company", "web development company"),
]

CH_SEARCH = ("https://find-and-update.company-information.service.gov.uk"
             "/search/companies?q=")
CH_API = "https://api.company-information.service.gov.uk/search/companies"
OC_API = "https://api.opencorporates.com/v0.4/companies/search"
OC_JURISDICTIONS = ["us_ca", "us_ny", "us_tx", "us_fl", "gb", "de", "nl", "ie"]

BLOCK_DOMAINS = [
    "companieshouse", "company-information.service.gov.uk",
    "opencorporates", "linkedin.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "youtube.com", "crunchbase.com",
    "bizapedia", "dandb.com", "zoominfo", "wikipedia.org",
    "gov.uk", "find-and-update",
]

SUFFIX_WORDS = {"ltd", "limited", "plc", "llp", "llc", "inc", "corp",
                "company", "co", "group", "holdings", "uk"}


def http_get(url, timeout=25, headers=None):
    h = {"User-Agent": "Mozilla/5.0 ovatic-new-business"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def clean_text(t):
    return re.sub(r"\s+", " ", html_mod.unescape(t or "")).strip()


def parse_ch_date(s):
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.datetime.strptime(s.strip(), fmt).date()
        except Exception:
            continue
    return None


def ch_search_html(query):
    """Companies House public search, no key. Returns list of dicts."""
    try:
        page = http_get(CH_SEARCH + urllib.parse.quote(query))
    except Exception as e:
        print(f"companies-house search failed: {type(e).__name__}")
        return []
    links = re.findall(r'/company/(\d{8})[^>]*>(.*?)</a>', page, re.S)
    dates = re.findall(r'Incorporated on ([^<]+)', page)
    out = []
    for i, (num, raw_name) in enumerate(links):
        name = clean_text(re.sub(r"<[^>]+>", " ", raw_name))
        if not name or len(name) < 3:
            continue
        inc = parse_ch_date(dates[i]) if i < len(dates) else None
        out.append({"company": name, "number": num,
                    "incorporated": inc, "status": "Unknown",
                    "locality": "", "source": "companies-house-html"})
    return out


def ch_search_api(query, key):
    token = base64.b64encode(f"{key}:".encode()).decode()
    url = (CH_API + "?q=" + urllib.parse.quote(query)
           + "&items_per_page=20")
    try:
        page = http_get(url, headers={"Authorization": "Basic " + token})
        data = json.loads(page)
    except Exception as e:
        print(f"companies-house api failed: {type(e).__name__}")
        return []
    out = []
    for item in data.get("items", []):
        inc = None
        if item.get("date_of_creation"):
            try:
                inc = datetime.date.fromisoformat(item["date_of_creation"])
            except Exception:
                pass
        addr = item.get("address_snippet", "") or ""
        town = addr.split(",")[-2].strip() if addr.count(",") >= 1 else ""
        out.append({"company": clean_text(item.get("title", "")),
                    "number": item.get("company_number", ""),
                    "incorporated": inc,
                    "status": clean_text(item.get("company_status", "")),
                    "locality": town, "source": "companies-house-api"})
    return out


def oc_search(query, token, jurisdictions):
    """OpenCorporates API. Returns list of dicts, [] when no token."""
    out = []
    for juris in jurisdictions:
        url = (OC_API + "?q=" + urllib.parse.quote(query)
               + "&jurisdiction_code=" + juris
               + "&per_page=10&api_token=" + urllib.parse.quote(token))
        try:
            page = http_get(url)
            data = json.loads(page)
        except Exception as e:
            print(f"opencorporates {juris} failed: {type(e).__name__}")
            continue
        for item in (data.get("results", {}).get("companies", []) or []):
            c = item.get("company", {}) or {}
            inc = None
            if c.get("incorporation_date"):
                try:
                    inc = datetime.date.fromisoformat(
                        c["incorporation_date"])
                except Exception:
                    pass
            addr = c.get("registered_address_in_full", "") or ""
            town = addr.split(",")[-2].strip() if addr.count(",") >= 1 else ""
            out.append({"company": clean_text(c.get("name", "")),
                        "number": str(c.get("company_number", "")),
                        "incorporated": inc,
                        "status": clean_text(
                            (c.get("current_status") or "") or ""),
                        "locality": town,
                        "source": "opencorporates-" + juris})
        time.sleep(1)
    return out


def name_tokens(company):
    words = re.findall(r"[a-z0-9]+", company.lower())
    return [w for w in words if w not in SUFFIX_WORDS and len(w) > 1]


def homepage_matches_company(url, tokens):
    """Fetch a homepage; True only if its title/H1 holds the company."""
    try:
        page = http_get(url, timeout=15)
    except Exception:
        return False
    low = page.lower()
    title = ""
    m = re.search(r"<title>(.*?)</title>", low, re.S)
    if m:
        title = clean_text(re.sub(r"<[^>]+>", " ", m.group(1)))
    h1 = ""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", low, re.S)
    if m:
        h1 = clean_text(re.sub(r"<[^>]+>", " ", m.group(1)))
    hay = (title + " " + h1).lower()
    if len(tokens) >= 2:
        return tokens[0] in hay and tokens[1] in hay
    return bool(tokens) and tokens[0] in hay


def bing_candidates(company, town):
    """Bing HTML search for the company website. Best effort."""
    q = company + (" " + town if town else "") + " website"
    try:
        page = http_get("https://www.bing.com/search?q="
                        + urllib.parse.quote(q))
    except Exception:
        return []
    hrefs = re.findall(r'<a[^>]+href="(https?://[^"]+)"', page)
    out = []
    for href in hrefs:
        try:
            host = urllib.parse.urlparse(href).netloc.lower()
        except Exception:
            continue
        if not host or any(b in host for b in BLOCK_DOMAINS):
            continue
        if href not in out:
            out.append(href)
        if len(out) >= 5:
            break
    return out


def candidate_domains(tokens):
    base = "".join(tokens)
    dashed = "-".join(tokens)
    cands = []
    for stem in (base, dashed):
        if not stem:
            continue
        for tld in (".co.uk", ".com", ".uk", ".co"):
            cands.append(stem + tld)
    seen = set()
    uniq = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq[:6]


def find_verified_website(company, town):
    """Returns (website, how) or (None, None). Nothing guessed blind."""
    tokens = name_tokens(company)
    if not tokens:
        return None, None
    for cand in bing_candidates(company, town):
        host = urllib.parse.urlparse(cand).netloc.lower()
        url = "https://" + host
        if homepage_matches_company(url, tokens):
            return url, "bing-verified"
        time.sleep(0.5)
    for dom in candidate_domains(tokens):
        url = "https://" + dom
        if homepage_matches_company(url, tokens):
            return url, "candidate-verified"
        time.sleep(0.5)
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=60)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--verify-websites", action="store_true", default=True)
    ap.add_argument("--no-verify-websites", dest="verify_websites",
                    action="store_false")
    args = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date()
    cutoff = today - datetime.timedelta(days=args.days)
    ch_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    oc_token = os.environ.get("OPENCORPORATES_TOKEN", "").strip()

    found = {}  # (source, number) -> company dict
    ch_used = "api" if ch_key else "html"
    oc_used = bool(oc_token)
    for query, niche in QUERIES:
        rows = (ch_search_api(query, ch_key) if ch_key
                else ch_search_html(query))
        for r in rows:
            r["niche"] = niche
            key = (r["source"], r["number"])
            if r["number"] and key not in found:
                found[key] = r
        time.sleep(1)
    if oc_token:
        for query, niche in QUERIES[:4]:
            for r in oc_search(query, oc_token, OC_JURISDICTIONS):
                r["niche"] = niche
                key = (r["source"], r["number"])
                if r["number"] and key not in found:
                    found[key] = r
    else:
        print("opencorporates skipped: OPENCORPORATES_TOKEN secret not set")

    fresh = []
    for r in found.values():
        inc = r.get("incorporated")
        if inc and inc < cutoff:
            continue
        status = (r.get("status") or "").lower()
        if status and "dissolved" in status:
            continue
        fresh.append(r)
    print(f"registry scan done: found={len(found)} fresh={len(fresh)} "
          f"ch={ch_used} oc={'on' if oc_used else 'skipped'}")

    try:
        import sheets
        sheets.ensure_tab("NewBusiness", NEWBUSINESS_HEADERS)
        sheets.ensure_tab("Directory", DIRECTORY_HEADERS)
        existing = sheets.rows_to_dicts(
            sheets.read_values("NewBusiness!A:J"))
        directory = sheets.rows_to_dicts(sheets.read_values("Directory!A:P"))
        queue = sheets.rows_to_dicts(sheets.read_values("Queue!A:P"))
        sent = sheets.rows_to_dicts(sheets.read_values("Sent!A:H"))
        pulled = sheets.rows_to_dicts(sheets.read_values("Pulled!A:C"))
    except Exception as e:
        print(f"registry tick: sheet unavailable: {type(e).__name__}")
        return

    seen_numbers = {(r.get("source", ""), r.get("company_number", ""))
                    for r in existing}
    seen_keys = {((r.get("place_key", "") or "").strip().lower())
                 for r in directory + pulled}
    seen_domains = set()
    for row in directory + queue + sent:
        w = (row.get("website", "") or "").strip().lower()
        if w:
            seen_domains.add(w.replace("https://", "").replace("http://", "")
                             .replace("www.", "").strip("/"))

    nb_rows = []
    dir_rows = []
    verified = 0
    for r in fresh:
        key = (r["source"], r["number"])
        if key in seen_numbers:
            continue
        if len(nb_rows) >= args.max_new:
            break
        place_key = "reg:" + str(r["number"])
        website, how = (None, None)
        if args.verify_websites:
            website, how = find_verified_website(
                r["company"], r.get("locality", ""))
        inc = r.get("incorporated")
        nb_rows.append([
            r["company"], str(r["number"]),
            inc.isoformat() if inc else "",
            r.get("status", ""), r.get("locality", ""),
            r.get("niche", ""), r["source"], website or "",
            how or "not-found", now.isoformat(),
        ])
        if website:
            domain = urllib.parse.urlparse(website).netloc.lower().replace(
                "www.", "")
            if domain not in seen_domains and place_key not in seen_keys:
                dir_rows.append([
                    r["company"], r.get("niche", ""), "", "", "", "", "",
                    "", website, "", "", place_key,
                    f"registry:{r['source']}",
                    r.get("locality", ""),
                    r.get("niche", ""), now.isoformat(),
                ])
                seen_domains.add(domain)
                verified += 1

    if nb_rows:
        try:
            sheets.append_values("NewBusiness!A:J", nb_rows)
        except Exception as e:
            print(f"registry tick: NewBusiness append failed: {type(e).__name__}")
            return
    if dir_rows:
        try:
            sheets.append_values("Directory!A:P", dir_rows)
        except Exception as e:
            print(f"registry tick: Directory append failed: {type(e).__name__}")
    try:
        sheets.append_values("Log!A:D", [[
            now.isoformat(), "new-business-registry",
            f"found={len(found)} fresh={len(fresh)} added={len(nb_rows)} "
            f"verified_websites={verified} ch={ch_used} "
            f"oc={'on' if oc_used else 'skipped'}",
            len(nb_rows)]])
    except Exception:
        pass
    print(f"registry tick done: added={len(nb_rows)} "
          f"verified_websites={verified}")


if __name__ == "__main__":
    main()

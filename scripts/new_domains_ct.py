"""New-domain watcher: Certificate Transparency -> Directory.

Why this exists: the Maps queries are a finite list and keep running
dry. A domain that just got its first SSL certificate is a business
whose website just went live: the freshest possible lead, and the
domain itself IS the lead, so no Maps lookup is needed.

Sources, in order:
1. crt.sh (free, no key): wildcard cert search per keyword pattern.
   crt.sh is flaky (it returned 502 for every query on 2026-11-05,
   even for example.com), so every pattern is retried and a total
   outage is logged, not fatal: the run exits 0 and the next hourly
   run retries. Never fail the workflow on a crt.sh outage.
2. CertStream (free, no key, websocket): best-effort fallback, only
   when crt.sh yielded nothing. Unverified in the dev VM (the wss
   handshake timed out there); guarded by a short timeout so a dead
   fallback costs minutes, not the run.

Timeout lesson (2026-11-05): the first version scanned all patterns
and only saved at the end, so when crt.sh retries ate the 30-minute
workflow limit the run was CANCELLED and nothing was saved. This
version therefore (a) saves after EVERY pattern, (b) keeps retries
short (2 attempts, 15s timeout), (c) stops starting new patterns
once the time budget (default 22 min) is nearly gone, and (d) rotates
the pattern order by UTC hour so a slow crt.sh cannot starve the
same tail patterns on every run.

Freshness: a cert is only kept when its not_before is inside the
lookback window (default 48h). An RDAP check (capped, best effort)
then drops domains whose registration is older than 90 days, so a
renewed cert on a ten-year-old domain does not pose as new. An RDAP
failure never drops a row, it just keeps it.

Output: Directory rows with place_key "ct:<domain>", so the existing
Bridge picks them up exactly like Maps rows (form check, CAPTCHA
skip, Queue). Dedupe is against Directory + Queue + Sent + Pulled,
so a domain is never added twice. Counts only in logs, never lead
names or domains (public repo logs are public).

Usage in GitHub Actions:
  python scripts/new_domains_ct.py --max-new 100 --lookback-hours 48 --certstream-minutes 3
"""

import argparse
import datetime
import json
import re
import time
import urllib.parse
import urllib.request

DIRECTORY_HEADERS = [
    "name", "category", "rating", "reviews", "address", "postcode",
    "town", "phone", "website", "hours", "maps_url", "place_key",
    "query", "locality", "niche", "scraped_at",
]

# Narrow patterns: a bare "%.agency" query returns an unusable flood
# and crt.sh 502s on it. Keyword + TLD keeps each result set small.
PATTERNS = [
    "%agency.com", "%studio.com", "%design.com", "%marketing.com",
    "%branding.com", "%webdesign.com", "%digitalagency.com",
    "%seoagency.com", "%agency.co.uk", "%studio.co.uk",
    "%design.co.uk", "%agency.agency", "%studio.studio",
    "%design.design", "%agency.io", "%studio.io",
    "%agency.de", "%studio.de", "%agency.nl", "%design.nl",
]

KEYWORDS = [
    "agency", "studio", "design", "marketing", "branding", "seo",
    "webdesign", "web", "media", "digital", "creative",
]

SKIP_SUBSTRINGS = [
    "google", "amazon", "facebook", "instagram", "linkedin",
    "twitter", "x.com", "youtube", "microsoft", "apple",
    "cloudflare", "akamai", "fastly", "wix", "squarespace",
    "shopify", "wordpress", "godaddy", "namecheap", "clutch",
    "upwork", "fiverr", "github", "gitlab",
]

EU_TLDS = (".co.uk", ".uk", ".de", ".nl", ".ie", ".se", ".dk",
           ".ch", ".at", ".fr", ".es", ".it", ".be", ".pl", ".eu")


def fetch_json(url, timeout=15):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 ovatic-new-domains"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def fetch_crt_pattern(pattern, attempts=2):
    """One crt.sh pattern, retried briefly. Returns (entries, ok)."""
    url = ("https://crt.sh/?q=" + urllib.parse.quote(pattern)
           + "&output=json&exclude=expired")
    last_err = None
    for i in range(attempts):
        try:
            return fetch_json(url, timeout=15), True
        except Exception as e:
            last_err = e
            time.sleep(5 * (i + 1))
    print(f"crt pattern failed after {attempts} tries: {type(last_err).__name__}")
    return [], False


def apex_domain(name):
    name = (name or "").strip().lower()
    name = name.removeprefix("*.").removeprefix("www.")
    if not name or " " in name or "_" in name:
        return ""
    if name.endswith(".co.uk") or name.endswith(".org.uk"):
        parts = name.split(".")
        return name if len(parts) == 3 else ""
    parts = name.split(".")
    if len(parts) != 2:
        return ""
    if not re.fullmatch(r"[a-z0-9-]+", parts[0]):
        return ""
    if not re.fullmatch(r"[a-z]{2,}", parts[1]):
        return ""
    return name


def region_of(domain):
    return "EU" if domain.endswith(EU_TLDS) else "US"


def human_name(domain):
    base = domain.split(".")[0]
    return re.sub(r"[-_]+", " ", base).strip().title()


def rdap_created_days(domain, timeout=8):
    """Days since RDAP registration, or None when unknown/failed."""
    try:
        req = urllib.request.Request(
            "https://rdap.org/domain/" + domain,
            headers={"User-Agent": "Mozilla/5.0 ovatic-new-domains"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        for event in data.get("events", []):
            if event.get("eventAction") == "registration":
                dt = datetime.datetime.fromisoformat(
                    event["eventDate"].replace("Z", "+00:00"))
                delta = datetime.datetime.now(datetime.timezone.utc) - dt
                return delta.days
    except Exception:
        return None
    return None


def certstream_capture(minutes, keywords):
    """Best-effort live CT capture. Returns a set of apex domains."""
    found = set()
    try:
        import websocket
    except Exception:
        print("certstream skipped: websocket-client not installed")
        return found
    try:
        ws = websocket.create_connection(
            "wss://certstream.calidog.io/", timeout=10)
        ws.settimeout(10)
        end = time.time() + minutes * 60
        while time.time() < end:
            try:
                msg = json.loads(ws.recv())
            except Exception:
                break
            try:
                domains = msg.get("data", {}).get("leaf_cert", {}).get(
                    "all_domains", [])
            except Exception:
                continue
            for d in domains or []:
                apex = apex_domain(d)
                if apex and any(k in apex for k in keywords):
                    found.add(apex)
        ws.close()
    except Exception as e:
        print(f"certstream unavailable: {type(e).__name__}")
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=100)
    ap.add_argument("--lookback-hours", type=int, default=48)
    ap.add_argument("--certstream-minutes", type=int, default=3)
    ap.add_argument("--max-rdap-checks", type=int, default=40)
    ap.add_argument("--time-budget-minutes", type=int, default=22)
    args = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(hours=args.lookback_hours)
    started = time.time()
    budget = args.time_budget_minutes * 60

    def time_left():
        return budget - (time.time() - started)

    try:
        import sheets
        sheets.ensure_tab("Directory", DIRECTORY_HEADERS)
        directory = sheets.rows_to_dicts(sheets.read_values("Directory!A:P"))
        queue = sheets.rows_to_dicts(sheets.read_values("Queue!A:P"))
        sent = sheets.rows_to_dicts(sheets.read_values("Sent!A:H"))
        pulled = sheets.rows_to_dicts(sheets.read_values("Pulled!A:C"))
    except Exception:
        print("new-domains tick: sheet unavailable")
        return

    seen_domains = set()
    for row in directory + queue + sent:
        w = (row.get("website", "") or "").strip().lower()
        if w:
            seen_domains.add(w.replace("https://", "").replace("http://", "")
                             .replace("www.", "").strip("/"))
    seen_keys = {((row.get("place_key", "") or "").strip().lower())
                 for row in directory + pulled}

    state = {"added": 0, "rdap_checked": 0, "candidates": 0}

    def flush(cands):
        """Dedupe + RDAP + append one batch immediately. Returns added."""
        rows = []
        for domain, (nb, pattern, keyword) in sorted(cands.items()):
            if state["added"] + len(rows) >= args.max_new:
                break
            if domain in seen_domains or ("ct:" + domain) in seen_keys:
                continue
            if time_left() < 30:
                break
            if state["rdap_checked"] < args.max_rdap_checks:
                state["rdap_checked"] += 1
                days = rdap_created_days(domain)
                if days is not None and days > 90:
                    continue
                time.sleep(0.3)
            rows.append([
                human_name(domain), keyword, "", "", "", "", "",
                "", f"https://{domain}", "", "", f"ct:{domain}",
                f"crt.sh:{pattern}", region_of(domain), keyword,
                now.isoformat(),
            ])
        if not rows:
            return 0
        try:
            sheets.append_values("Directory!A:P", rows)
        except Exception as e:
            print(f"new-domains tick: append failed: {type(e).__name__}")
            return -1
        for r in rows:
            seen_domains.add(r[8].replace("https://", ""))
            seen_keys.add(r[11])
        state["added"] += len(rows)
        return len(rows)

    # Rotate pattern order by UTC hour so a slow crt.sh cannot starve
    # the same tail patterns on every run.
    start_idx = now.hour % len(PATTERNS)
    ordered = PATTERNS[start_idx:] + PATTERNS[:start_idx]
    patterns_ok = 0
    patterns_failed = 0
    for pattern in ordered:
        if state["added"] >= args.max_new:
            break
        if time_left() < 60:
            print("new-domains tick: time budget reached, stopping early")
            break
        entries, ok = fetch_crt_pattern(pattern)
        if not ok:
            patterns_failed += 1
            continue
        patterns_ok += 1
        cands = {}
        for entry in entries or []:
            try:
                nb = datetime.datetime.fromisoformat(
                    (entry.get("not_before") or "").replace("Z", "+00:00"))
            except Exception:
                continue
            if nb < cutoff:
                continue
            for raw in (entry.get("name_value") or "").split("\n"):
                apex = apex_domain(raw)
                if not apex:
                    continue
                if any(s in apex for s in SKIP_SUBSTRINGS):
                    continue
                hit = [k for k in KEYWORDS if k in apex]
                if not hit:
                    continue
                if apex not in cands:
                    cands[apex] = (nb, pattern, hit[0])
        state["candidates"] += len(cands)
        if flush(cands) < 0:
            break
        time.sleep(1)

    if (state["added"] == 0 and patterns_failed > 0
            and args.certstream_minutes > 0
            and time_left() > (args.certstream_minutes + 1) * 60):
        print("crt.sh yielded nothing, trying certstream fallback")
        cs = {}
        for apex in certstream_capture(args.certstream_minutes, KEYWORDS):
            if apex not in cs and not any(s in apex for s in SKIP_SUBSTRINGS):
                hit = [k for k in KEYWORDS if k in apex]
                cs[apex] = (now, "certstream", hit[0] if hit else "agency")
        state["candidates"] += len(cs)
        flush(cs)

    try:
        sheets.append_values("Log!A:D", [[
            now.isoformat(), "new-domains-ct",
            f"patterns_ok={patterns_ok} failed={patterns_failed} "
            f"candidates={state['candidates']} added={state['added']} "
            f"rdap_checked={state['rdap_checked']}",
            state["added"]]])
    except Exception:
        pass
    print(f"new-domains tick done: added={state['added']} "
          f"candidates={state['candidates']} "
          f"patterns_failed={patterns_failed}")


if __name__ == "__main__":
    main()

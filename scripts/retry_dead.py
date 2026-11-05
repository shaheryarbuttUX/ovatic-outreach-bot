"""Weekly retry sweep: revive fetch-dead Directory businesses.

Bridge marks a business pulled with outcome fetch_errors_repeated
after its homepage fetch failed on two earlier ticks, so dead sites
stop eating every tick. Some of those sites come back alive later
(server was down, a block expired). This script re-checks them once
a week, oldest first:

1. Pulled rows with outcome fetch_errors_repeated give the dead
   place_keys; Directory gives their websites.
2. Each homepage is fetched once (15s timeout). A 2xx/3xx answer
   means the site is alive again.
3. A revived site has its Pulled row cleared AND its FetchErrors
   rows cleared, so Bridge re-checks it on the next run. Clearing
   only the Pulled row would not work: Bridge's >=2-errors rule
   would bury it again immediately without fetching.
4. A still-dead site is left untouched, so it stays buried.

Domains already in Queue or Sent are never touched. Writes one Log
row (counts only). No lead data in repo commits or public logs.
"""
import argparse
import datetime
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sheets


def domain_of(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def fetch_alive(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=500)
    args = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        service, sheet_id = sheets._client()
        directory = sheets.rows_to_dicts(sheets.read_values("Directory!A:P"))
        queue = sheets.rows_to_dicts(sheets.read_values("Queue!A:P"))
        sent = sheets.rows_to_dicts(sheets.read_values("Sent!A:H"))
        pulled = sheets.rows_to_dicts(sheets.read_values("Pulled!A:C"))
        fetch_errors = sheets.rows_to_dicts(sheets.read_values("FetchErrors!A:C"))
    except Exception as e:
        print(f"retry-dead tick: sheet unavailable: {type(e).__name__}")
        return

    seen_domains = set()
    for row in queue + sent:
        d = domain_of(row.get("website", ""))
        if d:
            seen_domains.add(d)

    # place_key -> website for every Directory business.
    site_by_key = {}
    for row in directory:
        key = (row.get("place_key", "") or "").strip().lower()
        website = (row.get("website", "") or "").strip()
        if key and website:
            site_by_key[key] = website

    # Dead candidates, sheet order (oldest first), one per place_key.
    dead_keys = []
    seen_keys = set()
    for row in pulled:
        if (row.get("outcome", "") or "").strip() != "fetch_errors_repeated":
            continue
        key = (row.get("place_key", "") or "").strip().lower()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        website = site_by_key.get(key, "")
        if not website:
            continue
        if domain_of(website) in seen_domains:
            continue
        dead_keys.append(key)
    dead_keys = dead_keys[: args.max_new]

    # Pulled / FetchErrors row numbers to clear if the site revives.
    pulled_rows_by_key = {}
    for row in pulled:
        key = (row.get("place_key", "") or "").strip().lower()
        if key:
            pulled_rows_by_key.setdefault(key, []).append(row["_row"])
    error_rows_by_domain = {}
    for row in fetch_errors:
        d = (row.get("domain", "") or "").strip().lower()
        if d:
            error_rows_by_domain.setdefault(d, []).append(row["_row"])

    checked = 0
    revived = 0
    ranges_to_clear = []
    for key in dead_keys:
        website = site_by_key[key]
        domain = domain_of(website)
        checked += 1
        alive = fetch_alive("https://" + domain)
        print(f"retry-dead: {domain} alive={alive}", flush=True)
        if not alive:
            continue
        revived += 1
        for r in pulled_rows_by_key.get(key, []):
            ranges_to_clear.append(f"Pulled!A{r}:C{r}")
        for r in error_rows_by_domain.get(domain, []):
            ranges_to_clear.append(f"FetchErrors!A{r}:C{r}")

    if ranges_to_clear:
        try:
            service.spreadsheets().values().batchClear(
                spreadsheetId=sheet_id, body={"ranges": ranges_to_clear}
            ).execute()
        except Exception as e:
            print(f"retry-dead tick: clear failed: {type(e).__name__}")
            return

    try:
        sheets.append_values("Log!A:D", [[now.isoformat(), "retry-dead",
            f"checked={checked} revived={revived} still_dead={checked - revived}",
            revived]])
    except Exception:
        pass
    print(f"retry-dead tick done: checked={checked} revived={revived} "
          f"still_dead={checked - revived}")


if __name__ == "__main__":
    main()
